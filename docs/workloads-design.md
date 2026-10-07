# Fleet workloads: design and contract

Status: Phase 1 design, approved by the owner. Every Phase 2 builder builds against this
file. Field names, function signatures, route paths and refusal codes here are fixed;
change them only by editing this file deliberately.

## 0. Why, and the rules that do not move

polymarket-fleet becomes a general platform: any **workload** (a folder with a manifest,
a Docker image and its own code) can be assigned to any **machine**. Polymarket is the
first workload; a demo-site factory (Node + headless Chrome) and a market data archive
come later.

Owner decisions (fixed):
- Polymarket orders are exempt from the manual approval queue. They keep the existing
  automatic host approval, kill switch and live switch. The Polymarket worker container
  declares no outbound actions: it only proposes orders to the host.
- New workloads use a new shared `workload_jobs` table. Polymarket's `jobs` table and
  every code path that reads it are untouched.
- Images come from a registry on the host (`registry:2`) and are pulled by digest.
- Workload secrets are encrypted in Postgres under `FLEET_SECRETS_KEY` from a host-only
  `secrets.env`.

Hard rules for this change:
1. **`fleet/` stays byte-identical to main.** The worker `code_version` is a hash of
   `fleet/*.py` (`host/bundle.py`, `94aa556ca56a` on main). Any edit there makes every
   worker self-update, live-trading machines included. The new machine agent is a separate
   package, `fleetagent/`. A test pins the hash.
2. `host/trading/`, `host/exchange/`, `host/kill.py`, the existing migrations, the existing
   settings schema and the `exchange` compose service are not edited.
3. Existing files may only get the small, listed edits in section 4.4.
4. Style of the repo: Python 3.11+, type hints, modules under about 300 lines, no
   em-dashes anywhere, `fleetagent/` and everything under `workloads/` that runs on a
   machine is standard-library only.

## 1. Vocabulary

- **Workload**: `workloads/<name>/` with `workload.toml`, a `Dockerfile` and code.
- **Machine**: a physical box running **fleetagent**, the stdlib Python supervisor under
  systemd `fleet-agent.service`. Identity in the `machines` table.
- **Worker**: unchanged. A Polymarket `fleet.worker` identity in the `workers` table.
- **Assignment**: one workload per machine (`workload_assignments`, NULL = nothing).
- **Epoch**: `workload_assignments.epoch`, incremented on every assignment change. It
  fences the supervisor's ack, the run token, the secrets and job leases, the same way
  `role_epoch` does for workers.
- **Runtime mode** of Polymarket on a machine: `native` (the existing systemd
  `fleet-worker`, untouched) or `container` (the same agent inside the `polymarket`
  workload). The supervisor reports `native_polymarket` = `active|inactive|absent` from
  `systemctl is-active fleet-worker`.

## 2. Manifest `workloads/<name>/workload.toml`

TOML, read with stdlib `tomllib`. Folders whose name starts with `_` or `.` are skipped
(`_template`). Parser: `host/workloads/manifest.py` (already on the branch).

```toml
schema = 1
name = "hello"                 # ^[a-z][a-z0-9-]{1,31}$, equals the folder name
description = "Says hello"     # at most 300 chars
image = "fleet/hello"          # registry repository; no tag, no digest (digest recorded at publish)
protocol = "workload-v1"       # "workload-v1" | "fleet-worker" (polymarket only)

[resources]
min_ram_mb = 128               # refuse when machine ram_total_mb < this
min_disk_mb = 300              # refuse when free MB on Docker's disk < this + image size
write_heavy = false            # true: refused on flash and on unknown disks
memory_max_mb = 256            # docker --memory; or memory_max_pct = 85 (not both); neither = no cap
cpus = 1.0                     # optional docker --cpus

[runtime]
mode = "jobs"                  # "jobs" (claims from workload_jobs) | "service" (long running)
job_kinds = ["hello"]          # required when mode = jobs; ^[a-z][a-z0-9_]{0,31}$
network = "bridge"             # "bridge" | "host" (host only with protocol fleet-worker)
uts_host = false               # share the machine hostname (protocol fleet-worker only)
uid = 10001                    # container user; owns /state and the secret files
state_volume = false           # true: /var/lib/fleet-workloads/<name>/state mounted at /state
scratch_mb = 512               # /scratch size hint; wiped at every start and stop
stop_timeout_s = 15            # docker stop grace before SIGKILL
no_restart_exit_codes = [78]   # exit codes after which the supervisor does not restart
nice = 0                       # 0..19

[secrets]
container = ["HELLO_GREETING"] # files /run/fleet/secrets/<NAME> inside the container
host_only = []                 # held by the host for outbound senders; never delivered

[outbound]
actions = []                   # subset of ["email", "log"]; non-empty = every send waits for approval

[trading]
can_trade = false              # protocol fleet-worker only; enables the pin rule
```

Python API (fixed):
- `parse_manifest(data: dict, folder_name: str | None = None) -> Manifest`
- `load_manifest(path: Path) -> Manifest` (folder or file)
- `discover(root: Path) -> list[Path]` (workload folders under root)
- `ManifestError(problems: list[str])`, a `ValueError`; lists every problem.
- `Manifest` (frozen): `name, description, image, protocol, resources: Resources,
  runtime: Runtime, container_secrets, host_only_secrets, outbound_actions, can_trade,
  schema`; `needs_approval` property; `to_json()` / `Manifest.from_json(d)`.
- `Resources`: `min_ram_mb, min_disk_mb, write_heavy, memory_max_mb, memory_max_pct, cpus`.
- `Runtime`: `mode, job_kinds, network, uts_host, uid, state_volume, scratch_mb,
  stop_timeout_s, no_restart_exit_codes, nice`.

Extra rules: at most 32 secrets; a name may not be in both lists; `host_only` requires
`outbound.actions`; `can_trade` requires `protocol = "fleet-worker"`.

