# polymarket-fleet

A small fleet of machines that builds, tests and (much later) runs NFL prediction-market models. One host runs a Postgres-backed job queue, a dashboard and the trading limits; any number of Debian worker boxes run a tiny stdlib-only agent that takes jobs according to the role you give it. Everything is private to your Tailscale network, paper trading is the default, and the host enforces every limit.

## Architecture

The host is a Windows 11 machine running a Docker Compose stack: `db` (postgres:16) and `host` (the FastAPI app that serves the worker API, the owner API and the dashboard). Both containers publish ports on `127.0.0.1` only. `tailscale serve` on the host OS puts HTTPS in front of port 8080 for your tailnet and injects a `Tailscale-User-Login` header, which the app compares with `FLEET_OWNER_LOGIN` to identify you. Workers are Debian 13 boxes: each runs the `fleet-worker` systemd service, which registers with an enroll token, sends a heartbeat every 5 seconds, claims leased jobs for its current role, checkpoints as it goes, and updates its own code from the host. The wire contract is in `docs/PROTOCOL.md`; the full design is in `docs/DESIGN.md`.

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

On each Debian box, as a user with sudo (needs root, python3 >= 3.11, curl and systemd, all present on Debian 13):

```bash
curl -fsSL https://<host>/install.sh | sudo bash -s -- https://<host> <token>
```

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
2. [ ] Step 2: dashboard fleet cards, role handshake, drain and watchdog, settings page, kill flag
3. [ ] Step 3: nflverse data, models, backtest / search / train jobs, leaderboard
4. [ ] Step 4: fleet-exchange, paper trading, approval limits, ledger, scoring
5. [ ] Step 5: Polymarket US live adapter, live switch, smoke order

Data is free-only for now; paid sources are considered once profit comes in.
