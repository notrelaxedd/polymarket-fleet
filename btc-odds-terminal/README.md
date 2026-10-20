# BTC Odds Terminal

A TradingView-style terminal for Polymarket US "BTC Up or Down: 15 min" markets. One chart puts live BTC price and the current window's Up odds on the same time axis, with the price to beat, window bands and an edge pane (fair value minus the market). The backend runs headless on a Debian worker, so it keeps recording (and, later, enforcing brackets and alerts) with the browser closed. The frontend is Next.js: run it locally, run it in mock mode, or deploy it to Vercel against the worker.

**Status:** build steps 1–2 are done: feeds, market discovery and rollover, SQLite recording, the WebSocket, the app shell, the header and the main chart. Not built yet: paper trading (3), drawings and stickers (4), the pair-relationship and other bottom tabs (5), and alerts, risk controls, replay and live trading (6). No code places orders yet.

## Architecture

```
 Coinbase, Kraken, Bitstamp (public WS)        gateway.polymarket.us (public REST)
   last trade / mid per exchange                 BBO, book, settlement
            |                                              |
            v                                              v
    BRTI proxy = median  ------>  +-------------- backend (Node 22 + tsx) ----------+
                                  | feeds, market discovery and rollover             |
                                  | fair value (Phi), recorder -> SQLite (DB_PATH)   |
                                  | Fastify REST  /api/*      WebSocket  /ws         |
                                  +------------------------+-------------------------+
                                                           | REST + one WebSocket
                                                           v
                              web (Next.js 14, Lightweight Charts v5)
                              localhost:3000, or Vercel (the browser connects directly)

 Live trading only (step 6): backend -> api.polymarket.us (Ed25519-signed requests)
```

## Data sources

- **BTC price, labeled "BTC · BRTI proxy".** The markets settle on CF Benchmarks BRTI, which is not freely streamable. The proxy is the median of the last trade or mid from the Coinbase BTC-USD, Kraken and Bitstamp WebSockets. It is not BRTI. Settlement can differ from the proxy, especially when price sits right at the strike.
- **Odds, Polymarket US market data.** Public, no key. BBO, book and settlement come from `POLY_GATEWAY_URL` (default `https://gateway.polymarket.us`), polled every `POLY_POLL_MS`. Discovery reads the active 15m BTC market and the next one, including price to beat, window start and end, and the Up and Down instruments, from market metadata. API reference: https://docs.polymarket.us/api-reference/introduction
- **Trading, Polymarket US authenticated API.** `api.polymarket.us` with an Ed25519 key pair from polymarket.us/developer. Used only in live mode (step 6). Keys live in `backend/.env` and nowhere else.
- **Fees are unconfirmed.** `FEE_CENTS_PER_CONTRACT` stays blank, and the UI shows **[FEE]** instead of a number until the Polymarket US fee schedule is confirmed. Once it is, record the source in `docs/POLYMARKET_US.md` (not written yet).

## Fair value

```
P(Up) = Φ( ln(S / K) / (σ · √t) )
```

- `S` = BRTI proxy, `K` = price to beat, `t` = seconds left in the window
- `σ` = realized volatility per √second of the proxy over `VOL_LOOKBACK_MIN` (default 30 minutes)
- **Edge = fair − Up mid**, in cents. The edge pane plots it; the header shows FAIR · EDGE.

## Worker setup (Debian 12/13)

You need sudo, outbound HTTPS and WSS on port 443, and a Tailscale install if you want to reach the worker or the dashboard from other devices.

From a checkout of this repo on the worker:

```bash
cd polymarket-fleet/btc-odds-terminal/backend/deploy
sudo ./install.sh
```

`install.sh` is idempotent, so re-run it to upgrade. It:

1. Installs Node 22 from NodeSource if it is missing, plus `build-essential` and `python3` for better-sqlite3.
2. Creates the `btcodds` system user.
3. Copies `backend/` and `shared/` to `/opt/btc-odds-terminal` (root-owned, so the service cannot change its own code).
4. Runs `npm ci` in the backend when the lockfile changes.
5. Creates `backend/.env` from `.env.example` with chmod 600 if it is missing.
6. Runs `npm run seed` on the first install.
7. Installs, enables and restarts the `btc-odds-terminal` service, then checks `/api/health`.

After the first install, edit the config and restart:

```bash
sudo nano /opt/btc-odds-terminal/backend/.env     # HOST, CORS_ORIGIN, TRADING_MODE, ...
sudo systemctl restart btc-odds-terminal
```

Manual setup, if you would rather not run the script. Run it from the `btc-odds-terminal/` project directory of your checkout:

```bash
sudo apt-get install -y curl rsync build-essential python3    # plus Node 22
sudo useradd --system --home-dir /opt/btc-odds-terminal --no-create-home --shell /usr/sbin/nologin btcodds
sudo install -d -m 0755 /opt/btc-odds-terminal/backend
sudo rsync -a --exclude=node_modules --exclude=data --exclude=.env backend/ /opt/btc-odds-terminal/backend/
sudo rsync -a shared/ /opt/btc-odds-terminal/shared/
(cd /opt/btc-odds-terminal/backend && sudo npm ci)
sudo install -d -m 0750 -o btcodds -g btcodds /opt/btc-odds-terminal/backend/data
sudo install -m 0600 -o btcodds -g btcodds /opt/btc-odds-terminal/backend/.env.example /opt/btc-odds-terminal/backend/.env
sudo cp backend/deploy/btc-odds-terminal.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now btc-odds-terminal
```

Service commands:

```bash
sudo systemctl status btc-odds-terminal
sudo systemctl restart btc-odds-terminal      # after editing .env
sudo systemctl stop btc-odds-terminal         # also stops feeds and recording
sudo journalctl -u btc-odds-terminal -f       # live logs
sudo journalctl -u btc-odds-terminal -n 200 --no-pager
curl -s http://127.0.0.1:8787/api/health      # {"ok":true,"serverTime":...}
```

The unit runs as `btcodds`, restarts after a crash (`Restart=always`, 3 s delay) and starts at boot. Logs are JSON lines (pino), because systemd's stdout is not a terminal.

## Frontend

Local development:

```bash
cd btc-odds-terminal/web
cp .env.example .env.local     # points at http://localhost:8787 by default
npm ci
npm run dev                    # http://localhost:3000
```

For the local backend, run `cd btc-odds-terminal/backend && cp .env.example .env && mkdir -p data && npm ci && npm start`.

**Mock mode** runs on synthetic data with no backend. Set `NEXT_PUBLIC_MOCK=1` in `.env.local`, or add `?mock=1` to the URL for one page load.

### Vercel pointed at the worker

The browser connects straight to the worker, so the worker has to be reachable from the browser over HTTPS and WSS. A page served over https cannot open `ws://`. Use `tailscale serve`, not Funnel. Funnel would publish the API to the internet, and the API has no login.

1. In the Tailscale admin console, enable MagicDNS and HTTPS certificates.
2. On the worker, bind the backend to loopback in `backend/.env` (`HOST=127.0.0.1`), restart the service, and serve it on the tailnet:

   ```bash
   sudo tailscale serve --bg --https=443 http://127.0.0.1:8787
   sudo tailscale serve status        # shows https://<worker>.<tailnet>.ts.net
   ```

3. On Vercel, create a project with **Root Directory** `btc-odds-terminal/web`. Keep "Include source files outside of the Root Directory" enabled, because the app imports `../shared`. Set:

   ```
   NEXT_PUBLIC_API_URL=https://<worker>.<tailnet>.ts.net
   NEXT_PUBLIC_WS_URL=wss://<worker>.<tailnet>.ts.net/ws
   NEXT_PUBLIC_MOCK=0
   ```

