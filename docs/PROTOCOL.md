# Host <-> worker protocol (step 1)

This file is the contract between `host/` (FastAPI + Postgres) and `fleet/` (the worker
agent, stdlib only). Both sides are built against it. Change it deliberately.

## Vocabulary

- **Role**: what a worker is allowed to run. `idle | backtest | model_search | train | trade`.
- **Job**: a row in `jobs`. `kind` is what to run (`sleep` in step 1; later `backtest`,
  `model_search`, `train`, `trade`). `role` is the role a worker must be in to claim it.
  Default mapping: kind `sleep` -> role `backtest`; every other kind maps to the role of
  the same name.
- **Lease**: a job is `leased` to one worker for `lease_seconds` (settings, default 30).
  The heartbeat renews it. A lease is fenced by `lease_token` (uuid): every write about
  a job must carry the token the host handed out, otherwise 409.
- **Checkpoint**: an arbitrary JSON object the runner emits; stored in `jobs.checkpoint`
  and handed back when the job is resumed anywhere.
- **Epoch**: `workers.role_epoch` increments on every desired-role change. The worker
  echoes `acked_epoch`; the host only lets it claim jobs when `acked_epoch == role_epoch`
  and `reported_role == desired_role`.
- **Online**: `last_heartbeat_at > now() - online_after_seconds` (settings, default 15).

## Authentication

- Worker routes (`/api/v1/...`): `Authorization: Bearer <worker_token>`. Tokens are
  random 32-byte urlsafe strings; the host stores only `sha256` hashes. A token is bound
  to the `{worker_id}` in the path; mismatch -> 401. Rotated on every register.
  (changed: a lost register reply must not brick a worker) The host also keeps
  `workers.prev_token_hash`, the hash of the token presented at the last rotation.
  `POST /register` accepts the current **or** the previous token, so an agent that never
  saw the reply can retry with the token it still has. Heartbeats and job routes accept
  only the current token; the first heartbeat authenticated with the current token clears
  `prev_token_hash`, which locks a zombie copy of the agent out from then on.
- Enrollment: `POST /api/v1/workers/register` with an `enroll_token` (minted by the owner,
  single use, default 1 hour expiry). Hashes only in `enroll_tokens`.
- Owner routes (`/api/...` without `/v1/`, and the dashboard pages): the request must carry
  `Tailscale-User-Login: <login>` equal to env `FLEET_OWNER_LOGIN`. That header is injected
  by `tailscale serve`, which is the only thing allowed to reach the host port (the app
  binds `127.0.0.1`). With env `FLEET_DEV=1` the header is not required (local testing).
  (changed: a compromised job on a worker must not reach owner routes) The request's peer
  IP is the last `X-Forwarded-For` hop (appended by `tailscale serve`) when env
  `FLEET_TRUST_PROXY` is set (default `1`, since the host sits behind `tailscale serve`;
  set `0` when clients hit the port directly, then the socket peer is used and the header
  is ignored). If that peer IP is any registered worker's `remote_ip` the owner request is
  refused with 403, unless env `FLEET_OWNER_ALLOW_WORKER_IPS=1`. Not applied in dev mode.
  The host machine must not run a worker that shares the owner's tailnet identity. A
  non-ASCII login header is a plain 401.
  CSRF: if an `Origin` header is present on a state-changing owner request it must equal one
  of `FLEET_ALLOWED_ORIGINS` (comma separated; default = `FLEET_PUBLIC_URL`), else 403.
  Requests without `Origin` (curl, CLI) pass.
- `GET /healthz`, `GET /install.sh`, `GET /dl/*`: no auth (tailnet only by deployment).

All bodies are JSON. Timestamps are ISO-8601 UTC strings. Errors: `{"detail": "..."}`
with 400/401/403/404/409.

## Worker routes

### `POST /api/v1/workers/register`

First registration (installer) or re-registration (every agent start).

