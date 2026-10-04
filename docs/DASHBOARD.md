# Dashboard spec (step 2, step 3 and step 4 additions marked)

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
- Refresh: `app.js` replaces `#fleet-grid` with `GET /fragments/fleet` every 5 s,
  (step 4) `#trading-live` with `GET /fragments/trading` every 5 s, and
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
  it), Trading (changed: step 4 enables it), Settings; status cluster: mode pill
  (`PAPER` grey or `LIVE` green from `settings.live_enabled`), P&L per mode "paper today
  $0.00 · all $0.00" (from `/api/pnl`; "live today · all" follows it only while
  `live_enabled` is on), red KILL button. (step 4) Two banners sit under the status
  cluster on their own line, each a link with `role="alert"`: a red "EXCHANGE DOWN"
  (`/trading#exchange`) when the exchange heartbeat is missing or older than 15 s while
  any order is active or the kill switch is on, and an amber "N assignments unattended"
  (`/trading#assignments`) when an active assignment's trade job has sat queued for more
  than 60 s. Both refresh with the top bar fragment. When `kill_switch` is true the whole bar
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
  with percent (or "no job"). Link to `/jobs/{id}`. (changed: step 4) A held `trade`
  job has no progress: its line is the link plus "held", one per assignment, and the
  link names the game ("KC @ LV") rather than "trade". Each job sits in its own
  `.jobrow` (flex, no wrap) so a label and its bar never split across lines.
- Stats line: `CPU 12% · RAM 1.2 / 7.7 GB · 3 s ago · v a1b2c3d4e5f6`.
- Today's P&L: `today $1.20`, the worker's share of `/api/pnl` (bets its orders settled
  today plus the mark-to-mid change of its open positions; (changed: step 4) real now).
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
market's, max drawdown (one decimal, "-" when null), seasons span, (changed: step 4) a paper
column "5 g · 30 bets · $27.95 · ROI +6.2% · CLV 0.013" pooled from `model_scores` ("-"
without paper games), the summary text with
an "Edit summary" `<details>` holding a textarea (maxlength 600) and Save, a Train
button (links to the jobs page with the train form prefilled) and an Assign button
(links to `/trading?model=<id>#assign`, which opens the create form with the model
selected; hidden on a retired lineage). (changed: step 4) Ranking follows
docs/TRADING.md: a lineage with at least 5 paper games and 30 paper bets ranks on shrunk
CLV (`avg_clv * bets / (bets + 25)`, ties by paper ROI) ahead of the backtest-ranked
ones and wears a green "paper" chip next to its rank; the rest keep the step 3 order. When no lineage is ranked
the page says so in one line instead of drawing an empty table: "No lineage has 50
backtest bets yet, so none is ranked." above the unranked list, or "No models yet. Send
a model search from the Jobs page." when there are none. On a phone the
row stacks: the name line first, the metrics as labelled pairs, the summary on its own
line and the two buttons side by side at 44 px. The page ends with the nflverse
attribution line (CC BY 4.0, links to nflverse-data and the licence).

