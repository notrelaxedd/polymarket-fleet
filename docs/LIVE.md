# Live trading (step 5)

Real money sits behind three gates that all live on the host: the typed live switch, a
lineage's `live_eligible` status (backtest and paper thresholds, no override), and the
exchange process's authenticated session. Workers never see keys and never talk to the
exchange. Everything below is built against an UNVERIFIED picture of the Polymarket US
API (docs unreachable from the build sandbox): every path, header name, signing rule and
field name is configurable in `settings.market_source_config.polymarket_us.auth` and
`.live`, and two probes return raw payloads so the owner can paste them back for fixes.

## Credentials and the exchange process

- `exchange.env` (readable only by the user who runs `docker compose`, `chmod 600`;
  git-ignored together with every `*.env` but `.env.example`; loaded only by the
  `exchange` compose service, never by `host`, so the keys never go in `.env`)
  holds `POLYMARKET_US_API_KEY` and `POLYMARKET_US_API_SECRET` (the Ed25519 private key
  seed, base64 or hex, auto-detected) and optionally `POLYMARKET_US_PASSPHRASE`.
  `host/exchange/credentials.py: load() -> Credentials | None` reads them once at start;
  the values are never logged, never written to the database, never returned by any route
  or CLI (probes show `key_present: true` and the key's last 4 characters only). A present
  but malformed secret raises `CredentialsError` whose message never carries the secret;
  the exchange records it as `exchange_state.last_auth_error` ("credentials malformed:
  ...") and the CLI commands print "secret malformed: ..." (exit 1), so the owner can tell
  a bad format from a missing file.
- `fleet-host` never loads them and has no code path that could.
- What the database may decide is pinned (`host/exchange/adapters/live_policy.py`, stdlib
  only, enforced both by the settings write and by the gateway at request time): the
  live `base_url` must be https on `polymarket.us` or a subdomain (another https host
  only through `POLYMARKET_US_LIVE_BASE_URL` in `exchange.env`, which only the exchange
  process reads), and the signing `template` must hold each of `{timestamp}`,
  `{method}`, `{path}` and `{body}` exactly once with nothing else but a few separator
  characters. `POST /api/settings` rejects anything else (400 naming the key), and a
  gateway built from a bad row refuses to sign and reports it as the auth error.
- Signing (`host/exchange/adapters/signing.py`): `message = template.format(timestamp,
  method, path, body)` with `template` default `"{timestamp}{method}{path}{body}"`,
  `timestamp` in ms (`"ms"`) or seconds, body = the exact JSON bytes sent (`""` for GET);
  signature = Ed25519 over the UTF-8 message, encoded base64 (or hex per config); headers
  `X-PM-Access-Key`, `X-PM-Signature`, `X-PM-Timestamp` (+ passphrase header when set).
  Deterministic: a fixed seed and message give a fixed signature (tested).
- Clock: every authenticated response's `Date` header (or the body field named by
  `auth.server_time_field`; no other body field is ever read as server time) updates
  `exchange_state.clock_skew_ms`. Skew above `auto_kill.clock_skew_ms` (30 000), whether
  the auth probe or any other live answer measured it, triggers an auto-kill and pauses
  live placements only (`ExchangeLoop.live_paused`, `Executor.live_blocked`; the gateway
  itself refuses to sign a place over `max_skew_ms`): cancels, the open-order audit and
  the fills poll keep running, so the kill's cancels reach the exchange instead of
  resting until GTD. The first live answer with the skew back in range clears the pause.

## Live gateway (`host/exchange/adapters/polymarket_us_live.py`)

`LiveGateway(OrderGateway)` over the configured paths (`.live`), defaults:
`base_url https://api.polymarket.us`, `place POST /v1/orders`, `cancel DELETE
/v1/orders/{order_id}`, `cancel_all null` (list-open-then-cancel-each when null), `open GET
/v1/orders/open`, `order GET /v1/order/{order_id}`, `fills GET /v1/fills?since=...`,
`balance GET /v1/balance`. Requests: `place` sends `{client_order_id:
orders.client_request_id, market_id: market_ref, side, price, size, time_in_force:
"GTD", expires_at: gtd_at}` where `side` is `live.side_buy` (default `"BUY"`) for a buy order
and `live.side_sell` (default `"SELL"`) for a sell order (step 6 Part B, docs/TRADING.md
"Selling"; a null `side_buy` or `side_sell` raises `NotConfigured` before any request
is sent) (field names configurable; `live.client_id_field` picks the
row column sent as the client id, `client_request_id` by default because that is what the
executor and the audit reconcile on); returns the exchange order id. The executor attaches
`markets.market_ref` to the row it hands over (the gateway has no database access). Every nested config block
is deep-merged over the defaults, so renaming one request field keeps the others (a
null drops `expires_at`; a null required field makes `place` raise `NotConfigured`, which
the executor records as `rejected_by_exchange` with the reason). Responses are parsed
defensively with configurable field names; an empty listing in any shape (`[]`, `{}`,
`{"orders": null}`, a null or empty body) is an empty list; unknown shapes raise
`SourceError` with the truncated payload in the message (logged at DEBUG), a fill with
a fractional size is refused the same way (never truncated), a fill's `fee` is in
`money_unit` while `fee_cents` is always cents; 401/403 raise `AuthError`, 429 raises
`RateLimited`. Every message built from a response body (unknown shapes, 4xx, 429) goes
through the key and passphrase redaction before it is raised, logged or stored. Every
call takes a token from the fleet-wide limiter by category (`orders`, `cancels`,
`account`); the executor asks the limiter whether an order token is due within
`limiter_wait_s` before marking a row `submitting`, otherwise the row stays `approved`
for the next tick. `probe_account()` performs the balance call and returns status + raw
payload (truncated, the key and passphrase replaced by `***<hint>`); it is what the
exchange container's `probe-account` CLI prints. The host's `POST /api/exchange/probe-account` holds
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
`ambiguous_reconciliation`); a fill the ledger can no longer book because its order is
already closed here, or exceeds what was open (`late_fill`: real money the books do not
show, booked by hand). A trigger fires only while the kill switch is off; a condition
that persists after the reset kills again on the next pass. Reset is the normal `RESUME`;
live stays off until re-enabled. The Settings page shows a one-line recovery per reason
(`host/views.py: AUTO_KILL_REMEDIES`, the same lines as the README).

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

