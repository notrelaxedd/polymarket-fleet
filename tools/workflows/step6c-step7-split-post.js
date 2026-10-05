export const meta = {
  name: 'fleet-split-post',
  description: 'Post-build phases of step 6C or step 7 in slices (integrate, one extend task, one review lens with its verifiers, one fix area, final) so several run in parallel',
  phases: [ { title: 'Run', detail: 'the phase named in args.phase' } ],
}

const SCRATCH = '<SCRATCHPAD>'
const REPO = args.step === '6c' ? '/home/user/pf-6c' : '/home/user/pf-7'
const SHOTS = `${SCRATCH}/screenshots-step${args.step}`
const BASE = args.step === '6c'
  ? `Repository: ${REPO} (git worktree, branch wip-6c, own venv ${REPO}/.venv; work only inside it; never touch /home/user/polymarket-fleet, /home/user/pf-6b, /home/user/pf-7 or /home/user/pf-m). Do NOT commit or push.
Project: multi-machine fleet for NFL Polymarket model search and paper/live trading; host/ = FastAPI + psycopg + Jinja2 + Postgres (host/exchange/ is the exchange process); fleet/ = stdlib-only worker package (CI enforces it). Steps 6A and 6B are in the tree; step 6C (in-game trading: docs/INGAME.md and the contract ${SCRATCH}/shapes6c.txt, which wins where they differ) has just been built by several builders.
Owner rules (HANDOFF.md section 2): paper default; the host enforces every limit; in-game orders are paper-only in this step; kill cancels open orders only; keys never logged; free data only (ESPN unofficial endpoints, nflverse); no broadcast capture; fleet/ stdlib-only; tests for limits and kill.`
  : `Repository: ${REPO} (git worktree, branch wip-7, own venv ${REPO}/.venv with playwright 1.56.0; work only inside it; never touch /home/user/polymarket-fleet, /home/user/pf-6b, /home/user/pf-6c, /home/user/pf-m). Do NOT commit or push.
Project: a fleet dashboard (FastAPI + Jinja2 server-rendered pages, no framework) for NFL Polymarket model search and paper/live trading. Step 7 is the UI overhaul: docs/UI.md and the component contract ${SCRATCH}/ui7_contract.txt. Nothing about the API, the data model or the rules changes: only templates, static files, display helpers and page tests. Forms for every action, fragment refresh, auth, no-store, anti-framing and JavaScript-off operation stay.`
const COMMON = `${BASE}
Python: ${REPO}/.venv/bin/python. Tests: .venv/bin/python -m pytest <files> -q -p no:cacheprovider -o addopts= . A full suite takes 10+ minutes on this loaded 4-core box: run it in the background with output to a file under ${SCRATCH} and wait for it, or in two halves by file list, so no single command passes 10 minutes. Postgres 16 at postgresql://postgres:postgres@127.0.0.1:5432/postgres (export FLEET_TEST_DATABASE_URL; "connection refused" -> pg_ctlcluster 16 main start). Screenshots: PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers .venv/bin/python tests/hw/screenshots.py <out-dir> (pip install playwright==1.56.0 into the venv if missing; never "playwright install").
A timing test failing once under load and passing alone is load, not a bug; never weaken a behavioural assertion. Style: Python 3.11+, type hints, modules under about 300 lines, no em-dashes anywhere.
`
const rep = args.reports || '(no reports)'

const REVIEW_SCHEMA = { type: 'object', properties: { findings: { type: 'array', items: { type: 'object', properties: {
  severity: { type: 'string', enum: ['high', 'medium', 'low'] }, file: { type: 'string' }, line: { type: 'integer' },
  summary: { type: 'string' }, failure_scenario: { type: 'string' }, suggested_fix: { type: 'string' }, verified_by: { type: 'string' },
}, required: ['severity', 'file', 'line', 'summary', 'failure_scenario', 'suggested_fix', 'verified_by'] } }, overall: { type: 'string' } }, required: ['findings', 'overall'] }
const VERDICT_SCHEMA = { type: 'object', properties: { real: { type: 'boolean' }, severity: { type: 'string', enum: ['high', 'medium', 'low'] }, evidence: { type: 'string' }, fix: { type: 'string' } }, required: ['real', 'severity', 'evidence', 'fix'] }

