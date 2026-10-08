# Dashboard spec (step 2; steps 3 to 6 added pages and sections; step 7 rewrote the layout)

Very simple and clean. Phone first. No framework, no build step, no external assets
(the dashboard is tailnet-only and phones may be offline from the public internet).
The one exception is the 3D fleet page at `/fleet` (React + three.js from `fleet-ui/`,
built by Vite inside the Docker image, fonts bundled, still no external assets); every
other page, including the fleet cards at `/fleet/list`, is server-rendered as below.
Step 7 (`docs/UI.md`) made every page answer one question in the first screen of a
phone and put everything else one tap away; where the two documents disagree about
layout or wording, `docs/UI.md` wins and this file follows it.

## Mechanics
- Server-rendered Jinja2 templates under `host/templates/` (`base.html` + one per page +
  fragments). Autoescape on. One stylesheet `host/static/style.css` (CSS variables, system
  font stack, light and dark via `prefers-color-scheme`), one script `host/static/app.js`
  (vanilla, no dependencies). Static files served by the app at `/static/`.
- (step 7) Every page is built from the components in `host/templates/_ui.html`: `stat`
  (a big number with its label and an optional muted note; a link when it has a page),
  `chip` (a state word on a state colour: ok green, warn amber, bad red, muted grey,
  never empty, an optional title says what the word means), `intro`, `bar` (progress),
  `disclosure` (a `<details>` styled as a card with a chevron, a count and a one-line
  summary of what is inside), `menu` (a `<details>` holding the row's actions, each a
  44 px `.menu-item`) and `row` (one list line: `a.row-main` with a title and one or two
  grey meta lines, then chips, `.row-value` and the menu). The tests look pages up by
  the `data-*` hooks these carry (`data-page`, `data-stat`, `data-row` + `data-id`,
  `data-chip`, `data-action`, `data-key`, `data-card`, `data-list`, `data-nav`,
  `data-banner`, `data-field`, `data-form`, `data-flash`), never by whole tags.
- Every state-changing action is a plain HTML form (POST, redirect back, flash message in
  a short-lived cookie that the next page shows once). JavaScript only adds: auto-submit
  of the role select on change, `confirm()` on KILL, Retire, Cancel all and Disable
  live, the periodic fragment refresh, hiding the flash after 8 s, a re-fetch when a page
  comes back from the back-forward cache, (step 7) remembered `<details>`, one open
  action menu at a time (a click outside or Escape closes it) and the kind switch of the
  New job form. Everything works with JavaScript off: disclosures and menus are
  `<details>`, every action is a form or a link.
- Refresh: `app.js` replaces `#fleet-grid` with `GET /fragments/fleet` every 5 s,
  `#trading-live` with `GET /fragments/trading` every 5 s, (step 7) Home's `#home-live`
  with `GET /fragments/home` every 5 s, and `#topbar-status` with
  `GET /fragments/topbar` every 10 s, skipping a refresh while an input inside has
  focus, while a select inside has had focus for less than 15 s (a select keeps focus
  after its picker is dismissed, so the hold is bounded), while an action menu inside is
  open, or while a form is submitting. A line under the sticky top bar shows "updated N s
  ago" measured from the page's own region's last successful fetch (so a held region
  reads as old), or "connection lost" if a fetch fails; while lost the region is dimmed
  and every status dot turns grey, because the server-rendered colours would otherwise
  claim health.
- (step 7) Remembered `<details>`: every `details[data-key]` keeps its open or closed
  state across fragment refreshes (saved before the swap, restored after) and across
  reloads in `localStorage` (`fleet.details.<key>`, inside try/catch, so it works
  without storage). A stored "closed" never hides a group holding an inline error or the
  element the URL fragment names. `#anchor` links open every disclosure around their
  target on load and on `hashchange` (a banner's `/trading#exchange` opens the Exchange
  group, `/settings#kill` the Live trading group); a section id opens the disclosure it
  wraps, `/jobs#<kind>` picks that kind in the New job form, and anchors land below the
  sticky top bar (`app.js` measures it into `--topbar-h`, used by `scroll-padding-top`).
  Only toggles by hand are stored. A group the server opens (`data-server-open`: an
  error, the kill, the exchange down with live orders, ledger problems, a model's
  Assign) is never folded by a stored or earlier "closed", only by a fold by hand after
  it opened.
- Owner auth is the same as the API (`Tailscale-User-Login` header, Origin check on POST).
  401, 403, 404, 405, 413 render a small HTML page explaining the cause, with a "Back to
  the dashboard" link. Every page is sent with anti-framing headers and
  `Cache-Control: no-store`. The 3D page's hashed build files under `/fleet/assets/`
  are the one exception to no-store: `private, max-age=31536000, immutable` (their names
  change with every build); they need the owner login like every page.
- Money: stored in cents, entered in dollars (server converts; `1,500` is a thousands
  group, `1,5` is refused, one billion dollars is the ceiling), shown as `$1,234.56`,
  with a sign for P&L (`+$6.57`, `-$1.20`, `$0.00`). (step 7) In lists, percentages have
  one decimal, probabilities are whole percentages (`62%`) and no raw float has more
  than three decimals; full precision stays on the detail pages and in the API.