The Polymarket manifest (`workloads/polymarket/workload.toml`):
`protocol = "fleet-worker"`, `mode = "service"`, `network = "host"`, `uts_host = true`,
`state_volume = true`, `memory_max_pct = 85`, `nice = 5`, `stop_timeout_s = 15`,
`no_restart_exit_codes = [78]`, `min_ram_mb = 3000`, `min_disk_mb = 2048`,
`write_heavy = false`, `secrets.container = ["FLEET_ENROLL_TOKEN"]`, `outbound.actions = []`,
`can_trade = true`.

## 3. Database: `host/migrations/0010_workloads.sql` (already on the branch)

New tables only; read the SQL file for exact columns and checks. Summary:
- `workloads(name PK, manifest jsonb, image_repo, image_digest 'sha256:<64 hex>' NULL until
  published, image_size_mb, enabled, synced_at, updated_at)`
- `machines(id 'm_'+6 hex, name, token_hash, prev_token_hash, hostname, boot_id, remote_ip,
  agent_version, docker_version, docker_ok, arch, cpu_count, cpu_pct, ram_total_mb,
  ram_used_mb, disk_type_detected ssd|hdd|flash|unknown, disk_type_override (same or NULL),
  disk_size_mb, disk_free_mb, docker_root, low_disk, native_polymarket active|inactive|absent,
  polymarket_worker_id -> workers, pinned, pinned_reason, pinned_at, enabled, registered_at,
  last_heartbeat_at)`
- `machine_enroll_tokens(token_hash PK, created_at, expires_at, used_by_machine_id, used_at)`
- `workload_assignments(machine_id PK, workload NULL, epoch, acked_epoch, state
  pending|starting|running|draining|stopped|failed, draining_to, draining_to_set,
  run_token_hash UNIQUE, assigned_by, assigned_at, container_id, image_digest_running,
  started_at, last_exit_code, restarts, last_error, cpu_pct, mem_mb, updated_at)`.
  One row per machine, created at enroll with `workload NULL, epoch 1, state stopped`.
- `workload_jobs` (same lifecycle as `jobs`; `target_machine_id`, `lease_machine_id`,
  `lease_epoch`, `lease_token`, `lease_expires_at`, `expiries`, `max_expiries` 3,
  `idempotency_key` UNIQUE) and `workload_job_events(job_id, ts, machine_id, event, detail)`.
- `workload_secrets((workload, name) PK, scope container|host_only, nonce, ciphertext,
  updated_by, updated_at)`
- `outbound_actions(id uuid, workload, machine_id, job_id, kind, payload jsonb, dedupe_key,
  status pending|approved|rejected|sending|sent|failed|expired, created_at, decided_by,
  decided_at, sent_at, error, result; UNIQUE (workload, dedupe_key))`
- `machine_logs(id, machine_id, workload, ts, stream stdout|stderr|agent, line <= 2048)`

Owner actions write `audit_log` rows through `host.events.add_audit`. Timing reuses the
existing settings `heartbeat_seconds`, `lease_seconds` and `online_after_seconds`
(read-only, `host.settings.get_int_setting`). No new settings keys.

## 4. Host code (`host/workloads/`)

Errors: `host/workloads/errors.py` (on the branch): `Unplaceable(refusals)` 422,
`TooMany` 429, `SecretsUnavailable` 409; plus `host.errors` `BadRequest` 400,
`Unauthorized` 401, `Forbidden` 403, `NotFound` 404, `Conflict` 409. All subclass
`QueueError`, which the app already maps to `{"detail": message}` (HTML on dashboard paths).

### 4.1 Pure logic

`placement.py`:
```python
@dataclass(frozen=True)
class Refusal:
    code: str
    message: str

DISK_TYPES = ("ssd", "hdd", "flash", "unknown")

def effective_disk_type(machine: dict) -> str          # override if set, else detected, else "unknown"
def check_placement(manifest: Manifest, machine: dict, *, image_size_mb: int | None = None,
                    image_published: bool = True, workload_enabled: bool = True) -> list[Refusal]
```
`machine` is a `machines` row dict. Codes, in this order, all that apply:
- `workload_disabled`: not `workload_enabled`.
- `image_not_published`: not `image_published`.
- `docker_missing`: `docker_ok` is false.
- `ram_too_small`: `ram_total_mb` is NULL or `< min_ram_mb`.
- `disk_too_small`: `disk_free_mb` is NULL or `< min_disk_mb + (image_size_mb or 0)`.
- `write_heavy_on_flash`: `write_heavy` and the effective disk type is `flash` or `unknown`.
Messages are short and concrete: "needs 8192 MB RAM, machine has 3800 MB".

### 4.2 Database operations (each takes a psycopg connection inside the caller's transaction)

`machines.py`:
```python
def create_enroll_token(conn, ttl_seconds: int = 3600) -> dict      # {"token", "expires_at"}
def register(conn, body: dict, peer_ip: str | None) -> dict          # register response (section 5.1)
def verify_machine(conn, machine_id: str, token: str) -> dict        # 401 unless current token
def heartbeat(conn, machine: dict, body: dict, peer_ip: str | None) -> dict  # heartbeat response
def link_polymarket_worker(conn, machine: dict) -> str | None        # by boot_id, unique match only
```
Register rotates the token and keeps `prev_token_hash` exactly like `host/auth.py` does
for workers (register accepts current or previous; the first heartbeat with the current
token clears the previous). `name`/`hostname` match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.
Enroll creates the `workload_assignments` row.

`assign.py`:
```python
def assign(conn, machine_id: str, workload: str | None, actor: str | None, ip: str | None) -> dict
def desired_run(conn, machine: dict, assignment: dict) -> dict | None   # the "run" block
def finish_drains(conn) -> int                                         # loop task
```
Checks in order: unknown machine 404; same workload as now: no-op, returns the row;
`pinned` 409 ("pinned: <reason>; unpin first"); `native_polymarket == 'active'` 409
("native fleet-worker is running on this machine; stop it first"); target workload
unknown 404; `check_placement` refusals 422 (`Unplaceable`). Then:
- Leaving `polymarket` while its container runs and a worker is linked: call the existing
  `host.scheduling.set_role(conn, worker_id, "idle", actor)` (which runs the existing trade
  release handshake on the worker), set `state='draining'`, `draining_to=<target>`,
  `draining_to_set=true`, and do **not** bump the epoch yet. `finish_drains` completes it
  once that worker has `reported_role='idle'`, `acked_epoch=role_epoch` and no
  `leased`/`cancel_requested` job, or its `last_heartbeat_at` is older than
  `online_after_seconds`.
