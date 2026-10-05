export const meta = {
  name: 'fleet-step1-build',
  description: 'Build step 1 (host DB/API/queue, worker agent, installer, docs, CI) in parallel against the protocol contract, integrate end to end, adversarially review, fix',
  phases: [
    { title: 'Build', detail: 'host, worker, docs/CI in parallel on disjoint paths' },
    { title: 'Integrate', detail: 'end-to-end test with real Postgres + agent, fix both sides' },
    { title: 'Review', detail: 'three adversarial lenses: concurrency, security, ops' },
    { title: 'Fix', detail: 'apply confirmed findings, keep tests green' },
  ],
}

const REPO = '/home/user/polymarket-fleet'
const COMMON = `
Repository: ${REPO} (git initialised, nothing committed yet; do NOT commit or push, the orchestrator does that).
Python for everything: ${REPO}/.venv/bin/python (3.11, with fastapi, uvicorn, psycopg[binary,pool], jinja2, python-multipart, pytest, httpx installed). Run tests with ${REPO}/.venv/bin/pytest <explicit test files>.
A Postgres 16 is running at postgresql://postgres:postgres@127.0.0.1:5432/postgres (superuser; you may create/drop databases).
Read ${REPO}/docs/PROTOCOL.md first and follow it exactly; it is the contract between host and worker. Also read ${REPO}/host/migrations/0001_init.sql and ${REPO}/pyproject.toml.
The broader design (for context only; step 1 scope is what PROTOCOL.md and this prompt say): <SCRATCHPAD>/fleet-design-v2.md.
Style: Python 3.11+, type hints, small functions, plain docstrings, no clever metaprogramming, no em-dashes in text. Keep files reasonably small (split modules rather than writing 800-line files).
If PROTOCOL.md is ambiguous, pick the simplest reading, implement it, and list the decision in your final report under "deviations/decisions".
Only touch the paths you own (listed below). Other agents are writing the other paths concurrently.
`

