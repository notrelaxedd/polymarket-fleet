export const meta = {
  name: 'fleet-step6a-build',
  description: 'Step 6 Part A: validation era, bootstrap CIs and market test, stress tests, stricter gates, multi-core search; build in parallel, integrate, review, fix',
  phases: [
    { title: 'Build', detail: 'worker statistics and search (fleet/sim), host gates/API/dashboard, docs' },
    { title: 'Integrate', detail: 'full suite, e2e validation flow, parallel-equals-serial check, screenshots' },
    { title: 'Review', detail: 'statistics and leakage, parallel correctness, gates and UI' },
    { title: 'Fix', detail: 'apply findings, rerun suite' },
  ],
}

const REPO = '/home/user/polymarket-fleet'
const SHOTS = '<SCRATCHPAD>/screenshots-step6a'
const COMMON = `
Repository: ${REPO} (git; steps 1-5 committed, 585 tests green; do NOT commit or push, the orchestrator does that).
Python: ${REPO}/.venv/bin/python. Run tests from ${REPO} as .venv/bin/python -m pytest <files> -q -p no:cacheprovider -o addopts=. Postgres 16 at postgresql://postgres:postgres@127.0.0.1:5432/postgres (if "connection refused": pg_ctlcluster 16 main start). Per-test databases come from a migrated template that is rebuilt when migrations change.
Read first, completely: ${REPO}/docs/ROBUSTNESS.md Part A (the spec), ${REPO}/docs/MODELS.md (the current backtester, search and train), and the code you touch. One deliberate deviation from ROBUSTNESS.md: the search era keeps its existing settings key backtest_seasons (no rename); validation_seasons is added. Everything else in Part A stands.
Style: Python 3.11+, type hints, modules under ~300 lines, plain docstrings, no em-dashes anywhere. fleet/ stays standard-library only (multiprocessing is stdlib). Determinism: every random draw seeded from strings as the spec says; no Date.now-style nondeterminism in metrics.
No network in this sandbox; the fixture tests/fixtures/games_sample.csv (2016-2025) is the data; for a search era + validation era split in tests use e.g. search [2016, 2021] and validation [2022, 2025].
Only touch the paths you own; other agents are writing the other paths concurrently.
`
const SHAPES = `
Shared data shapes (both sides implement exactly these):
- validation_metrics: the backtest metrics object (same keys as today: n_games, n_bets, total_stake_cents, pnl_cents, roi, hit_rate, avg_edge, avg_stake_cents, log_loss, brier, market_log_loss, calibration, max_drawdown_cents, max_drawdown, seasons, per_season, blend) PLUS
  "ci": {"roi": [lo, hi], "avg_clv": [lo, hi], "max_drawdown": [lo, hi], "hit_rate": [lo, hi], "avg_edge": [lo, hi]} (5th and 95th percentiles, B = 1000),
  "mean_ll_gain": float (mean over scored games of ll_market - ll_model), "market_p": float (sign-flip permutation p-value, 10 000 flips, one-sided for gain > 0),
  "brier_decomposition": {"reliability": f, "resolution": f, "uncertainty": f}, "calib_slope": f, "calib_intercept": f,
  "shrunk_roi": f (roi * n_bets / (n_bets + 100)), "era": "validation", "flags": ["overfit", ...] (subset of overfit).
- stress_metrics: {"prices": [{"name": "spread+0.01"|"spread+0.02"|"fee x1.5", "n_bets", "roi", "log_loss", "mean_ll_gain"}],
  "neighbourhood": {"n": 10, "shrunk_roi_median", "shrunk_roi_p10", "ll_gain_median", "ll_gain_p10"},
  "regimes": {"favourite": {...}, "underdog": {...}, "home": {...}, "away": {...}, "divisional": {...}, "non_divisional": {...}, "primetime": {...}, "day": {...}, "cold_or_windy": {...}, "other_weather": {...}}, each {"n_games", "n_bets", "roi", "pnl_cents", "mean_ll_gain"},
  "flags": ["fragile", "regime_dependent"] subset, "seed": int}.
- Flag rules exactly as docs/ROBUSTNESS.md A1 and A3. backtest_metrics (search era) also gains "ci", "mean_ll_gain", "market_p", "shrunk_roi", "era": "search" (cheap to add).
- Job kind validate: params {"model_id": uuid, "seed": int (default 1)}; role backtest; result = {"validation_metrics", "stress_metrics"}; the agent posts them to POST /api/v1/models/{id}/validation {job_id, validation_metrics, stress_metrics} before /complete (same pending-post machinery as backtest metrics).
- model_search result entries in create_models gain "validation_metrics" and "stress_metrics" (computed for the kept top_k on the validation era in params.validation_seasons); the host stores them on creation. Search params gain "validation_seasons" (host copies settings.validation_seasons in at creation) and "workers" (host copies settings.search_workers).
- Host eligibility: thresholds_backtest = {min_bets 50, min_roi 0.02, max_drawdown 0.3, require_validation true, min_roi_ci_low 0.0, max_market_p 0.1, forbid_flags ["overfit","fragile"]} evaluated on validation_metrics when require_validation (else on backtest_metrics as before); thresholds_paper gains clv_ci_excludes_zero true (paper CLV bootstrap over the lineage's paper bets, B=1000, seed "paper:<lineage_id>", 5th percentile > 0 with at least min_bets bets).
`