- Otherwise: `workload=<target>`, `epoch += 1`, `state='pending'` (or `stopped` for NULL),
  `run_token_hash=NULL`, release this machine's leased `workload_jobs` back to `queued`.
- Audit row `workload_assign` (entity = machine id, before/after = workload and epoch).

`pinning.py`:
```python
def is_live_trading(conn, machine_id: str) -> bool
def refresh_pins(conn) -> list[str]                    # machine ids newly pinned (loop task)
def pin(conn, machine_id: str, reason: str, actor: str | None, ip: str | None) -> dict
def unpin(conn, machine_id: str, confirm: str, actor: str | None, ip: str | None) -> dict
```
`is_live_trading` is true when the machine's `polymarket_worker_id` holds a `jobs` row with
`kind='trade'`, status `leased`/`cancel_requested`, whose `assignments` row (by
`params->>'assignment_id'`) has `mode='live'` and status `active`/`halted`; or has an
`orders` row with `mode='live'` and an open status (`approved`, `submitting`, `open`,
`partial`, `cancel_requested`). Read-only queries against Polymarket tables. Auto-pin sets
`pinned=true`, `pinned_reason='live trading'`, audit `machine_pin` (actor `system`). Pins
are sticky. `unpin` needs `confirm == f"UNPIN {machine['name']}"` exactly (400 otherwise),
is 409 while `is_live_trading`, and writes audit `machine_unpin` with
`confirmation_text`.

`secrets.py`:
```python
def secret_box() -> "nacl.secret.SecretBox | None"   # from env FLEET_SECRETS_KEY (base64 32 bytes)
def set_secret(conn, workload: str, name: str, value: str, actor, ip) -> dict   # names only back
def delete_secret(conn, workload: str, name: str, actor, ip) -> None
def list_secret_names(conn, workload: str) -> list[dict]   # [{name, scope, declared, set, updated_at}]
def secrets_for_machine(conn, machine_id: str, epoch: int) -> dict[str, str]
def host_only_secrets(conn, workload: str) -> dict[str, str]   # only for host senders
def secrets_version(conn, workload: str) -> str
```
`set_secret` refuses (400) a name the manifest does not declare; the scope comes from the
manifest. Value at most 16 KiB. No key configured: `SecretsUnavailable` (409) on writes and
on `secrets_for_machine` when the workload declares container secrets. Audit rows
`secret_set` / `secret_delete` never contain the value. `secrets_for_machine` returns the
container-scoped secrets of the workload assigned to that machine **at that epoch**
(409 on a stale epoch); never host-only ones.

`outbound.py`:
```python
MAX_PENDING = 500
def queue_action(conn, *, workload: str, machine_id: str | None, job_id, kind: str,
                 payload: dict, dedupe_key: str) -> dict      # idempotent on (workload, dedupe_key)
def approve(conn, action_id, actor, ip) -> dict              # pending -> approved
def reject(conn, action_id, actor, ip, reason: str = "") -> dict   # pending -> rejected
def expire_old(conn, days: int = 7) -> int                   # pending -> expired
def send_approved(conn, senders: dict[str, "Sender"]) -> int  # approved -> sending -> sent|failed
class Sender(Protocol):
    def send(self, action: dict, host_secrets: dict[str, str]) -> dict: ...
```
`queue_action` refuses (403) a kind the manifest does not declare, (400) a payload above
64 KiB, (429) when the workload has `MAX_PENDING` pending. Senders: `log` (appends a
`workload_job_events` row `outbound_sent` when there is a job, returns `{"logged": true}`)
and `email` (stdlib `smtplib`; payload `{to, subject, body}`; credentials from the
workload's host-only secrets `SMTP_URL` such as `smtps://user:pass@host:465` and
`EMAIL_FROM`). Nothing is ever sent unless the row is `approved`.

`queue.py`:
```python
def create_job(conn, *, workload: str, kind: str, params: dict, target: str | None = None,
               idempotency_key: str | None = None, max_expiries: int | None = 3) -> dict
def claim(conn, *, machine_id: str, workload: str, epoch: int, kinds: list[str] | None) -> dict | None
def renew(conn, *, job_id, lease_token: str, machine_id: str, epoch: int,
          progress: float, checkpoint: dict | None) -> dict   # {"status", "cancel"}
def release(conn, *, job_id, lease_token, machine_id, epoch, checkpoint, progress, reason) -> dict
def complete(conn, *, job_id, lease_token, machine_id, epoch, result: dict) -> dict
def fail(conn, *, job_id, lease_token, machine_id, epoch, error: str) -> dict
def cancel(conn, job_id, actor) -> dict
def reap(conn) -> int
def release_machine_jobs(conn, machine_id: str) -> int
```
Same semantics as `docs/PROTOCOL.md` for `jobs`: `claim` takes one queued job of that
workload (kinds filter), targeted at this machine first, then oldest, `FOR UPDATE SKIP
LOCKED`; lease `lease_seconds`; every write fenced by `(lease_token, lease_machine_id,
lease_epoch)` else 409; `complete` idempotent; reaper requeues with `expiries += 1`,
fails at `max_expiries`; `cancel_requested` becomes `cancelled` on release. `kind` must be
in the manifest's `job_kinds` (400). Events in `workload_job_events`.

`tokens.py`: `mint_run_token(conn, machine_id, epoch) -> str` (stores the sha256 in
`workload_assignments.run_token_hash`, replacing the old one) and
`run_scope(conn, token) -> dict` returning `{machine_id, workload, epoch}` or 401 when
the hash is unknown, the machine is disabled, or the assignment epoch moved on.

