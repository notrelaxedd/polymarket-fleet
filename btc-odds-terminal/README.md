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
- **Odds, Polymarket US market data.** Public, no key. BBO, book and settlement come from `POLY_GATEWAY_URL` (default `https://gateway.polymarket.us`), polled every `POLY_POLL_MS`. API reference: https://docs.polymarket.us/api-reference/introduction
- **Unverified: market discovery and the price to beat.** The endpoints, models and WebSocket signing were checked against the official SDKs, but no live response was ever captured. How the 15m BTC series is found, the slug pattern, the price-to-beat field names, and what an unsettled settlement looks like are best guesses (see [`docs/POLYMARKET_US.md`](docs/POLYMARKET_US.md)). When no price-to-beat field is found in the market metadata, the backend falls back to the BRTI proxy price at window start. K, and so fair value and edge, is then the proxy price and not the real strike. If discovery finds nothing, the guesses can be corrected from `backend/.env` with `POLY_SEARCH_QUERY`, `POLY_SERIES_ID`, `POLY_TAG_SLUG`, `POLY_SLUG_REGEX` and `POLY_PTB_FIELDS` (see Troubleshooting).
- **Authenticated API, Polymarket US.** `api.polymarket.us` with an Ed25519 key pair from polymarket.us/developer. The keys are optional, and they are used in two places. In any mode, setting both `POLYMARKET_US_KEY_ID` and `POLYMARKET_US_SECRET` makes the backend sign and open the markets WebSocket `wss://api.polymarket.us/v1/ws/markets` for BBO updates. Without keys, or with `POLY_WS_MODE=off`, it polls the public gateway instead. `POLY_WS_MODE` is `auto` (default: WebSocket when keys are set), `off` (always poll) or `noauth` (try the WebSocket unsigned). `POLY_WS_URL` and `POLY_TIMEOUT_MS` (REST timeout, default 5000) also exist. Live orders (step 6) will use the same keys. Keys live in `backend/.env` and nowhere else.
- **Fees are unconfirmed.** `FEE_CENTS_PER_CONTRACT` stays blank, and the UI shows **[FEE]** instead of a number until the Polymarket US fee schedule is confirmed. Once it is, record the source in [`docs/POLYMARKET_US.md`](docs/POLYMARKET_US.md), which also lists what was verified against the official SDKs and what is still unverified.

## Fair value

```
P(Up) = Φ( ln(S / K) / (σ · √t) )
```

