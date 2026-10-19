"""
Lazarus Core Queue Module (app/queue.py).
Strictly implements PRD Section 6 SQL contract using raw SQL with psycopg 3.
All post-claim writes are fenced. Leases use database now().
"""

import json
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID
import psycopg
from psycopg.rows import dict_row

# -----------------------------------------------------------------------------
# SQL CONTRACT CONSTANTS (PRD Section 6)
# -----------------------------------------------------------------------------

SQL_ENQUEUE = """
INSERT INTO jobs (tenant_id, type, payload, idempotency_key, priority, run_at, max_attempts)
VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s::float8), %s)
ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
RETURNING *;
"""

SQL_SELECT_BY_IDEMPOTENCY = """
SELECT * FROM jobs
WHERE tenant_id = %s AND idempotency_key = %s;
"""

SQL_INC_COUNTER_ACKED = """
INSERT INTO counters (name, value) VALUES ('acked', 1)
ON CONFLICT (name) DO UPDATE SET value = counters.value + 1;
"""

SQL_CLAIM = """
WITH picked AS (
  SELECT id FROM jobs
  WHERE status = 'queued' AND run_at <= now()
    AND type NOT IN (SELECT job_type FROM breakers WHERE open_until > now())
  ORDER BY priority DESC, run_at
  FOR UPDATE SKIP LOCKED
  LIMIT %s
)
UPDATE jobs j
SET status='running', lease_owner=%s,
    lease_expires_at = now() + make_interval(secs => %s::float8),
    attempts = attempts + 1, fence = fence + 1, started_at = now()
FROM picked WHERE j.id = picked.id
RETURNING j.*;
"""

SQL_BATCH_HEARTBEAT = """
UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %s::float8)
WHERE lease_owner = %s AND status = 'running'
RETURNING id, fence;
"""

SQL_COMPLETE_SAFE = """
UPDATE jobs SET status='succeeded', result=%s, finished_at=now(), duration_ms=%s,
                lease_owner=NULL, lease_expires_at=NULL
WHERE id=%s AND fence=%s AND status='running';
"""

SQL_COMPLETE_UNSAFE = """
UPDATE jobs SET status='succeeded', result=%s, finished_at=now(), duration_ms=%s,
                lease_owner=NULL, lease_expires_at=NULL
WHERE id=%s;
"""

SQL_INSERT_LEDGER = """
INSERT INTO ledger (job_id, tenant_id, account, amount)
VALUES (%s, %s, %s, %s)
RETURNING id;
"""

SQL_FAIL = """
UPDATE jobs SET
  status = CASE WHEN %s::boolean OR attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
  run_at = now() + make_interval(secs => %s::float8),
  last_error=%s, last_fp=%s, error_class=%s,
  finished_at = CASE WHEN %s::boolean OR attempts >= max_attempts THEN now() END,
  lease_owner=NULL, lease_expires_at=NULL
WHERE id=%s AND fence=%s AND status='running'
RETURNING id, status, attempts, max_attempts;
"""

SQL_REAPER = """
UPDATE jobs SET
  crash_count = crash_count + 1,
  quarantined = (crash_count + 1 >= %s::int),
  status = CASE WHEN crash_count + 1 >= %s::int OR attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
  run_at = now() + make_interval(secs => random() * LEAST(%s::float8, %s::float8 * power(2, attempts))),
  last_error = CASE WHEN crash_count + 1 >= %s::int
                    THEN 'quarantined: worker died silently ' || (crash_count + 1)::text || 'x'
                    ELSE 'lease expired (worker lost)' END,
  error_class = 'lease_expired',
  finished_at = CASE WHEN crash_count + 1 >= %s::int OR attempts >= max_attempts THEN now() END,
  lease_owner=NULL, lease_expires_at=NULL
WHERE id IN (
  SELECT id FROM jobs WHERE status='running' AND lease_expires_at < now()
  FOR UPDATE SKIP LOCKED LIMIT 100
)
RETURNING id, status, crash_count, quarantined, type;
"""

SQL_REPLAY_ONE = """
UPDATE jobs SET status='queued', attempts=0, crash_count=0, quarantined=false,
                run_at=now(), finished_at=NULL, lease_owner=NULL, lease_expires_at=NULL
WHERE id=%s AND status='dead'
RETURNING *;
"""