`registry.py`: `sync_from_dir(conn, root: Path) -> dict` (upsert every valid manifest,
`{"synced": [...], "errors": {name: problems}}`; never deletes), `set_image(conn, name,
digest: str, size_mb: int | None)`, `image_ref(workload_row) -> str | None` =
`f"{FLEET_REGISTRY}/{image_repo}@{image_digest}"`.

`loop.py`: `run_once(pool) -> None`: `queue.reap`, `pinning.refresh_pins`,
`machines` link refresh, `assign.finish_drains`, `outbound.expire_old`, log pruning
(newest 5000 lines per machine, nothing older than 3 days); each step in its own
transaction, each wrapped so one failure is logged and the rest still run.
`send_once(pool)` sends at most 5 approved outbound actions per pass. Both run in their
own threads (`start_threads`, started by `host/main.py` with their own small pool), never
inside the Polymarket loop: `host/loop.py` is unchanged, so a hanging SMTP server or a slow
workloads query cannot delay the reaper, the dispatcher or the orphan cancel.

`agent_bundle.py`: builds the `fleetagent/` tarball like `host/bundle.py` (reuse its
`source_files`, `compute_code_version`, `build_tarball` pattern; top-level dir
`fleetagent/`).

`cli.py`: `main(argv) -> int` for the subcommands in section 9; `host/cli.py` dispatches
these names to it.

### 4.3 API modules

- `api_machine.py`: `router` for section 5.1 (machine bearer).
- `api_run.py`: `router` for section 5.2 (run-token bearer).
- `api_owner.py`: `router` for section 5.3 (`dependencies=[Depends(require_owner)]`).
- `api_dl.py`: `router` for the agent downloads (no auth).
- `dashboard.py`: `router` for section 10 pages and forms; `views.py` its view models.

### 4.4 Edits to existing files (the only ones allowed)

- `host/api/app.py`: include the five routers above.
- `host/main.py`: start the workloads threads (`host.workloads.loop.start_threads`) with
  their own pool, next to the existing loop thread. `host/loop.py` is not edited.
- `host/auth.py` `owner_from_worker_ip`: also refuse a peer IP that is any
  `machines.remote_ip`.
- `host/cli.py`: dispatch the new subcommand names to `host.workloads.cli.main`.
- `host/templates/fleet.html`: one `{% include "_fleet_subnav.html" %}`.
- `host/static/style.css`: rules appended at the end only.
- `docker-compose.yml`: add `registry` (and volume `fleet-registry`); add an optional
  `secrets.env` (`required: false`) to the `host` service only. `db` and `exchange` are not
  changed.
- `Dockerfile`: `COPY fleetagent/ ./fleetagent/` and `COPY workloads/ ./workloads/`.
- `pyproject.toml`: packages `fleetagent*`; package data for nothing new.
- `.env.example`: `FLEET_REGISTRY`, `FLEET_WORKLOADS_DIR` (default `/app/workloads`).
- `.github/workflows/ci.yml`: the stdlib-only check also covers `fleetagent/`.
- `README.md`: a new "Workloads" section.

## 5. Host API

All bodies JSON, errors `{"detail": "..."}`. Body limit 1 MiB (the existing middleware).

### 5.1 Supervisor routes (`Authorization: Bearer <machine_token>`, bound to `{id}`)

`POST /api/v1/machines/register`
```json
{"enroll_token": "...", "name": "box1", "hostname": "box1", "boot_id": "...",
 "agent_version": "a1b2c3d4e5f6", "specs": {...}}
{"machine_id": "m_3f9a1c", "machine_token": "...", "hostname": "box1", "boot_id": "...",
 "agent_version": "...", "specs": {...}}
```
Response: `{"machine_id", "machine_token": "<NEW>", "heartbeat_seconds", "server_time",
"agent_version": "<host's current fleetagent version>"}`.

`POST /api/v1/machines/{id}/heartbeat`
```json
{"specs": {"cpu_pct": 4.0, "ram_used_mb": 900, "ram_total_mb": 3800, "cpu_count": 4,
           "arch": "x86_64", "disk_type": "flash", "disk_size_mb": 15000, "disk_free_mb": 9000,
           "docker_root": "/var/lib/docker", "docker_ok": true, "docker_version": "26.1.5"},
 "native_polymarket": "absent",
 "acked_epoch": 4,
 "container": {"workload": "hello", "epoch": 4, "state": "running", "container_id": "...",
               "image_digest": "sha256:...", "exit_code": null, "restarts": 0,
               "cpu_pct": 1.5, "mem_mb": 40, "started_at": "...", "error": null},
 "logs": [{"ts": "2026-10-06T20:00:00Z", "stream": "stdout", "line": "..."}],
 "cleanup": {"images_removed": 1, "bytes_freed": 123456, "low_disk": false}}
```
`container` is null when nothing runs. `logs` at most 200 entries and 64 KiB total; a line
longer than 2048 chars is cut. `container.state`: `starting|running|exited|failed|stopped`.

Response:
```json
{"epoch": 5, "workload": "hello",
 "run": {"image": "host.tailnet.ts.net:5000/fleet/hello@sha256:...",
         "protocol": "workload-v1", "mode": "jobs", "network": "bridge", "uts_host": false,
         "uid": 10001, "memory_mb": 256, "cpus": 1.0, "nice": 0, "stop_timeout_s": 15,
         "state_volume": false, "scratch_mb": 512, "no_restart_exit_codes": [78],
         "env": {"FLEET_HOST_URL": "...", "FLEET_WORKLOAD": "hello",
                 "FLEET_MACHINE_ID": "m_3f9a1c", "FLEET_EPOCH": "5", "FLEET_NICE": "0"}},
 "secrets_version": "9f2c...", "keep_images": ["sha256:...", "sha256:..."],
 "agent_version": "...", "server_time": "...", "heartbeat_seconds": 5}
```
`run` is null when `workload` is null, while the machine is disabled, or when the image is
not published. While a machine drains away from polymarket, `run` keeps describing the
running container (same workload and epoch) so it stays up until the worker finished the
trade release; `finish_drains` then moves the epoch. Disabling a machine is refused (409)
while it is pinned or runs polymarket. `memory_mb` is computed from `memory_max_mb` or
`memory_max_pct * ram_total_mb / 100` (null = no cap). `keep_images` = digests of the
current workload's image and the one that ran before it on this machine.
Host-side processing in one transaction: update specs and `last_heartbeat_at`,
`disk_type_detected`, `low_disk`, `native_polymarket`; store the container block on the
assignment row; set `acked_epoch = GREATEST(acked_epoch, body.acked_epoch)`; derive
`state` (`running` when the container reports running at the current epoch); insert logs.

