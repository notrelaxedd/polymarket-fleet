export const meta = {
  name: 'fleet-step5-build',
  description: 'Step 5: live trading behind the switch. Live gateway with signing, live switch, reconciliation, auto-kill, smoke order, direct cancel-all, dashboard, docs; integrate with a fake live gateway, review, fix',
  phases: [
    { title: 'Build', detail: 'gateway, live core, dashboard, docs in parallel' },
    { title: 'Integrate', detail: 'full suite, e2e live flow on a fake gateway, screenshots' },
    { title: 'Review', detail: 'live safety, gateway robustness, operability' },
    { title: 'Fix', detail: 'apply findings, rerun suite' },
  ],
}

const REPO = '/home/user/polymarket-fleet'
const SHOTS = '<SCRATCHPAD>/screenshots-step5'
const COMMON = `
Repository: ${REPO} (git; steps 1-4 committed, 502 tests green; do NOT commit or push, the orchestrator does that).
Python: ${REPO}/.venv/bin/python (pynacl installed for Ed25519). Run tests from ${REPO} as .venv/bin/python -m pytest <files> -q -p no:cacheprovider -o addopts=. Postgres 16 at postgresql://postgres:postgres@127.0.0.1:5432/postgres (if "connection refused": pg_ctlcluster 16 main start). Per-test databases come from a migrated template (migration 0005 is already there).
Read first, completely: ${REPO}/docs/LIVE.md (the step 5 spec), the "Step 5 additions" at the end of ${REPO}/docs/PROTOCOL.md, ${REPO}/docs/TRADING.md (step 4 behaviour you extend), ${REPO}/host/migrations/0005_live.sql, and the step 4 code you touch: host/exchange/{main,executor,state,cli,probe}.py, host/exchange/adapters/{base,polymarket_us}.py, host/kill.py, host/trading/{limits,assignments,orders,ledger}.py, host/settings.py, tests/conftest.py (helpers: trade_setup, make_assignment, approve, enable_live, set_setting, insert_market/snapshot ...).
Style: Python 3.11+, type hints, modules under ~300 lines, plain docstrings, no em-dashes anywhere. Keys are never logged, stored or returned.
No network in this sandbox: every exchange host is blocked; tests use fixtures and fakes only.
Only touch the paths you own; other agents are writing the other paths concurrently. Use exactly the cross-agent signatures below.
`
const SIGNATURES = `
Cross-agent signatures (implement or call exactly these):
- host/exchange/credentials.py (gateway agent): class Credentials(key: str, secret: bytes (32-byte Ed25519 seed), passphrase: str | None, key_hint: str (last 4 chars)); load(env: Mapping[str, str] | None = None) -> Credentials | None (env vars POLYMARKET_US_API_KEY, POLYMARKET_US_API_SECRET base64 or hex auto-detected, POLYMARKET_US_PASSPHRASE); never prints the secret (repr redacts).
- host/exchange/adapters/signing.py (gateway agent): sign_headers(creds, method: str, path: str, body: bytes, timestamp: str, auth_config: dict) -> dict[str, str]; timestamp_now(auth_config, clock) -> str; message_for(method, path, body, timestamp, auth_config) -> bytes.
- host/exchange/adapters/polymarket_us_live.py (gateway agent): class AuthError(SourceError); class LiveGateway(OrderGateway) with __init__(self, creds, config: dict (the whole market_source_config.polymarket_us block, defaults applied), limiter=None, http=None (callable (method, url, headers, body, timeout) -> (status, headers, text) for tests), clock=None); name = "polymarket_us"; place/cancel/open_orders/fills/balance per docs/LIVE.md returning plain dicts with keys exchange_order_id, client_order_id, market_ref, price, size, filled_size, status (open_orders) and exchange_fill_id, exchange_order_id, client_order_id, price, size, fee_cents, ts (fills) and balance_cents, buying_power_cents, server_time (balance); last_skew_ms attribute updated from the Date header; cancel_all() -> int; probe_account() -> dict {status, payload (truncated), key_hint, error}. live_config_with_defaults(config) -> dict adding the auth and live blocks.
- host/exchange/main.py (live-core agent): ExchangeLoop(pool, clock=utcnow, gateway_factory: Callable[[dict config, Credentials | None], OrderGateway] | None = None); the executor becomes Executor(paper_gateway, live_gateway) choosing by order mode.
- host/kill.py (live-core agent): auto_kill(conn, reason: str, detail: dict) -> bool (set_kill with actor "auto:<reason>" plus an audit row auto_kill); live_off(conn, actor: str, reason: str) -> dict (live_enabled false, live assignments halted with their orders cancelled, audit live_off).
- host/trading/live.py (live-core agent): expected_phrase(conn, now=None) -> str; enable_live(conn, actor, confirm: str, now=None) -> dict; disable_live(conn, actor, reason="owner") -> dict; live_state(conn, now=None) -> dict {live_enabled, live_enabled_at, live_enabled_by, credentials_present, auth_ok, auth_checked_at, auth_age_s, balance_cents, buying_power_cents, clock_skew_ms, last_auth_error, auto_kill_reasons: [..], expected_phrase}.
- host/exchange/live_sync.py (live-core agent): auth_check(conn, gateway, now, creds_present: bool) -> dict; poll_fills(conn, gateway, now) -> int; audit_open_orders(conn, gateway, now) -> dict; startup_reconcile(conn, gateway, now) -> dict.
- host/exchange/smoke.py (live-core agent): run_smoke(pool, confirm: str, market_id=None, hold_seconds=None, gateway=None, now=None, sleep=time.sleep) -> dict timeline.
- tests/fake_gateway.py (live-core agent): FakeLiveGateway(OrderGateway) with scripted behaviours: place ok / timeout (raise TimeoutError) / 429 / auth error; remote open orders store; fills queue; balance; counters; helpers add_remote_order(client_id=None, ...), add_fill(...), fail_next(method, exc), set_skew_ms.
- Dashboard agent reads host.trading.live.live_state and posts to host.trading.live.enable_live/disable_live.
`

