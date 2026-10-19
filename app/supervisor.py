"""
Lazarus Supervisor Module (app/supervisor.py).
Implements PRD W1, C6, N4, N5:
- Spawns and manages N worker OS processes (python -m app.worker.main).
- Reads 'desired_workers' from settings table (clamped 1-8, default 3).
- Restarts dead workers within ~3-4s with new worker_id.
- Single control loop: runs reaper every 2s, evaluates circuit breakers, computes forecast.
- Handles autoscale if autoscale='auto' mode is active.
"""

import asyncio
import os
import sys
import time
from typing import Dict, List, Optional
import psycopg
from psycopg.rows import dict_row

# Enforce selector event loop on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.config import settings
from app.db import create_pool
from app.queue import reap
from app.breaker import evaluate_breaker, get_all_breakers
from app.forecast import compute_recommendation


class Supervisor:
    def __init__(self):
        self.workers: Dict[str, asyncio.subprocess.Process] = {}
        self.worker_names: Dict[str, str] = {}
        self.worker_start_times: Dict[str, float] = {}
        self.pool = None
        self.running = True
        self.desired_workers = 3
        self.last_autoscale_time = 0.0

    async def start(self):
        self.pool = create_pool(min_size=1, max_size=3)
        await self.pool.open()

        # Launch control loop
        control_task = asyncio.create_task(self.control_loop())
        # Launch worker management loop
        workers_task = asyncio.create_task(self.worker_management_loop())

        await asyncio.gather(control_task, workers_task)

    async def control_loop(self):
        """Runs reaper, breaker checks, and forecast recommendation every REAPER_INTERVAL."""
        while self.running:
            try:
                await asyncio.sleep(settings.reaper_interval)
                async with self.pool.connection() as conn:
                    # 1) Run reaper: reclaim expired leases, check quarantine
                    await reap(
                        conn=conn,
                        backoff_cap=settings.backoff_cap,
                        backoff_base=settings.backoff_base,
                        poison_threshold=settings.poison_threshold,
                    )

                    # 2) Evaluate circuit breakers for all standard job types
                    for jtype in ("flaky_webhook", "legacy_sync", "ledger_credit"):
                        await evaluate_breaker(conn, jtype)

                    # 3) Check autoscale if in 'auto' mode (10s cooldown)
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute("SELECT value FROM settings WHERE key = 'autoscale'")
                        row = await cur.fetchone()
                        mode = row["value"] if row else "manual"

                    if mode == "auto" and (time.monotonic() - self.last_autoscale_time >= 10.0):
                        rec = await compute_recommendation(conn)
                        rec_workers = rec["recommended_workers"]
                        if rec_workers != self.desired_workers:
                            async with conn.cursor() as cur:
                                await cur.execute(
                                    "UPDATE settings SET value = %s WHERE key = 'desired_workers'",
                                    (str(rec_workers),),
                                )
                            await conn.commit()
                            self.last_autoscale_time = time.monotonic()

                    await conn.commit()

            except asyncio.CancelledError:
                break
            except Exception as e:
                pass

    async def worker_management_loop(self):
        """Monitors OS processes, restarts dead ones, and adjusts worker count."""
        worker_counter = 1
        while self.running:
            try:
                # 1) Read desired_workers from DB
                async with self.pool.connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute("SELECT value FROM settings WHERE key = 'desired_workers'")
                        row = await cur.fetchone()
                        if row:
                            self.desired_workers = max(1, min(8, int(row["value"])))

                # 2) Check for terminated processes
                for wid, proc in list(self.workers.items()):
                    if proc.returncode is not None:
                        # Process died
                        del self.workers[wid]
                        # Mark as dead in DB
                        async with self.pool.connection() as conn:
                            async with conn.cursor() as cur:
                                await cur.execute("UPDATE workers SET status = 'dead' WHERE worker_id = %s", (wid,))
                            await conn.commit()

                current_count = len(self.workers)

                # 3) Scale up if under desired
                if current_count < self.desired_workers:
                    for _ in range(self.desired_workers - current_count):
                        name = f"worker-{worker_counter}"
                        worker_id = f"{name}-{os.urandom(2).hex()}"
                        worker_counter += 1

                        cmd = [sys.executable, "-m", "app.worker.main", worker_id, name]
                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        self.workers[worker_id] = proc
                        self.worker_names[worker_id] = name
                        self.worker_start_times[worker_id] = time.monotonic()

                # 4) Scale down if above desired
                elif current_count > self.desired_workers:
                    excess = current_count - self.desired_workers
                    to_remove = list(self.workers.keys())[:excess]
                    for wid in to_remove:
                        proc = self.workers.pop(wid)
                        proc.terminate()
                        async with self.pool.connection() as conn:
                            async with conn.cursor() as cur:
                                await cur.execute("UPDATE workers SET status = 'dead' WHERE worker_id = %s", (wid,))
                            await conn.commit()

                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                await asyncio.sleep(1.0)


if __name__ == "__main__":
    supervisor = Supervisor()
    asyncio.run(supervisor.start())
