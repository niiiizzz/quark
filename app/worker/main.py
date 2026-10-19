"""
Lazarus Worker Process Main Loop (app/worker/main.py).
Implements PRD W1-W5, C2-C5, C9:
- Distinct OS process per worker.
- Pull-based concurrency semaphore.
- Batch heartbeat every HEARTBEAT_SECONDS.
- Lease-lost detection: cancels in-flight handler if lease was lost.
- Command channel: responds to 'kill' (os._exit(137)) and 'freeze' (blocking sleep).
- SIGTERM graceful drain: stops claiming, waits up to 10s, releases leftovers to 'queued'.
- Fenced atomic completion with ledger effects.
"""

import asyncio
import os
import signal
import sys
import time
from typing import Any, Dict, Optional, Set
from uuid import UUID
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

# Enforce selector event loop on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.config import settings
from app.db import create_pool
from app.queue import claim, batch_heartbeat, complete, fail, record_stale_write, StaleWriteError
from app.retry import compute_backoff, extract_fingerprint, classify_error_prior, classify_and_record_retry
from app.worker.handlers import HANDLERS, HandlerContext


class Worker:
    def __init__(self, worker_id: str, name: str):
        self.worker_id = worker_id
        self.name = name
        self.pid = os.getpid()
        self.pool: Optional[AsyncConnectionPool] = None
        self.running = True
        self.draining = False
        self.in_flight_tasks: Dict[UUID, asyncio.Task] = {}
        self.in_flight_fences: Dict[UUID, int] = {}
        self.in_flight_jobs: Dict[UUID, Dict[str, Any]] = {}
        self.semaphore = asyncio.Semaphore(settings.worker_concurrency)
        self.cached_settings: Dict[str, str] = {}
        self.last_settings_read = 0.0

    async def start(self):
        self.pool = create_pool(min_size=1, max_size=settings.worker_concurrency + 2)
        await self.pool.open()

        # Register worker in DB
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO workers (worker_id, name, pid, started_at, last_seen, in_flight, status)
                    VALUES (%s, %s, %s, now(), now(), 0, 'healthy')
                    ON CONFLICT (worker_id) DO UPDATE
                    SET pid = %s, last_seen = now(), status = 'healthy';
                    """,
                    (self.worker_id, self.name, self.pid, self.pid),
                )
            await conn.commit()

        # Set up shutdown signals
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))
        else:
            # On Windows, register console break / interrupt handlers
            signal.signal(signal.SIGINT, lambda s, f: asyncio.create_task(self.shutdown()))
            signal.signal(signal.SIGTERM, lambda s, f: asyncio.create_task(self.shutdown()))

        # Start background heartbeat and claim loops
        heartbeat_task = asyncio.create_task(self.heartbeat_loop())
        claim_task = asyncio.create_task(self.claim_loop())

        await asyncio.gather(heartbeat_task, claim_task)

    async def refresh_settings(self, conn: psycopg.AsyncConnection):
        now = time.monotonic()
        if now - self.last_settings_read >= 1.0:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT key, value FROM settings;")
                rows = await cur.fetchall()
                self.cached_settings = {r["key"]: r["value"] for r in rows}
            self.last_settings_read = now

    async def heartbeat_loop(self):
        """Batch heartbeat + command channel checks every HEARTBEAT_SECONDS."""
        while self.running:
            try:
                await asyncio.sleep(settings.heartbeat_seconds)
                async with self.pool.connection() as conn:
                    # 1) Heartbeat DB command channel
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(
                            """
                            UPDATE workers
                            SET last_seen = now(), in_flight = %s, status = %s
                            WHERE worker_id = %s
                            RETURNING command;
                            """,
                            (len(self.in_flight_tasks), "healthy" if not self.draining else "draining", self.worker_id),
                        )
                        row = await cur.fetchone()
                        cmd = row["command"] if row else None

                        if cmd:
                            # Clear command in DB
                            await cur.execute("UPDATE workers SET command = NULL WHERE worker_id = %s", (self.worker_id,))
                            await conn.commit()

                            if cmd == "kill":
                                # PRD W2: os._exit(137) with no cleanup; lease is NOT released
                                os._exit(137)
                            elif cmd == "freeze":
                                # PRD W3: Blocking sleep LEASE * 1.8; blocks whole event loop
                                freeze_duration = settings.lease_seconds * 1.8
                                # Mark as frozen in DB before blocking
                                await cur.execute("UPDATE workers SET status = 'frozen' WHERE worker_id = %s", (self.worker_id,))
                                await conn.commit()
                                time.sleep(freeze_duration)
                                # Wakes up and continues
                                await cur.execute("UPDATE workers SET status = 'healthy' WHERE worker_id = %s", (self.worker_id,))
                                await conn.commit()

                    # 2) Batch heartbeat for all in-flight jobs
                    active_pairs = await batch_heartbeat(conn, self.worker_id, settings.lease_seconds)
                    await conn.commit()

                    fencing_on = self.cached_settings.get("fencing_enabled", "true").lower() == "true"
                    if fencing_on:
                        active_set = set(active_pairs)
                        # Cancel any in-flight task whose (id, fence) was not renewed (lease lost / stolen)
                        for job_id, task in list(self.in_flight_tasks.items()):
                            fence = self.in_flight_fences.get(job_id)
                            if (job_id, fence) not in active_set and not task.done():
                                task.cancel()

            except asyncio.CancelledError:
                break
            except Exception as e:
                # Log heartbeat failure, keep trying
                pass

    async def claim_loop(self):
        """Continuous pull-based claim loop."""
        while self.running and not self.draining:
            try:
                # Wait for capacity
                await self.semaphore.acquire()

                async with self.pool.connection() as conn:
                    await self.refresh_settings(conn)
                    # Claim 1 job up to concurrency
                    claimed = await claim(conn, self.worker_id, batch_size=1, lease_seconds=settings.lease_seconds)
                    await conn.commit()

                if not claimed:
                    self.semaphore.release()
                    await asyncio.sleep(settings.poll_interval)
                    continue

                for job in claimed:
                    job_id = job["id"]
                    fence = job["fence"]
                    self.in_flight_fences[job_id] = fence
                    self.in_flight_jobs[job_id] = job
                    task = asyncio.create_task(self.run_job(job))
                    self.in_flight_tasks[job_id] = task

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.semaphore.release()
                await asyncio.sleep(1.0)

    async def run_job(self, job: Dict[str, Any]):
        job_id = job["id"]
        fence = job["fence"]
        job_type = job["type"]
        start_time = time.monotonic()

        try:
            handler = HANDLERS.get(job_type)
            if not handler:
                raise ValueError(f"Unknown job type: {job_type}")

            ctx = HandlerContext(job, self.cached_settings)
            result = await handler(ctx)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            # Completion
            fencing_on = self.cached_settings.get("fencing_enabled", "true").lower() == "true"
            async with self.pool.connection() as conn:
                try:
                    await complete(
                        conn=conn,
                        job_id=job_id,
                        fence=fence,
                        result=result,
                        duration_ms=duration_ms,
                        effects=ctx.effects,
                        fencing_enabled=fencing_on,
                        worker_id=self.worker_id,
                        job_type=job_type,
                    )
                except StaleWriteError:
                    # Stale write caught!
                    await record_stale_write(conn, job_id, fence, self.worker_id, job_type)

        except asyncio.CancelledError:
            # Lease was lost or task cancelled
            pass
        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            fp = extract_fingerprint(exc)
            is_perm_rule, err_class_rule = classify_error_prior(exc)

            async with self.pool.connection() as conn:
                # Check circuit breaker
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT open_until > now() FROM breakers WHERE job_type = %s",
                        (job_type,),
                    )
                    b_row = await cur.fetchone()
                    breaker_open = bool(b_row and b_row[0])

                # Check statistical learning
                is_perm_stat, err_class_stat, _ = await classify_and_record_retry(
                    conn, job_type, fp, is_success=False, breaker_is_open=breaker_open
                )

                is_permanent = is_perm_rule or is_perm_stat
                error_class = err_class_stat if is_perm_stat else err_class_rule
                delay = 0.0 if is_permanent else compute_backoff(job.get("attempts", 0), settings.backoff_base, settings.backoff_cap)

                await fail(
                    conn=conn,
                    job_id=job_id,
                    fence=fence,
                    error_text=str(exc)[:500],
                    is_permanent=is_permanent,
                    backoff_seconds=delay,
                    fingerprint=fp,
                    error_class=error_class,
                    worker_id=self.worker_id,
                    job_type=job_type,
                )
                await conn.commit()
        finally:
            self.in_flight_tasks.pop(job_id, None)
            self.in_flight_fences.pop(job_id, None)
            self.in_flight_jobs.pop(job_id, None)
            self.semaphore.release()

    async def shutdown(self):
        """PRD C9: Graceful SIGTERM drain."""
        if self.draining:
            return
        self.draining = True
        self.running = False

        # Wait up to 10s for in-flight tasks
        drain_deadline = time.monotonic() + 10.0
        while self.in_flight_tasks and time.monotonic() < drain_deadline:
            await asyncio.sleep(0.2)

        # Cancel remaining and release to queued
        if self.in_flight_tasks:
            for task in self.in_flight_tasks.values():
                task.cancel()

            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    for job_id, fence in self.in_flight_fences.items():
                        await cur.execute(
                            """
                            UPDATE jobs
                            SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL
                            WHERE id = %s AND fence = %s AND status = 'running';
                            """,
                            (job_id, fence),
                        )
                await conn.commit()

        # Update worker record to dead
        if self.pool:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("UPDATE workers SET status = 'dead' WHERE worker_id = %s", (self.worker_id,))
                await conn.commit()
            await self.pool.close()


if __name__ == "__main__":
    import uuid
    worker_id = sys.argv[1] if len(sys.argv) > 1 else f"worker-{uuid.uuid4().hex[:6]}"
    name = sys.argv[2] if len(sys.argv) > 2 else "worker"
    worker = Worker(worker_id, name)
    asyncio.run(worker.start())
