# Alpaca (steps 8 and 9)

Alpaca is a second trading venue for the fleet, beside Polymarket US. The probe (step
8.0) and step 9, stocks on Alpaca (parts 9.1 to 9.5), are built. Step 8, NFL event
contracts on Alpaca, is planned and on hold. Where this file and `docs/TRADING.md` or
`docs/LIVE.md` differ for the NFL side, those files win; for stocks this file is the
reference (the build contract was `tools/workflows/step9-contract.txt`).

## Owner decisions

- Stocks only for now: Alpaca trades US stocks and ETFs, long only. No crypto, no
  options, no event contracts. Step 8 (NFL event contracts on Alpaca) stays planned and
  on hold until the owner says otherwise.
- Side by side: Polymarket US (NFL) and Alpaca (stocks) run at the same time, in the
  same exchange process, each with its own tables, limits and pages.
- Free data: daily bars from Alpaca's free market data plan (the consolidated SIP
  history, which the free plan serves when the request ends at least 15 minutes back;
  `stock_history_feed` can be set to `iex`). No intraday data is used.
- Every existing rule stays: paper first, the host approves every order against limits,
  live needs the typed live switch and a `live_eligible` model (no override), the kill
  cancels open orders only (positions stay), keys only in `exchange.env` read only by the
  exchange process, workers never see keys and never talk to Alpaca, `fleet/` is
  standard-library only.

## Why two steps

Everything the NFL side does assumes a binary contract: a price in (0, 1), one
assignment per game, settlement at the final score, CLV against the closing price.
Kalshi event contracts offered through Alpaca would be the same kind of contract, so
step 8 would reuse the NFL models, limits, eligibility and settlement. Stocks are a
different domain (continuous prices, no final score, market hours, daily marks instead
of settlement), so step 9 is a separate track with its own models, tables and pages.

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

## Step 8: NFL event contracts on Alpaca (plan, on hold)

Not built; kept as the plan for when the owner turns it back on.


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

## Step 9: stocks on Alpaca (built)

Stocks trade once a day, market on close, from daily bars only. A model's live decision
uses exactly the bars its backtest used for the same day (every bar dated before the
session), and its orders fill at the session's closing price, which is what the
backtest assumes. The tables are in migrations `0012_stocks.sql` and
`0013_stock_trading.sql`.

### A trading day (New York time, a normal session)

| When | What happens | Where |
|---|---|---|
| every 30 s | broker check: account (cash, equity, buying power) and clock (open, next close, next open) into `stock_broker_state`, then the reconciliation | exchange, `stock_broker` |
| 15:40 (close minus `stock_decision_lead_min`, 20) | the decision is due: each stock_trade worker builds its orders from the bars through the previous session and posts them once | worker, `fleet/worker/stock_trade.py` |
| at once | the host approves or rejects each order with a reason code; approved buys reserve cash | host, `host/stocks/approve.py` |
| within 1 s | the executor places each approved order at Alpaca: market, whole shares, time in force `cls` (sells first) | exchange, `stock_executor` |
| 15:49 | the host stops approving (`moc_cutoff`, 11 minutes before the close) | host |
| 15:50 | Alpaca stops taking or cancelling cls orders | Alpaca |
| 16:00 | the closing auction fills the orders; the executor's poll (every `stock_orders_poll_s`) books the fills | exchange |
| 16:30 | the daily mark: every assignment's equity at the session's close, then the eligibility recompute | exchange, `stock_marks` |
| after `stock_bars_hour` (18:00) | the bar feed fetches every symbol's history again, so tomorrow's decision has today's bar | exchange, `stock_bars` |

Half days follow Alpaca's clock and calendar (the decision is 20 minutes before the
early close, the mark 30 minutes after it). Weekends and holidays have no session.
The decision window runs from the close minus `stock_decision_lead_min` to 15:49 (the
`moc_cutoff`), and the worker looks once per `stock_trade_tick_s`, so the Stocks settings
group refuses a lead and tick whose window holds fewer than two ticks ((lead - 11) x 60 s
< 2 x tick): such a window would skip sessions without a word.

### 9.1 Daily bar feed (exchange)

