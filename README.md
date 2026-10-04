# polymarket-fleet

A small fleet of machines that builds, tests and (much later) runs NFL prediction-market models. One host runs a Postgres-backed job queue, a dashboard and the trading limits; any number of Debian worker boxes run a tiny stdlib-only agent that takes jobs according to the role you give it. Everything is private to your Tailscale network, paper trading is the default, and the host enforces every limit.

## Architecture

The host is a Windows 11 machine running a Docker Compose stack: `db` (postgres:16), `host` (the FastAPI app that serves the worker API, the owner API and the dashboard) and `exchange` (`fleet-exchange`, the second process: market snapshots, the order executor, the paper fill simulator, settlement and scoring; it is the only process that ever talks to a market or exchange). `db` and `host` publish ports on `127.0.0.1` only; `exchange` publishes none. `tailscale serve` on the host OS puts HTTPS in front of port 8080 for your tailnet and injects a `Tailscale-User-Login` header, which the app compares with `FLEET_OWNER_LOGIN` to identify you. Workers are Debian 13 boxes: each runs the `fleet-worker` systemd service, which registers with an enroll token, sends a heartbeat every 5 seconds, claims leased jobs for its current role, checkpoints as it goes, and updates its own code from the host. The wire contract is in `docs/PROTOCOL.md`; the full design is in `docs/DESIGN.md`.

The dashboard is server-rendered HTML (Jinja2, one stylesheet, a little vanilla JS) served by the same `host` container, so there is no separate frontend to build. Owner pages and routes authenticate through the `Tailscale-User-Login` header; a request with a different login gets a 401 page. A worker machine's IP is refused on owner routes even if it sends the header, so a compromised worker cannot act as you. The spec is in `docs/DASHBOARD.md`.

## Host setup (Windows 11)

1. Install Docker Desktop (WSL 2 backend) and Tailscale for Windows. Sign both in; set Docker Desktop to start at login.
2. In the Tailscale admin console (DNS page) enable MagicDNS and HTTPS certificates.
3. Clone the repository and create your `.env`:

```powershell
git clone <this repo url> polymarket-fleet
cd polymarket-fleet
copy .env.example .env
```

4. Edit `.env` and set (use your machine name and tailnet name from the Tailscale admin console):

```
FLEET_PUBLIC_URL=https://<machine>.<tailnet>.ts.net
FLEET_OWNER_LOGIN=<your tailscale login email>
```

5. Start the stack. This now starts three services, `db`, `host` and `exchange`:

```powershell
docker compose up -d
docker compose ps
```

   `exchange.env` is optional (compose starts without it, and the system stays paper-only). It holds the Polymarket US API keys and is created in "Live trading (step 5)" below; `.gitignore` and `.dockerignore` keep it (and every other `*.env` file except `.env.example`) out of git and out of the image. Only the `exchange` service loads it, never `host`.

6. In an elevated PowerShell, expose port 8080 to the tailnet:

```powershell
tailscale serve --bg --https=443 http://127.0.0.1:8080
```

7. Verify nothing is public. This must show nothing enabled:

```powershell
tailscale funnel status
```

8. Open `https://<machine>.<tailnet>.ts.net` from your phone (on the tailnet). Check `https://<machine>.<tailnet>.ts.net/healthz` returns `{"ok": true, "db": true}`.

Also turn off sleep on the host machine (Settings, System, Power) so workers can always reach it.

## Worker install (Debian 13)

Mint a single-use enroll token (valid 1 hour) on the host:

```powershell
docker compose exec host python -m host.cli enroll-token
```

On each Debian box, as a user with sudo (needs root, python3 >= 3.11 and systemd, all present on Debian 13):

```bash
curl -fsSL https://<host>/install.sh | sudo bash -s -- https://<host> <token>
```

If `curl` is missing (minimal installs ship `wget` only), use `wget -qO- https://<host>/install.sh | sudo bash -s -- https://<host> <token>` or `sudo apt install curl` first.

Check it:

```bash
systemctl status fleet-worker
journalctl -u fleet-worker -f
```

Re-running the installer upgrades the worker and keeps its identity. To keep the token out of the sudo log and the process list, pass it through the environment instead: `curl -fsSL https://<host>/install.sh | sudo FLEET_ENROLL_TOKEN=<token> bash -s -- https://<host>` (or `--token-file PATH`).