`/models/{id}`: family, short params and status badge in the heading; id, lineage (root
chip), parent link, trained-through point, created time with the creating job link,
shrunk ROI (signed percent), (step 4) the paper record line (games, bets, P&L, ROI, CLV,
the shrunk CLV and "ranked on paper" when it is, or "no paper games yet"); Train / Assign
(the same link as the list) / "Retire lineage" (a form with a
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
- (step 4) Trading: one form, `POST /settings/trade`, holding every step 4 key under four
  sub-headings. Order approval: participation, book max age, order lifetime
  (`gtd_seconds`), orphan cancel after, trade tick, max paper models per game, max
  exposure paper and live (dollars, 0 = off), a "Pregame only" checkbox
  (`trade_pregame_only`; unticked sends nothing and stores false). Market source and
  snapshots: `market_source` as a select (sim, polymarket_us, polymarket_clob), market
  lookahead days, snapshot cadence active and idle, snapshot retention days, the scores
  URL, and `market_source_config` as a JSON textarea (must parse to an object). Paper
  thresholds (`thresholds_paper`): min games, min bets, min days, min avg CLV, min P&L in
  dollars; saving them recomputes the paper eligibility of every lineage with paper
  scores. Rate limits: the four per-second numbers. Errors re-render inline like the
  other groups.
- Enroll: "New enroll token" button; the response page shows the token once with the two
  install one-liners (argument form and `FLEET_ENROLL_TOKEN` form) and a Copy button.
- Kill switch: state, and when killed a reset form with a text field that must contain
  exactly `RESUME` (no surrounding whitespace, like the API).
- Audit log: last 20 rows (time, actor, action, entity, confirmation text).

## Trading page `/trading` (step 4)
Phone first like the rest; every table carries `stack`; every action is a form that
redirects back to `/trading` with a flash, and a refused action (409: kill on, game not
final, limit hit) comes back as a flash too, never an error page. The page is:
- A folded "New assignment" `<details>` card (`#assign`) above the live region, open when
  `?model=<id>` prefills it (the Assign buttons) or after a rejected post. Game select
  (upcoming games that have a confirmed market, labelled "KC @ LV · <kickoff in the
  owner's tz> · <game_id>"), model select (models of non-retired lineages, trained models
  first, "elo_blend · K 24 · HFA 55 · MOV on · thru 2024 w18 · 8f173b7b"; a model without
  an artifact is labelled "untrained: mirrors the market, never trades"), mode (paper; live appears only
  while `live_enabled`), bankroll in dollars (default `default_bankroll_cents`), optional
  max bet in dollars. `POST /assignments` calls `create_assignment`; a 400/409 re-renders
  the page with the error inline and the submitted values kept, nothing stored. The
  button is disabled with a one-line reason when no game or no model qualifies.
- `#trading-live`, refreshed every 5 s from `/fragments/trading` (held while a select in
  a link form has focus, like the fleet grid):
  - Assignments (`#assignments`): game (bold "KC @ LV", the id, "final 20-24" once
    final), kickoff, model link, mode chip, status badge (active green, halted amber,
    settled blue), bankroll "avail · reserved · open · realized", open order count, and
    one action: Halt (active), Activate (halted, hidden under kill), "Settle now" (game
    final, not settled). Under kill a red line says assignments stay halted; after the
    reset an "Activate all paper (N)" button (`POST /assignments/activate-paper`) shows
    while halted paper assignments of unfinished games exist.
  - Open orders (`#open-orders`): created, market with mode chip, the model
    ("elo_blend 08247922", so two assignments on one game can be told apart before
    pressing Cancel), "20 @ 0.58 (18 filled) · $11.84", status, worker, a Cancel button
    per row (`POST /orders/{id}/cancel`, paper cancels at once with the ledger release)
    and a "Cancel all" form (`POST /cancel-all`, JavaScript confirm, no kill).
  - Recent orders (`#orders`, last 50): the same plus the reject reason in red next to a
    `rejected` badge as the code and its meaning with the number it broke ("max_bet:
    over max bet $71.06 > $25.00", "participation: over 50% of book depth"), the cause
    of a cancelled, expired or exchange-rejected order from its last order event
    ("worker cancel", "kill", "drain", "owner cancel", "kickoff", "gtd expired"), the
    edge with "my 0.61 vs 0.57", the rationale line and the worker with the model.
  - Fills (`#fills`, last 50): time, market, "18 @ 0.58 of 30 @ 0.58", fee, worker.
  - Unmatched markets (`#unmatched`): title, platform and reference, the mapper's guess
    with its confidence, the book and snapshot age, and a link form (game select over
    every upcoming game, home/away wins, Link) posting to `POST /markets/{id}/link`; on a
    phone the two selects stack at full width so the game label is not cut off.
  - Markets (`#markets`): every confirmed market with game and side, bid / ask,
    liquidity, snapshot age (amber when older than 60 s or missing), status badge
    (`open`, `closed`, `resolved YES/NO` with the closing price).
  - Exchange (`#exchange`): an "up" or red "DOWN" chip (heartbeat older than 15 s or
    never), heartbeat age and time, market source, auth and balance, last error in red,
    and a "Probe markets" button (`POST /exchange/probe`) that renders the raw truncated
    payload on its own page with a Copy button for pasting back.
  - Ledger (`#ledger`): "OK" with the number of bankrolls whose replay matches the cached
    columns, or a red "problems" chip with one line per disagreement.