`POST /api/v1/machines/{id}/start` `{"epoch": 5}` returns
`{"run_token": "...", "secrets": {"HELLO_GREETING": "..."}}`, `Cache-Control: no-store`.
Mints a new run token (the old one stops working). 409 when the epoch is not current or
nothing is assigned. Never logged.

### 5.2 Container routes (`Authorization: Bearer <run_token>`)

The scope is the token's `(machine_id, workload, epoch)`. A run token is 401 on every
other route (it is not a worker token or a machine token).
- `POST /api/v1/wl/claim` `{"kinds": ["hello"]}` returns `{"job": {id, kind, params,
  checkpoint, progress, lease_token, lease_seconds} | null}`.
- `POST /api/v1/wl/jobs/{id}/heartbeat` `{lease_token, progress, checkpoint?}` returns
  `{"status", "cancel": bool}`.
- `POST /api/v1/wl/jobs/{id}/release` `{lease_token, checkpoint, progress, reason}`.
- `POST /api/v1/wl/jobs/{id}/complete` `{lease_token, result}`.
- `POST /api/v1/wl/jobs/{id}/fail` `{lease_token, error}` (error at most 16 KiB).
- `POST /api/v1/wl/outbound` `{kind, payload, dedupe_key, job_id?}` returns
  `{"id", "status": "pending"}` (or the existing row for a repeated dedupe key).
- `GET /api/v1/wl/outbound/{id}` returns `{"id", "status", "error", "result"}` for this
  workload's actions only (404 otherwise).

Polymarket's container never uses these routes; it speaks its existing worker protocol
with its own worker token.

### 5.3 Owner routes (`require_owner`, the Origin check; every write audited)

- `GET /api/workloads`, `GET /api/workloads/{name}`, `POST /api/workloads/{name}/enabled`
  `{"enabled": bool}`, `POST /api/workloads/sync`.
- `GET /api/machines` (each machine with its assignment, effective disk type, online flag,
  and per workload the placement refusals), `POST /api/machines/{id}/assign`
  `{"workload": "hello" | null}`, `POST /api/machines/{id}/pin` `{"reason"}`,
  `POST /api/machines/{id}/unpin` `{"confirm"}`, `POST /api/machines/{id}/disk-type`
  `{"disk_type": "flash" | null}`, `POST /api/machines/{id}/enabled` `{"enabled"}`,
  `POST /api/machine-enroll-token`.
- `GET /api/workloads/{name}/secrets` (names, scope, set or not, updated_at; never
  values), `PUT /api/workloads/{name}/secrets/{secret}` `{"value"}`,
  `DELETE /api/workloads/{name}/secrets/{secret}`.
- `GET /api/outbound?status=pending`, `POST /api/outbound/{id}/approve`,
  `POST /api/outbound/{id}/reject` `{"reason"}`.
- `POST /api/workload-jobs` `{workload, kind, params, target?, idempotency_key?}` (201),
  `GET /api/workload-jobs?workload=&status=&limit=`, `GET /api/workload-jobs/{id}`,
  `POST /api/workload-jobs/{id}/cancel`.
- `GET /api/machines/{id}/logs?limit=200`.

### 5.4 Downloads (no auth, tailnet only, like `/dl`)

`GET /dl/agent/version` `{"agent_version", "sha256"}`, `GET /dl/agent.tar.gz`,
`GET /install-agent.sh` (`deploy/install_agent.sh` with `__FLEET_HOST_URL__` replaced).

## 6. Supervisor (`fleetagent/`, stdlib only, Debian 13 `/usr/bin/python3`)

Layout: `__init__.py`, `__main__.py` (`enroll`, `run`, `status`, `specs`), `config.py`,
`http.py`, `specs.py`, `docker.py`, `supervisor.py`, `cleanup.py`, `secretfiles.py`,
`logship.py`, `update.py`.

- State: `/var/lib/fleet-agent/agent.conf` (`{host_url, machine_id, machine_token}`, 0600),
  `/var/lib/fleet-agent/app/<version>/fleetagent`, symlink `app/current`;
  workload data `/var/lib/fleet-workloads/<name>/{state,scratch}`; secrets
  `/run/fleet-agent/secrets/<name>/<NAME>` (tmpfs). Env overrides for tests:
  `FLEET_AGENT_STATE_DIR`, `FLEET_AGENT_DATA_DIR`, `FLEET_AGENT_RUN_DIR`, `FLEET_AGENT_DOCKER`
  (docker binary), `FLEET_AGENT_SYSTEMCTL`.
- Unit `deploy/fleet-agent.service`: `User=fleet-agent`, `SupplementaryGroups=docker`
  (the docker group is root-equivalent; the docs say so), `StateDirectory=fleet-agent
  fleet-workloads`, `RuntimeDirectory=fleet-agent`, `RuntimeDirectoryPreserve=yes`,
  `Restart=always`, `RestartSec=3`, `RestartPreventExitStatus=78`,
  `KillMode=process` (stopping the agent never stops the containers).
- Installer `deploy/install_agent.sh HOST_URL [TOKEN] [--name NAME] [--token-file PATH]`:
  installs `docker.io` with apt when `docker` is missing; writes `/etc/docker/daemon.json`
  with `{"log-driver": "local", "log-opts": {"max-size": "10m", "max-file": "3"}}` only when
  the file is absent; creates `fleet-agent`; downloads and verifies `/dl/agent.tar.gz`;
  enrolls; enables the unit. It never touches `fleet-worker.service` or `/var/lib/fleet`.