const GATEWAY_PROMPT = `You are building the LIVE GATEWAY of step 5: credentials loading, Ed25519 request signing, and the authenticated Polymarket US client behind the OrderGateway interface, all configurable because the real API is unverified. ${COMMON}
${SIGNATURES}
You own: ${REPO}/host/exchange/credentials.py, ${REPO}/host/exchange/adapters/signing.py, ${REPO}/host/exchange/adapters/polymarket_us_live.py, ${REPO}/host/exchange/adapters/polymarket_us.py (only to add live_config_with_defaults and the auth/live default blocks next to the existing public defaults), ${REPO}/tests/test_live_gateway.py, ${REPO}/tests/fixtures/polymarket_us_live_*.json. Do not touch main.py, executor.py, cli.py, kill.py, dashboard, docs.
Build exactly docs/LIVE.md "Credentials", "Signing" and "Live gateway":
1. credentials.load: both encodings, 32-byte seed check (a 64-byte secret key is accepted by taking its first 32 bytes), returns None when either variable is missing; Credentials.__repr__ shows only key_hint.
2. signing: template default "{timestamp}{method}{path}{body}", timestamp "ms"|"s", encoding base64|hex, header names configurable, passphrase header only when set; path includes the query string; body is the exact bytes sent. Use nacl.signing.SigningKey.
3. LiveGateway: urllib transport (10 s timeout, injectable http for tests), limiter tokens per category (orders for place, cancels for cancel, account for open_orders/fills/balance) when a limiter is given, Date header -> last_skew_ms, 401/403 -> AuthError, 429 -> RateLimited (call limiter.on_429 if present), 5xx/transport -> SourceError, unknown payload shape -> SourceError with truncated payload; field names configurable for request bodies (place) and responses; cancel_all = configured path or list-then-cancel-each; probe_account redacts the key.
4. tests/test_live_gateway.py with every name listed in docs/LIVE.md for this file: deterministic signature for a fixed seed and message (compute the expected value in the test with pynacl from the spec'd message so the test documents the assumption), header set, ms vs s timestamps, body bytes signed, request building for each call against the config (assert method, path, headers present, JSON body fields), responses from fixtures (write realistic fixture JSON for place, open orders, fills, balance plus malformed variants), 401/429 mapping, probe redaction, credentials formats (base64, hex, 64-byte, missing).
Run your tests three times. Final report: files written, summary lines, the exact assumptions (paths, headers, signing template, field names) in a table the owner can check against the real docs.`