## How to test step 1

Run these commands on the host with `docker compose exec host` in front of each `python -m host.cli ...` (or set `FLEET_DEV=1` and run them from a venv).

1. Two boxes appear. Install two workers, then open `https://<host>/api/fleet`. Both show up with `online: true`, and `cpu_pct` / `ram_used_mb` change every 5 seconds.
2. Crash recovery. On one box:

```bash
sudo pkill -9 -f 'fleet.worker run'
```

   systemd restarts it within seconds and `/api/fleet` shows the same worker id (the token rotates, the id does not).
3. Send a sleep job and watch it run:

```bash
python -m host.cli send-job sleep --params '{"seconds":60}' --target any_idle
```

   An idle worker flips its `desired_role` to `backtest`, claims the job, and `progress` climbs in `/api/fleet` and `python -m host.cli jobs`. When the job ends the worker returns to `idle` on its own.
4. Preemption with resume. Send another sleep job, and mid-job take the worker off its role:

```bash
python -m host.cli role <worker> train
```

   The job goes back to `queued` with its checkpoint (`{"elapsed": n}`) and resumes from there on the next worker in the `backtest` role: the dispatcher hands it to an idle worker, or put one into `backtest` yourself with `python -m host.cli role <other worker> backtest`. (Setting the first worker to `idle` instead also works, but an idle worker is a dispatch target, so the job may come straight back to it. To keep a worker out of the pool, disable it: `POST /api/workers/<worker>/enabled` with `{"enabled": false}`.)

On Windows PowerShell, quote the JSON for `--params` as `'{\"seconds\":60}'` if the shell strips quotes.

## How to test step 2

Everything here is done from your phone or the host, with workers installed as in step 1. Commands run on the host.

1. Open the dashboard on the phone (on the tailnet):

```
https://<machine>.<tailnet>.ts.net/
```

   The Fleet page shows one card per worker: an online dot, a role dropdown, the current job with a progress bar, and CPU, RAM and clock skew. The top bar shows the PAPER pill, P&L and the red KILL button. P&L reads $0.00 until step 4, because there are no bets or fills yet.
2. Change a role from the dropdown with a stopwatch in hand. The card says "switching to <role>" until the worker acknowledges. Expect under 10 seconds. If the card still says "switching" after 20 seconds it turns red; that is a failure, check `journalctl -u fleet-worker` on that box.
3. Run the automated version of the same timing (it sends a sleep job, flips the role to `train`, and prints the seconds from the click to the ack):

```powershell
docker compose exec host python -m host.cli roletest <worker>
```

   Read the printed number. Under 10 means pass (exit 0); over 10, or no ack, is exit 1. The worker is left in `train`. From a shell with the repo checked out, `tests/hw/roletest.sh <worker>` does the same.
4. Wrong user. On a tailnet device signed in as a different Tailscale user (a second account, or a shared-in device), open the dashboard URL. You should see a 401 page and no fleet data. The same goes for any worker machine: its IP is refused on owner routes.
5. Kill switch. Press KILL and confirm. The whole top bar turns red with "TRADING KILLED. Reset in Settings." and the button becomes a disabled KILLED chip. Then open Settings, find the kill form, type `RESUME` exactly and submit; the bar returns to normal. Anything other than `RESUME` is rejected and the flag stays set. The kill switch only affects trading (the trade role); batch jobs such as backtests and training keep running. From step 4, KILL also cancels all open orders.
6. Limits and audit. In Settings change a limit, for example max bet from 25 to 20 dollars, and save. The Audit log at the bottom of the Settings page gets a new row with the time, your login, the action and the entity. The kill presses and the reset from step 5 are in the same log.
7. Enroll token. In Settings press "New enroll token". The page shows the token once with the two install one-liners and a Copy button.

## How to test step 3

Needs at least one idle worker, installed as in step 1. Commands run on the host.

1. Ingest games. Either run the command, or press the "Refresh now" button in the nflverse games section of Settings:

```powershell
docker compose exec host python -m host.cli ingest-games
```

   It downloads the nflverse schedule file (games from 1999 on, with closing moneylines and spreads) and stores it in the host database (the `games` table). Workers fetch it from the host (`GET /api/v1/data/games`, cached on the worker with an ETag), never from the internet.