- `specs.py`: RAM and CPUs from `/proc`; the disk is the block device that holds
  `docker info --format '{{.DockerRootDir}}'` (`os.stat().st_dev` -> `/sys/dev/block/M:m`
  -> the parent disk); type `flash` when `removable=1`, the name starts with `mmcblk`, or
  the device path goes through a USB bus; else `hdd` when `queue/rotational=1`, `ssd` when
  `0`, else `unknown`. Size from sysfs `size * 512`, free from `statvfs(docker_root)`.
  `native_polymarket` from `systemctl is-active fleet-worker` / `is-enabled`.
- `docker.py`: `Docker(runner=subprocess.run, binary="docker")` with `info`, `pull(ref)`,
  `run(args) -> id`, `stop(id, timeout)`, `rm(id)`, `inspect(id)`, `ps(labels)`,
  `stats(id)`, `logs(id, since)`, `images(label or repo)`, `rmi(ref)`, `image_prune()`,
  `builder_prune()`, `container_prune(label)`. Tests inject a fake runner.
- `supervisor.py` reconcile loop, every `heartbeat_seconds` (fixed monotonic schedule):
  1. Heartbeat with specs, container status, logs, cleanup report.
  2. If `native_polymarket == "active"`: never start or stop anything; report only.
  3. Desired `(epoch, workload, run)` vs running container (found by labels
     `fleet.workload`, `fleet.epoch`):
     - running something not desired (other workload or older epoch): `docker stop -t
       <stop_timeout_s>`, `rm`, wipe its scratch and secret files.
     - desired and not running: low-disk check, `docker pull <image@digest>`,
       `POST /start` for the run token and secrets, write secrets (mode 0400, owner
       `run.uid`), wipe and create scratch, then `docker run -d` with
       `--name fleet-<workload>-<epoch>`, `--init` (an init process reaps orphaned children, as systemd does for the native worker), `--label fleet.workload`, `--label fleet.epoch`,
       `--read-only`, `--tmpfs /tmp:rw,nosuid,size=256m`, `--security-opt
       no-new-privileges`, `--user <uid>:<uid>`, `--memory <memory_mb>m --memory-swap -1`
       (when capped), `--cpus`, `--stop-timeout`, `--network <network>`, `--uts host` when
       set, `--log-driver local --log-opt max-size=10m --log-opt max-file=3`, mounts
       `/scratch`, `/state` (when `state_volume`), `/run/fleet/secrets` (read-only), env
       from `run.env` plus `FLEET_RUN_TOKEN`; then acknowledge the epoch.
     - desired container exited: restart 3 s later unless the exit code is in
       `no_restart_exit_codes` (then report `failed`); this mirrors systemd
       `Restart=always`, `RestartSec=3`, `RestartPreventExitStatus=78`.
  4. A supervisor restart adopts running containers by label; supervisor self-update
     never stops a workload. Losing contact with the host never stops a container.
- `logship.py`: `docker logs --timestamps --since <last ts>` per running container, every
  known secret value replaced with `[redacted]` before shipping, at most 200 lines per
  heartbeat (the rest waits).
- `cleanup.py`: after every start and hourly: remove images of `fleet/*` repositories whose
  digest is not in `keep_images`; `docker image prune -f`; `docker builder prune -af`;
  remove exited containers labelled `fleet.workload`. Scratch is wiped on start and stop.
  Low-disk guard: free below `max(1024 MB, 10% of disk)` prunes first; if still low the
  pull is refused and the heartbeat reports `low_disk: true`.
- `update.py`: like the worker: host `agent_version` differs and no start in progress:
  download, verify sha256, extract, swap `app/current`, exit 75.

## 7. Polymarket workload (`workloads/polymarket/`)

- `Dockerfile`: `ARG BASE_IMAGE=debian:trixie-slim`, `FROM ${BASE_IMAGE}`, then
  `apt-get install --no-install-recommends python3 ca-certificates` only when the base has
  no `python3` (the production base never has it; a base that already ships Python, used
  only where apt cannot run, skips the step), user `fleet` uid 10001, `bootstrap.py` at
  `/opt/fleet/bootstrap.py`, `ENTRYPOINT ["python3", "/opt/fleet/bootstrap.py"]`; the
  bootstrap execs the agent with `sys.executable`. **No Polymarket code in the
  image**: same Debian Python build as a native worker, same code from the host's `/dl`.
- `bootstrap.py` (stdlib): `os.nice(nice)`; if `/state/app/current` is missing, download
  `/dl/version` and `/dl/worker.tar.gz` from `FLEET_HOST_URL`, verify the sha256 and the
  members with the installer's rules, install to `/state/app/<version>`, point `current`;
  if `/state/worker.conf` is missing, run `python3 -m fleet.worker enroll` with the token
  from `/run/fleet/secrets/FLEET_ENROLL_TOKEN` (exit 78 when there is none); then `execve`
  `sys.executable -m fleet.worker run` with `PYTHONPATH=/state/app/current` and
  `FLEET_STATE_DIR=/state`. Exit codes pass straight to the supervisor (75 restart, 78 stop).
- Native to container migration (by hand, non-trading machines first): set the worker
  idle and disable it on `/fleet`; `sudo systemctl disable --now fleet-worker`;
  `sudo mkdir -p /var/lib/fleet-workloads/polymarket/state && sudo mv /var/lib/fleet/*
  /var/lib/fleet-workloads/polymarket/state/ && sudo chown -R 10001:10001
  /var/lib/fleet-workloads/polymarket/state`; assign polymarket on `/machines`; re-enable
  the worker. Same identity, same code version.
- The host links a machine to its worker by `boot_id`: the container shares the kernel.
- Runtime equivalence with `deploy/fleet-worker.service`: `MemoryMax=85%` ->
  `memory_max_pct=85` with unlimited swap; `Nice=5` -> `nice=5`; `TimeoutStopSec=15` ->
  `stop_timeout_s=15`; `Restart=always`/`RestartSec=3`/`RestartPreventExitStatus=78` ->
  the supervisor rule; `User=fleet` -> uid 10001; `NoNewPrivileges` -> `no-new-privileges`;
  `ProtectSystem=strict` + `ReadWritePaths=/var/lib/fleet` -> `--read-only` + `/state`;
  `PrivateTmp` -> `--tmpfs /tmp`; host network and UTS -> same IP and hostname.
