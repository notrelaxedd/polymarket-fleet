# Design

Multi-machine fleet for NFL prediction-market models. Step 1 (fleet core) is specified exactly in `docs/PROTOCOL.md`, models in `docs/MODELS.md`, the dashboard in `docs/DASHBOARD.md` and trading (step 4 paper) in `docs/TRADING.md`, live trading (step 5) in `docs/LIVE.md`, robustness (step 6A and 6B) in `docs/ROBUSTNESS.md`, in-game trading (step 6C) in `docs/INGAME.md`; this file is the overall design and does not repeat them. Where the Trading sections below differ from `docs/TRADING.md` or `docs/LIVE.md`, those files win.

## Summary

1. Host: a Windows 11 machine running a Docker Compose stack under Docker Desktop: `db` (postgres:16), `host` (`fleet-host`: worker API, job queue, dashboard) and, from step 4, `fleet-exchange` (a second process, started by the same `docker compose up -d`; sole holder of Polymarket US keys, separate container and database role). No broker. Every port is bound to `127.0.0.1` and exposed to the tailnet with `tailscale serve` (tailnet only, never Funnel). The same compose file runs on a Debian box if the host moves.
2. Worker: one zero-dependency Python agent, one installer, 5 s heartbeat, role switch within 10 s after checkpoint/cancel, automatic restart from host state.
3. Trading: workers only propose; the host approves every order against per-game bankroll, max bet, max daily loss and the liquidity floor. Paper by default, live behind a typed switch, one kill button.
4. Learning: per-game scoring (PnL, bets, CLV), all-games leaderboard by lineage, backtest AND paper gates before real money, no override.
5. Five build steps, hard stop after each for the owner's test and OK. All five are done; step 6 (robustness, Parts A and B; in-game trading, Part C) is done too, and so is step 7 (UI overhaul).
6. Final state: a Windows 11 host (Docker Compose: `db`, `host`, `exchange`) behind Tailscale serve; Debian workers on roles (backtest, model_search, train, trade); nflverse data (schedules, injury reports, play-by-play) and elo_blend and epa_blend models searched, validated, trained and scored by lineage; simulated and real Polymarket US market data snapshotted and replayed in snapshot backtests; paper trading by default, buying and selling; live trading behind the typed dated switch, a `live_eligible` lineage (no override) and an authenticated, clock-checked exchange session, with Ed25519-signed requests, GTD orders, restart reconciliation, open-order audit, auto-kill, `exchange-smoke` and `cancel-all --direct`. The Polymarket US request shapes remain unverified and configurable (see `docs/LIVE.md`).
7. Every limit number (lease, online window, bankroll, max bet, daily loss, floor, edge, Kelly, games per worker, thresholds) is editable on the dashboard settings page. Values quoted in this file are test defaults, not policy.

## Assumptions and things to verify

| # | Assumption | Verify |
|---|---|---|
| A1 | Polymarket US = `api.polymarket.us` (keyed, Ed25519 `X-PM-*` headers) + `gateway.polymarket.us` (public markets/books, no key); signing details unknown. Docs: https://docs.polymarket.com/getting-started/api | Steps 4, 5 |
| A2 | `POST /v1/orders`, `GET /v1/orders/open`, `GET /v1/order/{id}`; cancel, fills, balance paths unverified; cancel-all = list-then-cancel-each | Step 5 |
| A3 | ~20 req/s per key; seeded 5 orders/s, 10 cancels/s, 10 market-data/s, 2 account/s. Taker fee ~5%*p(1-p), maker 0%; configurable `fee_model` | Step 4 |
| A4 | Client order ids and GTD supported; else single-flight submission + open-order diff | Step 5 |
| A5 | NFL moneyline markets with public book depth; tick/min size unknown; no sandbox (paper simulates against real books) | Step 4 |
| A6 | ToS permits server-side API trading on a KYC'd retail account; possible geofence/device binding. The owner has no API keys yet and will obtain them | Step 5, owner |
| A7 | nflverse `games.csv` moneylines complete per season; closing lines are proxies | Steps 3, 4 |
| A8 | Docker Desktop on Windows 11 keeps the stack running across reboots (start at login, `restart: unless-stopped`); the host sleeping or logging out is a host outage | Step 1 |

## Stack and dependencies

| Side | Components |
|---|---|
| Host | Docker Desktop (Windows 11) or Docker on Debian; images `postgres:16` and the repo `Dockerfile` (Python 3.11); pip `fastapi`, `uvicorn`, `psycopg[binary,pool]`, `jinja2`, `python-multipart` (both host containers), `pynacl` (fleet-exchange only); dev `pytest`, `httpx`. Tailscale on the host OS provides `tailscale serve` |
| Worker | Debian 13 (Python 3.13), system `python3` >= 3.11; **zero pip packages**: stdlib `urllib`, `json`, `subprocess`, `signal`; CPU/RAM from `/proc`. Identical agent code everywhere; first model family is pure Python |
| Dashboard | Jinja2 server-rendered HTML + one CSS file + small vanilla JS (10 s fetch refresh, `confirm()`); phone-first, no build step |
| Python | 3.11+ syntax everywhere. No bundled interpreter, no uv/pyenv/venv on workers |