- Timestamps are shown in the owner's `tz` setting with the zone abbreviation.

## Layout
- Page frame: `<main class="page" data-page="...">` opens with the `<h1>`, then a grey
  "What this page is" line (a `details.intro` that starts open; a dismissal is
  remembered), then two to four stats (2 columns on a phone, 4 from 700 px). The first
  screen of a phone (390x844) always holds the title and at least one stat.
- Top bar (sticky): the wordmark "Fleet" with a house icon is the link to Home (`/`);
  the nav has five labels, Fleet (`/fleet`, the 3D page), Jobs, Models, Trading,
  Settings, each with `data-nav` and `aria-current="page"` on the current section (a job
  page counts as Jobs, a model page as Models, the probe page as Trading, the enroll page
  as Settings, the card view `/fleet/list` as Fleet); the status cluster: mode pill (`PAPER` grey or `LIVE` green from
  `settings.live_enabled`), (changed: step 7) the P&L of the current mode only,
  "paper +$6.57 today" (from `/api/pnl`, a link to Trading, red when negative; the
  other mode's figure and the all-time figures moved to the Trading stats), red KILL
  button. (step 4) Two banners sit under the status cluster on their own line, each a
  link with `role="alert"` and `data-banner`: a red "EXCHANGE DOWN"
  (`/trading#exchange`) when the exchange heartbeat is missing or older than 15 s while
  any order is active or the kill switch is on, and an amber "N assignments unattended"
  (`/trading#assignments`) when an active assignment's trade job has sat queued for more
  than 60 s. Both refresh with the top bar fragment. When `kill_switch` is true the whole
  bar (and on a phone the bottom nav) turns red, the pill and the P&L stay, the KILL
  button is replaced by a disabled "KILLED" chip and the text "TRADING KILLED. Reset in
  Settings." links to the reset form.
- (step 5) The pill is `LIVE` green only while `settings.live_enabled` is true, which
  only the typed switch sets; a kill or the live daily-loss trip turns it back to
  `PAPER`. When the exchange process pulled the kill switch itself, the killed bar reads
  "TRADING KILLED automatically: <reason>. Reset in Settings." (the reason of the newest
  `auto_kill` audit row since the last `kill_reset`, also in `data-auto-kill`); a hand
  kill keeps "TRADING KILLED. Reset in Settings.".
- (step 7) Width under 700 px: one column; the nav becomes a bar fixed to the bottom of
  the screen with an inline SVG icon above each label (44 px targets, safe-area padding
  through `viewport-fit=cover`), and the page gets bottom padding so nothing hides behind
  it. Tap targets are at least 44 px, gutters 16 px, no horizontal scroll at 390 px.
- (step 7) Rows: a list is `ul.rows` of one-line `.row` items (two lines at most, never
  taller than 88 px at 390 px). The row's own link is its primary action; every other
  action sits in its "..." menu. No generated paragraph appears in a list.

## Home `/` (step 7)
- The stats, "Needs attention" and "Recent" sit in `#home-live`, refreshed every 5 s
  from `GET /fragments/home` (`_home.html`), so the "updated N s ago" line measures them.
- Stats: "Workers online 5 / 7" (to Fleet), today's P&L of the current mode with the
  all-time figure as its note (to Trading), open orders (to `/trading#open-orders`) and
  the best model with its headline number by the Models page's own rule (CLV when ranked
  on paper or snapshot replay, the held-out ROI otherwise, "ROI -" without held-out bets;
  to the model page, "-" when none is ranked).
- "Needs attention": one row per problem with a state chip and a link to where it is
  fixed, built from the same signals as the top bar banners (kill switch with its
  auto-kill reason, exchange down, assignments unattended) plus an enabled worker offline
  for more than 5 minutes, an assignment of an unfinished game with no eligible model (a
  red "no model" chip on an active one whose lineage is retired, an amber one on a live
  one, active or halted, whose lineage is no longer live eligible: leaving live_eligible
  halts it in the same transaction, so the grey line says "live assignment halted, model
  candidate"; a halted assignment of a retired lineage is not listed, retiring halted it
  on purpose), a model whose latest validate job failed, and exchange credentials never
  checked or failing. Empty state: "Nothing needs you."
- "Recent": the last 5 settled bets as rows (game, mode, contract, time, a win/loss
  chip, the P&L), each linking to its model.

## Fleet `/fleet`: the 3D control page (changed: the cards moved to `/fleet/list`)
- A single-page app from `fleet-ui/` (React + three.js, Vite with base `/fleet/`), served
  by `host/api/dashboard_fleet3d.py`: `GET /fleet` is the built `index.html`
  (`Cache-Control: no-store`), `GET /fleet/` redirects to `/fleet`, and
  `GET /fleet/assets/<file>` serves the hashed JavaScript, CSS and fonts (a path that
  would leave `assets/` is a 404). All three sit behind the same owner check as every
  page (`Tailscale-User-Login`, worker-IP refusal) and carry the anti-framing headers.
  The build is looked up in `FLEET_UI_DIR`, then `host/fleet_ui/` (where the Docker image
  copies it), then `fleet-ui/dist` (a local build). Without a build `/fleet` is a 503
  page saying "run npm ci && npm run build in fleet-ui, or rebuild the Docker image",
  with a link to the card view.
