export const meta = {
  name: 'fleet-step6b-integrate',
  description: 'Step 6 Part B integrate, e2e/screenshots/docs, five-lens adversarial review with per-finding verification, area fixes, final verification (wip-6b worktree)',
  phases: [
    { title: 'Integrate', detail: 'reconcile builders, suite green' },
    { title: 'Extend', detail: 'e2e phase, screenshots, docs in parallel' },
    { title: 'Review', detail: 'five lenses' },
    { title: 'Verify', detail: 'one skeptic per high/medium finding' },
    { title: 'Fix', detail: 'worker, host-data, trading areas in parallel' },
    { title: 'Final', detail: 'full suite x3, screenshots, static checks' },
  ],
}

const REPO = '/home/user/pf-6b'
const SCRATCH = '<SCRATCHPAD>'
const CONTRACT = `${SCRATCH}/shapes6b.txt`
const REPORTS = `${SCRATCH}/reports6b.txt`
const SHOTS = `${SCRATCH}/screenshots-step6b`
const COMMON = `
Repository: ${REPO} (git worktree, branch wip-6b, its own venv ${REPO}/.venv; work only inside ${REPO}; never touch /home/user/polymarket-fleet or /home/user/pf-6c). Do NOT commit or push; the orchestrator does.
Project: multi-machine fleet for NFL Polymarket model search and paper/live trading; host/ = FastAPI + psycopg + Jinja2 + Postgres; fleet/ = stdlib-only worker package (CI enforces it). Step 6 Part B adds snapshot replay backtests, QB/injury/EPA signals, the epa_blend family and selling (specs: docs/ROBUSTNESS.md Part B, docs/TRADING.md "Selling"). Seven builders built it in parallel; the shared contract is ${CONTRACT} and their reports are in ${REPORTS}.
Python: ${REPO}/.venv/bin/python. Tests from ${REPO}: .venv/bin/python -m pytest <files> -q -p no:cacheprovider -o addopts= . Full suite: .venv/bin/python -m pytest tests -q -p no:cacheprovider -o addopts= (8 to 12 minutes; run it in the FOREGROUND with a long timeout, never in the background). Postgres 16 at postgresql://postgres:postgres@127.0.0.1:5432/postgres (export FLEET_TEST_DATABASE_URL to it; "connection refused" -> pg_ctlcluster 16 main start). Screenshots: PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers .venv/bin/python tests/hw/screenshots.py <out-dir> (never "playwright install").
The machine has 4 cores and other agents may be running tests; a timing test that fails once under load and passes alone is not a bug in the code under test, but never weaken a behavioural assertion to make it pass; if a timing bound is genuinely too tight for a loaded 4-core box, widen it modestly and say so.
Rules: HANDOFF.md section 2 (owner's rules: paper default, host-enforced limits, no gate override, kill cancels orders only, keys never logged, free data only, fleet/ stdlib-only, tests for limits and kill). Style: Python 3.11+, type hints, modules under about 300 lines (split rather than grow), no em-dashes anywhere.
Orchestrator decisions on the builders' open questions (apply them): (1) a snapshot backtest with no explicit last season replays through the latest season present (the season in progress included, games already played are scored), not only the last complete one; (2) snapshot results carry a top-level "avg_clv" (plain mean CLV over bets); (3) elo_blend penalties stay applied in fit, predict and observe alike; (4) sells may coexist with an open buy on the same market; (5) sell bets rows keep stake 0; a fully sold buy row keeps the market outcome as result with pnl covering the contracts still held (zero) minus its fee; (6) unrealized P&L at the bid deducts the taker fee a sale would pay; (7) snapshot-ranked lineages need no validation to rank (paper precedent); eligibility does not use snapshot metrics in this step.
`

const INTEGRATE = `You are the INTEGRATOR of step 6 Part B. ${COMMON}
You own everything under ${REPO}. Read the contract and all seven reports first, then:
1. Apply every "NEEDS FROM OTHERS" item in the reports that is still open (for example: host/jobparams.py price_source and the copied settings if not done; host/trading/state.py merging host.signals.game_signals; tests renamed by the contract such as avg_price -> avg_cost in tests/test_limits.py and tests/test_trade_worker.py and the games feed shape in tests/test_models_api.py; the numeric settings inputs test; dashboard counts changed by new sections; search_space BOUNDS for the elo_blend penalties with the tests/test_stress.py guard; an epa_blend short_params label; docs/PROTOCOL.md order_side and state fields; docs/LIVE.md side_sell). Check each claim against the code before acting.
2. Apply the orchestrator decisions above where the code does not already follow them.
3. Grep for shape drift between fleet/ and host/ (field names in the contract: signals keys, team_game_stats fields, prices feed fields, price_source, snapshot_metrics, order_side, basis_cents, avg_cost, side_sell) and fix mismatches.
4. Full suite until green, then twice more (three green runs). Fix every failure in any file.
5. compileall -q fleet host; the stdlib-only check from .github/workflows/ci.yml on fleet/; no em-dashes (U+2014) anywhere outside .venv and .git; list modules over 300 lines that this step created or grew and split the new ones that are clearly over.
Final report: what you changed (one line per file), the three pytest summary lines, remaining known issues.`

