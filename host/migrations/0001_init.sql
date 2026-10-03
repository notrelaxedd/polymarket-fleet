-- Step 1: fleet core. Money columns (later migrations) are bigint cents.
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     text PRIMARY KEY,
  applied_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE settings (
  key         text PRIMARY KEY,
  value       jsonb NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO settings (key, value) VALUES
  ('live_enabled',          'false'),
  ('kill_switch',           'false'),
  ('tz',                    '"America/New_York"'),
  ('lease_seconds',         '30'),
  ('heartbeat_seconds',     '5'),
  ('online_after_seconds',  '15'),
  ('max_expiries',          '3'),
  ('liquidity_floor_cents', '50000'),
  ('max_bet_cents',         '2500'),
  ('max_daily_loss_cents',  '{"live": 30000, "paper": 100000}'),
  ('default_bankroll_cents','10000'),
  ('min_edge',              '0.03'),
  ('kelly_fraction',        '0.25'),
  ('trade_max_games',       '6');

CREATE TABLE workers (
  id                text PRIMARY KEY,
  name              text NOT NULL,
  token_hash        text NOT NULL,
  desired_role      text NOT NULL DEFAULT 'idle'
                    CHECK (desired_role IN ('idle','backtest','model_search','train','trade')),
  role_epoch        bigint NOT NULL DEFAULT 1,
  reported_role     text NOT NULL DEFAULT 'idle',
  acked_epoch       bigint NOT NULL DEFAULT 0,
  auto_role         boolean NOT NULL DEFAULT false,
  enabled           boolean NOT NULL DEFAULT true,
  cpu_pct           real,
  ram_used_mb       integer,
  ram_total_mb      integer,
  skew_ms           integer,
  python_version    text,
  code_version      text,
  hostname          text,
  boot_id           text,
  remote_ip         text,
  registered_at     timestamptz NOT NULL DEFAULT now(),
  last_heartbeat_at timestamptz
);

CREATE TABLE enroll_tokens (
  token_hash        text PRIMARY KEY,
  created_at        timestamptz NOT NULL DEFAULT now(),
  expires_at        timestamptz NOT NULL,
  used_by_worker_id text REFERENCES workers(id),
  used_at           timestamptz
);

CREATE TABLE jobs (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind              text NOT NULL,
  role              text NOT NULL CHECK (role IN ('backtest','model_search','train','trade')),
  status            text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','leased','cancel_requested','succeeded','failed','cancelled')),
  params            jsonb NOT NULL DEFAULT '{}'::jsonb,
  checkpoint        jsonb,
  progress          real NOT NULL DEFAULT 0,
  target_worker_id  text REFERENCES workers(id),
  lease_worker_id   text REFERENCES workers(id),
  lease_token       uuid,
  lease_expires_at  timestamptz,
  expiries          integer NOT NULL DEFAULT 0,
  max_expiries      integer DEFAULT 3,
  preempt_requested boolean NOT NULL DEFAULT false,
  run_after         timestamptz NOT NULL DEFAULT now(),
  idempotency_key   text UNIQUE,
  result            jsonb,
  error             text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  started_at        timestamptz,
  finished_at       timestamptz,
  updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_queued_idx ON jobs (role, created_at) WHERE status = 'queued';
CREATE INDEX jobs_leased_idx ON jobs (lease_expires_at) WHERE status IN ('leased','cancel_requested');
CREATE INDEX jobs_lease_worker_idx ON jobs (lease_worker_id) WHERE status IN ('leased','cancel_requested');
CREATE INDEX jobs_target_idx ON jobs (target_worker_id) WHERE status IN ('queued','leased','cancel_requested');

CREATE TABLE job_events (
  id        bigserial PRIMARY KEY,
  job_id    uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  ts        timestamptz NOT NULL DEFAULT now(),
  worker_id text,
  event     text NOT NULL,
  detail    jsonb
);
CREATE INDEX job_events_job_idx ON job_events (job_id, id);

CREATE TABLE audit_log (
  id                bigserial PRIMARY KEY,
  ts                timestamptz NOT NULL DEFAULT now(),
  actor             text,
  ip                text,
  action            text NOT NULL,
  entity            text,
  before            jsonb,
  after             jsonb,
  confirmation_text text
);