## Repository layout

```
pyproject.toml                       project "polymarket-fleet" (packages fleet, host); extras [host], [dev]
Dockerfile, docker-compose.yml, .env.example
fleet/common/{http,sysinfo}.py
fleet/models/{base,registry,elo,blend_fit,elo_blend,epa_features,epa_blend,ingame_wp,newton,search_space,summary_text}.py
fleet/sim/{data,odds,signals,fills,book,prices,metrics,records,stats,robust,stress,backtest,validate,search,parallel,train,ingame,ingame_eval,control}.py
fleet/worker/{__main__,agent,runner,launch,jobs,context,config,posts,update,watchdog,trade,sell,trade_ingame,pbp_cache}.py
host/{main,config,db,auth,errors,events,leases,queue,loop,scheduling,heartbeat,recovery,bundle,kill,settings,settings_schema,settings_schema_ingame,settings_forms,settings_forms_replay,settings_forms_ingame,web,views}.py
host/{nflverse,data_refresh,ingest_injuries,ingest_pbp,pbp_rows,pbp_refresh,signals,games_feed,prices_feed}.py          data and feeds
host/{models,model_owner,model_validation,jobparams,ingame_jobparams,snapshot_store,leaderboard,leaderboard_snapshot,leaderboard_ingame,eligibility,ingame_eligibility,paper_gate,stats,pnl,money}.py
host/trading/{assignments,assignments_ingame,ledger,limits,sells,ingame,orders,positions,state,live,views,views_positions,views_ingame}.py
host/migrations/0001_init.sql .. 0009_ingame.sql
host/api/{app,deps,workers,jobs,data,data_pbp,dl,models,owner,owner_live,owner_trading,trade,limits,robustness,serialize,dashboard,dashboard_forms,dashboard_models,dashboard_trading,dashboard_ingame,job_forms}.py + templates/ + static/style.css   fleet-host (FastAPI)
host/exchange/{main,executor,live_sync,mapping,paper,probe,ratelimit,retention,scores,settle,settle_sells,smoke,snapshots,state,credentials,gamestate,gamestate_parse,gamestate_rate,feedlag}.py   fleet-exchange
host/exchange/adapters/{base,sim,polymarket_us,polymarket_us_live,polymarket_clob,live_http,live_parse,live_policy,signing,teams}.py
host/cli.py                          migrate | enroll-token | workers | jobs | role | send-job | cancel | run-loop | ingest-games | ingest-injuries | ingest-pbp | ingest-pbp-rows | models | kill | kill-reset | roletest | assign | assignments | orders | cancel-all | ledger-check | exchange-state | simulate-final
host/exchange/cli.py                 simulate-final | probe | run-once | exchange-state | exchange-smoke | cancel-all [--direct] | probe-account | auth-check | probe-gamestate --event ID [--yahoo] [--url U]
deploy/{install_worker.sh,fleet-worker.service}
tests/test_*.py + hw/{roletest.sh,screenshots.py,ui_checks.py,test_ui.py,test_row_audit.py,seed_*.py}
tools/workflows/                     the build orchestration scripts
```

## Database schema

Money `bigint` cents; timestamps `timestamptz`; the owner's day uses `settings.tz` (default `America/New_York`). Step 1 tables are in `host/migrations/0001_init.sql`; the list below is the full target.

