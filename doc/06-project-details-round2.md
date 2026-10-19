# 06 · Project Details for Round 2: Lazarus (Team Quark)

The judge-facing dossier: everything an evaluator may ask about the prototype, in one place. Fill every `[fill]` and `[measure]` from your real build before submitting. **Do not leave guessed numbers in this document.**

---

## 1. One-liner

**Lazarus is an Azure-oriented job runner that assumes workers will crash, and proves, live, that no job is lost and no effect is applied twice.**

## 2. The problem (from our submitted statement)

Cloud apps run payments, emails and webhooks through asynchronous queues. Naive queues fail predictably: a crashed worker loses its job; an expired timeout lets a second worker run the same job (a double charge); blind retries turn one outage into a retry storm; one poison job crash-loops the fleet; one noisy tenant starves the rest; and nobody can prove nothing was lost.

## 3. Our solution in three claims

| Claim | Mechanism | How the prototype proves it |
|---|---|---|
| **Durable and fenced** | Commit to PostgreSQL before acknowledging; time-limited leases; fencing tokens reject stale writes | Kill and freeze workers under load; invariants stay green; Fencing OFF makes them fail |
| **Self-healing and contained** | Reaper, jittered backoff, DLQ with replay, quarantine, per-job-type circuit breaker | Poison job gets quarantined; outage opens the breaker; replay from DLQ |
| **Provable** | Chaos harness + live invariant checker + dashboard | Report card PASS/FAIL with numbers |

## 4. What exists in the prototype

### 4.1 Feature list (tick what is built before submitting)

| Area | Feature | Status |
|---|---|---|
| Core | Idempotent enqueue (201 / 200) | [ ] |
| Core | Claim with `SKIP LOCKED`, lease, fence | [ ] |
| Core | Batch heartbeat + lease-lost cancellation | [ ] |
| Core | Fenced complete with transactional ledger effect | [ ] |
| Core | Retry with exponential backoff + full jitter | [ ] |
| Core | Reaper (expired lease reclaim) | [ ] |
| Core | DLQ + replay (single and all) | [ ] |
| Core | Delayed jobs | [ ] |
| Core | Graceful SIGTERM drain | [ ] |
| Runtime | Supervisor with auto-restart; scale 1-8 | [ ] |
| Runtime | Kill and Freeze commands | [ ] |
| Handlers | ledger_credit, flaky_webhook, validate_order, legacy_sync, report_export, poison_pill | [ ] |
| Novelty 1 | Live invariants (I1-I5) + chaos report card | [ ] |
| Novelty 2 | Zombie workers caught + Fencing ON/OFF demonstration | [ ] |
| Novelty 3 | Learned retry classification (Beta posterior) | [ ] |
| Novelty 4 | Circuit breaker + outage simulation | [ ] |
| Novelty 5 | Forecast + worker recommendation (+ slow-job flag if time) | [ ] |
| Novelty 6 | Poison quarantine from silent crashes | [ ] |
| Dashboard | Overview, Workers, Jobs, DLQ, Chaos Lab, Insights | [ ] |
| Platform | One-command run, README, pytest suite | [ ] |
| Azure | Azure Database for PostgreSQL in the demo | [ ] |
| Azure | App Insights traces / Container Apps deploy (optional) | [ ] |

### 4.2 What is real vs simulated (say this first)

| Real | Simulated in R2 | Real in R3 |
|---|---|---|
| Postgres state, SQL claim/lease/fence, reaper, retries, DLQ, quarantine, breaker, learning, invariants, chaos | Worker "replicas" are OS processes run by a supervisor on one host | Container Apps replicas |
| Process death on Kill (`os._exit`) and real blocked freeze | KEDA autoscaling (recommender + Apply button) | Real KEDA scale rule |
| | Downstream systems (handlers that fail on purpose) | Real integrations |

### 4.3 Job types used in the demo

| Type | Purpose |
|---|---|
| `ledger_credit` | The "money" job: writes one ledger row in the completing transaction. Used for exactly-once proof |
| `flaky_webhook` | Random 503s; outage toggle for the breaker demo |
| `validate_order` | Permanent validation errors → straight to DLQ |
| `legacy_sync` | Some records always fail with a transient-looking error → learned as permanent |
| `report_export` | Occasionally very slow (anomaly flag, P2) |
| `poison_pill` | Silently kills its worker → quarantine |