2. Send a model search. On the Jobs page create a `model_search` job with family `elo_blend`, n 50, seed 1, seasons 2012 to the last completed season, target any idle worker. Go to the Fleet page: the chosen card flips to `model_search` and its progress bar climbs, one step per candidate and test season.
3. When it finishes, open Models. Five new candidates appear, each with a three-sentence summary, ROI, bets, and log-loss against the market. The worker card returns to `idle` on its own.
4. Click one candidate and read the per-season table (bets, ROI, log-loss, drawdown for each test season).
5. Press Train and accept the default (through the last complete season, week 22). A child row appears in the same lineage, with the parent's backtest metrics and a trained-through label.
6. Send a backtest for one model to a chosen box from the Jobs page (target: that worker). When it ends, open the job detail page and read the metrics in the result.
7. Resume elsewhere. Start another model search, and while it runs move its worker to another role (Fleet page dropdown, or `python -m host.cli role <worker> train`). The job goes back to `queued` with its checkpoint, an idle or model_search-role worker picks it up, and it repeats at most one candidate-season of work.

**Read the numbers honestly.** Backtests here use sportsbook closing lines from nflverse, which are sharp and already efficient. An ROI near zero or slightly negative is normal and expected, and a model that beats it by a wide margin is more likely overfit than good. These backtests measure calibration and discipline (does the model add anything to the market price, and does it bet sensibly), not true edge. The real test is paper trading on live Polymarket prices in step 4, where closing line value is measured. Only lineages whose backtest clears the thresholds in Settings become `paper_ok`, and nothing trades real money before step 5.

## How to test step 4

Needs the compose stack up (`db`, `host`, `exchange`), games ingested (step 3), at least one model and one worker. Everything below is paper trading on simulated markets, so no network and no keys are needed. Commands run on the host.

1. Market source. Open Settings, Trading group, and check "market source" is `sim` (the default). The simulated source makes two markets per upcoming game (home and away YES) with a seeded random-walk price around the devigged nflverse moneyline and a 5-level book.
2. Ingest games if you have not (`docker compose exec host python -m host.cli ingest-games`). Within a minute the exchange creates markets and `/trading` shows snapshot ages per market that stay under a few seconds for assigned games. Check `docker compose ps` shows `exchange` running.
3. Create a paper assignment for an upcoming game: press Assign on a model on the Models page (opens `/trading` prefilled), or use the create form on `/trading`. Mode is paper, bankroll defaults to `default_bankroll_cents`. Repeat with two more models on the same game to see three paper models at once (the limit is `max_paper_models_per_game`).
4. Put a worker in the trade role (Fleet page dropdown, confirm the prompt). It claims the trade jobs, up to `trade_max_games`.
5. Watch `/trading`. Within about 5 to 10 seconds you should see orders proposed with a one-line rationale ("my 0.57 vs ask 0.52, fee 0.012, edge 0.038"), approved or rejected with a reason code, and fills arriving from the simulated book. Open orders have a Cancel button. The assignment row shows bankroll available, reserved, open and realized.
6. Daily loss limit. In Settings set the paper max daily loss to $1 and save. New proposals whose cost, added to today's losses and the cash still reserved by open paper orders, would pass $1 are rejected with `daily_loss`; existing paper assignments stay active. Put the limit back afterwards.
7. Kill switch. Press KILL and confirm. Every open paper order should be cancelled within 2 seconds (watch `/trading`), the reserved cash returns to the bankroll, and every assignment shows halted. Trade workers stop proposing.
8. Reset. In Settings type `RESUME` in the kill form. The flag clears but assignments stay halted; on `/trading` press "Activate all paper" and trading resumes.
9. Role switch. Change the trade worker's role to anything else. Its open orders are cancelled first (the host's release call), then the role acknowledges, and the trade jobs go back to the queue for another trade worker.
10. Simulate a final (only allowed for the `sim` source, or with `FLEET_DEV`):

