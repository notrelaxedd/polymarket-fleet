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
- **Online**: `last_heartbeat_at > now() - online_after_seconds` (settings, default 30;
  15 before the fleet UI, see "Fleet UI additions").

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
  `Tailscale-User-Login: <login>` equal to env `FLEET_OWNER_LOGIN` (one login, or a
  comma separated list of owners, compared case-insensitively). That header is injected
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
 "heartbeat_seconds": 3}
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

Sent every `heartbeat_seconds` (default 3; 5 before the fleet UI) on a fixed monotonic schedule, plus one immediate
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
most 256 KiB of JSON (64 KiB before step 6), floats must be finite (no NaN/Infinity), and
any request body above 1 MiB (256 KiB before step 6) is 413; violations are 400.

Response:
```json
{"desired_role": "backtest", "role_epoch": 4, "kill": false,
 "preempt": ["<job id>"], "cancel": ["<job id>"], "lost": ["<job id>"],
 "claimed": [{"id": "...", "kind": "sleep", "params": {...}, "checkpoint": null,
              "progress": 0.0, "lease_token": "...", "lease_seconds": 30}],
 "code_version": "a1b2c3d4e5f6", "server_time": "...", "heartbeat_seconds": 3}
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
must be the job's `lease_worker_id`, else 409. `checkpoint` and `result` are at most 256 KiB
of JSON (400 above that; 64 KiB before step 6); `error` is at most 16 KiB.

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
  last_heartbeat_at, current_jobs: [{id, kind, status, progress}]` (a `trade` job also
  carries `game`, "KC @ LV", for the fleet card).
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
  (worker releases it on its next heartbeat and the host marks it `cancelled`). (changed:
  step 4) A `trade` job's assignment is halted first, in the same transaction: its open
  orders are cancelled with the ledger release and `/trading` shows it `halted`
  (Activate makes a new trade job).
- `GET /api/jobs?status=&limit=` (newest first) and `GET /api/jobs/{id}` (job + last 50
  `job_events`).
- `POST /api/enroll-token` -> `{"token": "...", "expires_at": ...}` (1 hour). Also prints the
  one-line install command: `curl -fsSL <FLEET_PUBLIC_URL>/install.sh | sudo bash -s -- <FLEET_PUBLIC_URL> <token>`.
- `GET /api/settings` / `POST /api/settings` `{"key": value, ...}` (jsonb values; unknown key -> 400).
  (changed: validation and audit) Values are checked per key with no coercion (the string
  `"false"` is rejected); any problem rejects the whole batch with 400 and changes nothing.
  (changed: step 4) `kill_switch` is read-only here (400 "use /api/kill or /api/kill/reset":
  only the kill transaction and the RESUME reset move it), and `live_enabled: true` is 400
  while `kill_switch` is true.
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
  | `fee_model` | object | `{"taker_rate": 0..1, "half_spread": 0..1}` (step 3) |
  | `thresholds_backtest` | object | `{"min_bets": int 0..10^6, "min_roi": -1..1, "max_drawdown": 0..1}` (step 3) plus, optional with defaults (step 6): `"require_validation": bool`, `"min_roi_ci_low": -1..1`, `"max_market_p": 0..1`, `"forbid_flags": [overfit, fragile, regime_dependent]` (distinct) |
  | `backtest_seasons` | array | `[first, last]`, ints 1999..2100, `last` may be null, `last >= first` (step 3); the search era |
  | `validation_seasons` | array | same shape (step 6); `first` must be after `backtest_seasons[1]` when that is set, and after `backtest_seasons[0]` when it is null (cross-field, 400 naming both) |
  | `search_workers` | `"auto"` or int | 1..64 (step 6) |
  | `thresholds_paper` | object | the step 4 keys plus optional `"clv_ci_excludes_zero": bool` (step 6) |
  | `nflverse_refresh_hours` | int | 1..168 (step 3) |
  | `nflverse_url` | string | an `http(s)://` URL, at most 512 chars (step 3) |

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
  must not come back from the back-forward cache). The 1 MiB body limit also counts a
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

## Step 3 additions

### Data for workers
- `GET /api/v1/data/games` (worker bearer): every `games` row as JSON (fields listed in
  docs/MODELS.md), sorted by kickoff. Supports `ETag` / `If-None-Match` (304 when unchanged;
  the ETag is `<count>-<max updated_at epoch>`; writers are serialised and stamp
  `updated_at` with the wall clock, so the tag only ever moves forward, even when a
  request transaction that opened earlier commits later). The agent refreshes its cache file
  `<state>/cache/games.json` before starting any backtest, model_search or train runner and
  passes the path to the child in `job["context"]["games_path"]`.
  (changed: precision and shape) The epoch carries microseconds (`2761-1791053870.123456`)
  so two changes within one second still produce different tags; the header value is quoted
  (`ETag: "2761-..."`) and `If-None-Match` is accepted bare, quoted, weak (`W/`), as a list
  or `*`. The body is `{"games": [...], "count": n}`; `div_game` is `0`/`1`, `kickoff_at` an
  ISO UTC string with a `Z` suffix, `game_type` `REG` or `POST` (every playoff round folds
  into `POST`), scores and moneylines ints or null. Rows are served in `(kickoff_at,
  game_id)` order.
- `GET /api/v1/models/{id}` (worker bearer): `{id, lineage_id, family, params, artifact,
  parent_model_id, trained_through, status, backtest_metrics}`. 404 for an unknown or
  non-uuid id.
- `POST /api/v1/models` (worker bearer): body `{job_id, family, params, artifact | null,
  backtest_metrics | null, summary | null, parent_model_id | null, trained_through | null}`
  -> `{"id": ..., "lineage_id": ..., "created": true|false, "status": ...}` (changed: the
  reply also carries the lineage status after eligibility ran). Idempotent on
  `(family, params_hash, trained_through)`: an existing row is returned with `created: false`
  and HTTP 200 (201 when created); `params_hash` is computed server side with the
  docs/MODELS.md rule (`fleet.models.base.params_hash`), so `24.0` and `24.0000001` are the
  same model. The lookup is serialised on an advisory lock, so two workers posting the same
  model at once get the same row. (changed: review) When the existing row is a root and
  the post carries `backtest_metrics`, those replace the lineage's stored metrics (the
  latest evaluation wins, as with `POST /models/{id}/backtest`) and eligibility runs
  again, so the first search to evaluate a params set no longer fixes its metrics for good;
  the reply's `status` is the recomputed one. A child (`parent_model_id` set) inherits the
  parent's `lineage_id`, `status` and `backtest_metrics` (changed: a child's own
  `backtest_metrics` field is ignored, the lineage shares the root's; the parent row is
  locked while the child is written, so a backtest committing on the lineage at the same
  time cannot leave the child with the old metrics); a root gets `lineage_id = id` and
  status `candidate`, then eligibility runs. `job_id` must be a job leased by the calling
  worker (`leased` or `cancel_requested`, 409 otherwise) and bound to the write (changed:
  review): a root needs a `model_search` job, a child a `train` job whose
  `params.model_id` is the parent (409 otherwise), and a child must keep the parent's
  `family` and params (same `params_hash`, 400 otherwise); the model records the job as
  `created_by_job_id`. `params` values must be numbers, and NaN or Infinity anywhere in
  `params`, `artifact` or `backtest_metrics` is 400 (Postgres jsonb refuses them; the
  agent fails the job instead of retrying a 500). (changed: validation) Unknown `family` (not in
  `fleet.models.registry.FAMILIES`), non-object `params`, `artifact` or `backtest_metrics`,
  an unknown `parent_model_id` or a `trained_through` that is not `[season, week]` (an
  object `{"season", "week"}` is accepted too) are 400; a worker summary is capped at 2000
  characters; `artifact` and `backtest_metrics` are limited to 256 KiB each. `job_events`:
  `model_created` (`{model_id, status}`) or `model_exists` (`{model_id, status}`).
- `POST /api/v1/models/{id}/backtest` (worker bearer): `{job_id, backtest_metrics}` stores the metrics on that model (and on every row of its lineage) and re-runs eligibility.
  Reply `{"id", "lineage_id", "status"}`; `job_id` is fenced like above and must be a
  `backtest` job whose `params.model_id` is `{id}` (409 otherwise); a non-object
  `backtest_metrics`, or one holding NaN or Infinity, is 400; `job_events` `model_backtest`.
  Eligibility reads `thresholds_backtest` with a shared row lock before any model row is
  touched, so a model write and a thresholds change never interleave: the write waits for
  the new thresholds, and the thresholds change waits for in-flight writes before it
  recomputes every lineage.