Check `mode` requires `live_enabled`, lineage `live_eligible`, `auth_ok` and a market on
the live platform (`host/trading/live.py: live_platform`: the current `market_source`,
never `polymarket_clob`, which is a price source only; a market left over from another
source is never traded live); check `buying_power` requires
`exchange_state.buying_power_cents` fresher than `buying_power_max_age_s` (300) and
`cost + reserved live cents + cents live fills spent since balance_checked_at <=
buying_power` (a fill's cash has left the reservation but not yet the probed figure); a
stale or missing figure rejects with `buying_power`. Live assignments: creation (and
re-activation) requires the same gates plus a confirmed open market of the game on the
live platform, and takes the live approval lock first, so one can never commit beside a
live-off that did not see it. Turning live off halts them; so does every demotion out of
`live_eligible` (the paper gate, a thresholds change, a new backtest result, a
retirement: `eligibility.recompute_lineage` and `models.retire` halt the lineage's
assignments, which cancels their orders).

## Executor live path (`host/exchange/executor.py`, `host/exchange/live_sync.py`)

- Money rule: a live row is closed (its reservation released) only after its fills were
  read. `live_sync.fetch_fills` returns None when the fills call fails (429, timeout,
  5xx), and every closing path (the audit, the submitting grace, the cancel
  confirmation, the direct cancel-all) then leaves its rows active for that pass and
  reports `fills unavailable` as the task's error; the next pass retries.
- `submitting -> gateway.place -> open` with the exchange order id. A timeout leaves
  `submitting`; reconciliation every tick: `open_orders()` by client id -> `open`
  (adopt the exchange id); else `fills(since submitted_at - 60 s)` by client id -> record
  the fills (status `filled`/`partial`); else after `submitting_grace_s` (60) -> `expired`
  with the ledger release. Never resubmit blind. A row that changed under a place in
  flight (a kill, cancel-all --direct) has the order that reached the exchange cancelled
  there at once, whatever the row's status, then confirmed like any other cancel.
- Fills task every `live_fills_poll_s` (2) while live orders are active and for 120 s
  after the last one closed: `fills(since the earliest submission - 60 s)` ->
  `orders.record_fill` with `exchange_fill_id` idempotency; a fill whose client id is
  unknown -> auto-kill `unknown_fill`; a fill the ledger cannot book (its order already
  closed, or over its open size) -> auto-kill `late_fill`.
- Open-order audit every `open_orders_audit_s` (60) and at start: remote open orders not
  in our active set -> cancel them and auto-kill `unknown_order`; our active orders missing
  remotely -> read fills, then `cancelled`/`filled`/`expired` accordingly with release.
- Cancels: `cancel_requested -> gateway.cancel` with retry 1, 2, 4, 8 s forever until
  `open_orders()` no longer lists it, its last fills absorbed before the release; the row
  closes as `expired` when its `gtd_at` has passed, else `cancelled`. `cancel_all` =
  list-open-then-cancel-each unless a `cancel_all` path is configured.
- Expiry: a live `open`/`partial` row past `gtd_at` becomes `cancel_requested` (reason
  `gtd`) and goes through the same cancel confirmation (the exchange may have filled it
  in its last second), closing as `expired`; paper rows expire at once with the release.
- Startup: with credentials, the loop runs auth, reconciliation and the open-order audit
  before submitting anything, also while placements are paused by clock skew.

## Smoke order

`python -m host.exchange.cli exchange-smoke --confirm "SMOKE YYYY-MM-DD" [--market <id>]
[--hold S] [--drive]` (today's date in the Settings time zone): requires `live_enabled`,
`auth_ok`, kill off. Picks the given market (which must be on the live platform) or the
most liquid confirmed, unresolved market on the live platform (the current
`market_source`, never `polymarket_clob`) with a snapshot no older than `book_max_age_s`;
creates an `orders` row `kind='smoke'`, `mode='live'`, size 1 (the market's `min_size`),
limit price `best_bid - 0.05` (floored at 0.01, so it rests and never fills), no
assignment and no bankroll; `limits.approve_smoke` applies kill, price band, max bet,
auth (switch, `auth_ok`, platform) and buying power; the row goes through the normal
outbox (`--drive` runs the executor from the CLI process when the exchange service is
stopped); after `smoke_hold_seconds` (10, or `--hold`) the CLI cancels it and prints the
timeline (approved, submitting, open with the exchange id, cancel requested, cancelled).
A row that never reached `open` within 30 s is cancelled before the CLI returns
(approved rows at once, a submitting or open one through the exchange, reason "smoke not
confirmed in time"), so a later exchange start can never submit a stale-priced smoke
order; the CLI then exits 1 and the owner checks the row on `/trading`. The order is
visible on `/trading` flagged `smoke`. It proves keys, signing, placement and
cancellation without giving any model real money.

## Direct cancel-all

`python -m host.exchange.cli cancel-all --direct` (run as `docker compose run --rm exchange
...` even when the exchange service is stopped): loads the credentials, first turns live
off through `kill.live_off` (live assignments halted, `approved` live rows cancelled with
their release so a restart cannot submit them, the rest `cancel_requested`), then lists
open orders on the exchange, cancels each with retry, reads the fills and closes the
rows the exchange no longer lists (`cancelled`, or `expired` past gtd, with their ledger
releases) and prints what it did (audit `cancel_all` with `direct: true`, `live_was_on`,
`assignments_halted`, `approved_cancelled`, `rows_closed`, `left_for_exchange`, `error`).
A `submitting` row never seen on the exchange stays `cancel_requested` for the exchange
process to confirm at its next start (its place may be in flight). When the fills call
fails nothing is closed, the rows stay `cancel_requested` and the command exits 1. The
manual fallback for "exchange process down with live orders resting". Without `--direct`
it is the step 4 database cancel-all.

## Dashboard additions

- Settings, "Live trading" group: state (on/off, since when, by whom), the typed enable
  form (phrase shown as a hint with today's date), the "Disable live" button, credentials
  present (yes/no; "exchange.env missing or malformed (see the last auth error)" when
  not), auth status and age, balance and buying power, clock skew, the last auth error,
  auto-kill reasons from the audit log each with its one-line recovery; the kill card
  names the recovery for the newest automatic kill.
- Top bar: `LIVE` pill (green) when on; the killed bar shows an auto-kill reason when the
  kill was automatic. The live P&L segment shows while live is on and, with live off, as
  long as real money is still in play (an active live order, a live assignment not yet
  settled, cash reserved or in open live positions).
- `/trading`: exchange box gains auth, balance, buying power, live orders as "N open, M
  cancel pending" and, when the exchange is DOWN with live orders active, the direct
  cancel-all command (press KILL first); smoke orders are flagged; live assignments show
  mode `live` in a distinct colour.

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
`tests/test_review5_fixes.py` (the review regressions): expiry_absorbs_a_fill_that_landed_before_gtd;
failed_fills_call_closes_nothing; direct_cancel_all_with_fills_down_leaves_rows_for_the_exchange;
late_fill_for_a_closed_row_auto_kills; settings_cannot_redirect_or_forge_the_signed_request;
skew_seen_on_any_live_answer_pauses_and_resumes; gateway_skew_guard_is_wired_and_refuses_places_only;
backtest_demotion_and_retirement_halt_live_assignments; unknown_shape_and_429_messages_are_redacted;
direct_cancel_all_cancels_approved_rows_and_turns_live_off; direct_cancel_all_keeps_an_in_flight_place_cancellable;
buying_power_subtracts_fills_since_the_probe; live_assignment_cannot_slip_past_live_off;
executor_leaves_a_row_approved_when_no_order_token_is_due; empty_listings_and_fill_shapes;
request_fields_merge_and_server_time_field; malformed_secret_is_named_not_hidden;
smoke_cancels_its_row_when_never_confirmed; live_orders_stay_on_the_live_platform;
exchange_box_counts_pending_cancels_and_names_the_direct_command; settings_shows_a_remedy_per_auto_kill_reason;
secret_files_are_ignored_by_git_and_docker.
Plus the e2e extension (fake live gateway injected into the exchange loop).
