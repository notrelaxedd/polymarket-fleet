export const meta = {
  name: 'fleet-step6a-finish',
  description: 'Step 6 Part A finish: close integrate gaps, three adversarial reviews (statistics, parallel, gates/UI), fix pass',
  phases: [
    { title: 'Integrate', detail: 'close known gaps, full suite x3, screenshots' },
    { title: 'Review', detail: 'statistics and leakage, parallel correctness, gates and UI' },
    { title: 'Fix', detail: 'apply findings, rerun suite' },
  ],
}

const REPO = '/home/user/polymarket-fleet'
const SCRATCH = '<SCRATCHPAD>'
const SHOTS = `${SCRATCH}/screenshots-step6a`
const COMMON = `
Repository: ${REPO} (git; steps 1-5 committed, step 6 Part A built and on main; baseline full suite 633 passed, 4 skipped in 7.5 min; do NOT commit or push, the orchestrator does that).
Python: ${REPO}/.venv/bin/python (venv exists with host, dev and the playwright python package). Run tests from ${REPO} as .venv/bin/python -m pytest <files> -q -p no:cacheprovider -o addopts= . Full suite: .venv/bin/python -m pytest tests -q -p no:cacheprovider -o addopts= (about 8 minutes; run it in the FOREGROUND with a long timeout, never in the background). Postgres 16 at postgresql://postgres:postgres@127.0.0.1:5432/postgres (export FLEET_TEST_DATABASE_URL to that; if "connection refused": pg_ctlcluster 16 main start). Per-test databases are cloned from a migrated template rebuilt when migrations change; stale fleet_test_* databases from a killed run may be dropped by hand.
Screenshots: PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers .venv/bin/python tests/hw/screenshots.py <out-dir> (never run "playwright install").
Read first: ${REPO}/HANDOFF.md (rules and state), ${REPO}/docs/ROBUSTNESS.md Part A (the spec), ${REPO}/docs/MODELS.md, and the code you touch. One deliberate deviation from ROBUSTNESS.md: the search era keeps its existing settings key backtest_seasons (no rename); validation_seasons is added.
Style: Python 3.11+, type hints, modules under ~300 lines, plain docstrings, no em-dashes anywhere (not in code, tests, docs or templates). fleet/ stays standard-library only. Determinism: every random draw seeded from strings as the spec says.
No network to Polymarket/ESPN; the fixture tests/fixtures/games_sample.csv (2016-2025) is the data.
`
const SHAPES = `
Shared data shapes (as built):
- validation_metrics: the backtest metrics object (n_games, n_bets, total_stake_cents, pnl_cents, roi, hit_rate, avg_edge, avg_stake_cents, log_loss, brier, market_log_loss, calibration, max_drawdown_cents, max_drawdown, seasons, per_season, blend) PLUS "ci": {"roi","avg_clv","max_drawdown","hit_rate","avg_edge": [lo, hi]} (5th/95th percentiles, B = 1000), "mean_ll_gain", "market_p" (sign-flip permutation, 10 000 flips, one-sided gain > 0), "brier_decomposition", "calib_slope", "calib_intercept", "shrunk_roi" (roi * n_bets / (n_bets + 100)), "era": "validation", "flags" (subset of overfit).
- stress_metrics: {"prices": [{"name": "spread+0.01"|"spread+0.02"|"fee x1.5", "n_bets", "roi", "log_loss", "mean_ll_gain"}], "neighbourhood": {"n": 10, "shrunk_roi_median", "shrunk_roi_p10", "ll_gain_median", "ll_gain_p10"}, "regimes": {favourite, underdog, home, away, divisional, non_divisional, primetime, day, cold_or_windy, other_weather: {"n_games", "n_bets", "roi", "pnl_cents", "mean_ll_gain"}}, "flags": subset of ["fragile", "regime_dependent"], "seed"}.
- Job kind validate: params {"model_id", "seed" (default 1)}; role backtest; results posted to POST /api/v1/models/{id}/validation before /complete.
- Host eligibility: thresholds_backtest = {min_bets 50, min_roi 0.02, max_drawdown 0.3, require_validation true, min_roi_ci_low 0.0, max_market_p 0.1, forbid_flags ["overfit","fragile"]}; thresholds_paper gains clv_ci_excludes_zero true (paper CLV bootstrap over the lineage's paper bets, B=1000, seed "paper:<lineage_id>", 5th percentile > 0 with at least min_bets bets).
`

