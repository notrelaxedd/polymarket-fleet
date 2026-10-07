# polymarket-fleet

A small fleet of machines that builds, tests and (much later) runs NFL prediction-market models. One host runs a Postgres-backed job queue, a dashboard and the trading limits; any number of Debian worker boxes run a tiny stdlib-only agent that takes jobs according to the role you give it. Everything is private to your Tailscale network, paper trading is the default, and the host enforces every limit.

## Architecture

The host is a Windows 11 machine running a Docker Compose stack: `db` (postgres:16), `host` (the FastAPI app that serves the worker API, the owner API and the dashboard) and `exchange` (`fleet-exchange`, the second process: market snapshots, the order executor, the paper fill simulator, the in-game state feed from ESPN (step 6C), settlement and scoring; it is the only process that ever talks to a market or exchange). `db` and `host` publish ports on `127.0.0.1` only; `exchange` publishes none. `tailscale serve` on the host OS puts HTTPS in front of port 8080 for your tailnet and injects a `Tailscale-User-Login` header, which the app compares with `FLEET_OWNER_LOGIN` to identify you. Workers are Debian 13 boxes: each runs the `fleet-worker` systemd service, which registers with an enroll token, sends a heartbeat every 5 seconds, claims leased jobs for its current role, checkpoints as it goes, and updates its own code from the host. The wire contract is in `docs/PROTOCOL.md`; the full design is in `docs/DESIGN.md`.

The dashboard is server-rendered HTML (Jinja2, one stylesheet, a little vanilla JS) served by the same `host` container. The one exception is the 3D fleet control page at `/fleet`, a React + three.js page built from `fleet-ui/`; the Docker image builds it (a Node stage in the `Dockerfile`), so you still run only `docker compose`, never npm. The fleet cards are at `/fleet/list`. Owner pages and routes authenticate through the `Tailscale-User-Login` header; a request with a different login gets a 401 page. A worker machine's IP is refused on owner routes even if it sends the header, so a compromised worker cannot act as you. The spec is in `docs/DASHBOARD.md`.

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

Re-running the installer upgrades the worker and keeps its identity. Workers installed before the 3D fleet page must re-run the install line once to get the reboot and disk-wear units; self-update only replaces the code. To keep the token out of the sudo log and the process list, pass it through the environment instead: `curl -fsSL https://<host>/install.sh | sudo FLEET_ENROLL_TOKEN=<token> bash -s -- https://<host>` (or `--token-file PATH`).

## Fleet 3D page

`/fleet` is the 3D control page: every worker in one scene (several views), a machine list, an inspector with the role buttons and Reboot, a command bar (`reboot box3 box4`, `stop train`, `backtest on idle`, `search box1 box2`; moving a box into or out of trading asks first) and an event feed. It polls the host every 3 s and shows "Live" or "Disconnected". The phone-friendly cards are at `/fleet/list` (the 3D page links there, and they link back). The spec is in `docs/DASHBOARD.md`.

- Rebuild after pulling changes to `fleet-ui/`: `docker compose build host` then `docker compose up -d`. If `/fleet` says the 3D page has not been built, the image was built without it; rebuild it the same way.
- Local development: start the host on `127.0.0.1:8080` with `FLEET_DEV=1` (see Development below), then `cd fleet-ui && npm ci && npm run dev`. Vite serves the page with live reload and passes `/api` to the host. Role and reboot buttons post from Vite's own origin, so start the host with that origin allowed too, for example `FLEET_ALLOWED_ORIGINS=http://127.0.0.1:8080,http://localhost:5173`. `npm run build` writes `fleet-ui/dist`, which a host started from the checkout serves at `/fleet`.
- Workers: each existing worker needs the one-line install command run once more to get the reboot and wear units (`curl -fsSL https://<host>/install.sh | sudo bash -s -- https://<host>`; an installed box keeps its identity and needs no new token). Self-update only replaces the code, so until then the page says it cannot reboot that box. Heartbeats now come every 3 s and a box counts as offline after 30 s without one (Settings > Fleet).

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
3. When it finishes, open Models. Five new candidates appear, each with its ROI and bets (from step 7 the three-sentence summary and the log-loss against the market are on the model page). The worker card returns to `idle` on its own.
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