Request, first time:
```json
{"enroll_token": "...", "hostname": "box1", "name": "box1",
 "python_version": "3.13.1", "code_version": "a1b2c3d4e5f6", "boot_id": "..."}
```
Request, re-register (agent start, after crash/reboot/self-update):
```json
{"worker_id": "w_3f9a1c", "worker_token": "<current token>", "hostname": "box1",
 "python_version": "3.13.1", "code_version": "a1b2c3d4e5f6", "boot_id": "..."}
```
Response 200:
```json
{"worker_id": "w_3f9a1c", "worker_token": "<NEW token>",
 "desired_role": "idle", "role_epoch": 4, "kill": false,
 "held_jobs": [{"id": "...", "kind": "sleep", "params": {...}, "checkpoint": {...},
                "progress": 0.4, "lease_token": "<new uuid>", "lease_seconds": 30}],
 "code_version": "a1b2c3d4e5f6", "server_time": "2026-10-02T20:00:00Z",
 "heartbeat_seconds": 5}
```
Semantics:
- `enroll_token` path: token must exist, be unused and unexpired -> create worker
  (`id` = `w_` + 6 hex, `name` = given name or hostname), mark token used. 401 otherwise.
- `worker_token` path: must match the worker's current token hash **or** its previous
  one (changed: retry after a lost reply, see Authentication) -> rotate token, remember
  the presented hash as `prev_token_hash`. 401 otherwise. A zombie copy of the agent is
  locked out once the live agent has heartbeated with the new token.
- `held_jobs`: jobs currently `leased`/`cancel_requested` by this worker whose lease has
  not expired. Each gets a fresh `lease_token` and a renewed lease. The worker resumes
  them from `checkpoint` (job payloads carry the stored `progress` too, which the agent
  reports until the first new unit of work). Jobs whose lease already expired were requeued by the reaper and
  are not returned.
- Records `hostname`, `python_version`, `code_version`, `boot_id`, `remote_ip` (the peer
  IP as defined under Authentication). (changed: validation) `hostname` and `name` must
  match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; `python_version`, `code_version` and
  `boot_id` the same plus `+`; otherwise 400.
- `job_events`: `re-leased` for each held job (changed: dedupe) unless the job's newest
  event is already `re-leased` by this worker, so a register retry loop writes one row.

### `POST /api/v1/workers/{worker_id}/heartbeat`

Sent every `heartbeat_seconds` (5) on a fixed monotonic schedule, plus one immediate
out-of-cycle heartbeat after a role switch completes.

Request:
```json
{"cpu_pct": 12.5, "ram_used_mb": 1234, "ram_total_mb": 7890,
 "reported_role": "backtest", "acked_epoch": 4,
 "jobs": [{"id": "...", "lease_token": "...", "progress": 0.4, "checkpoint": {...}}],
 "released": [{"id": "...", "lease_token": "...", "progress": 0.4, "checkpoint": {...}}],
 "want_job": true, "code_version": "a1b2c3d4e5f6", "skew_ms": 12}
```
`checkpoint` inside `jobs[]` is optional (send when it changed since the last heartbeat).
(changed: limits) `jobs[]` and `released[]` hold at most 64 entries, a `checkpoint` is at
most 64 KiB of JSON, floats must be finite (no NaN/Infinity), and any request body above
256 KiB is 413; violations are 400.

Response:
```json
{"desired_role": "backtest", "role_epoch": 4, "kill": false,
 "preempt": ["<job id>"], "cancel": ["<job id>"], "lost": ["<job id>"],
 "claimed": [{"id": "...", "kind": "sleep", "params": {...}, "checkpoint": null,
              "progress": 0.0, "lease_token": "...", "lease_seconds": 30}],
 "code_version": "a1b2c3d4e5f6", "server_time": "...", "heartbeat_seconds": 5}
```
Host-side processing, in ONE transaction, in this order:
1. Update the worker row: `last_heartbeat_at=now()`, cpu, ram, `reported_role`,
   `acked_epoch`, `code_version`, `skew_ms`. (changed: races) The bearer token is checked
   inside this locking UPDATE, so a heartbeat that raced a register is 401 after the
   rotation commits; `acked_epoch` only moves forward (`GREATEST`), so a stale heartbeat
   cannot undo an ack; `prev_token_hash` is cleared.
2. **Renew** every `jobs[]` entry whose `(id, lease_token, lease_worker_id)` matches and
   whose status is `leased` or `cancel_requested`: `lease_expires_at = now() + lease_seconds`,
   `progress`, `checkpoint` (if given), `updated_at`. Ids that did not renew go in `lost[]`
   (the worker must SIGKILL that runner and forget the job).