const LENS = {
  '6c': {
    feed: 'FEED POLITENESS AND ROBUSTNESS: the ESPN request rate never exceeds gamestate_max_rps across games and passes, jittered exponential backoff on 429/403 with no tight loop anywhere (scoreboard fallback and probe included), malformed or partial payloads never raise and never write a wrong state, deduplication by play id, honest staleness, feed_lag window and lag sign, lag_status minimum events, Yahoo never fetched without a parser, postponed games stop polling. Scripts under ' + SCRATCH + '/review6c-feed/.',
    model: 'IN-GAME MODEL LEAKAGE AND CALIBRATION: ingame_wp never fits on validation seasons (candidate ranking and the train_fraction split included), the L2-per-1000-plays and time exponent behave, the pbp row to state mapping matches what the live feed hands the model, predictions monotone and sharpening, validation vs vegas_wp on the same plays, eligibility (beats_baseline, 10000 plays, never live_eligible). Scripts under ' + SCRATCH + '/review6c-model/.',
    trading: 'IN-GAME TRADING SAFETY: can any in-game order be approved in live mode, with stale state, inside the quiet period or the cutoff, while the lag suspends buys, beyond ingame_max_bet_cents or the bankroll and daily-loss limits; does the pre-game kickoff rule still hold for other orders; executor and paper exemptions only for in-game orders; GTD expiry; kill cancels in-game orders; no in-game buy/sell churn; attribution of in-game bets to the in-game lineage and the ledger identity after settlement; CLV excluded for in-game. Reproduce under ' + SCRATCH + '/review6c-trading/.',
    'ui-docs': 'UI AND DOCS ACCURACY: read the PNGs under ' + SHOTS + '/ and the templates (Trading in-game parts, Settings In-game group, Models in-game group, probe page) at 390 and 1280 light and dark: plain words, no overflow, chips with text, stale state obvious; every claim in README "How to test step 6C", docs/INGAME.md and docs/TRADING.md in-game parts backed by a line of code (quote both).',
  },
  '7': {
    phone: 'PHONE USABILITY AND ACCESSIBILITY: read every PNG under ' + SHOTS + '/ (390 and 1280, light and dark) and the CSS: does each page answer its question in the first screen, rows one line (two at most), 44 px tap targets, contrast in both schemes (run tests/test_style.py), no horizontal scroll, the bottom nav not covering content, details/summary usable with JS off (render with JS disabled through playwright), visible focus states.',
    behaviour: 'BEHAVIOUR PRESERVED: every form and action that existed before step 7 is still reachable and posts to the same route with the same fields (compare host/templates against the base commit d523f1a: list every form action and action link before and after), auth and owner checks, no-store and anti-framing headers, Origin checks, flash messages, the KILL flow, the live switch typed form, fragment refresh keeping open details, old links to / for the fleet page, the kill and auto-kill banner hooks.',
    wording: 'WORDING ACCURACY: every words-first phrase matches the number it summarises (CLV, ROI, P&L sign, probabilities as percentages, counts); every chip has a colour AND a word and the colour means state; the gate verdict sentence on the model page matches host/eligibility.py; the Needs attention rules match their code; captions do not overclaim.',
  },
}

const INTEGRATE = args.step === '6c'
  ? `You are the INTEGRATOR of step 6C. ${COMMON}
You own everything under ${REPO}. Builder reports:
${rep}
1. Apply every open NEEDS FROM OTHERS item (check each against the code), reconcile field names against the contract (grep "ingame", "game_state", "trade_ingame", "ingame_model_id", "state_at_entry", "ingame_n_bets", "lag"), wire anything left unwired (the agent posting in-game create_models, the exchange loop task, the dashboard include, the registry).
2. Full suite until green, then once more (two green runs). Fix every failure in any file.
3. compileall -q fleet host; the stdlib-only check from .github/workflows/ci.yml; no em-dashes.
Final report: changes (one line per file), pytest summary lines, remaining issues.`
  : `You are the INTEGRATOR of step 7. ${COMMON}
You own everything under ${REPO}. Reports:
${rep}
1. Apply open NEEDS; make the pages consistent with the contract (components built through host/templates/_ui.html macros).
2. tests/hw/screenshots.py: every page at 390 and 1280, light and dark (home, fleet, jobs, job detail, models, model detail, trading, settings, probe) with the UI.md assertions: no horizontal overflow; the first screen at 390x844 contains the h1 and at least one .stat; no .row taller than 88 px at 390; every .chip has text; every <details> has a <summary> with text; tap targets >= 44 px for buttons, row links, selects and inputs. Run into ${SHOTS}/ until exit 0.
3. A row-height audit test tests/hw/test_row_audit.py (skipped without playwright like tests/hw/test_ui.py): 20 lineages and 20 assignments with orders, Models and Trading at 390 px each under 6 x 844 px tall.
4. docs/DASHBOARD.md to the new layout and wording (UI.md wins), README build order step 7 done.
5. Full suite green twice (halves are fine). compileall, no em-dashes.
Final report: changes, summary lines, PNG list, remaining issues.`

