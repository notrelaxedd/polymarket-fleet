export const meta = {
  name: 'fleet-step4-build',
  description: 'Step 4: paper trading for NFL. Approval limits, exchange process with adapters and settlement, trade worker, trading dashboard, docs; integrate on the synthetic market source, review, fix',
  phases: [
    { title: 'Build', detail: 'limits/host API, exchange process, trade worker, dashboard, docs in parallel' },
    { title: 'Integrate', detail: 'full suite, e2e paper flow through the live agent on the sim source, screenshots' },
    { title: 'Review', detail: 'trading safety, exchange and settlement correctness, worker tick + UI' },
    { title: 'Fix', detail: 'apply findings, rerun suite' },
  ],
}

const REPO = '/home/user/polymarket-fleet'
const SHOTS = '<SCRATCHPAD>/screenshots-step4'
const COMMON = `
Repository: ${REPO} (git; steps 1-3 committed, 297 tests green; do NOT commit or push, the orchestrator does that).
Python: ${REPO}/.venv/bin/python. Run tests from ${REPO} as .venv/bin/python -m pytest <files> -q -p no:cacheprovider -o addopts=. Postgres 16 at postgresql://postgres:postgres@127.0.0.1:5432/postgres (if "connection refused", run: pg_ctlcluster 16 main start). Tests get per-test databases from a migrated template (tests/conftest.py); the template is rebuilt automatically when migrations change.
Read first, completely: ${REPO}/docs/TRADING.md (the step 4 spec), the "Step 4 additions" at the end of ${REPO}/docs/PROTOCOL.md, ${REPO}/host/migrations/0004_trading.sql (the schema, already applied by the test template), ${REPO}/host/trading/ledger.py and ${REPO}/host/trading/orders.py (shared money and order-state primitives: USE them, do not write your own ledger or status updates), ${REPO}/host/settings.py (new keys), and the step 1-3 code you touch. docs/MODELS.md and docs/DASHBOARD.md for the model interface and UI rules.
Style: Python 3.11+, type hints, modules under ~300 lines, plain docstrings, no em-dashes anywhere. fleet/ must stay standard-library only.
No network in this sandbox: every Polymarket and ESPN host is blocked; never make a test depend on the network; use fixture JSON files for external payloads.
Only touch the paths you own; other agents are writing the other paths concurrently. Where you depend on another agent's function, use exactly the signature given in your prompt and stub nothing in their paths.
`

// Cross-agent signatures (the integrator reconciles any drift)
const SIGNATURES = `
Cross-agent function signatures (implement or call exactly these):
- host/trading/assignments.py (owned by build:limits):
  create_assignment(conn, game_id: str, model_id, mode: str, bankroll_cents: int, actor: str | None, max_bet_cents: int | None = None) -> dict  (row + "bankroll" + "job_id"); raises BadRequest/Conflict per docs/TRADING.md rules; also inserts the trade job (kind "trade", params {"assignment_id": ...}, max_expiries NULL, idempotency_key "assignment:<id>").
  halt_assignment(conn, assignment_id, actor, reason: str) -> dict (cancels its open orders via orders.cancel_order, status halted, audit)
  activate_assignment(conn, assignment_id, actor) -> dict; activate_all_paper(conn, actor) -> int
  list_assignments(conn, status: str | None = None) -> list[dict] (joined with bankroll, game, model summary, open order count)
  trade_state(conn, worker_id: str) -> dict  (the GET /api/v1/trade/state body)
- host/trading/limits.py (owned by build:limits):
  approve_order(conn, worker: dict, body: dict) -> dict {"status": "approved"|"rejected", "order_id", "reason"}
  losses_today(conn, mode: str, now=None) -> dict {"realized_cents", "unrealized_cents", "losses_cents"}
  positions(conn, assignment_id) -> list[dict] {market_id, side, size, avg_price, basis_cents}
- host/trading/kill.py extension lives in host/kill.py (owned by build:limits): set_kill(conn, actor) now cancels/halts per docs/TRADING.md; new cancel_all(conn, actor, mode: str | None = None, reason: str = "cancel-all") -> dict {"cancelled": n, "requested": m}.
- host/exchange/settle.py (owned by build:exchange): settle_game(conn, game_id: str, actor: str = "settle") -> dict; simulate_final(conn, game_id, home_score, away_score, actor) -> dict (refused unless market_source == "sim" or FLEET_DEV).
- host/exchange/snapshots.py: latest_snapshot(conn, market_id) -> dict | None; record_snapshot(conn, market_id, book: Book, now=None) -> dict.
- host/exchange/probe.py: probe_markets(conn) -> dict {"source", "url", "status", "payload" (truncated 64 KiB), "error"}.
- host/exchange/state.py: read_state(conn) -> dict; heartbeat(conn, source: str, error: str | None = None).
- host/pnl.py (owned by build:dashboard): pnl(conn, now=None) -> {"today_cents", "all_time_cents", "by_worker": {id: cents}, "by_mode": {"paper": {"today_cents", "all_time_cents"}, "live": {...}}}.
- fleet/worker/trade.py (owned by build:worker): TradeLoop(agent) with tick(state) -> list of requests; pure helpers plan_proposals(assignment, settings) -> list[dict] and stale_orders(assignment, settings) -> list[order ids], unit-testable without HTTP.
`

