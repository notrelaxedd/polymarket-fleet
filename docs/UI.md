# UI overhaul spec (step 7)

Goal: the same dashboard, easier to read and much shorter. Every page answers one
question in the first screen of a phone, and everything else is one tap away. Nothing
about the API, the data model or the rules changes in this step; only `host/templates/`,
`host/static/`, the view helpers that shape numbers for display, and the tests that look
at pages.

The mechanics in `docs/DASHBOARD.md` stay (server-rendered Jinja2, no framework, forms
for every action, fragment refresh, auth, no-store, anti-framing). Where this document
and `docs/DASHBOARD.md` disagree about layout or wording, this document wins and
`docs/DASHBOARD.md` is updated to match in the same change.

## What was wrong
- Pages are long lists of dense key/value lines ("ROI · bets 0 · log-loss 0.609 vs
  0.609 · drawdown 0.0% · seasons 2019-2025"). On a phone the Models page is six
  screens tall for five rows, the Trading page is more than ten.
- Every row repeats the same generated paragraph, so the eye cannot find what differs.
- Everything is always expanded: open orders, fills, unmatched markets, the exchange
  block, the ledger check, all stacked on one page.
- Numbers are labelled with jargon in small grey type (CLV, log-loss, shrunk) and the
  explanation lives in the README, not next to the number.
- Buttons repeat on every row (Train, Assign, Edit summary) and take a full line each.

## Principles
1. One question per screen. The first screen of every page is a short answer in large
   type, not a list.
2. Rows are one line on a phone (two at most). Details open on tap.
3. Say it in words first, then the number: "beats the market by 0.6%" above "CLV 0.006".
4. Colour means state, never decoration. Green = good or on, amber = attention, red =
   stop or loss, grey = idle or unknown. Each colour always also has a word.
5. One action per row, the rest in a "..." menu (a `<details>` element, no JS needed).
6. No generated paragraphs in lists. The one-paragraph summary lives on the detail page.
7. Big tap targets (44 px), 16 px gutters, no horizontal scrolling, nothing narrower
   than 390 px breaks.
8. Everything still works with JavaScript off (details/summary, forms, links).

## Shared pieces
- `host/static/style.css` is rewritten around a small set of components: `.stat`
  (big number + caption), `.row` (one-line list item: title, meta, status chip, action),
  `.chip` (state chip, one of ok/warn/bad/muted, always with text), `.card` (section
  with a one-line header and an optional count), `.disclosure` (`<details>` styled as a
  card with a chevron), `.menu` (`<details>` that opens a short list of actions), `.bar`
  (progress). Spacing scale 4/8/12/16/24. Two type sizes for body and meta plus one
  display size for stats. Light and dark via `prefers-color-scheme` as before.
- `host/static/app.js` keeps the refresh logic; it gains one thing: open `<details>` keep
  their open state across fragment refreshes (the fragment carries `data-key`, the
  script restores `open` by key). A group the server opens for a reason (an error, the
  kill, an alert, a link that asked for it) is never folded by a remembered "closed",
  and an `#anchor` link opens the group it names, picks the job kind it names and lands
  below the sticky top bar.
- Numbers: money as `$1,234.56`, percentages to one decimal, probabilities as
  percentages ("62%"), never raw floats with five decimals in a list. Full precision
  stays on detail pages and in the API.
- Every page has a short grey "what this page is" line under the title that can be
  dismissed (a `details` that starts open, state kept in `localStorage`).
- Top bar: wordmark, mode pill, P&L for the current mode only ("paper +$6.57 today"),
  KILL. The second mode's P&L moves to the Trading page. The nav is five labels; on a
  phone it becomes a bottom bar with icons and labels.

## Pages

### Home `/` (replaces Fleet as the landing page; Fleet moves to `/fleet`)
First screen: four stats in a 2x2 grid: "Workers online 5 / 7", "Today paper
+$6.57", "Open orders 2", "Best model: elo_blend K 40 · CLV +0.6%". Each stat is a
link to its page. Below: "Needs attention" list (empty state: "Nothing needs you")
built from the same signals the top bar banners use plus: a worker offline > 5 min, an
assignment with no eligible model, a validate job failed, kill switch on, exchange
credentials not checked. Then "Recent" (last 5 settled bets as one-line rows).

### Fleet `/fleet`
One row per worker: dot + name, role select (unchanged mechanics), "job: model_search
42%" bar on the same line, and a "..." menu with Disable, Details. The stats line (CPU,
RAM, version, last seen) moves into the details disclosure. Cards stay as the
container on wide screens (two columns at 700 px and up), rows on a phone.