`host/exchange/stock_bars.py` with the data client `host/exchange/alpaca_data.py`.
For every symbol of the `stock_symbols` setting (20 large US stocks and ETFs by
default, SPY always useful as the benchmark) it stores the instrument (tradable or not)
and its split- and dividend-adjusted daily bars since `stock_history_start` in
`instruments` and `stock_bars`. A new symbol is fetched at once; every symbol is fetched
again in full once a day after `stock_bars_hour` (settings `tz`), because a split or a
dividend changes the old adjusted bars. A failed symbol is retried after 30 minutes and
its error shows on `/stocks`. Without keys nothing runs. CLI: `ingest-stock-bars
[--symbol S]` and `stock-bars-status` (exchange CLI). Workers get the bars from the host
(`GET /api/v1/data/stock_bars`, ETag and 304 like the games feed) into
`<state>/cache/stock_bars.json` (`fleet/worker/stock_cache.py`).

### 9.2 Models, backtest and search (worker)

`fleet/stocks/` (standard library only). A model turns the bars before a day into
weights per symbol (long only, each at most 1, the sum at most 1, deterministic):

- `momentum`: the `top_k` names with the best `lookback`-day return skipping the last
  `skip` days, rebalanced every `rebalance_days`; all cash while SPY is below its
  `trend_sma`-day average (0 turns the filter off).
- `meanrev`: the `top_k` names that fell most over `lookback` days, held `hold_days`;
  optionally only names above their `long_sma`-day average.
- `trend`: equal weight among up to `max_names` names whose `fast` average is above
  their `slow` average.
- `buyhold`: one symbol (SPY), the benchmark; it can be assigned too.

The backtest (`fleet/stocks/backtest.py`) walks the trading days: it marks the book at
the day's close, asks the model with the bars dated before the day only, and trades at
that close with `stock_cost_bps` per side (5 by default) on the traded value, starting
from equity 1.0 with fractional shares. Metrics (`metrics.py`): CAGR, annual volatility,
Sharpe (no risk-free rate, 252 days), max drawdown, annual turnover, trades, exposure,
per-year rows and the same numbers for SPY bought and held. Lookahead is tested three
ways in `tests/test_stock_backtest.py`.

Jobs (`fleet/worker/stock_jobs.py`): `stock_search` (role model_search) samples `n`
candidates of the chosen families with a seed, backtests each on the search years
(`stock_backtest_years`) and returns the `top_k` by Sharpe with at least 10 trades, each
with a two-sentence summary; `stock_backtest` (role backtest) is informational;
`stock_validate` (role backtest) backtests a stored model on the held-out validation
years (`stock_validation_years`). All three checkpoint every model-year, so a role
change resumes where it stopped. The host fills in the symbols, the cost and the
resolved years, and refuses any search or backtest that reaches into the validation
era and any validate of a model searched on it.

### 9.3 Host: models, gates, assignments, approval

`host/stocks/`. A search result becomes `stock_models` rows (one per family and params;
a repeat keeps the existing row). Status (`eligibility.py`), recomputed after every
result, every thresholds save, every paper mark and at host start:

- `candidate` to `paper_ok`: backtest Sharpe at least `min_sharpe` (0.5), max drawdown
  at most `max_drawdown` (30%), at least `min_trades` (30), and a validation result with
  Sharpe at least `min_validation_sharpe` (0.0) (`thresholds_stock_backtest`).
- `paper_ok` to `live_eligible`: the model's paper assignments have at least `min_days`
  (20) marked sessions, a total paper return at least `min_return` (-2%) and a paper max
  drawdown at most `max_drawdown` (15%) (`thresholds_stock_paper`).
- A model that stops meeting a gate is demoted the same way; leaving `live_eligible`
  halts its live assignments. `retired` is the owner's, and final.

An assignment (`assignments.py`) is a model trading a list of symbols with its own cash
(the bankroll), in one mode. The mode is the broker environment: paper keys trade the
Alpaca paper account, live keys the real account, and the host refuses an assignment
(or an order) whose mode differs from the keys. One Alpaca account is shared, so a new
bankroll must fit in the broker's cash minus the other active bankrolls of that mode; at
most `stock_max_assignments` (3) exist at once; live also needs the live switch and a
`live_eligible` model. Creating one queues its `stock_trade` job (role trade, one trade
slot). Halt cancels its open orders (positions stay), Resume re-checks the gates, Close
needs no positions and no open orders.