const LIMITS_PROMPT = `You are building the HOST TRADING CORE of step 4: assignments, order approval with every limit, the kill switch cancel-all, trade worker routes, owner trading routes, and the tests the owner demanded for limits and the kill switch. ${COMMON}
${SIGNATURES}
You own: ${REPO}/host/trading/assignments.py, ${REPO}/host/trading/limits.py, ${REPO}/host/trading/positions.py (if you split), ${REPO}/host/trading/views.py (JSON views for assignments/orders/fills/markets used by the owner API; the dashboard agent will call these), ${REPO}/host/api/trade.py (worker routes), ${REPO}/host/api/owner_trading.py (owner JSON routes), ${REPO}/host/kill.py, ${REPO}/host/heartbeat.py, ${REPO}/host/leases.py (trade claim LIMIT n), ${REPO}/host/loop.py (orphan rule), ${REPO}/host/cli.py (new subcommands), ${REPO}/host/api/app.py (mount the two new routers only), ${REPO}/tests/test_limits.py, ${REPO}/tests/test_kill_switch.py, ${REPO}/tests/test_trade_api.py, ${REPO}/tests/test_queue.py (trade claim tests), ${REPO}/tests/conftest.py (additions only: helpers to make a game, model, market, snapshot, assignment, approved order), ${REPO}/docs/PROTOCOL.md (sync, mark "(changed: reason)"). Do not touch host/exchange, host/pnl.py, host/leaderboard.py, templates, static, fleet/, README, docs/TRADING.md (report spec problems instead).

Build exactly what docs/TRADING.md and PROTOCOL.md step 4 say:
1. Trade claims: heartbeat want_jobs (int) for the trade role, claim LIMIT n, refused under kill; trade jobs never fail by expiry; a trade job's checkpoint is unused.
2. assignments.py per the signatures; rules: non-retired lineage, paper needs nothing else, live needs live_enabled + live_eligible + exchange auth_ok, max_paper_models_per_game, one live per game (the partial unique indexes give the DB guarantee; map the IntegrityError to a 409 with a clear message), bankroll via ledger.create_bankroll, trade job insert, audit rows; halt cancels open orders; settle hooks are called by the exchange agent's settle.py (do not implement settlement).
3. limits.approve_order: every check in the exact order of docs/TRADING.md with the stable reason codes (duplicate, killed, lease, assignment, market, kickoff, mode, stale_book, liquidity, participation, price_band, max_bet, bankroll, daily_loss, exposure, buying_power); advisory lock per mode; bankroll FOR UPDATE; cost = size * (price + fee) * 100 computed on the host from settings.fee_model (ignore any cost the worker sends); reserve via ledger; orders row approved or rejected (rejected rows carry reject_reason and an order_events row); daily loss uses ledger.realized_between over the owner's day (settings.tz) plus unrealized = mark-to-mid of open positions using the latest snapshot; the live daily trip (live_enabled=false, live assignments halted, live orders cancelled, audit daily_loss_trip) and the paper behaviour (keep rejecting, assignments stay active).
4. Kill: set_kill extended per docs/TRADING.md (one transaction, both locks, the scoped CASE update, releases for rows that became cancelled, assignments halted, audit); cancel_all; reset unchanged plus activate_all_paper exposed to the owner.
5. Worker routes: GET /api/v1/trade/state, POST /api/v1/orders/request, POST /api/v1/orders/{id}/cancel, POST /api/v1/trade/release (paper cancels immediately; live cancel_requested with a bounded 3 s wait polling for zero open; releases the jobs to queued with reason drain). Owner routes: assignments CRUD + halt/activate/settle(calls host.exchange.settle.settle_game if the game is final, else 409)/activate-paper, orders list + cancel, fills, markets?unmatched=1 + link, exchange state (read host.exchange.state.read_state; if that module is missing at test time, read the exchange_state row directly), probe (call host.exchange.probe.probe_markets; wrap ImportError into a 503 "exchange module not available"), cancel-all. CLI: assign, assignments, orders, cancel-all, ledger-check, exchange-state.
6. Orphan rule in host/loop.py: workers silent for orphan_cancel_after_s with active orders get them cancelled (paper now, live cancel_requested).
7. Tests: tests/test_limits.py with EVERY test name listed in docs/TRADING.md "Tests that must exist" (same names), tests/test_kill_switch.py extended with every listed kill test that does not need the exchange process (the executor/exchange ones may be written against a FakeGateway you define in conftest if cheap; otherwise leave a clearly named skip with reason "needs exchange agent" that the integrator will fill), tests/test_trade_api.py (state payload shape, request/approve/reject path through HTTP with the lease fence, cancel, release handshake, want_jobs claims up to the slots, kill refuses trade claims), test_queue.py additions. Concurrency tests use real threads with separate connections.
Run only the tests you own plus tests/test_ledger.py, tests/test_workers_api.py, tests/test_owner_api.py three times. Final report: files written, summary lines, deviations, spec problems found.`