3. **Release** every `released[]` entry whose token matches and whose `lease_worker_id`
   is this worker (changed: fence on the caller too): `status='queued'`, store
   checkpoint + progress, clear lease fields, `preempt_requested=false`; `expiries` is NOT
   incremented (changed: step 2, except for reason `oom`, see Release reasons). `job_events` `released`. If the job was `cancel_requested` it becomes
   `cancelled` instead. (changed: re-dispatch) If the job's target was chosen by the
   system (`jobs.target_auto`, set by the any_idle pick and the dispatcher) the target is
   cleared so the dispatcher can place it elsewhere; an owner-chosen target stays.
4. `preempt[]` = ids of this worker's leased jobs where `preempt_requested` or status
   `cancel_requested`. (changed: step 2, release reasons) `cancel[]` = the subset of
   `preempt[]` whose status is `cancel_requested`, so the agent releases those with
   reason `cancel` and the rest with `preempt`; an agent that ignores `cancel[]` still
   behaves correctly.
5. **Auto-return to idle**: if `workers.auto_role` and `desired_role != 'idle'` and
   `reported_role == desired_role` and `acked_epoch == role_epoch` and this worker has no
   `leased`/`cancel_requested` job, no `queued` job targeted at it, and (changed: avoid a
   needless idle flip) no untargeted `queued` job of its `desired_role` with
   `run_after <= now()` -> set `desired_role='idle'`, `role_epoch += 1`, `auto_role=false`.
   (A job sent to a chosen or idle machine flips it into the needed role and this flips
   it back; waiting work of the same role is claimed first.)
6. **Claim**: only if `workers.enabled`, `want_job`,
   `reported_role == desired_role`, `acked_epoch == role_epoch`, and the role is a batch role
   (`backtest|model_search|train`); (changed: kill scope, step 2) the kill flag refuses
   claims for the `trade` role only, batch roles keep claiming under kill; claim at most 1 job:
   ```sql
   WITH c AS (
     SELECT id FROM jobs
      WHERE status='queued' AND role=$role AND run_after<=now()
        AND (target_worker_id IS NULL OR target_worker_id=$wid)
      ORDER BY (target_worker_id IS NOT DISTINCT FROM $wid) DESC, created_at
      LIMIT 1 FOR UPDATE SKIP LOCKED)
   UPDATE jobs j SET status='leased', lease_worker_id=$wid, lease_token=gen_random_uuid(),
          lease_expires_at=now()+make_interval(secs=>$lease), started_at=COALESCE(started_at,now()),
          preempt_requested=false, updated_at=now()
     FROM c WHERE j.id=c.id RETURNING j.*;
   ```
   `job_events` `claimed`. (Trade role claims come in step 4.)
   (changed: claim idempotency) Before claiming, any `leased` job held by this worker that
   the request did not mention in `jobs[]` or `released[]` is an orphan (its claim reply was
   lost): its lease is renewed and it is returned in `claimed[]` with its existing
   `lease_token`, `job_events` `re-offered`, and nothing new is claimed while one exists.
   An orphan in `cancel_requested` is cancelled outright (`job_events` `cancelled`); its id
   may still appear in this reply's `preempt[]`, which the worker ignores for jobs it is
   not running. A worker therefore never holds two leases from one lost reply.
7. Respond with the worker's current `desired_role`/`role_epoch` (after step 5).

### `POST /api/v1/jobs/{job_id}/checkpoint`
```json
{"lease_token": "...", "checkpoint": {...}, "progress": 0.4, "release": false}
```
Stores checkpoint/progress (token must match, status leased/cancel_requested, else 409).
`release=true` -> same as a `released[]` entry (back to `queued`, or `cancelled` if cancel was
requested). Used by the drain path when the agent wants the release acknowledged before it
switches role. Response: `{"status": "<new status>"}`.
(changed: caller fence) On `/checkpoint`, `/complete` and `/fail` the bearer token's worker
must be the job's `lease_worker_id`, else 409. `checkpoint` and `result` are at most 64 KiB
of JSON (400 above that); `error` is at most 16 KiB.

### `POST /api/v1/jobs/{job_id}/complete`
`{"lease_token": "...", "result": {...}}` -> `status='succeeded'`, `result`, `progress=1`,
`finished_at`, lease cleared. Idempotent: a second call with the same token on a succeeded job
returns 200. Token mismatch / wrong status -> 409.

### `POST /api/v1/jobs/{job_id}/fail`
`{"lease_token": "...", "error": "..."}` -> `status='failed'`, `error`, `finished_at`, lease
cleared. Terminal (an explicit failure is not retried; only lease expiry retries).