const HOST_PROMPT = `You are building the HOST half of step 1 of a multi-machine fleet. ${COMMON}

You own: ${REPO}/host/** (except the existing migration, which you may fix if it has a bug), ${REPO}/Dockerfile, ${REPO}/docker-compose.yml, ${REPO}/.env.example, ${REPO}/tests/conftest.py, ${REPO}/tests/test_queue.py, ${REPO}/tests/test_workers_api.py, ${REPO}/tests/test_owner_api.py.

Build:
1. host/config.py: a Config dataclass loaded from env: DATABASE_URL (default postgresql://fleet:fleet@db:5432/fleet), FLEET_PUBLIC_URL (default http://127.0.0.1:8080), FLEET_OWNER_LOGIN (default ""), FLEET_DEV (bool, default false), FLEET_ALLOWED_ORIGINS (default = FLEET_PUBLIC_URL), FLEET_BIND (default 127.0.0.1:8080), FLEET_DEPLOY_DIR (default <repo>/deploy resolved from host/__file__), FLEET_LOOP_SECONDS (default 5).
2. host/db.py: psycopg_pool.ConnectionPool wrapper; connection context managers; migrate(conn_url) that applies host/migrations/*.sql in order, recording versions in schema_migrations (idempotent, each file in one transaction). Use psycopg's dict_row.
3. host/auth.py: token minting (secrets.token_urlsafe(32)), sha256 hashing, enroll-token create/consume, worker bearer verification bound to the path worker id, owner check (Tailscale-User-Login header == FLEET_OWNER_LOGIN unless FLEET_DEV), Origin check for state-changing owner requests.
4. host/queue.py: every SQL operation from PROTOCOL.md as plain functions taking a connection: heartbeat processing (one transaction, exact order from the contract), claim, renew, release, checkpoint, complete, fail, create_job (chosen worker / any_idle / idempotency), cancel_job, set_role (with preemption), set_enabled, reaper, dispatcher, idle_pick, auto-return-to-idle, held_jobs for register. Write job_events rows as specified. Kind->role mapping with the 'sleep' kind.
5. host/api/app.py create_app(config) (FastAPI), with routers: host/api/workers.py (register, heartbeat), host/api/jobs.py (checkpoint/complete/fail), host/api/owner.py (/api/fleet, /api/workers/{id}/role, /api/workers/{id}/enabled, /api/jobs CRUD, /api/enroll-token, /api/settings, /healthz), host/api/dl.py (/dl/version, /dl/worker.tar.gz, /install.sh). The tarball: built once at app start from the installed fleet package directory (import fleet; Path(fleet.__file__).parent), containing only *.py files and a VERSION file, deterministic code_version per the contract; served from memory. /install.sh reads FLEET_DEPLOY_DIR/install_worker.sh and substitutes __FLEET_HOST_URL__ (if the file is missing, return 503 with a clear message; another agent is writing it).
6. host/main.py: run migrations, start the background loop thread (reaper + dispatcher every FLEET_LOOP_SECONDS, resilient to exceptions, logging), then uvicorn on FLEET_BIND. host/cli.py (argparse, talks to the DB directly): migrate, enroll-token, workers (table), jobs (table, optional status filter), role <worker> <role>, send-job <kind> [--target any_idle|<worker>] [--params JSON], cancel <job>, run-loop (one iteration of reaper+dispatcher; for tests/debug).
7. Dockerfile (python:3.12-slim, copy pyproject + fleet/ + host/ + deploy/, pip install .[host], CMD python -m host.main), docker-compose.yml (db: postgres:16 with volume + healthcheck + 127.0.0.1:5432 published; host: build ., env_file .env, depends_on db healthy, 127.0.0.1:8080:8080, restart unless-stopped), .env.example with every FLEET_* variable commented.
8. tests/conftest.py: fixture that uses env FLEET_TEST_DATABASE_URL (default postgresql://postgres:postgres@127.0.0.1:5432/postgres) as an admin connection, creates a migrated template database once per session (fleet_test_template), and per test creates a fresh database from the template and drops it afterwards; a config fixture pointing at it with FLEET_DEV=1; an app/client fixture (fastapi TestClient via httpx). Provide a helper to register a fake worker directly (insert row + return token) and to post heartbeats.
9. Tests (all must pass): tests/test_queue.py: no double claim under 20 threads each heartbeating as different workers for one job; targeted job claimed before untargeted; release does not increment expiries; reaper requeues with checkpoint kept and fails at max_expiries; cancel_requested + expiry -> cancelled; stale lease token is reported in lost[] and refused on /checkpoint; any_idle sets role, targets, auto_role; chosen worker sets role and preempts a running job of another role; auto-return to idle happens only when no leased or targeted job remains; concurrent any_idle (10 threads, 3 idle workers) never picks the same worker twice; dispatcher assigns a waiting job when a worker becomes idle; idempotency_key returns the same job; claim refused while acked_epoch != role_epoch; held_jobs on re-register renew leases with new tokens and the old token is refused. tests/test_workers_api.py: enroll token single-use and expiry; register rotates token; heartbeat with wrong/other worker's token -> 401; /dl/version and /dl/worker.tar.gz contain fleet/VERSION matching code_version. tests/test_owner_api.py: owner header required unless FLEET_DEV; Origin mismatch -> 403; settings get/post with unknown key -> 400; /api/fleet shape.

Run ONLY your own test files with pytest (other test files may be mid-write). Final report (plain text): files written, test command + result summary (counts), deviations/decisions, anything you could not finish.`