const EXCHANGE_PROMPT = `You are building the EXCHANGE PROCESS of step 4: market sources and mapping, price snapshots, the executor outbox, the paper fill simulator, scores, settlement with CLV scoring and the paper eligibility gate, retention, rate limiting, and the process heartbeat. ${COMMON}
${SIGNATURES}
You own: ${REPO}/host/exchange/** (new package: main.py, state.py, adapters/{__init__,base,sim,polymarket_us,polymarket_clob,teams}.py, mapping.py, snapshots.py, executor.py, paper.py, scores.py, settle.py, retention.py, ratelimit.py, probe.py, cli.py), ${REPO}/host/eligibility.py (add the paper gate and demotion), ${REPO}/tests/test_exchange.py, ${REPO}/tests/test_paper_fills.py, ${REPO}/tests/test_settlement.py, ${REPO}/tests/test_adapters.py, ${REPO}/tests/test_scores.py, ${REPO}/tests/fixtures/*.json (new fixture payloads), ${REPO}/Dockerfile (only if the exchange entry point needs something). Do not touch host/trading/assignments.py, host/trading/limits.py, host/kill.py, host/api/*, templates, fleet/, README, docs/TRADING.md (report spec problems instead).

Build exactly what docs/TRADING.md says:
1. adapters/base.py: MarketInfo, Book, MarketSource, OrderGateway (place/cancel/open_orders/fills/balance raising NotConfigured in step 4 for live), PaperGateway (place marks open; fills come from paper.py). adapters/sim.py: deterministic synthetic markets from the games table (two per game, seeded random walk per minute, 5 levels per side, liquidity in the thousands), covering games within market_lookahead_days with both moneylines. adapters/polymarket_us.py and adapters/polymarket_clob.py: public reads only, paths/fields from settings.market_source_config, defensive parsing (every field optional, unknown shapes logged at DEBUG with the raw payload truncated), a probe function returning the raw payload. adapters/teams.py: one alias table (32 teams: city, nickname, full name, code, old codes OAK/SD/STL and common variants) with resolve(text) -> code | None.
2. mapping.py: map MarketInfo to games (same two teams, kickoff within 36 h or same gameday), upsert markets rows (platform, market_ref unique), mapping_confidence and mapping_confirmed per spec, never changing a confirmed mapping automatically.
3. snapshots.py: poller with the two cadences, liquidity within 5 cents of the touch on both sides, best bid/ask mirrored on markets, closing_price frozen at kickoff from the last pre-kickoff snapshot (fallback last snapshot), record_snapshot/latest_snapshot. retention.py: delete raw rows older than snapshot_retention_days after rolling up 1-minute bars; idempotent; closing prices and bets untouched.
4. executor.py: the 250 ms outbox (approved -> submitting -> place -> open; timeout -> stays submitting, reconcile by client id, never resubmit blind; cancel_requested -> gateway cancel with retry 1,2,4,8 s; GTD expiry -> expired with release) using host.trading.orders for every transition; honour kill (never submit while killed; an approved row found under kill is cancelled with release). paper.py: fill simulation per spec (walk ask levels <= order price, participation per level per snapshot, fee from fee_model, resting bids fill when a later ask crosses) calling orders.record_fill.
5. scores.py: ESPN scoreboard parser (fixture-driven; completed games -> game_id by teams + date via teams.py; updates games scores/status final, raw.score_source espn); nflverse confirmation is the existing refresh. settle.py: settle_game per spec (resolve markets, settle positions with ledger.settle, cancel open orders with release, bets rows with entry VWAP/fee/cost/my_p/market_p/edge/stake/closing_price/clv/result/pnl, model_scores upsert, assignment settled, trade job succeeded with a score summary via the queue functions, then eligibility recompute); simulate_final per spec. eligibility.py: paper_ok -> live_eligible on pooled lineage paper scores per thresholds_paper, demotion with halting live assignments; keep the step 3 backtest gate.
6. ratelimit.py: token buckets per category from settings.rate_limits with priority and the 429 halving. state.py: heartbeat every 5 s into exchange_state (market_source, last_error). main.py: the loop (heartbeat, discover markets every 5 min, snapshots on cadence, executor, paper fills, scores on game days, settlement every 30 s, retention nightly, orphan-safe) resilient to exceptions (log, continue), plus run_once(pool) for tests; cli.py: simulate-final, probe, run-once.
7. Tests: test_adapters.py (sim determinism and shape; polymarket_us/clob parsers on fixture JSON incl. malformed/missing fields; teams resolve), test_exchange.py (mapping confidence rules; snapshot cadence and liquidity; closing price freeze; retention and bars; executor outbox incl. kill race and GTD expiry; rate limiter; heartbeat/last_error), test_paper_fills.py (partial fills across snapshots, participation cap, resting bid crossing, fee maths, ledger invariants after every fill via ledger.replay_problems), test_settlement.py (win/loss/push settlement, CLV sign and closing price, bets rows, model_scores, trade job completion, paper gate promotion and demotion), test_scores.py (ESPN fixture parse, mapping to game ids, no change when not completed).
Run only your own tests plus tests/test_ledger.py three times. Final report: files written, summary lines, the exact URL paths and field names you ASSUMED for polymarket_us and polymarket_clob (so the owner can verify), deviations, spec problems.`

