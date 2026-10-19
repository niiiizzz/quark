# Docker + Lazarus Setup Guide

---

## Part 1: What is Docker? (Quick Primer)

Docker is a tool that packages your app and everything it needs (Python, libraries, Postgres, etc.) into **containers** — isolated boxes that run the same on any computer. Think of it like a shipping container: the contents don't change no matter which ship carries it.

### Key terms you need to know:

| Term | What it means |
|---|---|
| **Image** | A blueprint (like a class in OOP). Built once from a `Dockerfile`. |
| **Container** | A running instance of an image (like an object). |
| **docker-compose** | A tool to run *multiple* containers together — e.g., Postgres + your app. |
| **Volume** | A folder that persists data outside the container (so Postgres data survives restarts). |
| **Port mapping** | `5432:5432` means "port 5432 on your PC → port 5432 inside the container". |

### Why Docker for Lazarus?
- **Zero setup**: No need to install Python or Postgres locally.
- **Reproducible**: Works the same on your machine and in the cloud.
- **One command**: `docker compose up` starts everything.

---

## Part 2: Docker Setup (You Already Have It!)

✅ **Good news**: You already have Docker Desktop installed and running (verified: Docker v29.7.2).

You just need to **sign in** (optional for local use, but needed for pulling images from Docker Hub).

### Sign in to Docker Desktop:
1. Open **Docker Desktop** from your Start Menu / taskbar
2. Click **"Sign In"** in the top-right corner
3. Use your Docker Hub account (free at hub.docker.com) — or skip sign-in entirely, it's optional for local use

> **Note**: Sign-in is only required if you hit rate limits pulling images. For our project, the `postgres:16` image pulls from Docker Hub. Without sign-in you get ~100 pulls/6h which is more than enough.

---

## Part 3: How the Lazarus Docker Setup Works

Your project has **two containers** defined in `docker-compose.yml`:

```
┌──────────────────────────────────────────────────────┐
│                   docker compose up                   │
│                                                       │
│  ┌─────────────────────┐   ┌──────────────────────┐  │
│  │  postgres container │   │   app container      │  │
│  │  Image: postgres:16 │   │  Built from:         │  │
│  │  Port: 5432         │   │  your Dockerfile     │  │
│  │  DB: lazarus        │   │  Port: 8000          │  │
│  │  schema.sql auto-   │   │  Runs: run_all.py    │  │
│  │  applied on startup │   │  (API + Supervisor + │  │
│  │  Volume: pgdata     │   │   Workers)           │  │
│  └─────────────────────┘   └──────────────────────┘  │
│            ▲                         │                │
│            └──── DATABASE_URL ───────┘                │
└──────────────────────────────────────────────────────┘
```

### What happens automatically:
1. **Postgres container starts** → `schema.sql` is applied (creates all 8 tables)
2. **App container waits** until Postgres is healthy (healthcheck every 3s)
3. **App starts** → `run_all.py` launches the API + Supervisor
4. **Supervisor** spawns 3 worker processes automatically
5. **Dashboard** is available at `http://localhost:8000`

---

## Part 4: Step-by-Step — Running Lazarus in Docker

### Step 1: Open a terminal in the project folder

```powershell
# The project skeleton is here:
cd "c:Project"
```

### Step 2: Build the Docker image (one-time, ~2 min)

```powershell
docker compose build
```

This reads the `Dockerfile` and creates the app image with Python 3.12 + all packages from `requirements.txt`.

**You'll see output like:**
```
[+] Building 45.2s
 => [app] FROM python:3.12-slim
 => [app] RUN pip install -r requirements.txt
 => [app] COPY . .
 => exporting to image
```

### Step 3: Start everything

```powershell
docker compose up
```

**Expected output:**
```
lazarus-postgres  | PostgreSQL init process complete; ready for start up.
lazarus-postgres  | database system is ready to accept connections
lazarus-app       | ============================================================
lazarus-app       | Starting Lazarus Async Job Runner & Dashboard...
lazarus-app       | [API] Started PID 7 at http://localhost:8000
lazarus-app       | [Supervisor] Started PID 8
lazarus-app       | [Worker] worker-1-xxxx started
lazarus-app       | [Worker] worker-2-xxxx started
lazarus-app       | [Worker] worker-3-xxxx started
```

### Step 4: Open the dashboard

👉 Open your browser at: **http://localhost:8000**

You should see the Lazarus dashboard with:
- Left sidebar: Overview, Workers, Jobs, DLQ, Chaos Lab, Insights
- 3 healthy workers
- Invariants panel (all green)

### Step 5: Stop everything

```powershell
# Press Ctrl+C in the terminal, OR in a new terminal:
docker compose down
```

> **Data persists!** Postgres data is stored in the `pgdata` Docker volume. Next `docker compose up` picks up where you left off.

### Step 6: Full reset (clean slate)

```powershell
# Stop and delete all containers AND volumes (deletes all job data)
docker compose down -v

# Then start fresh:
docker compose up
```

---

## Part 5: The DATABASE_URL — How it Works

In Docker, the URL is set in `docker-compose.yml`:
```yaml
DATABASE_URL: postgresql://postgres:postgres@postgres:5432/lazarus
```

Breaking it down:
```
postgresql://  postgres  :  postgres  @  postgres  :5432/  lazarus
               ──user──    ─password─   ─hostname─  ─port─  ─db─
```