const WORKER_PROMPT = `You are building the WORKER half of step 1 of a multi-machine fleet. ${COMMON}

You own: ${REPO}/fleet/** , ${REPO}/deploy/install_worker.sh, ${REPO}/deploy/fleet-worker.service, ${REPO}/tests/test_runner.py, ${REPO}/tests/test_agent.py, ${REPO}/tests/fake_host.py.

Hard constraints: fleet/ must import ONLY the Python standard library (it is shipped as a tarball to old 4 GB Debian 13 boxes and run with the system python3, no pip). Must work on Python 3.11+. No compiled files.

Build:
1. fleet/__init__.py: __version__ helper that reads fleet/VERSION if present else "dev".
2. fleet/common/http.py: tiny JSON client over urllib (get_json/post_json with timeout, bearer header, raises a HttpError carrying status and detail; handles connection errors as a distinct exception). fleet/common/sysinfo.py: cpu_pct from a /proc/stat delta between calls (first call returns 0.0), ram_used_mb/ram_total_mb from /proc/meminfo (MemTotal - MemAvailable), boot_id from /proc/sys/kernel/random/boot_id; all tolerant of missing files (return None/0).
3. fleet/worker/jobs.py: registry JOBS and the sleep job per the contract (1 s units, checkpoint {"elapsed": n}, result {"slept": seconds}); run signature run(params, checkpoint, emit, should_stop).
4. fleet/worker/runner.py: the child-process side (reads job JSON from stdin, installs SIGTERM handler -> stop flag, runs the job, prints JSONL lines exactly as the contract says, flushes after every line, exits 0) AND the parent-side Runner class used by the agent: start (subprocess.Popen with -m fleet.worker.runner, stdin pipe, stdout pipe, env OMP_NUM_THREADS=1, PYTHONPATH preserved), a reader thread that keeps last_checkpoint/last_progress/result/error/stopped, stop(grace=3.0) that sends SIGTERM, waits, then SIGKILL, kill(), poll().
5. fleet/worker/agent.py: the Agent class implementing the state machine and loop from the contract: BOOT (conf), REGISTER (backoff), ACTIVE, DRAINING; fixed 5 s monotonic heartbeat period (configurable: heartbeat_seconds from the host response, overridable for tests), out-of-cycle heartbeat after a drain, lost/claimed/preempt handling, releases carried in heartbeats (and via /checkpoint release=true during drain), complete/fail with retry, degraded + re-register after lease_seconds of misses, self-update (download tarball, sha256 verify, extract safely into app/<version> with a tar member path check, atomic symlink swap via os.replace of a temp symlink, exit 75), exit 78 when conf missing. Make the loop testable: run_forever() plus run_once()/tick() style methods, injectable clock/sleep, and a 'stop' event. Log with logging to stderr (journald). State dir from env FLEET_STATE_DIR (default /var/lib/fleet). Config file worker.conf JSON with mode 0600.
6. fleet/worker/__main__.py: argparse subcommands: run, enroll --host URL --token T [--name NAME], status.
7. deploy/install_worker.sh: bash, run as root on Debian 12/13: usage "install_worker.sh HOST_URL ENROLL_TOKEN [--name NAME]"; checks python3 >= 3.11 and ca-certificates/curl-or-wget-or-python-urllib availability (prefer python3 urllib for downloads so no extra packages are needed); creates system user fleet with home /var/lib/fleet; downloads /dl/version and /dl/worker.tar.gz, verifies sha256, extracts to /var/lib/fleet/app/<version> and points app/current at it; runs enroll (as the fleet user) to write worker.conf; installs deploy/fleet-worker.service content (embed it in the script via heredoc so one file is enough) to /etc/systemd/system/fleet-worker.service; systemctl daemon-reload, enable --now; prints status. Idempotent: re-running upgrades code and keeps identity (skips enroll if worker.conf exists, unless --reenroll). The literal string __FLEET_HOST_URL__ may appear as the default host URL (the host substitutes it when serving /install.sh).
8. deploy/fleet-worker.service: [Unit] Description, After=network-online.target, Wants=network-online.target, StartLimitIntervalSec=0; [Service] User=fleet, Group=fleet, Environment=PYTHONPATH=/var/lib/fleet/app/current, Environment=FLEET_STATE_DIR=/var/lib/fleet, ExecStart=/usr/bin/python3 -m fleet.worker run, Restart=always, RestartSec=3, RestartPreventExitStatus=78, TimeoutStopSec=15, Nice=5, NoNewPrivileges=yes, ProtectSystem=strict, ReadWritePaths=/var/lib/fleet, PrivateTmp=yes, MemoryMax=85%; [Install] WantedBy=multi-user.target.
9. tests/fake_host.py: an in-memory fake of the host protocol as a threading HTTPServer on 127.0.0.1:0 (register with enroll/worker tokens, heartbeat implementing renew/release/claim/preempt/lost/role-change/auto-idle in the simplest correct way, checkpoint/complete/fail, /dl/version + /dl/worker.tar.gz built from the real fleet package), with a control API the tests call directly (set_desired_role, enqueue_job, request_preempt, expire_lease, set_code_version). tests/test_runner.py: sleep job emits checkpoints, completes with result, SIGTERM mid-way yields stopped + last checkpoint, SIGKILL path after grace, malformed job error line. tests/test_agent.py (uses fake_host, heartbeat period 0.2 s, FLEET_STATE_DIR tmp): enroll writes conf 0600; register adopts held job and resumes from checkpoint; claims and completes a sleep job; role change mid-job drains, releases with checkpoint and sends the out-of-cycle heartbeat within the budget; preempt releases the job; lost job kills the runner; conf missing -> exit code 78; self-update downloads, verifies, swaps symlink and requests exit 75 (test with a short tarball served by fake_host). Use the real runner subprocess (python from sys.executable).

Run ONLY your own test files with pytest (other test files may be mid-write). Final report (plain text): files written, test command + result summary, deviations/decisions, anything unfinished.`