### Runner context and results
The agent fills `job["context"]` before starting a runner: `{"games_path": "<cache file>",
"model": <GET /api/v1/models/{id} body or null>}` (the model is fetched when
`params.model_id` is set). When a runner finishes with a result containing
`create_models: [...]`, the agent posts each entry to `POST /api/v1/models` (with the job
id) in order, replaces the list with `created_models: [{"id", "lineage_id", "created"}]`,
and only then calls `/complete`. For a `backtest` job with `params.model_id`, the agent
also posts the metrics to `POST /api/v1/models/{id}/backtest` before completing. Posting
is retried like any pending post; the job is not completed until every post succeeded.

### Job kinds and params (validated by the host at creation, 400 on error)
- `backtest`: `{"model_id": uuid}` or `{"family": str, "params": {...}}`, plus optional
  `"seasons": [first, last]` (default settings `backtest_seasons`). Result: the metrics
  object from docs/MODELS.md plus `per_season`.
- `model_search`: `{"family": str, "n": int (1..5000, default 200), "seed": int,
  "seasons": [first, last], "top_k": int (1..20, default 5)}`. Result: see docs/MODELS.md.
- `train`: `{"model_id": uuid, "through": {"season": int, "week": int}}`. Result:
  `{"created_models": [...], "through": [season, week], "games_seen": n}`.
- `validate` (step 6, role `backtest`): `{"model_id": uuid, "seed": int (default 1)}`. Result:
  `{"validation_metrics", "stress_metrics"}` (docs/ROBUSTNESS.md A1 to A3), posted to
  `POST /api/v1/models/{id}/validation` before `/complete`.
- `sleep` stays for tests.
- (changed: exact rules, `host/jobparams.py`) For the three batch kinds any key outside
  the lists above is 400 (`unknown backtest params: ...`); `model_id` must be a uuid of an
  existing model (`unknown model`, 400 even though the lookup is a miss); `backtest` takes
  `model_id` or `family` + `params`, not both, and `params` defaults to `{}` (the family's
  defaults) and must hold numbers only; `family` must be a registered family; `seasons` is
  `[first, last]` with ints in 1999..2100, `last` may be null and must not precede `first`;
  `seed` defaults to 0; `through.week` is 1..22. (changed: review) `params` keys must be
  hyperparameters the family knows (`unknown elo_blend params: K, HFA`, 400), and a
  `seasons` range that no backtest can score is 400 (`no testable season in [first,
  last]`: a test season needs games and three earlier seasons with moneylines, judged on
  the `games` table when it has rows). `sleep` keeps its test shape: `seconds`
  (1..86400) is checked when present and other keys are tolerated. The host then copies
  the limits in force into the stored params of every batch job: `fee_model`,
  `default_bankroll_cents`, `max_bet_cents`, `trade_max_games` and `backtest_seasons`
  (from settings, a null last season resolved to the newest season whose every game is
  final; the null stays when `games` is empty), and resolves a null last season inside
  the job's own `seasons` the same way. A later settings change never touches a job that
  was already sent. Validation runs before the idempotency-key lookup.

### Owner API added in step 3
- `GET /api/models` (leaderboard by lineage: ranked and unranked lists), `GET /api/models/{id}`
  (model, lineage members, jobs that created or updated it), `POST /api/models/{id}/summary`
  `{"summary": str}` (max 600 chars, audit row), `POST /api/models/{id}/status`
  `{"status": "retired"}` (only `retired` is allowed from the owner; audit row).
  (changed: shapes, `host/leaderboard.py`) Each leaderboard entry is `{id, lineage_id,
  family, params, short_params ("K 24 · HFA 55 · MOV on"), status, summary, metrics {roi,
  n_bets, log_loss, market_log_loss, max_drawdown, seasons, hit_rate, avg_edge, pnl_cents},
  score (shrunk ROI), members (rows in the lineage), created_at, updated_at}` plus `rank`
  on ranked entries. Ranked: the root has `n_bets >= 50` and the lineage is not retired,
  ordered by score desc, log-loss asc, created_at; unranked are newest first. The detail
  adds `short_params`, `score`, `lineage: [{id, parent_model_id, trained_through, status,
  created_at, created_by_job_id, is_root}]` (root first) and `jobs` (the job that created
  the row plus every job whose `params.model_id` is it, newest first, `created_model`
  flag). A summary is trimmed, an empty one stored as null, over 600 characters is 400;
  the audit row is `model_summary` (entity = model id, before/after the text). Retiring
  sets `retired` on every row of the lineage, which drops it from the ranked list for
  good (eligibility never un-retires it); audit row `model_retired`. Both reply with the
  detail document. A change of `thresholds_backtest` through `POST /api/settings` or the
  settings form recomputes every lineage; a change of `thresholds_paper` through either
  recomputes the paper gate of every lineage with a paper record.
- `POST /api/jobs` accepts the three real kinds with the params above; the dashboard jobs
  page gets one form per kind (backtest, model search, train) with the owner's defaults from
  settings.
- `POST /api/data/refresh` (owner): fetch nflverse games.csv now; returns
  `{"rows": n, "updated": m, "fetched_at": ...}`. The host loop also refreshes every
  `nflverse_refresh_hours` (settings, default 6) and at startup when `games` is empty.
  (changed: details, `host/nflverse.py` and `host/data_refresh.py`) The reply also carries
  `inserted`, `changed` (`updated = inserted + changed`), `skipped` (records the parser
  could not read: a non-numeric season or week, a bad date) and `source`; a download
  failure or a body over 50 MB is 502 (`{"detail": "nflverse fetch failed: ..."}`), a file
  that is not a games.csv is 400, a row Postgres refuses is 502 (`nflverse ingest failed:
  ...`), the timeout is 60 s and the download runs outside the request's transaction.
  One malformed or duplicated record never aborts the ingest: an optional field that is
  not a finite number in range (`NA`, `nan`, `1e12`) becomes null, a `gametime` that is
  not `HH:MM` (`TBD`) falls back to 13:00 local, and a duplicated `game_id` keeps the last
  record. Rows are upserted by `game_id`; a row is rewritten (and its `updated_at` moved)
  only when its raw CSV record differs, so a re-ingest of the same file changes nothing.
  The last outcome of any refresh (time and counts, or the error) is shown in the Settings
  page's "nflverse games" section.
  The periodic refresh runs in its own thread (`DataRefreshThread`, checked every 60 s)
  so a slow download never delays the reaper and dispatcher; a failure is logged and
  retried after 15 minutes; a host that starts with games already loaded waits a full
  interval before its first fetch.
- Settings keys added: `fee_model {"taker_rate": 0.05, "half_spread": 0.01}`,
  `thresholds_backtest {"min_bets": 200, "min_roi": 0.02, "max_drawdown": 0.30}`,
  `backtest_seasons [2010, null]` (null = last complete season), `nflverse_refresh_hours 6`,
  `nflverse_url` (the games.csv URL). All editable on the Settings page (ranges in the
  settings table above).
- CLI: `ingest-games [--file PATH]`, `models` (table), `send-job` accepts `--params` for
  the three kinds (unchanged syntax). (changed: output) `ingest-games` prints
  `ingested N rows from <source>: I inserted, C changed; last complete season S` (exit 1
  with `cannot read <path>` for a missing file, or the fetch error); `models` prints
  `rank id status family params roi bets log_loss market drawdown seasons rows`, ranked
  rows first (`-` as rank for unranked).
- Dashboard routes added (same owner auth, form posts redirect with a flash): `GET /models`,
  `GET /models/{id}`, `POST /models/{id}/summary` (`summary`, `next`), `POST
  /models/{id}/retire` (`next`), `POST /data/refresh` (the "Refresh now" button on the
  settings page), `GET /jobs?train_model=<id>` (prefills the train form), settings groups
  `fees` (`taker_rate`, `half_spread`), `thresholds` (`min_bets`, `min_roi`,
  `max_drawdown`), `seasons` (`seasons_first`, `seasons_last`, blank = null) and
  `nflverse` (`nflverse_refresh_hours`, `nflverse_url`). The `POST /jobs` form carries
  `kind` plus, per kind: backtest `model_id` (blank = use `family` + `params` JSON),
  `seasons_first`, `seasons_last`; model_search `family`, `n`, `seed`, `seasons_first`,
  `seasons_last`, `top_k`; train `model_id`, `through_season`, `through_week`; sleep
  `seconds`; always `target`. A rejected job form re-renders the jobs page with the error
  inline in the posted form and status 400; nothing is stored.