- **workers**: `id text PK`, `name`, `token_hash`, `desired_role` CHECK (idle,backtest,model_search,train,trade), `role_epoch`, `reported_role`, `acked_epoch`, `auto_role`, `enabled`, `cpu_pct`, `ram_used_mb`, `ram_total_mb`, `skew_ms`, `python_version`, `code_version`, `hostname`, `boot_id`, `remote_ip`, `last_heartbeat_at`.
- **enroll_tokens**: `token_hash PK`, `expires_at`, `used_by_worker_id`, `used_at`.
- **jobs**: `id uuid PK`, `kind`, `role`, `status` (queued,leased,cancel_requested,succeeded,failed,cancelled), `target_worker_id`, `auto_role`, `params`, `checkpoint`, `progress`, lease fields (`lease_worker_id`, `lease_token`, `lease_expires_at`), `expiries`, `max_expiries` (NULL for trade), `run_after`, `preempt_requested`, `idempotency_key UNIQUE`, `result`, `error`.
- **job_events**: append-only `job_id`, `ts`, `worker_id`, `event`, `detail`.
- **models**: `id`, `lineage_id`, `family`, `params`, `params_hash`, `artifact`, `parent_model_id`, `trained_through`, `summary`, `status` (candidate,paper_ok,live_eligible,retired; held per lineage), `backtest_metrics`, (step 6A) `validation_metrics`, `stress_metrics`, (step 6B) `snapshot_metrics` (the snapshot replay result, never read by eligibility), `created_by_job_id`. UNIQUE `(family, params_hash, trained_through)`.
- **injuries** (step 6B): nflverse injury reports, PK `(season, game_type, week, team, gsis_id)`, with `full_name`, `position`, `report_status`, `date_modified`. **team_game_stats** (step 6B): per team-game play-by-play aggregates, PK `(game_id, team)`, with `season`, `week`, `kickoff_at`, `off_epa_per_play`, `def_epa_per_play`, `pass_rate`, `plays`, `success_rate`.
- **games**: nflverse `id`, `season`, `week`, teams, `kickoff_at`, `status`, scores, `closing_home_p`, `spread_line`, `temp`, `wind`, `roof`.
- **markets**: `id`, `game_id`, `platform`, `market_ref`, `side`, `mapping_confirmed` (default false), `liquidity_usd_cents`, `best_bid`, `best_ask`, `closing_price`, `status`, `tick`, `min_size`. `closing_price` = mid of the last snapshot strictly before kickoff (last trade if no book), frozen at kickoff.
- **price_snapshots**: one row per book fetch, partitioned by day: `market_id`, `ts`, `bid`, `ask`, `mid`, `last_trade`, depth (<= 10 levels within 5 cents of touch), `liquidity_usd_cents`. Cadence 2 s for markets with an active assignment, 30 s for other current-week markets (about 0.5M rows a day); raw partitions dropped after `snapshot_retention_days`; a nightly job downsamples into **price_bars** (minute OHLC, kept forever). `closing_price` and CLV are frozen first.
- **assignments**: `job_id`, `game_id`, `model_id`, `mode` (paper,live), `bankroll_id`, `max_bet_cents NULL` (may only lower the global), `status` (active,halted,settled). One live model per game via a partial unique index `ON assignments(game_id) WHERE mode='live' AND status IN ('active','halted')`; paper up to `paper_models_per_game` per game, counted under `FOR UPDATE` on the games row, plus a unique `(game_id, model_id)` partial index for paper.
- **bankrolls** and **ledger**: ledger is append-only (trigger forbids UPDATE/DELETE) with kinds fund, reserve, release, fill, (step 6B) sell, settle, adjust (fees are inside the fill and sell rows); a nightly replay asserts `initial + realized_pnl = available + reserved + open_cost`.
- **orders** (current state only; `id` is the client order id; step 6B `side` buy or sell), **order_events** (append-only, trigger on every status change; fleet-exchange adds exchange responses), **fills** (step 6B `basis_cents`: the basis a buy adds or a sell removes), **bets** (skill columns plus `order_id`, `model_id`, `game_id`, `mode`; `clv = closing_price - entry_price`, NULL for in-play entries and for sells; step 6B `order_side`, and `result` gains `sold`), **model_scores** (PK `(model_id, game_id, mode)`).
- **settings**: `key PK`, `value jsonb`. Seeds (test defaults, all editable on the dashboard): `live_enabled=false`, `kill_switch=false`, `tz`, `lease_seconds=30`, `heartbeat_seconds=5`, `online_after_seconds=15`, `max_expiries=3`, `liquidity_floor_cents=50000`, `max_bet_cents=2500`, `max_daily_loss_cents={live:30000, paper:100000}`, `default_bankroll_cents=10000`, `min_edge=0.03`, `kelly_fraction=0.25`, `trade_max_games=6`; later steps add `max_exposure_cents` (null = off), `paper_models_per_game=3`, `participation=0.5`, `book_max_age_s=60`, `orphan_cancel_after_s=30`, `gtd_seconds=900`, `snapshot_retention_days=14`, `fee_model`, `rate_limits`, eligibility thresholds.
- **exchange_state** (fleet-exchange only): heartbeat, `auth_ok`, balance, buying power, clock skew. **audit_log**: `ts`, `actor` (Tailscale login), `ip`, `action`, `entity`, `before`, `after`, `confirmation_text`. **schema_migrations**.

## Host API

Worker routes, enrollment, downloads and the step 1 owner routes are defined in `docs/PROTOCOL.md` and are not repeated here. Summary of what exists beyond step 1:

| Method | Path | Purpose | Caller |
|---|---|---|---|
| GET/POST | `/api/data/games`, `/api/models/{id}`, `/api/models` | nflverse cache (ETag; from step 6B with per-game signals and team stats); artifact JSON; upload artifact+metrics+summary as candidate | agent |
| GET | `/api/v1/data/prices?since=&platform=` | step 6B: recorded bars and depth per confirmed market for snapshot replay (ETag) | agent |
| GET | `/api/trade/state` | my assignments, books above the floor, features, bankrolls, positions, open orders, kill | trade worker |
| POST | `/api/trade/release` | `{lease_token}`: cancel this worker's open orders, wait up to 3 s, return `{cancelled, pending}` | trade worker, role change |
| POST | `/api/orders/request`, `/api/orders/{id}/cancel` | order proposal, a buy or (step 6B) a sell (`order_side`), approved or rejected with reason; cancel request | trade worker |
| GET | `/`, `/jobs`, `/jobs/{id}`, `/models`, `/models/{id}`, `/trading`, `/settings` | dashboard pages (JSON for refresh via `/api/fleet`) | owner |
| POST | `/trading/assignments`, `/trading/markets/{id}/link` | assignment + trade job; manual market-to-game link | owner |
| POST | `/kill`, `/kill/reset`, `/live`, `/live/off` | one-tap kill; reset `confirm=RESUME`; live `confirm="ENABLE LIVE TRADING YYYY-MM-DD"` | owner, CLI |
| POST | `/settings` | edit every limit, floor, threshold, tz | owner |