## 5. Architecture summary

- **API (FastAPI):** idempotent enqueue, queries, settings, worker commands, chaos runs; serves the dashboard.
- **Supervisor:** spawns/restarts workers; runs the reaper, breaker evaluation and forecast.
- **Workers:** pull jobs; hold leases; heartbeat; commit effect + completion in one fenced transaction.
- **PostgreSQL:** the queue and the source of truth (local container; Azure Database for PostgreSQL when `DATABASE_URL` points to it).
- **Dashboard:** polling single-page UI (paper-and-ink "logbook" design).

Diagrams: `02-architecture-workflow.md`. Design rationale and failure analysis: `05-system-design.md`.

## 6. Tech stack (summary)

Python 3.12 · FastAPI · Pydantic v2 · psycopg 3 (async, raw SQL) · PostgreSQL 16 · asyncio workers + subprocess supervisor · stdlib statistics · HTML/CSS/Alpine.js/Chart.js (vendored) · pytest · Docker Compose · optional Azure PostgreSQL / Container Apps / Application Insights. Details: `01-tech-stack.md`.

## 7. Demo walkthrough (3.5-4 min)

1. **Frame (20 s):** "Failure is the normal case, and we prove it." Idle dashboard, workers healthy, invariants green.
2. **Chaos under load (40 s):** run the chaos test; kill one worker by hand; show its jobs reclaimed.
3. **Zombie (40 s):** freeze a worker; show `stale writes rejected` increment.
4. **Proof (30 s):** report card **PASS**, zero duplicates.
5. **Break it on purpose (40 s):** Fencing OFF → rerun → **FAIL** with duplicate ledger rows ("the double charge from our problem statement"); turn Fencing ON.
6. **Intelligence (30 s):** outage → breaker opens; Insights shows learned-permanent verdict and the forecast recommendation; Apply.
7. **Containment (10 s):** inject a poison job → quarantined; DLQ → Replay. End on the invariants panel.

## 8. Results (fill from your own runs only)

| Measurement | Value |
|---|---|
| Chaos run size / configuration | [fill: jobs, tenants, kill interval] |
| Result of ≥ 3 consecutive runs, Fencing ON | [fill: PASS ×N] |
| Result with Fencing OFF | [fill: duplicates observed: N] |
| Worker kills survived in a run | [fill] |
| Time from kill to job completion elsewhere (median / max) | [measure] |
| Stale writes rejected in a typical run | [fill] |
| Throughput at 3 workers (jobs/s, steady state) | [measure] |
| Retries avoided by learned-permanent | [fill] |
| Test suite | [fill: N tests passing] |

## 9. Novelty (the six cards from our deck, as implemented)

1. **Proof, not promises:** live invariants + chaos report with PASS/FAIL.
2. **Zombie workers caught:** fencing token (`WHERE fence = $mine`) with a toggle that shows the bug when disabled.
3. **Learns what to retry:** per (job type, error fingerprint) posterior; early DLQ when retries almost never succeed.
4. **Stops retry storms:** per-job-type circuit breaker with held-back gauge.
5. **Sees trouble coming:** Holt forecast of arrival rate + Little's-law worker recommendation.
6. **Catches silent crashes:** quarantine after repeated lease expiries with no failure report.

## 10. Known limitations (volunteer these)

- Exactly-once holds for effects inside our database; external effects need the downstream to honour the forwarded idempotency key.
- A hung handler that keeps heartbeating is not detected yet (per-type timeouts planned).
- Single control loop; no leader election yet.
- No fair claiming across tenants yet; rate limit is an admission control (if built).
- Autoscaling is simulated; KEDA arrives in Round 3.
- The "learning" is deliberately small and explainable, not a trained model.
- No authentication; demo-only endpoints (reset) exist.

## 11. Round 3 roadmap

| Week/Phase | Deliverable |
|---|---|
| 1 | Azure deployment: Container Apps API + worker replicas, ACR, Azure PostgreSQL, secrets |
| 1 | KEDA scale rule on queue depth; OpenTelemetry → Application Insights traces for full job lifecycle |
| 2 | Advisory-lock leader election; fair per-tenant claiming; handler timeouts |
| 2 | React dashboard with SSE; SDK (`@job` decorator) and CLI (DLQ replay) |
| 3 | Benchmarks and published methodology; failure-injection test suite in CI |
| 3 | Authentication, tenant isolation, Key Vault; Service Bus ingestion adapter (optional) |