Sell all (`host/stocks/liquidate.py`, owner `POST /api/stocks/assignments/{id}/liquidate`
and the row menu on `/stocks`) is the way out of an assignment that can never trade
again: its model was retired (final) or dropped out of `live_eligible`, so Resume is
refused, and Close is refused while it holds shares. On a halted assignment it stores
one approved market-on-close sell per held symbol (the shares held minus those already
in open sells; `client_request_id` `liquidate:<session_date>:<symbol>`, rationale "owner
liquidation", event actor `owner:<login>`, audit `stock_assignment_liquidate`). Each
sell runs every approval check except `halted`, `not_live_eligible` and `max_order` (a
whole position must be sellable in one order), so it is still refused under kill, with
live off, on a stale broker check, outside a session and after `moc_cutoff`; when one
sell fails, nothing is stored and the refusal names the check. The executor places the
sells like any other, the fills are booked, and once the positions reach zero Close
works. Selling by hand at Alpaca instead leaves the host counting the shares: a
permanent `position_mismatch` (on live, the `stock_position_mismatch` auto-kill).

Approval (`approve.py`), per order, the first failing check names the reason: `kill`,
`halted`, `environment`, `broker_stale` (no broker check for 4 x
`stock_broker_poll_s`), `live_disabled`, `not_live_eligible`, `market_closed`,
`moc_cutoff`, `session`, `symbol`, `short` (selling more than is held, long only),
`max_order` (`stock_max_order_cents`, $1,000), `max_position`
(`stock_max_position_cents`, $2,500), `cash` (the buy plus `stock_price_band` (5%) must
fit in the assignment's cash), `daily_loss` (buys only; `stock_max_daily_loss_cents`
per mode). The reference price is the previous session's adjusted close. An approved
buy reserves its price plus the band; the fill gives back what it did not use. Every
status change writes a `stock_order_events` row. Routes: the worker's
`/api/v1/stock_trade/state`, `/api/v1/stock_orders/request`, `/api/v1/stock_trade/release`
and the owner's `/api/stocks/...` (`host/api/stocks.py`). Host CLI: `stock-models`,
`stock-assign MODEL_ID [--mode] [--bankroll DOLLARS] [--symbols A,B]`,
`stock-assignments`, `stock-orders [--status active]`.

The worker side (`fleet/worker/stock_trade.py`, `StockTradeLoop`) runs in the trade role
every `stock_trade_tick_s`: it skips under kill, for a halted assignment and until the
host says the decision is due (inside the window, bars through the previous session,
not decided yet this session); then it refreshes the bar cache, asks the model, and
turns the weights into whole-share orders: equity = cash + reserved + positions at the
reference prices, target = floor(weight x equity / price), order = target minus held
(open orders count as held), sells first, then the buys, each buy cut to the cash left
at the host's reservation so none is refused with `cash`. Each order carries a one-line
rationale ("momentum rank 2/20, w 0.25, target 12 held 8"). The batch, empty or not, is
posted once per session, with idempotent ids, so a retry never doubles an order.

### 9.4 Exchange: orders at Alpaca, fills, broker, marks

`host/exchange/alpaca_trading.py` is the trading client (place, cancel, order by client
id, orders, positions, account, clock, calendar); every error text is redacted, 401 or
403 is an auth error, 429 backs off, and a timeout is never resubmitted blind. The
stock tasks run on a thread of their own in the exchange process
(`host/exchange/stock_tasks.py`), so Alpaca can never slow the NFL loop; without keys
they do nothing.

- `stock_broker` (`stock_broker.py`): the broker check, then the reconciliation. Our
  positions per symbol (over the assignments of the keys' mode) are compared with
  Alpaca's, and Alpaca's open orders that are not ours are listed. Differences go into
  the warnings on `/stocks`; live also pulls the kill switch (`unknown_order` at once,
  `stock_position_mismatch` when seen on two checks in a row).
- `stock_executor` (`stock_executor.py`): approved, then submitting, then open with
  Alpaca's id. A timeout stays submitting and is looked up by client id (found: open; not
  found after 60 s: expired, cash released). A refusal is `rejected_by_exchange`. A
  cancel request (halt, kill) is cancelled at Alpaca; after 15:50 Alpaca refuses and the
  order stays open with reason "cancel refused (after 15:50)" and fills at the close.
  The poll books fills through `stock_fills.book_fill` (once per fill, positions, cost,
  cash and realized P&L kept exact) and closes orders Alpaca cancelled or expired.
- `stock_marks`: once per session, 30 minutes after the close, every active or halted
  assignment's equity (cash + reserved + shares at the session's close) into
  `stock_marks`; these marks are the paper record the `live_eligible` gate reads.