- `postgres` as hostname = the name of the Postgres container in Docker's internal network
- `postgres` as user/password = set by `POSTGRES_USER` and `POSTGRES_PASSWORD` in docker-compose.yml
- `lazarus` = the database name

### If running app locally (without Docker):
```
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/lazarus
```
(`localhost` because you access from outside Docker)

---

## Part 6: Useful Docker Commands for Development

```powershell
# View logs (both containers)
docker compose logs -f

# View only app logs
docker compose logs -f app

# View only postgres logs
docker compose logs -f postgres

# Connect directly to Postgres (run SQL manually)
docker exec -it lazarus-postgres psql -U postgres -d lazarus

# Rebuild app image (after code changes)
docker compose build app
docker compose up

# Quick rebuild + restart in one command
docker compose up --build

# Check what's running
docker compose ps

# Check resource usage
docker stats
```

---

## Part 7: Connecting to the Database (for inspection)

```powershell
# Interactive SQL shell:
docker exec -it lazarus-postgres psql -U postgres -d lazarus

# Once inside psql:
\dt                          -- list all tables
SELECT * FROM workers;       -- check worker registrations
SELECT count(*) FROM jobs;   -- count jobs
SELECT * FROM settings;      -- check settings
SELECT * FROM counters;      -- view counters
\q                           -- quit
```

---

## Part 8: Common Issues & Fixes

| Issue | Fix |
|---|---|
| `port 5432 already in use` | Another Postgres is running locally. Stop it or change the port in docker-compose.yml: `"5433:5432"` |
| `port 8000 already in use` | Change to `"8001:8000"` and visit http://localhost:8001 |
| App crashes immediately | Check logs: `docker compose logs app` |
| Postgres not ready yet | Wait ~10s; healthcheck handles this automatically |
| Changes to Python code not reflected | Run `docker compose up --build` to rebuild |
| "Cannot connect to Docker daemon" | Open Docker Desktop app and wait for it to start |

---

## Part 9: Azure PostgreSQL — For the Demo Video

> Do this AFTER you have the local Docker demo working perfectly.

### Goal: Same code, just swap DATABASE_URL to point to Azure

### Step 1: Create Azure Flexible Server

1. Go to https://portal.azure.com
2. Search **"Azure Database for PostgreSQL"** → **Create** → **Flexible Server**
3. Settings:
   - Name: `lazarus-pg` → becomes `lazarus-pg.postgres.database.azure.com`
   - Version: **16**
   - Compute: **Burstable, B1ms** (cheapest, ~$12/month)
   - Admin: `lazarusadmin` / your strong password
4. **Networking** tab: **Public access** → Add your current IP + check "Allow Azure services"
5. Create (takes ~5 min)

### Step 2: Create database and apply schema

```powershell
# Option A: Azure Cloud Shell (easiest — built into portal)
# Click the >_ icon in the portal top bar, then:
az postgres flexible-server execute \
  --name lazarus-pg \
  --admin-user lazarusadmin \
  --admin-password YOUR_PASSWORD \
  --database-name lazarus \
  --file-path schema.sql

# Option B: psql locally (if you have it)
psql "host=lazarus-pg.postgres.database.azure.com user=lazarusadmin dbname=lazarus sslmode=require" -f schema.sql
```

### Step 3: Set up initial data (settings table)

```sql
INSERT INTO settings (key, value) VALUES
  ('desired_workers', '3'),
  ('fencing_enabled', 'true'),
  ('outage_flaky_webhook', 'false'),
  ('autoscale', 'manual')
ON CONFLICT (key) DO NOTHING;

INSERT INTO counters (name, value) VALUES
  ('acked', 0), ('stale_writes_rejected', 0),
  ('lease_expired', 0), ('quarantined', 0),
  ('retries_avoided', 0), ('rate_limited', 0)
ON CONFLICT (name) DO NOTHING;
```

### Step 4: Run app pointing to Azure

```powershell
# In your project directory, set the env var and restart:
$env:DATABASE_URL = "postgresql://lazarusadmin:YOUR_PASSWORD@lazarus-pg.postgres.database.azure.com:5432/lazarus?sslmode=require"
docker compose up --build

# OR edit docker-compose.yml temporarily:
# DATABASE_URL: postgresql://lazarusadmin:PWD@lazarus-pg.postgres.database.azure.com:5432/lazarus?sslmode=require
```

### Step 5: Screenshot for judges

Take a screenshot of:
1. Azure Portal showing the Flexible Server running
2. The dashboard at localhost:8000 with Azure PG as the backend
3. Run `SELECT version();` in the Azure portal Query editor to show PostgreSQL 16

---

## Part 10: Project Completion Roadmap

The skeleton is quite complete. Here's what to verify/complete:

### Phase 1: Get it running in Docker (Now)
- [ ] `docker compose up` → dashboard loads at localhost:8000
- [ ] 3 workers show as healthy
- [ ] `/healthz` returns `{"status": "ok"}`

### Phase 2: Verify core functionality
- [ ] Run chaos test → PASS
- [ ] Kill a worker → see it restart
- [ ] Freeze a worker → see stale writes rejected
- [ ] Flip fencing OFF → rerun → FAIL with duplicates

### Phase 3: Polish & Azure
- [ ] Switch DATABASE_URL to Azure Flexible Server
- [ ] Screenshot for judges
- [ ] Record 3-4 min demo video