### Downloads (no auth)
- `GET /dl/version` -> `{"code_version": "a1b2c3d4e5f6", "sha256": "<tarball sha256>"}`
- `GET /dl/worker.tar.gz` -> tarball containing exactly one top-level directory `fleet/`
  (the installed `fleet` package: pure Python, no compiled files, no `__pycache__`).
  `code_version` = first 12 hex of sha256 over the sorted relative paths + file bytes of
  the `fleet/` tree, computed at host start. The same value is written to
  `fleet/VERSION` inside the tarball.
- `GET /install.sh` -> `deploy/install_worker.sh` with the literal string
  `__FLEET_HOST_URL__` replaced by `FLEET_PUBLIC_URL`.

## Owner routes (JSON API used by the dashboard, CLI and tests)

- `GET /api/fleet` -> `{"workers": [...], "settings": {...public subset...}, "server_time": ...}`.
  Each worker: `id, name, online (bool), desired_role, reported_role, role_epoch, acked_epoch,
  switching (bool: acked_epoch != role_epoch or reported != desired), auto_role, enabled,
  cpu_pct, ram_used_mb, ram_total_mb, code_version, python_version, hostname,
  last_heartbeat_at, current_jobs: [{id, kind, status, progress}]`.
- `POST /api/workers/{id}/role` `{"role": "backtest"}` -> sets `desired_role`,
  `role_epoch += 1`, `auto_role=false`; marks this worker's leased jobs whose `role` differs
  from the new role `preempt_requested=true`; `audit_log`. Returns the worker. 400 on bad role.
  (Step 4 adds: switching away from `trade` cancels open orders first.)
- `POST /api/workers/{id}/enabled` `{"enabled": false}`; disabled workers never claim.
- `POST /api/jobs` `{"kind": "sleep", "params": {"seconds": 30}, "target": "any_idle" | "<worker_id>",
  "idempotency_key": "optional"}` -> the job (201). Rules, all in one transaction:
  - `role` derived from kind. Unknown kind -> 400.
  - `target = "<worker_id>"`: insert with `target_worker_id`. If that worker's `desired_role`
    differs from the job's role: set `desired_role = role`, `role_epoch += 1`, `auto_role = true`,
    and mark its leased jobs of other roles `preempt_requested`. (Step 4 adds the trade
    confirmation.) If the worker is unknown -> 404.
  - `target = "<worker_id>"` records `target_auto=false`; the any_idle pick and the
    dispatcher record `target_auto=true` (changed: see heartbeat step 3 and the reaper).
  - `target = "any_idle"`: pick one online idle worker under lock:
    ```sql
    SELECT id FROM workers w
     WHERE enabled AND desired_role='idle' AND reported_role='idle' AND acked_epoch=role_epoch
       AND last_heartbeat_at > now() - make_interval(secs=>$online_after)
       AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.target_worker_id=w.id
                       AND j.status IN ('queued','leased','cancel_requested'))
     ORDER BY last_heartbeat_at DESC LIMIT 1 FOR UPDATE SKIP LOCKED
    ```
    If found: same as the chosen-worker path (target it, flip its role, `auto_role=true`).
    If none: insert untargeted; the dispatcher assigns it later. Response includes
    `"waiting_for_idle_worker": true`.
  - `idempotency_key` present and already used -> return the existing job (200).
- `POST /api/jobs/{id}/cancel` -> `queued` -> `cancelled`; `leased` -> `cancel_requested`
  (worker releases it on its next heartbeat and the host marks it `cancelled`).
- `GET /api/jobs?status=&limit=` (newest first) and `GET /api/jobs/{id}` (job + last 50
  `job_events`).
- `POST /api/enroll-token` -> `{"token": "...", "expires_at": ...}` (1 hour). Also prints the
  one-line install command: `curl -fsSL <FLEET_PUBLIC_URL>/install.sh | sudo bash -s -- <FLEET_PUBLIC_URL> <token>`.