const WORKER_PROMPT = `You are building the TRADE WORKER of step 4: the trade role in the stdlib-only agent. ${COMMON}
${SIGNATURES}
You own: ${REPO}/fleet/worker/trade.py (new), ${REPO}/fleet/worker/agent.py, ${REPO}/fleet/worker/config.py, ${REPO}/tests/fake_host.py, ${REPO}/tests/test_trade_worker.py (new), ${REPO}/tests/test_agent.py. Do not touch host/, fleet/models, fleet/sim (you may import them), other tests, docs.

Build exactly what docs/TRADING.md "Trade worker tick" and PROTOCOL.md step 4 say:
1. Trade role in the agent: heartbeat want_jobs = trade_max_games (from settings in the state payload, default 6) minus held trade jobs; claimed trade jobs do not start runners; the agent runs TradeLoop.tick every trade_tick_s (from the state payload) in the main loop; trade jobs are renewed like any lease (jobs[] in heartbeats); lost[] drops them; preempt/cancel releases them through the release handshake.
2. fleet/worker/trade.py: TradeLoop with pure helpers plan_proposals(assignment, settings) -> list[dict] (edge maths exactly as the spec: devig of mids for market_p_home, model.predict via fleet.models.registry from the artifact in the payload (cache by model id), per-side price/fee/cost/edge, Kelly stake in cents, size = floor(stake / (cost * 100)), min_size, one open order per market, client_request_id = sha256(assignment|market|snapshot_id|price|size)[:32], rationale string) and stale_orders(assignment, settings) -> ids whose edge at the current ask is below 0; tick(state) posts requests to POST /api/v1/orders/request and cancels via POST /api/v1/orders/{id}/cancel; proposes nothing under kill, when halted, after kickoff with trade_pregame_only, or when the market is below_floor.
3. Role change away from trade: call POST /api/v1/trade/release with the held trade jobs before the ack heartbeat (bounded 4 s; on failure retry once, then proceed and log); under kill keep ticking without proposals; status.json shows trade assignments and last tick summary.
4. tests/fake_host.py: trade endpoints (state from an in-memory assignment store with markets/snapshots/bankroll, request with a simple approval that reserves and records orders, cancel, release that cancels and releases jobs), controls to set snapshots/asks, set kill, halt an assignment, set kickoff in the past.
5. Tests: test_trade_worker.py: plan_proposals maths against hand-computed numbers (devig, fee, edge, Kelly stake and cap, size floor, min_size, no proposal below min_edge, one per market, client id stable), stale_orders; test_agent.py additions: trade worker claims up to the slots, ticks and proposes, cancels stale orders, stops proposing under kill, role change away from trade calls release before the ack and the ack carries no trade jobs, kickoff past -> no proposals, preempted trade job is released.
Run your tests three times. Final report: files written, summary lines, deviations.`

