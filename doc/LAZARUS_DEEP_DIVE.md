# Lazarus: Reliable Asynchronous Job Runner
## Complete Architecture, Rationale, Dashboard Breakdown, and Gap Analysis

> **Document Purpose**: This document is an exhaustive, production-grade guide to the **Lazarus** project (Team Quark). It covers the complete operational mechanics, mathematical models, database invariants, rationale for every architectural decision, detailed UI explanations, and a clear gap analysis of what is implemented versus what is needed for future stages.
> You can copy and paste this entire document into any LLM or AI coding agent to provide full domain context.

---

## 1. Executive Summary & Problem Space

### 1.1 The Problem
In modern distributed systems, asynchronous background job processors (e.g., Celery, BullMQ, Sidekiq, AWS SQS workers) run mission-critical tasks like ledger transactions, financial webhooks, batch inventory updates, and report generation. Under production anomalies (worker SIGKILL, network partitions, out-of-memory errors, downstream rate limits, and zombie process states), traditional queues suffer from:
1. **Lost Jobs**: When a worker crashes silently, jobs remain locked in limbo forever.
2. **Duplicate Ledger Charges / Ghost Writes**: When a worker freezes (GC pause, network hang) and is presumed dead, another worker takes its job. When the frozen worker awakens, it commits stale data, causing double debits or corrupted states.
3. **Thundering Herds & Retry Storms**: When a downstream API goes down, blind retries flood the failing service, causing cascading failures.
4. **Poison Pills**: Malformed jobs repeatedly crash workers silently on deserialization or execution, bringing down the entire fleet.

### 1.2 The Lazarus Solution
Lazarus is a **fault-tolerant, database-native asynchronous job orchestrator** backed by PostgreSQL. It eliminates message broker dependencies (like Redis or RabbitMQ) by leveraging PostgreSQL 16 row-level locking (`FOR UPDATE SKIP LOCKED`), transactional lease fencing tokens, and automated statistical intelligence.

Lazarus guarantees three core operational properties:
* **No Lost Acknowledged Jobs** ($I_1$)
* **Zero Duplicate External Effects** ($I_2$)
* **Zero Ghost/Unearned Effects** ($I_4$)

---

## 2. Core Architectural Design & Technology Choices

### 2.1 The High-Level Architecture