## Reading a model

**Search era and validation era.** A model search tries many parameter settings and keeps the best by their results on the search era (the Settings key `backtest_seasons`, for example 2010 to 2021). The kept models are then run once on the validation era (`validation_seasons`, for example 2022 to the last complete season), seasons the search never looked at. The split matters because the best of many tries looks good partly by luck; the search-era number is the winner's curse, while the validation number is an honest second test. Selection never sees validation results. Trust the validation columns, and treat the search columns as context.

**The 90% range on ROI.** ROI comes from a limited number of bets, so it is noisy. The range is the 5th to 95th percentile of ROI over 1000 resamples of the bets (seeded, so it is the same every time). If the range includes zero, the data cannot tell the model from a coin flip with fees, and a positive average ROI is not evidence of anything. Only a range whose low end is above zero counts, and even then only for the era it was measured on.

**Beats market p.** For every game the model's log-loss is compared with the market's. Beats market p is the chance of seeing a gain at least this large if the model had no real advantage over the market (a sign-flip permutation test, 10 000 flips). Below 0.05 is the bar for "beats the market"; the smallest value the test can give prints as "p < 0.001". A model near 0.5 matches the market; that is the usual outcome.

**Flags.**
- `overfit`: the search-era ROI is clearly better than the validation ROI (by more than 3 points of shrunk ROI), or the search era beat the market and the validation era did not. The search found noise.
- `fragile`: the result falls apart under small changes: a wider spread (+0.02 half-spread) removes half the bets or turns a profit into a loss, or nudging the parameters by up to 10% makes the typical shrunk ROI negative.
- `regime_dependent`: in one pair of slices (favourite or underdog, home or away, divisional or not, primetime or day, cold or windy or not) one side makes the profit and the other side, holding at least a fifth of the bets, gives back more than half of it. A small loss on a small slice does not count.

A flagged model should be retired, or the search rerun with another seed or a wider space. Do not tune it until the flag goes away on the same validation data; that spends the validation era and brings the overfit back.

**Price stress and neighbourhood.** The price stress table reruns the validation era with a worse market (half-spread +0.01 and +0.02, taker fee x1.5) and shows how many bets and how much ROI survive; a real edge shrinks gently, a fake one vanishes. The neighbourhood numbers rerun ten slightly perturbed copies of the parameters and report the median and the 10th percentile of shrunk ROI and log-loss gain. A trustworthy model has a median near its base result and a 10th percentile that is not much worse. The regime table shows where the profit comes from.

**How the gates use this.** A lineage becomes `paper_ok` only with validation numbers (`require_validation`): enough validation bets, an ROI range whose low end clears `min_roi_ci_low`, a market p at or below `max_market_p`, and none of the forbidden flags (`overfit`, `fragile` by default). It becomes `live_eligible` only after that and with paper trading whose closing line value (CLV) range excludes zero (5th percentile above 0 over the minimum number of bets), plus the paper thresholds from step 4. Eligibility is recomputed after every validation and settlement, and for every lineage when the host starts. A lineage that fails the backtest gate returns to `candidate` (from `paper_ok` or `live_eligible`, live assignments halted); one that fails only the paper gate returns from `live_eligible` to `paper_ok`. The leaderboard ranks validated lineages with enough paper trading on paper CLV first, then (step 6B) those with 30 or more bets replayed on recorded prices on snapshot CLV, then the rest by validation numbers; models never validated are listed unranked as "not validated", whatever their paper or snapshot record. Snapshot replays do not feed the gates. Full spec: `docs/ROBUSTNESS.md`.

**What a closing-line backtest can and cannot say.** Backtests here bet against sportsbook closing lines, the sharpest and most efficient prices there are, so they measure calibration and discipline rather than true edge. A model that really beats the market on them is rare, and a significant result deserves suspicion before celebration. A model that merely matches the market (market p well above 0.05, ROI range spanning zero, no flags) can still be paper traded, where Polymarket prices may differ from the closing line and where CLV measures what a backtest cannot. Be clear about what that buys: with the default gate (`max_market_p` 0.10, `min_roi_ci_low` 0) such a model stays a `candidate`, and only a `paper_ok` lineage can be promoted to `live_eligible`, so however good its paper CLV it cannot reach real money unless you loosen those two thresholds in Settings.