### Tables added in step 3 (`host/migrations/0003_models.sql`)
`games` (one row per nflverse game, `game_id` primary key, `status` scheduled|final,
`raw` the full CSV record, indexes on `(season, week)` and `kickoff_at`) and `models`
(`id`, `lineage_id`, `family`, `params`, `params_hash`, `artifact`, `parent_model_id`,
`trained_through`, `summary`, `status` candidate|paper_ok|live_eligible|retired,
`backtest_metrics`, `created_by_job_id`, timestamps; unique on `(family, params_hash,
COALESCE(trained_through::text, ''))`, index on `lineage_id`).

## Step 4 additions

### Trade role claims
A `trade` worker sends `"want_jobs": <free slots>` (int, `trade_max_games` minus the trade
jobs it holds; `want_job: true` still means 1 for batch roles). The host claims up to that
many `trade` jobs (same claim CTE, `LIMIT $n`), refusing under `kill`. Trade jobs have
`max_expiries NULL`; a crashed trade worker's job expires after `lease_seconds` and any
trade worker reclaims it (all state is on the host). (changed: shape) `want_jobs` is an
integer 0..100 (400 outside), a trade worker's `want_job: true` claims nothing, and a
batch worker's `want_jobs` is ignored. A trade job's `checkpoint` is unused and a release
with reason `oom` never fails it either (`max_expiries` is NULL). (changed: cap) The host
also caps a claim at `settings.trade_max_games` minus the trade jobs the worker already
holds, so a worker's `want_jobs` never exceeds the setting (and `trade_max_games 0`
pauses claims fleet-wide).

### `GET /api/v1/trade/state` (worker bearer)
Returns `{"kill", "server_time", "settings": {"min_edge", "kelly_fraction", "participation",
"trade_pregame_only", "fee_model", "trade_tick_s", "max_bet_cents", "trade_max_games"},
"assignments": [...]}` (the worker caps its stake with `max_bet_cents`, its size with
`participation` and its claims with `trade_max_games`), one entry per
trade job this worker holds: `{"id", "job_id", "lease_token", "status", "mode",
"max_bet_cents", "game": {game row fields incl. kickoff_at, status}, "model": {"id",
"family", "params", "artifact"}, "bankroll": {"available_cents", "reserved_cents",
"open_cost_cents", "realized_pnl_cents"}, "markets": [{"id", "side", "bid", "ask", "mid",
"tick", "min_size", "snapshot_id", "snapshot_at", "liquidity_usd_cents", "ask_depth",
"bid_depth", "status"}], "open_orders": [{"id", "market_id", "side", "price", "size",
"filled_size", "status"}], "positions": [{"market_id", "side", "size", "basis_cents",
"avg_cost"}]}` (step 6 Part B added `bid_depth`, the open order's `side` and the game's
`signals` and `team_stats`, and renamed `avg_price` to `avg_cost`; see "Step 6 additions
(Part B)" below). Markets below the liquidity
floor are included but flagged `"below_floor": true`. (changed: detail) Only markets with a
confirmed mapping are listed; `game` is the games row without `raw`; `open_orders` entries
also carry `snapshot_id` and `created_at`;
an entry is omitted when a held trade job's assignment no longer exists.

### `POST /api/v1/orders/request` (worker bearer)
`{"client_request_id", "job_id", "lease_token", "assignment_id", "market_id", "snapshot_id",
"price", "size", "my_p", "market_p", "edge", "rationale"}` ->
`{"status": "approved"|"rejected", "order_id", "reason"}` (200 in both cases; 401 only for
auth failures). See docs/TRADING.md for the checks. (changed: fence) A lease mismatch (wrong
token, another worker's job, a preempted or cancel-requested job, a worker whose desired
role is not `trade`) is a recorded rejection with reason `lease`, not a 409, so the worker's
log and the owner's orders table show it. A repeated `client_request_id` returns the stored
decision with `"duplicate": true`. A malformed price (outside 0..1), size (< 1) or a body
missing the ids is 400. A request naming an unknown market cannot be stored (`orders.market_id`
is NOT NULL) and answers `{"status": "rejected", "order_id": null, "reason": "market"}`.

### `POST /api/v1/orders/{id}/cancel` (worker bearer)
Marks the worker's own open order `cancel_requested` (paper: cancelled at once with the
ledger release). `{"status": ...}`. (changed: codes) 404 for an unknown order, 409 for
another worker's order; a terminal order answers its current status unchanged.

### `POST /api/v1/trade/release` (worker bearer)
`{"jobs": [{"id", "lease_token"}]}`: cancels the worker's open orders for those assignments
(paper at once; live `cancel_requested`, waiting up to 3 s), releases the jobs to `queued`
(reason `drain`), answers `{"cancelled": n, "pending": m, "released": [ids]}`. The agent
calls it before the ack heartbeat of any role change away from `trade`, and under kill it
keeps running the tick (proposing nothing) until told otherwise. (changed: detail) Entries
whose id or token do not match a trade job this worker holds are ignored (not 409); `pending`
counts live orders still `cancel_requested` when the 3 s wait ends (including ones already
pending from an earlier call); the orders and job releases are committed before the wait so
the exchange can see the cancel requests. (changed: fence) The release runs under both
approval locks, so an approval that read a job as leased cannot commit after the job
has been handed back.