const SIM_PROMPT = `You are building the WORKER-SIDE statistics and search changes of step 6 Part A. ${COMMON}
${SHAPES}
You own: ${REPO}/fleet/sim/** , ${REPO}/fleet/models/** (search_space clipping helper for perturbations, qb params untouched), ${REPO}/fleet/worker/jobs.py, ${REPO}/fleet/worker/agent.py (only: post validation results and search validation fields; follow the existing create_models/backtest post pattern), ${REPO}/fleet/worker/posts.py (if the post sequence lives there), ${REPO}/tests/fake_host.py (accept the new endpoint and fields), ${REPO}/tests/test_stats.py, ${REPO}/tests/test_stress.py, ${REPO}/tests/test_validation.py, ${REPO}/tests/test_search_parallel.py, ${REPO}/tests/test_search_train.py, ${REPO}/tests/test_backtest.py, ${REPO}/tests/test_agent.py (post tests). Do not touch host/, docs/, templates.
Build:
1. fleet/sim/stats.py: bootstrap_ci(values, stat, B, rng) for bets (ROI as sum pnl / sum stake over the resample; hit rate; avg edge; avg clv) and season-block bootstrap for max drawdown (resample seasons with replacement, concatenate per-season game pnl sequences in order, drawdown of the cumulative series); permutation_market_test(d_list, n_flips, rng) returning (mean_gain, p); brier_decomposition(p_list, outcomes, buckets=10); logistic_recalibration(p_list, outcomes) via Newton (slope, intercept). Pure Python, deterministic, fast (bootstrap over 1000 bets x 1000 draws must run under 2 s; permutation 10 000 x 1000 games under 3 s, vectorise with precomputed cumulative tricks or reduce flips adaptively while documenting).
2. fleet/sim/backtest.py: expose per-game records (p_model, p_market, outcome, ll_model, ll_market, bet record with features for regimes: favourite side, home/away, div_game, kickoff hour ET, temp, wind) so stats and stress can consume them; add era labelling and shrunk_roi to metrics; add the ci/market fields to the metrics builder (metrics.py) given the records.
3. fleet/sim/validate.py: run_validate(games, model params/family, validation_seasons, limits, seed, emit, should_stop, checkpoint) producing validation_metrics and stress_metrics: base validation backtest; price stress (3 runs); neighbourhood (10 perturbed param sets, numeric params scaled by uniform [0.9, 1.1] from random.Random(f"{seed}:nbhd:{i}"), clipped to the family search space bounds; mov_scale stays); regimes from the base run's records; flags per the spec; units = one backtest run each (checkpoint between runs, resumable).
4. fleet/sim/search.py: after selecting top_k on the search era, run run_validate for each kept candidate (validation_seasons from params; skip with a clear "no validation era" note when absent) and attach validation_metrics + stress_metrics to create_models entries. Multi-core: params.workers ("auto" or int); candidates evaluated through multiprocessing.Pool(imap, chunksize 1) in index order (fork start method; each worker loads games once via initializer); the checkpoint advances only through completed indices in order; SIGTERM terminates the pool and the job raises JobStopped with the last checkpoint; identical results to workers=1 (test). Keep the single-process path for workers=1 and for validate/backtest.
5. jobs.py: register "validate"; search reads params.workers and params.validation_seasons; context model for validate comes from params["_context"]["model"].
6. agent.py/posts.py: a validate job's result is posted to POST /api/v1/models/{id}/validation {job_id, validation_metrics, stress_metrics} before /complete; search create_models entries carry the new fields through unchanged.
7. Tests: test_stats.py (bootstrap deterministic and sane: CI of a constant is degenerate, widens with variance, contains the point estimate; permutation p ~0.5 for identical model and market (d all zero -> p 1.0 by convention, document), p < 0.01 for a clearly better model, p > 0.9 for a clearly worse one; brier decomposition identity reliability - resolution + uncertainty = brier; recalibration recovers slope 1 intercept 0 for calibrated synthetic data); test_stress.py (price stress reduces or keeps bets, flag rules on constructed cases, neighbourhood determinism, regime partition covers every scored game exactly once per dimension); test_validation.py (selection never uses validation: property test on the fixture that scrambling validation-era scores changes no kept candidate; validation metrics only change; era labels; resume equivalence for validate); test_search_parallel.py (workers=3 gives byte-identical create_models and checkpoint sequence as workers=1 on a 12-candidate search; SIGTERM mid-search then resume yields the same final result; peak RSS of the process group stays under 300 MB); agent tests for the validation post order.
Run your tests three times; report timings (validate of one model on the fixture; 50-candidate search with workers=1 vs 3). Final report: files, summary lines, timings, deviations.`