const EXTEND = {
  e2e: `You are the E2E writer of step 6C. ${COMMON}
Reports:
${rep}
You own new tests/e2e_ingame.py and its call from tests/test_e2e.py (placed so it does not disturb the other phases' totals; tests/e2e_paper_gate.py runs last). phase_ingame(...) drives the REAL agent, host and exchange loop with the sim source: an assignment with an ingame_wp model (a tiny in-game search or an inserted fitted model row) and trade_ingame on; game_state rows injected (or a fake fetch for gamestate.poll fed from the ESPN fixtures) to put the game in progress; the trade worker proposes an in-game buy that is approved and paper-filled after kickoff; a stale state, a quiet period after a score change and the final two minutes each give the matching rejection; a live in-game request is rejected ingame_paper_only; the kill cancels the open in-game order; simulate-final settles with ingame bets rows and the in-game lineage scores. Bounded waits. Run tests/test_e2e.py until it passes twice. Final report: coverage, timings, summary lines.`,
  screenshots: `You are the SCREENSHOTS writer of step 6C. ${COMMON}
Reports:
${rep}
You own tests/hw/screenshots.py, new tests/hw/seed_step6c.py, tests/hw/README.md. Seed an in-game assignment with a fresh game state, an in-game order and fill, feed lag rows for ESPN, an ingame_wp lineage; capture trading (live score, in-game chip, In-game feed block), settings (In-game group), models (in-game group), the game-state probe page, at 390 and 1280, light and dark, through the existing layout checks. Run into ${SHOTS}/ until exit 0 and look at the 390-light ones yourself. Final report: PNG list, exit status, observations.`,
  docs: `You are the DOCS writer of step 6C. ${COMMON}
Reports:
${rep}
You own README.md ("How to test step 6C": the in-game toggle on an assignment, what the Trading page shows during a game, the In-game feed lag and suspension, why in-game is paper-only, the ESPN probe the owner runs during a live game and pastes back: docker compose run --rm exchange python -m host.exchange.cli probe-gamestate --event <espn id>, the pbp ingest ingest-pbp-rows --season 2012-2025 and an ingame_wp model search; build order: 6C done, step 7 pending), docs/DESIGN.md (build-order row 6C, the exchange CLI list with probe-gamestate, data sources), docs/INGAME.md, docs/PROTOCOL.md, docs/TRADING.md, docs/DASHBOARD.md, docs/MODELS.md: sync every statement with the code as built (read the code). Every claim must match a line of code. No em-dashes. Final report: files changed and claims verified.`,
}

const AREAS = {
  '6c': {
    feed: 'ONLY host/exchange/gamestate.py, host/exchange/gamestate_parse.py, host/exchange/scores.py (and host/exchange/main.py only for sharing the ESPN rate window and backoff between the scores task and the gamestate task), tests/test_gamestate.py, tests/test_scores.py, new tests/test_gamestate_review.py, and the feed paragraphs of docs/INGAME.md. Other builders are editing host/exchange/feedlag.py, executor.py, paper.py and the trading files right now: do not touch them.',
    model: 'ONLY fleet/models/ingame_wp.py, fleet/sim/ingame.py, fleet/sim/ingame_eval.py, host/pbp_rows.py, host/models.py (only the existing-root update so an ingame_wp artifact and its metrics are always written together), host/exchange/gamestate_parse.py only if the live kickoff encoding must change to match training (coordinate: a feed fixer is editing gamestate_parse.py at the same time, so prefer fixing the training side in host/pbp_rows.py and the model features), tests/test_ingame_wp.py, tests/test_ingame_search.py, tests/test_pbp_rows.py, new tests/test_ingame_model_review.py, and the ingame_wp section of docs/MODELS.md. An integrator is editing other files at the same time: do not touch anything else.',
    worker: 'fleet/**, tests/fake_host.py and worker-side tests (tests/test_trade_ingame.py, test_ingame_wp.py, test_ingame_search.py, test_ingame_job.py, test_trade_worker.py, test_sell_worker.py)',
    trading: 'host/trading/**, host/exchange/**, host/kill.py, host/pnl.py, host/paper_gate.py, host/api/trade.py, host/api/owner_trading.py, host/api/dashboard_trading.py, host/api/dashboard_ingame.py, host/templates/_trading.html, trading.html, probe.html and the trading and in-game host tests',
    'host-data': 'every other path (host models, eligibility, leaderboard, jobparams, settings, data_refresh, pbp_rows, api data, other templates, static, docs, README.md, tests/hw/**, tests/e2e_ingame.py and other tests)',
  },
  '7': { all: 'everything under the repository' },
}

