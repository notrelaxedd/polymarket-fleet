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
- `step4` (ff6ed16), `step5` (6cc3818), `step6a` and `step6b`: stable installs, newest last.
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

### Step 6 Part A (robustness): done, branch `step6a`

Finished on 2026-10-05 by the session that took over (workflow
`tools/workflows/step6a-finish.js`: integrate, three adversarial reviews, a fix pass,
then two clean full-suite runs by the orchestrator). The review found and fixed 15
issues, among them two validation leaks: a backtest of a stored model on
validation-era seasons could overwrite its search-era metrics and clear the overfit
flag (now refused on the host and on the worker), and a validate job was accepted for
models searched on the validation seasons (now refused; models from steps 3 to 5 were
searched on 2010 to the last complete season and must be searched again before they
can be validated). Also: `regime_dependent` no longer fires on every profitable model
(`REGIME_LOSS_SHARE` in `fleet/sim/stress.py`), the pool search no longer hangs when a
worker dies and `Runner.kill()` reaps pool workers, unvalidated lineages are always
unranked, every lineage's status is recomputed at host start (`host/startup.py`), and
the Models table is usable at 1280 px. New e2e coverage: the not-validated chip and a
validate job that ranks the lineage, and the paper CLV interval gate
(`tests/e2e_paper_gate.py`). The screenshot tool needs `playwright==1.56.0` (the
release that matches the sandbox's Chromium build 1194).

### Step 6 Part B: done, branch `step6b`

Built 2026-10-05 by seven builders on disjoint paths (contract in
`tools/workflows/step6b-contract.txt`), then one workflow: integrate; e2e phase
(`tests/e2e_signals.py`, `tests/e2e_sells.py`), screenshots and docs in parallel; five
review lenses (replay, leakage, sells, safety, UI and docs) with one skeptical verifier
per high or medium finding; area fixers; a final verifier. 16 of 17 findings were
confirmed and fixed, among them: the replay read the decision-time bar by its opening
minute (it now uses closed bars only), a fill row could be written before a refused
ledger movement (now one savepoint), a live sell fill after settlement was half-booked,
sim-price replays ranked on the leaderboard (now shown, never ranked), the games feed
injury cutoff followed the current setting rather than the job's (the worker fetches the
feed per decision-minutes cutoff), the QB-change signal was 0 at trade time (fixed), and
the worker dropped `team_game_stats` when caching the games feed. What it contains:
- Snapshot replay backtests (`price_source: snapshots`, `fleet/sim/prices.py`, the shared
  order-book walk `fleet/sim/book.py`), stored in `models.snapshot_metrics` and ranked
  on snapshot CLV after 30 bets (validated lineages only, never on sim prices).
- Signals: QB change and injuries (nflverse `injuries`, `host/ingest_injuries.py`),
  team EPA per game (`host/ingest_pbp.py`, `team_game_stats`), served with the games
  feed; `elo_blend` penalties; the `epa_blend` family (`fleet/models/newton.py`).
- Selling (mark-to-model): `fleet/worker/sell.py`, `host/trading/sells.py`,
  `host/exchange/settle_sells.py`, signed positions with average cost, the `sell`
  ledger kind, sold bets rows, the live gateway's `side_sell`.
Steps 6A and 6B were merged through the branch `merge-6a-6b`; the rule that a lineage
never validated stays unranked (step 6A review) wins over the 6B draft.

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