const DASH_PROMPT = `You are building the TRADING DASHBOARD of step 4: the /trading page, real P&L, leaderboard paper columns, Assign flow, banners, settings group, and screenshots tooling. ${COMMON}
${SIGNATURES}
You own: ${REPO}/host/api/dashboard_trading.py (new), ${REPO}/host/api/dashboard.py, ${REPO}/host/api/dashboard_forms.py, ${REPO}/host/api/dashboard_models.py, ${REPO}/host/api/job_forms.py, ${REPO}/host/templates/**, ${REPO}/host/static/**, ${REPO}/host/web.py, ${REPO}/host/pnl.py, ${REPO}/host/leaderboard.py, ${REPO}/host/views.py, ${REPO}/host/settings_forms.py, ${REPO}/tests/test_dashboard.py, ${REPO}/tests/test_style.py, ${REPO}/tests/hw/**, ${REPO}/docs/DASHBOARD.md. Do not touch host/trading/*, host/exchange/*, host/kill.py, host/api/trade.py, host/api/owner_trading.py, fleet/, README.

Build per docs/TRADING.md "Dashboard additions" and docs/DASHBOARD.md style (phone first, no JS framework, forms work without JS): the /trading page and its fragment (assignments, create form, open orders with cancel, recent orders with reject reason and rationale, fills, unmatched markets with link form, snapshot ages, exchange state, ledger status, "Activate all paper"); form handlers call host.trading.assignments functions and host.trading.orders.cancel_order with the signatures above (and host.kill.cancel_all); real pnl.py (bets settled today in the owner's tz plus mark-to-mid of open positions from the latest snapshots; per worker from orders.worker_id; per mode); top bar per-mode P&L and the two banners (EXCHANGE DOWN after 15 s when orders are open or kill is on; N assignments unattended when a trade job is queued > 60 s); fleet cards' today P&L; Models page Assign enabled (opens /trading#assign prefilled with the model); leaderboard paper columns and paper ranking rule from docs/TRADING.md; Settings "Trading" group with every new key from settings.py (market_source as a select; market_source_config as a JSON textarea; thresholds_paper fields); add docs/DASHBOARD.md section for /trading. Tests in test_dashboard.py for every page/form (seed rows directly in SQL where the other agents' functions are not yet available; the integrator reconciles), pnl maths in a small test, phone layout assertions as before; extend tests/hw/screenshots.py + seed with trading data (/trading at 390 and 1280, light and dark, plus fleet with P&L and models with paper columns).
Run only your own tests three times. Final report: files written, summary lines, deviations.`