- `GET /api/settings` / `POST /api/settings` `{"key": value, ...}` (jsonb values; unknown key -> 400).
  (changed: validation and audit) Values are checked per key with no coercion (the string
  `"false"` is rejected); any problem rejects the whole batch with 400 and changes nothing.
  Every changed key writes an `audit_log` row `settings_changed` (entity = key, actor = the
  owner login, before/after). Schema:

  | key | type | range |
  |---|---|---|
  | `live_enabled`, `kill_switch` | bool | |
  | `tz` | string | IANA name such as `America/New_York` |
  | `lease_seconds` | int | 10..3600 |
  | `heartbeat_seconds` | int | 1..60 |
  | `online_after_seconds` | int | 5..3600 |
  | `max_expiries` | int or null | 1..100 (null = retry forever) |
  | `liquidity_floor_cents`, `max_bet_cents`, `default_bankroll_cents` | int | 0..10^11 (one billion dollars) |
  | `max_daily_loss_cents` | object | `{"live": int 0..10^11, "paper": int 0..10^11}` |
  | `min_edge`, `kelly_fraction` | number | 0..1 |
  | `trade_max_games` | int | 0..100 |

  Readers treat only the JSON `true` as on (`value is True`).
  (changed: cross-field check) The fleet timing keys are also judged together, over the
  stored settings merged with the update: `lease_seconds >= 2 * heartbeat_seconds + 5`
  (two renewals plus the agent's 4 s HTTP timeout, otherwise every lease expires between
  heartbeats) and `online_after_seconds > heartbeat_seconds` (otherwise workers flicker
  offline between beats). A violation is a 400 naming both keys and stores nothing.
- `GET /healthz` -> `{"ok": true, "db": true}`.

## Background loop (host, every 5 s, single thread)

1. **Reaper**: every `leased`/`cancel_requested` job with `lease_expires_at < now()`:
   `expiries += 1`; `cancel_requested` -> `cancelled`; else if `max_expiries IS NULL OR
   expiries < max_expiries` -> `queued` (checkpoint kept) else `failed` with
   `error='failed after N expiries (last: lease expired)'` (changed: the counter is shared
   with `oom` releases, so the text names the total and the last cause). Lease fields
   cleared, `preempt_requested=false`.
   `job_events` `lease_expired`. (changed: re-dispatch) A system-chosen target
   (`target_auto`) is cleared so the job can go to another idle worker.
2. **Dispatcher**: for each untargeted `queued` batch job, oldest first: run the any-idle
   pick above; if a worker is found, target the job at it and flip its role (`auto_role=true`).
   Every enabled idle worker is a dispatch target, including one the owner just set to
   `idle` by hand (a released job with a system-chosen target may come straight back to
   it); a worker that must not take work is disabled, not idled.

## Worker agent (`python3 -m fleet.worker run`)

Files: `/var/lib/fleet/worker.conf` (JSON: `host_url, worker_id, worker_token`, mode 0600,
owned by system user `fleet`), `/var/lib/fleet/app/<code_version>/fleet/...` and the
symlink `/var/lib/fleet/app/current`. The unit runs
`PYTHONPATH=/var/lib/fleet/app/current python3 -m fleet.worker run`.
Env `FLEET_STATE_DIR` overrides `/var/lib/fleet` (tests).
(changed: durability and rollback) The agent also keeps `pending_posts.json` (unsent
`/complete` and `/fail` calls, resent on the next start) and, under `app/`, `previous`
(the version `current` pointed at before the last self-update), `pending.json`
(`{"version", "starts"}`: a self-updated version that has not registered yet) and
`bad_versions` (one version per line; never updated to again).

States: `BOOT` (conf missing -> exit 78; the unit's `RestartPreventExitStatus=78` stops the
restart loop) -> `REGISTER` (retry with backoff 1,2,4..30 s until the host answers; adopt
`held_jobs`, start their runners from checkpoint) -> `ACTIVE` -> (`DRAINING` ->) `ACTIVE`.

Loop (fixed 5 s period on a monotonic clock; HTTP timeout 4 s):
1. Build the heartbeat: cpu% from a `/proc/stat` delta, RAM from `/proc/meminfo`, current
   role, `acked_epoch`, running jobs with latest progress/checkpoint, pending releases,
   `want_job = (role is a batch role and no runner is active)`.