const DOCS_PROMPT = `You are writing docs and CI for step 1 of a multi-machine fleet. ${COMMON}

You own: ${REPO}/README.md, ${REPO}/docs/DESIGN.md, ${REPO}/.github/workflows/ci.yml. Do not touch anything else.

1. docs/DESIGN.md: take <SCRATCHPAD>/fleet-design-v2.md and adapt it to the owner's answers: (a) the HOST is a Windows 11 machine, so the host runs as a Docker Compose stack under Docker Desktop (postgres:16 + the fleet-host container, and fleet-exchange from step 4), bound to 127.0.0.1 and exposed to the tailnet with 'tailscale serve' (tailnet only, never Funnel); the same compose file runs on a Debian box if the host moves; (b) owner authentication is the Tailscale-User-Login header that tailscale serve injects (equal to FLEET_OWNER_LOGIN), not 'tailscale whois'; CSRF by Origin; (c) workers are Debian 13 (Python 3.13) so remove the python-build-standalone / Debian 10 material and the apt-free constraint; the installer needs root, python3 >= 3.11 and nothing else; (d) worker agent, protocol and step-1 API exactly as ${REPO}/docs/PROTOCOL.md (reference it rather than duplicating it); (e) all limit numbers are editable on the dashboard settings page, the quoted values are test defaults; (f) data is free-only for now, paid sources only after profit; (g) remove the open-questions section (answered) and keep a short 'Decisions' section recording the answers. Keep the same H2 structure otherwise; keep it tight.
2. README.md: what the project is (3 sentences); architecture sketch in words; HOST SETUP on Windows 11 step by step (install Docker Desktop and Tailscale, enable MagicDNS + HTTPS in the Tailscale admin console, clone, copy .env.example to .env and set FLEET_PUBLIC_URL=https://<machine>.<tailnet>.ts.net and FLEET_OWNER_LOGIN=<your tailscale login email>, 'docker compose up -d', then in an elevated PowerShell 'tailscale serve --bg --https=443 http://127.0.0.1:8080', verify 'tailscale funnel status' shows nothing, open the URL from the phone); WORKER INSTALL (mint an enroll token: 'docker compose exec host python -m host.cli enroll-token', then on each Debian box 'curl -fsSL https://<host>/install.sh | sudo bash -s -- https://<host> <token>', check with 'systemctl status fleet-worker' and 'journalctl -u fleet-worker -f'); HOW TO TEST STEP 1 (the owner test from the design's build table: two boxes appear in /api/fleet with CPU/RAM every 5 s; kill -9 the agent and it returns with the same id; send a sleep job with 'python -m host.cli send-job sleep --params {"seconds":60} --target any_idle', watch the role flip to backtest and progress climb, 'python -m host.cli role <worker> idle' mid-job and see the job go back to queued with its checkpoint and resume on the next backtest worker); DEVELOPMENT (create venv, pip install -e .[host,dev], run Postgres via 'docker compose up -d db' or any local Postgres, FLEET_TEST_DATABASE_URL, pytest); a short 'Build order' list with step 1 marked done and steps 2-5 pending. Use fenced code blocks for every command. No em-dashes.
3. .github/workflows/ci.yml: on push and pull_request; ubuntu-latest; services: postgres:16 with POSTGRES_PASSWORD=postgres and health check, port 5432; python 3.11 and 3.13 matrix; pip install -e .[host,dev]; env FLEET_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres; run pytest; plus a job that byte-compiles fleet/ with python3.11 and asserts that 'grep -rE "^(import|from) " fleet/' finds only stdlib modules (a small inline python check using sys.stdlib_module_names).

Final report: files written and any assumption you made.`