```powershell
docker compose exec exchange python -m host.exchange.cli simulate-final <game_id> --home 24 --away 20
```

    The game id is on `/trading` (assignments table). Expect: open orders cancelled, positions settled, one `bets` row per filled order with entry price, closing price and CLV (`closing_price - entry_price`), `model_scores` filled in, the assignment `settled`, today's P&L updated on the Fleet card of the worker that traded and in the top bar, and a paper line (games, bets, P&L, ROI, CLV) for the model's lineage on the Models page. Once a lineage has enough paper games, bets, days and positive CLV and P&L (thresholds in Settings), it becomes `live_eligible`; nothing trades real money before step 5.
11. Real market data. On the host (which has internet), set market source to `polymarket_us` or `polymarket_clob` in Settings. Press "Probe markets" on `/trading` (or run `docker compose exec exchange python -m host.exchange.cli probe`). If NFL markets do not appear under "unmatched markets" or in the assignment form, paste the probe output back so the field names can be fixed. Markets whose teams or date do not match exactly are never traded until you link them to a game by hand on `/trading`.

**An honest note.** The Polymarket US and CLOB readers were written without access to the exchange docs (they were unreachable from the build sandbox), so endpoint paths and field names are best guesses kept in `market_source_config` and may need fixes from your probe output. The private order endpoints (place, cancel, fills, balance, signing) are step 5 (below); paper orders are filled by a simulator against the real or simulated books. Paper fills are optimistic, so expect live results below paper.

## Live trading (step 5)

Real money sits behind three gates, all enforced on the host: the typed live switch, a lineage that is `live_eligible`, and the exchange process's authenticated session. Workers never see keys and never talk to the exchange. The full spec is `docs/LIVE.md`. Live is off after install and stays off until you turn it on.

### Get API keys

1. Open the Polymarket US app and complete identity verification (KYC). API keys are created in the app's account or developer settings once the account is verified.
2. Read the API docs at https://docs.polymarket.us for the key format and signing rule. You need an API key, an API secret (the Ed25519 private key seed, base64 or hex) and, if the app issues one, a passphrase.

### Create exchange.env on the host

In the repository folder, create `exchange.env` with these lines (the passphrase line is optional):

```
POLYMARKET_US_API_KEY=<your key>
POLYMARKET_US_API_SECRET=<your secret>
POLYMARKET_US_PASSPHRASE=<only if you were given one>
```