const E2E = (rep) => `You are the E2E writer of step 6 Part B. ${COMMON}
Integration report:
${rep}
You own: new tests/e2e_signals.py and the call into it from tests/test_e2e.py (follow how tests/e2e_validation.py's phase_validation is wired, placed after the trading phase or wherever its preconditions hold), plus tests/fake_host.py only if strictly needed. Write phase_signals(...) that drives the REAL agent and host (as the other e2e phases do):
- a snapshot-replay backtest: seed confirmed markets with platform "sim" and minute bars around the decision time for a few fixture games (insert price_bars/price_snapshots and markets rows directly, with closing prices), first with allow_sim_prices false (the job succeeds with those games unscored or is refused per the built behaviour: assert what the code does and that no sim price was used), then true: the model's lineage gets snapshot_metrics with price_source "snapshots", CLV values with the right sign for a constructed rising close, backtest_metrics unchanged; the Models page shows the snapshot column group.
- an epa_blend search on the fixture with team_game_stats seeded (insert a few rows) that creates models.
- a paper sell end to end in the exchange loop with the sim source: an assignment holds a filled buy, the market then overshoots the model so the trade worker proposes a sell, the host approves it, the paper simulator fills it (partial then full if practical), ledger replay_problems is empty after each fill, then simulate-final settles with a sold row and pro-rata buy rows whose pnl sums match the bankroll's realized change.
Keep every wait bounded like the other phases. Run tests/test_e2e.py until it passes twice in a row. Final report: what the phase covers, run times, the two pytest summary lines.`

const SCREENSHOTS = (rep) => `You are the SCREENSHOTS writer of step 6 Part B. ${COMMON}
Integration report:
${rep}
You own: tests/hw/screenshots.py, new tests/hw/seed_step6b.py, tests/hw/README.md. Add a seed for step 6B (a lineage with snapshot_metrics ranked by snapshot CLV, an epa_blend model, an assignment with a position and a filled sell, an open sell order) wired into the script's seed(), and captures: models (snapshot columns), model-detail of the snapshot-ranked lineage, jobs (backtest form with price_source), settings (the new keys), trading (positions table and sell chips), each at 390 and 1280, light and dark, through the existing shoot_all and layout checks (no horizontal scroll at 390, tap targets). Run the script into ${SHOTS}/ until it exits 0. Look at the new 390-light PNGs yourself and report anything unreadable or broken. Final report: PNG list, exit status, observations.`

