-- Fleet workloads (docs/workloads-design.md). New tables only: no existing table is altered,
-- so the Polymarket queue, trading and settings stay exactly as they were.

CREATE TABLE workloads (
  name          text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9-]{1,31}$'),
  manifest      jsonb NOT NULL,
  image_repo    text NOT NULL,
  image_digest  text CHECK (image_digest IS NULL OR image_digest ~ '^sha256:[0-9a-f]{64}$'),
  image_size_mb integer,
  enabled       boolean NOT NULL DEFAULT true,
  synced_at     timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE machines (
  id                   text PRIMARY KEY,
  name                 text NOT NULL,
  token_hash           text NOT NULL,
  prev_token_hash      text,
  hostname             text,
  boot_id              text,
  remote_ip            text,
  agent_version        text,
  docker_version       text,
  docker_ok            boolean NOT NULL DEFAULT false,
  arch                 text,
  cpu_count            integer,
  cpu_pct              real,
  ram_total_mb         integer,
  ram_used_mb          integer,
  disk_type_detected   text NOT NULL DEFAULT 'unknown'
                       CHECK (disk_type_detected IN ('ssd','hdd','flash','unknown')),
  disk_type_override   text CHECK (disk_type_override IN ('ssd','hdd','flash','unknown')),
  disk_size_mb         bigint,
  disk_free_mb         bigint,
  docker_root          text,
  low_disk             boolean NOT NULL DEFAULT false,
  native_polymarket    text NOT NULL DEFAULT 'absent'
                       CHECK (native_polymarket IN ('active','inactive','absent')),
  polymarket_worker_id text REFERENCES workers(id),
  pinned               boolean NOT NULL DEFAULT false,
  pinned_reason        text,
  pinned_at            timestamptz,
  enabled              boolean NOT NULL DEFAULT true,
  registered_at        timestamptz NOT NULL DEFAULT now(),
  last_heartbeat_at    timestamptz
);
CREATE INDEX machines_remote_ip_idx ON machines (remote_ip);
CREATE INDEX machines_boot_id_idx ON machines (boot_id);

CREATE TABLE machine_enroll_tokens (
  token_hash         text PRIMARY KEY,
  created_at         timestamptz NOT NULL DEFAULT now(),
  expires_at         timestamptz NOT NULL,
  used_by_machine_id text REFERENCES machines(id),
  used_at            timestamptz
);

CREATE TABLE workload_assignments (
  machine_id           text PRIMARY KEY REFERENCES machines(id) ON DELETE CASCADE,
  workload             text REFERENCES workloads(name),
  epoch                bigint NOT NULL DEFAULT 1,
  acked_epoch          bigint NOT NULL DEFAULT 0,
  state                text NOT NULL DEFAULT 'stopped'
                       CHECK (state IN ('pending','starting','running','draining','stopped','failed')),
  draining_to          text REFERENCES workloads(name),
  draining_to_set      boolean NOT NULL DEFAULT false,
  run_token_hash       text,
  assigned_by          text,
  assigned_at          timestamptz NOT NULL DEFAULT now(),
  container_id         text,
  image_digest_running text,
  started_at           timestamptz,
  last_exit_code       integer,
  restarts             integer NOT NULL DEFAULT 0,
  last_error           text,
  cpu_pct              real,
  mem_mb               integer,
  updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX workload_assignments_run_token_idx
  ON workload_assignments (run_token_hash) WHERE run_token_hash IS NOT NULL;

CREATE TABLE workload_jobs (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workload          text NOT NULL REFERENCES workloads(name),
  kind              text NOT NULL,
  status            text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','leased','cancel_requested','succeeded','failed','cancelled')),
  params            jsonb NOT NULL DEFAULT '{}'::jsonb,
  checkpoint        jsonb,
  progress          real NOT NULL DEFAULT 0,
  target_machine_id text REFERENCES machines(id),
  lease_machine_id  text REFERENCES machines(id),
  lease_epoch       bigint,
  lease_token       uuid,
  lease_expires_at  timestamptz,
  expiries          integer NOT NULL DEFAULT 0,
  max_expiries      integer DEFAULT 3,
  run_after         timestamptz NOT NULL DEFAULT now(),
  idempotency_key   text UNIQUE,
  result            jsonb,
  error             text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  started_at        timestamptz,
  finished_at       timestamptz,
  updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX workload_jobs_queued_idx ON workload_jobs (workload, created_at) WHERE status = 'queued';
CREATE INDEX workload_jobs_leased_idx ON workload_jobs (lease_expires_at)
  WHERE status IN ('leased','cancel_requested');
CREATE INDEX workload_jobs_lease_machine_idx ON workload_jobs (lease_machine_id)
  WHERE status IN ('leased','cancel_requested');

CREATE TABLE workload_job_events (
  id         bigserial PRIMARY KEY,
  job_id     uuid NOT NULL REFERENCES workload_jobs(id) ON DELETE CASCADE,
  ts         timestamptz NOT NULL DEFAULT now(),
  machine_id text,
  event      text NOT NULL,
  detail     jsonb
);
CREATE INDEX workload_job_events_job_idx ON workload_job_events (job_id, ts);

CREATE TABLE workload_secrets (
  workload   text NOT NULL REFERENCES workloads(name),
  name       text NOT NULL CHECK (name ~ '^[A-Z][A-Z0-9_]{0,63}$'),
  scope      text NOT NULL CHECK (scope IN ('container','host_only')),
  nonce      bytea NOT NULL,
  ciphertext bytea NOT NULL,
  updated_by text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workload, name)
);

CREATE TABLE outbound_actions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workload    text NOT NULL REFERENCES workloads(name),
  machine_id  text REFERENCES machines(id),
  job_id      uuid REFERENCES workload_jobs(id) ON DELETE SET NULL,
  kind        text NOT NULL,
  payload     jsonb NOT NULL,
  dedupe_key  text NOT NULL,
  status      text NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending','approved','rejected','sending','sent','failed','expired')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  decided_by  text,
  decided_at  timestamptz,
  sent_at     timestamptz,
  error       text,
  result      jsonb,
  UNIQUE (workload, dedupe_key)
);
CREATE INDEX outbound_actions_status_idx ON outbound_actions (status, created_at);

CREATE TABLE machine_logs (
  id         bigserial PRIMARY KEY,
  machine_id text NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
  workload   text,
  ts         timestamptz NOT NULL,
  stream     text NOT NULL CHECK (stream IN ('stdout','stderr','agent')),
  line       text NOT NULL CHECK (length(line) <= 2048)
);
CREATE INDEX machine_logs_machine_idx ON machine_logs (machine_id, id DESC);