const DOCS_PROMPT = `You are writing docs for step 4 of a multi-machine fleet. ${COMMON}
You own: ${REPO}/README.md and ${REPO}/docs/DESIGN.md only.
1. README.md: host setup gains the exchange service (docker compose up -d now starts db, host and exchange; exchange.env is optional until step 5 and must be root-only); a "How to test step 4" section: set Settings > Trading > market source to sim (default), ingest games, create a paper assignment from the Models page (Assign) or /trading for an upcoming game, put a worker in the trade role, watch /trading: orders proposed with rationale, approved or rejected with reasons, fills arriving from the simulated book; set paper max daily loss to $1 and see daily_loss rejections; press KILL and see every paper order cancelled within 2 s and assignments halted; reset with RESUME and "Activate all paper"; switch the trade worker's role and see its orders cancelled first; simulate a final with docker compose exec exchange python -m host.exchange.cli simulate-final <game_id> --home 24 --away 20 and see bets rows with CLV, the model's paper line on the Models page, today's P&L on the card and top bar; then switch market source to polymarket_us or polymarket_clob on the host (which has internet), press "Probe" on /trading and paste the output back if markets do not appear. An honest note: the Polymarket US and CLOB readers were written without access to the docs and may need field fixes from the probe output; the private order endpoints arrive in step 5. Build order checklist: step 4 done. No em-dashes.
2. docs/DESIGN.md: Trading sections reference docs/TRADING.md; step 4 build-order row done; note the two-process host (fleet-host, fleet-exchange) in compose.
Final report: files written and assumptions.`