- It talks only to the owner JSON API (`docs/PROTOCOL.md`, "Fleet UI additions"): `GET /api/fleet`
  and `GET /api/fleet/events` every 3 s, `POST /api/workers/{id}/role` and
  `POST /api/workers/{id}/reboot`. A chip in the corner reads "Live" while polls succeed
  and "Disconnected" once one fails, until a poll succeeds again.
- Scene: the Data city (`fleet-ui/src/scene/city.ts`), one tower per worker on a grid
  that fits any number of workers. A tower's height follows CPU load, its colour the
  state (working, idle, running hot, offline), and its label shows the role, CPU and RAM.
- Machine list: every worker by name with its state, role and load; picking one (in the
  list or in the scene) opens the inspector.
- Inspector: the machine's name, host, role, online state and last heartbeat, CPU, RAM,
  temperature, boot disk (flash, SSD, HDD) with its wear and GB written since boot, and
  its current jobs (a trade job shows its game); one button per role (Idle, Backtest,
  Search, Train, Trade) and Reboot. Reboot needs a worker that is online and was
  installed with the reboot unit (`can_reboot`); otherwise the host answers 409 ("worker
  is offline", or "this worker cannot reboot yet: re-run install.sh on it") and the page
  shows that text.
- Command bar, one line, worker names separated by spaces or commas:
  - `reboot <workers>`: request a reboot of each;
  - `stop <role>`: every worker in that role goes to Idle;
  - `<role> on idle`: every idle worker takes that role;
  - `<role> <workers>`: those workers take that role.
  Roles are the ids or short names from `/api/fleet` `roles`. A command that moves any
  worker into or out of `trade` asks for a confirm first (leaving trade cancels that
  worker's open orders).
- Event feed: newest first, from `GET /api/fleet/events` (audit rows: role changes by
  the owner and the auto-role, enable and disable, enrollment, reboot requested and
  done, kill and kill reset; job events of jobs on a worker: claimed, succeeded, failed,
  released, lease expired, cancelled) plus what the page derives by comparing polls
  (a machine going offline or coming back, a temperature crossing). Each line has a
  tone: ok, hot (warnings), off (machine down or going down) or neutral.

## Fleet cards `/fleet/list` (changed: step 7 put them at `/fleet`, the 3D page moved them here)
- The phone-friendly view of the same fleet, linked as "3D view" (to `/fleet`) under its
  title; the nav marks Fleet as current here too. Home's offline-worker rows link here.
- Stats (inside `#fleet-grid`, so the 5 s refresh keeps them current): online / total,
  running a job, offline, switching role.
- One card per worker, sorted by name, two columns from 700 px. A card is one row: the
  status dot (green online below `online_after_seconds`, 30 s by default, amber stale up
  to 60 s, grey offline after that, announced
  through `aria-label`), the name with a "stale"/"offline" and a "disabled" chip, a grey
  line with the state in words ("offline 1 h ago · disabled · no job") and the current
  jobs ("job: model_search 42%"; a held `trade` job reads "KC @ LV held", one per
  assignment; after the "switching to ..." text while a role change is pending), the
  role `<select>` and the "..." menu. The "offline" chip is amber on an enabled worker
  (as on Home) and grey on a disabled one. The row links to the worker's first current
  job; the menu has an "Open job: ..." link for each current job. A running job's progress bar (`role="progressbar"` with numeric
  `aria-valuenow`) sits on a thin line under the row; a held trade job has none.
- Role `<select>` with idle / backtest / model_search / train / trade and a Set button
  (hidden when JS auto-submits; `POST /workers/{id}/role`, redirect back to `/fleet/list`).
  While `switching` is true the grey line reads "switching to backtest (epoch 7)…" until
  the ack; the select is disabled only while the worker is online (the ack takes
  seconds). A stale or offline worker keeps its select enabled so a wrong pick can be
  undone before it comes back.
- The menu: Disable or Enable (`POST /workers/{id}/enabled`, back to `/fleet/list`) and a
  Details disclosure with the stats line (`CPU 12% · RAM 1.2 / 7.7 GB · 3 s ago · v
  a1b2c3d4e5f6`), the id, hostname and Python version, and today's P&L of the worker (its
  share of `/api/pnl`).
- Empty state: "No workers yet. Mint an enroll token in Settings and run the install line
  on a Debian box."

