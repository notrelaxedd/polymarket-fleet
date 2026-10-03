# Dashboard spec (step 2)

Very simple and clean. Phone first. No framework, no build step, no external assets
(the dashboard is tailnet-only and phones may be offline from the public internet).

## Mechanics
- Server-rendered Jinja2 templates under `host/templates/` (`base.html` + one per page +
  fragments). Autoescape on. One stylesheet `host/static/style.css` (CSS variables, system
  font stack, light and dark via `prefers-color-scheme`), one script `host/static/app.js`
  (vanilla, under ~120 lines). Static files served by the app at `/static/`.
- Every state-changing action is a plain HTML form (POST, redirect back, flash message in
  the query string or a cookie). JavaScript only adds: auto-submit of the role select on
  change, `confirm()` on the KILL button, and the periodic fragment refresh. Everything
  works with JavaScript off.
- Refresh: `app.js` replaces `#fleet-grid` with `GET /fragments/fleet` every 5 s and
  `#topbar-status` with `GET /fragments/topbar` every 10 s, skipping a refresh while any
  select or input inside has focus or a form is submitting. The page shows "updated 3 s
  ago" and "connection lost" if a fetch fails.
- Owner auth is the same as the API (`Tailscale-User-Login` header, Origin check on POST).
  401 and 403 render a small HTML page explaining the cause.
- Money: stored in cents, displayed as `$12.50`, entered in dollars (server converts).

## Layout
- Top bar (sticky): wordmark "Fleet"; nav: Fleet, Jobs, Models (disabled until step 3),
  Trading (disabled until step 4), Settings; status cluster: mode pill (`PAPER` grey or
  `LIVE` green from `settings.live_enabled`), P&L "today $0.00 · all $0.00" (from
  `/api/pnl`), red KILL button. When `kill_switch` is true the whole bar turns red with the
  text "TRADING KILLED. Reset in Settings." and the KILL button is replaced by a disabled
  "KILLED" chip.
- Width under 700 px: single column, nav collapses to a row of four icons/labels, tap
  targets at least 44 px, no horizontal scroll.

## Fleet page `/`
Grid of worker cards (`auto-fill, minmax(280px, 1fr)`), sorted by name. Each card:
- Status dot (green online < 15 s, amber stale 15-60 s, grey offline > 60 s), name,
  small id and hostname.
- Role `<select>` with idle / backtest / model_search / train / trade and a Set button
  (hidden when JS auto-submits). While `switching` is true: the select is disabled and a
  line reads "switching to backtest (epoch 7)…"; it clears on ack. Disabled workers show a
  "disabled" chip and an Enable link; enabled ones a small Disable link.
- Current job: kind + progress bar with percent (or "no job"). Link to `/jobs/{id}`.
- Stats line: `CPU 12% · RAM 1.2 / 7.7 GB · 3 s ago · v a1b2c3d4e5f6`.
- Today's P&L: `$0.00` (muted until trading exists).
Empty state: "No workers yet. Mint an enroll token in Settings and run the install line on
a Debian box."

## Jobs page `/jobs`
- Send form: kind (sleep now; backtest / model_search / train listed but disabled with
  "step 3"), params (for sleep: seconds, default 60), target (Any idle worker, then each
  worker by name), Send.
- Table (newest first, 50 rows): created, kind, status badge (queued / leased / cancel
  requested / succeeded / failed / cancelled, colours muted), worker, progress, Cancel
  button for queued and leased. "waiting for an idle worker" badge on untargeted queued jobs.
- `/jobs/{id}`: params, status, worker, progress, checkpoint (pretty JSON), result or
  error, events timeline (ts, event, worker, detail).

## Settings page `/settings`
Groups, each its own form with a Save button and inline validation errors:
- Trading limits (dollars): max bet, max daily loss paper, max daily loss live, default
  bankroll per game, liquidity floor; plus min edge, Kelly fraction, max games per trade
  worker. These are the owner's editable limits; the seeded values are test defaults.
- Fleet: lease seconds, heartbeat seconds, online-after seconds, max lease expiries.
- Time zone (IANA name).
- Enroll: "New enroll token" button; the response page shows the token once with the two
  install one-liners (argument form and `FLEET_ENROLL_TOKEN` form) and a Copy button.
- Kill switch: state, and when killed a reset form with a text field that must contain
  `RESUME`.
- Audit log: last 20 rows (time, actor, action, entity, confirmation text).