const DOCS = (rep) => `You are the DOCS writer of step 6 Part B. ${COMMON}
Integration report:
${rep}
You own: README.md (a "How to test step 6B" section in the style of "How to test step 6A": snapshot backtest from the Jobs page with price_source snapshots (and that it needs recorded prices: on a fresh install only sim prices exist, so set allow_sim_prices to try it, and turn it back off), the snapshot columns on Models, the injury and play-by-play refresh (CLI commands ingest-injuries / ingest-pbp and the automatic refresh), an epa_blend search, and selling in paper: what the sell chip and the positions table show; plus the build-order list: 6A done, 6B done, 6C and step 7 pending), docs/DESIGN.md (build-order table rows for 6B done, 6C and 7 pending; any section that changed meaning, e.g. data sources and the exchange CLI list), docs/MODELS.md and docs/ROBUSTNESS.md and docs/TRADING.md and docs/PROTOCOL.md and docs/DASHBOARD.md and docs/LIVE.md (sync every statement with the code as built: read the code, do not trust the builders' summaries; mark deviations from the original spec explicitly). Every claim you write must match a line of code. No em-dashes. Final report: files changed and the claims you verified.`

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
const VERDICT_SCHEMA = {
  type: 'object',
  properties: { real: { type: 'boolean' }, severity: { type: 'string', enum: ['high', 'medium', 'low'] }, evidence: { type: 'string' }, fix: { type: 'string' } },
  required: ['real', 'severity', 'evidence', 'fix'],
}
const LENSES = [
  { key: 'replay', text: 'SNAPSHOT REPLAY CORRECTNESS: decision time and the 30-minute bar window, devigging, which side is bought, the depth walk vs touch fallback and its participation, fees, CLV sign and the closing price fallback, sim exclusion (host and worker), resume exactness, closing_line results unchanged, the prices feed window [kickoff - 6 h, kickoff), ETag correctness across queries, and the leaderboard snapshot ranking boundaries. Build constructed cases with known answers under ' + SCRATCH + '/review6b-replay/.' },
  { key: 'leakage', text: 'SIGNAL LEAKAGE AND FEATURE TIMING: injury rows counted only when date_modified is before the decision time; QB change uses only the previous game; for unplayed games the QB fields are empty and give 0; rolling EPA strictly before the game and the league mean leak-free; team_game_stats and signals identical on the host path (games feed, trade state) and the worker csv path; the epa_blend fit never sees the test season; elo_blend with zero penalties reproduces the old numbers exactly. Write property tests and scripts under ' + SCRATCH + '/review6b-leakage/.' },
  { key: 'sells', text: 'SELL MATHS AND LEDGER INVARIANTS: sell basis (average cost, the closing sale takes exactly the remaining basis), ledger rows and the identity initial + realized = available + reserved + open after any sequence of buys, partial sells, full sells and settlement; settlement pro rata with the rounding residual; model_scores pnl including sells and n_bets counting buys; daily-loss accounting of sell P&L; paper sell fills (marketable, resting, shared participation per side, fill-id suffixes, idempotency); positions and P&L views. Construct randomized sequences under ' + SCRATCH + '/review6b-sells/ and check the identity after every step.' },
  { key: 'safety', text: 'APPROVAL SAFETY AND OWNER RULES: can any path short (sell more than held, two concurrent sells racing past the one-open-sell rule, a sell after settlement), can a sell bypass kill, lease, mode (live switch, live_eligible), stale book, or the approval lock; idempotency of client_request_id for sells; buys unchanged; kill cancels open sells; the live gateway sends side_sell only for sells and never logs keys; the worker cannot produce runaway sell/buy churn (sell then rebuy loops) under min_edge; rate limits. Reproduce with tests or scripts under ' + SCRATCH + '/review6b-safety/.' },
  { key: 'ui-docs', text: 'UI AND DOCS ACCURACY: read the PNGs under ' + SHOTS + '/ (models, model detail, jobs, settings, trading at 390 and 1280, light and dark) and the templates: are new numbers labelled in plain words, is the positions table readable on a phone, are chips consistent, does anything overflow; is every claim in README "How to test step 6B", docs/ROBUSTNESS.md Part B, docs/TRADING.md Selling, docs/PROTOCOL.md and docs/MODELS.md backed by a line of code (quote both).' },
]
const reviewPrompt = (lens, rep) => `You are an adversarial reviewer of step 6 Part B. ${COMMON}
Lens: ${lens.text}
Integration and extension reports:
${rep}
Report only findings you verified by running code, a test or a script, or by quoting exact lines that contradict the spec or contract; each with a concrete failure scenario and a minimal fix. Severity: high = money can be mis-accounted, a limit or gate can be bypassed, a statistic shown can be wrong or leaked, or results can be corrupted; medium = wrong behaviour under realistic use or a clear usability failure; low = hygiene. Do NOT modify repository files (scratch work goes under ${SCRATCH}/).`
const verifyPrompt = (f) => `You are a skeptical verifier. ${COMMON}
A reviewer reported this finding about step 6 Part B. Try hard to REFUTE it: read the code at the cited place and around it, and reproduce the failure scenario with a test or script under ${SCRATCH}/verify6b/ (do not modify repository files). Decide real=true only if you reproduced it or the cited lines unambiguously show it; otherwise real=false. Re-rate the severity on the same scale (high = money mis-accounted, limit or gate bypassed, wrong or leaked statistic, corrupted results; medium = wrong under realistic use or clear usability failure; low = hygiene). Give the evidence and the minimal fix.
FINDING (lens ${f.lens}):
${JSON.stringify(f, null, 1)}`
const AREAS = [
  { key: 'worker', test: f => f.file.startsWith('fleet/') || /tests\/(test_replay|test_signals|test_epa|test_elo|test_backtest|test_stress|test_search|fake_host)/.test(f.file),
    owns: 'fleet/** (all of it), tests/fake_host.py, and new or existing worker-side tests (tests/test_replay*.py, tests/test_signals_features.py, tests/test_epa_blend.py, tests/test_elo_penalties.py, tests/test_backtest.py, tests/test_stress.py, tests/test_search_train.py, tests/test_sell_worker.py, tests/test_trade_worker.py)' },
  { key: 'trading', test: f => /host\/(trading|exchange|kill|pnl|paper_gate)|_trading\.html|trading\.html|test_sell|test_limits|test_ledger|test_settlement|test_paper|test_kill|test_live|e2e_trading|e2e_live/.test(f.file),
    owns: 'host/trading/**, host/exchange/**, host/kill.py, host/pnl.py, host/paper_gate.py, host/api/trade.py, host/api/dashboard_trading.py, host/templates/_trading.html and trading.html, and trading tests (tests/test_sell_*.py, test_limits.py, test_ledger.py, test_settlement.py, test_paper_fills.py, test_kill_switch.py, test_live_*.py, test_trading_sells_page.py)' },
  { key: 'host-data', test: f => true,
    owns: 'every other host/ path (ingest, signals, feeds, models, snapshot_store, leaderboard, jobparams, settings, data_refresh, cli, api except trade and dashboard_trading, templates except the trading ones, static), docs/**, README.md, tests/hw/**, tests/e2e_signals.py, and the other tests' },
]
const fixPrompt = (area, fs) => `You are the ${area.key.toUpperCase()} fixer of step 6 Part B. ${COMMON}
You own only: ${area.owns}. Other fixers are editing the other areas at the same time; if a fix needs a change outside your paths, describe it in your report instead.
Apply every finding below (all were independently verified), add a regression test for each high and medium (new test files are fine), keep the docs in your paths in sync, and run the test files that cover your changes (not the full suite; a final agent runs it). If a finding turns out wrong on closer reading, say why and skip it.
Final report: per finding fixed/skipped(reason), files changed, pytest summary lines, cross-area needs.
FINDINGS:
${JSON.stringify(fs, null, 1)}`
const FINAL = (rep) => `You are the FINAL VERIFIER of step 6 Part B. ${COMMON}
You own everything under ${REPO}. The fixers' reports:
${rep}
1. Apply any "cross-area needs" the fixers listed that are still open (check each against the code).
2. Full suite three times in a row, green each time (fix anything that fails, in any file).
3. compileall -q fleet host; the stdlib-only check; no em-dashes.
4. Screenshots into ${SHOTS}/ (exit 0).
Final report: changes made, the three pytest summary lines, screenshot status, remaining known issues.`

