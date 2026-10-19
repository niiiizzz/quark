# 01 · Tech Stack: Lazarus Prototype

Companion to `lazarus-prototype-prd.md`. Where this file and PRD §5 or §10 disagree, **this file wins** (see §4: Tailwind dropped, palette changed).

**Principles:** one language end to end · no build step · boring tech that AI tools write reliably · every dependency must earn its place · the demo must survive bad venue Wi-Fi.

---

## 1. Stack at a glance

| Layer | Choice | Why | Round 3 |
|---|---|---|---|
| Language | Python 3.12 | One language for API, workers, supervisor, chaos, stats. Fastest to build with Antigravity | same |
| API framework | FastAPI | Typed routes, free OpenAPI docs at `/docs` (useful live in the demo) | same |
| ASGI server | uvicorn | Standard pairing | + gunicorn workers if needed |
| Validation | Pydantic v2 | Request/response models double as the dashboard JSON contract | same |
| Database | PostgreSQL 16 | `FOR UPDATE SKIP LOCKED`, transactions, partial indexes. The whole design depends on it | Azure Database for PostgreSQL Flexible Server |
| DB driver | psycopg 3 (`psycopg[binary,pool]`), async, **raw SQL** | The queue SQL must be exact and reviewable. No ORM | same |
| Worker runtime | `asyncio` per process | Many in-flight jobs per worker; a real OS process per worker so kill/freeze is real | Container Apps replicas |
| Supervisor | `asyncio.create_subprocess_exec` | Spawns, watches and restarts worker processes; hosts the control loop | Container Apps + KEDA |
| Statistics ("ML") | Python stdlib `math` only | Beta posterior, Holt smoothing: tiny, explainable, no install risk | scikit-learn only if justified |
| Frontend | Hand-written HTML + CSS custom properties + **Alpine.js 3** + **Chart.js 4** | No build step, no framework churn, full control of the visual identity | React + Vite |
| Fonts | IBM Plex Sans, IBM Plex Mono, Fraunces (titles only) | Distinctive, readable, tabular numerals. Self-hosted woff2 | same |
| Tests | pytest + pytest-asyncio + httpx | Real Postgres in compose; concurrency bugs do not appear with mocks | + load tests |
| Packaging | Docker + `docker-compose.yml`, plus `run_all.py` | One-command run, with or without Docker for the app | Azure Container Registry |
| Azure (prototype) | Azure Database for PostgreSQL via `DATABASE_URL` (P1); Container Apps + App Insights (P2) | See §6 | full topology |

---

## 2. Backend decisions

- **One image, three roles.** `python -m app.api`, `python -m app.supervisor`, `python -m app.worker`. `run_all.py` starts API + supervisor; the supervisor starts workers.
- **Raw SQL in one module** (`app/queue.py`). Every state transition is a named function with its SQL in a constant. Reviewers (and judges) can read the reliability logic in one file.
- **Connection budget** (Postgres default `max_connections` is 100): API pool 10 + supervisor pool 3 + workers 8 × pool 3 = 37. Do not use a transaction-mode pooler; it breaks session behaviour we rely on in R3 (advisory locks).
- **Time source:** the database `now()` for leases and `run_at`. App clocks are never compared with each other.
- **Settings and toggles live in the DB** (`settings` table), read by workers on each claim cycle (cached ≤ 1s). This lets the dashboard flip Fencing / Outage live across processes without IPC.
- **No message broker in R2.** Postgres is the queue and the source of truth. Service Bus is a Round 3 adapter, not the core (effect and completion must commit atomically).

## 3. Database decisions

- Schema and SQL contract: **PRD §6, verbatim.** Do not let agents "improve" it.
- Extensions: `pgcrypto` (UUIDs). Nothing else.
- Local: `postgres:16` in compose, volume-mounted, `max_connections` default.
- Azure option: same `DATABASE_URL` with `sslmode=require`. Needs a firewall rule for your client IP (or "allow Azure services" for a deployed container). Verify in the portal; I have not checked your subscription.

## 4. Frontend decisions (supersedes PRD §5 / §10)

| Decision | Reason |
|---|---|
| **No Tailwind.** Plain CSS with custom properties | Tailwind defaults are a large part of the "AI-generated" look. Tokens in CSS variables give a deliberate identity (see `03-ui-ux-palette.md`) and remove a CDN dependency |
| **Alpine.js** for state and rendering | Declarative (`x-data`, `x-for`, `x-show`): fewer DOM-glue bugs from agent-written vanilla JS |
| **Chart.js 4** for charts | Canvas, small, easy to restyle flat |
| **Polling, not SSE** | `GET /api/overview` every 1s. Simpler and robust; SSE is R3 |
| **Vendor everything** into `static/vendor/` and `static/fonts/` | Venue Wi-Fi must not be able to break the demo |
| Single page, tab switching client-side | No router, no build, one `index.html` |