Never put the keys in `.env`: the `host` service loads `.env`, and only the `exchange` service loads `exchange.env`. Make the file readable only by the user who runs `docker compose` (compose reads env files on the client side, so a root-only file breaks a docker-group user's `docker compose up`). Windows (PowerShell):

```powershell
icacls exchange.env /inheritance:r /grant:r "$($env:USERNAME):(R,W)"
```

Debian, as the user who runs compose:

```bash
chmod 600 exchange.env
```

The file is git-ignored (`exchange.env` and every `*.env` but `.env.example`), so `git add .` cannot pick it up. If the exchange should talk to a sandbox instead of the real API, add `POLYMARKET_US_LIVE_BASE_URL=https://<sandbox host>` to the same file: the Settings value `polymarket_us.live.base_url` is pinned to https on `polymarket.us` and cannot send signed requests anywhere else.

Restart only the exchange service so it loads the file (do not restart `host` or `db`):

```powershell
docker compose up -d --force-recreate exchange
```

The keys are never logged, stored in the database or returned by any page or command.

### Check the credentials

Open Settings, "Live trading" group. It should show credentials present: yes, and auth OK within the last few minutes (the exchange re-checks every 5 minutes). Or run the checks directly:

```powershell
docker compose exec exchange python -m host.exchange.cli auth-check
docker compose exec exchange python -m host.exchange.cli probe-account
```

If auth fails, paste the probe output back. The API was written without access to the docs, so the signing rule, header names and paths are assumptions kept in `market_source_config` (`polymarket_us.auth` and `.live`) and may need fixing from the raw payload. The probe shows the key's last 4 characters only, never the secret. If Settings says credentials "no" with a last auth error "credentials malformed: ...", the secret in `exchange.env` is not a usable Ed25519 seed (the message names the format problem, never the value); `probe-account` and the other live commands print the same as "secret malformed".

### How to test step 5

Needs the compose stack up, credentials present and auth OK as above. Start with a clean state: kill off, nothing open on `/trading`, and in Settings set market source to `polymarket_us` and confirm the NFL markets on `/trading` first: live orders and the smoke order only go to markets of the current source (`polymarket_clob` is a price source only, and markets left over from `sim` or the CLOB are never traded live).

1. Enable live. In Settings, "Live trading", type the phrase exactly, with the current date in your time zone. For example, on 2026-10-04 it is:

```
ENABLE LIVE TRADING 2026-10-04
```

   The date is the one in the Settings time zone (the form shows the exact phrase). Yesterday's or tomorrow's date, other wording or extra whitespace is refused. It is also refused while the kill is on, without credentials, or if auth was not checked within the last 10 minutes. On success the top bar pill turns green and says LIVE, and the audit log gets a `live_on` row. The form shows the exact phrase to type.
2. Smoke order. This places one real 1-share order far below the best bid so it rests and never fills, then cancels it. It proves keys, signing, placement and cancellation without giving any model money. Use today's date:

```powershell
docker compose exec exchange python -m host.exchange.cli exchange-smoke --confirm "SMOKE 2026-10-04"
```

   Add `--market <id>` to pick a market; otherwise the most liquid confirmed market of the current source with a fresh price is used. The command prints a timeline (approved, submitting, open with the exchange order id, cancel requested, cancelled). While it is open (10 seconds by default, `--hold 120` for two minutes) it appears on `/trading` flagged `smoke`, and you should see the same order in the Polymarket US app's open orders. After the hold it is cancelled and disappears from the exchange. No bankroll is touched. A non-zero exit means the order never reached `open` in time (the exchange service is stopped and `--drive` was not given, or placement failed): the command cancels the row before exiting, and you should check `/trading` for the smoke row and the Polymarket US app for a resting order.
3. KILL. Place another smoke order with a longer hold, for example `exchange-smoke --confirm "SMOKE 2026-10-04" --hold 120`, press KILL and confirm. The order is cancelled and live turns off (the pill returns to PAPER). Then type `RESUME` in Settings: the kill clears, but live stays off and assignments stay halted. Type the dated phrase again to re-enable live.
4. Disable live without the kill: press "Disable live" in Settings. It is immediate: live assignments are halted and live orders are cancelled through the exchange.
5. Cancel everything directly. If the exchange service is down with live orders resting: press KILL first (it works with the exchange down: approved rows are cancelled at once, live rows become cancel-requested, live turns off), then stop the service and cancel straight on the exchange:

```powershell
docker compose stop exchange
docker compose run --rm exchange python -m host.exchange.cli cancel-all --direct
```

   It loads the credentials, turns live off itself (live assignments halted, approved live rows cancelled so a restart cannot submit them), lists open orders on the exchange, cancels each with retry, reads the fills and marks the rows the exchange no longer lists cancelled (releasing reserved cash) and prints what it did. A row whose placement may still be in flight is left cancel-requested for the restarted exchange to confirm, and if the fills call fails nothing is closed and the command exits 1 (run it again). Restart the exchange afterwards with `docker compose up -d exchange`; the `/trading` exchange box shows this command whenever the exchange is DOWN with live orders active. Every live order also expires on its own at the exchange (GTD, 15 minutes by default) if the host is dead.
6. Restart mid-order. Restart the exchange service while a smoke order is open: it runs auth, reconciliation and the open-order audit before submitting anything, and you should see no duplicate order.

### Auto-kill reasons

The system kills trading by itself (kill on, live off, every live order cancel-requested and cancelled on the exchange by the exchange process) and shows the reason in the top bar until you RESUME; the Settings page repeats the recovery line next to the reason:

- `auth_failures`: three authentication probes in a row failed (bad or revoked keys, wrong signing rule, exchange down). Recovery: fix `exchange.env` or the auth config, `docker compose up -d --force-recreate exchange`, run `probe-account` or `auth-check` until auth is ok (the count stays over the limit until a probe succeeds, so a RESUME before that kills again at once), then RESUME and re-enable live.
- `clock_skew`: the host clock and the exchange's `Date` header differ by more than 30 seconds (measured by the auth probe and on every other live answer). New live orders are paused while cancels, the open-order audit and the fills poll keep running, so the kill's cancels still reach the exchange; if the exchange refuses a cancel signed with the bad clock, those orders rest until the skew clears or their GTD (15 minutes by default), and the top bar P&L keeps showing live money while any live order or position remains. Recovery: fix the host clock (Docker Desktop on Windows drifts after sleep: `wsl --shutdown` or restart Docker Desktop), then either wait for the next live answer (the loop resumes on the first in-range one, at the latest the next auth probe, `auth_probe_interval_s`, 5 minutes) or `docker compose restart exchange` so the startup probe clears the pause at once; confirm the skew in Settings, RESUME, re-enable. To pull resting orders right now, `cancel-all --direct` as in step 5 above.
- `unknown_order`: the exchange shows an open order that this system did not create. Someone else is using the account or the keys, or the database lost an order. The stray order is cancelled. This system needs exclusive use of the account: do not place orders by hand in the app. Check the app's open orders before you RESUME and re-enable.
- `unknown_fill`: a fill arrived for a client order id this system does not know. Same causes and same advice as `unknown_order`; reconcile the position by hand in the app.
- `ambiguous_reconciliation`: after a timeout, two remote orders matched one client order id, so the system cannot tell which is ours. Cancel the duplicate in the app, then RESUME and re-enable.
- `late_fill`: a fill arrived for an order this system had already closed (or exceeds what was open), so the ledger could not book it. Real money moved that the books do not show: check the app, book the position by hand (a ledger adjust), then RESUME and re-enable.

Reset is the normal `RESUME`; live then stays off until you re-enable it with the typed phrase.

### When model orders go live

Model-driven live orders only begin when a lineage is `live_eligible`. That requires the backtest and paper thresholds in Settings (backtest bets, ROI and drawdown, then paper games, bets, days, positive CLV and positive P&L) and there is no override and no typed bypass. Even then, an order is placed only if you created a live assignment for that model and game (which needs live on, the lineage `live_eligible`, auth OK and a confirmed market of the current source), and every order still passes the host's limits: max bet, per-game bankroll, daily loss, liquidity floor, and buying power fresher than 5 minutes (minus what live fills spent since the probe). One live model per game. A breach of the live daily-loss limit turns live off while paper keeps running; so does retiring the model or any demotion of its lineage out of `live_eligible` (the assignment is halted and its orders cancelled). Start with small limits.

### Risks

- The Polymarket US API is unverified. Paths, headers, the signing rule and response fields are best guesses in `market_source_config` and may need fixes from your probe output. Expect to iterate on the smoke order before trusting anything else.
- Single host. If the host machine or Docker is down, nothing can cancel or kill; resting live orders depend on GTD expiry. Keep Docker Desktop starting at login and back up the Postgres volume.
- Paper fills are optimistic, so live results will be worse than paper.
- A Windows host that sleeps, updates or reboots is an outage. Turn sleep off (Settings, System, Power) and set active hours so updates do not restart it mid-game.
- Account terms (server-side API trading, geofencing, device binding) are unconfirmed. Check the Polymarket US terms before relying on this.

## Data

Game schedules, scores and closing lines come from [nflverse](https://github.com/nflverse/nflverse-data) (`games.csv`), licensed CC BY 4.0. Attribution: "Data: nflverse (https://nflverse.com), CC BY 4.0." It is also shown on the Models page.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[host,dev]'
docker compose up -d db
export FLEET_TEST_DATABASE_URL=postgresql://fleet:fleet@127.0.0.1:5432/fleet
.venv/bin/pytest tests/
```

Any local Postgres 16 works instead of the compose `db` service; point `FLEET_TEST_DATABASE_URL` at a superuser connection so the tests can create and drop databases. The `fleet/` package must stay stdlib-only (CI enforces it).

## Build order

1. [x] Step 1: fleet core (queue, leases, worker agent, installer, owner API, host in Docker)
2. [x] Step 2: dashboard fleet cards, role handshake, drain and watchdog, settings page, kill flag
3. [x] Step 3: nflverse data, models, backtest / search / train jobs, leaderboard
4. [x] Step 4: fleet-exchange, paper trading, approval limits, ledger, scoring
5. [x] Step 5: Polymarket US live adapter, live switch, smoke order

Data is free-only for now; paid sources are considered once profit comes in.