**Owner auth**: the app binds `127.0.0.1`; the only way in is `tailscale serve`, which injects `Tailscale-User-Login`. The request must carry that header equal to `FLEET_OWNER_LOGIN` (or to one of the logins in its comma separated list), else 403. There is no `tailscale whois` call. **CSRF**: a state-changing owner request that carries `Origin` must match `FLEET_ALLOWED_ORIGINS` (default `FLEET_PUBLIC_URL`), else 403; requests without `Origin` (CLI) pass. `FLEET_DEV=1` skips the header check for local tests only. Worker routes need the per-worker bearer token bound to the path id.

## Job queue semantics

Claim, lease, renewal, reaper, dispatcher, any-idle pick, chosen-worker flip, auto-return to idle, checkpoint, cancel and idempotency are exactly as in `docs/PROTOCOL.md` (steps 1 to 3). Later steps add:

- Trade role: claims up to its free slots of `trade_max_games`; trade jobs stay leased until settlement and never fail by expiry (`max_expiries NULL`); a crashed worker's job expires in `lease_seconds` and any trade worker reclaims it; queued more than 60 s with an active assignment shows an "N assignments unattended" banner.
- A chosen worker in `trade` needs `confirm_trade=1` ("this will cancel its open orders"), then runs the trade drain.

## Worker agent

Behaviour, states, loop, drain, self-update, enrollment and the runner contract are in `docs/PROTOCOL.md`. Design notes beyond it:

- Runs under systemd on Debian 13 as system user `fleet`; unit and installer below.
- **Unit size**: every unit of work must finish inside the 3 s drain budget in pure Python: backtest = one season (or week batch) of one model; model_search = one candidate x one season; train = one fit chunk. Worst case on a kill: one unit repeated.
- **RSS watchdog** (step 2): RSS over 80% of RAM: SIGTERM, SIGKILL after 3 s, release with the last checkpoint, `expiries + 1`.
- **Role-change handshake** (step 2 owner test): worst case 5.0 s poll + 3.0 s drain + about 1 s for two requests = 9 s plus 2x RTT. Trade drain (step 4): stop proposing, `POST /api/trade/release`, then release trade jobs; slow live cancels finish asynchronously. From the click, approval rejects a worker whose `desired_role` is not `trade` or a job with `preempt_requested`/`cancel_requested`.
- Crash recovery: `Restart=always`; register rotates tokens (a zombie is locked out) and re-adopts held jobs from checkpoints.

## Trading flow

Full specification: `docs/TRADING.md`. The host is two processes in compose: `fleet-host` (service `host`: APIs, approval, kill switch, dashboard) and `fleet-exchange` (service `exchange`: snapshots, executor, paper fills, settlement, heartbeat). They share only the database; `exchange.env` (optional until step 5) is loaded by the exchange service alone.

1. Owner creates an assignment (game, model, mode=paper default, bankroll): ledger fund + trade job. Live needs `live_enabled` and a `live_eligible` lineage.
2. fleet-exchange polls books for mapped markets into `price_snapshots`; matches markets to games by team+date; unconfirmed mappings are not tradeable until linked on `/trading`.
3. Trade tick (5 s per assignment): read `/api/trade/state`; `my_p = model.predict(game, market_p, features)`; per side `edge = my_p - ask - fee`; if `edge >= min_edge` and no open same-side order, stake `kelly_fraction x available x edge/(1-ask)`; cancel own resting orders whose edge went negative; `POST /api/orders/request` with a deterministic `client_request_id`. From step 6B, per market held, `sell_edge = bid - fee - p_side`; at `>= min_edge` a limit sell at the bid, never more than the position (`docs/TRADING.md`, "Selling").
4. **Approval** (`host/trading/limits.approve_order`, one transaction, advisory lock per mode, bankroll `FOR UPDATE`), in order: idempotent replay, kill switch, lease fence, assignment/market/mapping/cutoff checks, mode gate (live: `live_enabled`, lineage `live_eligible`, `auth_ok`), stale book, liquidity floor (cited and newest snapshot), participation, price band, max bet (including open same-side cost, assignment may only lower), per-game bankroll, daily loss, optional exposure cap, live balance (buying power if reported, else cash minus reserved; a cash balance already reflects fills). Pass: ledger reserve + order `approved`; fail: order `rejected` with reason. Limit fields in the payload are ignored. A sell (step 6B, `host/trading/sells.py`) skips the money checks and reserves nothing but must fit the held position, one open sell per market.
5. **Executor outbox** (fleet-exchange, 250 ms): `approved` to `submitting` (commit), rate token (live only), `adapter.place(client_order_id, gtd)`, then `open`. A timeout stays `submitting` and is reconciled by client id, never resubmitted blind. A4 fallback: single-flight live submission with an open-order diff; ambiguous result triggers auto-kill.
6. **Fills**: live polled; paper simulated in `host/exchange/paper.py` (with `fleet/sim/book.py`) against later snapshots, walking ask levels up to the price for a buy and (step 6B) bid levels down to the price for a sell, taking at most `participation` of each level; resting orders fill at their limit only when a later snapshot crosses.
7. **Settlement**: ESPN final (nflverse confirms next day): ledger settle, `bets` rows (from step 6B a sold row per sell and buy rows for the contracts still held), `model_scores`, assignment settled, lineage eligibility recomputed.