const HOST_PROMPT = `You are building the HOST SIDE of step 6 Part A: settings and migration, the validate job kind, the validation endpoint, eligibility gates with intervals and flags, leaderboard ranking by validation, and the dashboard. ${COMMON}
${SHAPES}
You own: ${REPO}/host/** (new migration 0006_robustness.sql: models.validation_metrics jsonb, models.stress_metrics jsonb, settings seeds validation_seasons [2022, null], search_workers "auto", updated thresholds_backtest and thresholds_paper values (UPDATE existing rows to the new objects), plus validators in settings.py), ${REPO}/tests/test_models_api.py, ${REPO}/tests/test_eligibility.py, ${REPO}/tests/test_dashboard.py, ${REPO}/tests/test_queue.py, ${REPO}/tests/test_owner_api.py, ${REPO}/tests/conftest.py (additions), ${REPO}/tests/hw/** (screenshots seed), ${REPO}/docs/PROTOCOL.md and ${REPO}/docs/DASHBOARD.md (sync). Do not touch fleet/, README.md, docs/ROBUSTNESS.md, docs/DESIGN.md.
Build:
1. Migration + settings validators (validation_seasons: same shape as backtest_seasons and must start after backtest_seasons[1] when both are set; search_workers: "auto" or int 1..64; thresholds_backtest/paper new fields with ranges; a cross-field check that validation starts after the search era).
2. Job params: kind validate {model_id, seed?}; host copies validation_seasons (null resolved to the last complete season) and search_workers into model_search params as "validation_seasons" and "workers", and into validate params; validate requires the model to exist.
3. Worker API: POST /api/v1/models/{id}/validation {job_id (must be a validate or model_search job leased by the caller, params.model_id matching for validate), validation_metrics, stress_metrics} stored on every row of the lineage; POST /api/v1/models accepts and stores validation_metrics/stress_metrics on creation; eligibility recompute after both.
4. host/eligibility.py: the gate rules from SHAPES (require_validation -> use validation_metrics; CI lower bound; market_p; forbid_flags; min_bets on the validation era); paper gate clv_ci_excludes_zero via a bootstrap over the lineage's paper bets (implement a small deterministic bootstrap in host/stats.py; B=1000, seed "paper:<lineage_id>"); store the computed paper CI on model_scores or a lineage summary so the leaderboard can show it; demotion as before.
5. host/leaderboard.py + views: rank by validation shrunk ROI then mean_ll_gain; lineages without validation_metrics listed unranked with reason "not validated"; expose ci, market_p, flags, paper_ci in /api/models.
6. Dashboard: Models page columns (validation ROI with its 90% range, "beats market" p, flag chips overfit/fragile/regime-dependent, a "not validated" chip); model page "Robustness" section (CI line, market test sentence with p, calibration slope/intercept, price stress table, neighbourhood summary, regime table, flags with a one-line meaning each); Jobs page "Validate" form (model select, seed) and the model page "Validate" button; Settings: validation seasons, search workers, the new gate fields with hints. Phone layout rules as always.
7. Tests: migration + validators (overlap/ordering errors); validate job params; validation endpoint attribution and lineage propagation; eligibility boundaries for every new rule (CI lower bound exactly 0, market_p exactly max, each forbidden flag, require_validation false falls back); paper CI gate on constructed bets; leaderboard ordering and unranked reasons; dashboard renders every new element; screenshots seed with validated models (two ranked, one flagged overfit, one not validated).
Run your tests three times plus tests/test_settlement.py and tests/test_limits.py. Final report: files, summary lines, deviations.`