2. On response:
   - `lost[]`: SIGKILL those runners, forget them.
   - `claimed[]`: start a runner per job. (changed: re-offer) `claimed[]` can repeat a job
     the worker already holds (its claim reply was lost) or has just finished whose
     `/complete` or `/fail` is still pending; the worker must not start a second runner for
     a job it is running or has a pending completion for.
   - `preempt[]`: stop those runners (see drain), then release them with their last
     checkpoint on the next heartbeat (or immediately via `/checkpoint release=true`).
   - `desired_role != role` or `role_epoch != acked_epoch`: **drain**: send SIGTERM to every
     runner, wait up to 3.0 s for each to print its final checkpoint and exit, SIGKILL
     survivors; collect last checkpoints; set `role = desired_role`,
     `acked_epoch = role_epoch`; send one immediate out-of-cycle heartbeat carrying
     `released[]` and the new role. (Step 4 adds the trade release call before this.)
   - `code_version` differs from the running one: **self-update** once no runner is active
     and no `/complete`, `/fail` or release is pending (or right after a drain): download
     `/dl/worker.tar.gz`, verify sha256 against `/dl/version`, extract to `app/<version>`
     (regular files and directories under `fleet/` only), write `app/previous` and
     `app/pending.json`, atomically repoint `app/current`, exit 75 (systemd
     `Restart=always` brings the new code up; it re-registers and re-adopts held jobs
     within one heartbeat). (changed: rollback) `run` counts starts of the pending
     version; a start that fails before the agent runs exits 1, and after 3 failed
     starts `app/current` is repointed at `app/previous`, the version is appended to
     `app/bad_versions` and the process exits 75 so systemd restarts the old code. The
     first successful register clears `pending.json`. A version listed in
     `bad_versions` is never downloaded again.
   - Network failure: keep the runners going; after 2 misses mark `degraded`.
     (changed: kill before the lease can expire) The agent remembers the send time of
     the last acknowledged heartbeat or register; once
     `max(lease_seconds - heartbeat_seconds - http_timeout, lease_seconds / 2)` seconds
     have passed without an answer (checked on every miss and between heartbeats, every
     0.1 s) it SIGKILLs its runners and goes back to `REGISTER`, so a runner never
     outlives its lease on the host.
3. Runners that finish: `POST /complete` (or `/fail`) with the token; retry until the host
   answers (2xx or 4xx; a 5xx or no answer is retried). (changed: durability) While the
   call is unsent the job stays in heartbeat `jobs[]` (id, token, progress) so the host
   keeps the lease alive and does not re-offer it; unsent calls survive re-register
   (re-keyed with the new lease token from `held_jobs`), self-update (which waits for
   them) and restarts (`pending_posts.json`). A runner stopped by an external SIGTERM is
   released, not failed.
4. Shutdown (SIGTERM to the agent): finished runners are completed, the rest are drained
   and released, unsent posts get 3 bounded attempts, and one final heartbeat carries
   `released[]` with `want_job=false`. Unsent posts are written to `pending_posts.json`.

Worst-case role-change latency: 5.0 s (poll) + 3.0 s (drain) + ~1 s (two round trips) = 9 s.

### Runner

Each batch job runs in a child process: `python3 -m fleet.worker.runner` with the job
(id, kind, params, checkpoint, progress) as JSON on stdin, in its own process group
(SIGTERM/SIGKILL go to the whole group, so grandchildren die with the runner). The
agent reports the stored `progress` until the first unit of a resumed job is done. It writes one JSON object per line on
stdout:
- `{"checkpoint": {...}, "progress": 0.42}` after every unit of work (units must take < 3 s),
- `{"done": true, "result": {...}}` on success,
- `{"stopped": true, "checkpoint": {...}, "progress": 0.42}` after SIGTERM,
- `{"error": "..."}` on failure.
SIGTERM sets a stop flag that the job checks between units. The agent keeps the last
checkpoint it saw; that is what gets released. Env: `OMP_NUM_THREADS=1`.

Job registry (`fleet/worker/jobs.py`): `JOBS = {"sleep": run_sleep}`; a job function has the
signature `run(params, checkpoint, emit, should_stop) -> result`. `sleep` sleeps
`params["seconds"]` in 1 s units with checkpoint `{"elapsed": n}` and result
`{"slept": seconds}`.

### Enrollment (installer)
`python3 -m fleet.worker enroll --host URL [--token T] [--name NAME]` registers and writes
`worker.conf`; without `--token` the token comes from env `FLEET_ENROLL_TOKEN`.
`python3 -m fleet.worker status` prints the conf (token redacted) and the last heartbeat
result.

