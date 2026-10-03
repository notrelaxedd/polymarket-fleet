# polymarket-fleet

A small fleet of machines that builds, tests and (much later) runs NFL prediction-market models. One host runs a Postgres-backed job queue, a dashboard and the trading limits; any number of Debian worker boxes run a tiny stdlib-only agent that takes jobs according to the role you give it. Everything is private to your Tailscale network, paper trading is the default, and the host enforces every limit.

## Architecture

The host is a Windows 11 machine running a Docker Compose stack: `db` (postgres:16) and `host` (the FastAPI app that serves the worker API, the owner API and the dashboard). Both containers publish ports on `127.0.0.1` only. `tailscale serve` on the host OS puts HTTPS in front of port 8080 for your tailnet and injects a `Tailscale-User-Login` header, which the app compares with `FLEET_OWNER_LOGIN` to identify you. Workers are Debian 13 boxes: each runs the `fleet-worker` systemd service, which registers with an enroll token, sends a heartbeat every 5 seconds, claims leased jobs for its current role, checkpoints as it goes, and updates its own code from the host. The wire contract is in `docs/PROTOCOL.md`; the full design is in `docs/DESIGN.md`.

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

5. Start the stack:

```powershell
docker compose up -d
```

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
5. Kill switch. Press KILL and confirm. The whole top bar turns red with "TRADING KILLED. Reset in Settings." and the button becomes a disabled KILLED chip. Then open Settings, find the kill form, type `RESUME` exactly and submit; the bar returns to normal. Anything other than `RESUME` is rejected and the flag stays set. The kill switch only affects trading (the trade role); batch jobs such as backtests and training keep running. In step 4 KILL also cancels all open orders.
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
4. [ ] Step 4: fleet-exchange, paper trading, approval limits, ledger, scoring
5. [ ] Step 5: Polymarket US live adapter, live switch, smoke order

Data is free-only for now; paid sources are considered once profit comes in.