SQL_REPLAY_ALL = """
UPDATE jobs SET status='queued', attempts=0, crash_count=0, quarantined=false,
                run_at=now(), finished_at=NULL, lease_owner=NULL, lease_expires_at=NULL
WHERE status='dead'
RETURNING id;
"""

SQL_INC_COUNTER = """
INSERT INTO counters (name, value) VALUES (%s, %s)
ON CONFLICT (name) DO UPDATE SET value = counters.value + %s;
"""

SQL_LOG_EVENT = """
INSERT INTO job_events (job_id, job_type, event, worker, attempt, detail)
VALUES (%s, %s, %s, %s, %s, %s);
"""

# -----------------------------------------------------------------------------
# CORE TRANSITION FUNCTIONS
# -----------------------------------------------------------------------------


async def enqueue(
    conn: psycopg.AsyncConnection,
    tenant_id: str,
    job_type: str,
    payload: Dict[str, Any],
    idempotency_key: Optional[str] = None,
    priority: int = 0,
    delay_seconds: float = 0.0,
    max_attempts: int = 5,
) -> Tuple[Dict[str, Any], bool]:
    """
    Idempotent enqueue. Returns (job_row, is_created).
    If duplicate idempotency_key, returns (existing_job_row, False).
    Increments counters.acked only on insert.
    """
    payload_json = json.dumps(payload)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            SQL_ENQUEUE,
            (tenant_id, job_type, payload_json, idempotency_key, priority, delay_seconds, max_attempts),
        )
        row = await cur.fetchone()
        if row:
            await cur.execute(SQL_INC_COUNTER_ACKED)
            await cur.execute(
                SQL_LOG_EVENT,
                (row["id"], job_type, "enqueued", None, 0, json.dumps({"priority": priority})),
            )
            return row, True

        # Duplicate: retrieve existing
        await cur.execute(SQL_SELECT_BY_IDEMPOTENCY, (tenant_id, idempotency_key))
        existing = await cur.fetchone()
        return existing, False


async def claim(
    conn: psycopg.AsyncConnection,
    worker_id: str,
    batch_size: int,
    lease_seconds: float,
) -> List[Dict[str, Any]]:
    """
    Claim jobs with FOR UPDATE SKIP LOCKED, lease extension, and fence++.
    Skips types whose breaker is currently open.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(SQL_CLAIM, (batch_size, worker_id, lease_seconds))
        rows = await cur.fetchall()
        for r in rows:
            await cur.execute(
                SQL_LOG_EVENT,
                (r["id"], r["type"], "claimed", worker_id, r["attempts"], json.dumps({"fence": r["fence"]})),
            )
        return rows


async def batch_heartbeat(
    conn: psycopg.AsyncConnection,
    worker_id: str,
    lease_seconds: float,
) -> List[Tuple[UUID, int]]:
    """
    Extend leases for all running jobs owned by worker_id.
    Returns list of (id, fence) currently active.
    """
    async with conn.cursor() as cur:
        await cur.execute(SQL_BATCH_HEARTBEAT, (lease_seconds, worker_id))
        rows = await cur.fetchall()
        return [(r[0], r[1]) for r in rows]


class StaleWriteError(Exception):
    """Raised when a fenced state write matches 0 rows (stale worker / zombie)."""
    pass


async def complete(
    conn: psycopg.AsyncConnection,
    job_id: UUID,
    fence: int,
    result: Optional[Dict[str, Any]] = None,
    duration_ms: Optional[int] = None,
    effects: Optional[List[Dict[str, Any]]] = None,
    fencing_enabled: bool = True,
    worker_id: Optional[str] = None,
    job_type: Optional[str] = None,
) -> bool:
    """
    Atomically commit effect (ledger insert) and job completion in ONE transaction.
    If fencing_enabled=True: checks (id, fence, status='running').
      If rowcount == 0, rolls back the transaction (discarding effects),
      then raises StaleWriteError so caller can record stale_writes_rejected in a new tx.
    If fencing_enabled=False: matches WHERE id=$1 with no fence check (unsafe demo mode).
    """
    result_json = json.dumps(result or {})
    async with conn.transaction():
        async with conn.cursor() as cur:
            # 1) Execute effects in the same transaction
            if effects:
                for eff in effects:
                    await cur.execute(
                        SQL_INSERT_LEDGER,
                        (job_id, eff.get("tenant_id"), eff.get("account"), eff.get("amount")),
                    )

            # 2) Complete the job
            if fencing_enabled:
                await cur.execute(SQL_COMPLETE_SAFE, (result_json, duration_ms, job_id, fence))
            else:
                await cur.execute(SQL_COMPLETE_UNSAFE, (result_json, duration_ms, job_id))

            matched = cur.rowcount

            if matched == 0 and fencing_enabled:
                # Stale write detected: trigger rollback by raising exception
                raise StaleWriteError(f"Stale write rejected for job {job_id} with fence {fence}")

            # Record success event in same tx
            await cur.execute(
                SQL_LOG_EVENT,
                (job_id, job_type, "succeeded", worker_id, None, result_json),
            )

    return True


async def record_stale_write(
    conn: psycopg.AsyncConnection,
    job_id: UUID,
    fence: int,
    worker_id: Optional[str] = None,
    job_type: Optional[str] = None,
) -> None:
    """Record stale write rejection counter and event in a separate transaction."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(SQL_INC_COUNTER, ("stale_writes_rejected", 1, 1))
            await cur.execute(
                SQL_LOG_EVENT,
                (
                    job_id,
                    job_type,
                    "stale_write_rejected",
                    worker_id,
                    None,
                    json.dumps({"attempted_fence": fence}),
                ),
            )