## Limits, kill switch, live switch, eligibility, rate limiting, liquidity floor

Specified exactly in `docs/TRADING.md` (approval order, kill transaction, settlement, eligibility).

- **Three limits**, all host-side and editable on the settings page: per-game bankroll, max bet, and max daily loss per mode (realized + unrealized P&L for the owner's day in `settings.tz`). A live breach halts live (`live_enabled=false`, live assignments halted, live orders cancelled) while paper keeps running. Optional exposure cap (null = off).
- **Kill switch** (`host/kill.py`, `host.cli kill`): one atomic transaction under the per-mode locks: `kill_switch=true`, `live_enabled=false`, `approved` orders to `cancelled` (reserve released), `submitting/open/partial` to `cancel_requested`, assignments halted, audit. fleet-exchange cancels every `cancel_requested` row with retry and reconciles until zero. Heartbeats carry `kill=true`. Reset needs `confirm=RESUME` and clears the flag only; assignments stay halted, live stays off, filled positions are not flattened. Auto-kill: 3 auth failures, unknown live order or fill, clock skew over 30 s (placements paused, cancels keep going), ambiguous A4 reconciliation, a fill for an order already closed.
- **Exchange watchdog**: red "EXCHANGE DOWN, N cancels pending" banner when the exchange heartbeat is older than 15 s while orders are in flight or the kill is engaged. Compose restarts the container; the manual fallback is `docker compose stop exchange && docker compose run --rm exchange python -m host.exchange.cli cancel-all --direct` (press KILL first; the exchange container is the one holding the keys, the host container has none); every live order is GTD (`gtd_seconds`, default 900), so it expires at the exchange even if the host is dead.
- **Live switch**: `live_enabled=false` at migration; `/live` needs the dated typed phrase, `auth_ok` within 10 min, kill off; `/live/off` immediate; every order re-checks it. `docker compose exec exchange python -m host.exchange.cli exchange-smoke` places one 1-share order well below the bid through the normal paths, then cancels it.
- **Eligibility** (per lineage, recomputed on every backtest, validation and settlement and on a thresholds change; thresholds editable; `host/eligibility.py`, `host/paper_gate.py`, `docs/ROBUSTNESS.md` A4): candidate to paper_ok when the root's held-out validation era (`require_validation`, on by default) has >= 50 bets, ROI >= 2%, drawdown <= 30%, a bootstrap ROI 5th percentile >= 0 (`min_roi_ci_low`), a market-test p <= 0.10 (`max_market_p`) and none of the forbidden flags (`overfit`, `fragile`); a lineage without validation metrics stays a candidate (with `require_validation` off the three base rules judge the search era instead); paper_ok to live_eligible at paper >= 10 games, >= 40 bets, >= 21 days, stake-weighted CLV >= 0, PnL > 0, and (`clv_ci_excludes_zero`) a paper CLV bootstrap 5th percentile above 0 over at least `min_bets` bets with a CLV; drops one step on failure. **Only `live_eligible` lineages ever receive live orders**: no override, no typed bypass.
- **Rate limiting**: fleet-exchange is the sole exchange client, so its token buckets are fleet-wide; paper orders take 0 tokens; cancels first; a 429 halves refill for 60 s.
- **Liquidity floor** `liquidity_floor_cents`: `/api/trade/state` omits markets below it; approval rejects on cited and newest snapshot. **Orphan rule**: no heartbeat for `orphan_cancel_after_s` and the host cancels that worker's open orders.
- **Keys**: Polymarket US keys live only in the fleet-exchange container (env file `exchange.env`, not mounted into fleet-host; own Postgres role). Ed25519 signing via `pynacl`; the exchange refuses to sign when its clock skew check fails.

## Models, backtester, scoring and leaderboard

The step 3 scope (data, model interface, `elo_blend`, backtest rule and metrics, search, training, summaries, eligibility, leaderboard) is specified exactly in `docs/MODELS.md`; where it differs from the summary below, `docs/MODELS.md` wins. Step 3 signatures: `fit(games, through, should_stop)` and `summary(params, metrics)`.

- `Model` interface, one signature everywhere: `fit(games, stop)`, `predict(game, market_p, features) -> p_home`, `to_json/from_json`, `summary(metrics) -> 3 sentences`; `registry.py` maps family to class.
- First family `elo_blend` (pure Python): Elo with MOV multiplier, HFA, rest, season regression; logit blend with the market fitted by gradient descent; search space K, HFA, regress, MOV, a, b, min_edge, kelly, and from step 6B a quarterback-change and a per-player-Out penalty. Second family (step 6B) `epa_blend`: a logistic regression on Elo, shrunk rolling EPA per play, rest, quarterback change, players Out, divisional games and the market logit, fitted by Newton with L2. No existing model code was provided; any later model plugs in behind `Model`.
- **Job kinds**: *backtest* `{model_id|params, seasons}`; *model_search* `{family, seed, n, seasons}` yielding top-5 new `models`; *train* `{model_id, through}` yielding a child row in the same lineage.
- Backtester: walk-forward by season, two labelled price regimes: (a) nflverse closing line with the closing-line fill rule (CLV is 0 by construction); (b) from step 6B (`price_source` "snapshots") own `price_snapshots`/`price_bars`, served by `GET /api/v1/data/prices`: the bet is placed at the recorded ask a set time before kickoff and filled on the recorded book with the paper participation rule (`fleet/sim/prices.py`, `docs/ROBUSTNESS.md` B1). Per-game bankroll reset; checkpoint per season.
- Scoring per `(model, game, mode)`: `n_bets`, `stake`, `pnl` (fees in), stake-weighted `avg_clv`; in-play excluded from CLV. **Leaderboard rolls up by `lineage_id`**; ranked only with `games >= 5 AND bets >= 30` in the rank mode; sort by shrunk CLV `clv x bets/(bets+25)`, tiebreak ROI. From step 6A the backtest ranking uses the held-out validation era (shrunk ROI then log-loss gain), with unvalidated models listed unranked; see `docs/ROBUSTNESS.md` A1. From step 6B a lineage with 30 or more snapshot replay bets ranks on shrunk snapshot CLV between the paper-ranked and the validation-ranked lineages (`docs/ROBUSTNESS.md` B1).

## Data sources

Free only for now. Paid sources are considered only after the system shows profit.

| Source | Use | Cost |
|---|---|---|
| nflverse `games.csv` + schedules (CC-BY-4.0, attribution shown) | schedule, scores, closing spread/ML, temp/wind/roof, starting quarterbacks; host-cached | free |
| nflverse `injuries_{season}.csv` (step 6B) | players listed Out per team and week, filtered by report time; host table `injuries`, refreshed every `signals_refresh_hours` | free |
| nflverse `play_by_play_{season}.csv.gz` (CC BY 4.0) | step 6B: per team-game EPA per play, pass rate, success rate in `team_game_stats`, streamed, never held in memory; step 6C: one row per play in `pbp_rows`, the `ingame_wp` training data with nflverse's `vegas_wp` as the baseline | free |
| ESPN scoreboard JSON (unofficial) | live finals, polled every 60 s while an assigned game is in play (from step 6C through the in-game feed's request window and backoff); nflverse fallback next day; also the fallback state of a game whose summary cannot be parsed | free |
| ESPN summary JSON (unofficial, step 6C) | live game state (score, clock, possession, down and distance, plays) of assigned games in play, every `gamestate_poll_s` (4 s) per game, at most `gamestate_max_rps` (1.0) ESPN requests per second, jittered backoff on 429 or 403 (`docs/INGAME.md`) | free |
| Yahoo play-by-play (step 6C) | opt-in cross-check, not fetched until its parser is written from the owner's probe | free |
| Polymarket US gateway (public) | markets, books into own `price_snapshots` from day one; replayed by snapshot backtests (step 6B) | free (A1) |
| NWS `api.weather.gov` (public domain, User-Agent required) | forecasts for outdoor games, host-cached hourly | free |
| The Odds API, Kalshi historical, SportsDataIO/Sportradar, Open-Meteo, offshore CLOB history, NFL.com live feeds (rotating app tokens), broadcast capture | not used | deferred until profit (broadcasts never) |

## Dashboard

The step 2 dashboard (pages, fragments, forms, auth, empty states) is specified exactly in `docs/DASHBOARD.md`; the list below is the full target across steps. Trading pages arrive in step 4. The kill switch flag and reset are live from step 2 and gate only the trade role; its cancel-all of open orders arrives in step 4.

- Top bar: PAPER/LIVE pill, KILLED, "EXCHANGE DOWN" and "N assignments unattended" banners, today/all-time PnL per mode, red KILL button (one confirm).
- `/` Fleet: card per worker with online dot, role `<select>` (submits on change, shows "switching (E+1/E)", red after 20 s), current job and progress, CPU/RAM/skew.
- `/jobs`: create buttons with target (any idle | chosen worker, with the trade confirmation) and params; queue table with Cancel/Requeue. `/jobs/{id}`: params, progress, checkpoint, result, error, `job_events` timeline.
- `/models`: ranked by lineage (backtest | paper | live), status badge, three-sentence summary, Assign, Train.
- `/trading`: assignments, open orders, last 50 orders with reject reasons, fills, snapshot ages, unmatched markets, ledger replay status, exchange heartbeat. `/settings`: every limit number, floor, thresholds, tz, fee model, retention, enroll token, live typed form, kill reset.
- Phone: single column under 700 px, 44 px targets, 10 s JSON refresh.
- Access: containers publish only on `127.0.0.1`; `tailscale serve --https=443` fronts them. `tailscale funnel status` must show nothing; the owner checks it after setup (see README). On a Debian host the same compose file applies, with `tailscale serve` run there.

## Install script and systemd

- Worker: `curl -fsSL https://<host>/install.sh | sudo bash -s -- https://<host> <token> [--name box1]`. Needs only root, `python3` >= 3.11 and what Debian 13 ships (curl, tar, systemd). Creates system user `fleet`, downloads the tarball from the host mirror with sha256 check, enrolls (single-use token, 1 h), writes `/var/lib/fleet/worker.conf` mode 0600, `systemctl enable --now fleet-worker`. Idempotent: re-run upgrades and keeps identity.
- `fleet-worker.service`: `After=network-online.target`, `StartLimitIntervalSec=0`; `User=fleet`, `Restart=always`, `RestartSec=3`, `RestartPreventExitStatus=78`, `TimeoutStopSec=15`, `MemoryMax=85%`, `Nice=5`, `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ReadWritePaths=/var/lib/fleet`, `PrivateTmp=yes`.
- Host: no host installer. `docker compose up -d` starts `db`, `host` and `exchange` (`fleet-host` and `fleet-exchange`), all `restart: unless-stopped`, Postgres data in a named volume, ports published on `127.0.0.1` only. Migrations apply at container start. Backups: nightly `pg_dump` via a scheduled task or cron on the host OS.

## Tests

`pytest` against a real Postgres (`FLEET_TEST_DATABASE_URL`; CI uses a service container); a database per test; FastAPI `TestClient`; FakeAdapter can fill/reject/timeout/delay-cancels.

- **test_queue.py** (step 1): no double claim, targeted before untargeted, release does not increment expiries, pairwise lease renewal, reaper requeues and keeps checkpoint then fails at max, stale token refused, lost returned for rotated token, any-idle sets role/target/auto_role, chosen worker flips role and preempts, auto-return to idle, concurrent any-idle never picks the same worker, dispatcher assigns a waiting job, idempotency key.
- **test_role_change.py** (step 2): fake-clock agent; ack within 9 s + 2x RTT; zero extra round trips on drain; SIGTERM ignored then SIGKILL at 3 s with job requeued from checkpoint; RSS watchdog; immediate ack after drain; double role change acks the latest epoch; self-update re-adopts leases.
- **test_auth.py**: owner header match, Origin check, missing header 403, `FLEET_DEV` bypass.
- **test_limits.py**, **test_kill_switch.py** (step 4): every approval gate, concurrency on bankroll and daily loss, kill scoping and races, executor never submits while killed, reset needs exact `RESUME`, every status change writes an `order_events` row.
- **test_executor.py**, **test_paper_fills.py**, **test_models.py**, **test_scoring.py**, **test_live_switch.py**.
- **Hardware**: `tests/hw/roletest.sh <worker>` measures click-to-ack and fails over 10 s; `tests/hw/netcheck.sh` confirms the host port is unreachable from the LAN and public IP.

## Build order

**Gate: after each step the owner runs the test and nothing from the next step starts until they say OK.**

| Step | Deliverables | Owner test |
|---|---|---|
| 1 | Migrations, `host/db.py`, FastAPI app + bearer auth, register/heartbeat/claim/checkpoint/complete/fail, reaper, dispatcher, lease tokens, `/dl` mirror, agent (idle + `sleep` job), worker installer and unit, compose stack, `host.cli`, owner header auth + Origin check, test_queue | Host + 2 boxes installed; `/api/fleet` shows CPU/RAM every 5 s; `kill -9` agent and it returns with the same id; sleep job survives a crash; role set to idle mid-job and the job resumes elsewhere from its checkpoint |
| 2 (done) | Delivered: fleet cards, role dropdown + epoch handshake, child-process drain + immediate ack, agent memory (RSS) watchdog, release reasons (`drain`, `preempt`, `cancel`, `oom`, `shutdown`), top bar, settings page with editable limits, enroll token page, audit log, kill flag + reset (flag only; cancel-all arrives in step 4 with the exchange), `roletest` (`host.cli roletest`, wrapper `tests/hw/roletest.sh`) | On phone: change role and see the new role within 10 s; `roletest.sh` passes; non-owner tailnet device gets 401; KILL turns the bar red, RESUME clears |
| 3 (done) | Delivered: nflverse ingest (`host.cli ingest-games`, Settings Refresh, `GET /api/v1/data/games`), `fleet/models` (`Model` interface, registry, `elo_blend`), `fleet/sim` (walk-forward backtest, model search, train) with sub-3 s checkpointed units, jobs page and `/jobs/{id}`, Models page with per-season table and Train, leaderboard by lineage on the root model's backtest, eligibility candidate to paper_ok, three-sentence summaries; spec in `docs/MODELS.md` | Model search on any idle: box switches, 5 models with summaries appear, box returns to idle; role switch mid-search resumes elsewhere repeating at most one candidate-season; Train gives a child row in the same lineage |
| 4 (done) | `fleet-exchange` container (heartbeat, watchdog), adapter interface, snapshots + partitions/bars, matching UI, approval with all three limits, ledger, executor outbox, `order_events`, `/api/trade/release`, paper simulator, trade tick, `/trading`, settlement/scoring, paper eligibility, kill end to end, orphan rule | Three paper models on one game: approvals/rejections with reasons, realistic fills; tiny paper daily loss gives `daily_loss`; KILL cancels all paper orders in under 2 s; stopping fleet-exchange with an open order shows the banner within 15 s; next day: bets rows, lineage paper line, ledger replay OK |
| 5 (done) | Delivered, spec in `docs/LIVE.md`: `polymarket_us.py` (Ed25519, place/cancel/open/fills/balance), restart reconciliation, rate buckets + 429 backoff, live switch, live halt, buying-power check, GTD, auto-kill triggers, `exchange-smoke`, `cancel-all --direct` | Keys in `exchange.env` give "auth OK"; typed dated phrase gives LIVE; `exchange-smoke` order visible in the exchange UI; KILL cancels it and live flips off; restart mid-order gives no duplicate. Steps 1 to 5 are complete |
| 6A (done) | Delivered, spec in `docs/ROBUSTNESS.md` Part A: held-out validation era (`validation_seasons`; the search era keeps the `backtest_seasons` key), `validate` job, bootstrap intervals and market test, stress tests and flags, stricter gates, multi-core search (`search_workers`) | Settings shows validation seasons and search workers; a search fills validation columns on Models; Validate on an older model; Robustness section on the model page; a search on 4 cores runs about 3x faster |
| 6B (done) | Delivered, spec in `docs/ROBUSTNESS.md` Part B and `docs/TRADING.md` "Selling": migrations `0007_signals.sql` and `0008_sells.sql`; `GET /api/v1/data/prices` and snapshot replay backtests (`price_source`, `snapshot_metrics`, snapshot leaderboard column and rank mode); quarterback-change and injury signals and per team-game EPA in the games feed (`ingest-injuries`, `ingest-pbp`, periodic refresh); `elo_blend` penalties; the `epa_blend` family; selling a held position end to end (worker rule, `approve_sell`, paper sell fills, ledger `sell`, settlement after partial sales, kill, live `side_sell`, Trading page positions and sell chips) | Signals loaded by the CLI; an `epa_blend` search fills Models; a snapshot backtest (sim prices allowed for the test, then turned off) shows the replay line and the snapshot column; a paper assignment shows positions and, when the bid overshoots the model, a sell with its realized P&L (README "How to test step 6B") |
| 6C (done) | Delivered, spec in `docs/INGAME.md`: the ESPN game-state feed (`gamestate` exchange task, `game_state`, rate cap and backoff, `probe-gamestate`), feed-lag measurement and buy suspension (`feed_lag`), `pbp_rows` (`ingest-pbp-rows`, weekly refresh, `GET /api/v1/data/pbp`), the `ingame_wp` family and its search validated against `vegas_wp`, in-game assignments (`ingame_model_id`, `trade_ingame`), worker in-game rules, in-game approval checks (paper only), in-game settlement and scoring, the in-game dashboard parts | The in-game toggle on a paper assignment; during a game the Trading page shows score, clock, state age and the model probability, in-game orders and the In-game feed block; `probe-gamestate` output pasted back; `ingest-pbp-rows --season 2012-2025` and an `ingame_wp` search on Models. Step 7 (UI overhaul) is pending |
| 7 (pending) | Spec in `docs/UI.md`: the same dashboard, shorter and easier to read; no API, data model or rule changes | Not started |

## Risks

- Polymarket US API unverified (A1 to A5); the live adapter is built from assumptions kept in `market_source_config` and may need rework from the owner's probe output; the paper fee model may be wrong until then.
- Paper fills are optimistic; expect live below paper. Closing-line backtest CLV is 0 by construction; snapshot replays (step 6B) measure CLV only on games the exchange recorded, so they start small.
- Single host is a single point of failure, and a Windows desktop can sleep, update or reboot: with the host down, resting live orders depend on GTD expiry and the kill is unavailable. Disable sleep, keep Docker Desktop starting at login, back up the Postgres volume.
- Docker Desktop port publishing and `tailscale serve` must both be healthy for workers to reach the host; the dashboard should show stale workers promptly.
- ESPN is unofficial; settlement may lag a day. Pure-Python models cap complexity.
- The in-game feed lags the field (15 to 60 s) and the market moves first, so in-game buys are adversely selected; in-game orders are paper-only in step 6C, buying pauses while the measured feed lag is too high, and the ESPN payloads are unverified until the owner's `probe-gamestate` output is checked.

## Decisions

Owner answers that shaped this design:

1. Existing model-search code: none to import (the question was unclear to the owner); build everything new behind `Model`. A new repo was created for it.
2. Polymarket US API keys: not yet held; the owner will obtain them. API docs: https://docs.polymarket.com/getting-started/api
3. Host: Windows 11, Docker Desktop. Workers: Debian 13. Timezone: `America/New_York`.
4. Limits: the quoted numbers are test values; real values are adjustable on the dashboard settings page.
5. Eligibility, kill and other seeds: defaults as written above (no override, kill cancels open orders only).
6. Data: free sources only; paid sources are added after profit.