`deploy/install_worker.sh HOST_URL [ENROLL_TOKEN] [--name NAME] [--reenroll]
[--token-file PATH]` (served as `/install.sh` with `HOST_URL` baked in) creates the
`fleet` user, downloads and verifies the tarball, installs it under `app/<version>`,
repoints `current`, writes the systemd unit and enrolls when `worker.conf` is missing
(or `--reenroll` is given). (changed: installer hardening) The token may also come from
env `FLEET_ENROLL_TOKEN` or `--token-file`, which keeps it out of the sudo log and the
process list; the installer passes it to `enroll` through the environment. Tarball
members are checked before extraction (regular files and directories under `fleet/`
only, no `..`, no `.pyc`), `tar` runs with `--no-same-owner --no-same-permissions
--no-overwrite-dir` and the tree is chmod'ed `u=rwX,go=rX`; the sha256 check is an
integrity check only (same origin as the tarball), so serve `/install.sh` and `/dl`
over the tailnet only. A `HOST_URL` that differs from the stored `host_url` is written
into `worker.conf` (identity and token kept). An installer-chosen version is not a
self-update candidate (`app/pending.json` is removed).

## Step 2 additions

### Release reasons
`released[]` entries (heartbeat) and `POST /checkpoint` with `release=true` may carry
`"reason": "drain" | "preempt" | "cancel" | "oom" | "shutdown" | "stopped"`. The host stores it in the
`released` job event's `detail` and ignores unknown values. (changed: one more value)
`stopped` is a runner that an outside SIGTERM ended while the agent itself kept running
(`shutdown` is the agent stopping); `cancel` is used for ids the heartbeat reply listed in
`cancel[]`, `preempt` for the rest of `preempt[]`, `drain` for jobs stopped by a role change. A release with reason `oom`
counts like a lease expiry: `expiries += 1`, and the job fails once `expiries` reaches
`max_expiries`, so a job that always runs out of memory does not bounce around the fleet
forever. All other reasons leave `expiries` alone.
(changed: precise semantics) The `released` event detail is `{"status", "reason"}` (no
`reason` key when none or an unknown one was sent) plus `"expiries"` for `oom`. A job that
fails this way gets `status='failed'`, `error='failed after N expiries (last: out of
memory)'`, `finished_at`, its checkpoint kept. A `cancel_requested` job released with `oom` becomes `cancelled` (the
expiry is still counted). `/checkpoint` with `release=true` returns the new status
(`queued`, `cancelled` or `failed`).

