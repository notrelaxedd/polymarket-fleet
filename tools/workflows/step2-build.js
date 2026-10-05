export const meta = {
  name: 'fleet-step2-build',
  description: 'Step 2: dashboard fleet page with role switching, kill flag, agent memory watchdog; build in parallel, integrate with screenshots, review, fix',
  phases: [
    { title: 'Build', detail: 'dashboard (host), agent watchdog (worker), docs + hw script' },
    { title: 'Integrate', detail: 'full suite, e2e through the HTML forms, Playwright screenshots' },
    { title: 'Review', detail: 'UI/phone usability, security of the new owner surface, correctness' },
    { title: 'Fix', detail: 'apply findings, rerun suite, regenerate screenshots' },
  ],
}

const REPO = '/home/user/polymarket-fleet'
const SHOTS = '<SCRATCHPAD>/screenshots-step2'
const COMMON = `
Repository: ${REPO} (git, committed through step 1; do NOT commit or push, the orchestrator does that).
Python: ${REPO}/.venv/bin/python (fastapi, uvicorn, psycopg[binary,pool], jinja2, python-multipart, pytest, httpx installed). Run tests as ${REPO}/.venv/bin/python -m pytest <files> -q -p no:cacheprovider -o addopts= (so the summary line shows).
Postgres 16 at postgresql://postgres:postgres@127.0.0.1:5432/postgres (superuser; tests create per-test databases from a template, see tests/conftest.py).
Read first: ${REPO}/docs/PROTOCOL.md (the whole file, including the new "Step 2 additions" section at the end), ${REPO}/docs/DASHBOARD.md (the dashboard spec), and the step 1 code you will touch. Step 1 is complete and green (106 tests): host/ (FastAPI, queue, heartbeat, owner API), fleet/ (stdlib-only worker agent), tests/.
Style: Python 3.11+, type hints, small modules (split rather than exceed ~300 lines), plain docstrings, no em-dashes anywhere (code, templates, docs). Templates: Jinja2 with autoescape; CSS: variables, system fonts, light and dark; JS: vanilla, tiny.
Only touch the paths you own; other agents are writing the other paths concurrently.
`