const INTEGRATE_PROMPT = (hostReport, workerReport) => `You are integrating the host and worker halves of step 1 of a multi-machine fleet. ${COMMON}

Both halves were just written by other agents (their reports are below). You own everything under ${REPO} now (except README.md, docs/, .github/), and you may fix bugs on either side, but keep PROTOCOL.md as the source of truth; if you must change the contract, change docs/PROTOCOL.md too and say so.

1. Run the whole suite: ${REPO}/.venv/bin/pytest ${REPO}/tests -x -q. Fix import errors, collection errors and failures.
2. Write ${REPO}/tests/test_e2e.py: a real end-to-end test. Start the FastAPI app (host.api.app.create_app with FLEET_DEV=1, FLEET_PUBLIC_URL=http://127.0.0.1:<port>) with uvicorn in a background thread on a free port against a fresh test database (reuse the conftest fixtures), start the host background loop with a 0.5 s period, then run the real worker Agent (fleet.worker.agent) in a background thread with heartbeat period 0.3 s and FLEET_STATE_DIR in a temp dir, using the real subprocess runner. Scenario, with assertions and generous but bounded waits (poll every 50 ms, timeouts around 10 s): mint enroll token via POST /api/enroll-token; enroll via the agent's enroll function; worker appears in /api/fleet online with cpu/ram; send a sleep job {"seconds": 6} target any_idle -> worker desired_role flips to backtest with auto_role, agent acks (reported_role backtest, switching false), job leased, progress rises above 0; change role to idle via POST /api/workers/{id}/role mid-job -> job returns to queued with checkpoint.elapsed >= 1 and the worker reports idle within 2 s at this test cadence; set role backtest again -> job resumes from the checkpoint (started elapsed > 0) and completes with result slept == 6, and total wall time proves it did not restart from zero; after completion the worker auto-returns to idle (auto_role was set by the second path? No: a manual role set clears auto_role, so instead verify auto-return in a separate scenario: a second any_idle job on the idle worker completes and the worker returns to idle by itself); cancel a running job -> cancelled; stop the agent thread, simulate crash by starting a new Agent instance from the same conf -> register rotates the token and the old token gets 401; a job leased by the dead instance is re-adopted via held_jobs. Keep the test under ~60 s wall time.
3. Also add tests/test_cli.py smoke tests for host.cli (enroll-token, workers, jobs, send-job, role) against the test DB using subprocess or by calling main() with argv.
4. Make sure ${REPO}/.venv/bin/pytest ${REPO}/tests -q passes completely and is reasonably fast (< 3 minutes). Flaky tests are not acceptable: run the suite three times.
5. Byte-compile check: ${REPO}/.venv/bin/python -m compileall -q ${REPO}/fleet ${REPO}/host.

Host agent report:
${hostReport}

Worker agent report:
${workerReport}

Final report: what you changed (file list with one line each), the final pytest summary line for three consecutive runs, any contract changes, and known limitations.`

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