const INTEGRATE_PROMPT = (reports) => `You are integrating step 4 (paper trading) after five agents built it in parallel (reports below). ${COMMON}
${SIGNATURES}
You own everything under ${REPO}. docs/TRADING.md and docs/PROTOCOL.md are the spec; when code and spec disagree fix whichever is wrong and say so. Reconcile signature drift between agents first (grep the call sites).
1. Full suite three times: .venv/bin/python -m pytest tests -q -p no:cacheprovider -o addopts=. Fix every failure in any file; fill any "needs exchange agent" skips in test_kill_switch.py with real tests against the exchange executor.
2. Extend tests/test_e2e.py with a step 4 phase (real uvicorn host + real agent + exchange loop run in a thread via host.exchange.main.run_once or a LoopThread, market_source sim): insert a scheduled game 2 days ahead with both moneylines (from the fixture, shifted); discover sim markets and record snapshots; create a paper assignment for a model from the step 3 phase; put the worker in the trade role -> it claims the trade job, ticks, proposes; the host approves at least one order (set min_edge low enough, e.g. 0.0, or seed the sim mid away from the model's p so an edge exists) and rejects one with a reason (lower max_bet to force it); paper fills arrive after new snapshots; ledger replay has no problems; kill cancels every paper order within 2 s and halts the assignment; RESUME + activate-all-paper; switch the worker's role to idle -> release cancels its open orders first and the trade job returns to queued; put it back in trade -> it reclaims; simulate-final -> bets rows with clv and result, model_scores row, assignment settled, trade job succeeded, /api/pnl today reflects the pnl, leaderboard shows the paper columns, /trading renders everything. Keep the whole e2e under ~200 s.
3. Screenshots: extend tests/hw/screenshots.py + seed so /trading, fleet (with P&L), models (paper columns) and settings (Trading group) are captured at 390x844 and 1280x800, light and dark, into ${SHOTS}/; keep the layout assertions.
4. compileall on fleet/ and host/; stdlib-only check on fleet/; no em-dashes.
Final report: files changed (one line each), three pytest summary lines, PNG list, the list of assumed external API paths/fields (from the exchange report), remaining known issues.
BUILD REPORTS:
${reports}`

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    findings: { type: 'array', items: { type: 'object', properties: {
      severity: { type: 'string', enum: ['high', 'medium', 'low'] },
      file: { type: 'string' }, line: { type: 'integer' },
      summary: { type: 'string' }, failure_scenario: { type: 'string' }, suggested_fix: { type: 'string' },
      verified_by: { type: 'string' },
    }, required: ['severity', 'file', 'line', 'summary', 'failure_scenario', 'suggested_fix', 'verified_by'] } },
    overall: { type: 'string' },
  },
  required: ['findings', 'overall'],
}

const lenses = [
  { key: 'safety', text: 'TRADING SAFETY: can a worker ever get an order approved outside the limits? Attack the approval transaction (lock scope, bankroll FOR UPDATE, daily-loss computation across the owner-tz day boundary and across concurrent requests on different games, unrealized mark-to-mid, max_bet counting open orders, the cost computed on the host, participation vs depth at or better than price, stale snapshot citations, lease fence and preempted jobs, assignment halted mid-request, kickoff cutoff), the kill switch (atomicity, scoped CASE update, releases, halted assignments, executor race, exchange down, reset semantics, activate-all-paper), the release handshake and orphan rule, ledger invariants after every path (run ledger.replay_problems in scripts), the live daily trip, and the one-live-per-game rule. Reproduce with threads and scripts under <SCRATCHPAD>/review4-safety/ before reporting.' },
  { key: 'exchange', text: 'EXCHANGE AND SETTLEMENT CORRECTNESS: snapshot poller cadence and liquidity maths, closing price freeze timing (strictly before kickoff; what if no pre-kickoff snapshot), paper fill simulator realism and bugs (fills before the order existed, fills at better prices, participation per level per snapshot, resting bids, fees rounding, double-counting across snapshots), executor outbox races (approved under kill, submitting timeouts, reconciliation by client id, GTD expiry with release), settlement (win/loss/push, positions basis vs fills, CLV sign and the held side, bets rows idempotent on re-run, model_scores upsert, trade job completion, eligibility promotion/demotion with pooled lineage stats and min_days), scores parser mapping to game ids (same teams twice a season, date boundaries), retention never deleting what settlement still needs, rate limiter maths, exchange heartbeat and last_error, the sim adapter determinism, and the defensive parsing of the two Polymarket readers against malformed payloads. Verify with scripts under <SCRATCHPAD>/review4-exchange/.' },
  { key: 'worker-ui', text: `WORKER TICK MATHS AND UI: fleet/worker/trade.py edge/Kelly/size/fee maths against docs/TRADING.md by hand, devig with one or two markets, no double proposals, cancel of stale orders, behaviour under kill/halt/kickoff/below_floor, client_request_id stability, release-before-ack ordering, status.json; then the UI: look at every PNG under ${SHOTS}/ (Read them) and the templates: is /trading simple and clean on a phone, are reject reasons and rationale readable, can the owner halt/activate/cancel with one tap, are banners unmistakable, do P&L numbers agree between fleet cards, top bar and /trading (sum rule), are the Settings trading fields labelled with units, does Assign prefill correctly, any contrast or overflow problems. Report concrete fixes.` },
]
const reviewPrompt = (lens, report) => `You are an adversarial reviewer of step 4 (paper trading) of a multi-machine fleet. ${COMMON}
Lens: ${lens.text}
Integration report:
${report}
Report only findings you verified; give a concrete failure scenario and a minimal fix each. Severity: high = money can move outside the limits, the kill switch can fail, the ledger can go wrong, or results are misstated; medium = wrong behaviour under realistic use or a clear usability failure; low = hygiene. Do NOT modify repository files.`