async def fail(
    conn: psycopg.AsyncConnection,
    job_id: UUID,
    fence: int,
    error_text: str,
    is_permanent: bool,
    backoff_seconds: float,
    fingerprint: Optional[str] = None,
    error_class: str = "transient",
    worker_id: Optional[str] = None,
    job_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Fenced failure transition. Requeues with backoff or moves to 'dead'.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            SQL_FAIL,
            (is_permanent, backoff_seconds, error_text, fingerprint, error_class, is_permanent, job_id, fence),
        )
        row = await cur.fetchone()
        if row:
            event_name = "dead" if row["status"] == "dead" else "failed"
            await cur.execute(
                SQL_LOG_EVENT,
                (
                    job_id,
                    job_type,
                    event_name,
                    worker_id,
                    row["attempts"],
                    json.dumps({"error": error_text, "fp": fingerprint, "class": error_class}),
                ),
            )
        return row


async def reap(
    conn: psycopg.AsyncConnection,
    backoff_cap: float,
    backoff_base: float,
    poison_threshold: int = 3,
) -> List[Dict[str, Any]]:
    """
    Reaper control loop step. Reclaims expired leases.
    Tracks crash_count; quarantines at poison_threshold.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(SQL_REAPER, (poison_threshold, poison_threshold, backoff_cap, backoff_base, poison_threshold, poison_threshold))
        rows = await cur.fetchall()
        for r in rows:
            await cur.execute(SQL_INC_COUNTER, ("lease_expired", 1, 1))
            if r["quarantined"]:
                await cur.execute(SQL_INC_COUNTER, ("quarantined", 1, 1))
                await cur.execute(
                    SQL_LOG_EVENT,
                    (r["id"], r.get("type"), "quarantined", None, None, json.dumps({"crash_count": r["crash_count"]})),
                )
            else:
                await cur.execute(
                    SQL_LOG_EVENT,
                    (r["id"], r.get("type"), "lease_expired", None, None, json.dumps({"crash_count": r["crash_count"]})),
                )
        return rows


async def replay_one(conn: psycopg.AsyncConnection, job_id: UUID) -> Optional[Dict[str, Any]]:
    """Replay a single dead job."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(SQL_REPLAY_ONE, (job_id,))
        row = await cur.fetchone()
        if row:
            await cur.execute(
                SQL_LOG_EVENT,
                (job_id, row.get("type"), "replayed", None, 0, json.dumps({"action": "single"})),
            )
        return row


async def replay_all(conn: psycopg.AsyncConnection) -> int:
    """Replay all dead jobs. Returns count of replayed jobs."""
    async with conn.cursor() as cur:
        await cur.execute(SQL_REPLAY_ALL)
        rows = await cur.fetchall()
        for r in rows:
            await cur.execute(
                SQL_LOG_EVENT,
                (r[0], None, "replayed", None, 0, json.dumps({"action": "all"})),
            )
        return len(rows)