const reviewLenses = [
  { key: 'concurrency', text: 'QUEUE AND CONCURRENCY CORRECTNESS: compare host/queue.py and the heartbeat handler against docs/PROTOCOL.md line by line. Hunt for: double claims, lease fencing holes (a stale token that can still write), the auto-return-to-idle race with the dispatcher, reaper vs heartbeat races, released jobs losing their checkpoint, preempt flags never cleared, transactions that are not actually single transactions (autocommit, pool misuse), SQL that is wrong under concurrent load, and any place the worker agent could run two runners for one job or drop a job silently. Write and run small targeted scripts or tests to confirm a suspicion before reporting it.' },
  { key: 'security', text: 'SECURITY: token handling (hashes only, constant-time compare, rotation, enroll single-use + expiry, token bound to path id), owner auth and the Origin check (can a worker token reach owner routes? can a missing header bypass?), tarball extraction path traversal in the agent self-update, symlink swap atomicity, worker.conf permissions, the install script running as root (quoting, download verification, what happens on a MITM or a malicious tarball), secrets in logs, SQL injection via settings keys or params, and anything a compromised worker could do to the host (e.g. mark another worker\'s job complete, flood job_events). Verify each claim by reading the code and, where cheap, running it.' },
  { key: 'ops', text: 'OPERATIONS AND WORKER ROBUSTNESS on old 4 GB Debian 13 boxes: the systemd unit (exit codes 75/78, Restart, RestartPreventExitStatus, ProtectSystem/ReadWritePaths vs where the agent writes, PrivateTmp, MemoryMax), installer idempotency and re-run upgrade path, self-update atomicity and rollback if the new code crashes at start, the 5 s monotonic heartbeat under slow networks (timeouts, backoff, degraded/re-register), drain timing (does a role change really complete within 10 s end to end? compute it from the code), runner subprocess zombies/pipes/blocking reads, memory footprint of the agent, Docker Compose/Dockerfile correctness (does the image build? does /install.sh find deploy/?), host background loop crash resilience, logging usefulness for journalctl. Verify by reading the code and running what you can (e.g. start the agent against the fake host, build nothing that needs Docker).' },
]

const reviewPrompt = (lens, integrateReport) => `You are an adversarial reviewer of step 1 of a multi-machine fleet (host API + Postgres job queue + worker agent). ${COMMON}
Lens: ${lens.text}
Integration report from the previous agent:
${integrateReport}
Report only findings you verified or could reproduce; for each give the concrete failure scenario and a minimal fix. Severity: high = data loss, double execution, money/security hole, worker bricked; medium = wrong behaviour under realistic conditions; low = hygiene. Do NOT modify any files; you may create scratch scripts under <SCRATCHPAD>/review-${lens.key}/ only.`

const fixPrompt = (findings) => `You are fixing verified review findings in step 1 of a multi-machine fleet. ${COMMON}
You own everything under ${REPO}. Apply every HIGH and MEDIUM finding below (and LOW ones that are trivial), keeping docs/PROTOCOL.md in sync if behaviour changes. Add a regression test for each high finding. Then run ${REPO}/.venv/bin/pytest ${REPO}/tests -q three times; all must pass. Also run ${REPO}/.venv/bin/python -m compileall -q ${REPO}/fleet ${REPO}/host.
If a finding is wrong, say why and skip it.
FINDINGS:
${JSON.stringify(findings, null, 1)}
Final report: per finding: fixed / skipped (reason); files changed; the three pytest summary lines.`

phase('Build')
log('Building host, worker and docs/CI in parallel')
const built = await parallel([
  () => agent(HOST_PROMPT, { label: 'build:host', phase: 'Build', effort: 'high' }),
  () => agent(WORKER_PROMPT, { label: 'build:worker', phase: 'Build', effort: 'high' }),
  () => agent(DOCS_PROMPT, { label: 'build:docs', phase: 'Build', model: 'sonnet', effort: 'medium' }),
])
const [hostReport, workerReport, docsReport] = built.map(r => r || '(agent returned nothing)')

phase('Integrate')
const integrateReport = (await agent(INTEGRATE_PROMPT(hostReport, workerReport), { label: 'integrate', phase: 'Integrate', effort: 'high' })) || '(no report)'

phase('Review')
const reviews = (await parallel(reviewLenses.map(l => () =>
  agent(reviewPrompt(l, integrateReport), { label: `review:${l.key}`, phase: 'Review', schema: REVIEW_SCHEMA, model: 'opus', effort: 'high' })
))).filter(Boolean)
const findings = reviews.flatMap((r, i) => r.findings.map(f => ({ ...f, lens: reviewLenses[i] ? reviewLenses[i].key : 'unknown' })))
const actionable = findings.filter(f => f.severity !== 'low')
log(`${findings.length} findings (${actionable.length} high/medium)`)