## Jobs `/jobs`
- Stats: running, queued, trading (held trade jobs), done in 24 h (with the failed count).
- (changed: step 7) One "New job" disclosure (`jobs-new`, closed by default) holds the
  five send forms, each its own `data-form` under a heading: Backtest (model select with
  a "(none: use family + params)" option, family, params JSON, first and last season
  (the last season's help reads "blank = last complete (snapshots: through the season in
  progress)", since a snapshot replay with it blank includes the season in progress),
  (step 6B) "Price source": "closing line (every game, CLV 0)" or "snapshots (recorded
  prices, real CLV)" with a muted line naming what a snapshot backtest replays and a red
  line when the market source is sim while sim prices are not allowed), Model search
  (family, candidates, seed, seasons, keep top), Train (model, through season and week;
  disabled until a model exists), (step 6) Validate (model, seed; names the validation
  era), and Sleep (test). Every form has a target select (Any idle worker, then each
  worker by name). With JavaScript a "Kind" select shows only the chosen form; without it
  every form shows under its heading. The disclosure opens with the train form behind
  `?train_model=<id>`, the validate form behind `?validate_model=<id>` (model
  preselected), or the form just rejected (400, error inline, values kept). Model labels
  are "K 24 · HFA 55 · MOV on · thru 2024 w18 · 8f173b7b" (or "untrained"); defaults
  come from settings. (step 6C) The Model search family select lists `ingame_wp`; for it
  the "ingame_wp only" fieldset (train first and last, validation first and last; blank
  = the host defaults [2012, 2021] and [2022, open]) is sent as `train_seasons` and
  `validation_seasons` instead of the pre-game seasons. The Backtest family select and
  the Backtest, Train and Validate model selects leave ingame_wp out (the host refuses
  those jobs for it).
- Two tabs (links with `aria-current`): Running (queued, leased, cancel requested, held)
  and Done (`?tab=done`, the last 50 succeeded, failed and cancelled). Each job is one
  row: kind and target (the model, the family and size, the game), a "snapshots" chip on
  a snapshot backtest, the worker (or "waiting for an idle worker") and the created or
  finished time, a bar with the percent while it runs, the state chip, and Cancel in the
  menu for queued and leased jobs.
- `/jobs/{id}`: the kind, target and short id as the title, a link to the model when
  `params.model_id` is set; stats for progress (with the state chip), worker, run time
  and lease expiries; created, started and finished times; Cancel; then the Error card,
  or the Result card: created models as buttons ("(existing)" when the row was already
  there), a backtest's metrics as labelled pairs and its per-season table, (step 6B) a
  snapshot backtest's replay line first, a search's top candidates (params, shrunk ROI,
  ROI, bets, log-loss, market, drawdown, the created model). (step 6) A validate job
  shows the same Robustness section as the model page. Then the disclosures
  Parameters (the params JSON; (step 6B) the snapshot replay params in words),
  Checkpoints (a one-line digest such as "candidate 12, season index 3; 12 evaluated" or
  "stage regimes", then the JSON), Log (the events: time, event, worker, detail) and Raw
  (the raw result as stored).

## Models `/models` (step 3; changed: steps 4, 6, 6B, 7)
- Stats: the best model with its headline number, ranked (with how many of them are
  cleared for paper: paper ok or live eligible), live eligible, unranked.
- One grey line says how the list is sorted: paper CLV after 5 paper games and 30 paper
  bets, then snapshot CLV after 30 bets replayed on recorded prices, else the validation
  ROI; few bets are shrunk toward zero (docs/TRADING.md and docs/ROBUSTNESS.md hold the
  formulas).