const fixPrompt = (findings) => `You are fixing verified review findings in step 4 (paper trading) of a multi-machine fleet. ${COMMON}
You own everything under ${REPO}. Apply every HIGH and MEDIUM finding (and cheap LOW ones), keep docs/TRADING.md, docs/PROTOCOL.md and docs/DASHBOARD.md in sync, add a regression test for each HIGH and MEDIUM. If a finding is wrong, say why and skip it. Then: full suite three times (.venv/bin/python -m pytest tests -q -p no:cacheprovider -o addopts=), compileall on fleet/ and host/, stdlib-only check, and regenerate the screenshots with .venv/bin/python tests/hw/screenshots.py ${SHOTS} if any template/CSS/JS changed. Write the three pytest summary lines into ${REPO}/.fix4_runs.txt as you go (one line per run) so the orchestrator can read them even if your report is cut short.
Final report: per finding fixed/skipped(reason); files changed; three pytest summary lines; whether screenshots were regenerated.
FINDINGS:
${JSON.stringify(findings, null, 1)}`

phase('Build')
log('Building limits/API, exchange, trade worker, dashboard, docs in parallel')
const built = await parallel([
  () => agent(LIMITS_PROMPT, { label: 'build:limits', phase: 'Build', effort: 'high' }),
  () => agent(EXCHANGE_PROMPT, { label: 'build:exchange', phase: 'Build', effort: 'high' }),
  () => agent(WORKER_PROMPT, { label: 'build:worker', phase: 'Build', effort: 'high' }),
  () => agent(DASH_PROMPT, { label: 'build:dashboard', phase: 'Build', effort: 'high' }),
  () => agent(DOCS_PROMPT, { label: 'build:docs', phase: 'Build', model: 'sonnet', effort: 'medium' }),
])
const reports = built.map((r, i) => `=== ${['limits', 'exchange', 'worker', 'dashboard', 'docs'][i]} ===\n${r || '(no report)'}`).join('\n\n')

phase('Integrate')
const integrateReport = (await agent(INTEGRATE_PROMPT(reports), { label: 'integrate', phase: 'Integrate', effort: 'high' })) || '(no report)'

phase('Review')
const reviews = (await parallel(lenses.map(l => () =>
  agent(reviewPrompt(l, integrateReport), { label: `review:${l.key}`, phase: 'Review', schema: REVIEW_SCHEMA, model: 'opus', effort: 'high' })
))).filter(Boolean)
const findings = reviews.flatMap((r, i) => r.findings.map(f => ({ ...f, lens: lenses[i] ? lenses[i].key : 'unknown' })))
log(`${findings.length} findings (${findings.filter(f => f.severity !== 'low').length} high/medium)`)

phase('Fix')
let fixReport = '(no findings)'
if (findings.length > 0) {
  fixReport = (await agent(fixPrompt(findings), { label: 'fix', phase: 'Fix', effort: 'high' })) || '(no report)'
}
return { reports, integrateReport, reviews, fixReport }