**What a snapshot replay adds (step 6B).** A snapshot backtest replays the same walk-forward test on the prices the host recorded from its market source (Polymarket in real use): it bets at the recorded ask a set time before kickoff, fills against the recorded book, and measures CLV against the frozen closing price, so it is the first backtest that can show real edge. It only covers games the exchange recorded, so it starts small and grows a week at a time; read its CLV range the same way as the ROI range above.

## How to test step 6A

Needs the stack and at least one idle worker as in step 3, with games ingested.

1. Open Settings. It shows the search seasons (`backtest_seasons`), the validation seasons (`validation_seasons`, the end may be empty for the last complete season) and `search_workers` (`auto` or a number). The two season ranges must not overlap and validation must come after search (with an empty search end, after the first search season). A job whose seasons do not work out, for example a validation era that starts after the last complete season, is refused with a message naming the setting rather than run on other seasons.
2. Start a model search from the Jobs page. When it finishes, open Models: the new candidates now have validation columns (ROI with its range, market p, flags) next to the search-era ones.
3. Older models (from steps 3 to 5) show "not validated" and are unranked. Most were searched on the old default seasons, 2010 to the last complete season, which includes the validation era, so pressing Validate on them is refused with a message saying their validation would be in-sample: run a new model search instead and validate its models. A model whose search ended before the validation era can be validated: a `validate` job runs and when it finishes the row fills in. On the first start after the upgrade every lineage is re-judged, so older `paper_ok` or `live_eligible` lineages without validation numbers return to `candidate`.
4. Open the model page and read the Robustness section: the ROI range, the market test, the stress table, the neighbourhood summary and the regime table. Flags appear as chips on the leaderboard.
5. Watch a search on a 4-core box: with `search_workers` at `auto` it uses three processes and finishes about three times faster than with `search_workers` set to 1, with identical results.

## How to test step 6B

Needs the stack and at least one idle worker as in step 3, with games ingested, and the exchange running as in step 4. Step 6B adds four things: quarterback and injury signals plus team strength from play-by-play, a second model family (`epa_blend`), backtests replayed on the prices the host recorded ("snapshot replay"), and selling a held position. Full specs: `docs/ROBUSTNESS.md` Part B and the "Selling" section of `docs/TRADING.md`.

1. Load the signals. Injury reports and play-by-play come from nflverse (free). Load one season of each by hand:

```powershell
docker compose exec host python -m host.cli ingest-injuries --season 2025
docker compose exec host python -m host.cli ingest-pbp --season 2025
```

   Each prints one line per season, for example `pbp:2025: 570 rows, 570 inserted, 0 changed, 0 skipped`. `--season all` backfills every season (injuries from 2009, play-by-play from 1999, one season at a time; a play-by-play season is a download of tens of megabytes, so the full backfill takes a while). `--file <path>` loads a local CSV (CSV.gz for play-by-play) instead. A season that fails is reported and the command exits 1, but the other seasons are still loaded. You do not have to repeat this: the host refreshes the current and the previous season by itself every `signals_refresh_hours` (24 by default), the first time 5 minutes after start when either table is empty. Settings has a "nflverse signals" group with the refresh hours and the two download URL templates (each must contain `{season}`). The outcome of the automatic refresh is only in the host log (`signals refresh: N loaded, M failed`), not on the Settings page yet.
