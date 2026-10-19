# 02 · Architecture and Workflow: Lazarus Prototype

How the pieces fit and how data and control move through them. All diagrams are Mermaid: paste into mermaid.live or any Markdown viewer that renders it.

---

## 1. Runtime component view

```mermaid
flowchart LR
  subgraph BROWSER["Browser"]
    UI["Dashboard<br/>HTML + CSS + Alpine + Chart.js"]
  end
  subgraph HOST["App host or single container"]
    API["API process<br/>FastAPI + uvicorn<br/>REST + static files"]
    CHAOS["Chaos orchestrator<br/>async task inside API"]
    SUP["Supervisor process<br/>worker lifecycle + control loop"]
    subgraph WP["Worker processes 1..8"]
      W1["worker-1"]
      W2["worker-2"]
      WN["worker-N"]
    end
  end
  PG[("PostgreSQL 16<br/>local container or Azure Flexible Server")]
  DOWN["Simulated downstream<br/>flaky_webhook, legacy_sync handlers"]
  UI -->|"poll GET /api/overview every 1s<br/>POST actions"| API
  API --> CHAOS
  API -->|"enqueue, reads, settings, worker commands"| PG
  CHAOS -->|"enqueue load, kill and freeze commands"| PG
  SUP -->|"spawn, restart"| WP
  SUP -->|"reaper, breakers, forecast"| PG
  WP -->|"claim, heartbeat, fenced commit"| PG
  WP --> DOWN
```

| Component | Owns | Never does |
|---|---|---|
| **API** | Enqueue (idempotent), queries, settings, worker commands, chaos runs, serving the dashboard | Run job handlers |
| **Supervisor** | Worker process lifecycle (desired count), reaper loop, breaker evaluation, forecast | Serve HTTP |
| **Worker** | Claim, heartbeat, run handlers, fenced commit, obey `kill`/`freeze` | Decide retry policy alone (it calls `retry.py`, which reads shared stats) |
| **Postgres** | All durable state, leases, fencing tokens, effects ledger, counters | Anything else. It is the single source of truth |
| **Dashboard** | Rendering, button actions | Hold authoritative state; it re-reads everything each second |

## 2. Job lifecycle

```mermaid
stateDiagram-v2
  [*] --> queued: enqueue (idempotent)
  queued --> running: claim (lease, fence++)
  running --> succeeded: fenced complete (effect + status in one tx)
  running --> queued: transient failure, or lease expiry (backoff + jitter)
  running --> dead: permanent, learned-permanent, attempts exhausted, or quarantined
  dead --> queued: replay
  succeeded --> [*]
```

## 3. Worker lifecycle

```mermaid
stateDiagram-v2
  [*] --> starting
  starting --> healthy: registered, heartbeating
  healthy --> frozen: freeze command (no heartbeats for LEASE x 1.8)
  frozen --> healthy: wakes, attempts late commits
  healthy --> dead: kill command, crash, or poison job
  dead --> starting: supervisor restarts (about 3s)
  healthy --> draining: SIGTERM
  draining --> [*]: in-flight finished or released
```

## 4. Workflows (sequence diagrams)

### 4.1 Enqueue and process (happy path)

```mermaid
sequenceDiagram
  autonumber
  participant P as Producer or Chaos Lab
  participant A as API
  participant D as Postgres
  participant W as Worker
  P->>A: POST /api/jobs {tenant, type, payload, idempotency_key}
  A->>D: INSERT ... ON CONFLICT DO NOTHING RETURNING
  D-->>A: new row, or none for a duplicate
  A->>D: counters.acked++ (new rows only)
  A-->>P: 201 new, or 200 existing
  loop every POLL_INTERVAL while capacity is free
    W->>D: CLAIM (SKIP LOCKED, lease, fence++)
    D-->>W: jobs with fence = f
  end
  par handler runs
    W->>W: run handler
  and lease kept alive
    W->>D: batch heartbeat every 2s
  end
  W->>D: BEGIN; INSERT ledger; UPDATE jobs WHERE fence = f; COMMIT
  D-->>W: rowcount 1 = success, 0 = stale (ROLLBACK)
```

### 4.2 Worker killed mid-job

```mermaid
sequenceDiagram
  autonumber
  participant U as Operator (dashboard)
  participant A as API
  participant D as Postgres
  participant W1 as Worker 1
  participant R as Reaper (supervisor)
  participant W2 as Worker 2
  participant S as Supervisor
  U->>A: POST /workers/{id}/kill
  A->>D: workers.command = kill
  W1->>D: heartbeat reads command
  W1--xW1: os._exit(137), lease NOT released
  Note over D: job stays running until lease_expires_at passes (at most 6s)
  R->>D: REAPER UPDATE (crash_count++, requeue with jitter)
  W2->>D: CLAIM (new fence)
  W2->>D: fenced complete, success
  S->>S: sees process exit
  S->>W1: spawn replacement after about 3s (new worker_id)
```

### 4.3 Frozen worker ("zombie"), fencing ON vs OFF

```mermaid
sequenceDiagram
  autonumber
  participant Z as Worker Z (frozen)
  participant D as Postgres
  participant R as Reaper
  participant W2 as Worker 2
  Z->>D: CLAIM job J (fence = 1)
  Note over Z: freeze command, event loop blocked for LEASE x 1.8, no heartbeats
  R->>D: lease expired, requeue J
  W2->>D: CLAIM J (fence = 2)
  W2->>D: BEGIN; INSERT ledger; UPDATE WHERE fence = 2; COMMIT (ok)
  Z->>Z: wakes, handler returns
  alt Fencing ON (safe)
    Z->>D: BEGIN; INSERT ledger; UPDATE WHERE fence = 1
    D-->>Z: 0 rows, ROLLBACK, stale_writes_rejected++
  else Fencing OFF (unsafe demo)
    Z->>D: BEGIN; INSERT ledger; UPDATE WHERE id = J; COMMIT
    Note over D: two ledger rows for job J, so invariants I2 and I3 turn red
  end
```