const INTEGRATE_PROMPT = `You are finishing the integration of step 6 Part A (robustness) of a multi-machine fleet. Everything is built and the full suite was green on the current head; a gap analysis found the items below. ${COMMON}
${SHAPES}
You own everything under ${REPO}.
Close these gaps:
1. tests/e2e_validation.py never asserts the "not validated" chip/state on the Models page, because every e2e lineage gets validated. Add an unvalidated lineage in the e2e phase (for example a model created through the existing API or a backtest-only lineage, or clear one lineage's validation_metrics via SQL the way the phase already forces metrics), assert it is listed unranked with reason "not validated" in /api/models and that the Models page row shows the not-validated chip, then run a validate job on it through the live agent and assert it becomes validated and ranked. Use data-* attributes or class-prefix lookups for new markup assertions where possible.
2. tests/e2e_validation.py around line 122 has a tautology: entry["validation"]["ci"]["roi"] == entry["validation"]["ci"]["roi"]. Compare against the stored model's validation_metrics instead.
3. The paper CLV interval gate (thresholds_paper.clv_ci_excludes_zero) is only unit-tested (tests/test_eligibility.py test_paper_ci_gate_on_constructed_bets). Add an e2e check: with paper bets whose CLV range excludes zero over at least min_bets bets (insert them the way existing tests construct paper bets, or reuse conftest helpers), the lineage reaches live_eligible when the other paper thresholds pass; with a range that straddles zero it does not. Keep it fast.
4. docs/ROBUSTNESS.md "Tests that must exist" requires "a shuffled-outcome model never beats the market"; tests/test_stats.py lacks it. Add a test: a model whose probabilities are the market's on shuffled outcomes (or a model fit on shuffled outcomes) gets market_p well above 0.05 across several seeds (deterministic seeds).
5. docs/DESIGN.md line ~129 still says "candidate to paper_ok at backtest >= 200 bets, ROI >= 2%, drawdown <= 30%"; update the eligibility sentence to the step 6A gates (validation era, min_bets 50, CI lower bound, market p, forbidden flags; paper CLV interval) consistent with host/eligibility.py and docs/ROBUSTNESS.md A4.
6. tests/hw/screenshots.py DEFAULT_OUT is "/tmp/screenshots-step5"; make it a neutral default such as /tmp/screenshots.
7. tests/hw/seed_step6.py line ~65 writes a checkpoint {"stage": "regimes"} as a string while the real validate checkpoint stage is an int (fleet/sim/validate.py); make the seed realistic.
Then:
8. Full suite three times from a clean state; fix every failure in any file (do not weaken behavioural assertions; a stale markup assertion may be updated to the current markup).
9. Screenshots into ${SHOTS}/ (the script prints problems and exits 1 on layout failures; it must exit 0). Look at models-390-light.png, model-detail-390-light.png, jobs-validate-form-390-light.png and settings-390-light.png yourself and note anything unreadable.
10. compileall fleet/ host/; stdlib-only check on fleet/ (the snippet in .github/workflows/ci.yml); grep for em-dashes (U+2014) across the repo excluding .venv and .git.
Final report: files changed (one line each), the three pytest summary lines, the screenshot script exit status and PNG count, timings observed, and remaining known issues.`

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
  { key: 'statistics', text: `STATISTICS AND LEAKAGE: is the validation era truly unseen by selection (any path where validation numbers influence top_k, tie-breaks, or the search objective; Elo state or blend fits that peek past the era boundary)? Are the bootstrap percentiles computed correctly (sampling unit, ROI as ratio of sums, drawdown block bootstrap by season order), is the permutation test one-sided and correctly handling ties and zero gains, is the Brier decomposition identity exact, does the recalibration fit converge and clip extreme probabilities, are the flags (overfit, fragile, regime_dependent) computed from the right eras and exactly per docs/ROBUSTNESS.md A1 and A3, can the summary sentence overclaim? Also judge one known deviation: price stress re-plans the base run's records instead of running a full backtest per change (fleet/sim/stress.py lines 6-8 claim equivalence); verify the claim by running both ways on the fixture, and report a finding only if numbers differ. Write scripts under ${SCRATCH}/review6a-stats/ with synthetic data where the truth is known and report numbers.` },
  { key: 'parallel', text: `PARALLEL CORRECTNESS AND RESOURCES: multiprocessing search determinism (identical results and checkpoint sequence vs serial, including the order of create_models and tie-breaks), fork safety (no inherited sockets or pipes used by children; stdout JSONL only from the parent), SIGTERM and SIGKILL handling (no orphan processes, process group reaped, watchdog still sees children), memory per worker on a 4 GB box with the full ~7500-game history (synthesise a larger games list from the fixture if needed and measure), the drain budget (a candidate in flight is lost but nothing else), the "auto" worker count on 2- and 4-core boxes, and that validate/backtest remain single-process. Reproduce with scripts under ${SCRATCH}/review6a-parallel/.` },
  { key: 'gates-ui', text: `GATES AND UI: every eligibility rule boundary (CI lower bound exactly 0, market_p exactly max, each forbidden flag, require_validation off, paper CLV interval, min_bets on the right era), demotion side effects (live assignments halted), the leaderboard order and unranked reasons, the validate job attribution checks (a worker cannot post validation for a model or job it does not hold), settings cross-field validation (validation after search era, null ends), and the pages: read the PNGs under ${SHOTS}/ (models, model-detail, model-overfit, jobs-validate-form, settings at 390 and 1280, light and dark) and the templates: are the new numbers labelled in plain words, is the Robustness section readable on a phone, are chips consistent, is the README "Reading a model" guide accurate against the code (every claim it makes about a rule must match a line of code)?` },
]
const reviewPrompt = (lens, report) => `You are an adversarial reviewer of step 6 Part A (robustness) of a multi-machine fleet. ${COMMON}
${SHAPES}
Lens: ${lens.text}
Integration report:
${report}
Report only findings you verified (by running code, a script or a test, or by citing exact lines that contradict the spec); give a concrete failure scenario and a minimal fix each. Severity: high = a statistical claim on the dashboard can be wrong or leaked, a gate can pass a model it should not, or the parallel search can corrupt results; medium = wrong behaviour under realistic use or a clear usability failure; low = hygiene. Do NOT modify repository files (scratch scripts go under ${SCRATCH}/).`
const fixPrompt = (findings) => `You are fixing verified review findings in step 6 Part A. ${COMMON}
${SHAPES}
You own everything under ${REPO}. Apply every HIGH and MEDIUM finding (and cheap LOW ones), keep docs/ROBUSTNESS.md, docs/MODELS.md, docs/PROTOCOL.md, docs/DASHBOARD.md and README.md in sync, add a regression test for each HIGH and MEDIUM. If a finding is wrong, say why and skip it. Then: full suite three times, compileall fleet host, the stdlib-only check, no em-dashes, and screenshots regenerated into ${SHOTS}/ if templates or CSS changed (must exit 0). Append each pytest summary line to ${SCRATCH}/fix6a_runs.txt as you go.
Final report: per finding fixed/skipped(reason); files changed; three pytest summary lines; screenshot status.
FINDINGS:
${JSON.stringify(findings, null, 1)}`

phase('Integrate')
const integrateReport = (await agent(INTEGRATE_PROMPT, { label: 'integrate', phase: 'Integrate', effort: 'high' })) || '(no report)'

phase('Review')
const reviews = await parallel(lenses.map(l => () =>
  agent(reviewPrompt(l, integrateReport), { label: `review:${l.key}`, phase: 'Review', schema: REVIEW_SCHEMA, effort: 'high' })
    .then(r => r ? { ...r, lens: l.key } : null)
))
const okReviews = reviews.filter(Boolean)
if (okReviews.length < lenses.length) log(`only ${okReviews.length} of ${lenses.length} reviews returned`)
const findings = okReviews.flatMap(r => r.findings.map(f => ({ ...f, lens: r.lens })))
log(`${findings.length} findings (${findings.filter(f => f.severity !== 'low').length} high/medium)`)

phase('Fix')
let fixReport = '(no findings)'
if (findings.length > 0) {
  fixReport = (await agent(fixPrompt(findings), { label: 'fix', phase: 'Fix', effort: 'high' })) || '(no report)'
}
return { integrateReport, reviews: okReviews, fixReport }