phase('Fix')
const PARTIAL = `
IMPORTANT CONTEXT: a previous fix attempt was interrupted by a container restart after it had modified these files only: host/migrations/0002_review_fixes.sql (new: adds workers.prev_token_hash and jobs.target_auto), host/auth.py, host/config.py (adds allow_worker_ips), host/heartbeat.py (rewritten), host/leases.py, host/scheduling.py, host/settings.py (rewritten with validation), host/api/deps.py, host/api/workers.py, pyproject.toml (package-data for migrations). It had NOT touched fleet/, deploy/ or tests/. The tree may therefore be inconsistent and some tests may fail right now. Start by reading those files and running the tests you own, then finish the job. Run tests as ${REPO}/.venv/bin/python -m pytest <files> -q -p no:cacheprovider.
`
const byHost = f => f.file.startsWith('host/') || f.file === 'pyproject.toml'
const hostFindings = findings.filter(byHost)
const workerFindings = findings.filter(f => !byHost(f))

const fixHostPrompt = `You are fixing verified review findings in the HOST half of step 1 of a multi-machine fleet. ${COMMON}
${PARTIAL}
You own: ${REPO}/host/**, ${REPO}/pyproject.toml, ${REPO}/docs/PROTOCOL.md (keep it in sync with any behaviour change and mark changes with a short "(changed: reason)" note), ${REPO}/tests/conftest.py, tests/test_queue.py, tests/test_workers_api.py, tests/test_owner_api.py, tests/test_cli.py. Another agent is concurrently fixing fleet/, deploy/, tests/fake_host.py, tests/test_agent.py, tests/test_runner.py; do not touch those. tests/test_e2e.py is handled by a later integration agent; it may fail for now.
Apply every HIGH and MEDIUM finding below and the LOW ones that are cheap; add a regression test for each HIGH and MEDIUM. Specific guidance:
- Token rotation: implement prev_token_hash exactly as the findings suggest (register accepts current or previous hash; first successful heartbeat with the new token clears prev_token_hash; heartbeat never accepts the previous hash). Document in PROTOCOL.md.
- Commit before response: use Depends(db_conn, scope="function") (FastAPI 0.142 supports it) or explicit commits; add a test that proves the write is visible to a second connection as soon as the response returns.
- target_auto: system-chosen targets are cleared on release and lease expiry so the job can be re-dispatched; owner-chosen targets stay.
- Claim idempotency: if a worker heartbeats with want_job=true and an empty jobs[] while it still holds a valid lease, re-offer that leased job in claimed[] (same job, same token) instead of claiming another one; also mark it in PROTOCOL.md.
- Settings: type/range validation per key with a schema table, audit_log row on change.
- Owner auth: when a request carries X-Forwarded-For / the tailnet peer IP and that IP belongs to a registered worker, reject owner routes unless config allow_worker_ips is set; trust X-Forwarded-For only when config trust_proxy is true (default true, since the host sits behind tailscale serve; document it).
- Jobs routes: the caller's worker id must equal lease_worker_id. Cap checkpoint/result payload sizes (64 KiB) and dedupe the re-leased job_events spam (one row per register, not per retry loop) or rate-limit register per worker (1/s).
If a finding is wrong, say why and skip it. When done, all of your test files must pass three times in a row. Finish with: per finding fixed/skipped(reason); files changed; three pytest summary lines.
FINDINGS (host side):
${JSON.stringify(hostFindings, null, 1)}`

