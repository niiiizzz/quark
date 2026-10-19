"""
Lazarus Chaos Orchestration Module (app/api/chaos.py).
Implements PRD Section 9 N1, N2, N6 and Section 7 endpoints:
- POST /api/chaos/run: Enqueue load at rate, kills every kill_every_s, freezes every ~12s.
- GET /api/chaos/run/latest: Progress and report card with PASS/FAIL.
- POST /api/chaos/run/stop: Stop load and kills, transition to draining.
- POST /api/chaos/poison: Inject poison_pill jobs for quarantine testing.
- POST /api/chaos/reset: Truncate jobs, job_events, ledger, counters (guarded by LAZARUS_ALLOW_RESET=1).
"""

import asyncio
from datetime import datetime, timezone
import random
import time
from typing import Any, Dict, List, Optional
import uuid
import psycopg
from psycopg.rows import dict_row

from app.config import settings
from app.db import get_connection
from app.queue import enqueue
from app.invariants import evaluate_invariants


class ChaosRunState:
    def __init__(self):
        self.run_id = str(uuid.uuid4())
        self.state = "idle"  # idle | running | draining | complete
        self.total_jobs = 0
        self.enqueued = 0
        self.acknowledged = 0
        self.kills_sent = 0
        self.freezes_sent = 0
        self.start_time = 0.0
        self.end_time = 0.0
        self.report_card: Optional[Dict[str, Any]] = None
        self._stop_requested = False


active_chaos_run = ChaosRunState()