4. Add your Vercel origin to `CORS_ORIGIN` on the worker, for example `https://your-app.vercel.app`. Comma-separate it with any local origins. Restart the service.
5. Redeploy Vercel after changing any `NEXT_PUBLIC_*` value. Those values are baked in at build time.

The browser must be on the tailnet, meaning Tailscale is running on that device and signed in to your tailnet. Vercel's servers never call the worker.

## Paper to live

Paper trading is the default and the only mode that is safe to use today. Live trading lands in step 6. It will require `TRADING_MODE=live` in `backend/.env`, the settings toggle in the app, and a confirm dialog on the first live order in each session. Until step 6 lands, leave `TRADING_MODE=paper`. The `POLYMARKET_US_*` keys are used only in live mode.

## Seed script

`npm run seed` (in `backend/`) backfills `SEED_HOURS` (default 6) of recent BTC history as 60-second bars, so the chart is not empty on first load. `install.sh` runs it once on a new install. Run it by hand if you need more history.

## Tests and checks

```bash
cd btc-odds-terminal/backend && npm test          # vitest, test/**/*.test.ts
cd btc-odds-terminal/backend && npm run typecheck
cd btc-odds-terminal/web && npm run typecheck && npm run build
```

## REST and WebSocket API

The wire types live in `shared/protocol.ts`. Every timestamp is epoch milliseconds in UTC. Every contract price is in cents (0–100). BTC prices are USD.

REST, all under `/api`:

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{ ok, serverTime }` |
| GET | `/api/state` | `StateSnapshot`: settings, current and next window, recent windows, last tick, odds and fair, feed status |
| GET | `/api/history/bars?from&to` | `Bar[]`, ascending: 60 s seeded bars plus 1 s live bars |
| GET | `/api/history/odds?from&to` | `OddsSnap[]`, ascending, at most about one per second |
| GET | `/api/history/fair?from&to` | `FairSnap[]`, ascending, at most about one per second |
| GET | `/api/windows?limit=50` | `MarketWindow[]`, newest first |
| GET | `/api/settings` | `Settings` |
| PUT | `/api/settings` | `Settings`, from a partial body |

WebSocket at `/ws`. The server sends `{ type, data }` messages: `snapshot`, `btc`, `odds`, `fair`, `window` (rollover, price-to-beat known, settlement), `rollover`, `feeds` (every second) and `settings`. The client sends `{ type: "ping", ts }`. A feed is stale after 3 s without a message (`STALE_AFTER_MS`).

## Troubleshooting

- **Stale-feeds banner or amber dots.** A feed is `stale` after 3 s without a message, and `down` when its socket is closed or never connected. Check `curl -s http://127.0.0.1:8787/api/state` and read `feeds`. Feeds reconnect with backoff. If one stays down, read the logs: `sudo journalctl -u btc-odds-terminal -n 200 --no-pager`.
- **Outbound firewall.** The worker needs outbound TCP 443 to the three exchanges' WebSocket endpoints, to `gateway.polymarket.us`, and to `api.polymarket.us` (live only). If a firewall blocks one of them, the matching feed stays `down`. Allow it, then restart the service.
- **Chart is empty on first load.** The seed did not run or failed. Re-run `sudo ./install.sh`; it retries the seed until one succeeds.
- **Service will not start, with a namespace or read-write path error.** The data directory is missing. Run `sudo install -d -m 0750 -o btcodds -g btcodds /opt/btc-odds-terminal/backend/data`.
- **Vercel page cannot connect, or shows mixed-content errors.** Check that the URLs are `https://` and `wss://`, that the browser is on the tailnet, and that the Vercel origin is in `CORS_ORIGIN`.
- **The BRTI proxy and the settlement disagree.** This is expected near the strike. See Data sources.
- **`.env` and secrets.** `backend/.env` holds the Polymarket keys once you add them. It is ignored by git and install.sh keeps it chmod 600, owned by `btcodds`.