### Jobs `/jobs`
Two tabs implemented as links with `aria-current`: "Running" (queued + running + held)
and "Done" (last 50 finished, failed, cancelled). Each job is one row: kind, target
(model or game), state chip, progress or finished time, worker. The "New job" forms
(backtest, model search, train, validate) sit behind one "New job" disclosure with a
kind select that shows only the relevant fields (JS) or all fields with headings (no
JS). Job detail keeps its content but groups it: Summary stat row, then disclosures for
Parameters, Checkpoints, Log, Raw.

### Models `/models`
One row per lineage: rank, name + key params ("elo_blend K 40 · HFA 70"), status chip
(paper / live_eligible / candidate / retired), one headline number chosen by rank basis
("CLV +0.6%" when ranked on paper, "ROI +6.2%" when ranked on backtest), and the
"..." menu (Train, Assign, Validate, Retire). The flags (overfit, fragile,
regime_dependent) are small chips after the name, on their own line under it (they wrap
there rather than being cut by the name's ellipsis; on a phone the number sits beside
the name so that line gets the full width). Tapping the row opens the detail
page. Sorting and the rank basis note are a single grey line at the top. Unranked rows
are in a collapsed disclosure "Unranked (12)" with the reason in the row.

### Model detail `/models/{id}`
First screen: three stats ("Edge vs market CLV +0.6% (CI -0.2% to +1.4%)", "Backtest
ROI +6.2% on 143 bets", "Paper 5 games · 30 bets · +$27.95"), the status chip and the
gate verdict in words: "Not yet eligible for live: needs 50 paper bets (30 so far) and
a CLV interval above zero." Then disclosures: Summary (editable), Robustness (the
validation and stress tables from step 6, each with a one-line reading above it),
Backtest metrics, Paper results, Assignments, History (train/validate jobs). The
"Reading a model" guide from the README is reduced to one sentence per metric shown
inline as a muted caption under the number.

### Trading `/trading`
First screen: stats "Paper today +$6.57", "Open orders 2", "Exposure $34.12",
"Exchange ok · sim · 2 s ago". Then "Assignments" as rows: game, model, bankroll
available, status chip, "..." (Halt, Fund, Detail). Then disclosures, collapsed by
default: Open orders (count in the header, Cancel all inside), Fills, Markets
(snapshots), Unmatched markets, Exchange (probe buttons, credentials status), Ledger
check. The "New assignment" form is a disclosure at the top, closed by default.
Live rows get a red "LIVE" chip; the live switch stays in Settings.

### Settings `/settings`
Grouped into disclosures: Limits (the money rules), Trading (market source, liquidity
floor, participation, price band), Robustness gates, Fleet (enroll tokens, update
channel, auto-role), Live trading (the typed switch, kill reset), Data (refresh
nflverse). Each group header shows a one-line summary of its current values so the
page reads at a glance when everything is closed. Each field keeps its help text, now
shown as a muted line under the input rather than beside it.

## Tests
- `tests/test_dashboard.py` and `tests/e2e_*.py`: keep every behavioural assertion
  (forms, redirects, flash, auth, banners), replace markup assertions with class-prefix
  or `data-*` lookups so a restyle does not break them again.
- `tests/hw/screenshots.py` takes every page at 390 and 1280, light and dark, and
  asserts: no horizontal overflow, every page's first screen (390x844) contains the
  title and at least one `.stat`, no list row taller than 88 px at 390, every `.chip`
  has text and none in a row title or flag line is cut, every `<details>` has a
  `<summary>` with text, every summary and control is a 44 px target, and every open
  "..." menu item is the topmost thing under its centre (also with the connection lost).
- A row-height audit test renders Models and Trading with 20 rows each and checks the
  page height at 390 px is under 6 screens.

## Out of scope for step 7
New data, new rules, new API endpoints, charts (a later step can add sparklines once
the pages are short), and any change to the worker.