const DASH_PROMPT = `You are building the HOST side of step 2 (dashboard fleet page with role switching, kill flag). ${COMMON}

You own: ${REPO}/host/** (including new host/templates/ and host/static/), ${REPO}/tests/test_dashboard.py, ${REPO}/tests/test_kill_switch.py, ${REPO}/tests/test_owner_api.py, ${REPO}/tests/test_queue.py (only the kill-related edits), ${REPO}/tests/test_cli.py, ${REPO}/tests/conftest.py (additions only), ${REPO}/docs/PROTOCOL.md (keep in sync; mark edits with "(changed: reason)"). Do not touch fleet/, deploy/, README.md, docs/DESIGN.md, tests/test_agent.py, tests/fake_host.py, tests/test_runner.py, tests/test_e2e.py.

Build exactly what docs/DASHBOARD.md and the "Step 2 additions" of docs/PROTOCOL.md describe:
1. host/pnl.py: pnl(conn) -> {"today_cents": 0, "all_time_cents": 0, "by_worker": {id: 0}} with a clear TODO(step 4) and the by_worker keys populated from workers. host/kill.py: set_kill(conn, actor) (idempotent, audit row 'kill'), reset_kill(conn, actor, confirm) (requires exactly "RESUME", audit 'kill_reset', else QueueError 400), is_killed(conn). JSON routes in host/api/owner.py: POST /api/kill, POST /api/kill/reset, GET /api/pnl, GET /api/audit?limit=.
2. Kill scope: in host/heartbeat.py the kill flag must only block claims for the trade role; batch roles keep claiming (update _may_claim and the existing tests that assumed otherwise). Release reasons: accept "reason" in released[] entries and in POST /checkpoint release=true; store it in the released job event detail; reason "oom" increments expiries and fails the job at max_expiries (same rule as the reaper), other reasons do not.
3. host/api/dashboard.py (+ host/templates/*.html, host/static/style.css, host/static/app.js): pages GET / , /jobs, /jobs/{id}, /settings, /kill/confirm; fragments GET /fragments/fleet and /fragments/topbar; form posts POST /workers/{id}/role, /workers/{id}/enabled, /jobs, /jobs/{id}/cancel, /settings/{group}, /enroll-token, /kill, /kill/reset, each a thin wrapper over the existing JSON logic (reuse host/scheduling, host/settings, host/kill, host/views; do not duplicate SQL), redirecting back with a flash message (query parameter 'flash', rendered escaped). Static files via StaticFiles mounted at /static (owner auth NOT required for /static, everything else under the dashboard requires it). 401/403 render a small HTML page. Use the same require_owner dependency as /api. Jinja2 Environment with autoescape=True, templates loaded from host/templates via PackageLoader or a path relative to host/__init__.py (must work inside the Docker image where WORKDIR is /app and the package is installed; add package-data for templates and static in pyproject.toml [tool.setuptools.package-data] host = ["migrations/*.sql", "templates/*.html", "templates/**/*.html", "static/*"]).
4. Money helpers: cents to "$12.50" filter; settings forms take dollars for *_cents keys and convert server-side; validation errors from host/settings SCHEMA are shown inline (re-render the form with the error, 400).
5. The topbar fragment shows the mode pill, P&L from host/pnl.py and the KILL button or the red killed state. The fleet fragment renders the cards from host/views.fleet_workers plus pnl by_worker; status dot thresholds: online < online_after_seconds, stale < 60 s, offline otherwise; "switching to X (epoch N)" when switching.
6. app.js: role select auto-submit on change, confirm() on KILL (the no-JS path goes through GET /kill/confirm which has a real POST button), fragment refresh every 5 s (fleet) and 10 s (topbar) skipping when an input/select inside the target has focus or a submit is in flight, "updated N s ago" / "connection lost" indicator. Under ~120 lines. No external assets at all.
7. CLI (host/cli.py): kill, kill-reset [--yes] (prompts for RESUME otherwise), roletest <worker> as PROTOCOL.md describes (sends a 120 s sleep job targeted at the worker, waits for the role ack, then sets role train, measures ack minus request from host-side timestamps (audit_log.ts for the role change vs workers.last_heartbeat_at / the heartbeat that acked), prints seconds, exit 1 if over 10 s or if the worker never acks within 30 s).
8. Tests. tests/test_kill_switch.py: kill sets the flag and writes audit; heartbeat replies carry kill=true for every role; a trade-role worker cannot claim under kill while a backtest worker still can; POST /api/kill is idempotent; reset requires exactly RESUME (wrong text 400, flag unchanged); reset writes audit; CLI kill and kill-reset --yes; the dashboard shows the red killed bar and the settings page shows the reset form. tests/test_dashboard.py (TestClient with FLEET_DEV): every page renders 200 with the expected elements; fleet page shows one card per worker with dot class, role select, job progress, stats line; the role form flips desired_role and bumps epoch and redirects with a flash; enabled form; send sleep job form (any_idle and chosen) and cancel; settings form converts dollars to cents, rejects bad values with an inline error and audits changes; enroll-token form shows the token once and the two one-liners; fragments return only the inner HTML; worker names and job params containing <script> are escaped; a non-owner header gets 401 HTML; a POST with a foreign Origin gets 403; /static/style.css is served without auth; release reason stored and oom counts as expiry (in test_queue.py).
Run only the tests you own plus tests/test_workers_api.py; the full suite is run by the integration agent. Final report: files written, test summary lines, deviations, anything unfinished.`

const AGENT_PROMPT = `You are building the WORKER side of step 2 (memory watchdog, release reasons, kill flag scope). ${COMMON}

You own: ${REPO}/fleet/**, ${REPO}/deploy/**, ${REPO}/tests/fake_host.py, ${REPO}/tests/test_agent.py, ${REPO}/tests/test_runner.py. Do not touch host/, docs/, README.md or other tests. fleet/ must stay standard-library only and Python 3.11 compatible.

Build exactly what the "Step 2 additions" section of docs/PROTOCOL.md describes:
1. Release reasons: every release the agent sends (drain on role change, preempt, cancel, shutdown, oom) carries "reason". Check fleet/worker/agent.py for the drain/preempt/shutdown paths and the _release_now / pending_releases structures; add the field in both the heartbeat released[] entries and the POST /checkpoint release=true body.
2. Memory watchdog: a function in fleet/common/sysinfo.py that sums VmRSS (kB from /proc/<pid>/status) over all processes whose session id (field 6 of /proc/<pid>/stat, parse after the last ')') equals the runner child's pid (the runner starts the child with start_new_session=True so the child's pid is the session id); tolerate vanished processes. In the agent's service loop (the 0.1 s loop that services runners), at most every 1 s per runner, compare the sum with watchdog_rss_fraction (AgentOptions, default 0.8) times ram_total_mb (from sysinfo, overridable through AgentOptions.ram_total_mb for tests): if above, log a warning with the MB figures, stop the runner (terminate, drain_grace, kill), and release the job with its last checkpoint and reason "oom". Count the trips in status.json.
3. Kill flag scope: under kill the agent keeps claiming and running batch jobs (wants_job must not depend on self.kill for batch roles); record kill in status.json; the trade behaviour arrives in step 4 (leave a clear hook: a method on_kill() that is a no-op for batch roles).
4. tests/fake_host.py: store reason on releases (expose it in job_events detail), make an "oom" release count as an expiry with max_expiries 3, keep returning kill=true/false from a control setter (set_kill), and keep batch claims flowing under kill.
5. Tests in tests/test_agent.py: watchdog trips on a real sleep job when AgentOptions.ram_total_mb is set to 4 (MB) so the child's real RSS exceeds 0.8 x 4 MB: job released with reason oom and checkpoint elapsed >= 1, runner process gone (no zombie, process group empty), status.json counts the trip; watchdog does not trip with a realistic ram_total; with a patched measurement returning 0 nothing happens; drain/preempt/cancel/shutdown releases carry the right reason; kill=true does not stop a backtest worker from claiming and completing a job and status.json shows kill. tests/test_runner.py: a test that the session-id based RSS sum includes a grandchild (spawn python -c that forks a child which allocates ~30 MB and sleeps) and excludes unrelated processes.
Run only your own test files three times; all green. Final report: files written, test summary lines, deviations, anything unfinished.`