### Owner API added in step 4
- `GET/POST /api/assignments`, `POST /api/assignments/{id}/halt|activate|settle`,
  `POST /api/assignments/activate-paper` (after a kill reset). (changed: shapes)
  `POST /api/assignments` takes `{"game_id", "model_id", "mode" (default paper),
  "bankroll_cents" (default `default_bankroll_cents`), "max_bet_cents" (optional)}` and
  answers 201 with the row plus `bankroll` and `job_id`; 400 for a bad input or a retired
  lineage, 409 for the per-game limits and the live preconditions. `GET /api/assignments?status=`
  rows carry `bankroll`, `game`, `model`, `job` and `open_orders`; `GET /api/assignments/{id}`
  is the same row shape plus `orders`, `fills` and `positions`. `halt` takes an optional `{"reason"}` (idempotent
  on a halted row; 409 on settled); `activate` is 409 under kill, for a final game or when
  a live precondition fails; `settle` is 409 until the game is final and 503 ("exchange
  module not available") when host/exchange is missing; `activate-paper` answers
  `{"activated": n}` and is 409 under kill. Audit rows: `assignment_created`,
  `assignment_halted`, `assignment_activated`, `activate_all_paper`.
- `GET /api/orders?status=&limit=`, `POST /api/orders/{id}/cancel`, `GET /api/fills`,
  `GET /api/markets?unmatched=1`, `POST /api/markets/{id}/link` `{"game_id", "side"}`.
  (changed: detail) `status` is one order status or `active`; `&assignment_id=` filters;
  rows carry `market_title`, `side`, `game_id`, `platform`, `worker_name`, `model_id`,
  `family` and (step 6C) `in_play` (an untagged order approved under the in-game rules);
  `GET /api/orders/{id}` adds `events` and `fills`. `GET /api/fills?limit=` rows
  carry the order's `assignment_id`, `worker_id`, `market_id`, `side`, `game_id`. Markets
  carry `snapshot_age_s`; `link` writes audit `market_linked` and answers the market.
  `POST /api/cancel-all` `{"mode": "paper"|"live"|null}` cancels every active order (one
  mode's, or all) under the approval lock(s) without a kill, answers
  `{"cancelled", "requested"}`, audit `cancel_all`.
- `GET /api/exchange` (state), `POST /api/exchange/probe` (raw markets payload of the
  configured source, truncated to 64 KiB, for pasting back), `GET /api/pnl` (real now).
  (changed: detail) The state carries `heartbeat_age_s` and `down` (no heartbeat, or one
  older than 15 s); the probe is 503 "exchange module not available" without host/exchange.
- `POST /api/kill` now cancels orders and halts assignments (docs/TRADING.md).
  (changed: audit) The `kill` audit row keeps its before/after flag shape; when the press
  actually disabled live, cancelled or cancel-requested orders or halted assignments, a
  second row `kill_cancel_all` lists their ids. The live daily-loss trip writes
  `daily_loss_trip`. (changed: daily loss) The approval's daily-loss check counts the
  reservation of still-open orders of the mode as money at risk
  (`losses_today + reserved + cost > max_daily_loss`), so concurrent approvals across
  games cannot together pass the limit; the trip condition stays `losses_today >= max`.
- Orphan rule: the host loop's third pass cancels the active orders of workers whose last
  heartbeat is older than `orphan_cancel_after_s` (actor `orphan`, reason "worker silent").
- Settings keys added (all on the Settings page): `participation`, `book_max_age_s`,
  `orphan_cancel_after_s`, `gtd_seconds`, `snapshot_retention_days`, `snapshot_active_s`,
  `snapshot_idle_s`, `market_source`, `market_source_config`, `market_lookahead_days`,
  `max_paper_models_per_game`, `thresholds_paper`, `trade_pregame_only`, `trade_tick_s`,
  `rate_limits`, `max_exposure_cents`, `scores_url`.
- CLI: `assign <game_id> <model_id> [--mode paper] [--bankroll DOLLARS] [--max-bet DOLLARS]`,
  `assignments [--status]`, `orders [--status] [--limit]`, `cancel-all [--mode]`,
  `simulate-final <game_id> --home N --away M`, `exchange-state`, `ledger-check` (exit 1 and
  one line per problem when a bankroll's ledger disagrees with its cached columns).

## Step 5 additions

Workers are unchanged in step 5: they never learn whether an assignment is paper or live
beyond the `mode` field, and they never see keys. Host-side additions (see docs/LIVE.md):
- Owner routes (`host/api/owner_live.py`): `POST /live` with `{"confirm": "ENABLE LIVE
  TRADING YYYY-MM-DD"}` (today in `settings.tz`; a form field `confirm` is accepted too):
  400 unless the phrase matches exactly (the detail names the phrase to type), 409 when
  the kill switch is on, the exchange reports no credentials, `auth_ok` is false or
  `auth_checked_at` is older than 10 minutes, or `clock_skew_ms` is over
  `auto_kill.clock_skew_ms`; 200 with the live state. `POST /live/off` (optional
  `{"reason"}`): immediate, always 200, `live_enabled` false, live assignments halted,
  live orders `cancel_requested` for the exchange. Both answer JSON errors to JSON
  callers (a form post gets the dashboard error page). `GET /api/live`:
  `{live_enabled, live_enabled_at, live_enabled_by, credentials_present, auth_ok,
  auth_checked_at, auth_age_s, auth_failures, balance_cents, buying_power_cents,
  balance_checked_at, clock_skew_ms, last_auth_error, open_orders_checked_at, killed,
  auto_kill_reasons: [..] (since the last RESUME), expected_phrase, problems: [..]}`.
  `POST /api/exchange/probe-account`: fleet-host holds no key, so it returns what the
  exchange process recorded (`key_present`, `status`, auth and balance fields,
  `last_auth_error`) with `payload: null` and `raw_payload_command`, the exchange
  container's CLI command that returns the raw balance payload.
- `/api/settings` refuses `live_enabled` (400, "use /live or /live/off"), on or off, and
  refuses a `market_source_config.polymarket_us` block whose `live.base_url` is not
  https on `polymarket.us` (or a subdomain) or whose `auth.template` does not hold each
  of `{timestamp}`, `{method}`, `{path}`, `{body}` exactly once with nothing else but a
  few separator characters (400 naming the key): no settings write can redirect the
  signed requests or choose the signed bytes (`host/exchange/adapters/live_policy.py`).
- Exchange state columns: `auth_failures`, `credentials_present`, `last_auth_error`
  (also "credentials malformed: <secret-free message>" when exchange.env holds an
  unusable secret), `open_orders_checked_at`, `live_enabled_at`, `live_enabled_by`
  (the last two are cleared whenever live goes off: the switch, the kill, the
  daily-loss trip, cancel-all --direct).
- Settings keys: `auth_probe_interval_s`, `buying_power_max_age_s`, `submitting_grace_s`,
  `auto_kill {"auth_failures", "clock_skew_ms"}`, `smoke_hold_seconds`, `live_fills_poll_s`,
  `open_orders_audit_s`, and `market_source_config.polymarket_us.auth` / `.live` blocks
  (defaults applied in code when absent).
- Exchange loop tasks (`host/exchange/main.py`): `auth` (at start and every
  `auth_probe_interval_s`), `open_orders_audit` (at start and every `open_orders_audit_s`),
  `live_fills` (every `live_fills_poll_s`, only while a live order is active); the live
  tasks run only when credentials loaded. At start: auth, then `startup_reconcile`
  (submitting rows, then the audit) before the executor's first submission. A clock skew
  over the limit, measured by the auth probe or on any other live answer, pauses live
  placements only (`ExchangeLoop.live_paused`, `Executor.live_blocked`; the gateway
  refuses to sign a place over `max_skew_ms`) and auto-kills `clock_skew`; cancels, the
  open-order audit and the fills poll keep running so the kill's cancels reach the
  exchange, and the first live answer with the skew back in range clears the pause.
- Auto-kill reasons (`kill.auto_kill`, actor `auto:<reason>`, audit `auto_kill`):
  `auth_failures`, `clock_skew`, `unknown_order`, `unknown_fill`,
  `ambiguous_reconciliation`, `late_fill` (a fill the ledger cannot book: its order is
  already closed here, or it exceeds the open size). A trigger fires only while the kill
  switch is off; a condition that persists after RESUME kills again on the next pass.
- Executor live path: `Executor(paper_gateway, live_gateway)`; a live row is closed only
  after its fills were read: when the fills call fails nothing closes that pass and the
  task reports `fills unavailable`. A live cancel counts as confirmed only when
  `open_orders()` no longer lists the order (its last fills are absorbed before the
  release), closing as `expired` past `gtd_at`, else `cancelled`; a live expiry goes
  through that same path (`cancel_requested`, reason `gtd`); our rows missing on the
  exchange are closed from their fills as `filled`, `cancelled` (cancel_requested, or
  dropped by the exchange) or `expired` (past `gtd_at`). The fills poll runs while a
  live order is active and for 120 s after the last one closed.
- Order approval check `mode` (live) also requires the market to be on the live
  platform: the current `market_source`, never `polymarket_clob`. Check `buying_power`
  (live): `exchange_state.buying_power_cents` with `balance_checked_at` within
  `buying_power_max_age_s`, and `cost + SUM(live bankrolls.reserved_cents) + SUM(live
  ledger fill cents since balance_checked_at) <= buying_power_cents`; missing or stale
  rejects. A live assignment's creation and activation need a confirmed open market of
  the game on that platform and take the live approval lock before the gate.
- CLI (`python -m host.exchange.cli`): `exchange-smoke --confirm "SMOKE YYYY-MM-DD"
  [--market ID] [--hold S] [--drive]` (`--drive` submits and cancels from this process
  when the exchange service is stopped; otherwise the running loop's outbox does it; a
  row not `open` within 30 s is cancelled before the command exits 1), `cancel-all
  --direct` (live off first, then the exchange; exit 1 when the fills call failed and
  rows were left `cancel_requested`), `probe-account`, `auth-check`. The live commands
  need the credentials in the environment (exit 1 otherwise, "secret malformed: ..."
  for a present but unusable secret) and never print them.
- Audit actions: `live_on` (confirmation_text = the phrase), `live_off` (`reason`,
  `assignments_halted`, `orders_cancelled`, `orders_cancel_requested`), `auto_kill`,
  `smoke_order`, `cancel_all` with `direct: true` (`remote`, `cancelled`, `rows_closed`,
  `live_was_on`, `assignments_halted`, `approved_cancelled`, `left_for_exchange`,
  `error`, after a `live_off` row with reason `cancel-all --direct`),
  `eligibility_changed` for every demotion out of `live_eligible` (followed by the
  `assignment_halted` rows), `model_retired` with `assignments_halted`, and the step 4
  `daily_loss_trip` which now follows a `live_off` row.

## Step 6 additions (Part A: robustness, docs/ROBUSTNESS.md)

The search era keeps its settings key `backtest_seasons` (no rename); the held-out era
is `validation_seasons`.

- Settings keys: `validation_seasons [2022, null]` (null = last complete season),
  `search_workers "auto"`, `thresholds_backtest` gains `require_validation true`,
  `min_roi_ci_low 0.0`, `max_market_p 0.1`, `forbid_flags ["overfit", "fragile"]` and its
  `min_bets` becomes 50 (it applies to the validation era); `thresholds_paper` gains
  `clv_ci_excludes_zero true`. The migration (`0006_robustness.sql`) rewrites the
  thresholds rows to the new objects, adds the key to the paper thresholds, and moves a
  still-default `backtest_seasons [2010, null]` to `[2010, 2021]`. The two eras must not
  overlap: `validation_seasons[0] > backtest_seasons[1]` whenever the latter is set, and
  `validation_seasons[0] > backtest_seasons[0]` when it is null (so the capped search era
  is never empty).
- Model rows gain `validation_metrics` and `stress_metrics` (held on every row of the
  lineage like the backtest metrics); `lineage_paper_ci` caches the paper CLV bootstrap
  per lineage (`n_bets`, `avg_clv`, `clv_low`, `clv_high`, `computed_at`).
- Job kind `validate` (`{"model_id", "seed"}`, role `backtest`); the model must exist.
  The host copies into a `model_search` and a `validate` job, besides the step 3 limits,
  `validation_seasons` (null last resolved to the last complete season) and `workers`
  (`search_workers`). The search era never reaches into the validation era
  (`host/eras.py`): a null last `backtest_seasons` resolves to the last complete season
  capped at the season before the validation era, and these are 400, never run on a
  fallback era: a `model_search` whose own `seasons` end in or after the validation era
  (`seasons must end before the validation era (which starts in 2022)`); a `backtest` with
  `model_id` (its result becomes the lineage's search-era `backtest_metrics`) whose
  `seasons` do; a `backtest`, `model_search` or `validate` when `backtest_seasons` is
  malformed, empty once capped, or reaches into the validation era; a `model_search` or
  `validate` when `validation_seasons` does not resolve (for example it starts after the
  last complete season); and a `validate` of a model whose lineage was searched on a
  season at or after the first validation season (the root's `backtest_metrics.seasons`,
  else the range of the search job that created it): its validation would be in-sample,
  so models searched on the step 3 default `[2010, null]` must be searched again. A
  `backtest` by `family` and `params` may still test any seasons. The worker refuses the
  same overlap (a validate job errors when the context model's `backtest_metrics.seasons`
  reach the validation era) so the overfit flag never compares overlapping eras. A `train`
  job, which never reads the eras, keeps the old fallback.
- `POST /api/v1/models` accepts `validation_metrics | null` and `stress_metrics | null`
  (objects, 256 KiB each, no NaN or Infinity) and stores them on creation; an identity hit
  on a root replaces the stored validation and stress metrics too (latest wins, each
  field independently) and re-runs eligibility; a child inherits the lineage's three
  metrics objects. `GET /api/v1/models/{id}` returns them.
- `POST /api/v1/models/{id}/validation` (worker bearer): `{job_id, validation_metrics,
  stress_metrics}` stores both on every row of the lineage and re-runs eligibility;
  reply `{"id", "lineage_id", "status"}`; `job_events` `model_validation`. `job_id` must
  be leased by the caller and be a `validate` job whose `params.model_id` is `{id}` or a
  `model_search` job that created or found that model (a `model_created` or
  `model_exists` event of the job names it); any other job is 409. A non-object or
  missing metrics field, or NaN or Infinity inside, is 400; an unknown model 404.
- Eligibility (`host/eligibility.py`, `host/paper_gate.py`): with `require_validation`
  the gate judges `validation_metrics`: `n_bets >= min_bets`, `roi >= min_roi`,
  `max_drawdown <= max_drawdown`, `ci.roi[0] >= min_roi_ci_low`, `market_p <=
  max_market_p` (both inclusive) and no flag of `forbid_flags` among
  `validation_metrics.flags` and `stress_metrics.flags`; a lineage without validation
  metrics (or with a missing CI or p) is a candidate. With `require_validation false`
  the three step 3 rules judge `backtest_metrics` as before. The paper gate adds
  `clv_ci_excludes_zero`: the 5th percentile of the bootstrapped stake-weighted average
  CLV over the lineage's settled paper bets with a CLV (B = 1000, `random.Random("paper:
  <lineage_id>:boot")`, percentiles by linear interpolation) must be above 0 over at least
  `min_bets` such bets; the interval is cached in `lineage_paper_ci` on every paper
  recompute (settlement, a paper thresholds save). Demotion halts live assignments as
  before. The host recomputes every lineage once at startup, after the migrations
  (`host/startup.py`: the backtest gate on every lineage, the paper gate on every lineage
  with a paper record), so a gate a migration made stricter demotes existing lineages
  (and halts their live assignments) on the first boot instead of at their next
  settlement.
- Owner API: each leaderboard entry gains `validation` (`roi, n_bets, log_loss,
  market_log_loss, max_drawdown, seasons, hit_rate, avg_edge, pnl_cents, shrunk_roi, ci,
  mean_ll_gain, market_p, flags, calib_slope, calib_intercept, brier_decomposition,
  beats_market` or null when not validated), `validated`, `score` (the validation shrunk
  ROI, 0 when not validated), `search_score` (the search-era shrunk ROI), `ll_gain`,
  `flags` (validation then stress flags), `stress_flags`, `paper_ci` (`{n_bets, avg_clv,
  ci: [low, high], computed_at}` or null) and `rank_mode` `paper | validation`; an
  unranked entry carries `unranked_reason` (`not validated` or `retired`; a lineage
  without validation metrics is unranked whatever its paper record). Ranked: the
  paper-ranked lineages first (unchanged), then every validated, non-retired lineage by
  validation shrunk ROI desc, `mean_ll_gain` desc, `created_at`. The detail adds
  `validation_metrics`, `stress_metrics`, `validation`, `validated`, `score`,
  `search_score`, `flags`, `flag_meanings` and `paper_ci`.
- Dashboard routes: `GET /jobs?validate_model=<id>` prefills the validate form; the
  `POST /jobs` form takes kind `validate` with `model_id` and `validate_seed`; settings
  group `thresholds` gains `min_roi_ci_low`, `max_market_p` and the checkboxes
  `require_validation`, `forbid_overfit`, `forbid_fragile`, `forbid_regime_dependent`
  (unticked = false, an empty list); group `seasons` gains `validation_first`,
  `validation_last` (blank = null) and `search_workers` (blank or `auto` = auto, else an
  integer); group `trade` gains the `paper_clv_ci` checkbox.

## Step 6 additions (Part B: signals and recorded prices for workers, docs/ROBUSTNESS.md B)

### Data for workers
- `GET /api/v1/data/games` (worker bearer, unchanged route, `ETag`, `304`, `Cache-Control:
  no-cache`): the body becomes `{"games": [...], "count": n, "team_game_stats": [...],
  "decision_minutes_before_kickoff": m}`. Workers that predate the change ignore the new
  keys. An optional query `?decision_minutes=N` (integer 0..300, 400 otherwise) sets the
  injury cutoff; without it the current `decision_minutes_before_kickoff` setting
  applies. `m` is the cutoff the signals used. A snapshot backtest fetches the feed
  with its own `params.decision_minutes_before_kickoff` into its own cache file
  (`<state>/cache/games.d<N>.json` and `.etag`), so a setting changed after the job was
  created never lets it count a report filed after its bet; a feed that states another
  cutoff, or none, fails the job. The agent caches the object body whole (games,
  `team_game_stats` and `m`), so `load_games` attaches each game's team stats; a bare
  list from an older host is cached as it is. Each game row gains `"signals":
  {"home_qb_changed", "away_qb_changed", "home_out_qb", "away_out_qb", "home_out_count",
  "away_out_count"}` (ints; the flags are 0 or 1):
  - `*_qb_changed` is 1 when the team's starting quarterback for this game
    (`games.raw` `home_qb_id` / `away_qb_id`) differs from its last known starter in its
    previous games, across seasons. A team's first game, and a game whose starter is
    unknown (an unplayed game), read 0.
  - `*_out_count` counts the players of the game's (season, game_type, week, team) listed
    `Out` in `injuries`; `*_out_qb` is 1 when any of them plays QB. Only rows whose
    `date_modified` is strictly before the decision time (kickoff minus the decision
    minutes: the query's, else the `decision_minutes_before_kickoff` setting) count; a row modified later, or without a
    date, never counts, so a backtest cannot use a report written after its bet.
  `team_game_stats` lists every team-game row `{"game_id", "season", "week", "team",
  "kickoff_at", "off_epa_per_play", "def_epa_per_play", "pass_rate", "plays",
  "success_rate"}` sorted by `kickoff_at, game_id, team` (`kickoff_at` from `games` when
  the game is known, ISO UTC with `Z`). The ETag is
  `<games count>-<games max updated_at>.<injuries count>-<max updated_at>.<team_game_stats
  count>-<max updated_at>.d<decision minutes used>`: it changes when games, injuries or
  team stats change, or when the decision lead (which the injury signals depend on)
  does. Clients treat it as opaque.
- The trade state's `game` carries the same `signals` plus `team_stats: {"home": [...],
  "away": [...]}`: each side's team_game_stats rows strictly before this game's kickoff
  with both EPA values (the worker's games cache drops a row without them too), oldest
  first, at most 16 (`host/signals.py` `game_signals`).
- `GET /api/v1/data/prices?since=<iso>&platform=<name>` (worker bearer, `ETag`,
  `If-None-Match` as for games, `304`, `Cache-Control: no-cache`) -> `{"markets":
  [{"market_id", "game_id", "side": "home"|"away", "platform", "confirmed": true,
  "closing_price": float|null, "kickoff_at", "bars": [[minute, bid, ask, close,
  min_liquidity_usd_cents], ...], "depth": [[ts, bid_depth, ask_depth], ...]}], "count":
  n}`.
  - Markets: confirmed mappings with a side, of games with `kickoff_at >= since` (every
    game without `since`; a date means midnight UTC, a naive time is UTC), ordered by
    kickoff, game, side. `platform` names one platform; without it every platform except
    `sim` is served, so simulated prices only reach a worker that asks for `platform=sim`
    (and the worker refuses them unless `allow_sim_prices`).
  - Bars and depth lie inside `[kickoff - 6 h, kickoff)`. Bars are the rolled-up
    `price_bars` merged with bars built the same way (last mid as close, last bid and ask,
    least liquidity) from the raw `price_snapshots` not rolled up yet; for a minute that
    has both, the raw close, bid and ask win and the liquidity is the least of the two.
    Depth is the raw snapshots thinned to the last one per minute (`bid_depth` and
    `ask_depth` are the stored `[[price, size], ...]` levels, best first).
  - ETag: `<price_bars count>-<max minute>-<max price_snapshots id>-<max markets
    updated_at>-<games ETag>-<hash of since and platform>`. A bad `since` or `platform`
    is 400; a missing or unknown token is 401.

### Snapshot replay jobs and results (docs/ROBUSTNESS.md B1)
- `backtest` params gain an optional `"price_source": "closing_line" | "snapshots"`
  (stored only when named; absent means `closing_line` on host and worker; any other
  value is 400, and the key is 400 on the other kinds as an unknown param). A
  `snapshots` backtest also gets, copied from settings at creation,
  `decision_minutes_before_kickoff` (0..300, default 60), `allow_sim_prices` (true only
  when the setting is exactly true), `price_platform` (settings `market_source`) and
  `participation` (0..1, default 0.5); a closing-line backtest does not get them. For a
  `snapshots` backtest a null last season, in its own `seasons` or in the copied
  `backtest_seasons`, resolves to the newest season in `games` (the season in progress
  included), without the validation-era cap.
- Runner context: a `snapshots` backtest's context also has `"prices_path"`, the worker's
  cache of `GET /api/v1/data/prices?since=2000-01-01&platform=<price_platform>`
  (`<state>/cache/prices-<platform>.json` and `.etag`). Platform `sim` without
  `allow_sim_prices` is not fetched and the job fails (`SimPricesRefused`).
- Result: the backtest metrics object plus `"price_source": "snapshots"`, `"platform"`,
  `"n_unscored_no_prices"` (also in each `per_season` entry) and a top-level
  `"avg_clv"` (the plain mean CLV over bets, null without bets). A closing-line result is
  unchanged (none of these keys).
- `POST /api/v1/models/{id}/backtest` routes by the job's `params.price_source`: a
  `snapshots` result is stored in `models.snapshot_metrics` on every row of the lineage,
  logged as job event `model_snapshot_backtest`, and leaves `backtest_metrics` and the
  status alone; the metrics' own `price_source` must match the job's (400 otherwise). A
  child created later inherits the lineage's `snapshot_metrics`.
- Owner API: each leaderboard entry and the model detail gain `snapshot` (`{n_games,
  n_bets, roi, pnl_cents, avg_clv, clv_estimated, clv_ci, roi_ci, log_loss,
  market_log_loss, platform, n_unscored_no_prices, seasons, score}` or null; `avg_clv` is
  the result's, else the middle of `ci.avg_clv` with `clv_estimated` true) and
  `rank_mode` can be `snapshot`; entries also carry `snapshot_score`. Ranked: paper
  first, then lineages with at least 30 snapshot bets and a CLV by `score` (`clv * bets
  / (bets + 25)`) desc, snapshot ROI desc, `created_at`, validated or not, then the
  validation-ranked ones.
- Migrations: `0007_signals.sql` (tables `injuries` and `team_game_stats`,
  `models.snapshot_metrics`, settings `allow_sim_prices false`,
  `decision_minutes_before_kickoff 60`, `nflverse_injuries_url`, `nflverse_pbp_url`,
  `signals_refresh_hours 24`) and `0008_sells.sql` (`orders.side`, `fills.basis_cents`,
  ledger kind `sell`, `bets.result` `sold`, `bets.order_side`).
- Dashboard routes: `POST /jobs` with kind `backtest` takes `price_source`; settings
  groups `replay` (`decision_minutes_before_kickoff`, the `allow_sim_prices` checkbox,
  unticked = false) and `signals` (`signals_refresh_hours` 1..168, `nflverse_injuries_url`
  and `nflverse_pbp_url`, http(s) templates that must contain `{season}`).

### Ingest (host)
- `injuries` is loaded from nflverse `injuries_{season}.csv` (setting
  `nflverse_injuries_url`, a template with `{season}`): game_type folds every playoff
  round into `POST`, team codes take the games aliases (OAK -> LV, SD -> LAC, STL -> LA),
  a player without a gsis_id is keyed `name:<full name>`, a duplicated key keeps the
  latest `date_modified`, an unreadable record is skipped and counted. Upserts rewrite a
  row (and move its `updated_at`) only when a field changed.
- `team_game_stats` is loaded from nflverse `play_by_play_{season}.csv.gz` (setting
  `nflverse_pbp_url`), streamed through gzip and csv and folded per (game, team) as it is
  read (a season is never held in memory; the download is capped at 200 MB). A play
  counts when its `play_type` is `pass` or `run` and its `epa` is finite: offence is
  `posteam`, defence `defteam`; `off_epa_per_play` and `def_epa_per_play` (EPA allowed)
  are means, `pass_rate` the share of offensive plays with `pass = 1`, `success_rate` the
  share with `success = 1`, `plays` the offensive plays.
- The host's data thread refreshes both for the current season (the newest season with a
  game kicking off within 30 days) and the previous one every `signals_refresh_hours`
  (default 24), first 5 minutes after start when either table is empty, else after a full
  interval. One season's failure (a 404 before a season's first report) never stops the
  others; when nothing succeeds it retries after 15 minutes.
- CLI: `ingest-injuries` and `ingest-pbp`, each with `--season <year>`, `--season all`
  (the full backfill: injuries from 2009, play-by-play from 1999, through the current
  season, one season at a time, each committed on its own) or `--file PATH` (a local
  CSV, or CSV.gz for play-by-play). One line per season (`pbp:2023: R rows, I inserted,
  C changed, S skipped`); exit 1 when any season failed (the others are still loaded) or
  when neither `--season` nor `--file` is given.

### Selling (Part B, docs/TRADING.md "Selling")
- `GET /api/v1/trade/state`: each market also carries `bid_depth` (the latest snapshot's
  `[[price, size], ...]` bid levels, best first); each open order carries `side`
  (`"buy"` | `"sell"`, the order side, not the team side); positions are signed (buys
  minus sells) rows `{"market_id", "side", "size", "basis_cents", "avg_cost"}` with
  `avg_cost = basis_cents / (size * 100)`, and a position sold down to zero disappears;
  `game` carries `signals` and `team_stats` as described under "Data for workers" above.
- `POST /api/v1/orders/request` takes an optional `"order_side": "buy"` (default) |
  `"sell"` (any other value is 400). The existing `side` key, when sent, is still the
  team side and is ignored. A buy's `client_request_id` keeps the step 4 formula; a
  sell's is `sha256(assignment|market|snapshot_id|price|size|sell)[:32]`. A sell is
  approved by `host/trading/sells.py` `approve_sell`: kill, lease, assignment, market,
  kickoff, mode, stale book, participation on the bid side, price band (0.01 to 0.99, on
  the tick, and `price >= bid - 0.05`), then `no_position`, `sell_exceeds_position` (size above the position minus the
  open sells) and `open_sell_exists` (at most one open sell per market). A sell reserves
  nothing (`cost_cents` 0) and skips the money checks. A sell may coexist with an open
  buy on the same market.
- The owner's kill cancels open sells like buys. Fills of a sell carry
  `fills.basis_cents` (the basis the sale removed) and post a ledger `sell` row; settled
  sells get their own `bets` row (`order_side` sell, `result` sold, `stake_cents` 0).

## Step 6 additions (Part C: in-game, docs/INGAME.md)

### Data for workers
- `GET /api/v1/data/pbp?seasons=A-B` (worker bearer; 401 without a valid token):
  every `pbp_rows` row of the seasons (`A` or `A-B`, 1999..2100, 400 otherwise; every
  season when omitted), ordered by game then play. The body is gzip-compressed JSON
  lines, one object per row in this key order: `game_id, play_id, season, home_win,
  score_diff, seconds_remaining, half, down, ydstogo, yardline_100, posteam_is_home,
  home_timeouts, away_timeouts, pregame_p_home, vegas_wp` (reals rounded to 6 digits),
  served as `Content-Type: application/x-ndjson+gzip` with no `Content-Encoding`, so
  urllib stores it as is. `ETag: "<count>-<max season>-<checksum>"` over the selected
  rows (unchanged by an idempotent re-ingest), `Cache-Control: no-cache`, 304 on a
  matching `If-None-Match`.
- The worker keeps it as `<state>/cache/pbp.jsonl.gz` with `pbp.etag` (`{"seasons",
  "etag"}`; the ETag is sent only for the same season selection), streamed to a
  temporary file and checked for the gzip magic before it replaces the cache; a failed
  fetch falls back to the cached file (`fleet/worker/pbp_cache.py`).

### Runner context and the in-game search
- A `model_search` with `params.family` `"ingame_wp"` gets the context `{"games_path":
  null, "model": null, "pbp_path": "<cache file>"}` and the games feed is not fetched.
  It asks for the seasons from the first train season to the last validation season
  (an open end is the current UTC year). Rows unavailable with no cached copy fail the
  job with `context unavailable: ...`.
- Params (`host/ingame_jobparams.py`, 400 on error): `{"family": "ingame_wp",
  "train_seasons": [first, last] (default [2012, 2021]), "validation_seasons": [first,
  last or null] (default [2022, null]), "n" (1..500, default 20), "seed" (default 1),
  "top_k" (1..20, default 5), "train_fraction" (above 0, at most 1, default 0.3)}`.
  Seasons are ints in 1999..2100; `seasons` is accepted in place of `train_seasons`
  (not both); a null last train season is capped at the season before the validation
  era; the train era must end before the validation era starts; any other key is 400
  (`unknown ingame_wp model_search params: ...`). No settings are copied in.
- Result: `{"evaluated", "train_seasons", "validation_seasons", "train_fraction",
  "n_fit_plays", "top", "create_models"}`. Each `create_models` entry is `{"family":
  "ingame_wp", "params", "artifact", "summary", "trained_through": null,
  "parent_model_id": null, "backtest_metrics" (the selection metrics with `"era":
  "search"`), "validation_metrics" (with `"era": "validation"`), "stress_metrics":
  null}`; the agent posts them to `POST /api/v1/models` as for any search. When the
  identity hits an existing ingame_wp root whose artifact differs, the host replaces
  the artifact, the three metrics and the summary in one update.
- A `backtest` of family `ingame_wp`, and a `backtest`, `validate` or `train` naming an
  ingame_wp `model_id`, is 400 at creation; a worker that gets one anyway fails it.

### `GET /api/v1/trade/state`
- `settings` gains `ingame_tick_s, ingame_max_state_age_s, ingame_quiet_seconds,
  ingame_cutoff_seconds, ingame_dead_zone, ingame_min_edge, ingame_max_bet_cents,
  ingame_gtd_seconds`.
- Each assignment entry gains `"ingame": {"enabled" (trade_ingame and an in-game model
  set), "model": {"id", "family", "params", "artifact"} | null, "game_state":
  {"state", "ts", "age_s", "source", "last_change": {"kind", "ts"} | null} | null,
  "pregame_p_home" (devigged closing moneyline, else the frozen closing price of the
  home market, else null), "lag": {"suspended", "median_lag_s", "n"}}`; `state` has
  exactly the keys `status, period, clock_seconds, home_score, away_score, possession,
  down, distance, yardline_100, home_timeouts, away_timeouts`. Open orders carry
  `ingame`.

### `POST /api/v1/orders/request`
- Optional `"ingame": true` (default false) and `"gtd_seconds"` (int 1..86400 or null,
  400 outside). An in-game request is decided by `host/trading/ingame.py` (reasons
  `ingame_disabled`, `ingame_paper_only`, `ingame_stale`, `ingame_quiet`,
  `ingame_cutoff`, `ingame_lag_suspended`; docs/INGAME.md). The order's GTD is always
  `settings.ingame_gtd_seconds`; the worker's `gtd_seconds` is only recorded in the
  approval event (`gtd_seconds_requested`). The worker's in-game `client_request_id` is
  `"ingame-" + sha256(assignment|market|snapshot_id|price|size|order_side|ingame)[:32]`.
- A request without `"ingame": true` on a game that has kicked off (not final) is
  rejected `kickoff` while `trade_pregame_only` is on; with it off it runs the same
  in-game checks and reasons (so nothing is approved after kickoff outside the in-game
  rules, and never a live order), keeps `orders.ingame` false and carries `"in_play":
  true` in its approval event (docs/TRADING.md, "In-game trading").

### Owner API
- `POST /api/assignments` also takes `"ingame_model_id"` (an ingame_wp model of a lineage
  that is not retired, else 400) and `"trade_ingame"` (bool; left out it takes
  `settings.trade_ingame`, false for a live assignment; true on a live assignment is
  409). An ingame_wp model as `model_id` is 400.
- `POST /api/assignments/{id}/ingame {"ingame_model_id", "trade_ingame"}` changes only the
  fields sent (an explicit null `ingame_model_id` clears it; an empty body is 400) on an
  active or halted assignment (409 otherwise), audited as `assignment_ingame`. Turning
  in-game trading off cancels the open in-game orders (`orders_cancelled` in the
  answer); changing the in-game model once in-game orders exist is 409.
- `GET /api/assignments` rows carry `ingame_model_id` and `trade_ingame`.
- `GET /api/models`: every entry carries `ingame` (`{"games", "bets", "pnl_cents"}` or
  null), `is_ingame`, `ingame_validation` and `ingame_reason`; ingame_wp lineages are
  listed in `unranked` with the reason `in-game model`, after the others.
- Dashboard forms: `POST /assignments/{id}/ingame` (the toggle), `POST
  /exchange/probe-gamestate` (field `event`, digits only), `POST /settings/ingame`, and
  `GET /trading?ingame_model=<id>` preselects the in-game model.

### Ingest and operator commands
- `python -m host.cli ingest-pbp-rows --season <year | first-last> [--file <csv.gz>]`
  loads nflverse play-by-play into `pbp_rows` (docs/INGAME.md); the host refreshes the
  current season weekly in season.
- `python -m host.exchange.cli probe-gamestate --event <espn id> [--yahoo] [--url
  <template>]`: one game-state request, printing the status, the first 64 KiB of the
  payload and what the parser extracted; it never raises.

## Fleet UI additions (the 3D fleet page, `fleet-ui/` served at `/fleet`)

### Settings: faster heartbeats, a longer online window
`heartbeat_seconds` defaults to 3 (was 5) and `online_after_seconds` to 30 (was 15), so
the page sees changes within a few seconds and a slow beat does not flicker a machine
offline. Migration `0011_fleet_ui.sql` moves the stored values only where they still
equal the old defaults; a value the owner changed is left alone. The code fallbacks for a
missing row are 3 and 30 as well. The cross-field rules (`lease_seconds >= 2 *
heartbeat_seconds + 5`, `online_after_seconds > heartbeat_seconds`) are unchanged.

### Register and heartbeat fields (worker -> host)
- `POST /api/v1/workers/register` takes two optional fields: `can_reboot` (bool: this
  install has the reboot unit, see below) and `boot_media` (`"flash" | "ssd" | "hdd" |
  "unknown"`). Both are stored when given and keep their stored value when absent
  (`can_reboot` defaults to false for a new worker). A wrong type or value is 400.
- The heartbeat takes four optional fields, each null when the agent does not know it:
  `temp_c` (float, hottest CPU sensor, -50..150), `boot_media` (as above),
  `wear_pct` (float 0..100, percent of the boot disk's rated life used) and
  `disk_gb_written` (float >= 0, GB written to the boot disk since boot). Floats must be
  finite; anything out of range or of the wrong type is 400. `temp_c`, `wear_pct` and
  `disk_gb_written` are stored as reported on every heartbeat (null clears them);
  `boot_media` keeps its stored value when absent.

### Reboot (owner -> host -> agent)
Host side:
- `workers.reboot_id` / `workers.reboot_requested_at` hold the owner's request. A
  request is **pending** while `reboot_id` is set and `reboot_requested_at > now() - 5
  minutes` (database clock).
- The heartbeat reply carries `"reboot": "<request id>"` while one is pending, else
  `"reboot": null`, and the heartbeat claims nothing new (no claims, no orphan
  re-offers) while one is pending. Renewals, releases, preempt and cancel work as usual.
- Register finishes a request: when `reboot_id` is set (pending or lapsed) and the
  presented `boot_id` differs from the stored one, the machine has rebooted, so
  `reboot_id` and `reboot_requested_at` are cleared and `audit_log` gets `reboot_done`
  (entity and actor = worker id, before `{reboot_id, boot_id}`, after `{boot_id}`).
- A request that lapses (5 minutes without the machine coming back with a new boot id)
  is no longer sent and no longer blocks claims; a new request replaces it.

Agent side (fleet/worker/agent.py, deploy/install_worker.sh):
- The worker unit sets `FLEET_REBOOT_TRIGGER=/run/fleet/reboot`; an agent with it
  registers `can_reboot: true`. On a reply with a new `reboot` id the agent stops the
  way it does on SIGTERM (runners drained and released, trade jobs handed back through
  the release handshake that cancels their orders, unsent posts flushed), then writes the
  request id to `$FLEET_REBOOT_TRIGGER`. A root `fleet-reboot.path` unit watches that
  path and runs `systemctl reboot`. `/run` is tmpfs, so the file never outlives the
  reboot; a trigger found at agent start means the reboot never happened: it is moved
  aside (`reboot.done`), its id is ignored and `can_reboot` stays false until a clean
  restart. Without `FLEET_REBOOT_TRIGGER` (an install older than the reboot unit) the
  request is logged once and ignored, and the owner route answers 409 until install.sh is
  re-run.
- `status.json` now lives in the runtime directory `/run/fleet` (`FLEET_RUN_DIR`;
  tmpfs, so the per-heartbeat rewrite causes no flash wear); outside systemd it falls back
  to the state directory as before. `python3 -m fleet.worker status` finds it in either.
- Telemetry is read from /proc and /sys each heartbeat (`fleet.common.hwinfo`, no
  writes); SMART wear comes from a root `fleet-wear.timer` writing
  `/run/fleet-wear/wear.json` hourly.

### Owner routes (same owner auth as every `/api` route)
- `GET /api/fleet` adds top-level `roles` (`[{"id", "name", "short"}]` in display
  order: idle Idle/Idle, backtest Backtest/Backtest, model_search Model Search/Search,
  train Training/Train, trade Trading/Trade, from host/labels.py) and `online_after_seconds` (int). Each
  worker adds `ram_pct` (used/total * 100, one decimal, null when unknown), `temp_c`,
  `boot_media`, `wear_pct`, `disk_gb_written` (as last reported, null when never),
  `seconds_since_heartbeat` (int by the database clock, null before the first
  heartbeat), `can_reboot` (bool) and `rebooting` (bool: a request is pending).
- `POST /api/workers/{id}/reboot` (no body) -> 200 `{"worker_id", "reboot_id",
  "requested_at"}`. Idempotent: a pending request returns the same id (even once the
  worker went offline for the reboot). 404 unknown worker; 409 `worker is offline` (no
  heartbeat within `online_after_seconds`); 409 `this worker cannot reboot yet: re-run
  install.sh on it` when `can_reboot` is false. Writes `audit_log` `reboot_requested`
  (entity = worker id, actor = owner login, after `{reboot_id}`).
- `GET /api/fleet/events?since=<ISO-8601>&limit=<1..200>` -> `{"events": [...],
  "server_time"}`, newest first. Without `since`: the newest `limit` (default 20). With
  `since`: every event with `ts >= since` (inclusive; the client dedupes by `key`),
  capped at `limit`. A bad `limit` or `since` is 400. Each event: `key` (`a:<audit_log
  id>` or `j:<job_events id>`), `ts`, `worker_id` (null for fleet-wide rows), `who` (the
  worker's name, or `fleet`), `tone` (`ok` normal, `hot` warning, `off` machine going
  down, `fg` neutral) and `text`, a short plain-English line:

  | source | text | tone |
  |---|---|---|
  | audit `set_role` | Moved to <role name> by <owner> | ok |
  | audit `auto_role` | Moved to <role name> for a job | ok |
  | audit `auto_idle` | Back to Idle, no work left | ok |
  | audit `set_enabled` | Enabled by <owner> / Disabled by <owner> | ok / fg |
  | audit `worker_enrolled` | Enrolled | ok |
  | audit `reboot_requested` | Reboot requested by <owner> | off |
  | audit `reboot_done` | Back up after a reboot | ok |
  | audit `kill` | Kill switch on by <owner> (not written for auto kills) | hot |
  | audit `auto_kill` | Kill switch on automatically: <reason> | hot |
  | audit `kill_reset` | Kill switch off by <owner> | ok |
  | job `claimed` | Took a <kind> job | ok |
  | job `succeeded` | Finished a <kind> job | ok |
  | job `failed` | A <kind> job failed | hot |
  | job `released` | Handed back a <kind> job; Stopped a cancelled <kind> job; Ran out of memory on a <kind> job (`oom`) | fg; fg; hot |
  | job `lease_expired` | Lost a <kind> job (no heartbeat) | hot |
  | job `cancelled` | Cancelled a <kind> job | fg |

  `<kind>` reads Test Sleep, Backtest, Validation, Model Search, Training or Trading
  (host/labels.py), and `<reason>` is the auto-kill reason code in Title Case
  (`clock_skew` reads Clock Skew).
  Job events count only when they name a worker. Online/offline and temperature
  crossings are not stored; the page derives them by comparing polls.

## Step 9 additions (stocks on Alpaca, docs/ALPACA.md "Step 9")

Worker routes (worker bearer; the stock_trade ones also take the job's lease token,
like `/api/v1/trade/state`), in `host/api/stocks.py`:
- `GET /api/v1/data/stock_bars`: every instrument's daily bars, New York dates, oldest
  first (`{"generated_at", "symbols": {"SPY": [["2016-01-04", o, h, l, c, v], ...]}}`),
  with an ETag and 304 on `If-None-Match` like `/api/v1/data/games`.
- `GET /api/v1/stock_trade/state?job_id=...`: one held stock_trade job's tick: kill, the
  assignment, the model, positions, open orders, the broker clock, the decision (due,
  session date, reference prices, bars through) and the two cadence settings.
- `POST /api/v1/stock_orders/request`: one session's batch; each order approved or
  rejected with a reason code (200 either way), idempotent per (assignment,
  client_request_id), and the session marked decided even when every order is rejected.
- `POST /api/v1/stock_trade/release` `{"job_ids"}`: before a role change, the approved
  orders of those jobs' assignments are cancelled and the jobs handed back.

Owner routes under `/api/stocks` (owner login, Origin check): `summary`, `models`,
`models/{id}/retire`, `assignments` (GET, POST), `assignments/{id}/halt`, `/resume`,
`/liquidate` (sell all of a halted assignment at the close), `/close`, and `jobs`
(stock_search, stock_backtest, stock_validate with host-filled params).