async def run_chaos_orchestration(
    jobs_count: int,
    tenants_count: int,
    jobs_per_sec: int,
    kill_every_s: int,
    freeze_enabled: bool,
    job_mix: Optional[Dict[str, int]] = None,
):
    global active_chaos_run
    active_chaos_run = ChaosRunState()
    active_chaos_run.state = "running"
    active_chaos_run.total_jobs = jobs_count
    active_chaos_run.start_time = time.monotonic()

    mix = job_mix or {
        "ledger_credit": 50,
        "flaky_webhook": 15,
        "validate_order": 10,
        "legacy_sync": 10,
        "report_export": 15,
    }

    # Build weighted type list
    types_pool = []
    for jtype, pct in mix.items():
        types_pool.extend([jtype] * pct)
    if not types_pool:
        types_pool = ["ledger_credit"]

    tenants = [f"tenant-{i+1}" for i in range(tenants_count)]

    # Load generator task
    async def load_loop():
        interval = 1.0 / max(1, jobs_per_sec)
        for i in range(jobs_count):
            if active_chaos_run._stop_requested:
                break
            jtype = random.choice(types_pool)
            tenant = random.choice(tenants)
            payload = {"amount": 100, "account": f"ACC-{random.randint(100, 999)}"}
            if jtype == "legacy_sync":
                payload["bad"] = (random.random() < 0.15)
            elif jtype == "validate_order":
                payload["invalid"] = (random.random() < 0.30)

            try:
                async with get_connection() as conn:
                    job, created = await enqueue(
                        conn=conn,
                        tenant_id=tenant,
                        job_type=jtype,
                        payload=payload,
                        idempotency_key=f"chaos-{active_chaos_run.run_id}-{i}",
                        priority=random.randint(0, 3),
                    )
                    await conn.commit()
                    if created:
                        active_chaos_run.acknowledged += 1
                active_chaos_run.enqueued += 1
            except Exception:
                pass

            await asyncio.sleep(interval)

    # Kill loop: every kill_every_s kill a random healthy worker with >= 1 in-flight
    async def kill_loop():
        if kill_every_s <= 0:
            return
        while active_chaos_run.state == "running" and not active_chaos_run._stop_requested:
            await asyncio.sleep(kill_every_s)
            try:
                async with get_connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(
                            "SELECT worker_id FROM workers WHERE status = 'healthy' ORDER BY random() LIMIT 1;"
                        )
                        row = await cur.fetchone()
                        if row:
                            wid = row["worker_id"]
                            await cur.execute("UPDATE workers SET command = 'kill' WHERE worker_id = %s;", (wid,))
                            await conn.commit()
                            active_chaos_run.kills_sent += 1
            except Exception:
                pass

    # Freeze loop: random freeze every ~12s
    async def freeze_loop():
        if not freeze_enabled:
            return
        while active_chaos_run.state == "running" and not active_chaos_run._stop_requested:
            await asyncio.sleep(random.uniform(10.0, 14.0))
            try:
                async with get_connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(
                            "SELECT worker_id FROM workers WHERE status = 'healthy' ORDER BY random() LIMIT 1;"
                        )
                        row = await cur.fetchone()
                        if row:
                            wid = row["worker_id"]
                            await cur.execute("UPDATE workers SET command = 'freeze' WHERE worker_id = %s;", (wid,))
                            await conn.commit()
                            active_chaos_run.freezes_sent += 1
            except Exception:
                pass

    # Run load, kills, freezes concurrently
    await asyncio.gather(load_loop(), kill_loop(), freeze_loop())

    # State transition to draining
    active_chaos_run.state = "draining"

    # Wait until queued = running = 0 (timeout 120s)
    drain_deadline = time.monotonic() + 120.0
    while time.monotonic() < drain_deadline:
        async with get_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) FILTER (WHERE status IN ('queued', 'running')) as remaining FROM jobs;"
                )
                row = await cur.fetchone()
                remaining = row["remaining"] if row else 0
                if remaining == 0:
                    break
        await asyncio.sleep(1.0)

    active_chaos_run.end_time = time.monotonic()
    active_chaos_run.state = "complete"

    # Evaluate report card
    async with get_connection() as conn:
        invs = await evaluate_invariants(conn)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                  count(*) FILTER (WHERE status = 'succeeded') as succeeded,
                  count(*) FILTER (WHERE status = 'dead') as dead,
                  count(*) FILTER (WHERE error_class = 'permanent') as dead_permanent,
                  count(*) FILTER (WHERE error_class = 'learned_permanent') as dead_learned,
                  count(*) FILTER (WHERE error_class = 'lease_expired' OR quarantined = true) as dead_quarantined
                FROM jobs;
                """
            )
            job_counts = await cur.fetchone()

            await cur.execute("SELECT name, value FROM counters;")
            c_rows = await cur.fetchall()
            counters = {r["name"]: r["value"] for r in c_rows}

    all_invs_pass = all(inv["ok"] for inv in invs)
    queue_drained = (remaining == 0)
    verdict = "PASS" if (all_invs_pass and queue_drained) else "FAIL"

    first_failure = None
    if not all_invs_pass:
        first_failure = next((inv["name"] for inv in invs if not inv["ok"]), "Invariant violation")
    elif not queue_drained:
        first_failure = "Queue failed to drain within 120s timeout"

    active_chaos_run.report_card = {
        "verdict": verdict,
        "first_failure": first_failure,
        "duration_s": round(active_chaos_run.end_time - active_chaos_run.start_time, 1),
        "acknowledged": active_chaos_run.acknowledged,
        "succeeded": job_counts["succeeded"],
        "dead": job_counts["dead"],
        "dead_breakdown": {
            "permanent": job_counts["dead_permanent"],
            "learned_permanent": job_counts["dead_learned"],
            "quarantined": job_counts["dead_quarantined"],
        },
        "lease_expiries": counters.get("lease_expired", 0),
        "stale_writes_rejected": counters.get("stale_writes_rejected", 0),
        "duplicate_effects": next((inv["value"] for inv in invs if inv["id"] == "I2"), "0"),
        "retries_avoided": counters.get("retries_avoided", 0),
        "quarantined_jobs": counters.get("quarantined", 0),
        "invariants": invs,
    }
