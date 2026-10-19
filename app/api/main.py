"""
Lazarus FastAPI Application & Routes (app/api/main.py, app/api/routes.py).
Strictly implements:
- PRD Section 7 API contract.
- Approved additions: POST /api/chaos/run/stop, GET /api/events?worker=, reset guard.
- 02-architecture-workflow Section 5 (/api/overview contract).
- Serves static dashboard with vendored assets.
"""

import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from typing import Any, Dict, List, Optional
from uuid import UUID
from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import psycopg
from psycopg.rows import dict_row

from app.config import settings
from app.db import get_connection, close_pool
from app.queue import (
    enqueue,
    replay_one,
    replay_all,
)
from app.invariants import evaluate_invariants
from app.breaker import get_all_breakers
from app.forecast import compute_recommendation


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_pool()


app = FastAPI(title="Lazarus API", lifespan=lifespan)

# Mount static files
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/healthz")
async def healthz():
    try:
        async with get_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1;")
                await cur.fetchone()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database unavailable: {str(e)}")


# -----------------------------------------------------------------------------
# PYDANTIC SCHEMAS
# -----------------------------------------------------------------------------

class EnqueueRequest(BaseModel):
    tenant_id: str
    type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = None
    priority: int = 0
    delay_seconds: float = 0.0


class ScaleRequest(BaseModel):
    desired: int


class SettingsUpdateRequest(BaseModel):
    fencing_enabled: Optional[bool] = None
    outage_flaky_webhook: Optional[bool] = None
    autoscale: Optional[str] = None


from app.api.chaos import active_chaos_run, run_chaos_orchestration
import asyncio


class ChaosRunRequest(BaseModel):
    jobs: int = 300
    tenants: int = 3
    jobs_per_sec: int = 15
    kill_every_s: int = 8
    freeze: bool = True
    noisy_tenant: bool = False
    mix: Optional[Dict[str, int]] = None


class PoisonRequest(BaseModel):
    count: int = 1


# -----------------------------------------------------------------------------
# CORE QUEUE & JOB ROUTES (PRD §7)
# -----------------------------------------------------------------------------

@app.post("/api/jobs")
async def api_enqueue(req: EnqueueRequest, response: Response):
    try:
        async with get_connection() as conn:
            job, is_created = await enqueue(
                conn=conn,
                tenant_id=req.tenant_id,
                job_type=req.type,
                payload=req.payload,
                idempotency_key=req.idempotency_key,
                priority=req.priority,
                delay_seconds=req.delay_seconds,
                max_attempts=settings.max_attempts,
            )
            await conn.commit()

            if is_created:
                response.status_code = status.HTTP_201_CREATED
            else:
                response.status_code = status.HTTP_200_OK
            return job
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/jobs")
async def api_list_jobs(
    status_filter: Optional[str] = Query(None, alias="status"),
    job_type: Optional[str] = Query(None, alias="type"),
    tenant: Optional[str] = Query(None, alias="tenant"),
    limit: int = 50,
):
    query = "SELECT * FROM jobs WHERE 1=1"
    params = []
    if status_filter:
        query += " AND status = %s"
        params.append(status_filter)
    if job_type:
        query += " AND type = %s"
        params.append(job_type)
    if tenant:
        query += " AND tenant_id = %s"
        params.append(tenant)

    query += " ORDER BY created_at DESC LIMIT %s;"
    params.append(limit)

    async with get_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, tuple(params))
            return await cur.fetchall()


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: UUID):
    async with get_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM jobs WHERE id = %s;", (job_id,))
            job = await cur.fetchone()
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")

            await cur.execute(
                "SELECT * FROM job_events WHERE job_id = %s ORDER BY at ASC;", (job_id,)
            )
            events = await cur.fetchall()
            return {"job": job, "events": events}


@app.post("/api/jobs/{job_id}/replay")
async def api_replay_job(job_id: UUID):
    async with get_connection() as conn:
        row = await replay_one(conn, job_id)
        await conn.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Job not dead or not found")
        return row


@app.post("/api/dlq/replay-all")
async def api_replay_all():
    async with get_connection() as conn:
        cnt = await replay_all(conn)
        await conn.commit()
        return {"replayed_count": cnt}


# -----------------------------------------------------------------------------
# WORKERS & SUPERVISOR (PRD §7)
# -----------------------------------------------------------------------------