const CORE_PROMPT = `You are building the LIVE CORE of step 5: the typed live switch, auto-kill, live reconciliation and fills, the authentication task, the smoke order, direct cancel-all, and their tests. ${COMMON}
${SIGNATURES}
You own: ${REPO}/host/trading/live.py (new), ${REPO}/host/api/owner_live.py (new: POST /live, POST /live/off, GET /api/live, POST /api/exchange/probe-account), ${REPO}/host/api/app.py (mount only), ${REPO}/host/kill.py, ${REPO}/host/settings.py (refuse live_enabled through the settings API with "use /live"), ${REPO}/host/trading/limits.py (buying_power freshness per docs/LIVE.md, approve_smoke), ${REPO}/host/trading/assignments.py (live gates reuse live.py helpers; live_off halting), ${REPO}/host/exchange/{main,executor,live_sync,smoke,state,cli}.py, ${REPO}/tests/fake_gateway.py, ${REPO}/tests/test_live_switch.py, ${REPO}/tests/test_live_executor.py, ${REPO}/tests/test_smoke.py, ${REPO}/tests/test_kill_switch.py (auto-kill cases), ${REPO}/tests/conftest.py (additions only), ${REPO}/docs/PROTOCOL.md (sync). Do not touch the gateway agent's files (import them; if host/exchange/adapters/polymarket_us_live.py is not there yet when you run tests, your tests must use tests/fake_gateway.py only, and main.py's default factory must import the live gateway lazily inside the function).
Build exactly docs/LIVE.md:
1. live.py and the owner routes; the exact dated phrase in the owner's tz; the three preconditions; audit rows; /api/settings refuses live_enabled; kill and the daily-loss trip keep turning live off through kill.live_off semantics (halt live assignments, cancel live orders through the exchange).
2. Executor with two gateways chosen by mode; reconciliation of submitting rows from open_orders then fills then expiry after submitting_grace_s; never resubmit blind; cancel retry until open_orders no longer lists the order; cancel_all list-then-cancel.
3. live_sync.py tasks wired into ExchangeLoop (auth every auth_probe_interval_s and at start; fills every live_fills_poll_s while live orders are active; open-order audit every open_orders_audit_s and at start; startup_reconcile before any submission): auto-kill triggers exactly as docs/LIVE.md (auth failures threshold, clock skew, unknown remote order (cancel it too), unknown fill, ambiguous reconciliation); exchange_state columns from migration 0005 maintained; credentials_present from credentials.load (import lazily).
4. smoke.py + CLI exchange-smoke; CLI cancel-all --direct (loads credentials, builds the live gateway, lists and cancels with retry, updates rows with releases, prints a table), probe-account, auth-check.
5. Tests with every name in docs/LIVE.md for test_live_switch.py, test_live_executor.py and test_smoke.py, all on FakeLiveGateway; dashboard_form_and_pill may be a thin HTTP assertion on GET /api/live and the top bar fragment if the dashboard agent's template is present, else assert the JSON only and note it.
Run your tests three times plus tests/test_limits.py and tests/test_exchange.py. Final report: files written, summary lines, deviations, spec problems.`

const DASH_PROMPT = `You are building the DASHBOARD side of step 5. ${COMMON}
${SIGNATURES}
You own: ${REPO}/host/api/dashboard_forms.py, ${REPO}/host/api/dashboard_trading.py, ${REPO}/host/api/dashboard.py, ${REPO}/host/templates/**, ${REPO}/host/static/**, ${REPO}/host/web.py, ${REPO}/host/settings_forms.py, ${REPO}/host/views.py, ${REPO}/tests/test_dashboard.py, ${REPO}/tests/hw/**, ${REPO}/docs/DASHBOARD.md. Do not touch host/trading, host/exchange, host/kill.py, host/api/owner_live.py, docs/LIVE.md, README.
Build docs/LIVE.md "Dashboard additions": Settings "Live trading" group (state with since/by whom, the typed enable form showing the exact phrase for today as a hint, Disable live button, credentials present yes/no, auth status + age, balance and buying power in dollars, clock skew, last auth error, auto-kill reasons from the audit log) posting to host.trading.live.enable_live / disable_live (import lazily; if the module is missing at test time, seed exchange_state and settings rows directly and skip the post assertions with a clear reason the integrator will remove); top bar LIVE pill green when live_enabled, killed bar shows the auto-kill reason (latest audit row auto_kill since the last kill_reset); /trading exchange box with auth, balance, buying power, open live orders count, smoke orders flagged, live assignments in a distinct colour; the Settings form for live_enabled removed from the generic settings groups. Tests: every element renders, the enable form posts the phrase, wrong phrase shows the inline error, pill states, auto-kill reason shown, phone layout assertions. Extend tests/hw/screenshots.py + seed: settings (live group on and off), trading with a live assignment and a smoke order, fleet with LIVE pill, killed bar with an auto-kill reason, at 390 and 1280, light and dark, into ${SHOTS}/.
Run your tests three times. Final report: files written, summary lines, deviations.`