```
                  ┌────────────────────────────────────────┐
                  │           FastAPI REST API             │
                  │   (Enqueue, Dashboard APIs, Chaos)     │
                  └──────────────────┬─────────────────────┘
                                     │
           ┌─────────────────────────┴─────────────────────────┐
           ▼                                                   ▼
┌───────────────────────┐                           ┌─────────────────────┐
│  Alpine.js Dashboard  │                           │ Process Supervisor  │
│  (Live Polling 1s)    │                           │ (Controls Workers & │
└───────────────────────┘                           │  Runs Reaper Loop)  │
           ▲                                        └──────────┬──────────┘
           │                                                   │
           │                                 ┌─────────────────┴─────────────────┐
           │                                 ▼                                   ▼
           │                     ┌───────────────────────┐           ┌───────────────────────┐
           │                     │   Worker Process 1    │           │   Worker Process N    │
           │                     │ (4 Async Concurrent)  │           │ (4 Async Concurrent)  │
           │                     └───────────┬───────────┘           └───────────┬───────────┘
           │                                 │                                   │
           │                                 │                                   │
           ▼                                 ▼                                   ▼
┌────────────────────────────────────────────────────────────────────────────────────────────┐
│                             PostgreSQL Database (Local or Azure)                           │
│  Tables: jobs | job_events | workers | ledger | settings | counters | retry_stats | breakers│
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Why PostgreSQL as the Queue Engine? (Instead of Redis/RabbitMQ/Kafka)

| Criterion | Redis / Celery | RabbitMQ / SQS | PostgreSQL (Lazarus) |
|---|---|---|---|
| **Atomicity with Business Data** | ❌ Dual-write problem: enqueuing and saving state are separate | ❌ Separate network call, requires 2-phase commit (2PC) | ✅ **ACID Transaction**: Ledger write and job completion happen in the **exact same transaction**. |
| **Fence Token Enforcement** | ❌ No native SQL row fencing | ❌ External coordinator needed (e.g. Zookeeper/Etcd) | ✅ **SQL WHERE clause check**: `WHERE id = job_id AND fence = token` guarantees zombie isolation. |
| **Crash Durability** | ⚠️ Redis AOF/RDB can lose milliseconds of data | ✅ Durable disk writes | ✅ Full WAL (Write-Ahead Logging) persistence. |
| **Operational Simplicity** | ❌ Requires managing Redis + PostgreSQL + Sentinel | ❌ Requires cluster management + dead-letter queues | ✅ Single database infrastructure to monitor, back up, and secure. |

### 2.3 Why Raw SQL (`psycopg 3`) Instead of an ORM (SQLAlchemy/Tortoise)?
* **Deterministic Execution**: Queue mechanics require precise locking semantics (`FOR UPDATE SKIP LOCKED`). ORMs often obscure generated SQL, introduce unwanted eager loading, or execute multi-statement round trips.
* **Non-blocking Async I/O**: `psycopg 3` provides native asynchronous connection pooling (`AsyncConnectionPool`) with binary protocol support, minimizing connection overhead.
* **Server-side Time Anchoring**: Using `now()` in PostgreSQL ensures clock synchronization across all workers. If worker system clocks drift, lease expirations and scheduling calculations remain perfectly synchronized.

### 2.4 Why Multiple OS Processes with an Async Supervisor?
* In Python, the Global Interpreter Lock (GIL) prevents CPU-bound parallelism within a single process.
* Running workers as distinct OS processes (`python -m app.worker.main`) guarantees that a catastrophic failure (e.g., `os._exit(137)` or C-extension segmentation fault) does not bring down the API or supervisor.
* Each worker process runs `asyncio` concurrency (handling up to 4 concurrent job coroutines), combining OS-level process isolation with lightweight asynchronous I/O.

---

## 3. Deep Dive into the 6 Core Novelty Features

### 3.1 Novelty 1: "Proof, Not Promises" (Live Formal Invariants Engine)
Traditional systems report basic metrics like "jobs per second." Lazarus continuously evaluates **5 mathematical safety invariants** against the active database every 1 second:

* **$I_1$: No Acknowledged Job Lost**
  $$\text{Total Jobs in DB} = \text{Acknowledged Counter} = \text{Succeeded} + \text{Dead} + \text{Queued} + \text{Running}$$
  *Meaning*: Every job accepted by the system is accounted for. No job can vanish into an ether.
* **$I_2$: No Duplicate Effects**
  $$\left|\{ \text{job\_id} \in \text{ledger} \mid \text{count}(*) > 1 \}\right| = 0$$
  *Meaning*: Under no circumstances should an external business side effect (e.g., charging an account) be written twice.
* **$I_3$: Exactly-Once Succeeded Credits**
  $$\forall j \in \text{jobs} \text{ where } j.\text{type} = \text{'ledger\_credit'} \land j.\text{status} = \text{'succeeded'} \implies \text{count}(\text{ledger}) = 1$$
  *Meaning*: If the system reports success, the effect exists exactly once.
* **$I_4$: No Ghost Effects**
  $$\left|\{ l \in \text{ledger} \mid \text{jobs}(l.\text{job\_id}).\text{status} \neq \text{'succeeded'} \}\right| = 0$$
  *Meaning*: If a job failed, timed out, or was killed, it must never leave an orphaned ledger record behind.
* **$I_5$: No Stuck Leases**
  $$\left|\{ j \in \text{jobs} \mid j.\text{status} = \text{'running'} \land j.\text{lease\_expires\_at} < \text{now}() - \epsilon \}\right| = 0$$
  *Meaning*: The background Reaper must reclaim dead worker leases within a bounded delay ($\epsilon = \text{LEASE\_SECS} + 3\text{s}$).

---

### 3.2 Novelty 2: Zombie Worker Prevention via Optimistic Fencing Tokens
#### The Zombie Problem
1. Worker A claims Job #101 at Fence Token = `1`.
2. Worker A enters a stop-the-world garbage collection pause, network hiccup, or CPU freeze for 20 seconds.
3. The Reaper detects lease expiry and reclaims Job #101.
4. Worker B claims Job #101. Fence Token increments to `2`. Worker B runs the job and writes to the ledger.
5. Worker A suddenly wakes up, unaware it was timed out. It executes the write to the ledger and marks the job succeeded.
6. **Result in traditional queues**: Double billing, corrupted account balances!

#### The Lazarus Solution (Fencing)
Every claim query executes an atomic counter increment:
```sql
UPDATE jobs SET
  status = 'running',
  lease_owner = %s,
  fence = fence + 1,
  lease_expires_at = now() + interval '8 seconds'