Exchange CLI: `stock-smoke --confirm "STOCK SMOKE YYYY-MM-DD"` (one 1-share SPY cls buy
on the keys' account, cancelled after 10 s; refused under kill, on live keys while live
is off, and from 15:45) and `stock-cancel-all [--direct]` (without `--direct`: every
active stock order is cancelled in the database, the ones already at Alpaca by the
exchange process; with `--direct`, for when the exchange process is down: live keys
turn live off first, paper keys halt the paper assignments, then every open order on
the Alpaca account is cancelled and our rows are closed from what Alpaca reports).

### 9.5 Dashboard, settings, tests

`/stocks` (owner only, refreshed every 5 s, reached from the NFL | Stocks tabs under the
Trading title): the broker card (PAPER or LIVE, account status, equity, cash, buying
power, market open or closed, next close, last check, warnings), the assignments (mode,
symbols, bankroll, equity, today's P&L, total return, status; Halt, Resume, Sell all
(halted with shares), Close), the models (family and params, Sharpe, max drawdown, CAGR
against SPY, validated, status, the gate it still fails in words; Validate, Backtest
with the newest informational backtest and a link to its job, Assign, Retire), positions, the newest session's orders with their
reason codes and rationales, the per-symbol feed, and the New assignment and New
search forms. Home's "Needs attention" shows a Stocks row when an assignment is halted
or the broker has warnings. Settings has a Stocks group for every `stock_*` key and the
two threshold objects (dollars in, cents stored).

Tests: `tests/test_stock_models.py`, `test_stock_backtest.py`, `test_stock_worker.py`
(worker), `test_stock_host.py`, `test_stock_limits.py`, `test_stock_kill.py` (host),
`test_stock_exchange.py`, `test_stock_fills.py`, `test_stock_broker.py` (exchange, on the
in-memory Alpaca of `tests/fake_alpaca.py`), `test_stocks_page.py` (dashboard), and the
end to end `tests/e2e_stocks.py` (run by `tests/test_e2e.py::test_stocks_end_to_end`):
bars, a search and a validate run by the worker job functions, paper_ok, a paper
assignment decided by the worker 20 minutes before the close, approval, cls orders
placed and filled at the fake close, fills, positions and cash checked, the mark, and
the kill cancelling an open order.

### Known gaps (read before trusting the numbers)

- Survivorship: the default symbols are today's large companies, so a backtest from 2017
  holds names known to have done well since. Backtest returns are flattering; compare
  every model with SPY bought and held over the same years.
- Selection: a search keeps the best of many candidates by Sharpe, so its backtest
  Sharpe is biased up. The validation years are the honest number, and they are few.
- Live versus backtest: the backtest uses fractional shares and full investment; live
  uses whole shares and keeps the price band (5%) of the buys in cash. Sells free no
  cash until they fill at the close, so when a model rotates out of one name into
  another the buy is cut that day and completed the next session.
- Twenty paper days is a small sample: it proves the plumbing and the costs, not the
  edge. Alpaca's paper fills are simulated by Alpaca.
- The broker's cash is shared: the host keeps each assignment's own cash, but a manual
  trade on the same Alpaca account shows up only as a reconciliation warning.

## Assumptions to verify

| # | Assumption | Verify |
|---|---|---|
| L1 | Individual Trading API accounts can trade Kalshi event contracts (announcements name Alpaca's broker partners) | Owner asks Alpaca support; probe |
| L2 | Event contracts appear as an asset class on `/v2/assets`; its name is unknown | Probe |
| L3 | The paper environment supports event contracts | Probe on paper keys |
| L4 | Free plan: IEX real-time stocks, full-market history older than about 15 minutes, about 30 streaming symbols, about 200 requests a minute | Alpaca docs, owner |
| L5 | Kalshi fee formula and tick size for NFL contracts | Alpaca docs, probe |
| S1 | Alpaca refuses market-on-close orders and their cancels from 15:50 New York; the paper account fills cls orders at the official close | Alpaca docs; `stock-smoke` and the first paper session |
| S2 | A buy beyond buying power answers 403 (not 422) | First paper sessions; the order's reason on `/stocks` |
| S3 | The free plan serves SIP daily bars when the request ends 15 minutes or more back | `stock-bars-status` after the first fetch |