- `S` = BRTI proxy, `K` = price to beat, `t` = seconds left in the window
- `σ` = realized volatility per √second of the proxy over the lookback (`VOL_LOOKBACK_MIN`, default 30 minutes)
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
3. Copies `backend/` and `shared/` to `/opt/btc-odds-terminal` (root-owned, mode 755/644 whatever the checkout's umask, so the service cannot change its own code).
4. Runs `npm ci` in the backend when the lockfile changes.
5. Creates `backend/.env` from `.env.example` with chmod 600 if it is missing.
6. Runs `npm run seed` until it succeeds once. The seed exits non-zero when fewer than 80% of the expected minutes come back, and then the service still starts and a re-run of `install.sh` retries it.
7. Installs, enables and restarts the `btc-odds-terminal` service, then checks `/api/health`.

After the first install, edit the config and restart:

```bash
sudo nano /opt/btc-odds-terminal/backend/.env     # HOST, CORS_ORIGIN, POLYMARKET_US_*, ...
sudo systemctl restart btc-odds-terminal
```

`HOST`, `PORT`, `CORS_ORIGIN`, `DB_PATH` and the `POLY_*` variables are read on every start. `TRADING_MODE`, `VOL_LOOKBACK_MIN` and `FEE_CENTS_PER_CONTRACT` are only defaults for the three app settings: once you change a setting in the app (`PUT /api/settings`), the stored value wins over `.env` from then on, and later `.env` edits to that one setting are ignored. Settings you never changed keep following `.env`. To go back to paper after trying live, switch the mode in the app, not in `.env`.

`HOST` must be a literal address (`127.0.0.1`, `0.0.0.0`, `::` or `::1`) for the installer's health check to work. `install.sh` warns if `CORS_ORIGIN` is still `*`.

Manual setup, if you would rather not run the script. This is for the **first install only**: use `install.sh` to upgrade. Run it from the `btc-odds-terminal/` project directory of your checkout, as a user whose umask is 022 (`umask 022`). Node 22 is installed separately (NodeSource: `curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - && sudo apt-get install -y nodejs`).

```bash
umask 022
sudo apt-get install -y curl rsync build-essential python3    # plus Node 22, see above
sudo useradd --system --home-dir /opt/btc-odds-terminal --no-create-home --shell /usr/sbin/nologin btcodds
sudo install -d -m 0755 /opt/btc-odds-terminal/backend
sudo rsync -a --chown=root:root --chmod=D755,F644 --exclude=node_modules --exclude=data --exclude=.env backend/ /opt/btc-odds-terminal/backend/
sudo rsync -a --chown=root:root --chmod=D755,F644 shared/ /opt/btc-odds-terminal/shared/
(cd /opt/btc-odds-terminal/backend && sudo npm ci)
sudo install -d -m 0750 -o btcodds -g btcodds /opt/btc-odds-terminal/backend/data
[ -f /opt/btc-odds-terminal/backend/.env ] || sudo install -m 0600 -o btcodds -g btcodds /opt/btc-odds-terminal/backend/.env.example /opt/btc-odds-terminal/backend/.env
(cd /opt/btc-odds-terminal/backend && sudo -u btcodds env HOME=/opt/btc-odds-terminal/backend/data npm run seed)
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

4. Set `CORS_ORIGIN` on the worker to your Vercel origin, for example `https://your-app.vercel.app`, comma-separated with any local origins. The shipped default is `http://localhost:3000,http://127.0.0.1:3000` (the local Next.js app). `*` allows any web page open in any browser that can reach the worker to read and change settings, so only use it deliberately. Restart the service.
5. Redeploy Vercel after changing any `NEXT_PUBLIC_*` value. Those values are baked in at build time.

The browser must be on the tailnet, meaning Tailscale is running on that device and signed in to your tailnet. Vercel's servers never call the worker.

## Paper to live

Paper trading is the default and the only mode that is safe to use today. Live trading lands in step 6, and no code places orders yet.

The gates are weaker than you might assume, so read this before adding keys:

- `TRADING_MODE` in `backend/.env` is only the default for the stored mode setting, not a gate. It can be overridden from the app.
- The backend accepts `PUT /api/settings {"mode":"live"}` whenever both `POLYMARKET_US_KEY_ID` and `POLYMARKET_US_SECRET` are set. Having the keys is the only server-side gate. At startup a stored `live` falls back to `paper` if the keys are missing.
- The API has no login. Anyone who can reach it, from an origin in `CORS_ORIGIN` or with a non-browser client, can flip the mode. Keep `HOST` on loopback and the API on the tailnet.
- Step 6 is meant to add a confirm dialog on the first live order in each session. Until it lands, leave the keys out unless you want the authenticated markets WebSocket (see Data sources), and keep the mode on paper.

Keys are not live-only. Setting them turns on the authenticated markets WebSocket in paper mode too (`POLY_WS_MODE=off` keeps polling).

## Seed script

`npm run seed` (in `backend/`) backfills `SEED_HOURS` (default 6) of recent BTC history as 60-second bars from the Coinbase REST API (`api.exchange.coinbase.com`), so the chart is not empty on first load. `install.sh` runs it on a new install and re-runs it on each later `install.sh` until it succeeds once. It exits non-zero when a fetch fails or fewer than 80% of the expected minutes come back. Inserts are idempotent, so running it again is safe. To re-run it by hand on the worker, as the service user:

```bash
cd /opt/btc-odds-terminal/backend
sudo -u btcodds env HOME=/opt/btc-odds-terminal/backend/data npm run seed
```

To backfill more, change `SEED_HOURS` in `.env` first, or add `SEED_HOURS=24` after `env HOME=...` in the command.

If `install.sh` stamped a seed that left the chart empty, delete `/opt/btc-odds-terminal/backend/data/.seeded` and re-run `install.sh`, or run the command above.

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
| GET | `/api/history/bars?from&to` | `Bar[]`, ascending: 60 s seeded bars plus 1 s live bars (see range rules below) |
| GET | `/api/history/odds?from&to` | `OddsSnap[]`, ascending, at most about one per second |
| GET | `/api/history/fair?from&to` | `FairSnap[]`, ascending, at most about one per second |
| GET | `/api/windows?limit=50` | `MarketWindow[]`, newest first. `limit` defaults to 50 and is capped at 1000 |
| GET | `/api/settings` | `Settings` |
| PUT | `/api/settings` | `Settings`, from a partial body |

History range rules, shared by the three `/api/history/*` endpoints. With neither `from` nor `to`, you get the last 3 hours. With only `from`, `to` is `min(now, from + 3 h)`, so `?from=<6 h ago>` returns 3 hours, not 6. With only `to`, `from` is `to - 3 h`. A range longer than 24 hours is not an error: `from` is silently moved forward to `to - 24 h`, so the oldest part is dropped. To read more, page through 24 hour ranges with explicit `from` and `to`. `from > to` returns 400.

WebSocket at `/ws`. The server sends `{ type, data }` messages: `snapshot`, `btc`, `odds`, `fair`, `window` (rollover, price-to-beat known, settlement), `rollover`, `feeds` (every second) and `settings`. The client sends `{ type: "ping", ts }`. A feed is stale after 3 s without a message (`STALE_AFTER_MS`).

## Troubleshooting

- **Stale-feeds banner or amber dots.** A feed is `stale` after 3 s without a message, and `down` when its socket is closed or never connected. Check `curl -s http://127.0.0.1:8787/api/state` and read `feeds`. Feeds reconnect with backoff. If one stays down, read the logs: `sudo journalctl -u btc-odds-terminal -n 200 --no-pager`.
- **Outbound firewall.** The worker needs outbound TCP 443 to the three exchanges' WebSocket endpoints, to `api.exchange.coinbase.com` (the seed's REST history), to `gateway.polymarket.us`, and to `api.polymarket.us` (the markets WebSocket, used whenever the `POLYMARKET_US_*` keys are set, in paper mode too, and later live orders). If a firewall blocks one of them, the matching feed stays `down` or the seed fails. Allow it, then restart the service. Without the keys, `api.polymarket.us` is not needed.
- **Chart is empty on first load.** The seed did not run, failed, or returned too few bars. Re-run `sudo ./install.sh` (it retries until a seed succeeds), or run the seed by hand, see Seed script. Check that `api.exchange.coinbase.com` is reachable.
- **Polymarket feed `down` with detail `no active window`, or no odds.** Discovery found no 15m BTC window. Its heuristics are unverified. Follow "Recovery procedure when the terminal shows no active window" in [`docs/POLYMARKET_US.md`](docs/POLYMARKET_US.md): run the verification `curl` against `https://gateway.polymarket.us/v1/series` and `/v1/search`, then set `POLY_SERIES_ID`, `POLY_SEARCH_QUERY`, `POLY_TAG_SLUG`, `POLY_SLUG_REGEX` or `POLY_PTB_FIELDS` in `backend/.env` and restart. Detail `ws: upgrade rejected: HTTP 401` means the keys or the clock are wrong. Fix them or set `POLY_WS_MODE=off`.
- **Service will not start, with a namespace, read-write path or "read-only file system" error.** Either the data directory is missing (run `sudo install -d -m 0750 -o btcodds -g btcodds /opt/btc-odds-terminal/backend/data`), or `DB_PATH` points outside `backend/data`. The unit's `ProtectSystem=strict` only allows writes there, so SQLite fails on every start and `Restart=always` loops. Keep `DB_PATH=./data/terminal.db`, or add the directory to `ReadWritePaths=` in the unit.
- **Vercel page cannot connect, or shows mixed-content errors.** Check that the URLs are `https://` and `wss://`, that the browser is on the tailnet, and that the Vercel origin is in `CORS_ORIGIN`.
- **The BRTI proxy and the settlement disagree.** This is expected near the strike. See Data sources.
- **`.env` and secrets.** `backend/.env` holds the Polymarket keys once you add them. It is ignored by git and install.sh keeps it chmod 600, owned by `btcodds`.
