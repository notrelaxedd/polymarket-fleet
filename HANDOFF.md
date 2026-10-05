# Handoff: finishing step 6 and building step 7

Written 2026-10-05 for the session that takes over. Read this file first, then the
docs it points at. Everything here is in the repo except the owner's machines and
secrets.

## 1. What this project is

A multi-machine fleet for NFL prediction-market (Polymarket US) model search and
trading. One host (the owner's Windows 11 box, Docker Compose, tailnet-only through
`tailscale serve`) plus old Debian 13 workers (i5, 4 to 12 GB RAM). Postgres is the
single source of truth and the job queue. Workers poll every 5 s and take the roles
idle, backtest, model_search, train and trade. A role change lands within 10 s with
checkpointing; trade workers cancel their orders before switching. One phone-friendly
dashboard controls it all.

Architecture, contracts and rules live in `docs/`:
`DESIGN.md` (overview), `PROTOCOL.md` (worker API and queue), `DASHBOARD.md`,
`MODELS.md` (backtester, search, train), `TRADING.md` (paper trading, limits, ledger,
selling rule), `LIVE.md` (real money), `ROBUSTNESS.md` (step 6 Parts A and B),
`INGAME.md` (step 6 Part C), `UI.md` (step 7). `README.md` has the owner's setup and
per-step test walkthroughs.

## 2. The owner's rules (do not relax any of these)

- NFL first. One trade worker covers several games; each game has its own model and
  bankroll.
- Paper mode is the default. Real money sits behind a global host switch, off at
  install, enabled only by a typed confirmation phrase.
- The host enforces per-game bankroll, max bet and max daily loss; workers only
  request approval. The liquidity floor is settable. Fleet-wide rate limits. Every
  order, fill and price snapshot is logged.
- Polymarket API keys live only on the host, in `exchange.env`, read only by the
  exchange service, never logged, stored, returned by an endpoint or put in `.env`.
  Enroll tokens may be pasted into chat; API keys must not be.
- Only models that pass the backtest and paper thresholds can be assigned real money.
  There is no override.