@app.get("/api/workers")
async def api_list_workers():
    async with get_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT worker_id, name, pid, started_at, last_seen, in_flight, command, status,
                       EXTRACT(EPOCH FROM (now() - started_at))::int as uptime_s,
                       (EXTRACT(EPOCH FROM (now() - last_seen)) * 1000)::int as last_seen_ms
                FROM workers
                WHERE last_seen >= now() - interval '10 seconds' OR status != 'dead'
                ORDER BY name ASC;
                """
            )
            return await cur.fetchall()


@app.post("/api/workers/{worker_id}/kill")
async def api_kill_worker(worker_id: str):
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE workers SET command = 'kill' WHERE worker_id = %s;", (worker_id,))
        await conn.commit()
    return {"status": "kill_queued", "worker_id": worker_id}


@app.post("/api/workers/{worker_id}/freeze")
async def api_freeze_worker(worker_id: str):
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE workers SET command = 'freeze' WHERE worker_id = %s;", (worker_id,))
        await conn.commit()
    return {"status": "freeze_queued", "worker_id": worker_id}


@app.post("/api/workers/scale")
async def api_scale_workers(req: ScaleRequest):
    desired = max(1, min(8, req.desired))
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE settings SET value = %s WHERE key = 'desired_workers';", (str(desired),))
        await conn.commit()
    return {"desired_workers": desired}


@app.post("/api/settings")
async def api_update_settings(req: SettingsUpdateRequest):
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            if req.fencing_enabled is not None:
                await cur.execute("UPDATE settings SET value = %s WHERE key = 'fencing_enabled';", (str(req.fencing_enabled).lower(),))
            if req.outage_flaky_webhook is not None:
                await cur.execute("UPDATE settings SET value = %s WHERE key = 'outage_flaky_webhook';", (str(req.outage_flaky_webhook).lower(),))
            if req.autoscale is not None:
                await cur.execute("UPDATE settings SET value = %s WHERE key = 'autoscale';", (req.autoscale,))
        await conn.commit()
    return {"status": "updated"}


# -----------------------------------------------------------------------------
# OVERVIEW & INSIGHTS (02 §5, PRD §7, §9)
# -----------------------------------------------------------------------------

@app.get("/api/overview")
async def api_overview():
    async with get_connection() as conn:
        # Settings
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT key, value FROM settings;")
            s_rows = await cur.fetchall()
            settings_map = {r["key"]: r["value"] for r in s_rows}

            # KPIs
            await cur.execute(
                """
                SELECT
                  count(*) as total,
                  count(*) FILTER (WHERE status = 'running') as processing,
                  count(*) FILTER (WHERE status = 'queued' AND attempts > 0) as retrying,
                  count(*) FILTER (WHERE status = 'dead') as dlq,
                  count(*) FILTER (WHERE status = 'succeeded') as succeeded
                FROM jobs;
                """
            )
            kpis = await cur.fetchone()

            # Counters
            await cur.execute("SELECT name, value FROM counters;")
            c_rows = await cur.fetchall()
            counters_map = {r["name"]: r["value"] for r in c_rows}

            # Throughput last 60s
            await cur.execute(
                """
                WITH seconds AS (
                  SELECT generate_series(
                    date_trunc('second', now()) - interval '59 seconds',
                    date_trunc('second', now()),
                    interval '1 second'
                  ) as s
                )
                SELECT
                  to_char(sec.s, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as t,
                  count(e.id) FILTER (WHERE e.event = 'succeeded') as ok,
                  count(e.id) FILTER (WHERE e.event IN ('failed', 'dead')) as failed
                FROM seconds sec
                LEFT JOIN job_events e ON date_trunc('second', e.at) = sec.s
                GROUP BY sec.s
                ORDER BY sec.s ASC;
                """
            )
            throughput = await cur.fetchall()

            # Recent events (15 latest)
            await cur.execute(
                """
                SELECT to_char(at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as at,
                       event, job_id, job_type, worker, detail
                FROM job_events
                ORDER BY at DESC
                LIMIT 15;
                """
            )
            events = await cur.fetchall()

        # Invariants
        invariants = await evaluate_invariants(conn)

        # Breakers
        breakers = await get_all_breakers(conn)

        # Workers
        workers = await api_list_workers()

        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "settings": {
                "fencing_enabled": settings_map.get("fencing_enabled", "true") == "true",
                "desired_workers": int(settings_map.get("desired_workers", "3")),
                "outage_flaky_webhook": settings_map.get("outage_flaky_webhook", "false") == "true",
                "autoscale": settings_map.get("autoscale", "manual"),
            },
            "kpis": kpis,
            "throughput": throughput,
            "invariants": invariants,
            "counters": counters_map,
            "breakers": breakers,
            "workers": workers,
            "events": events,
        }


@app.get("/api/insights")
async def api_insights():
    async with get_connection() as conn:
        rec = await compute_recommendation(conn)
        breakers = await get_all_breakers(conn)

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT job_type, fp, successes, failures,
                       (successes + failures) as n,
                       round(((1.0 + successes) / (2.0 + successes + failures))::numeric, 3) as p_success,
                       CASE WHEN (successes + failures) >= 8 AND ((1.0 + successes) / (2.0 + successes + failures)) < 0.15
                            THEN 'learned-permanent' ELSE 'retry' END as verdict
                FROM retry_stats
                ORDER BY job_type, fp;
                """
            )
            retry_stats = await cur.fetchall()

        return {
            "recommendation": rec,
            "breakers": breakers,
            "retry_stats": retry_stats,
        }


@app.get("/api/events")
async def api_get_events(worker: Optional[str] = Query(None)):
    query = "SELECT * FROM job_events"
    params = []
    if worker:
        query += " WHERE worker = %s"
        params.append(worker)
    query += " ORDER BY at DESC LIMIT 50;"

    async with get_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, tuple(params))
            return await cur.fetchall()


