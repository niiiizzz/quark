# Lazarus — Reliable Asynchronous Job Runner

> **Team Quark · Prototype Build**  
> Proves live, in a 3-minute demo, that no acknowledged job is lost and no effect is applied twice — under worker kills, freezes, and downstream outages.

---

## Table of Contents

- [Overview](#overview)
- [Core Claims](#core-claims)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Job Lifecycle](#job-lifecycle)
- [How Reliability Works](#how-reliability-works)
  - [Idempotent Enqueue](#1-idempotent-enqueue)
  - [Fenced Completion](#2-fenced-completion-no-double-effects)
  - [Lease and Reaper](#3-lease-and-reaper-no-lost-jobs)
  - [Circuit Breaker](#4-circuit-breaker-no-retry-storms)
  - [Poison Quarantine](#5-poison-quarantine-silent-crashes-caught)
  - [Statistical Retry Classifier](#6-statistical-retry-classifier)
- [Live Dashboard and Chaos Lab](#live-dashboard-and-chaos-lab)
- [Project Structure](#project-structure)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Running Tests](#running-tests)
- [Prototype vs Production](#prototype-vs-production)
- [Database Schema](#database-schema)
- [Tech Stack](#tech-stack)

---

## Overview

Lazarus is a production-shaped asynchronous job queue built on **PostgreSQL 16** with a live web dashboard. It proves three claims:

| Claim | How it is proved |
|---|---|
| **No job is lost** | Every acknowledged job appears in the invariants panel; `acked` counter always equals total job count |
| **No effect applied twice** | Ledger inserts and job completion happen in one atomic transaction, gated by a per-job fence token |
| **Failures are contained** | Circuit breakers isolate bad job types; a learned retry policy dead-letters permanently-failing jobs early |

The proof is not documentation — it runs **live** in the dashboard while workers are being killed and frozen.

---

## Core Claims

These five invariants are evaluated every second against the live database and displayed in the **Invariants panel**:

```
I1  Acknowledged jobs  =  Succeeded + Dead + Queued + Running  (no job is ever lost)
I2  Duplicate ledger rows for any job_id                 = 0   (no double effects)
I3  Succeeded ledger_credit jobs with ledger count != 1  = 0   (exactly-once credits)
I4  Ledger rows for non-succeeded jobs                   = 0   (no ghost effects)
I5  Running jobs with expired lease > 2x LEASE ago       = 0   (no stuck leases)
```

---

## Quick Start

### Option A — Without Docker (local PostgreSQL)

**Prerequisites:** Python 3.12+, PostgreSQL 16 running on port 5432.

```bash
# 1. Clone and install dependencies
git clone <repo-url>
cd lazarus
pip install -r requirements.txt

# 2. Configure the database URL
cp .env.example .env
# Edit .env: set DATABASE_URL=postgresql://user:pass@localhost:5432/lazarus

# 3. Apply the schema
psql -U postgres -d lazarus -f schema.sql

# 4. Start everything (API + Supervisor + Workers)
python run_all.py
```

Open **http://localhost:8000** in your browser.

### Option B — With Docker Compose

```bash
docker compose up --build
```

Starts PostgreSQL 16 and the application in a single command. Open **http://localhost:8000**.

---

### 3-Minute Demo Walkthrough

| Time | Action | What you see |
|---|---|---|
| `0:00` | Open dashboard | Idle system, healthy workers, all 5 invariants green |
| `0:20` | Chaos Lab → **Start Chaos Run** | Throughput chart moves, KPI cards update live |
| `1:00` | Watch Workers table | A worker flips to `DEAD`, supervisor restarts it, orphaned jobs reclaimed |
| `1:30` | Invariants panel | All still green; `stale_writes_rejected` is non-zero (zombies caught) |
| `2:00` | Report card | **PASS** — acknowledged = accounted, duplicate effects = 0 |
| `2:15` | Toggle **Fencing OFF**, rerun chaos | **FAIL** — duplicate ledger rows appear, invariants turn red |
| `2:45` | Insights tab | Learned retry verdict, breaker open during outage, forecast |
| `3:15` | DLQ tab → **Replay All** | Dead jobs requeue and complete |

---

## Architecture

```mermaid
flowchart LR
  subgraph BROWSER["Browser"]
    UI["Dashboard\nHTML + CSS + Alpine.js + Chart.js"]
  end
  subgraph HOST["App host / single container"]
    API["API process\nFastAPI + uvicorn\nREST + static files"]
    CHAOS["Chaos orchestrator\nasync task inside API"]
    SUP["Supervisor process\nworker lifecycle + control loop"]
    subgraph WP["Worker processes 1..8"]
      W1["worker-1"]
      W2["worker-2"]
      WN["worker-N"]
    end
  end
  PG[("PostgreSQL 16\nlocal or Azure Flexible Server")]
  DOWN["Downstream services\nflaky_webhook, legacy_sync"]

  UI -->|"poll GET /api/overview every 1s\nPOST actions"| API
  API --> CHAOS
  API -->|"enqueue, reads, settings, worker commands"| PG
  CHAOS -->|"enqueue load, kill and freeze commands"| PG
  SUP -->|"spawn, restart"| WP
  SUP -->|"reaper, breakers, forecast"| PG
  WP -->|"claim, heartbeat, fenced commit"| PG
  WP --> DOWN
```

### Component Responsibilities

| Component | Owns | Never does |
|---|---|---|
| **API** | Enqueue (idempotent), queries, settings, worker commands, chaos runs, serve dashboard | Run job handlers |
| **Supervisor** | Worker process lifecycle, reaper loop, breaker evaluation, forecast | Serve HTTP |
| **Worker** | Claim, heartbeat, run handlers, fenced commit, obey `kill`/`freeze` | Decide retry policy alone |
| **PostgreSQL** | All durable state — leases, fence tokens, effects ledger, counters | Anything else; it is the single source of truth |
| **Dashboard** | Rendering, button actions | Hold authoritative state; re-reads everything each second |

### Process Map

```
python run_all.py
├── app.api.server        →  uvicorn :8000  (REST + dashboard + chaos task)
└── app.supervisor        →  control loop every 2s
    ├── reaper            →  reclaims expired leases, evaluates quarantine
    ├── breaker eval      →  opens / probes circuit breakers
    ├── forecast          →  Holt-Winters smoothing, worker recommendation
    └── worker processes x N
        ├── claim loop    →  FOR UPDATE SKIP LOCKED, lease, fence++
        ├── heartbeat     →  extends lease every 2s, checks kill/freeze command
        └── handlers      →  ledger_credit, flaky_webhook, validate_order, ...
```

---

## Job Lifecycle

```mermaid
stateDiagram-v2
  [*] --> queued : enqueue (idempotent, ON CONFLICT DO NOTHING)
  queued --> running : claim (lease, fence++)
  running --> succeeded : fenced complete (effect + status in one tx)
  running --> queued : transient failure or lease expiry (backoff + jitter)
  running --> dead : permanent or learned-permanent or attempts exhausted or quarantined
  dead --> queued : replay
  succeeded --> [*]
```

### Happy-Path Sequence

```mermaid
sequenceDiagram
  autonumber
  participant P as Producer / Chaos Lab
  participant A as API
  participant D as PostgreSQL
  participant W as Worker

  P->>A: POST /api/jobs {tenant, type, payload, idempotency_key}
  A->>D: INSERT ... ON CONFLICT DO NOTHING RETURNING
  D-->>A: new row, or none for a duplicate
  A->>D: counters.acked++ (new rows only)
  A-->>P: 201 created, or 200 existing

  loop every POLL_INTERVAL while capacity free
    W->>D: CLAIM (SKIP LOCKED, lease, fence++)
    D-->>W: job row with fence = f
  end

  par handler runs
    W->>W: execute job handler
  and lease kept alive
    W->>D: batch heartbeat every 2s
  end

  W->>D: BEGIN; INSERT ledger; UPDATE jobs WHERE fence = f; COMMIT
  D-->>W: rowcount 1 = success, rowcount 0 = ROLLBACK (stale fence)
```

---

## How Reliability Works

### 1. Idempotent Enqueue

```sql
INSERT INTO jobs (tenant_id, type, payload, idempotency_key, ...)
VALUES (...)
ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
RETURNING *;
```

The same `(tenant_id, idempotency_key)` pair always returns the same job row. The `acked` counter increments only on actual insert. Callers get `201 Created` or `200 OK` — safe to retry the HTTP call.

---

### 2. Fenced Completion (no double effects)

Every claim increments a per-job `fence` counter. Completion requires both the ledger write and job status update to succeed atomically:

```sql
-- Both succeed together or neither does
INSERT INTO ledger (job_id, tenant_id, account, amount) VALUES (...);

UPDATE jobs
  SET status = 'succeeded', finished_at = now(), ...
WHERE id     = $job_id
  AND fence  = $my_fence      -- must match the fence we hold
  AND status = 'running';     -- must still be running
```

If `rowcount = 0` (fence mismatch — we are a zombie), the transaction rolls back entirely. The ledger row is discarded. `stale_writes_rejected` is incremented in a new transaction.

**Toggling Fencing OFF** skips the fence check — running the chaos demo with fencing off demonstrates duplicate charges in real time and turns invariants I2 and I3 red.

#### Frozen Worker (Zombie) Sequence

```mermaid
sequenceDiagram
  autonumber
  participant Z as Worker Z (frozen)
  participant D as PostgreSQL
  participant R as Reaper
  participant W2 as Worker 2

  Z->>D: CLAIM job J (fence = 1)
  Note over Z: freeze command — event loop blocked for LEASE x 1.8, no heartbeats

  R->>D: lease expired, requeue J (crash_count++)
  W2->>D: CLAIM J (fence = 2)
  W2->>D: BEGIN; INSERT ledger; UPDATE WHERE fence = 2; COMMIT OK

  Z->>Z: wakes, handler returns

  alt Fencing ON (safe)
    Z->>D: BEGIN; INSERT ledger; UPDATE WHERE fence = 1
    D-->>Z: 0 rows, ROLLBACK, stale_writes_rejected++
  else Fencing OFF (unsafe demo)
    Z->>D: BEGIN; INSERT ledger; UPDATE WHERE id = J; COMMIT
    Note over D: two ledger rows for job J, invariants I2 and I3 turn RED
  end
```

---

### 3. Lease and Reaper (no lost jobs)

Workers hold a time-bounded lease renewed every `HEARTBEAT_SECONDS`. If a worker dies without releasing its lease, the reaper reclaims it:

```mermaid
sequenceDiagram
  autonumber
  participant U as Operator
  participant A as API
  participant D as PostgreSQL
  participant W1 as Worker 1
  participant R as Reaper
  participant W2 as Worker 2
  participant S as Supervisor

  U->>A: POST /workers/{id}/kill
  A->>D: workers.command = kill
  W1->>D: heartbeat reads command
  W1--xW1: os._exit(137), lease NOT released

  Note over D: job stays running until lease_expires_at passes (at most 6s)

  R->>D: REAPER UPDATE (crash_count++, requeue with jitter backoff)
  W2->>D: CLAIM (new fence = f+1)
  W2->>D: fenced complete, success

  S->>S: detects process exit
  S->>W1: spawn replacement after about 3s (new worker_id)
```

The reaper uses `FOR UPDATE SKIP LOCKED` so multiple supervisors cannot double-reap — safe for multi-node topology in Round 3:

```sql
UPDATE jobs SET
  crash_count      = crash_count + 1,
  quarantined      = (crash_count + 1 >= $poison_threshold),
  status           = CASE
                       WHEN crash_count + 1 >= $poison_threshold
                            OR attempts >= max_attempts THEN 'dead'
                       ELSE 'queued'
                     END,
  run_at           = now() + make_interval(secs =>
                       random() * LEAST($cap, $base * power(2, attempts))),
  lease_owner      = NULL,
  lease_expires_at = NULL
WHERE id IN (
  SELECT id FROM jobs
  WHERE  status = 'running' AND lease_expires_at < now()
  FOR UPDATE SKIP LOCKED LIMIT 100
)
RETURNING id, status, crash_count, quarantined, type;
```

---

### 4. Circuit Breaker (no retry storms)

When a downstream fails, jobs of that type queue up. The breaker detects failure rate >= 70% over a 20-second window and opens:

```mermaid
sequenceDiagram
  autonumber
  participant U as Operator
  participant D as PostgreSQL
  participant W as Workers
  participant B as Breaker logic

  U->>D: settings.outage_flaky_webhook = true
  W->>D: flaky_webhook jobs fail with HTTP 503
  W->>B: record failure, evaluate window
  B->>D: samples >= 6 and failure_rate >= 70%, set open_until = now() + 15s

  Note over W,D: CLAIM skips type flaky_webhook while open

  U->>D: outage off
  Note over B: after cooldown, probe jobs flow; success keeps it closed
```

Breaker state lives in the `breakers` table so all workers respect it without IPC.

---

### 5. Poison Quarantine (silent crashes caught)

Some jobs kill their worker silently (OOM, native crash). Each lease expiry increments `crash_count`. At `crash_count >= POISON_THRESHOLD` (default 3) the job is quarantined — moved to `dead` with reason `quarantined: worker died silently Nx`. The fleet stabilises automatically without operator intervention.

---

### 6. Statistical Retry Classifier

Each job type and error fingerprint is tracked in `retry_stats`. Using a Beta posterior:

```
p_success = (1 + successes) / (2 + successes + failures)
```

Jobs where `p_success < 15%` with at least 8 samples are classified **learned-permanent** and dead-lettered immediately, skipping unnecessary retries. The **Insights** tab shows the full verdict table and the cumulative `retries_avoided` counter.

---

## Live Dashboard and Chaos Lab

The dashboard is a single-page app served by FastAPI at `http://localhost:8000`. It polls `GET /api/overview` every second with no WebSocket or SSE dependencies.

### Chaos Run Flow

```mermaid
flowchart TD
  S["POST /chaos/run"] --> A["Create run, state: running"]
  A --> L["Load loop: enqueue N jobs at rate r\ncount 2xx as acknowledged"]
  A --> K["Kill loop: every kill_every_s seconds\nkill a healthy worker with in-flight jobs"]
  A --> F["Freeze loop (optional):\nevery 12s freeze a random worker"]
  L --> W{"All enqueued?"}
  W -->|yes| DR["state: draining\nwait until queued = running = 0\ntimeout 120s"]
  K --> DR
  F --> DR
  DR --> INV["Evaluate invariants I1 through I5"]
  INV --> V{"All hold and drained?"}
  V -->|yes| PASS["Report card: PASS"]
  V -->|no| FAIL["Report card: FAIL\nwith failing invariant and values"]
```

### Dashboard Tabs

| Tab | Contents |
|---|---|
| **Overview** | KPI cards (total / processing / retrying / DLQ), 60s throughput chart, 5 invariants, breaker chips, counters, live events feed |
| **Workers** | Worker table with status, PID, in-flight count, uptime; Kill and Freeze buttons; scale stepper |
| **Jobs** | Filterable job table, job detail drawer with full event timeline, manual enqueue form |
| **Dead Letter Queue** | DLQ table, Replay one, Replay all |
| **Chaos Lab** | Run config, Fencing ON/OFF toggle, Outage toggle, Poison injector, live progress and report card |
| **Insights** | Retry intelligence table (`p_success` per fingerprint), breaker status, forecast chart, recommended worker count, Apply button |

---

## Project Structure

```
lazarus/
├── PRD.md                     # Product requirements (SQL contract is law)
├── README.md                  # This file
├── AGENTS.md                  # Rules for AI agents working on this repo
├── docker-compose.yml         # PostgreSQL 16 + app container
├── Dockerfile                 # Single image: API + supervisor + workers
├── run_all.py                 # One-command launcher (no Docker required)
├── requirements.txt           # Python dependencies
├── schema.sql                 # Full database schema — apply once
├── .env.example               # Environment variable template
│
├── app/
│   ├── config.py              # Settings loaded from environment
│   ├── db.py                  # Async connection pool (psycopg 3)
│   ├── queue.py               # ALL queue SQL — enqueue, claim, complete, fail, reap, replay
│   ├── retry.py               # Backoff, fingerprint, classify, learn
│   ├── breaker.py             # Circuit breaker evaluate + open
│   ├── forecast.py            # Holt-Winters smoothing + worker recommendation
│   ├── invariants.py          # I1 through I5 live invariant checks
│   ├── supervisor.py          # Worker process lifecycle + control loop
│   ├── api/
│   │   ├── main.py            # FastAPI routes (PRD section 7 contract)
│   │   ├── chaos.py           # Chaos orchestration async task
│   │   └── server.py          # uvicorn launcher
│   └── worker/
│       ├── main.py            # Worker: claim loop, heartbeat, fenced commit, shutdown
│       └── handlers.py        # Job handlers (ledger_credit, flaky_webhook, ...)
│
├── static/
│   ├── index.html             # Dashboard (single page, tab switching client-side)
│   ├── app.css                # Design tokens + components (no Tailwind)
│   ├── app.js                 # Alpine.js stores + Chart.js integration
│   ├── vendor/                # Vendored Alpine.js and Chart.js (no CDN at runtime)
│   └── fonts/                 # Self-hosted IBM Plex Sans, IBM Plex Mono, Fraunces woff2
│
├── tests/
│   └── test_queue.py          # pytest suite (5 tests against real PostgreSQL)
│
└── doc/
    ├── 01-tech-stack.md
    ├── 02-architecture-workflow.md
    ├── 03-ui-ux-palette.md
    ├── 04-pages-and-button-workflow.md
    ├── 05-system-design.md
    └── reference/lazarus-prototype-prd.md
```

> **`app/queue.py` is the single source of truth for all queue SQL.** Every state transition is a named function with its SQL in a named constant. No ORM. The reliability logic is fully readable in one file.

---

## Configuration

Copy `.env.example` to `.env` and edit as needed.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/lazarus` | PostgreSQL connection string |
| `LEASE_SECONDS` | `6` | How long a worker claim lease lasts |
| `HEARTBEAT_SECONDS` | `2` | How often workers renew their lease |
| `WORKER_CONCURRENCY` | `4` | Concurrent jobs per worker process |
| `POLL_INTERVAL` | `0.3` | Seconds between claim attempts when idle |
| `MAX_ATTEMPTS` | `5` | Maximum retries before a job is dead-lettered |
| `BACKOFF_BASE` | `1.5` | Exponential backoff base in seconds |
| `BACKOFF_CAP` | `20.0` | Maximum backoff delay in seconds |
| `POISON_THRESHOLD` | `3` | Lease expiry count before job quarantine |
| `REAPER_INTERVAL` | `2.0` | Reaper run frequency in seconds |
| `PORT` | `8000` | API server port |
| `HOST` | `0.0.0.0` | API server bind address |
| `LAZARUS_ALLOW_RESET` | `0` | Set to `1` to enable the demo reset endpoint |

### Live Settings (DB table, no restart needed)

| Key | Default | Description |
|---|---|---|
| `desired_workers` | `3` | Target worker process count (clamped 1–8) |
| `fencing_enabled` | `true` | Fence token check on completion; `false` demonstrates duplicate effects |
| `outage_flaky_webhook` | `false` | Simulate downstream outage to trigger the circuit breaker |
| `autoscale` | `manual` | Set to `auto` to apply forecast recommendations automatically |

---

## API Reference

Full interactive docs at **http://localhost:8000/docs**.

### Jobs

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/jobs` | Idempotent enqueue. Returns `201` on create, `200` on duplicate. |
| `GET` | `/api/jobs` | List jobs. Optional `?status=`, `?type=`, `?tenant=` filters. |
| `GET` | `/api/jobs/{job_id}` | Job detail with full event timeline. |
| `POST` | `/api/jobs/{job_id}/replay` | Replay a single dead job. |
| `POST` | `/api/dlq/replay-all` | Replay all dead jobs. |

### Workers and Settings

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/workers` | List all workers (healthy, frozen, dead). |
| `POST` | `/api/workers/{worker_id}/kill` | Queue a kill command for the worker. |
| `POST` | `/api/workers/{worker_id}/freeze` | Queue a freeze command for the worker. |
| `POST` | `/api/workers/scale` | Set desired worker count. Body: `{"desired": 4}` |
| `POST` | `/api/settings` | Update live settings. Body: `{"fencing_enabled": false}` |

### Overview and Insights

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/overview` | Single JSON: KPIs, throughput, invariants, counters, breakers, workers, events. |
| `GET` | `/api/insights` | Retry intelligence, breaker status, forecast recommendation. |
| `GET` | `/api/events` | Recent job events. Optional `?worker=` filter. |
| `GET` | `/healthz` | Health check (`{"status": "ok"}` or 503). |

### Chaos Lab

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/chaos/run` | Start a chaos run. Body: `{"jobs": 300, "tenants": 3, "jobs_per_sec": 15, "kill_every_s": 8, "freeze": true}` |
| `GET` | `/api/chaos/run/latest` | Chaos run progress and report card. |
| `POST` | `/api/chaos/run/stop` | Stop the current chaos run. |
| `POST` | `/api/chaos/poison` | Inject poison-pill jobs. Body: `{"count": 1}` |
| `POST` | `/api/chaos/reset` | Reset all jobs and counters (requires `LAZARUS_ALLOW_RESET=1`). |

---

## Running Tests

Tests run against a real PostgreSQL instance — concurrency bugs do not appear with mocks.

```bash
pytest tests/ -v
```

Expected output:

```
tests/test_queue.py::test_backoff_bounds                      PASSED
tests/test_queue.py::test_idempotent_enqueue                  PASSED
tests/test_queue.py::test_fenced_complete_rolls_back_ledger   PASSED
tests/test_queue.py::test_reaper_reclaims_expired_leases      PASSED
tests/test_queue.py::test_concurrent_claim_no_double_claims   PASSED

5 passed in 2.94s
```

| Test | What it proves |
|---|---|
| `test_backoff_bounds` | Delay always satisfies `[0, min(cap, base * 2^attempt)]` |
| `test_idempotent_enqueue` | Same `(tenant_id, idempotency_key)` returns the same job id; `acked` increments once |
| `test_fenced_complete_rolls_back_ledger` | Stale fence triggers `StaleWriteError`; ledger row is rolled back (0 rows committed) |
| `test_reaper_reclaims_expired_leases` | Reaper reclaims expired lease, requeues job, increments `crash_count` |
| `test_concurrent_claim_no_double_claims` | 200 concurrent claimers over 1,000 jobs — zero double claims |

---

## Prototype vs Production

> "The reliability core is production-shaped and real. Scaling and cloud topology are simulated in this prototype and become real in Round 3."

| Component | This Prototype (Round 2) | Production Target (Round 3) |
|---|---|---|
| API (FastAPI) | **Real** | same |
| PostgreSQL state | **Real** — local or Azure PostgreSQL Flexible Server | Azure PG Flexible Server |
| Worker pool | **Real** — 1–8 OS processes on one host | Azure Container Apps replicas |
| Reaper / Scheduler | **Real** — single control loop | Advisory-lock leader election |
| Fencing, leases, retries, DLQ, replay | **Real** | same |
| Circuit breaker, quarantine, retry classifier | **Real** — stdlib only | + tuning, scikit-learn if justified |
| KEDA autoscaling | **Simulated** — forecast recommends; Apply button scales supervisor | Real KEDA scale rule on queue-depth metric |
| Observability | In-app events + counters | Full OTel to Application Insights |
| Dashboard | Static HTML + CSS + Alpine.js + Chart.js | React + Vite |
| Per-tenant fair claiming | Not in R2 | Fair claim in SQL |

---

## Database Schema

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Core job table
CREATE TABLE jobs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        text NOT NULL,
  type             text NOT NULL,
  payload          jsonb NOT NULL DEFAULT '{}',
  idempotency_key  text,
  priority         int NOT NULL DEFAULT 0,
  status           text NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued','running','succeeded','dead')),
  run_at           timestamptz NOT NULL DEFAULT now(),
  attempts         int NOT NULL DEFAULT 0,
  max_attempts     int NOT NULL DEFAULT 5,
  lease_owner      text,
  lease_expires_at timestamptz,
  fence            bigint NOT NULL DEFAULT 0,    -- incremented on every claim
  crash_count      int NOT NULL DEFAULT 0,
  quarantined      boolean NOT NULL DEFAULT false,
  last_error       text,
  error_class      text,    -- transient | permanent | learned_permanent | lease_expired
  last_fp          text,    -- error fingerprint for the statistical classifier
  result           jsonb,
  started_at       timestamptz,
  duration_ms      int,
  created_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX jobs_claim_idx ON jobs (priority DESC, run_at) WHERE status = 'queued';
CREATE INDEX jobs_lease_idx ON jobs (lease_expires_at)      WHERE status = 'running';
CREATE INDEX jobs_status_idx ON jobs (status);

-- Supporting tables
CREATE TABLE job_events  (id bigserial PRIMARY KEY, job_id uuid, job_type text,
                          event text NOT NULL, worker text, attempt int, detail jsonb,
                          at timestamptz NOT NULL DEFAULT now());

CREATE TABLE workers     (worker_id text PRIMARY KEY, name text, pid int,
                          started_at timestamptz, last_seen timestamptz,
                          in_flight int, command text, status text);

CREATE TABLE ledger      (id bigserial PRIMARY KEY, job_id uuid NOT NULL,
                          tenant_id text, account text, amount numeric,
                          created_at timestamptz DEFAULT now());

CREATE TABLE settings    (key text PRIMARY KEY, value text NOT NULL);
CREATE TABLE counters    (name text PRIMARY KEY, value bigint NOT NULL DEFAULT 0);
CREATE TABLE retry_stats (job_type text, fp text,
                          successes int NOT NULL DEFAULT 0,
                          failures  int NOT NULL DEFAULT 0,
                          PRIMARY KEY (job_type, fp));
CREATE TABLE breakers    (job_type text PRIMARY KEY, open_until timestamptz,
                          window_start timestamptz DEFAULT now(),
                          opened_count int NOT NULL DEFAULT 0);
```

**Invariants enforced in `app/queue.py`:**
- All timestamps use database `now()` — app clocks are never compared.
- Every post-claim write is fenced: `WHERE id = $1 AND fence = $2 AND status = 'running'`.
- Effects (ledger rows) and job completion commit in the **same transaction**.
- No ORM. All SQL lives in named constants in a single file.

---

## Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12+ | One language for API, workers, supervisor, and stats |
| API framework | FastAPI + Pydantic v2 | Typed routes; free OpenAPI docs at `/docs` |
| ASGI server | uvicorn | Standard pairing with FastAPI |
| Database | PostgreSQL 16 | `FOR UPDATE SKIP LOCKED` is required for the claim design |
| DB driver | psycopg 3 async + pool, raw SQL | Queue SQL must be exact and reviewable; no ORM |
| Worker runtime | asyncio per OS process | Many in-flight jobs per worker; real process death for kill/freeze |
| Supervisor | asyncio.create_subprocess_exec | Spawns, watches, and restarts worker processes |
| Statistics | Python stdlib `math` only | Beta posterior, Holt-Winters EWMA; no heavy dependencies |
| Frontend | HTML + CSS custom properties + Alpine.js 3 + Chart.js 4 | No build step, no CDN at runtime (everything vendored) |
| Fonts | IBM Plex Sans, IBM Plex Mono, Fraunces | Self-hosted woff2; distinctive, readable, tabular numerals |
| Tests | pytest + pytest-asyncio | Real PostgreSQL; concurrency bugs do not appear with mocks |

---

*Built by Team Quark.*