2. Search the new family. On the Jobs page open Model search, pick family `epa_blend` and send it (n 50 is enough to see it work). It is a logistic regression on Elo, recent offensive and defensive EPA per play, rest, quarterback change, players listed Out, divisional games and the market price (`docs/MODELS.md`, "epa_blend"). Without play-by-play loaded the EPA features are flat and the model leans on the rest. When it finishes, Models shows the new candidates with their validation columns like any search, short params such as "window 8 · shrink 3.0 · L2 1.00", and a summary whose first sentence names the signals that carried the result, or says that none moved the price. `elo_blend` searches now also draw two penalties (Elo points for a quarterback change, and per player listed Out); its summary mentions them when they are not zero.
3. Allow sim prices for the test. A snapshot replay needs recorded prices. On a fresh install the market source is `sim`, so the only recorded prices are simulated ones, and a worker refuses those unless Settings allows them. Open Settings, "Snapshot replay", tick "Allow sim prices" and save. The same group holds the decision time (`decision_minutes_before_kickoff`, 60 by default): the replay bets that long before kickoff, on prices recorded within the 30 minutes before that.
4. Get some recorded prices. A game is scored only when it is final, has a confirmed market on the market source, and the exchange recorded a price for it within the 30 minutes before its decision time (so between 90 and 60 minutes before kickoff with the defaults). On a fresh install nothing qualifies yet: keep the stack running through a game day, and after the games are final (the games refresh, every `nflverse_refresh_hours`, 6 by default, picks up the scores once nflverse publishes them; on the sim source `simulate-final` after kickoff also works, as in step 4) there are games to replay. Until then a replay finishes with 0 games scored, which is the correct answer.
5. Run a snapshot backtest. On the Jobs page open Backtest, choose a model, set First season to the current season and clear Last season (it is prefilled from Settings; for a snapshot backtest blank means through the latest season, the season in progress included, as the hint under the label says), and set Price source to "snapshots (recorded prices, real CLV)". The line under the form names what it will replay (market source, minutes before kickoff, participation); a red line warns when the market source is sim and sim prices are not allowed. Send it. The job list marks it with a "snapshots" chip; its page shows "price source snapshots: recorded sim prices 60 minutes before kickoff, participation 0.5, sim prices allowed (testing)" and, when done, a replay line: games scored, bets, ROI, CLV per contract with its 90% range, and how many games were skipped for lack of recorded prices.
6. Read it on Models. A lineage with a replay shows a snapshot column ("60 games · 30 bets · ROI +3.1% · CLV 0.020" with the CLV range beside it). CLV here is the frozen closing price minus the price paid, so above zero means the market moved the model's way after it bought. Once a lineage has 30 or more replayed bets it ranks on shrunk snapshot CLV with a "snapshot" chip, after the paper-ranked lineages and ahead of the ones ranked on validation, validated or not. A replay never changes a model's status: the gates still read only the validation and paper numbers. The model page has a "Snapshot replay" section with the same line, the metrics and a per-season table, and a one-tap "Replay on snapshots" button. That button sends no seasons: it replays from the first search season in Settings through the latest season, the season in progress included, and seasons without recorded prices are simply skipped. A replay that scores no game is logged on its job but never replaces an earlier replay's numbers. A replay on sim prices is shown with its "sim" platform but never ranks: only real recorded prices set the snapshot rank.
7. Turn "Allow sim prices" back off. Simulated prices are made up, so a replay on them proves nothing; it is only a way to check the plumbing.
8. Selling, in paper. Assign a model to an upcoming game and put a worker in the trade role as in step 4. Once it holds contracts, every tick it compares the bid with its own probability: when `bid - fee - p_side` is at least `min_edge` it offers a limit sell at the bid (never more than it holds, at most one open sell per market). On `/trading`:
   - Sell orders and sell fills wear a `sell` chip. A sell order reads "sell N @ p" with its realized P&L instead of a cost; a sell fill reads "sold N @ p" with the realized P&L and the basis it removed.
   - Positions (between Assignments and the order lists) has one table per assignment that holds contracts: market and side, size, average cost with the basis, the current bid, and the unrealized P&L at that bid net of the taker fee a sale would pay, with a total per assignment. With nothing held it says "No open positions."
   - Rejections of sells have their own reasons: "nothing held to sell", "sell larger than the position", "a sell is already open on this market".
   - Sells only fire when the market overshoots the model, which on the sim source can take a while. Lowering Min edge in Settings (Trading limits) makes both buys and sells fire more readily; put it back afterwards.
   - KILL cancels open sells exactly like buys and leaves positions alone. At settlement each filled sell gets its own bets row (result `sold`), and the buy rows only cover the contracts still held.