const DOCS_PROMPT = `You are writing docs and the hardware test wrapper for step 2 of a multi-machine fleet. ${COMMON}

You own: ${REPO}/README.md, ${REPO}/docs/DESIGN.md, ${REPO}/tests/hw/** (new directory). Do not touch anything else.
1. README.md: add a "How to test step 2" section after the step 1 one, matching docs/DASHBOARD.md and PROTOCOL.md's step 2 additions: open the dashboard on the phone (https://<host>/), what the fleet card shows, change a role from the dropdown and time it with a stopwatch (expect under 10 s until the card stops saying "switching"), run the roletest (docker compose exec host python -m host.cli roletest <worker>) and read its printed seconds, check that a tailnet device signed in as a different Tailscale user gets a 401 page, press KILL and see the red bar, reset in Settings by typing RESUME, edit a limit in Settings and see the audit row; mention that the kill switch only affects trading (batch jobs keep running) and that P&L shows $0.00 until step 4. Update the Build order checklist (step 2 done, 3-5 pending) and the Architecture paragraph (dashboard is server-rendered HTML from the host container; owner auth via the Tailscale-User-Login header; a worker machine's IP is refused on owner routes). Keep commands in fenced blocks. No em-dashes.
2. docs/DESIGN.md: update the Dashboard section to reference docs/DASHBOARD.md and the step 2 build-order row to "done" with the delivered scope (fleet cards, role switching, kill flag + reset, settings page with editable limits, enroll token page, audit log, agent memory watchdog, release reasons); note that the kill switch's cancel-all arrives in step 4; keep everything else.
3. tests/hw/roletest.sh: bash wrapper: usage "roletest.sh <worker-id> [compose-dir]"; runs "docker compose exec host python -m host.cli roletest <worker>" from the compose dir (default: the repo root) and exits with its status; prints a one-line explanation of the number. tests/hw/README.md: what the hardware tests are and when to run them.
Final report: files written and assumptions.`

