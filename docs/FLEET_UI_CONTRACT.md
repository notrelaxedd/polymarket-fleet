# Fleet UI contract (build-time coordination; folded into PROTOCOL.md and DASHBOARD.md)

The 3D control page (`fleet-ui/`, served at `/fleet`) talks only to these owner routes.
All are behind the existing owner auth (`Tailscale-User-Login`, worker-IP refusal,
Origin check on POST). Errors are `{"detail": "..."}`.

## Worker -> host additions

Register body (optional fields): `can_reboot: bool`, `boot_media: "flash"|"ssd"|"hdd"|"unknown"`.

Heartbeat body (optional, null when unknown):
- `temp_c`: float, finite, -50..150
- `boot_media`: `"flash" | "ssd" | "hdd" | "unknown"`
- `wear_pct`: float, finite, 0..100 (percent of rated life used)
- `disk_gb_written`: float, finite, >= 0 (GB written to the boot disk since boot)

Heartbeat reply adds `reboot`: the pending request id (string) or null. A request is
pending while `workers.reboot_id` is set and `reboot_requested_at > now() - 5 minutes`.
While a reboot is pending the heartbeat claims nothing new.

Register: when `reboot_id` is set and the presented `boot_id` differs from the stored
one, the reboot happened: clear `reboot_id` / `reboot_requested_at`, audit `reboot_done`.
Register stores `can_reboot` and `boot_media` when given.

Settings: heartbeat every 3 s (`heartbeat_seconds` 3), offline after 30 s without one
(`online_after_seconds` 30). Migration 0010 moves stored values from the old defaults
(5 and 15) to 3 and 30; any value the owner changed is left alone.

## `GET /api/fleet` (existing, extended)

Top level adds:
- `roles`: `[{"id":"idle","name":"Idle","short":"Idle"}, {"id":"backtest","name":"Backtest","short":"Backtest"}, {"id":"model_search","name":"Model search","short":"Search"}, {"id":"train","name":"Training","short":"Train"}, {"id":"trade","name":"Trading","short":"Trade"}]`
- `online_after_seconds`: int

Each worker adds (existing fields stay: `id, name, online, desired_role, reported_role,
switching, enabled, cpu_pct, ram_used_mb, ram_total_mb, hostname, last_heartbeat_at,
current_jobs[{id, kind, status, progress, game?}]`, ...):
- `ram_pct`: float|null (used/total*100, one decimal)
- `temp_c`, `boot_media`, `wear_pct`, `disk_gb_written` as last reported (null when never)
- `seconds_since_heartbeat`: int|null (database clock)
- `can_reboot`: bool
- `rebooting`: bool (a reboot request is pending, see above)

Workers are ordered by `name, id`. A worker's task is `desired_role`.

## `POST /api/workers/{id}/role` (existing)

Body `{"role": "<role id>"}`. Returns the worker. Leaving `trade` cancels that worker's
open orders first (existing behaviour).

## `POST /api/workers/{id}/reboot` (new)

No body. 200 with `{"worker_id", "reboot_id", "requested_at"}`. Idempotent: a pending
request returns the same id. 404 unknown worker. 409 `worker is offline` when it has
no heartbeat within `online_after_seconds`. 409 `this worker cannot reboot yet: re-run
install.sh on it` when `can_reboot` is false. Writes audit `reboot_requested`
(entity = worker id, actor = owner login).

## `GET /api/fleet/events?since=<ISO-8601>&limit=<1..200>` (new)

Newest first. Without `since`: the newest `limit` (default 20). With `since`: every
event with `ts >= since` (inclusive; the client dedupes by `key`), capped at `limit`.

```json
{"events": [{"key": "a:123", "ts": "2026-10-07T13:00:00+00:00", "worker_id": "w_3f9a1c",
             "who": "box3", "tone": "ok", "text": "Moved to Model search by owner@example.com"}],
 "server_time": "2026-10-07T13:00:01+00:00"}
```

- `key`: `a:<audit_log.id>` or `j:<job_events.id>`, unique and stable.
- `who`: the worker's name, or `fleet` for fleet-wide rows.
- `tone`: `ok` (normal), `hot` (warning: failures, kill, lease expiry), `off` (machine
  down or going down), `fg` (neutral).
- Sources: audit_log actions `set_role`, `auto_role`, `auto_idle`, `set_enabled`,
  `worker_enrolled`, `reboot_requested`, `reboot_done`, `kill`, `auto_kill`, `kill_reset`
  (whatever the kill reset action is named); job_events `claimed`, `succeeded`,
  `failed`, `released`, `lease_expired`, `cancelled` for jobs with a worker.
- Online/offline and temperature crossings are not stored; the page derives them by
  comparing polls.