## How to test step 6C

Needs the stack and at least one idle worker as in step 3, with games ingested, the exchange running and a worker in the trade role as in step 4. Step 6C adds in-game trading on paper: a live game-state feed from ESPN, a measurement of how far that feed lags the market, an in-game win-probability model (`ingame_wp`) trained on nflverse play-by-play, and trade rules that run once a game has kicked off. Full spec: `docs/INGAME.md`.

Why in-game is paper-only. ESPN's endpoints are unofficial and can lag the field by 15 to 60 s, while the market moves on the same touchdown at once, so an in-game buyer is adversely selected: it tends to buy after the price already moved. There are also no historical in-game prices, so an in-game model can only be validated for calibration against nflverse's `vegas_wp`, never backtested for edge; paper trading is the real test. So in this step the host refuses in-game trading on a live assignment (409, "in-game trading is paper-only in this step"), rejects any live in-game order with `ingame_paper_only`, and never makes an `ingame_wp` lineage `live_eligible`. A live in-game gate is a later decision.

1. Load the in-game training rows (free nflverse play-by-play, one row per play):

```powershell
docker compose exec host python -m host.cli ingest-pbp-rows --season 2012-2025
```

   It prints one line per season, for example `season 2023: N plays in G games (W with a closing moneyline) from <url>: I inserted, C changed`. Each season is a download of about 20 MB, streamed, so the range takes a while. The closing moneyline comes from `games`, so ingest the games first. A season that fails stops the command with `error: ...` and exit 1; the seasons before it are kept, so run it again from that season. Running it twice changes nothing. If you ran `ingest-pbp-rows` on any season before installing this build, run it again for those seasons (kickoffs are now stored the way the live feed shows them, as the kicking team at its own 35) and search `ingame_wp` again afterwards. You do not have to repeat it: while the season is on, the host ingests the current season by itself once a week.