const INTEGRATE_PROMPT = (reports) => `You are integrating step 2 of a multi-machine fleet (dashboard + kill flag + agent watchdog) after three agents built it in parallel (reports below). ${COMMON}
You own everything under ${REPO}. Keep docs/PROTOCOL.md and docs/DASHBOARD.md as the spec; if code and spec disagree, fix whichever is wrong and say so.
1. Full suite three times: ${REPO}/.venv/bin/python -m pytest ${REPO}/tests -q -p no:cacheprovider -o addopts=. Fix every failure (any file).
2. Extend tests/test_e2e.py (real uvicorn host + real agent): change the role through the HTML form route (POST /workers/{id}/role with form encoding and an Origin header equal to the public URL) and assert the fleet fragment first shows "switching" and then the new role within the step 1 bound; set kill through POST /kill (form) and assert the next heartbeat reply carries kill=true, the agent's status.json shows kill, and a backtest job still completes; reset via the form with RESUME.
3. Screenshots with Playwright: install the python package into the venv only (${REPO}/.venv/bin/python -m pip install playwright; do NOT run "playwright install"; Chromium is pre-installed under PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers). Start the app with FLEET_DEV=1 against a fresh test database seeded directly through SQL with three workers: "box1" online running a sleep job at 42% (leased job row), "box2" switching (desired backtest, acked_epoch behind) and idle, "box3" offline (last heartbeat 10 minutes ago), plus two finished jobs and one queued untargeted job. Capture to ${SHOTS}/ : fleet, jobs, job-detail, settings at 390x844 (phone) and 1280x800, each in light and dark colour scheme (emulate prefers-color-scheme), plus fleet-killed-390 after POST /kill. Also assert at 390 px that document.scrollingElement.scrollWidth <= window.innerWidth on every page, that every button/select/link in the card has a bounding box height >= 40 px, and that the fleet fragment refresh updates the "updated N s ago" text. Fix any layout problem you see in the PNGs (overflow, clipped text, unreadable contrast) and re-capture. Keep the screenshot script at ${REPO}/tests/hw/screenshots.py (it is a dev tool, not a pytest test; guard it from pytest collection).
4. ${REPO}/.venv/bin/python -m compileall -q ${REPO}/fleet ${REPO}/host and the stdlib-only check on fleet/ must pass.
Final report: files changed (one line each), the three pytest summary lines, the list of PNG paths with one line each describing what they show, and any remaining known issue.
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
  { key: 'ui', text: `UI AND PHONE USABILITY against docs/DASHBOARD.md and the owner's words "very simple and clean", "works on a phone": look at every PNG under ${SHOTS}/ (Read them; they are images) and the templates/CSS/JS. Hunt for: visual clutter, inconsistent spacing or type scale, poor contrast in light or dark, tap targets under 44 px, truncated or overflowing text, information the owner needs that is missing from a card or page (online status, role, current job, today's P&L, role dropdown), confusing states (switching, offline, killed), the no-JS path being broken, the refresh fighting with the user (focus rule), and anything that would read as unfinished. Also check that nav links for steps 3 and 4 are clearly disabled rather than dead. Report concrete fixes (CSS or template changes), not taste.` },
  { key: 'security', text: `SECURITY of the new owner surface: XSS through worker names, hostnames, job params/results/checkpoints/errors, audit rows, flash messages (is autoescape really on for every template and fragment? any |safe or Markup?); CSRF: do the HTML form posts enforce the Origin rule the same way the JSON routes do, and what happens with a missing Origin from a browser (Origin is always sent on POST, but check Referer-only clients)? open redirects via "next"/"back" parameters; the kill reset accepting anything but the exact RESUME; /static path traversal; owner auth on fragments and pages (a worker-IP request must be refused like on /api); the enroll token page caching or logging the token; settings forms allowing values the SCHEMA rejects; body size limits still applying to forms; the CLI roletest leaving jobs behind. Verify by running the TestClient or curl against a dev instance.` },
  { key: 'correctness', text: `CORRECTNESS of step 2 behaviour: kill flag scope on both sides (host _may_claim, agent wants_job), reason handling and the oom-as-expiry rule (does an oom release really fail at max_expiries? does the reaper and the oom path share the counter correctly?), the watchdog measurement (session-id based RSS sum: does it include grandchildren, exclude the agent itself, survive /proc races, and never trip on a healthy box with a realistic threshold?), the role switch through the dashboard form (epoch bump, flash, redirect, the fragment's "switching" state and its clearing), fragment refresh race with form submission, the settings dollars/cents conversion and rounding, roletest timing math, and whether tests/test_e2e.py actually exercises the UI path. Write small scripts under <SCRATCHPAD>/review2-correctness/ to confirm suspicions before reporting.` },
]
const reviewPrompt = (lens, report) => `You are an adversarial reviewer of step 2 of a multi-machine fleet (dashboard, kill flag, agent watchdog). ${COMMON}
Lens: ${lens.text}
Integration report:
${report}
Report only findings you verified; give a concrete failure scenario and a minimal fix each. Severity: high = security hole, data loss, worker bricked, owner misled about fleet state; medium = wrong behaviour under realistic use or a clear usability failure on a phone; low = hygiene. Do NOT modify repository files.`

const fixPrompt = (findings) => `You are fixing verified review findings in step 2 of a multi-machine fleet. ${COMMON}
You own everything under ${REPO}. Apply every HIGH and MEDIUM finding (and cheap LOW ones), keep docs/PROTOCOL.md and docs/DASHBOARD.md in sync, add a regression test for each HIGH and MEDIUM. If a finding is wrong, say why and skip it. Then: full suite three times (${REPO}/.venv/bin/python -m pytest ${REPO}/tests -q -p no:cacheprovider -o addopts=), compileall on fleet/ and host/, and regenerate the screenshots with ${REPO}/.venv/bin/python ${REPO}/tests/hw/screenshots.py (same output dir ${SHOTS}/) if any template/CSS/JS changed.
Final report: per finding fixed/skipped(reason); files changed; three pytest summary lines; whether screenshots were regenerated.
FINDINGS:
${JSON.stringify(findings, null, 1)}`

phase('Build')
log('Building dashboard (host), watchdog (worker), docs + hw script in parallel')
const built = await parallel([
  () => agent(DASH_PROMPT, { label: 'build:dashboard', phase: 'Build', effort: 'high' }),
  () => agent(AGENT_PROMPT, { label: 'build:agent', phase: 'Build', effort: 'high' }),
  () => agent(DOCS_PROMPT, { label: 'build:docs', phase: 'Build', model: 'sonnet', effort: 'medium' }),
])
const reports = built.map((r, i) => `=== ${['dashboard', 'agent', 'docs'][i]} ===\n${r || '(no report)'}`).join('\n\n')

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