WHERE id = %s RETURNING fence;
```
When a worker attempts to commit its side effects and complete the job, it runs inside a single database transaction:
```sql
BEGIN;
  -- Insert business effect
  INSERT INTO ledger (job_id, tenant_id, account, amount) VALUES (...);

  -- Atomic Fenced Complete
  UPDATE jobs SET status = 'succeeded', finished_at = now()
  WHERE id = %s AND fence = %s AND status = 'running';
COMMIT;
```
* **Safe Mode (Fencing ON)**: If Worker A wakes up and passes `fence = 1`, the `UPDATE jobs` query affects **0 rows** because the fence in the database is already `2`. The code detects `rows_updated == 0`, immediately rolls back the entire transaction (discarding the ledger insert), increments the `stale_writes_rejected` counter, and logs a `stale_write_rejected` event.
* **Unsafe Mode (Fencing OFF)**: Demonstrates the failure mode live in the Chaos Lab by omitting `AND fence = %s`, producing instant duplicate charges and turning Invariants $I_2$ and $I_3$ red!

---

### 3.3 Novelty 3: Adaptive Retry Intelligence (Beta Posterior Learning)
Instead of blindly retrying every failure until `max_attempts` is exhausted, Lazarus classifies errors into two categories:

1. **Deterministic Rule Prior**:
   * Validation errors (`ValidationError`, missing fields, HTTP 400, 422) are permanently non-retryable. They transition straight to the Dead Letter Queue (DLQ) with `attempts = 1`.
2. **Statistical Learning (Beta-Binomial Conjugate Model)**:
   * Some bugs present as transient exceptions (e.g., `ConnectionResetError` or malformed legacy payload bytes) but will never succeed no matter how many times retried.
   * Lazarus tracks successes ($s$) and failures ($f$) per `(job_type, error_fingerprint)`.
   * It calculates the Bayesian posterior probability of success on retry:
     $$P(\text{success}) = \frac{1 + s}{2 + s + f}$$
   * Once sample size $n = (s + f) \ge 8$ and $P(\text{success}) < 0.15$, Lazarus classifies the error as `learned-permanent`.
   * Future jobs with this fingerprint bypass remaining retries and move straight to DLQ, incrementing the `retries_avoided` KPI and saving valuable CPU cycles.

---

### 3.4 Novelty 4: Dynamic Circuit Breakers (Stopping Retry Storms)
When an external dependency crashes (e.g., downstream payment gateway returns HTTP 503):
* Continuing to claim and retry jobs causes a "retry storm," worsening the downstream outage and tying up worker threads.
* **Sliding Window Detection**: Lazarus evaluates job outcomes over a 20-second rolling window.
* **Trip Condition**: If total attempts $\ge 6$ and failure rate $\ge 70\%$, the circuit breaker for that `job_type` transitions to `OPEN` for 15 seconds.
* **Claim Suppression**: The claim query dynamically excludes open types:
  ```sql
  WHERE status = 'queued'
    AND type NOT IN (SELECT job_type FROM breakers WHERE open_until > now())
  ```
* Jobs remain safely buffered in PostgreSQL without burning retry attempts. Once the cooldown timer expires, the breaker transitions to `HALF-OPEN`/`CLOSED`.

---

### 3.5 Novelty 5: Predictive Capacity Forecasting (Holt's Linear Smoothing)
Instead of reacting only when queue depth has already exploded, Lazarus predicts near-future backlog requirements using **Holt's Two-Parameter Linear Exponential Smoothing**:

1. **Arrival Rate ($\hat{\lambda}$)**: Enqueue rates are sampled in 5-second buckets over the last 120 seconds.
   * Level Equation: $L_t = \alpha Y_t + (1 - \alpha)(L_{t-1} + T_{t-1})$ (where $\alpha = 0.4$)
   * Trend Equation: $T_t = \beta (L_t - L_{t-1}) + (1 - \beta)T_{t-1}$ (where $\beta = 0.2$)
   * Forecast: $\hat{\lambda} = \max(0, L_t + T_t)$
2. **Mean Service Time ($s$)**: Dynamically computed from the moving average duration of the last 100 successful jobs.
3. **Capacity Dimensioning**:
   $$\text{Workload} = (\hat{\lambda} \cdot s) + \frac{\text{Backlog} \cdot s}{\text{Target Drain Window (30s)}}$$
   $$\text{Recommended Workers} = \text{clamp}\left( \left\lceil \frac{\text{Workload}}{\text{Worker Concurrency (4)}} \times 1.2 \right\rceil, 1, 8 \right)$$
* In **Manual Mode**, an operator clicks "Apply" to scale worker processes.
* In **Auto Mode**, the supervisor automatically converges the running worker pool to the recommended size.

---

### 3.6 Novelty 6: Silent Worker Crash Quarantine (Poison Pill Isolation)
* If a job contains malicious or corrupt payload data that triggers a native C-level crash, an out-of-memory kernel kill, or `os._exit(1)`, the worker process terminates instantly without running error handlers or `finally` blocks.
* The job's lease expires, and traditional systems would blindly retry it, causing every worker in the fleet to crash sequentially (a cascading crash loop).
* **The Lazarus Reaper**:
  * Tracks `crash_count` on the job record whenever a lease expires without worker check-in.
  * When `crash_count + 1 >= 3` (`poison_threshold`), the job is flagged as `quarantined = true`, moved to `dead` status, and labeled `'quarantined: worker died silently 3x'`.
  * The rest of the fleet is protected, and the poison pill is safely isolated in the DLQ.

---

## 4. Complete UI / Dashboard Walkthrough

The Lazarus Web Dashboard is built as an ultra-fast, zero-build single-page interface using **Alpine.js**, vanilla CSS, and Google Fonts (`Inter` & `JetBrains Mono`). It polls the API every 1000ms.

### 4.1 Top Header & System Status
* **Logo & System Title**: "Lazarus · Reliable Asynchronous Job Runner"
* **Live System Status Pill**: Indicates API reachability and WebSocket/polling health (`ALL SYSTEMS OPERATIONAL`).
* **Active Fencing Mode Indicator**: Shows `FENCING: SECURE` (Green) or `FENCING: DISABLED` (Red warning).
* **Autoscale Pill**: Displays `MANUAL` or `AUTO` scaling mode.

---

### 4.2 Page 1: Overview
The primary mission control center for evaluating system health:
1. **KPI Cards**:
   * **Total**: All jobs submitted across all time.
   * **Processing**: Current jobs actively leased by workers (`status = 'running'`).
   * **Retrying**: Jobs that failed with transient errors and are waiting on exponential backoff timers before next claim.
   * **Dead Letter Queue (DLQ)**: Jobs permanently failed, exhausted retries, or quarantined.
2. **Telemetry Counters Row**:
   * **Stale writes rejected**: Total zombie commits intercepted and aborted by fence tokens.
   * **Leases expired**: Total orphaned jobs detected by the Reaper loop.
   * **Quarantined**: Total silent killer jobs isolated from the worker fleet.
   * **Retries avoided**: Total unnecessary retry iterations bypassed by learned intelligence.
3. **Throughput & State Distribution Chart**:
   * Real-time rolling chart showing job state breakdown over time.
4. **Events Feed (Console Stream)**:
   * Real-time ledger of the last 15 system transitions (`enqueued`, `claimed`, `succeeded`, `failed`, `lease_expired`, `stale_write_rejected`).
5. **Invariants Verification Panel**:
   * Displays all 5 live mathematical correctness checks ($I_1$ to $I_5$) with green `PASS` or red `FAIL` badges and real-time numeric proofs.

---

### 4.3 Page 2: Workers Management
Direct introspection and chaos control of the process pool:
1. **Worker Fleet Table**:
   * **Worker ID**: Unique identifier (e.g., `worker-1`, `worker-2`).
   * **Status Badge**: `healthy` (Green), `frozen` (Yellow), or `dead` (Red).
   * **In-Flight Slots**: Current concurrency occupancy (e.g., `2 / 4`).
   * **Uptime**: Elapsed time since process spawn.
   * **Last Seen**: Milliseconds since the worker last transmitted an active heartbeat to the database.
   * **PID**: The actual operating system process ID on the host/container.
2. **Actions**:
   * **Kill Button**: Sends an immediate kill signal (`os._exit(137)`). The worker dies instantly with zero clean-up. Evaluators can watch the Supervisor detect the process death, spin up a replacement within 3-4 seconds, and watch the Reaper rescue orphaned jobs.
   * **Freeze Button**: Injects an artificial blocking sleep (`time.sleep(15)`). The worker stops heartbeating. The Reaper reclaims the job, and when the frozen worker thaws and attempts to commit, it is caught as a zombie.
3. **Scale Stepper**:
   * Buttons (`-` / `+`) to manually adjust desired worker count between 1 and 8 processes.

---

### 4.4 Page 3: Jobs Explorer
Detailed job inspection and submission:
1. **Enqueue Form**:
   * Allows manual submission of test jobs with custom `tenant_id`, `type`, JSON payload, and `idempotency_key`.
2. **Jobs Table**:
   * Filterable by status (`queued`, `running`, `succeeded`, `dead`).
   * Displays ID, Type, Tenant, Attempts, Current Fence Token, Age, and Error Class.
3. **Job Detail Drawer**:
   * Clicking any job row slides open a right-side drawer showing full payload, output result, execution duration in milliseconds, and the complete step-by-step event audit timeline.

---

### 4.5 Page 4: Dead Letter Queue (DLQ)
Management of failed jobs:
* Lists all jobs that exhausted attempts, suffered fatal validation errors, or were quarantined.
* Displays error message, stack trace snippet, and error classification.
* **Replay Button**: Resets a specific job's `attempts = 0`, `crash_count = 0`, clears errors, and places it back in `queued` state.
* **Replay All Button**: Batch-requeues all dead-lettered jobs in a single click.

---

### 4.6 Page 5: Chaos Lab
The interactive evaluation suite designed specifically for hackathon judging:
1. **Safety Toggles**:
   * **Fencing Mode Toggle (ON / OFF)**: Allows evaluator to disable fencing checks and witness the failure mode in real time.
   * **Simulate Downstream Outage Toggle (ON / OFF)**: Forces `flaky_webhook` to return 100% HTTP 503, demonstrating how the Circuit Breaker opens and protects the system.
   * **Inject Poison Pill**: Submits a job programmed to trigger an unhandled process termination, proving silent crash containment.
2. **Chaos Test Configuration**:
   * Inputs for Total Jobs (e.g., 300), Tenants (e.g., 5), Injection Rate (e.g., 20 jobs/sec), and Worker Kill Interval (e.g., Kill every 8 seconds).
3. **Execution & Report Card**:
   * Clicking **Run Chaos Test** starts high-concurrency ingestion while background timers periodically kill and freeze workers.
   * Once finished, it generates an automated **Report Card** proving:
     * Acknowledged == Accounted
     * Duplicate Effects == 0 (PASS) or > 0 (FAIL if fencing was turned off)
     * Stale writes rejected count
     * Total quarantined jobs

---

### 4.7 Page 6: Insights & Intelligence
Deep observability into adaptive statistical models:
1. **Learned Retry Intelligence Table**:
   * Shows each `(job_type, error_fingerprint)` pair.
   * Displays observation count ($n$), calculated Bayesian probability $P(\text{success})$, and verdict (`transient` vs `learned-permanent`).
2. **Circuit Breakers Panel**:
   * Visual cards for each job type showing state (`CLOSED` vs `OPEN`), active countdown timer, and total historical trip count.
3. **Arrival Forecast & Capacity Planning**:
   * Displays current smoothed arrival rate ($\hat{\lambda}$), current ready backlog, and recommended worker count.
   * Includes toggle for **Autoscale Mode** (`Manual` vs `Auto`).

---

## 5. Job Types & Synthetic Handlers Catalog

| Job Type | Simulated Behavior | Failure Modes Exercised |
|---|---|---|
| `ledger_credit` | Simulates a banking ledger transaction. Sleeps 1.0–3.5s, then commits funds to `ledger` table atomically with fenced job update. | Targeted by Kill and Freeze commands to test duplicate effect prevention ($I_2, I_3$). |
| `flaky_webhook` | Calls external web API. Runs in 0.3–0.8s with a base 15% random HTTP 503 failure rate. If Outage toggle is ON, returns 100% 503. | Exercises Capped Exponential Backoff with full jitter and trips the Circuit Breaker. |
| `validate_order` | Validates customer checkout data. 30% of jobs raise `ValidationError:missing_field`. | Tests deterministic prior classification: bypasses retries, moves straight to DLQ. |
| `legacy_sync` | Synchronizes records with legacy backend. 15% fail with `ConnectionResetError:ECONNRESET`. | Appears transient but never succeeds; triggers Beta posterior learning to classify as `learned-permanent`. |
| `report_export` | Generates large data exports. Usually completes in 1.0s, but 5% take 10x longer (10.0s). | Demonstrates slow-job anomaly detection and dynamic lease extension. |
| `poison_pill` | Executes `os._exit(1)` immediately inside the handler coroutine. | Triggers silent worker process termination; verifies Reaper quarantine at threshold 3. |

---

## 6. Gap Analysis: What is Built vs. What is Missing for Future Stages

### 6.1 Status Matrix

| Component | Prototype Status (Current) | Future / Production Gap (Round 3) |
|---|---|---|
| **Core Storage Engine** | ✅ Fully operational on PostgreSQL 16 (local or Azure Flexible Server). | ⚠️ Multi-region active-active replication, connection pool tuning with PgBouncer. |
| **Row Locking & Claims** | ✅ Implemented via `FOR UPDATE SKIP LOCKED`. | ⚠️ Per-tenant fair-share claiming (preventing one noisy tenant from monopolizing claims). |
| **Fencing & Leases** | ✅ Complete optimistic fencing token implementation. | None (already production grade). |
| **Reaper Engine** | ✅ Single-instance background loop inside supervisor. | ⚠️ Distributed leader election (e.g. via `pg_try_advisory_lock`) so multiple supervisors don't race. |
| **Worker Pool** | ✅ Multi-process supervisor managing 1–8 local OS processes. | ⚠️ Azure Container Apps / Kubernetes (KEDA) container-level autoscaling across multiple physical VMs. |
| **Circuit Breakers** | ✅ Rolling 20s window, dynamic claim exclusion. | ⚠️ Per-tenant and per-endpoint fine-grained breaker keys. |
| **Retry Intelligence** | ✅ Beta posterior conjugate model with fingerprinting. | ⚠️ Persistent historical decay (weights older errors less than recent errors). |
| **Capacity Forecast** | ✅ Holt's linear exponential smoothing model. | ⚠️ Multi-seasonal Holt-Winters (handling daily/weekly diurnal cycles). |
| **Front-End UI** | ✅ Single-file Alpine.js + CSS dashboard (zero build tools). | ⚠️ React / Next.js / Vite SPA with WebSocket/SSE event push rather than 1s polling. |
| **Observability** | ✅ In-app event logs, SQL counters, live web console. | ⚠️ OpenTelemetry export to Azure Application Insights / Prometheus / Grafana. |
| **Security & Auth** | ⚠️ Development mode (unauthenticated endpoints). | ⚠️ Azure Active Directory (Entra ID) / OAuth2 JWT bearer tokens, tenant API keys. |

---

## 7. Setup & Run Instructions

### 7.1 Local Docker Deployment
1. Ensure Docker Desktop is running.
2. In terminal, navigate to:
   ```powershell
   cd c:\Users\Noothan\Desktop\Lazarus\Project\quark
   ```
3. Launch services:
   ```powershell
   docker compose up -d
   ```
4. Access the web dashboard at: **`http://localhost:8000`**
5. Stop services:
   ```powershell
   docker compose down
   ```

### 7.2 Connecting to Azure PostgreSQL
1. Create an Azure Database for PostgreSQL Flexible Server in the Azure Portal.
2. Add your local IP address (or `0.0.0.0 - 255.255.255.255` for demo testing) to the firewall rules.
3. Open `docker-compose.yml` or your `.env` file and update `DATABASE_URL`:
   ```env
   DATABASE_URL=postgresql://<admin_user>:<password>@<your-server-name>.postgres.database.azure.com:5432/lazarus?sslmode=require
   ```
4. Run `schema.sql` against the Azure database once to initialize the tables.
5. Re-run `docker compose up -d`. All API operations, workers, and metrics now persist in Azure.