### 4.4 Downstream outage and the circuit breaker

```mermaid
sequenceDiagram
  autonumber
  participant U as Operator
  participant D as Postgres
  participant W as Workers
  participant B as Breaker logic
  U->>D: settings.outage_flaky_webhook = true
  W->>D: flaky_webhook attempts fail with HTTP 503
  W->>B: record failure, evaluate window (20s)
  B->>D: samples >= 6 and failure rate >= 70%, so breakers.open_until = now + 15s
  Note over W,D: CLAIM skips job type flaky_webhook while open, held-back gauge rises
  U->>D: outage off
  Note over B: after cooldown jobs flow again as the probe; success keeps it closed, failure re-opens it
```

### 4.5 Chaos run orchestration

```mermaid
flowchart TD
  S["POST /chaos/run"] --> A["Create run record<br/>state: running"]
  A --> L["Load loop: enqueue N jobs at rate r<br/>count 2xx as acknowledged"]
  A --> K["Kill loop: every kill_every_s kill a healthy worker with in-flight jobs"]
  A --> F["Freeze loop (optional): about every 12s freeze a random worker"]
  L --> W{"all enqueued?"}
  W -->|yes| DR["state: draining<br/>wait until queued = running = 0<br/>(timeout 120s)"]
  K --> DR
  F --> DR
  DR --> INV["Evaluate invariants I1-I5"]
  INV --> V{"all hold and drained?"}
  V -->|yes| PASS["Report card: PASS"]
  V -->|no| FAIL["Report card: FAIL<br/>(with failing invariant and values)"]
```

### 4.6 Dashboard refresh loop

```mermaid
sequenceDiagram
  participant UI as Alpine store
  participant API as GET /api/overview
  participant DB as Postgres
  loop every 1s
    UI->>API: fetch
    API->>DB: KPIs, 60s throughput, invariants, counters, breakers, workers, 15 latest events
    DB-->>API: rows
    API-->>UI: one JSON document
    UI->>UI: update cards, charts, tables, flash changed values
  end
  Note over UI: on fetch failure show "Lost contact with API, retrying" and keep last data greyed
```

## 5. `GET /api/overview` contract (build the dashboard against this)

Values are illustrative, not measured.

```json
{
  "ts": "2026-10-18T14:03:11Z",
  "settings": { "fencing_enabled": true, "desired_workers": 3, "outage_flaky_webhook": false, "autoscale": "manual" },
  "kpis": { "total": 512, "processing": 28, "retrying": 12, "dlq": 8, "succeeded": 464 },
  "throughput": [ { "t": "2026-10-18T14:02:12Z", "ok": 9, "failed": 1 } ],
  "invariants": [
    { "id": "I1", "name": "No acknowledged job lost", "ok": true, "value": "512 / 512" },
    { "id": "I2", "name": "No duplicate effects", "ok": true, "value": "0" },
    { "id": "I3", "name": "Exactly-once credits", "ok": true, "value": "0 violations" },
    { "id": "I4", "name": "No ghost effects", "ok": true, "value": "0" },
    { "id": "I5", "name": "No stuck leases", "ok": true, "value": "0" }
  ],
  "counters": { "acked": 512, "stale_writes_rejected": 7, "lease_expired": 12, "quarantined": 1, "retries_avoided": 14, "rate_limited": 0 },
  "breakers": [ { "type": "flaky_webhook", "state": "closed", "open_for_s": 0, "held_back": 0 } ],
  "workers": [ { "worker_id": "worker-1-a3f1", "name": "worker-1", "status": "healthy", "in_flight": 3, "uptime_s": 412, "last_seen_ms": 640, "pid": 4121 } ],
  "events": [ { "at": "2026-10-18T14:03:10Z", "event": "lease_expired", "job_id": "…", "job_type": "ledger_credit", "worker": "worker-2-bb12", "detail": "worker lost" } ]
}
```

`throughput` has 60 entries (one per second). `workers` includes dead rows for 10s after death.

## 6. Deployment views

```mermaid
flowchart LR
  subgraph R2A["R2 default: local"]
    L1["docker compose<br/>postgres + app (run_all.py)"]
  end
  subgraph R2B["R2 optional: partly in Azure"]
    A1["Laptop or one container<br/>API + supervisor + workers"] --> A2[("Azure Database for PostgreSQL")]
    A1 -.->|"P2: OTel"| A3["Application Insights"]
  end
  subgraph R3["Round 3 target"]
    C1["Container App: API"] --> C4[("Azure PostgreSQL")]
    C2["Container App: workers 2..8<br/>KEDA scale rule on queue depth"] --> C4
    C3["Container App: control plane<br/>advisory-lock leader"] --> C4
    C1 -.-> C5["Application Insights + Log Analytics"]
    C2 -.-> C5
  end
```

## 7. Cross-cutting rules

| Concern | Rule |
|---|---|
| Time | Database `now()` only |
| Idempotency | `UNIQUE(tenant_id, idempotency_key)`; duplicate returns the existing job |
| Fencing | Every post-claim write has `fence = $mine`; unsafe mode exists only as a labelled demo toggle |
| Effects | Written in the same transaction as the fenced completion |
| Events | Every transition appends to `job_events` (feeds the feed, throughput chart, breaker window, forecast) |
| Settings | Live in `settings`; workers re-read at most once per second |
| Failure of the API | Workers keep running; the queue is independent of the API |
| Failure of the supervisor | Workers keep running but no reaping. Mention as a known single point (R3: leader election in worker image) |