# -----------------------------------------------------------------------------
# CHAOS LAB ENDPOINTS (PRD §7, §9 N1, N2, N6, Approved additions)
# -----------------------------------------------------------------------------

@app.post("/api/chaos/run")
async def api_chaos_run(req: ChaosRunRequest):
    if active_chaos_run.state == "running":
        raise HTTPException(status_code=409, detail="A chaos run is already in progress")

    asyncio.create_task(
        run_chaos_orchestration(
            jobs_count=req.jobs,
            tenants_count=req.tenants,
            jobs_per_sec=req.jobs_per_sec,
            kill_every_s=req.kill_every_s,
            freeze_enabled=req.freeze,
            job_mix=req.mix,
        )
    )
    return {"status": "started", "run_id": active_chaos_run.run_id}


@app.get("/api/chaos/run/latest")
async def api_chaos_latest():
    return {
        "run_id": active_chaos_run.run_id,
        "state": active_chaos_run.state,
        "total_jobs": active_chaos_run.total_jobs,
        "enqueued": active_chaos_run.enqueued,
        "acknowledged": active_chaos_run.acknowledged,
        "kills_sent": active_chaos_run.kills_sent,
        "freezes_sent": active_chaos_run.freezes_sent,
        "report_card": active_chaos_run.report_card,
    }


@app.post("/api/chaos/run/stop")
async def api_chaos_stop():
    active_chaos_run._stop_requested = True
    return {"status": "stopping"}


@app.post("/api/chaos/poison")
async def api_chaos_poison(req: PoisonRequest):
    async with get_connection() as conn:
        for _ in range(max(1, req.count)):
            await enqueue(
                conn=conn,
                tenant_id="tenant-poison",
                job_type="poison_pill",
                payload={},
                priority=10,
            )
        await conn.commit()
    return {"status": "poison_injected", "count": req.count}


@app.post("/api/chaos/reset")
async def api_chaos_reset():
    # Hard Rule 3: Guard demo reset - refuse unless LAZARUS_ALLOW_RESET=1
    if not settings.lazarus_allow_reset:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Reset refused: LAZARUS_ALLOW_RESET is not enabled.",
        )

    async with get_connection() as conn:
        async with conn.cursor() as cur:
            # Truncates only jobs, job_events, ledger, counters (preserves settings)
            await cur.execute("TRUNCATE TABLE job_events, ledger, jobs RESTART IDENTITY;")
            await cur.execute(
                """
                UPDATE counters SET value = 0;
                DELETE FROM retry_stats;
                UPDATE breakers SET open_until = NULL, opened_count = 0;
                UPDATE workers SET command = NULL, in_flight = 0 WHERE status != 'dead';
                """
            )
        await conn.commit()
    return {"status": "reset_complete"}

