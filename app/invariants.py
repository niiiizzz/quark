"""
Lazarus Invariants Verification Module (app/invariants.py).
Evaluates invariants I1-I5 live from PRD Section 6 / Section 9 N1.
"""

from typing import Any, Dict, List
import psycopg
from psycopg.rows import dict_row

from app.config import settings

SQL_I1_JOB_LOSS = """
SELECT
  (SELECT value FROM counters WHERE name = 'acked') as acked,
  (SELECT count(*) FROM jobs) as total_jobs,
  (SELECT count(*) FROM jobs WHERE status = 'succeeded') as succeeded,
  (SELECT count(*) FROM jobs WHERE status = 'dead') as dead,
  (SELECT count(*) FROM jobs WHERE status = 'queued') as queued,
  (SELECT count(*) FROM jobs WHERE status = 'running') as running;
"""

SQL_I2_DUPLICATE_EFFECTS = """
SELECT count(*) as dup_count FROM (
  SELECT job_id FROM ledger GROUP BY job_id HAVING count(*) > 1
) t;
"""

SQL_I3_EXACTLY_ONCE_CREDITS = """
SELECT count(*) as violations FROM (
  SELECT j.id, count(l.id) as ledger_rows
  FROM jobs j
  LEFT JOIN ledger l ON l.job_id = j.id
  WHERE j.type = 'ledger_credit' AND j.status = 'succeeded'
  GROUP BY j.id
  HAVING count(l.id) != 1
) t;
"""

SQL_I4_NO_GHOST_EFFECTS = """
SELECT count(*) as ghost_count
FROM ledger l
JOIN jobs j ON j.id = l.job_id
WHERE j.status != 'succeeded';
"""

SQL_I5_NO_STUCK_LEASES = """
SELECT count(*) as stuck_count
FROM jobs
WHERE status = 'running'
  AND lease_expires_at < now() - make_interval(secs => %s::float8);
"""


async def evaluate_invariants(conn: psycopg.AsyncConnection) -> List[Dict[str, Any]]:
    """
    Evaluates the five core correctness invariants:
    - I1: No acknowledged job lost
    - I2: No duplicate effects
    - I3: Exactly-once for succeeded credits
    - I4: No ghost effects
    - I5: No stuck leases
    """
    results = []
    async with conn.cursor(row_factory=dict_row) as cur:
        # I1: No acknowledged job lost
        await cur.execute(SQL_I1_JOB_LOSS)
        r1 = await cur.fetchone()
        acked = r1["acked"] or 0
        total = r1["total_jobs"] or 0
        sum_states = (r1["succeeded"] or 0) + (r1["dead"] or 0) + (r1["queued"] or 0) + (r1["running"] or 0)
        i1_ok = (total == acked) and (total == sum_states)
        results.append({
            "id": "I1",
            "name": "No acknowledged job lost",
            "ok": bool(i1_ok),
            "value": f"{total} / {acked}",
            "detail": f"total={total}, acked={acked}, sum_states={sum_states}",
        })

        # I2: No duplicate effects
        await cur.execute(SQL_I2_DUPLICATE_EFFECTS)
        r2 = await cur.fetchone()
        dups = r2["dup_count"] or 0
        results.append({
            "id": "I2",
            "name": "No duplicate effects",
            "ok": (dups == 0),
            "value": str(dups),
            "detail": f"{dups} jobs with duplicate ledger rows",
        })

        # I3: Exactly-once for succeeded credits
        await cur.execute(SQL_I3_EXACTLY_ONCE_CREDITS)
        r3 = await cur.fetchone()
        i3_viols = r3["violations"] or 0
        results.append({
            "id": "I3",
            "name": "Exactly-once credits",
            "ok": (i3_viols == 0),
            "value": f"{i3_viols} violations",
            "detail": f"{i3_viols} succeeded ledger_credit jobs with ledger count != 1",
        })

        # I4: No ghost effects
        await cur.execute(SQL_I4_NO_GHOST_EFFECTS)
        r4 = await cur.fetchone()
        ghosts = r4["ghost_count"] or 0
        results.append({
            "id": "I4",
            "name": "No ghost effects",
            "ok": (ghosts == 0),
            "value": str(ghosts),
            "detail": f"{ghosts} ledger rows for non-succeeded jobs",
        })

        # I5: No stuck leases (running jobs with lease_expires_at < now() - 2*LEASE)
        stuck_threshold_secs = settings.lease_seconds * 2.0
        await cur.execute(SQL_I5_NO_STUCK_LEASES, (stuck_threshold_secs,))
        r5 = await cur.fetchone()
        stuck = r5["stuck_count"] or 0
        results.append({
            "id": "I5",
            "name": "No stuck leases",
            "ok": (stuck == 0),
            "value": str(stuck),
            "detail": f"{stuck} jobs running with lease expired > {stuck_threshold_secs}s ago",
        })

    return results