2. Search the in-game family. On the Jobs page open Model search, pick family `ingame_wp`, set Candidates to 20 (the form starts at the pre-game 200; blank also means 20) and leave the four "ingame_wp only" fields blank (train 2012 to 2021, validation 2022 on); the pre-game season fields are not used for this family. Send it. The worker downloads the play-by-play rows from the host once (`pbp.jsonl.gz`, kept with its ETag). When it finishes, Models has a folded "In-game models" group: one row per lineage with its plays and seasons, the validation log-loss against vegas_wp's, a "beats vegas_wp" chip, the gain per play at the side, and the in-game paper record once it has one (the summary is on its model page). A lineage is `paper_ok` when its validation log-loss is at or below vegas_wp's over at least 10 000 plays, otherwise `candidate`. Its model page shows the same validation by period, by score and as a calibration table. Backtest, Validate and Train do not take ingame_wp models.
3. Turn in-game trading on for a paper assignment. On an `ingame_wp` lineage (preferably a `paper_ok` one; a `candidate` that loses to vegas_wp is offered too) press "Assign in-game" (the row's "..." menu, or the button on its page): Trading opens the New assignment form with that model in the "In-game model" select. Pick an upcoming game and its pre-game model as before, mode paper, tick "Trade in-game (paper only)" and create it. For an existing paper assignment use the in-game switch in its row's "..." menu instead (in-game model select, "Trade in-game" box, "Save in-game"); the flash reads "assignment 1a2b3c4d: in-game trading on, model 5e6f7a8b". Turning it off cancels the assignment's open in-game orders, and the in-game model cannot be swapped once the assignment has open or filled in-game orders (turn trading off instead). A live assignment's menu says in-game orders are paper only and has no switch. Settings has a folded "In-game" group with every rule (tick, max game-state age 30 s, quiet 20 s after a score or possession change, cutoff 120 s before the end, dead zone 0.03, min edge 0.05, max bet $5, order lifetime 60 s), the feed-lag limits (20 s over at least 5 measured events) and the feed settings (poll each game every 4 s, at most 1 ESPN request per second, the ESPN summary URL, Yahoo off).
4. Watch a game. Before kickoff the assignment trades with the pre-game rules exactly as in step 4. Once the game kicks off, its row on `/trading` gets an in-game line: "in-game on", the live score and clock with the state age ("Q3 4:12 · 17-14 · 3 s ago", away score first as in the "KC @ LV" heading; also "Half", "End Q1", "OT 1:01", "Final"), and the in-game model's home probability next to the home market mid ("model LV 62% · mid 58%"). When the feed stops answering, the line turns into an amber "state stale" after 30 s and in-game trading pauses; the probability stays muted and ends "(from the stale state, not traded)". In-game orders and their fills wear an `in-game` chip; in-game rejections read in words ("game state too old", "too soon after a score or possession change", "inside the end-of-game cutoff", "feed lag suspends in-game buys"). Pre-game orders are cancelled at kickoff whatever "Pregame only" says. With "Pregame only" on (the default) an order request after kickoff without the in-game tag is refused; with it unticked such a request runs the in-game rules (paper only). Each in-game order lives 60 s.
5. Open the "In-game feed" group right under the assignments (its header already shows the chip and the lag per source). For every score change and possession change the host records when ESPN first showed it and when the Polymarket mid of that game moved by more than 0.03, and shows the median over the last 20 measured events per source: "ESPN: median 6 s behind the market over 12 events" ("ahead of" when the feed was first), or "not enough data (3 of 5 events measured)". The chip says "not suspended", or a red "buys suspended" once the median is over 20 s with at least 5 measured events; then in-game buying stops and sells stay allowed. On the sim market source the mid is a seeded random walk that knows nothing of the game, so the lag means something only on real prices.
6. Probe ESPN during a live game and paste the output back. The parsers were written from ESPN's documented payloads, not from a live game, so run this while a game you have assigned is in play:

```powershell
docker compose run --rm exchange python -m host.exchange.cli probe-gamestate --event <espn id>
```

   The ESPN event id is the nflverse `espn` field of the game; the "Probe game state" field in Trading's Exchange group offers the ids of your assigned unfinished games, and that button shows the same result in the browser. The command prints the source, event id, the game it maps to, the URL and HTTP status, the first 64 KiB of the payload and what the parser extracted (one state per play plus the current situation, last). Paste the whole output back; it holds no keys. If you know Yahoo's play-by-play URL, run it again with `--yahoo --url '<url with {event_id}>'` to print Yahoo's payload instead of ESPN's (paste that output too): Yahoo is not polled until a parser is written from that.
7. After the game. Settlement writes the in-game bets with an in-game flag, the game state at entry and no CLV (closing line value is a pre-game measure), credited to the in-game model's lineage: its row in "In-game models" and its model page show "N bets · +$P&L". KILL cancels open in-game orders like any other open order and nothing else.

## How to test step 7

The UI overhaul changes only the pages (`docs/UI.md`, `docs/DASHBOARD.md`); every form, address and rule is the same.

1. Open the dashboard on the phone. The landing page is now Home: workers online, today's P&L of the current mode, open orders and the best model, then "Needs attention" (empty: "Nothing needs you."; a live assignment halted because its model lost live eligibility shows here too) and the last settled bets, all refreshed every 5 s like Fleet and Trading. The wordmark "Fleet" at the top left always leads back here; the five sections sit in a bar at the bottom of the screen, with Fleet now at `/fleet`.
2. The top bar shows only the current mode's P&L ("paper +$6.57 today"). The other mode's figure and the all-time totals are the first stats on Trading.
3. Every page opens with its title, a grey "What this page is" line (tap it to fold it; the dashboard remembers) and two to four big numbers. Lists are one line per row: tap the row for its page, tap "..." for the rest (Halt, Cancel, Train, Validate, Assign, Retire, Disable).
4. Trading, Settings and the model page fold their sections. Each header says what is inside ("Open orders 2", "max bet $25.00 · daily loss ..."); open the ones you need and they stay open across the 5 s refresh and a reload. A link to a section (the EXCHANGE DOWN banner, `/settings#kill`) opens it.
5. With JavaScript off everything still works: the sections and "..." menus open on tap, every action is a form, and Jobs shows all five New job forms under their headings.
6. Developers: `tests/hw/screenshots.py` captures every page at 390 and 1280 px in light and dark and checks the layout rules; `tests/hw/test_row_audit.py` checks Models and Trading stay under six phone screens with 20 rows each.

## Workloads

Machines can run more than the Polymarket worker. A workload is a folder under `workloads/` (a manifest, a Dockerfile, code); you publish its image to a registry on the host and assign it to a machine, and a small agent on the machine (`fleetagent/`, separate from the worker, standard library only) pulls the image by digest and runs the container. Everything about the contract is in `docs/workloads-design.md`; how to write a workload is in `workloads/README.md` and `workloads/_template/`. Polymarket's own orders keep their existing automatic approval, kill switch and live switch; these steps change none of that.

### One-time setup on the host

1. Pull the new code and start the stack. This adds a `registry` service (`registry:2`, published on `127.0.0.1:5000` only, data in the volume `fleet-registry`). Rebuild only `host` and start `registry`: the `exchange` code is unchanged, and rebuilding it would restart the exchange (do that later, never while games are live):

```powershell
docker compose up -d --build host registry
```

2. Put the registry on the tailnet, in an elevated PowerShell, so machines can pull from it:

```powershell
tailscale serve --bg --https=5000 http://127.0.0.1:5000
tailscale funnel status
```

   The second command must still show nothing enabled.
3. Add the registry address to `.env` (same machine and tailnet names as `FLEET_PUBLIC_URL`) and restart the host:

```
FLEET_REGISTRY=<machine>.<tailnet>.ts.net:5000
```

4. Create `secrets.env` next to `.env`. It holds the key that encrypts workload secrets in Postgres, is read only by the `host` service and is kept out of git and out of the image like every `*.env` file. Back it up: without the key the stored secrets cannot be read.

```powershell
python -c "import base64,os;print('FLEET_SECRETS_KEY=' + base64.b64encode(os.urandom(32)).decode())" > secrets.env
docker compose up -d host
```

### Publish a workload and use it

5. Sync the manifests (rebuild `host` first when a manifest changed, because the image carries `workloads/`), then publish the image. `publish.sh` is a bash script: run it in Git Bash or WSL on the host.

```powershell
docker compose exec host python -m host.cli workloads-sync
bash tools/workloads/publish.sh hello
```

   Publishing builds the image on the host, pushes it to `localhost:5000`, and records its digest and size. The Workloads page (`/workloads`) now shows `hello` as published.
6. Set the greeting secret (the value is read from standard input and is never shown again), or use the form on `/workloads/hello`:

```powershell
echo Ahoy | docker compose exec -T host python -m host.cli secret-set hello HELLO_GREETING
```

7. Install the agent on a machine. Mint a single-use token (valid 1 hour) and run the installer as a user with sudo; it installs `docker.io` with apt when Docker is missing, creates the `fleet-agent` user (a member of the `docker` group, which is root-equivalent on that machine), enrolls and starts the `fleet-agent` service. It never touches `fleet-worker.service` or `/var/lib/fleet`.

```powershell
docker compose exec host python -m host.cli machine-enroll-token
```

```bash
curl -fsSL https://<host>/install-agent.sh | sudo bash -s -- https://<host> <token> --name <machine name>
systemctl status fleet-agent
```

8. Open `/machines`. The machine appears with its disk type, RAM and Docker state. Pick `hello` in its workload dropdown (or `docker compose exec host python -m host.cli machine-assign <machine> hello`; plain `assign` is the Polymarket game command). Workloads that do not fit are greyed with the reason, for example a write-heavy one on an SD card. Within a few heartbeats the container is running.
9. Send a job and read the result on `/workloads/hello`:

```powershell
docker compose exec host python -m host.cli wl-send-job hello hello --params '{"name": "Ada", "steps": 3, "notify": true}'
```

   The result greeting uses your secret. Because the job asked to `notify`, it also queued a log action: open `/outbound` (the Approvals tab under Fleet) and it waits there as pending. Nothing is sent until you press Approve; after that the host marks it sent.
10. Assign `none` to the machine. The container stops and its scratch folder and secret files are wiped; the agent's hourly cleanup removes the image and prunes the build cache.

To try all of this on one computer without touching a real machine, run `tools/workloads/local_demo.sh` (needs Docker, the local images `registry:2` and `python:3.13-slim`, jq, curl and a Postgres superuser URL in `FLEET_TEST_DATABASE_URL`). It starts a throwaway registry, database, host and agent, runs the hello job, checks the secret and log redaction, the approval queue, the cleanup and the write-heavy refusal, prints PASS or FAIL per step and removes everything. Note that the agent's cleanup runs `docker image prune` and `docker builder prune -af` on the local Docker daemon.

### Rolling out to one non-trading machine first

Do this before moving anything that trades. The host pins any machine whose worker holds a live trade or open live order, and refuses to assign a workload to a pinned machine; still, pick the machine yourself.

1. On `/fleet/list` choose a worker that is idle and has no live assignment. Set it to idle and disable it, then stop the native worker: `sudo systemctl stop fleet-worker`. (Assignment is refused with a 409 while the native worker is active.)
2. Install the agent as in step 7. On `/machines` check the machine is online, has no active native worker, shows a plausible disk type (set it by hand under Details if it was detected wrongly; an SD card must read `flash`) and that Docker is reported working.
3. Assign `hello`, send the job from step 9, approve the log action, then assign `none`. On the machine confirm the cleanup:

```bash
docker ps -a
docker images
ls -A /var/lib/fleet-workloads/hello/scratch
```

   No `fleet.workload` container, no `fleet/hello` image and an empty scratch folder are what you want to see.
4. Reboot the machine once with `hello` assigned. The agent starts at boot, adopts or restarts the container and the machine returns to `running` without you touching it.
5. Re-enable the native worker: `sudo systemctl start fleet-worker`, then enable it on `/fleet/list`. Check `systemctl status fleet-worker`; its identity and code version are unchanged.
6. Only then consider moving Polymarket itself into a container, one non-trading machine at a time, with the steps and the paper parity check in `docs/workloads-design.md` section 7 (`tools/workloads/paper_parity.py`). A machine that trades live stays native until you decide otherwise.

## Data

Game schedules, scores and closing lines come from [nflverse](https://github.com/nflverse/nflverse-data) (`games.csv`), licensed CC BY 4.0. Attribution: "Data: nflverse (https://nflverse.com), CC BY 4.0." It is also shown on the Models page. From step 6B the host also loads two more nflverse-data files per season: the weekly injury reports (`injuries_{season}.csv`, for the players listed Out) and play-by-play (`play_by_play_{season}.csv.gz`, folded into per team-game EPA per play, pass rate and success rate). Both download URLs are editable templates in Settings. From step 6C play-by-play also becomes one row per play (`pbp_rows`), the training data of the in-game model, with nflverse's `vegas_wp` as its baseline. Prices for snapshot replays are the host's own recordings from the market source, never a third-party history. The live game state comes from ESPN's unofficial summary endpoint (the scoreboard as a fallback and for finals), at most one request per second by default and backing off on 429 or 403; broadcasts are never captured, and NFL.com's live feeds are not used.

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
6. Step 6: robustness (`docs/ROBUSTNESS.md`)
   - [x] 6A: validation era, confidence intervals, market test, stress tests, stricter gates, multi-core search
   - [x] 6B: snapshot replay backtests, quarterback, injury and play-by-play signals, the `epa_blend` family, selling a held position
   - [x] 6C: in-game trading on paper (`docs/INGAME.md`): ESPN game-state feed, feed-lag measurement, the `ingame_wp` family on nflverse play-by-play, in-game trade rules and settlement
7. [x] Step 7: UI overhaul (`docs/UI.md`): Home at `/`, Fleet at `/fleet`, one-line rows with action menus, disclosures, a bottom nav on phones

Data is free-only for now; paid sources are considered once profit comes in.