## 12. Judge Q&A prep

| Question | Answer (short) |
|---|---|
| **Why Postgres instead of Service Bus/Kafka/Redis?** | Effect and completion must commit atomically. With a broker you can ack and crash before applying the effect, or apply and crash before ack. A single transaction in the same store as the effect removes that gap. Service Bus is a planned ingestion adapter |
| **Is it exactly-once?** | No system gives exactly-once delivery across failures. We do at-least-once delivery plus fenced, transactional effects, so effects inside our database happen once. For external systems, we forward an idempotency key and say so |
| **What stops a zombie worker?** | Every claim increments `fence`; every write is `WHERE fence = $mine`. A stale write matches zero rows and its transaction rolls back. The Fencing OFF demo shows the duplicate it prevents |
| **Why not just use a bigger visibility timeout?** | It only shifts the window. A worker can always pause longer than any timeout; fencing makes that safe |
| **How do you prove nothing was lost?** | Five invariants evaluated every second plus a chaos report; I1 compares acknowledged enqueues to rows and accounts for every state |
| **What if Postgres is down?** | Nothing is lost and nothing progresses; state is as durable as Postgres. Azure Flexible Server HA is the R3 answer |
| **Clock skew?** | Only the database clock is used for leases and `run_at` |
| **Won't SKIP LOCKED bottleneck?** | It removes lock waits; the ceiling is the single database. We measured [fill] jobs/s and describe the R3 path (partitioning, tenant fairness) rather than claim numbers |
| **Poison-job detection false positives?** | Threshold of 3 lease expiries with no failure report; the drawer shows the timeline so an operator can replay. Tunable |
| **What does the ML actually do?** | A Beta posterior over "does a retry succeed" per (job type, error fingerprint), and Holt smoothing for load. Small, explainable, CPU-only. We suspend learning during breaker episodes so outages are not mistaken for bad jobs |
| **How is the breaker different from backoff?** | Backoff spaces one job's retries; the breaker pauses claims for a whole job type when the dependency looks down, so retries do not burn attempts or hammer it |
| **How do you scale it?** | Stateless pull workers scale horizontally; KEDA on queue depth in R3; leader-elected reaper; fair claim for tenants |
| **What about noisy tenants?** | Admission rate limiting now (if built); fair claim in R3 |
| **Where is Azure in this?** | State in Azure Database for PostgreSQL in the demo [fill: yes/no]; Container Apps, ACR, App Insights and KEDA are the R3 deployment, designed already |
| **What would you do with two more weeks?** | Section 11 |
| **How did you test it?** | pytest on real Postgres for claim/fence/reaper/idempotency, plus repeated chaos runs: [fill] |

## 13. Submission checklist (confirm format with the organizers)

- [ ] Repo with README, one-command run, screenshots
- [ ] Demo video 3-4 min (recorded before the deadline) and/or live demo plan
- [ ] Architecture diagram + prototype-vs-vision table
- [ ] This dossier with all `[fill]`/`[measure]` replaced
- [ ] Screenshots: Overview, Chaos Lab PASS, Chaos Lab FAIL (Fencing OFF), Insights
- [ ] Azure evidence if available: Azure PG screenshot, App Insights trace
- [ ] Rehearsed 3×; fallback video ready; laptop charged; app tested with Wi-Fi off

## 14. Glossary

| Term | Meaning |
|---|---|
| Lease | Time-limited right for a worker to run a job; expires if not renewed |
| Fencing token | Counter incremented on each claim; writes carrying an old value are rejected |
| Reaper | Loop that requeues jobs whose leases expired |
| DLQ | Dead-letter queue: jobs that will not be retried automatically (`status = dead`) |
| Poison job | A job that crashes its worker every time |
| Zombie worker | A paused/partitioned worker that resumes after its lease was reassigned |
| Idempotency key | Client-supplied key so a retried submission maps to the same job |
| Invariant | A property that must always hold (I1-I5), checked continuously |
| Breaker | Per-job-type switch that pauses claims when failures spike |