const DOCS_PROMPT = `You are writing docs for step 5 of a multi-machine fleet. ${COMMON}
You own: ${REPO}/README.md and ${REPO}/docs/DESIGN.md only.
1. README.md: a "Live trading (step 5)" section: where to get Polymarket US API keys (the Polymarket US app, after identity verification; the API docs at docs.polymarket.us), creating exchange.env on the host with POLYMARKET_US_API_KEY and POLYMARKET_US_API_SECRET (and optional passphrase), root-only permissions (Windows icacls and Debian chmod examples), restarting only the exchange service, checking Settings shows credentials present and auth OK (or running docker compose exec exchange python -m host.exchange.cli auth-check and probe-account; paste the probe output back if auth fails, since the signing rule and paths are assumptions kept in market_source_config); "How to test step 5": the typed phrase to enable live (exact text with today's date), the smoke order command (docker compose exec exchange python -m host.exchange.cli exchange-smoke --confirm "SMOKE YYYY-MM-DD") and what to expect on /trading and in the exchange UI, KILL cancelling it and turning live off, RESUME then re-enable, cancel-all --direct with the exchange service stopped (docker compose stop exchange; docker compose run --rm exchange python -m host.exchange.cli cancel-all --direct), the auto-kill reasons and what each means, and the rule that model-driven live orders only begin when a lineage is live_eligible (thresholds in Settings) with no override; a short "Risks" list (unverified API, single host, paper fills optimistic, Windows host sleeping). Build order: all five steps done. No em-dashes.
2. docs/DESIGN.md: step 5 row done; the final state of the system in the Summary; reference docs/LIVE.md.
Final report: files written and assumptions.`

