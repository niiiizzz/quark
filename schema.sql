-- ==============================================================================
-- Lazarus: Reliable Asynchronous Job Runner
-- Schema Definition (PRD Section 6 verbatim contract)
-- ==============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id text NOT NULL,
  type text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}',
  idempotency_key text,
  priority int NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','succeeded','dead')),
  run_at timestamptz NOT NULL DEFAULT now(),
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 5,
  lease_owner text,
  lease_expires_at timestamptz,
  fence bigint NOT NULL DEFAULT 0,
  crash_count int NOT NULL DEFAULT 0,
  quarantined boolean NOT NULL DEFAULT false,
  last_error text,
  error_class text,                 -- transient | permanent | learned_permanent | lease_expired
  last_fp text,                     -- error fingerprint of the last failure
  result jsonb,
  started_at timestamptz,
  duration_ms int,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs (priority DESC, run_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS jobs_lease_idx ON jobs (lease_expires_at) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);

CREATE TABLE IF NOT EXISTS job_events (
  id bigserial PRIMARY KEY,
  job_id uuid,
  job_type text,
  event text NOT NULL,   -- enqueued|claimed|succeeded|failed|lease_expired|dead|quarantined|stale_write_rejected|replayed|retry_avoided
  worker text,
  attempt int,
  detail jsonb,
  at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS job_events_at_idx ON job_events (at);
CREATE INDEX IF NOT EXISTS job_events_job_idx ON job_events (job_id);

CREATE TABLE IF NOT EXISTS workers (
  worker_id text PRIMARY KEY,
  name text,
  pid int,
  started_at timestamptz DEFAULT now(),
  last_seen timestamptz DEFAULT now(),
  in_flight int DEFAULT 0,
  command text,            -- NULL | 'kill' | 'freeze'
  status text DEFAULT 'healthy'                     -- healthy | frozen | dead
);

-- Deliberately NO unique constraint on job_id: this table is the proof of exactly-once.
CREATE TABLE IF NOT EXISTS ledger (
  id bigserial PRIMARY KEY,
  job_id uuid NOT NULL,
  tenant_id text,
  account text,
  amount numeric,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS settings (
  key text PRIMARY KEY,
  value text NOT NULL
);

-- Seed default settings
INSERT INTO settings (key, value) VALUES
  ('desired_workers', '3'),
  ('fencing_enabled', 'true'),
  ('outage_flaky_webhook', 'false'),
  ('autoscale', 'manual')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS counters (
  name text PRIMARY KEY,
  value bigint NOT NULL DEFAULT 0
);

-- Seed default counters
INSERT INTO counters (name, value) VALUES
  ('acked', 0),
  ('stale_writes_rejected', 0),
  ('lease_expired', 0),
  ('quarantined', 0),
  ('retries_avoided', 0),
  ('rate_limited', 0)
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS retry_stats (
  job_type text,
  fp text,
  successes int NOT NULL DEFAULT 0,
  failures int NOT NULL DEFAULT 0,
  PRIMARY KEY (job_type, fp)
);

CREATE TABLE IF NOT EXISTS breakers (
  job_type text PRIMARY KEY,
  open_until timestamptz,
  window_start timestamptz DEFAULT now(),
  opened_count int NOT NULL DEFAULT 0
);