### Memory watchdog (agent)
Every service-loop pass (at most once per second per runner) the agent sums the memory of
every process whose session id is the runner's. (changed: shared pages counted once) The
per-process figure is `Pss_Anon + Pss_Shmem` from `/proc/<pid>/smaps_rollup`: proportional,
so copy-on-write pages shared by forked children count once, and anonymous or shmem only,
so file pages the kernel can drop (a mmapped data file, the interpreter's own text) do not
count. Kernels without the split fall back to `Pss`, then to `RssAnon + RssShmem` and
finally `VmRSS` from `/proc/<pid>/status`. If the sum exceeds `watchdog_rss_fraction`
(default 0.8) of `ram_total_mb`, the agent stops the runner (SIGTERM, grace, SIGKILL),
releases the job with its last checkpoint and reason `oom`, and logs a warning with the
measured MB. Tests may lower the threshold through `AgentOptions`.

### Kill flag
`kill` in register and heartbeat responses mirrors `settings.kill_switch`. It affects the
**trade** role only: under kill the host refuses trade claims and (step 4) every order
approval, and a trade worker stops proposing and asks the host to cancel its open orders.
Batch roles (`backtest`, `model_search`, `train`) keep claiming and running; a kill switch
that also stopped backtests would hide research for no safety gain. The agent records the
flag in `status.json` so `python3 -m fleet.worker status` shows it.

Owner API: `POST /api/kill` sets `kill_switch=true` (idempotent, audit row `kill`). `POST
/api/kill/reset` with body `{"confirm": "RESUME"}` clears it (audit row `kill_reset`); any
other body is 400. Step 4 adds the cancel-all behind `/api/kill`.
(changed: audit detail) Every press of kill writes a `kill` audit row (before/after carry
the flag) so repeated presses are visible; `kill_reset` stores the typed text in
`audit_log.confirmation_text` and the flag is unchanged on a 400. Both reply with
`{"kill_switch": <bool>}`. `host/kill.py` (`set_kill`, `reset_kill`, `is_killed`) is the
single implementation behind the API, the dashboard forms and the CLI. (changed: race)
Both writers lock the `kill_switch` row (`SELECT ... FOR UPDATE`) before reading it, so a
press that overlaps an uncommitted reset waits and then re-applies: the last action wins
and the audit order matches the final flag. The dashboard form and the CLI compare the
typed text exactly like the API (no whitespace trimming).

### Owner routes added in step 2
- Dashboard pages (HTML, same owner auth as `/api`): `GET /` (fleet), `GET /jobs`,
  `GET /jobs/{id}`, `GET /settings`, `GET /kill/confirm`.
- Fragments for the 5 s refresh: `GET /fragments/fleet` (the card grid), `GET /fragments/topbar`.
- Form posts (HTML forms, `application/x-www-form-urlencoded`, redirect back with a flash
  message): `POST /workers/{id}/role`, `POST /workers/{id}/enabled`, `POST /jobs` (send),
  `POST /jobs/{id}/cancel`, `POST /settings/{group}`, `POST /enroll-token`, `POST /kill`,
  `POST /kill/reset`. Each is a thin wrapper over the JSON route of the same name.
  (changed: form details) The redirect is a 303 to the page the form lives on; the
  message travels in a short-lived `flash` cookie (HttpOnly, SameSite=Lax, 60 s) that the
  next page render shows once and clears, so a crafted link cannot put text into the
  status line and a reload does not repeat it (changed: was the query string). Fields: `role`; `enabled`
  (`true`/`false`); `kind`, `seconds` (sleep, 1..86400, default 60), `target` (`any_idle`
  or a worker id); `next` (optional local path to return to after a cancel); `confirm`
  (kill reset). Settings groups are `trading` (`max_bet`, `max_daily_loss_paper`,
  `max_daily_loss_live`, `default_bankroll`, `liquidity_floor` in dollars, converted to
  the `*_cents` keys server side, plus `min_edge`, `kelly_fraction`, `trade_max_games`),
  `fleet` (`lease_seconds`, `heartbeat_seconds`, `online_after_seconds`, `max_expiries`,
  blank = null) and `tz` (`tz`). A rejected settings or reset form re-renders the
  settings page with the error inline and status 400; nothing is stored.
  `POST /enroll-token` renders the token page directly (the token is shown once), it does
  not redirect.
- `GET /static/*` (stylesheet, script) needs no owner login; every other dashboard path
  does. On dashboard paths (anything outside `/api/`) 400/401/403/404/405/409/413 render a
  small HTML page instead of the JSON `{"detail"}` body, including the framework's own 404
  for an unknown path or a missing static file (changed: was JSON).
- (changed: hardening) Every response outside `/api/` carries `X-Frame-Options: DENY`,
  `Content-Security-Policy: frame-ancestors 'none'`, `Referrer-Policy: same-origin` and
  `X-Content-Type-Options: nosniff`: owner auth comes from the network, so a page that
  framed the dashboard could otherwise click-jack KILL or a Disable link with a passing
  Origin. Every HTML page and redirect is `Cache-Control: no-store` (the enroll token page
  must not come back from the back-forward cache). The 256 KiB body limit also counts a
  chunked body as it arrives, so a missing Content-Length is not a way around it.
- Dashboard timestamps (jobs, job events, audit log, enroll expiry) are shown in
  `settings.tz` with the zone abbreviation (`2026-10-02 23:20:34 EDT`); an unknown zone
  name falls back to UTC.
- `GET /api/pnl` -> `{"today_cents": 0, "all_time_cents": 0, "by_worker": {"<id>": 0}}`
  computed by `host/pnl.py` (zeros until step 4 adds bets and fills).
- `GET /api/audit?limit=20` -> newest audit rows (`limit` 1..500; full rows including
  `confirmation_text`).
- CLI: `kill`, `kill-reset` (prompts for RESUME unless `--yes`), `roletest <worker>`
  (sends a 120 s sleep job to the worker, waits for the ack, flips the role to `train`,
  waits for the ack and prints `ack - request` in seconds from host timestamps; exit 1 if
  over 10 s). (changed: details) `request` is `audit_log.ts` of the `set_role` row and
  `ack` is `workers.last_heartbeat_at` as written by the heartbeat that acked the epoch.
  Each wait is bounded by `--timeout` (default 30 s); a worker that never acks is exit 1
  too. The sleep job is cancelled on every exit path (success, timeout, limit exceeded,
  Ctrl-C), so a worker that never acked does not run it when it comes back; the worker is
  left in `train` (or in the role it was flipped to).