- "Ranked" card: one row per lineage, best first. The title is "#1 elo_blend K 40 · HFA
  70" with the flag chips after the name (`overfit` red, `fragile` and
  `regime-dependent` amber, each with its meaning as the title), a green "beats market"
  chip and a dashed "not validated" chip. The grey line starts with the status chip
  (`paper ok` and `live eligible` green, `candidate` and `retired` grey; it leads this
  line rather than sitting at the side so the name keeps its width on a phone), then the
  rank basis chip ("paper" or "snapshot", its meaning as the title) and the record behind
  the headline ("5 games · 50 bets · +$14.00 · range +0.4% to +3.1%", "60 games · 30
  bets replayed · range ...", or "130 held-out bets · range -1.2% to +9.4% · p =
  0.012"), and an "N rows" chip when the lineage has children. A paper or live record
  counts each game once, as the paper gate does (two models of one lineage trading the
  same game make one game; the API's `games` and the paper rank threshold still count
  one per model), and counts read "1 game", "1 bet". At the side: the one
  headline number ("CLV +0.6%" when ranked on paper or snapshot, "ROI +6.2%" on the
  validation era, the search-era ROI for an unvalidated lineage, which its title says),
  then the "..." menu: Details, Train (the Jobs form prefilled), Validate (the same),
  Assign (`/trading?model=<id>#assign`) and Retire (a POST with a confirm); a retired
  lineage has only Details and Train. Tapping the row opens the model page.
- "Unranked (n)": a closed disclosure with the not validated and retired lineages, the
  reason on each row. With no ranked lineage one line says why ("No lineage is validated
  yet, so none is ranked." or "No models yet. Send a model search from the Jobs page.").
- (step 6C) A pre-game row has no in-game line: settlement credits every in-game bet to
  the assignment's in-game model, so only an ingame_wp lineage has an in-game record (its
  row in "In-game models" shows it).
- (step 6C) "In-game models (n)" (`#ingame`, a closed disclosure `models-ingame` whose
  summary counts the ones beating vegas_wp; `host/api/models_ingame_view.py`): the
  ingame_wp lineages, never ranked with the pre-game ones (in `GET /api/models` they are
  in `unranked` with the reason "in-game model", after the others), the ones beating
  vegas_wp first, then by the log-loss gain over it. One row each (`data-row="ingame-model"`):
  "ingame_wp L2 1.00 · time 1.00 · field 1.00" with a green "beats Vegas WP" chip (or a
  dashed "not validated" chip); the grey line has the status chip (its reason as the
  title) and "41,812 plays 2022-2024 · log-loss 0.447 vs 0.452"; the second grey line is
  the status reason ("its validation log-loss is worse than the vegas_wp baseline",
  "validated on N plays, fewer than 10000"), or, once the lineage has in-game bets, the
  in-game paper record ("in-game paper 3 bets · +$4.80", `.row-ingame`). At the side the
  log-loss gain per play ("+0.005"), then the menu: Details, "Assign in-game"
  (`/trading?ingame_model=<id>#assign`) and Retire (both hidden once retired).
- The page ends with the nflverse attribution line (CC BY 4.0, links to nflverse-data
  and the licence).

`/models/{id}` (changed: step 7):
- The family and short params as the title, then three stats: edge vs the market (CLV
  with its 90% range), the backtest ROI with its bets, and the paper record (games, bets,
  P&L; its games counted once each, the number the verdict's "(N so far)" uses). Under
  them the status chip, the "not validated" chip and the gate verdict in words ("Not yet
  eligible for live: needs 40 paper bets (30 so far) and a CLV interval above zero.";
  the P&L rule reads "a paper profit" at the default one cent, else "a paper P&L of at
  least +$5.00"; with no paper games yet the sentence lists every paper rule, the average
  CLV floor included; live eligible says it passed the held-out seasons, or the backtest
  gate when "Require validation" is off; also not validated, a candidate with the rules
  it misses and retired), one caption explaining CLV, and the
  actions: Train, Validate (a one-tap form: a validate job for this model to any idle
  worker, seed 1), Assign, and "Retire lineage" in the menu (hidden once retired).
- Closed disclosures, each with a one-line summary: Summary (the text, the folded "Edit
  summary" form, maxlength 600; id, lineage with its root chip, parent, trained-through
  point, creating job, the validation shrunk ROI (the rank key) and the search shrunk
  ROI, each with a caption; the params JSON), (step 6) Robustness (the flag chips or a
  green "no flags" chip, the "beats market" chip, one line per flag, the CI line, the
  market test sentence, the calibration pairs, then the price stress table, the
  neighbourhood sentence and the regime table, each with a one-line reading above it
  (the stress reading follows the worker's fragile rule: "the edge survives worse
  prices" only when the base ROI is positive, every stressed run keeps at least half
  the base bets and the worst stressed ROI stays above zero, else "worse prices cut the
  bets to 40 of 120", "worse prices remove the edge" or "there was no edge at base
  prices"), and the validation per-season table; the 90% range caption says a range
  above zero makes luck an unlikely explanation; "Not validated yet ..." without numbers),
  Backtest metrics (labelled pairs with a muted caption under ROI, hit rate, edge,
  log-loss, drawdown and shrunk ROI; per-season and calibration tables), (step 6B)
  Snapshot replay (the replay line, pairs, per-season table, "Replay on snapshots"),
  Paper results (the paper record, signed like the stat, with the shrunk CLV and its 90%
  range, the live record when there is one), Assignments (the games this lineage was
  assigned to, a red LIVE chip on live ones) and History (the lineage members and the
  jobs that created, trained, validated or replayed it). (step 6C) A pre-game model page
  has no in-game line: in-game bets always belong to the ingame_wp lineage.
- (step 6C) An ingame_wp model page (judged on held-out play-by-play against nflverse's
  vegas_wp, never backtested for edge): the stats are the log-loss (vegas_wp's and the
  gain as its note), the held-out plays (seasons, 10,000 needed) and the in-game paper
  record; the verdict reads "Cleared for in-game paper trading: it beats Vegas WP over
  41,812 held-out plays. In-game orders never go live in this step." or "Not cleared for
  in-game paper trading: <reason>. Paper OK needs ..."; one caption explains log-loss and
  vegas_wp; the actions are "Assign in-game" and "Retire lineage" in the menu (no Train,
  Validate, Assign or snapshot replay). The Summary disclosure shows "fitted on" ("train
  seasons 2012-2021", from its search-era metrics, with a caption: the model search fits
  it once and an in-game model is not trained week by week) instead of the
  trained-through point, then "held-out validation" (with a "beats Vegas WP" or amber
  "does not beat Vegas WP" chip), "status rule" and "in-game paper record" ("1 game · 2
  bets · +$4.80") instead of the shrunk ROIs. In place of Robustness,
  Backtest metrics, Snapshot replay and Paper results it has (`_model_ingame.html`,
  `#ingame-validation`) the closed disclosures In-game validation (log-loss and Brier
  against vegas_wp, the plays left out without a vegas_wp), By period (Q1 to Q4, OT), By
  score (home minus away before the play: <=-9, -8..-1, 0, 1..8, >=9) and Calibration
  (headed "p, plays, model, actual, vegas_wp" so the table fits a phone, the long names
  "mean model probability", "mean outcome" and "mean vegas_wp probability" as the
  headers' titles), each with a one-line reading, or "No held-out validation stored.";
  then Assignments and History.

## Settings `/settings` (changed: step 7)
- Stats: live trading on/off, kill switch, max bet, market source (each a link to its
  group), all read from the stored settings: a rejected post re-renders its form with
  what was typed, but the stats keep saying what is in force.
- Closed disclosures, one per group, each with a one-line summary of the values in force
  in its header (`host/settings_summary.py`; "-" when a value is missing), so the page
  reads at a glance with everything closed. A rejected form opens its group, keeps what
  was typed, shows the error inline and puts a "Not saved" line at the top linking to the
  form; being killed opens Live trading. Every field is a text input with
  `inputmode="decimal"` (dollars, fractions) or `inputmode="numeric"` (integers) so a
  phone opens the number keyboard while a blank value and server-side validation keep
  working, and every field's help is a muted line under its input. Each form keeps its own
  address and field names:
  - Limits (`POST /settings/trading`, dollars): max bet, max daily loss paper and live,
    default bankroll per game, liquidity floor; min edge, Kelly fraction, max games per
    trade worker.
  - Trading (`POST /settings/trade`, step 4) under four headings: Order approval
    (participation, book max age, order lifetime, orphan cancel after, trade tick, max
    paper models per game, max exposure paper and live, "Pregame only", whose help
    reads "no orders after kickoff; unticked, orders after kickoff go only through the
    in-game rules (paper only)" (step 6C: a pre-game order is cancelled at kickoff
    either way); a note on the fixed price band), Market source and snapshots (`market_source` select, lookahead,
    snapshot cadence active and idle, retention, scores URL, `market_source_config` JSON),
    Paper thresholds (min games, bets, days, avg CLV, P&L, (step 6) "CLV interval above
    zero"; saving recomputes paper eligibility) and Rate limits.
  - Robustness gates: Backtest thresholds (`/settings/thresholds`: min bets, min ROI, max
    drawdown, min ROI 5th percentile, max market p, "Require validation", "Forbid
    overfit", "Forbid fragile", "Forbid regime-dependent"; saving recomputes every
    lineage's status), Seasons and search (`/settings/seasons`: search and validation
    eras, search workers; an overlapping era is an inline error) and the Fee model
    (`/settings/fees`).
  - Snapshot replay and signals (step 6B): decision minutes before kickoff and "Allow sim
    prices" (`#replay`); the nflverse signals refresh hours and the injuries and
    play-by-play URL templates, each must contain `{season}` (`#signals`).
  - Fleet: worker timing (`/settings/fleet`: lease, heartbeat, online-after, max lease
    expiries; checked together: lease >= 2 x heartbeat + 5 and online-after >
    heartbeat), the time zone (`/settings/tz`, IANA name) and Enroll a worker ("New
    enroll token"; the next page shows the token once with the environment and argument
    install one-liners and Copy buttons). Roles are set per worker on the Fleet page.
  - (step 5) Live trading (`#live`, `#kill`): the typed switch (off: what fails, then the
    form whose field must be exactly `ENABLE LIVE TRADING YYYY-MM-DD`, today in the
    owner's zone; on: since when and by whom, and "Disable live" with a confirm), the
    credentials, auth, balance, buying power, clock skew, last auth error and the
    auto-kill reasons each with its recovery (`data-remedy`), then the kill switch: its
    state and, when killed, the reset form that needs exactly `RESUME` (and, after an
    automatic kill, the reason, the detail and the reminder that live stays off). A live
    group is tinted green, a killed one red. `live_enabled` has no field in any generic
    group.
  - Data: nflverse games (refresh hours, the games.csv URL, the row count, the last
    refresh outcome or "Last refresh failed <time>: <error>" in red, and "Refresh now",
    which reports in the flash).
  - Audit log: the last 20 owner actions as rows (action, entity, time, actor,
    confirmation text).
  - (step 6C) In-game (`_settings_ingame.html`, `#ingame`, `POST /settings/ingame`,
    `host/settings_forms_ingame.py`, validators in `host/settings_schema_ingame.py`),
    after Trading; its header reads "off by default · max bet $5.00 · min edge 5.0% · lag
    limit 20 s · feed espn · paper only". A line says in-game orders are paper-only, then
    three headings. Trade rules: "Trade in-game by default" (`trade_ingame`), in-game
    tick (1..60 s), max game-state age (5..300 s), quiet seconds (0..300), cutoff (0..900
    game seconds), dead zone (0..0.5), in-game min edge (0..0.5), in-game max bet in
    dollars (`ingame_max_bet_cents`), in-game order lifetime (10..3600 s). Feed lag: max
    feed lag (above 0, at most 600 s), min measured events (1..100). Game-state feed: poll
    each live game every 3..5 s (`gamestate_poll_s`), ESPN max requests per second (above
    0, at most 10), one checkbox per source (ESPN, Yahoo; stored as `gamestate_sources`),
    the ESPN summary URL (must contain `{event_id}`), the Yahoo play-by-play URL (empty =
    off, else must contain `{event_id}`), Yahoo poll (5..120 s).

## Trading `/trading` (step 4; changed: step 7)
Every action is a form that redirects back to `/trading` with a flash, and a refused
action (409: kill on, game not final, limit hit) comes back as a flash too, never an
error page.
- A closed "New assignment" disclosure (`#assign`) at the top, above and outside the
  live region so a refresh never wipes a half-typed bankroll; open when `?model=<id>`
  prefills it (the Assign buttons) or after a rejected post (error inline, values kept,
  nothing stored). Game select (upcoming games with a confirmed market), model select
  (models of non-retired lineages, trained first; an untrained one says it mirrors the
  market and never trades), mode (live appears only while `live_enabled`), bankroll and
  optional max bet in dollars. `POST /assignments` calls `create_assignment`. The button
  is disabled with a one-line reason when no game or no model qualifies. (step 6C) An
  "In-game model" select (`ingame_model_id`, ingame_wp models of non-retired lineages,
  "ingame_wp · L2 1.00 · time 1.00 · field 1.00 · paper_ok · 8f173b7b", first option "no
  in-game model") and a "Trade in-game (paper only)" box (`trade_ingame`, ticked by
  default when `settings.trade_ingame` is true); a hidden `ingame_form=1` marker tells an
  unticked box (explicit off) from a post without the fields (the step 4 call). The
  pre-game model select leaves ingame_wp models out (they trade only in-game);
  `?ingame_model=<id>` (the "Assign in-game" actions) or `?model=<id>` of an ingame_wp
  model opens the form with it preselected in the in-game select.
- `#trading-live`, refreshed every 5 s from `/fragments/trading`:
  - Stats: "Paper today +$6.57" (all time as its note), (changed: step 7) "Live today"
    (`data-stat="live-today"`, all time as its note) while live is on or real money is
    still in play (an active live order, a live assignment not yet settled, or cash
    reserved or in open live positions), open orders (with the live count), exposure
    (open order reservations plus position cost, the figure the exposure limit checks)
    and "Exchange ok · sim · 2 s ago" (or DOWN).
  - Assignments (`#assignments`): one row each, linking to the model. The title is the
    game ("KC @ LV"), led by a red LIVE chip on a live assignment (whose row is tinted
    with a green left edge) or followed by a grey "paper" chip; the grey line holds the
    model and params, kickoff, game id and "final 20-24" once final. Then the bankroll
    available, "Settle now" once the game is final, the status chip (active green,
    halted amber, settled grey) and the menu: Halt (active) or Activate (halted, hidden
    under kill), Detail (`/api/assignments/{id}`) and the bankroll breakdown (initial,
    available, reserved in orders, open in positions, realized, open orders). Under kill
    a red line says assignments stay halted; after the reset "Activate all paper (N)"
    (`POST /assignments/activate-paper`) shows while halted paper assignments of
    unfinished games exist. There is no Fund action: the API has no top-up route.
    (step 6C, `host/trading/views_ingame.py`, `_ingame_row.html`) An assignment with an
    in-game model, switch or game state gets a third grey line (`.row-meta.row-ingame`):
    an "in-game on" (green, accent ring) or "in-game off" chip, the latest game state as
    "Q3 4:12 · 17-14 · 3 s ago" (away-home as in the "KC @ LV" title; "Half", "End Q1",
    "OT 1:01", "Final", "Pre-game"), or an amber "state stale" chip before the last line
    once the state is older than `ingame_max_state_age_s` (a final state never goes
    stale), or "no game state yet"; while the game is in progress the in-game model's home
    probability next to the home market mid ("model LV 62% · mid 58%", `data-p-home`
    carries three decimals); with a stale state that probability is muted and ends
    "(from the stale state, not traded)", since the host rejects in-game orders on it
    (`ingame_stale`). A paper assignment that can still trade has the in-game
    switch in its menu (`data-form="ingame-toggle"`, `POST /assignments/{id}/ingame`:
    in-game model select, "Trade in-game" box, "Save in-game"), which calls
    `assignments_ingame.set_ingame` (audited; turning it off cancels the open in-game
    orders, named in the flash; a refusal is a flash). A live assignment's menu says
    in-game orders are paper only.
  - (step 6C) In-game feed (`#ingame-feed`, `data-card="ingame"`, `_trading_ingame.html`),
    a closed disclosure right after the assignments (open while buys are suspended): the
    header has a "not suspended" (green) or "buys suspended" (red) chip and the lag per
    source ("ESPN 6 s behind · ESPN scoreboard 3 of 5 events"); inside, one line per
    source over its last 20 measured events ("ESPN: median 6 s behind the market over 12
    events", "ahead of" when negative, "not enough data (3 of 5 events measured)" below
    `ingame_lag_min_events`), the reason line (median lag against `ingame_max_lag_s`;
    sells stay allowed) and the polled sources.
  - Closed disclosures, each with a count and a one-line summary, keyed
    `trading-<name>` and wrapped in a section with the old anchor:
    - Open orders (`#open-orders`): "Cancel all" (`POST /cancel-all`, confirm, no kill),
      then one row per order: "20 @ 0.58 (18 filled) · $11.84" with the live, sell,
      smoke and (step 6C) `in-game` chips, the market, exchange id, model, worker and time on the grey line,
      the status chip and Cancel (`POST /orders/{id}/cancel`) in the menu.
    - (step 6B) Positions (`#positions`): per assignment holding contracts, its label
      and total ("unrealized (net of sale fee) +$1.20"; the header says "unrealized net
      of fee"), then one row per market: side, size, average cost, "basis $8.00" and
      the bid on the grey line, a second grey line "unrealized, net of sale fee" and the
      unrealized P&L at the bid after the taker fee of the sale at the side ("no bid"
      when there is none).
    - Recent orders (`#orders`, last 50): the same rows (the edge with the model's and
      the market's probability as whole percentages, "edge +3.4% (my 55% vs 52%)") plus
      a second grey line with the reject reason in red ("max_bet: over max bet $71.06 > $25.00"), the cause of a
      cancelled or expired order ("kill", "gtd expired") and the rationale. (step 6C)
      The in-game reject reasons read in words: `ingame_disabled`, `ingame_paper_only`,
      `ingame_stale` ("game state too old"), `ingame_quiet`, `ingame_cutoff`,
      `ingame_lag_suspended`; `max_bet` on an in-game order names the lower of the caps
      including `ingame_max_bet_cents`.
    - Fills (`#fills`, last 50): "18 @ 0.58" ("sold N @ p" with a sell chip, an
      `in-game` chip on the fill of an in-game order), the order,
      fee, basis, market, worker and time, the realized P&L of a sell.
    - Markets (`#markets`): every confirmed market with game, side, bid / ask,
      liquidity and closing price, the snapshot age (amber when older than 60 s or
      missing) and the status chip (`resolved YES/NO`).
    - Unmatched markets (`#unmatched`): the mapper's guess with its confidence, the book
      and age, and the link form (game, home/away wins, `POST /markets/{id}/link`) in
      the row's menu.
    - Exchange (`#exchange`): heartbeat, source, auth chip with its age, credentials,
      clock skew, balance and buying power, live orders ("N open, M cancel pending", a
      "smoke" chip), the `cancel-all --direct` recovery line when the exchange is DOWN
      with live orders (the group then opens by itself), the last errors and "Probe
      markets" (`POST /exchange/probe`), which renders the raw truncated payload on its
      own page (`probe.html`: source and HTTP status stats, the URL, a Copy button).
      (step 6C) Beside it the "Probe game state" form (`data-form="probe-gamestate"`: the
      ESPN event id, digits only, with the event ids of assigned unfinished games offered
      as suggestions) posting to `POST /exchange/probe-gamestate`, which renders
      `probe.html` as "Game-state probe": the event and its game as a stat, event_id,
      game_id (or "no game has this ESPN event id"), url, status, error, the parsed
      states (Copy) and the raw payload (Copy).
    - Ledger check (`#ledger`): "OK" with the number of bankrolls whose replay matches,
      or a red "problems" chip with one line per disagreement (the group then opens).

## Screenshots and layout checks

`tests/hw/screenshots.py` (see tests/hw/README.md) seeds a throwaway database
(`tests/hw/seed_shots.py` with the step 3, 4, 5, 6, 6B and 6C seeds) and captures Home,
Fleet, Jobs (both tabs), the job pages (sleep, search, backtest, validate, snapshot
replay), Models, the model pages (paper-ranked, flagged, snapshot-ranked, ingame_wp),
Trading (with the in-game line, chips and the In-game feed), Settings, the market and
game-state probe pages, the validate form and the New assignment form (also with an
ingame_wp model preselected), at
390x844 and 1280x800 in light and dark; then settings, trading, fleet and home with live
on, after an auto-kill, after a hand kill, and trading after the reset. Every capture
runs the `docs/UI.md` assertions (`tests/hw/ui_checks.py`): no horizontal overflow,
every chip has text, every `<details>` has a summary with text; at 390 px the h1 and a
stat in the first screen, no row taller than 88 px and 44 px tap targets for buttons,
row links, selects, inputs and menu items, measured again with every disclosure open.
`tests/hw/test_row_audit.py` renders Models with 20 lineages and Trading with 20
assignments and their orders and checks each page stays under six phone screens.