const DOCS_PROMPT = `You are writing docs for step 6 Part A of a multi-machine fleet. ${COMMON}
You own: ${REPO}/README.md, ${REPO}/docs/DESIGN.md, ${REPO}/docs/ROBUSTNESS.md (only the note that the search era keeps the backtest_seasons key).
1. README.md: a "Reading a model" guide in plain words: what the search era and validation era are and why the split matters; what the 90% range on ROI means and why a range that includes zero is not evidence; what "beats market p" means (and that p < 0.05 is the bar); what overfit, fragile and regime-dependent mean and what to do with a flagged model (retire it or rerun the search); what the price stress and neighbourhood numbers tell you; how the gates use these (a lineage becomes paper_ok only with validation numbers that clear the interval and p rules and carries no forbidden flag; live eligibility additionally needs paper CLV whose range excludes zero); and a one-paragraph honest statement that closing-line backtests measure calibration, so "beats the market" there is rare and a model that merely matches the market is a fine candidate for paper trading, where Polymarket prices may differ. A "How to test step 6A" section: Settings shows the validation seasons and search workers; a search now fills validation columns on Models; press Validate on an older model; read the Robustness section; watch a search on a 4-core box run about three times faster. Build order: step 6A done, 6B pending.
2. docs/DESIGN.md: eligibility and leaderboard sections reference docs/ROBUSTNESS.md; step 6A row done.
Final report: files written and assumptions.`