phase('Integrate')
const integ = (await agent(INTEGRATE, { label: 'integrate', phase: 'Integrate', effort: 'high' })) || '(no report)'

phase('Extend')
const ext = await parallel([
  () => agent(E2E(integ), { label: 'e2e', phase: 'Extend', effort: 'high' }),
  () => agent(SCREENSHOTS(integ), { label: 'screenshots', phase: 'Extend', effort: 'high' }),
  () => agent(DOCS(integ), { label: 'docs', phase: 'Extend', effort: 'high' }),
])
const extRep = `INTEGRATE:\n${integ}\n\nE2E:\n${ext[0] || '(none)'}\n\nSCREENSHOTS:\n${ext[1] || '(none)'}\n\nDOCS:\n${ext[2] || '(none)'}`

phase('Review')
const verified = await pipeline(
  LENSES,
  l => agent(reviewPrompt(l, extRep), { label: `review:${l.key}`, phase: 'Review', schema: REVIEW_SCHEMA, effort: 'high' })
    .then(r => (r ? r.findings : []).map(f => ({ ...f, lens: l.key }))),
  fs => parallel(fs.map(f => () => {
    if (f.severity === 'low') return Promise.resolve({ ...f, verdict: { real: true, severity: 'low', evidence: 'not separately verified (low)', fix: f.suggested_fix } })
    return agent(verifyPrompt(f), { label: `verify:${f.lens}:${f.file.split('/').pop()}:${f.line}`, phase: 'Verify', schema: VERDICT_SCHEMA, effort: 'high' })
      .then(v => ({ ...f, verdict: v }))
  })),
)
const all = verified.flat().filter(Boolean)
const confirmed = all.filter(f => f.verdict && f.verdict.real)
log(`${all.length} findings, ${confirmed.length} confirmed (${confirmed.filter(f => f.verdict.severity !== 'low').length} high/medium)`)

phase('Fix')
const groups = AREAS.map(a => ({ area: a, fs: [] }))
for (const f of confirmed) {
  const g = groups.find(x => x.area.test(f))
  g.fs.push({ severity: f.verdict.severity, file: f.file, line: f.line, summary: f.summary, failure_scenario: f.failure_scenario, fix: f.verdict.fix || f.suggested_fix, evidence: f.verdict.evidence, lens: f.lens })
}
const fixReports = await parallel(groups.filter(g => g.fs.length > 0).map(g => () =>
  agent(fixPrompt(g.area, g.fs), { label: `fix:${g.area.key}`, phase: 'Fix', effort: 'high' }).then(r => `=== ${g.area.key} ===\n${r || '(no report)'}`)
))

phase('Final')
const finalRep = (await agent(FINAL(fixReports.filter(Boolean).join('\n\n') || '(no fixes needed)'), { label: 'final', phase: 'Final', effort: 'high' })) || '(no report)'
return { integ, ext, confirmed, rejected: all.filter(f => !(f.verdict && f.verdict.real)).map(f => ({ lens: f.lens, file: f.file, summary: f.summary, why: f.verdict ? f.verdict.evidence : 'no verdict' })), fixReports, finalRep }