- Kill switch means cancel open orders only (positions stay).
- All limits and thresholds are adjustable on the dashboard (Settings).
- Free data only. Ask before adding any paid data source ("expand to paid once profit
  comes in").
- No automated capture of licensed broadcasts for in-game data.
- Tests must exist for limit enforcement and the kill switch. Keep dependencies light:
  the worker package `fleet/` is standard-library only (a test enforces it).
- Style: Python 3.11+, type hints, modules under about 300 lines, no em-dashes in any
  file (a test enforces it for templates, CSS and JS; keep it everywhere).
- Build order with a stop after each step so the owner can test. Each step ends with
  tests green, a commit, a push and screenshots for the owner.

## 3. State of the repository

Branches on `origin` (tags do not survive the git proxy; publish branches):
- `step4` (ff6ed16) and `step5` (6cc3818): stable installs. The owner should run
  `step5` until step 6 lands.
- `main`: step 5 final is 4478bb7; the docs commit 6cc3818 adds the selling rule and
  the in-game spec; the commits after that are work-in-progress snapshots of step 6
  Part A, ending with the handoff commit that had one green full-suite run.

Steps 1 to 5 are complete and were verified with two full-suite runs each
(585 tests at step 5). What they contain, briefly:
- Host: FastAPI + psycopg + Jinja2, migrations `host/migrations/0001` to `0005`,
  owner auth from the `Tailscale-User-Login` header, worker auth by lease token.
- Worker agent `fleet/worker/`: heartbeats, leases, JSONB checkpoints, drain within
  3 s, self-update with rollback, memory watchdog, the trade loop.
- Models `fleet/models/` (`elo_blend`), backtester and search `fleet/sim/`,
  walk-forward by season, closing-line fill rule, fractional Kelly.
- Trading `host/trading/` and the exchange process `host/exchange/`: assignments,
  bankrolls, append-only ledger, approval with reason codes, kill switch, paper fill
  simulator, settlement with CLV, scores, eligibility, live adapter behind the switch.

### Step 6 Part A (robustness) is built but not integrated

Three build agents finished on 2026-10-04 and their output is on `main`:
- Worker side: `fleet/sim/{stats,robust,stress,validate,parallel,records}.py`, changes
  to `backtest.py`, `search.py`, `metrics.py`; `fleet/worker/context.py` gained the
  `validate` kind in `CONTEXT_KINDS` (keep it). Its tests
  (`tests/test_stats.py`, `test_stress.py`, `test_validation.py`,
  `test_search_parallel.py`, plus additions in `test_backtest.py`,
  `test_search_train.py`, `test_agent.py`) were reported green three times in
  isolation (182 tests in those files).
- Host side: `host/migrations/0006_robustness.sql` (`models.validation_metrics`,
  `models.stress_metrics`, `lineage_paper_ci`), `host/stats.py`, `host/paper_gate.py`,
  `host/model_validation.py`, `host/model_owner.py`, `host/settings_schema.py`,
  `host/api/robustness.py`, `host/templates/_robustness.html`, the validate job form,
  the Models page columns and chips, settings keys, `tests/hw/seed_step6.py`.
- Docs: `docs/ROBUSTNESS.md`, README "Reading a model" and "How to test step 6A",
  `docs/PROTOCOL.md` and `docs/DASHBOARD.md` additions.

The integration pass was interrupted twice (once by the weekly subagent limit, once
by this handoff). Before handing off, the previous session ran the suite itself on
2026-10-05: 632 tests passed outside the end-to-end test, and the end-to-end test
passed after two stale assertions were updated (`tests/e2e_validation.py` line 128,
a markup check on the Models row; `tests/e2e_trading.py` line 391, which expected
the old `backtest` rank mode where the leaderboard now says `validation`). So the
head of `main` had one green run of all 633 tests. `compileall` is clean. The
integrate agent had also added a "jobs-validate" capture to
`tests/hw/screenshots.py`; whether the screenshot tool passes end to end is recorded
in the commit message of the handoff commit.

Remaining work for Part A, in order:
1. Two more full-suite runs from a clean database to confirm nothing is flaky.
2. Check `tests/e2e_validation.py` against the integrate brief in
   `tools/workflows/step6a-build.js` (a pooled search with a held-out era, the
   single-process rerun giving the same numbers, a validate job on an older model,
   the Models page columns and chips, the stricter gates with a forced pass and a
   forced demotion). Most of it exists; add what is missing.
3. Serial versus parallel is covered by the e2e (workers 2 then workers 1) and by
   `tests/test_search_parallel.py`; confirm the SIGTERM-and-resume case is there.
4. Screenshots: models, model detail (Robustness section), jobs (validate form),
   settings at 390 and 1280, light and dark, with the layout assertions in
   `tests/hw/screenshots.py`.
5. Three adversarial reviews (statistics, parallel search, gates and UI) and a fix
   pass; the lens texts are in `tools/workflows/step6a-build.js`.
6. Two clean full-suite runs by the orchestrator, commit "Step 6 Part A", push,
   publish branch `step6a`, send the owner the screenshots and a short handoff.

### Step 6 Part B: not started

Spec: `docs/ROBUSTNESS.md` Part B (snapshot replay backtests, richer signals,
`epa_blend`) and `docs/TRADING.md` section "Selling (step 6 Part B): mark-to-model".
A build split that keeps paths disjoint:
- Builder "sim" (worker): `fleet/sim/` (price_source `snapshots` in the backtester,
  the decision time, CLV against frozen closing prices, sim exclusion), QB-change and
  injury features in `fleet/sim/data.py`, EPA features, the new family
  `fleet/models/epa_blend.py` registered in `fleet/models/registry.py`, search spaces,
  its tests.
- Builder "host-data": migration `0007_signals.sql` (`injuries`, `team_game_stats`,
  backtest params), `GET /api/v1/data/prices` and the signals served with the games
  feed (`host/api/data.py`, `host/nflverse.py` plus new ingest modules for injuries
  and play-by-play), settings keys (`allow_sim_prices`, `decision_minutes_before_kickoff`),
  the backtest form's `price_source` choice, leaderboard snapshot columns
  (`host/leaderboard.py`, `host/api/dashboard_models.py`, Models templates), docs.
- Builder "trading": migration `0008_sells.sql` (`orders.side`, `bets.result` gains
  `sold`), sells end to end: `fleet/worker/trade.py` proposes sells, `host/trading/`
  approval path for sells (no reservation, no daily-loss check, size within the
  position, one open sell per market, no shorting), `host/exchange/paper.py` sell
  fills, ledger rules for a sell fill, settlement after partial sales,
  `model_scores` including sells, Trading page chips and positions, kill cancels open
  sells, the live gateway `side: SELL`. Tests listed in the TRADING.md section.
Then integrate, review (lenses: replay correctness and CLV sign, signal leakage and
feature timing, sell maths and ledger invariants), fix, verify, commit, publish
`step6b`.

### Step 6 Part C: not started

Spec: `docs/INGAME.md`. ESPN summary endpoint polled every 3 to 5 s per live game
(about 1 request per second per IP, back off on 429 or 403), Yahoo play-by-play as a
cross-check, a measured feed lag against the market, the `ingame_wp` family on
play-by-play, conservative in-game rules, scoring and dashboard. Free data only.
No Polymarket, ESPN or NFL.com host is reachable from the build sandbox, so adapters
are built against recorded fixtures with probe commands the owner runs and pastes.

### Step 7: UI overhaul, not started

Spec: `docs/UI.md`. Build it after Parts B and C, because both add rows and columns
to the pages it redesigns. Keep every behavioural test; replace markup assertions
with class-prefix or `data-*` lookups.

## 4. How the steps were built (the method that worked)

Each step ran as one orchestration workflow, script in `tools/workflows/`:
1. Two to four build agents in parallel, each owning disjoint paths, each handed the
   shared data shapes verbatim (the `SHAPES` constant) so the sides meet.
2. One integrate agent: full suite three times, e2e extension, screenshots, static
   checks, a report.
3. Three adversarial reviewers with different lenses returning structured findings
   (file, line, severity, repro, fix).
4. One fix agent for the verified findings.
5. The orchestrator itself then runs the full suite twice, commits, pushes, publishes
   the `stepN` branch and sends screenshots to the owner.

The scripts contain `<SCRATCHPAD>` where this session's scratch directory was;
replace it with the new session's scratch path and keep `REPO` right. The step 6A
script was last resumed from run `wf_df9f1dfe-d1f`; a new session cannot resume it,
so start a fresh run with the Build phase replaced by the integrate, review and fix
phases (the build output is already on `main`).

Agent prompts that worked well say: read the spec and the code you touch first, only
touch the paths you own, run the named tests, no commits (the orchestrator commits),
report files changed and the pytest summary lines.

## 5. Working in a fresh sandbox

```bash
cd polymarket-fleet
python3 -m venv .venv
.venv/bin/pip install -e '.[host,dev]'
.venv/bin/pip install "playwright==1.56.0" # python package only (pairs with Chromium build 1194), never "playwright install"
# Postgres 16: any local cluster with a superuser works
pg_ctlcluster 16 main start               # the sandbox cluster stops between turns; rerun when "connection refused"
export FLEET_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres   # the default
.venv/bin/python -m pytest tests -q -p no:cacheprovider -o addopts=        # about 5 to 8 minutes
.venv/bin/python -m pytest tests/test_e2e.py -q -p no:cacheprovider -o addopts= -x   # the long one
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers .venv/bin/python tests/hw/screenshots.py <out-dir>
.venv/bin/python -m compileall -q fleet host
```

Sandbox facts learned the hard way:
- Per-test databases are cloned from a migrated template; the template is rebuilt
  when migrations change. If a run dies mid-way, stale `fleet_test_*` databases can be
  dropped by hand.
- The shell's working directory can reset between commands. Use absolute paths and
  `git -C <repo>`.
- No network to Polymarket, ESPN, Yahoo or NFL.com. nflverse (`games.csv`,
  `injuries`, play-by-play CSV.gz) is reachable. The fixture
  `tests/fixtures/games_sample.csv` (2016 to 2025) is the data for tests.
- `git push` of tags is silently dropped; push branches with full refspecs.
- A stop hook asks for commits; clearly labelled "WIP:" snapshots to `main` are fine,
  stable branches (`stepN`) are what the owner installs from.
- Background shell commands of a subagent die when the session is interrupted; a
  waiting monitor then never fires. Prefer foreground runs with a long timeout for
  test suites.
- The subagent weekly limit can stop a workflow mid-way; the journal under the
  workflow's transcript directory records which agents finished.

## 6. The owner's deployment

- Host: Windows 11, Docker Compose from the repo folder (`db`, `host`, `exchange`),
  `tailscale serve --bg --https=443 http://127.0.0.1:8080`, public URL
  `https://cpizzle.taila52b65.ts.net`, `FLEET_OWNER_LOGIN` set to their Google login
  (the check is case-insensitive; the exact address is not in the repo on purpose).
- One Debian 13 worker enrolled and running (installed from the `step4` branch, then
  told to move to `step5`). The owner runs the installer as root via `su -`.
- Time zone America/New_York. Limits are the test numbers; the owner adjusts them in
  Settings.
- Polymarket US API keys: the owner does not have them yet but can get them. When
  they do: `exchange.env` on the host, then `auth-check` and `probe-account` from the
  exchange CLI, outputs pasted into chat for field fixes. The Polymarket US private
  API shapes are unverified; the adapters are defensive and ship with probes
  (`Probe markets` button, `probe-account`).
- The owner wanted to paper trade on real prices on a Sunday: switch
  `market_source` to `polymarket_us`, press "Probe markets", paste the output.
- Live trading is not possible yet by design: no lineage is `live_eligible`, the
  private API is unverified, and the stricter gates arrive with step 6.

## 7. Research notes already settled (do not redo)

- Closing-line backtests show no edge for `elo_blend`; that is expected, and the
  summaries say so honestly. Paper CLV on Polymarket prices is where edge would show.
- Paid feeds: Genius Sports is sportsbook-only revenue share; Sportradar is quote-only
  (estimates 10 to 30 thousand dollars a year); retail feeds run 20 to 30 s behind
  broadcast. ESPN free endpoints are the in-game source (3 to 5 s per live game),
  Yahoo play-by-play as cross-check. NFL.com has no free live feed.
- Video capture of broadcasts was rejected (terms, latency, weight).
- Stocks: the fleet, limits, gates and robustness machinery transfer; data, model
  scoring (returns, not binary outcomes), a broker adapter (Alpaca paper first) and
  mark-to-market settlement would be new. Parked until after step 7.

## 8. Checklist before calling any step done

- Full suite green twice in a row from a clean database.
- `compileall` clean; `fleet/` stdlib-only; no em-dashes.
- e2e extended for the step and green.
- Screenshots at 390 and 1280, light and dark, layout assertions passing.
- Docs updated: the contract docs for the step, README test walkthrough, the build
  order list.
- Commit, push `main`, publish the `stepN` branch, send the owner screenshots and a
  short "how to test" note.