- Parity check: `tools/workloads/paper_parity.py` runs the same frozen-clock paper scenario
  with the agent native and in the container and compares normalized orders, fills,
  ledger rows and bets.

## 8. Guardrails

- **Pinning**: `refresh_pins` every loop pass; sticky; `unpin` typed and refused while live;
  `assign` 409 on a pinned machine and on `native_polymarket = active`.
- **Secret isolation**: one workload per machine; secrets served only by `/start` (machine
  token, current epoch, that machine's workload); run tokens scoped to one workload and
  refused everywhere else; values encrypted at rest, never returned by owner routes,
  never in audit rows, redacted from shipped logs, deleted from the machine when the
  container stops; Polymarket keys stay in `exchange.env`.
- **Outbound approval**: a workload with `outbound.actions` never gets sender credentials
  (they are host-only secrets); the host sends only `approved` rows. Honest limit: this is
  enforced by credential custody, not by a network firewall.
- **Placement**: section 4.1; refused workloads are disabled in the dropdown with the reason.
- **Cleanup**: section 6; images are built on the host only, never on machines.

## 9. Operations

- Compose `registry`: `image: registry:2`, `ports: ["127.0.0.1:5000:5000"]`, volume
  `fleet-registry:/var/lib/registry`, `REGISTRY_STORAGE_DELETE_ENABLED: "true"`,
  `restart: unless-stopped`. Tailnet: `tailscale serve --bg --https=5000
  http://127.0.0.1:5000`. `.env`: `FLEET_REGISTRY=<host>.<tailnet>.ts.net:5000`.
- `tools/workloads/publish.sh <name>`: `docker build -t localhost:5000/<image>:latest
  workloads/<name>`, `docker push`, read the digest and size, then `docker compose exec host
  python -m host.cli workload-image <name> <digest> --size-mb N`.
- `secrets.env` (host only): `FLEET_SECRETS_KEY=<base64 of 32 random bytes>`
  (`python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"`).
- CLI (`python -m host.cli ...`): `workloads-sync`, `workload-image NAME DIGEST
  [--size-mb N]`, `machine-enroll-token`, `machines`, `assign MACHINE WORKLOAD|none`,
  `pin MACHINE [--reason R]`, `unpin MACHINE` (prompts for the phrase), `secret-set
  WORKLOAD NAME` (value from stdin), `outbound [--approve ID | --reject ID]`,
  `wl-send-job WORKLOAD KIND [--params JSON] [--target MACHINE]`.

## 10. Dashboard

The main nav stays at five items (`tests/test_style.py` pins it). `/fleet` and the new
pages show a sub-nav `_fleet_subnav.html`: Workers (`/fleet`) | Machines (`/machines`) |
Workloads (`/workloads`) | Approvals (`/outbound`, with the pending count). Pages set
`{% block page %}fleet{% endblock %}`-style data so the Fleet nav item stays current.
Everything is built from `_ui.html` macros with `data-*` hooks, server-rendered, forms
POST then 303 with a flash, no em-dashes, works without JS, follows `docs/UI.md`.

- `/machines`: h1, intro, stats (machines online, running workloads, pinned, approvals
  pending). One card per machine (`data-row="machine"`, `data-id`): dot, name, chips for
  disk ("flash 16 GB"), RAM ("4 GB RAM"), container state; a workload `<select
  name="workload" data-autosubmit="1">` posting to `/machines/{id}/assign` with options
  `none` plus each workload, refused ones `disabled` with the reason in the label
  ("demo-site (needs 8 GB RAM)"); pinned: chip "pinned: live trading", select disabled,
  menu item Unpin -> `/machines/{id}/unpin` typed-confirm page. Menu: Details (CPU, free
  disk, Docker and agent versions, linked worker), Set disk type (select), Disable or
  Enable, Pin, Logs (`/machines/{id}/logs`).
- `/workloads`: one row per workload: name, image published or not, machines running it,
  queued jobs. `/workloads/{name}`: manifest summary (resources, mode, outbound), image
  digest, machines running it, jobs (Running and Done), a "send job" form for jobs-mode
  workloads, last 50 log lines per machine, secrets form (names, set or not; write-only
  value input; delete), pending outbound actions.
- `/outbound`: pending actions, one row each: workload, kind, summary ("email to x@y:
  subject"), Approve and Reject buttons; the full payload in a disclosure. Done list below.
- `/machines/{id}/logs`: the last 200 lines, monospace, newest last.

Form posts: `POST /machines/{id}/assign`, `/machines/{id}/pin`, `/machines/{id}/unpin`,
`/machines/{id}/disk-type`, `/machines/{id}/enabled`, `/workloads/{name}/secrets`
(`name`, `value`, or `delete=1`), `/workloads/{name}/jobs`, `/outbound/{id}/approve`,
`/outbound/{id}/reject`, `/workloads/sync`, `/machine-enroll-token` (renders the token
once, like the worker enroll page). Each calls the same function as the JSON route.

## 11. SDK, hello and the template

- `workloads/_template/app/fleet_client.py` (stdlib): `Client.from_env()` (reads
  `FLEET_HOST_URL`, `FLEET_RUN_TOKEN`, `FLEET_WORKLOAD`, `FLEET_MACHINE_ID`,
  `FLEET_EPOCH`), `secret(name) -> str | None` (reads `/run/fleet/secrets/<NAME>`),
  `claim(kinds) -> Job | None`, `Job.progress(p, checkpoint=None)` (also renews; a
  background thread renews every `lease_seconds / 3`), `Job.complete(result)`,
  `Job.fail(error)`, `Job.release(reason)`, `Job.scratch` (a `Path`
  `/scratch/<job_id>`, created on claim, deleted after complete, fail or release),
  `outbound(kind, payload, dedupe_key, job=None) -> dict`, `outbound_status(id)`,
  `run_forever(handlers: dict[str, Callable[[Job], dict]], idle_sleep=5)` which installs
  a SIGTERM handler that releases the current job with reason `shutdown` and exits 0.
- `workloads/hello/`: built from the template (`ARG BASE_IMAGE=python:3.13-slim`, stdlib only, no apt step). Kind `hello`, params `{"name": str?,
  "steps": int 1..60 (default 3), "notify": bool}`. Reads `HELLO_GREETING` (default
  "Hello"), writes and reads back a file in `job.scratch`, reports progress per step,
  returns `{"greeting": "<greeting>, <name>!", "machine": ..., "epoch": ..., "steps": n}`;
  with `notify`, queues a `log` outbound action with dedupe key `hello:<job id>`.
- A test checks every workload's `app/fleet_client.py` equals the template's copy.

## 12. Phase 2 ownership (one git worktree each; no commits; report files and test lines)

| Builder | Model | Owns |
|---|---|---|
| polymarket | opus | `workloads/polymarket/**`, `tools/workloads/paper_parity.py`, `tests/test_wl_polymarket.py` |
| host | sonnet | `host/workloads/**` except `dashboard.py`, `views.py`, `manifest.py`, `errors.py`; the section 4.4 edits except `fleet.html`, `style.css`, `ci.yml`, `README.md`; `tests/test_wl_api_*.py` |
| agent | sonnet | `fleetagent/**`, `deploy/install_agent.sh`, `deploy/fleet-agent.service`, `.github/workflows/ci.yml`, `tests/test_fleetagent_*.py` |
| dashboard | sonnet | `host/workloads/dashboard.py`, `host/workloads/views.py`, `host/templates/{machines,_machines,workloads,workload,outbound,unpin_confirm,machine_logs,machine_enroll_token,_fleet_subnav}.html`, `fleet.html` include, appended `style.css`, `tests/test_wl_pages.py` |
| guardrails | sonnet | `tests/test_wl_placement.py`, `tests/test_wl_pinning.py`, `tests/test_wl_secret_isolation.py`, `tests/test_wl_outbound.py`, `tests/test_wl_queue.py`, `tests/wl_helpers.py` |
| hello | sonnet | `workloads/hello/**`, `workloads/_template/**`, `workloads/README.md`, `README.md`, `tools/workloads/publish.sh`, `tools/workloads/local_demo.sh`, `tests/test_wl_manifests.py` |

Docker in the build sandbox: a daemon runs and has `debian:trixie-slim`, `python:3.13-slim`
and `registry:2` locally (Docker Hub is rate limited; never pull other images). Containers
have no internet: never route container traffic through the session proxy and never run
`apt-get` or `pip` inside a build; use stdlib code and those bases. Name test images and
containers with your builder name as a prefix and remove them when done.

Shared code already on the branch (use it, do not rewrite it): `host/migrations/0010_workloads.sql`,
`host/workloads/__init__.py`, `host/workloads/manifest.py`, `host/workloads/errors.py`.
`tests/conftest.py` and existing tests are not edited. Tests: `FLEET_TEST_DATABASE_URL`
Postgres, run with `PYTHONPATH=$PWD /home/user/.venv-fleet/bin/python -m pytest <files>`.

## 13. Changes after review (Phase 3)

The integration review found these and the code now does them; they override anything
above that says otherwise.
- **Drains.** While a machine drains away from polymarket the `run` block keeps describing
  the running container, so it stays up until the worker finished the trade release. A
  drain finishes only when the machine is not pinned, nothing on it trades live, no
  worker on it holds a leased job, and the linked worker acked idle (or went silent with
  nothing leased: its trade jobs leave through the reaper and its orders through the
  orphan rule first). Leaving polymarket always drains when a worker is linked; when no
  worker can be identified while the container runs, the assign is 409.
- **Live check.** `assign` calls `is_live_trading` directly (409) instead of waiting for
  the loop to pin. `is_live_trading` looks at the linked worker and at every worker that
  shares the machine's boot_id. The link prefers the single online worker when several
  share a boot_id.
- **Machine disable** is 409 while the machine is pinned or runs polymarket.
- **Snapshots and updates (replaces the sync freeze).** An assignment snapshots the workload's
  manifest and image digest (`workload_assignments.run_manifest`, `run_image_digest`); the
  heartbeat `run` block and `keep_images` come from the snapshot. A sync or publish only
  changes the workloads row and never touches a running container, so it is never refused.
  A machine moves to the new manifest and image when the owner applies the update:
  `POST /api/machines/{id}/update` (dashboard "Apply update", CLI `machine-update`), or
  `POST /api/workloads/{name}/rollout` (dashboard "Roll out the update", CLI
  `workload-rollout`), which updates every machine that can take it and reports the rest.
  Applying goes through every refusal of an assign (pinned or live 409, native 409,
  placement 422) and, for polymarket, through the drain: the worker is set idle (its trades
  released) before the container is replaced, and its role has to be set again on /fleet
  afterwards. `GET /api/machines` carries `update_available`.
- **Secrets at start.** With nothing stored, `/start` needs no key. For protocol
  `fleet-worker` (polymarket) a missing or wrong key never blocks a restart: undecryptable
  secrets are skipped with a log line (the enroll token only matters for a first start).
- **Scrubbing.** The host scrubs a workload's stored secret values from shipped log lines and from
  job results, errors and checkpoints before storing them. Outbound payloads are not scrubbed:
  the owner must see exactly what would be sent before approving it.
- **Assign notes.** An assign or update of a machine that is offline (or never checked in)
  succeeds and carries `note` (shown in the flash and by the CLI): the change waits for it.
- **Dropped logs.** The heartbeat reply carries `logs_dropped` (lines over the per-beat limits),
  and the host logs a warning.
- **Disk type on VMs.** A virtual disk often reports `rotational=1` and shows as `hdd`; that is
  the kernel's flag and is kept as is. The owner override corrects a misdetected disk.
- **Email.** The sender refuses to log in when STARTTLS is not offered.
- **Agent.** Workload containers run with `--init`; `deactivating` counts as an active
  native worker; a removed container's image is freed on the next heartbeat; an
  unassigned machine keeps no images.
- **Enroll tokens** for machines never start with `-`.
