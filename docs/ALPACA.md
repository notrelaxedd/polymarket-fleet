# Alpaca (steps 8 and 9)

Alpaca becomes a second trading venue for the fleet. This file is the plan and the
contract for that work. Only the probe (step 8.0) is built so far; everything after it
is the plan, built in order with a stop after each part for the owner to test, like
steps 1 to 7. Where this file and `docs/TRADING.md` or `docs/LIVE.md` differ, those
files win until the part in question is built.

## Owner decisions

- Both tracks: NFL event contracts on Alpaca (step 8) and stocks and crypto (step 9).
- Side by side: Polymarket US and Alpaca run at the same time, not one or the other.
- Free data first: stocks use Alpaca's free IEX feed. The full consolidated feed (SIP,
  paid, about $99 a month) is a later owner decision, behind a `feed` setting, so the
  upgrade is a settings change and not a rebuild.
- Every existing rule stays: paper by default, host-enforced limits, the typed live
  switch, `live_eligible` lineages only (no override), kill cancels open orders only,
  keys only in `exchange.env`, `fleet/` standard-library only.

## Why two steps

Everything the fleet does today assumes a binary NFL contract: a price in (0, 1), whole
contract sizes, one assignment per game, settlement at the final score, CLV against the
closing price. Kalshi event contracts offered through Alpaca (Alpaca registered as an
FCM in August 2026 and partnered with Kalshi; availability depends on the account type,
Kalshi's listings and geography) are the same kind of contract, so step 8 reuses the
models, limits, eligibility and settlement. Stocks and crypto are a different domain
(continuous prices, fractional sizes, no final score, market hours, the pattern day
trader rule), so step 9 is a separate track inside the fleet.

## Credentials

`exchange.env` on the host, read only by the `exchange` compose service, never by `host`:

```
ALPACA_API_KEY_ID=<key id>
ALPACA_API_SECRET_KEY=<secret>
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

`host/exchange/alpaca_credentials.py: load() -> AlpacaCredentials | None` reads them.
`ALPACA_BASE_URL` is optional (default paper) and must be https on
`paper-api.alpaca.markets` (paper) or `api.alpaca.markets` (live); a trailing `/v2` is
accepted and dropped; any other host, a port, a path, a query or credentials in the URL
are refused with `AlpacaConfigError`, whose message never carries a key. Paper and live
keys are different pairs; live keys only work on the live host. Market data always goes
to `data.alpaca.markets` with the same headers (`APCA-API-KEY-ID`, `APCA-API-SECRET-KEY`).
The key and the secret are never logged, stored or printed: `repr()` shows the last 4
characters of the key id, and `redact()` replaces both in any text before it is printed.

## The probe (step 8.0, built)

```bash
docker compose run --rm --build exchange python -m host.exchange.cli probe-alpaca
```

Read-only: every request is a GET, nothing is ordered, written or stored, and no
database is needed. It prints one JSON document, safe to paste back (the key and the
secret are redacted, the account's id and number are left out):

- `verdict.keys`: `ok (paper account, key ***XXXX)`, or `rejected (401/403)` with what
  to check, or the transport error.
- `verdict.event_contracts`: where Kalshi event contracts were found, or "not found".
- `account`: status, cash, buying power, pattern day trader flag, crypto and options
  status, and the names (not values) of every account field, so a new event-contract
  flag shows up.
- `clock`: market open or closed, and `clock_skew_ms` from the `Date` header.
- `stock_asset` (AAPL), `crypto_assets` (count and sample), `options_contracts` (SPY sample).
- `event_contract_guesses`: `/v2/assets?asset_class=<name>` for `event_contract`,
  `prediction_market`, `event` and `kalshi`, because Alpaca's name for the asset class
  is not verified yet. Records whose `class` is `us_equity`, `crypto` or `us_option` are
  never counted, so an endpoint that ignores an unknown class and answers with stocks
  is not reported as event contracts.
- `extra`: any `--get PATH` (`/v2/...` on the trading host, `data:/v.../...` on the
  market data host; a full URL, a host or `..` is refused). Shown for reading only: a
  path can answer with anything, so these never change `verdict.event_contracts`.
- `market_data`: one AAPL quote from the free IEX feed and one BTC/USD quote.

Options: `--asset-class NAME` (repeatable) tries another class name, `--get PATH`
(repeatable) another GET path, `--raw` prints up to 64 KiB of each answer instead of 2 KiB.
Exit 0 when the account call answers 200, else 1 (also without keys or with a bad
`ALPACA_BASE_URL`).

## Step 8: NFL event contracts on Alpaca (plan)

- **8.1 Read-only market source.** `host/exchange/adapters/alpaca.py` implements
  `MarketSource`: `list_markets` finds NFL moneyline contracts and maps them to games
  through `teams.py` and `mapping.py` (unconfirmed until the owner links them, as today);
  `fetch_book` converts prices to (0, 1). `market_source_config.alpaca` holds every path
  and field name, configurable because the shapes come from the probe. A per-platform
  `fee_model` (Kalshi's fee formula differs from Polymarket's).
- **8.2 Side by side.** Markets snapshotted on both platforms at once. Each game can have
  a market on each; approval picks the better price after fees per order. Needs: a set of
  active sources instead of the single `market_source`, `live_platform` per market,
  per-platform and combined exposure limits, buying power per platform. The one live model
  per game rule stays (one model, either venue).
- **8.3 Alpaca paper account as a gate.** Approved paper orders can be sent to Alpaca's
  paper API, which exercises placing, cancelling, fills and reconciliation through the
  real API with no money. Polymarket US has no such environment.
- **8.4 Live gateway.** `adapters/alpaca_live.py` implements `OrderGateway` with the
  key headers (no signing), `client_order_id` for reconciliation, open orders, fills
  (account activities), balance and buying power from `/v2/account`. GTD is not an Alpaca
  time in force; the executor's existing expiry path (cancel at `gtd_at`) covers it. The
  live gates, auto-kill, `exchange-smoke` and `cancel-all --direct` get Alpaca paths.
- **8.5 Real time.** The `trade_updates` WebSocket in the exchange process delivers fills
  in well under a second; polling stays as the backstop and the audit. One dependency in
  the exchange container only.
- **8.6 Tests and docs.** A fake Alpaca gateway, limit, kill switch and auto-kill tests on
  the Alpaca path, an e2e from proposal to settlement, README walkthrough.

## Step 9: stocks and crypto (plan)

- Data: an `instruments` table and minute bars from the Alpaca data API, IEX feed for
  stocks (backtests also on IEX history, so models train and trade on the same feed),
  crypto from Alpaca's crypto feed. `feed` setting `iex` or `sip`.
- Models: a time-series model interface beside the game-based `Model`; hourly or daily
  horizons first.
- Trading unit: an instrument plus a session instead of a game; fractional sizes;
  positions marked to market daily instead of settled at a final.
- Limits: notional per position, gross exposure, market hours, a pattern day trader guard
  (accounts under $25,000: at most 3 day trades in 5 trading days; crypto is exempt),
  shorting off by default.
- Gates: walk-forward backtest, then paper on the Alpaca paper account, then
  `live_eligible`, like NFL lineages.

## Assumptions to verify

| # | Assumption | Verify |
|---|---|---|
| L1 | Individual Trading API accounts can trade Kalshi event contracts (announcements name Alpaca's broker partners) | Owner asks Alpaca support; probe |
| L2 | Event contracts appear as an asset class on `/v2/assets`; its name is unknown | Probe |
| L3 | The paper environment supports event contracts | Probe on paper keys |
| L4 | Free plan: IEX real-time stocks, full-market history older than about 15 minutes, about 30 streaming symbols, about 200 requests a minute | Alpaca docs, owner |
| L5 | Kalshi fee formula and tick size for NFL contracts | Alpaca docs, probe |
