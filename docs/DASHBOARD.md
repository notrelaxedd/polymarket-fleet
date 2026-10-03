# Dashboard spec (step 2, step 3 additions marked)

Very simple and clean. Phone first. No framework, no build step, no external assets
(the dashboard is tailnet-only and phones may be offline from the public internet).

## Mechanics
- Server-rendered Jinja2 templates under `host/templates/` (`base.html` + one per page +
  fragments). Autoescape on. One stylesheet `host/static/style.css` (CSS variables, system
  font stack, light and dark via `prefers-color-scheme`), one script `host/static/app.js`
  (vanilla, under ~120 lines). Static files served by the app at `/static/`.
- Every state-changing action is a plain HTML form (POST, redirect back, flash message in
  a short-lived cookie that the next page shows once). JavaScript only adds: auto-submit
  of the role select on change, `confirm()` on the KILL button, the periodic fragment
  refresh, hiding the flash after 8 s and a re-fetch when a page comes back from the
  back-forward cache. Everything works with JavaScript off.
- Refresh: `app.js` replaces `#fleet-grid` with `GET /fragments/fleet` every 5 s and
  `#topbar-status` with `GET /fragments/topbar` every 10 s, skipping a refresh while an
  input inside has focus, while a select inside has had focus for less than 15 s (a
  select keeps focus after its picker is dismissed, so the hold is bounded), or while a
  form is submitting. A line under the sticky top bar shows "updated N s ago" measured
  from the fleet grid's own last successful fetch (so a held grid reads as old), or
  "connection lost" if a fetch fails; while lost the grid is dimmed and every status dot
  turns grey, because the server-rendered colours would otherwise claim health.
- Owner auth is the same as the API (`Tailscale-User-Login` header, Origin check on POST).
  401, 403, 404, 405, 413 render a small HTML page explaining the cause. Every page is
  sent with anti-framing headers and `Cache-Control: no-store`.
- Money: stored in cents, displayed as `$12.50`, entered in dollars (server converts;
  `1,500` is a thousands group, `1,5` is refused, one billion dollars is the ceiling).
- Timestamps are shown in the owner's `tz` setting with the zone abbreviation.

## Layout
- Top bar (sticky): wordmark "Fleet"; nav: Fleet, Jobs, Models (changed: step 3 enables
  it), Trading (disabled, labelled "step 4"), Settings; status cluster: mode pill
  (`PAPER` grey or `LIVE` green from `settings.live_enabled`), P&L "today $0.00 · all
  $0.00" (from `/api/pnl`), red KILL button. When `kill_switch` is true the whole bar
  turns red, the pill and the P&L stay, the KILL button is replaced by a disabled
  "KILLED" chip and the text "TRADING KILLED. Reset in Settings." links to the reset
  form (on a phone it takes its own line under the status cluster).
- Width under 700 px: single column, nav collapses to a row of four icons/labels, tap
  targets at least 44 px, no horizontal scroll.

## Fleet page `/`
Grid of worker cards (`auto-fill, minmax(280px, 1fr)`), sorted by name. Each card:
- Status dot (green online < 15 s, amber stale 15-60 s, grey offline > 60 s, announced
  through `aria-label`), name, a text chip "stale" or "offline" when not online (colour
  alone is not the signal; offline cards are dimmed), small id and hostname.
- Role `<select>` with idle / backtest / model_search / train / trade and a Set button
  (hidden when JS auto-submits). While `switching` is true a line reads "switching to
  backtest (epoch 7)…" until the ack; the select is disabled only while the worker is
  online (the ack takes seconds). A stale or offline worker keeps its select enabled so a
  wrong pick can be undone before it comes back; a newer choice simply supersedes the
  older one. Disabled workers show a "disabled" chip and an Enable link; enabled ones a
  small Disable link.
- Current job: kind + progress bar (`role="progressbar"` with numeric `aria-valuenow`)
  with percent (or "no job"). Link to `/jobs/{id}`.
- Stats line: `CPU 12% · RAM 1.2 / 7.7 GB · 3 s ago · v a1b2c3d4e5f6`.
- Today's P&L: `$0.00` (muted until trading exists).
Empty state: "No workers yet. Mint an enroll token in Settings and run the install line on
a Debian box."

## Jobs page `/jobs`
- (changed: step 3) Three send cards, one per kind, each a `<details>` card holding its
  own form with a target select (Any idle worker, then each worker by name) and a Send
  button: Backtest (model select with a "(none: use family + params)" option, family
  select, params JSON textarea, first and last season), Model search (family, candidates,
  seed, first and last season, keep top), Train (model select, through season and week;
  the button is disabled until a model exists). (changed: review) Only one card is open:
  the backtest card by default, the train card behind `?train_model`, the card whose form
  was just rejected; the others fold to their heading so the job list sits near the top
  on a phone. The sleep test form is the same kind of card under them. Model select
  labels are "K 24 · HFA 55 · MOV on · thru 2024 w18 · 8f173b7b" (or "untrained"), with
  the family prefixed only when more than one family exists. Defaults come from settings
  (`backtest_seasons`, last complete season for the train point) and `?train_model=<id>`
  preselects that model in the train form (the Train buttons on the Models page link
  there). A rejected form re-renders the page with the error inline in the posted form
  (status 400) and keeps the submitted values.