Vendor list (download once while online, commit to the repo): `alpine.min.js` (Alpine 3, from jsDelivr `alpinejs@3/dist/cdn.min.js`), `chart.umd.min.js` (Chart.js 4, from jsDelivr `chart.js@4/dist/chart.umd.min.js`), IBM Plex Sans / IBM Plex Mono / Fraunces as `woff2` (via the `@fontsource` packages or Google Fonts download). Confirm the exact URLs when you fetch them.

```
static/
  index.html
  app.css            # tokens + components
  app.js             # Alpine stores + fetch helpers
  vendor/alpine.min.js
  vendor/chart.umd.min.js
  fonts/*.woff2
```

## 5. Testing stack

| Test | Tool | Gate |
|---|---|---|
| 200 concurrent claimers over 1,000 jobs: no double claim | pytest-asyncio, real Postgres | must pass before Sun 14:00 |
| Fenced complete rejects stale write and rolls back the ledger row | same | must pass |
| Reaper reclaims expired lease; `crash_count` and quarantine at threshold | same | must pass |
| Idempotent enqueue (201 then 200, same id) | httpx against the app | must pass |
| Backoff bounds `[0, min(cap, base·2^n)]` | pure unit test | must pass |
| End-to-end chaos PASS ×3 | Chaos Lab itself | before freeze |

## 6. Azure plan (be explicit about what is real)

| Azure service | Round 2 status | How |
|---|---|---|
| Azure Database for PostgreSQL Flexible Server | **P1: do this one if you do only one** | Change `DATABASE_URL`. Zero code change, and the demo state then really lives in the cloud |
| Azure Container Apps | P2 | One container (API + supervisor + workers) from the Dockerfile |
| Azure Container Registry | P2 | Only needed for the Container Apps deploy |
| Application Insights (OpenTelemetry) | P2 | `azure-monitor-opentelemetry`, enabled only if `APPLICATIONINSIGHTS_CONNECTION_STRING` is set |
| KEDA scale rule | **Not in R2** | Simulated by the recommender + Apply button; real in R3 |

Judging lens: the problem statement named Azure. Minimum credible evidence = Azure PG in the demo + one App Insights trace screenshot, plus the architecture slide. State plainly in the demo what is simulated.

## 7. Dependencies

`requirements.txt` (pin exact versions the first time you install, then freeze):

```
fastapi
uvicorn[standard]
pydantic
psycopg[binary,pool]
httpx
pytest
pytest-asyncio
# optional (P2)
azure-monitor-opentelemetry
```

Rule for agents: **no new dependency without telling the team.** The frontend has zero npm dependencies.

## 8. Antigravity setup

Put the PRD at repo root as `PRD.md` and the following as `AGENTS.md` (mirror it in whatever rules or instructions file your Antigravity version reads):

```
# Rules for agents working on Lazarus
1. Read PRD.md first. Section 6 (SQL contract) is law: do not change its semantics.
2. Raw SQL via psycopg 3. No ORM. All queue SQL lives in app/queue.py.
3. Every job state write after claim is fenced (WHERE id=$1 AND fence=$2 AND status='running'), except the explicit unsafe mode.
4. Use database now() for leases and run_at. Never compare app clocks.
5. No new dependencies without asking. Frontend: no npm, no CDN at runtime (vendored files only).
6. Frontend design must follow 03-ui-ux-palette.md tokens exactly. No Tailwind, no gradients, no emoji icons.
7. Run pytest before reporting a task done. Show the output.
8. Do not add features that are not in PRD.md. If something is missing, ask.
9. Keep functions small; name SQL constants after the transition they implement.
10. Never leave TODO stubs in P0 paths. If you cannot finish, say so.
```

## 9. Rejected alternatives

| Option | Why not (for R2) |
|---|---|
| React + Vite + Tailwind | Build step, dependency risk, and it produces the default template look |
| Redis / RabbitMQ / Celery | Moves the reliability story out of code we can show; breaks effect + completion atomicity |
| Azure Service Bus as core | Same atomicity problem; kept as R3 adapter |
| Azure Functions / Durable Functions | Hides the exact mechanics (lease, fence) we are being judged on |
| AKS | Setup tax, no benefit at this scale |
| SQLite for local | No `SKIP LOCKED`; would invalidate the proof |
| SQLAlchemy / any ORM | Hides the SQL that is the product |
| WebSockets / SSE | Polling is enough for a 1s dashboard; fewer failure modes |
| scikit-learn / NumPy | The statistics are ~60 lines of stdlib; avoids install and build issues |

## 10. Tech risks

| Risk | Mitigation |
|---|---|
| CDN or font fetch fails on stage | Everything vendored locally |
| Agent rewrites the SQL "for cleanliness" | AGENTS.md rule 1 + the concurrency and fencing tests as a gate |
| Too many Postgres connections | Pool sizes in §2; workers capped at 8 |
| Windows vs Linux process semantics (`os._exit`, signals) | Test kill/freeze on the exact machine you will demo from. Docker (Linux) is the safest runtime |
| Azure PG latency makes the chaos run slow | Keep local compose as the demo default; Azure PG as a second run or screenshot |