const fixWorkerPrompt = `You are fixing verified review findings in the WORKER half of step 1 of a multi-machine fleet. ${COMMON}
${PARTIAL}
You own: ${REPO}/fleet/**, ${REPO}/deploy/**, ${REPO}/tests/fake_host.py, tests/test_agent.py, tests/test_runner.py. Another agent is concurrently fixing host/ and the host tests; do not touch those. The host side is adding: workers.prev_token_hash (register accepts the previous token once; first heartbeat with the new token clears it), claim re-offer (a heartbeat with want_job and empty jobs[] while a valid lease exists returns that same job again in claimed[]), target_auto, and Depends scope fixes. Mirror the register/prev-token and re-offer behaviour in tests/fake_host.py so agent tests stay faithful.
Apply every HIGH and MEDIUM finding below and the cheap LOW ones; add a regression test for each HIGH and MEDIUM. Specific guidance:
- Self-update rollback: keep app/previous; write app/pending.json {version, starts}; __main__ run increments starts before importing the agent and resets it after the first successful register; after 3 failed starts of a pending version, repoint current to previous and exit 75; never update to a version listed in app/bad_versions. Test with a tarball whose agent.py raises at import.
- Never drop pending /complete or /fail posts: flush them (with bounded retries) before re-register, before self-update exit 75 and in shutdown; persist unsent posts to the state dir so a crash does not lose a finished result, and resend on start.
- shutdown(): final heartbeat must send want_job=false.
- Lease deadline: measure from the send time of the last acknowledged heartbeat and check it on every tick and on every heartbeat failure.
- Runner: start_new_session=True and kill the whole process group (os.killpg) on terminate/kill; handle ProcessLookupError.
- Installer: extract with tar --no-same-owner --no-same-permissions after listing members and rejecting anything that is not a regular file or directory under fleet/ (or run the Python updater as the fleet user); warn and update worker.conf when HOST_URL differs from the stored host_url; allow the enroll token via env FLEET_ENROLL_TOKEN as an alternative to the command line and say so in the usage text.
- Log job failure tracebacks to stderr (journald) too; drop the duplicate timestamp in the log format.
If a finding is wrong, say why and skip it. When done, your test files must pass three times in a row. Finish with: per finding fixed/skipped(reason); files changed; three pytest summary lines.
FINDINGS (worker side):
${JSON.stringify(workerFindings, null, 1)}`

log(`fixing ${hostFindings.length} host-side and ${workerFindings.length} worker-side findings in parallel`)
const fixes = await parallel([
  () => agent(fixHostPrompt, { label: 'fix:host', phase: 'Fix', effort: 'high' }),
  () => agent(fixWorkerPrompt, { label: 'fix:worker', phase: 'Fix', effort: 'high' }),
])
const [fixHostReport, fixWorkerReport] = fixes.map(r => r || '(no report)')

const fixIntegratePrompt = `You are the final integrator for step 1 of a multi-machine fleet. ${COMMON}
Two agents just applied review fixes to the host side and the worker side in parallel (reports below). You own everything under ${REPO}. Tasks:
1. Run the full suite three times: ${REPO}/.venv/bin/python -m pytest ${REPO}/tests -q -p no:cacheprovider. Fix any failure, including tests/test_e2e.py, which must still exercise the real host + real agent end to end; extend it with: a lost register response (host rotates, agent retries with the old token and succeeds via prev_token_hash) and a claim re-offer after a dropped heartbeat response.
2. Check that docs/PROTOCOL.md matches the code after both fix passes (prev_token_hash, re-offer, target_auto, settings validation, owner auth IP rule, installer notes) and that README.md's test instructions still work; fix mismatches.
3. ${REPO}/.venv/bin/python -m compileall -q ${REPO}/fleet ${REPO}/host must pass; confirm fleet/ imports only the standard library (python -c using sys.stdlib_module_names over every import line).
4. Verify the Docker build inputs are consistent (Dockerfile copies deploy/ and host/migrations/, .env.example lists every config field in host/config.py) without running Docker.
Finish with: files changed (one line each), three pytest summary lines, and any remaining known issue.
HOST FIX REPORT:
${fixHostReport}
WORKER FIX REPORT:
${fixWorkerReport}`
const fixReport = (await agent(fixIntegratePrompt, { label: 'fix:integrate', phase: 'Fix', effort: 'high' })) || '(no report)'

return { hostReport, workerReport, docsReport, integrateReport, reviews, fixHostReport, fixWorkerReport, fixReport }
