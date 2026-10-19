# PROGRESS · Lazarus Prototype Audit and Progress Log

*Created: 2026-10-19*

---

## 1. Document Inventory

| Document | Path | Role / Notes |
|---|---|---|
| Scope authority | `PRD.md` (copied from `doc/reference/lazarus-prototype-prd.md`) | Scope authority, SQL contract §6, APIs, novelty specs, build plan |
| Active spec | `doc/01-tech-stack.md` | Tech stack, dependencies, connection budget, AGENTS rules |
| Active spec | `doc/02-architecture-workflow.md` | Architecture, workflows, sequence diagrams, `/api/overview` contract |
| Active spec | `doc/03-ui-ux-palette.md` | Paper/ink/ember palette, UI tokens, components (supersedes PRD §10) |
| Active spec | `doc/04-pages-and-button-workflow.md` | Navigation, pages, buttons, endpoints, empty/loading states |
| Context only | `doc/05-system-design.md` | Rationale and failure analysis |
| Context only | `doc/06-project-details-round2.md` | Judge dossier (not edited, preserved) |

---

## 2. Precedence and Architecture Decisions

1. **SQL Contract**: Verbatim implementation of PRD §6 schema and query definitions in `app/queue.py`. No ORM. Raw SQL using `psycopg 3` async.
2. **Frontend UI**: Strictly following `doc/03-ui-ux-palette.md` (paper `#F2EEE3`, ink `#1C1B18`, ember `#C8401A`, no gradients, no shadows, rectangular borders, Alpine.js 3 + Chart.js 4 vendored in `static/vendor/`).
3. **Approved API Additions**: `POST /api/chaos/run/stop`, `GET /api/events?worker=`, and demo reset guard (`LAZARUS_ALLOW_RESET=1`).
4. **Environment / OS**: Running on Windows host with Python 3.14 (`asyncio.WindowsSelectorEventLoopPolicy` enforced for `psycopg` async loop compatibility).
5. **Database Strategy**: Real PostgreSQL 16 required. Since Docker daemon was not running, local portable PostgreSQL 16 binaries or remote PostgreSQL target (`DATABASE_URL`) will be used to ensure genuine ACID and concurrency tests.

---

## 3. Milestone Tracking

| Milestone | Description | Status | Evidence / Notes |
|---|---|---|---|
| **M0** | Scaffold (layout, config, db pool, schema.sql, healthz, run_all.py, .env.example) | IN PROGRESS | Repo layout created, dependencies verified |
| **M1** | Core queue (`queue.py`, SQL contract) + pytest concurrency | PENDING | |
| **M2** | Workers + supervisor + handlers + kill/freeze | PENDING | |
| **M3** | API + `/api/overview` + routes | PENDING | |
| **M4** | Dashboard v1 (Overview, Workers, Jobs, DLQ, paper/ink theme) | PENDING | |
| **Checkpoint A** | P0 items end-to-end audit | PENDING | |
| **M5** | Chaos Lab + report card (PASS/FAIL) + Fencing toggle + quarantine | PENDING | |
| **Checkpoint B** | Chaos PASS x3 + Fencing OFF FAIL verification | PENDING | |
| **M6** | Circuit breaker, learned retries, forecast + Apply | PENDING | |
| **M7** | Polish (README, screenshots, demo reset, rehearsal) | PENDING | |
| **M8** | Optional Azure PostgreSQL verification | PENDING | |

---

## 4. Test Results Log

*(Will be populated with actual pytest and benchmark logs as milestones complete)*

---

## 5. Known Defects & Blockers

- Docker Desktop engine not running on host; setting up portable local PostgreSQL 16 engine or connecting via DATABASE_URL to execute real SQL concurrency tests.
