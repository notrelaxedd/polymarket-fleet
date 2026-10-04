# Live trading (step 5)

Real money sits behind three gates that all live on the host: the typed live switch, a
lineage's `live_eligible` status (backtest and paper thresholds, no override), and the
exchange process's authenticated session. Workers never see keys and never talk to the
exchange. Everything below is built against an UNVERIFIED picture of the Polymarket US
API (docs unreachable from the build sandbox): every path, header name, signing rule and
field name is configurable in `settings.market_source_config.polymarket_us.auth` and
`.live`, and two probes return raw payloads so the owner can paste them back for fixes.

## Credentials and the exchange process

- `exchange.env` (root-only on the host OS, loaded only by the `exchange` compose service)
  holds `POLYMARKET_US_API_KEY` and `POLYMARKET_US_API_SECRET` (the Ed25519 private key
  seed, base64 or hex, auto-detected) and optionally `POLYMARKET_US_PASSPHRASE`.
  `host/exchange/credentials.py: load() -> Credentials | None` reads them once at start;
  the values are never logged, never written to the database, never returned by any route
  or CLI (probes show `key_present: true` and the key's last 4 characters only).
- `fleet-host` never loads them and has no code path that could.
- Signing (`host/exchange/adapters/signing.py`): `message = template.format(timestamp,
  method, path, body)` with `template` default `"{timestamp}{method}{path}{body}"`,
  `timestamp` in ms (`"ms"`) or seconds, body = the exact JSON bytes sent (`""` for GET);
  signature = Ed25519 over the UTF-8 message, encoded base64 (or hex per config); headers
  `X-PM-Access-Key`, `X-PM-Signature`, `X-PM-Timestamp` (+ passphrase header when set).
  Deterministic: a fixed seed and message give a fixed signature (tested).
- Clock: every authenticated response's `Date` header (or a configured server-time field)
  updates `exchange_state.clock_skew_ms`; skew above `auto_kill.clock_skew_ms` (30 000)
  stops signing and triggers an auto-kill.

## Live gateway (`host/exchange/adapters/polymarket_us_live.py`)

`LiveGateway(OrderGateway)` over the configured paths (`.live`), defaults:
`base_url https://api.polymarket.us`, `place POST /v1/orders`, `cancel DELETE
/v1/orders/{order_id}`, `cancel_all null` (list-open-then-cancel-each when null), `open GET
/v1/orders/open`, `order GET /v1/order/{order_id}`, `fills GET /v1/fills?since=...`,
`balance GET /v1/balance`. Requests: `place` sends `{client_order_id:
orders.client_request_id, market_id: market_ref, side: "BUY", price, size, time_in_force:
"GTD", expires_at: gtd_at}` (field names configurable; `live.client_id_field` picks the
row column sent as the client id, `client_request_id` by default because that is what the
executor and the audit reconcile on); returns the exchange order id. The executor attaches
`markets.market_ref` to the row it hands over (the gateway has no database access). Responses are parsed defensively with
configurable field names; unknown shapes raise `SourceError` with the truncated payload in
the message (logged at DEBUG), 401/403 raise `AuthError`, 429 raises `RateLimited`. Every
call takes a token from the fleet-wide limiter by category (`orders`, `cancels`,
`account`). `probe_account()` performs the balance call and returns status + raw payload
(truncated, the key and passphrase replaced by `***<hint>`); it is what the exchange
container's `probe-account` CLI prints. The host's `POST /api/exchange/probe-account` holds
no key, so it answers with what the exchange process recorded (`payload: null`) and names
that CLI command.

## Authentication probe and auto-kill

The exchange loop task `auth` runs every `auth_probe_interval_s` (300) and at start:
`balance()` -> `exchange_state.auth_ok, balance_cents, buying_power_cents,
auth_checked_at, credentials_present`. A failure increments `auth_failures` and records
`last_auth_error`; success resets it. Auto-kill (`host/kill.auto_kill(conn, reason,
detail)`: `set_kill` with actor `auto:<reason>` and an `auto_kill` audit row; the top bar
shows the reason until reset) fires on: `auth_failures` reaching `auto_kill.auth_failures`
(3); clock skew over the limit (`clock_skew`); an open order on the exchange that is not
ours (open-order audit, `unknown_order`); a fill for a client id we do not know
(`unknown_fill`); an ambiguous reconciliation (two remote orders for one client id,
`ambiguous_reconciliation`). A trigger fires only while the kill switch is off; a condition
that persists after the reset kills again on the next pass. Reset is the normal `RESUME`;
live stays off until re-enabled.

## Live switch

- `POST /live` (owner, JSON; the Settings form posts the same phrase to
  `POST /settings/live`, which re-renders the page with the error inline): body
  `{"confirm": "ENABLE LIVE TRADING YYYY-MM-DD"}` with today's date in the owner's time
  zone, exact match. Requires kill
  off, credentials present, `auth_ok` with `auth_checked_at` within 10 minutes, and
  clock skew within limits. Sets `live_enabled=true`, audit `live_on` with the
  confirmation text and the actor. The form shows the exact phrase to type.
- `POST /live/off` (owner; the Settings "Disable live" button posts to
  `POST /settings/live/off`): immediate; `live_enabled=false`, live assignments halted,
  live orders cancelled through the exchange, audit `live_off`.
- `live_enabled` can no longer be written through `/api/settings` (400: use /live). The
  kill switch and the live daily-loss trip also turn it off. Default at install: off.
- Top bar: `LIVE` pill green only while on; `PAPER` otherwise.

## Live approvals (additions to docs/TRADING.md)

Check `mode` requires `live_enabled`, lineage `live_eligible`, `auth_ok`; check
`buying_power` requires `exchange_state.buying_power_cents` fresher than
`buying_power_max_age_s` (300) and `cost + reserved live cents <= buying_power`; a stale
or missing figure rejects with `buying_power`. Live assignments: creation requires the
same three gates; turning live off or demoting a lineage halts them.

## Executor live path (`host/exchange/executor.py`, `host/exchange/live_sync.py`)

- `submitting -> gateway.place -> open` with the exchange order id. A timeout leaves
  `submitting`; reconciliation every tick: `open_orders()` by client id -> `open`
  (adopt the exchange id); else `fills(since submitted_at - 60 s)` by client id -> record
  the fills (status `filled`/`partial`); else after `submitting_grace_s` (60) -> `expired`
  with the ledger release. Never resubmit blind.
- Fills task every `live_fills_poll_s` (2) while live orders are active: `fills(since last
  seen - 60 s)` -> `orders.record_fill` with `exchange_fill_id` idempotency; a fill whose
  client id is unknown -> auto-kill `unknown_fill`.
- Open-order audit every `open_orders_audit_s` (60) and at start: remote open orders not
  in our active set -> cancel them and auto-kill `unknown_order`; our active orders missing
  remotely -> check fills, then `cancelled`/`filled`/`expired` accordingly with release.
- Cancels: `cancel_requested -> gateway.cancel` with retry 1, 2, 4, 8 s forever until
  `open_orders()` no longer lists it; `cancel_all` = list-open-then-cancel-each unless a
  `cancel_all` path is configured.
- Startup: with credentials, the loop runs auth, reconciliation and the open-order audit
  before submitting anything.

## Smoke order

`python -m host.exchange.cli exchange-smoke --confirm "SMOKE YYYY-MM-DD" [--market <id>]`
(today's date, owner tz): requires `live_enabled`, `auth_ok`, kill off. Picks the given
market or the most liquid confirmed, unresolved live-tradable market; creates an `orders`
row `kind='smoke'`, `mode='live'`, size 1 (the market's `min_size`), limit price `best_bid
- 0.05` (floored at 0.01, so it rests and never fills), no assignment and no bankroll;
`limits.approve_smoke` applies kill, price band, max bet, auth and buying power; the row
goes through the normal outbox; after `smoke_hold_seconds` (10) the CLI cancels it and
prints the timeline (approved, submitting, open with the exchange id, cancel requested,
cancelled). The order is visible on `/trading` flagged `smoke`. It proves keys, signing,
placement and cancellation without giving any model real money.

## Direct cancel-all

`python -m host.exchange.cli cancel-all --direct` (run as `docker compose run --rm exchange
...` even when the exchange service is stopped): loads the credentials, lists open orders
on the exchange, cancels each with retry, then marks the matching database rows
`cancelled` with their ledger releases and prints what it did. The manual fallback for
"exchange process down with live orders resting". Without `--direct` it is the step 4
database cancel-all.

## Dashboard additions

- Settings, "Live trading" group: state (on/off, since when, by whom), the typed enable
  form (phrase shown as a hint with today's date), the "Disable live" button, credentials
  present (yes/no), auth status and age, balance and buying power, clock skew, the last
  auth error, auto-kill reasons from the audit log.
- Top bar: `LIVE` pill (green) when on; the killed bar shows an auto-kill reason when the
  kill was automatic.
- `/trading`: exchange box gains auth, balance, buying power, open live orders; smoke
  orders are flagged; live assignments show mode `live` in a distinct colour.

## Tests that must exist

`tests/test_live_switch.py`: default_off_after_install; enable_requires_exact_dated_phrase
(yesterday and tomorrow fail, wrong wording fails, whitespace fails); enable_requires_fresh_auth;
enable_refused_while_killed; enable_refused_without_credentials; off_is_immediate_and_halts_live;
settings_api_cannot_set_live_enabled; kill_turns_live_off; audit_rows_live_on_off;
dashboard_form_and_pill.
`tests/test_live_gateway.py`: signing_is_deterministic_for_a_known_seed; headers_and_timestamp_formats;
body_bytes_are_what_is_signed; request_building_for_every_call_uses_config_paths;
responses_parsed_from_fixtures_and_malformed_payloads_raise_source_error;
401_raises_auth_error_429_raises_rate_limited; probe_redacts_the_key; credentials_loader_formats.
`tests/test_live_executor.py` (FakeLiveGateway with scripted responses, timeouts, 429s,
unknown orders): place_ok_opens_with_exchange_id; place_timeout_reconciled_from_open_orders;
place_timeout_reconciled_from_fills; place_timeout_expires_after_grace_with_release;
never_resubmits_blind; fills_poll_is_idempotent; unknown_fill_auto_kills; open_order_audit_unknown_remote_cancels_and_auto_kills;
missing_remote_order_closed_from_fills; cancel_retries_until_confirmed; cancel_all_direct_cancels_everything_and_updates_rows;
auth_failures_auto_kill_after_three_and_reset_on_success; clock_skew_auto_kills_and_stops_signing;
buying_power_stale_rejects; buying_power_counts_reserved_live; startup_runs_auth_reconcile_audit_first;
live_off_cancels_live_orders_through_gateway; auto_kill_reason_shown_until_reset.
`tests/test_smoke.py`: smoke_requires_live_auth_and_phrase; smoke_rests_below_bid_and_is_cancelled_after_hold;
smoke_touches_no_bankroll; smoke_goes_through_max_bet_and_kill.
Plus the e2e extension (fake live gateway injected into the exchange loop).