const INTEGRATE_PROMPT = (reports) => `You are integrating step 5 after four agents built it in parallel (reports below). ${COMMON}
${SIGNATURES}
You own everything under ${REPO}. docs/LIVE.md and docs/PROTOCOL.md are the spec; fix whichever side is wrong when they disagree and say so. Remove any skips the builders left for missing modules once everything is present.
1. Full suite three times: .venv/bin/python -m pytest tests -q -p no:cacheprovider -o addopts=. Fix every failure in any file.
2. Extend tests/test_e2e.py (real host + real agent + exchange loop in a thread, market_source sim) with a step 5 phase using a FakeLiveGateway injected through ExchangeLoop(gateway_factory=...): seed credentials_present/auth via the fake gateway's balance; enable live through the HTML form with the exact phrase (wrong phrase first, inline error); force a lineage live_eligible in the DB (the gate itself is tested elsewhere); create a live assignment; the trade worker proposes; approval passes buying power; the executor places on the fake gateway (exchange id recorded); a fill arrives through poll_fills; a second order is placed with a timeout and reconciled from open_orders; an unknown remote order appears -> auto-kill with the reason on the top bar, live off, live orders cancelled through the gateway; RESUME; re-enable; run the smoke order via host.exchange.smoke.run_smoke with the fake gateway (rests, cancelled after a 1 s hold); cancel-all --direct through the CLI entry point with the loop stopped; settle via simulate-final allowed? (sim source: yes) -> bets rows for the live assignment and live P&L in /api/pnl by_mode. Keep the e2e under ~240 s.
3. Screenshots: run tests/hw/screenshots.py into ${SHOTS}/ with the step 5 seed; keep the layout assertions.
4. compileall on fleet/ and host/; stdlib-only check on fleet/; no em-dashes; grep that no code path logs or returns the secret (grep for POLYMARKET_US_API_SECRET and "secret" in host/ and confirm only credentials.py reads it).
Final report: files changed (one line each), three pytest summary lines, PNG list, the assumptions table from the gateway report, remaining known issues.
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
  { key: 'live-safety', text: 'LIVE SAFETY: can real money move without all three gates (typed switch with today\'s date, lineage live_eligible, fresh auth)? Attack: the phrase check (tz boundary, whitespace, unicode), live_enabled writable anywhere else (settings API, forms, CLI, kill reset), live assignments surviving a demotion or live-off, approvals racing a live-off, buying power freshness and double counting, the smoke order path bypassing limits, auto-kill paths (do they really cancel through the gateway and keep cancelling until confirmed? can an unknown fill be ignored?), reconciliation double-submitting or marking filled orders expired, cancel-all --direct correctness with rows in every status, key custody (grep every place the secret could leak: logs, DB, audit detail, probe payloads, exceptions, repr), the host container never importing credentials. Reproduce with scripts under <SCRATCHPAD>/review5-safety/ before reporting.' },
  { key: 'gateway', text: 'GATEWAY ROBUSTNESS: signing correctness and determinism (message bytes, timestamp units, encoding, path with query), config defaults merging with owner-edited config, transport errors and retries, 429 handling with the limiter, parsing of every response shape with missing/odd fields (sizes as strings, prices as strings, ISO vs epoch times, nested data envelopes), idempotency of fills by exchange_fill_id, open-order audit false positives (a just-placed order not yet in our DB? an order placed by the smoke CLI? partially filled rows), skew measurement from Date headers, time-in-force/expiry fields, cancel_all list-then-cancel under rate limits, probe redaction. Verify with scripts and the fixtures.' },
  { key: 'operability', text: `OPERABILITY AND DOCS: read README's live section and docs/LIVE.md against the code: does every documented command exist with those flags, is the enable phrase exactly what the form expects, does the killed bar show auto-kill reasons, are auth/balance/skew visible and labelled with units, is the smoke order visible and distinguishable, can the owner recover from each auto-kill reason by following the docs, is cancel-all --direct usable with the service stopped (compose run), are credentials instructions safe (permissions, never in .env). Look at every PNG under ${SHOTS}/ (Read them). Report concrete fixes.` },
]
const reviewPrompt = (lens, report) => `You are an adversarial reviewer of step 5 (live trading behind the switch) of a multi-machine fleet. ${COMMON}
Lens: ${lens.text}
Integration report:
${report}
Report only findings you verified; give a concrete failure scenario and a minimal fix each. Severity: high = real money could move outside the gates or limits, a key could leak, the kill or auto-kill could fail, or an order could be duplicated; medium = wrong behaviour under realistic use or a clear operability failure; low = hygiene. Do NOT modify repository files.`

const fixPrompt = (findings) => `You are fixing verified review findings in step 5 of a multi-machine fleet. ${COMMON}
You own everything under ${REPO}. Apply every HIGH and MEDIUM finding (and cheap LOW ones), keep docs/LIVE.md, docs/PROTOCOL.md, docs/DASHBOARD.md and README.md in sync, add a regression test for each HIGH and MEDIUM. If a finding is wrong, say why and skip it. Then: full suite three times (.venv/bin/python -m pytest tests -q -p no:cacheprovider -o addopts=), compileall on fleet/ and host/, stdlib-only check, screenshots regenerated into ${SHOTS}/ if templates changed. Append each pytest summary line to ${REPO}/.fix5_runs.txt as you go.
Final report: per finding fixed/skipped(reason); files changed; three pytest summary lines.
FINDINGS:
${JSON.stringify(findings, null, 1)}`

phase('Build')
log('Building gateway, live core, dashboard, docs in parallel')
const built = await parallel([
  () => agent(GATEWAY_PROMPT, { label: 'build:gateway', phase: 'Build', effort: 'high' }),
  () => agent(CORE_PROMPT, { label: 'build:live-core', phase: 'Build', effort: 'high' }),
  () => agent(DASH_PROMPT, { label: 'build:dashboard', phase: 'Build', effort: 'high' }),
  () => agent(DOCS_PROMPT, { label: 'build:docs', phase: 'Build', model: 'sonnet', effort: 'medium' }),
])
const reports = built.map((r, i) => `=== ${['gateway', 'live-core', 'dashboard', 'docs'][i]} ===\n${r || '(no report)'}`).join('\n\n')

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