- Table (newest first, 50 rows): created, kind, status badge (queued / leased / cancel
  requested / succeeded / failed / cancelled, colours muted), worker, progress, Cancel
  button for queued and leased. "waiting for an idle worker" badge on untargeted queued jobs.
- `/jobs/{id}`: params, status, worker, progress, checkpoint (a one-line digest such as "candidate 12, season index 3; 12 evaluated", the raw JSON behind a collapsed "raw checkpoint" element: a search checkpoint carries the whole top list), result or
  error, events timeline (ts, event, worker, detail). (changed: step 3) A `params.model_id`
  links to the model; a backtest result renders its whole-run metrics as labelled pairs
  (games, bets, ROI, hit rate, avg edge, P&L, log-loss vs market, max drawdown with one
  decimal, seasons; ROI, hit rate and avg edge read "-" when nothing was bet) and the
  per-season table stacks on a phone like the leaderboard; a model search result renders
  the top list as a stacked table (candidate params, shrunk ROI as a signed percent, ROI,
  bets, log-loss, market, drawdown, the created model link); any `created_models` are
  shown as buttons linking to `/models/{id}` ("(existing)" when the row was already
  there); the raw JSON sits in a collapsed "raw result" element.

## Models page `/models` (step 3)
One row per lineage from `GET /api/models`, ranked list first (`#1`, `#2`, ...), then an
"Unranked" section (fewer than 50 bets, no backtest yet, or retired). Each row: status
badge (`candidate` grey, `paper_ok` green, `live_eligible` blue, `retired` muted), family
in bold with the short params ("K 24 · HFA 55 · MOV on"), a "N rows" chip when the
lineage has children, ROI (signed percent, "-" without bets), bets, log-loss "vs" the
market's, max drawdown (one decimal, "-" when null), seasons span, the summary text with
an "Edit summary" `<details>` holding a textarea (maxlength 600) and Save, a Train
button (links to the jobs page with the train form prefilled) and a disabled Assign
button labelled "step 4" (dashed, full-contrast muted text). When no lineage is ranked
the page says so in one line instead of drawing an empty table: "No lineage has 50
backtest bets yet, so none is ranked." above the unranked list, or "No models yet. Send
a model search from the Jobs page." when there are none. On a phone the
row stacks: the name line first, the metrics as labelled pairs, the summary on its own
line and the two buttons side by side at 44 px. The page ends with the nflverse
attribution line (CC BY 4.0, links to nflverse-data and the licence).

`/models/{id}`: family, short params and status badge in the heading; id, lineage (root
chip), parent link, trained-through point, created time with the creating job link,
shrunk ROI (signed percent); Train / Assign (disabled) / "Retire lineage" (a form with a
JavaScript confirm, hidden once retired); the summary with its edit form folded into an
"Edit summary" `<details>` like the list; params as JSON; the backtest metrics as
labelled pairs (games, bets, ROI, hit rate, average edge, P&L, log-loss vs market, brier,
max drawdown with its cents, seasons), the stacked per-season table and the calibration
table (ten `p`
buckets: games, mean p, mean outcome); the lineage members (id, trained through, status,
created, job) and the related jobs (created, kind, status, "created this model" or "ran
against it").

## Settings page `/settings`
Groups, each its own form with a Save button and inline validation errors. Every field is
a text input with `inputmode="decimal"` (dollars, fractions) or `inputmode="numeric"`
(integers) so a phone opens the number keyboard while a blank value and server-side
validation keep working:
- Trading limits (dollars): max bet, max daily loss paper, max daily loss live, default
  bankroll per game, liquidity floor; plus min edge, Kelly fraction, max games per trade
  worker. These are the owner's editable limits; the seeded values are test defaults.
- Fleet: lease seconds, heartbeat seconds, online-after seconds, max lease expiries.
  Checked together: lease >= 2 x heartbeat + 5 and online-after > heartbeat.
- Time zone (IANA name).
- (changed: step 3) Fee model (taker fee rate, half spread), Backtest thresholds (min bets,
  min ROI, max drawdown; saving recomputes every lineage's status), Backtest seasons
  (first season, last season blank = last complete), nflverse games (refresh hours, the
  games.csv URL, the row count and last complete season in the heading, a line with the
  last refresh outcome of this host process: time and counts including skipped records,
  or "Last refresh failed <time>: <error>" in red, and a "Refresh now" button that
  fetches immediately and reports the counts in the flash; a failed refresh is a flash
  too, never an error page).
- Enroll: "New enroll token" button; the response page shows the token once with the two
  install one-liners (argument form and `FLEET_ENROLL_TOKEN` form) and a Copy button.
- Kill switch: state, and when killed a reset form with a text field that must contain
  exactly `RESUME` (no surrounding whitespace, like the API).
- Audit log: last 20 rows (time, actor, action, entity, confirmation text).