phase('Run')
let result
if (args.phase === 'integrate') {
  result = await agent(INTEGRATE, { label: `${args.step}:integrate`, phase: 'Run', effort: 'high' })
} else if (args.phase === 'extend') {
  result = await agent(EXTEND[args.task], { label: `${args.step}:${args.task}`, phase: 'Run', effort: 'high' })
} else if (args.phase === 'review') {
  const lens = LENS[args.step][args.lens]
  const r = await agent(`You are an adversarial reviewer of step ${args.step}. ${COMMON}
Lens: ${lens}
Reports:
${rep}
Report only findings you verified by running code, a test or a script, or by quoting exact lines that contradict the contract or spec; each with a concrete failure scenario and a minimal fix. Severity: high = a limit or gate bypassed, money mis-accounted, an action unreachable or unsafe, a wrong number shown, wrong state used for trading; medium = wrong behaviour under realistic use or a clear usability failure; low = hygiene. Do NOT modify repository files (scratch under ${SCRATCH}/review${args.step}/).`,
    { label: `${args.step}:review:${args.lens}`, phase: 'Run', schema: REVIEW_SCHEMA, effort: 'high' })
  const fs = r ? r.findings : []
  const judged = await parallel(fs.map(f => () => f.severity === 'low'
    ? Promise.resolve({ ...f, verdict: { real: true, severity: 'low', evidence: 'low, not separately verified', fix: f.suggested_fix } })
    : agent(`You are a skeptical verifier. ${COMMON}
Try hard to REFUTE this finding about step ${args.step}: read the code at and around the cited place and reproduce the failure scenario (scratch under ${SCRATCH}/verify${args.step}/; no repository edits). real=true only if reproduced or unambiguous in the cited lines. Re-rate severity, give evidence and the minimal fix.
FINDING (lens ${args.lens}):
${JSON.stringify(f, null, 1)}`, { label: `${args.step}:verify:${args.lens}:${f.file.split('/').pop()}:${f.line}`, phase: 'Run', schema: VERDICT_SCHEMA, effort: 'high' })
      .then(v => ({ ...f, verdict: v }))))
  result = { lens: args.lens, overall: r ? r.overall : '(no review)', findings: judged.filter(Boolean) }
} else if (args.phase === 'fix') {
  result = await agent(`You are the ${args.area.toUpperCase()} fixer of step ${args.step}. ${COMMON}
You own only: ${AREAS[args.step][args.area]}. Other fixers may edit other areas at the same time; list cross-area needs in your report instead of editing outside your paths.
Apply every finding below (all independently verified), add a regression test (or a screenshot assertion for layout) for each high and medium, keep docs in your paths in sync, and run the tests covering your changes. Skip a finding only with a reason.
Final report: per finding fixed/skipped(reason), files changed, pytest summary lines, cross-area needs.
FINDINGS:
${JSON.stringify(args.findings || [], null, 1)}`, { label: `${args.step}:fix:${args.area}`, phase: 'Run', effort: 'high' })
} else if (args.phase === 'final') {
  result = await agent(`You are the FINAL VERIFIER of step ${args.step}. ${COMMON}
You own everything under ${REPO}. Fix reports:
${rep}
1. Apply open cross-area needs (check each). 2. Full suite twice, green each time (halves are fine). 3. compileall, stdlib-only check on fleet/, no em-dashes. 4. Screenshots into ${SHOTS}/ (exit 0).
Final report: changes, pytest summary lines, screenshot status, remaining issues${args.step === '6c' ? ', and the final list of UI additions this step made to the Trading, Settings and Models pages (for the step 7 port)' : ', and where the 6C in-game additions should go in each new template (template and block) for the port'}.`, { label: `${args.step}:final`, phase: 'Run', effort: 'high' })
}
return { step: args.step, phase: args.phase, result }