const INTEGRATE_PROMPT = (reports) => `You are integrating step 6 Part A after three agents built it in parallel (reports below). ${COMMON}
${SHAPES}
You own everything under ${REPO}. Reconcile shape drift between fleet/sim and host first (grep the field names).
1. Full suite three times: .venv/bin/python -m pytest tests -q -p no:cacheprovider -o addopts=. Fix every failure in any file.
2. Extend tests/test_e2e.py: a small model search (n 4, search era [2016, 2019], validation era [2020, 2021], workers 2) through the live agent -> models carry validation_metrics with ci/market_p/flags and stress_metrics; the Models page shows the validation columns and chips; a validate job on an older model fills its lineage; eligibility reflects the new rules (force metrics to clear the gates and check paper_ok appears; set a forbidden flag and check demotion).
3. Serial-vs-parallel check in the e2e or a dedicated test: the same search with workers 1 and workers 3 yields identical create_models (ids may differ, params and metrics must match exactly).
4. Screenshots into ${SHOTS}/: models, model-detail (robustness section), jobs (validate form), settings at 390 and 1280, light and dark; layout assertions as before.
5. compileall fleet/ host/; stdlib-only check on fleet/; no em-dashes.
Final report: files changed (one line each), three pytest summary lines, PNG list, timings observed, remaining known issues.
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
  { key: 'statistics', text: 'STATISTICS AND LEAKAGE: is the validation era truly unseen by selection (any path where validation numbers influence top_k, tie-breaks, or the search objective; Elo state or blend fits that peek past the era boundary)? Are the bootstrap percentiles computed correctly (sampling unit, ROI as ratio of sums, drawdown block bootstrap by season order), is the permutation test one-sided and correctly handling ties and zero gains, is the Brier decomposition identity exact, does the recalibration fit converge and clip extreme probabilities, are the flags computed from the right eras, can the summary sentence overclaim? Write scripts under <SCRATCHPAD>/review6a-stats/ with synthetic data where the truth is known and report numbers.' },
  { key: 'parallel', text: 'PARALLEL CORRECTNESS AND RESOURCES: multiprocessing search determinism (identical results and checkpoint sequence vs serial, including the order of create_models and tie-breaks), fork safety (no inherited sockets or pipes used by children; stdout JSONL only from the parent), SIGTERM and SIGKILL handling (no orphan processes, process group reaped, watchdog still sees children), memory per worker on a 4 GB box with the full 7500-game history (measure), the drain budget (a candidate in flight is lost but nothing else), the "auto" worker count on 2- and 4-core boxes, and that validate/backtest remain single-process. Reproduce with scripts under <SCRATCHPAD>/review6a-parallel/.' },
  { key: 'gates-ui', text: `GATES AND UI: every eligibility rule boundary (CI lower bound 0, market_p max, forbidden flags, require_validation off, paper CLV interval, min_bets on the right era), demotion side effects (live assignments halted), the leaderboard order and unranked reasons, the validate job attribution checks, settings cross-field validation (validation after search era), and the pages: read the PNGs under ${SHOTS}/ and the templates: are the new numbers labelled in plain words, is the Robustness section readable on a phone, are chips consistent, is the README "Reading a model" guide accurate against the code (every claim it makes about a rule must match a line of code)?` },
]
const reviewPrompt = (lens, report) => `You are an adversarial reviewer of step 6 Part A (robustness) of a multi-machine fleet. ${COMMON}
Lens: ${lens.text}
Integration report:
${report}
Report only findings you verified; give a concrete failure scenario and a minimal fix each. Severity: high = a statistical claim on the dashboard can be wrong or leaked, a gate can pass a model it should not, or the parallel search can corrupt results; medium = wrong behaviour under realistic use or a clear usability failure; low = hygiene. Do NOT modify repository files.`
const fixPrompt = (findings) => `You are fixing verified review findings in step 6 Part A. ${COMMON}
You own everything under ${REPO}. Apply every HIGH and MEDIUM finding (and cheap LOW ones), keep docs/ROBUSTNESS.md, docs/MODELS.md, docs/PROTOCOL.md, docs/DASHBOARD.md and README.md in sync, add a regression test for each HIGH and MEDIUM. If a finding is wrong, say why and skip it. Then: full suite three times (.venv/bin/python -m pytest tests -q -p no:cacheprovider -o addopts=), compileall, stdlib-only check, screenshots regenerated into ${SHOTS}/ if templates changed. Append each pytest summary line to ${REPO}/.fix6a_runs.txt as you go.
Final report: per finding fixed/skipped(reason); files changed; three pytest summary lines.
FINDINGS:
${JSON.stringify(findings, null, 1)}`

phase('Build')
log('Building worker statistics/search, host gates/API/dashboard, docs in parallel')
const built = await parallel([
  () => agent(SIM_PROMPT, { label: 'build:sim', phase: 'Build', effort: 'high' }),
  () => agent(HOST_PROMPT, { label: 'build:host', phase: 'Build', effort: 'high' }),
  () => agent(DOCS_PROMPT, { label: 'build:docs', phase: 'Build', model: 'sonnet', effort: 'medium' }),
])
const reports = built.map((r, i) => `=== ${['sim', 'host', 'docs'][i]} ===\n${r || '(no report)'}`).join('\n\n')

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
