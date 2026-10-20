# Polymarket US API: what the terminal relies on

Status as of 2026-10-20. Code: `backend/src/polymarket/`.

## How this was verified

- **docs.polymarket.us could not be reached** from the build sandbox. WebFetch got `ENOTFOUND` for
  `docs.polymarket.us`, `docs-polymarket-us.mintlify.app` and `gateway.polymarket.us`, and curl got a proxy 403.
  No live response was captured.
- **Primary source: Polymarket's official SDKs**, cloned from GitHub. PyPI lists `docs.polymarket.us` as their
  documentation home.
  - https://github.com/Polymarket/polymarket-us-typescript, commit `abdcaae` (2026-10-08). Read: `src/client.ts`,
    `src/auth.ts`, `src/resources/*.ts`, `src/types/*.ts`, `src/websocket/*.ts`, `tests/markets.test.ts`,
    `tests/websocket-flows.test.ts`.
  - https://github.com/Polymarket/polymarket-us-python, commit `6fde8f2` (2026-10-08). Read: README ("Market
    response type migration", WebSocket and Retries sections).
  - https://pypi.org/pypi/polymarket-us/json: version 2.4.1, Documentation = https://docs.polymarket.us.
- Secondary source, third-party and **not authoritative**: https://github.com/jaracah/polymarket-us-go. It agrees
  that the markets WebSocket requires auth. It uses an older settlement shape that the official SDK replaced.
- `host/exchange/adapters/*` and `docs/LIVE.md` in the parent repo are marked UNVERIFIED there, and several of their
  paths **disagree** with the official SDK: cancel, cancel-all, open orders and balance. Do not reuse them.

Labels used below:
- **VERIFIED (SDK)**: matches the official SDK source.
- **UNVERIFIED**: a guess. It is configurable in code and must be checked against a live response.

## Hosts — VERIFIED (SDK `src/client.ts`)

| Purpose | Host |
|---|---|
| Public market data (no auth) | `https://gateway.polymarket.us` (`GATEWAY_BASE_URL`) |
| Trading, portfolio and account (Ed25519 auth) | `https://api.polymarket.us` (`API_BASE_URL`) |
| WebSockets (auth) | `wss://api.polymarket.us/v1/ws/markets` and `wss://api.polymarket.us/v1/ws/private` |

## Public market data — VERIFIED (SDK `src/resources/{events,markets,search,series}.ts`, `src/types/*.ts`)

| Call | Response |
|---|---|
| `GET /v1/events` | `{ events: Event[] }` |
| `GET /v1/events/{id}` | `{ event: Event }` |
| `GET /v1/events/slug/{slug}` | `{ event: Event }` |
| `GET /v1/search?query=&limit=&seriesIds=&status=active\|closed\|upcoming&page=` | `{ events: Event[] }` (events include nested markets) |
| `GET /v1/series` | `{ series: Series[] }`. `Series = { id, slug, title, description?, active, closed, archived, recurrence? }` |
| `GET /v1/series/id/{id}` | `{ series: Series }` |
| `GET /v1/markets` | `{ markets: MarketDetail[] }` |
| `GET /v1/market/id/{id}` | `{ market: MarketDetail }` (note the singular `market`) |
| `GET /v1/market/slug/{slug}` | `{ market: MarketDetail }` |
| `GET /v1/markets/{slug}/bbo` | `{ marketData: MarketBBO }` |
| `GET /v1/markets/{slug}/book` | `{ marketData: MarketBook }` |
| `GET /v1/markets/{slug}/settlement` | `{ slug: string, settlement: number }` |

`GET /v1/events` query parameters (`EventsListParams`): `limit`, `offset`, `orderBy[]`, `orderDirection`, `id[]`,
`slug[]`, `archived`, `active`, `closed`, `liquidityMin/Max`, `volumeMin/Max`, `startDateMin/Max`,
`endDateMin/Max`, `tagId`, `tagSlug`, `relatedTags`, `featured`, `seriesId[]`, `eventDate`, `eventWeek`,
`startTimeMin/Max`, `gameId`, `ended` and `categories[]`. Array parameters repeat the key (`seriesId=1&seriesId=2`).

Shapes:
- `Event = { id, slug, title, description?, startTime?, endTime?, active, closed, archived, featured, liquidity?, volume?, markets?: Market[], tags?: {id,slug,label}[], series?: {id,slug,title} }`
- `Market` (nested in an event) = `{ id, slug, title, outcome, active, closed, liquidity?, volume? }`
- `MarketDetail` = the same fields plus `description?`, `eventSlug?` and `team?`

**Units:**
- Every price is an `Amount = { value: "0.55", currency: "USD" }`: a decimal string in **dollars per contract**.
  The terminal converts every price to cents (`normalize.ts: toCents`).
- `qty` is a string.
- `bidDepth` and `askDepth` are numbers.
- `sharesTraded` and `openInterest` are strings, and may be `""`.

`MarketBBO = { marketSlug, bestBid?: Amount|null, bestAsk?: Amount|null, bidDepth?, askDepth?, lastTradePx?: Amount|null, sharesTraded?, openInterest? }`.
`bestBid`, `bestAsk` and `lastTradePx` can be `null` (empty book).

`MarketBook = { marketSlug, bids: {px: Amount, qty: string}[], offers: {px: Amount, qty: string}[], state: MarketState, stats?: {lastTradePx?, sharesTraded?, openInterest?, highPx?, lowPx?}|null, transactTime?: string|null }`.
Asks are called **`offers`**. `MarketState` is one of `MARKET_STATE_{OPEN,PREOPEN,CLOSED,SUSPENDED,HALTED,EXPIRED,TERMINATED,MATCH_AND_CLOSE_AUCTION}`.

Settlement: `{ slug, settlement: number }`.
- The SDK tests use the values `0`, `0.5` and `1`. These are the long side's payout in dollars per contract.
- Mapping in the terminal: `1` → long won, `0` → long lost, `0.5` → treated as void (no Up/Down result).
- SDK 2.0 replaced the older `marketSlug, settlementPrice, settledAt` shape. The parser accepts both shapes.
- **UNVERIFIED:** what an unsettled market returns (404, an error, or `0`). Because of this, the tracker accepts a
  `0` only once `/book` no longer reports `MARKET_STATE_OPEN` or `MARKET_STATE_PREOPEN`.
- The endpoint does **not** publish the BTC settlement value, so `MarketWindow.settlePrice` stays `null`.

## How a "BTC Up or Down: 15 min" window is represented — mostly UNVERIFIED

**Verified:**
- A Polymarket US market is one binary instrument, traded with the intents
  `ORDER_INTENT_{BUY,SELL}_{LONG,SHORT}`.
- BBO and book quote the **long** side.
- Polymarket US launched 15-minute BTC Up/Down markets on 2026-09-22 (press coverage via web search). They settle on
  CF Benchmarks BRTI, which reportedly averages the readings of the final minute.

**UNVERIFIED (no live listing was available):**
- **Single market or two?** Discovery accepts both representations (`discovery.ts: windowsFromEvents`):
  - One market per window: Up = `<slug>:long`, Down = `<slug>:short`. If the market's `outcome` reads "Down", the
    sides flip.
  - Two markets (outcome Up and outcome Down) in one event: each side = that market's long.
  - Short-side prices are derived: Down bid = 100 − Up ask, and Down ask = 100 − Up bid.
- **How the series is found:**
  - `GET /v1/search?query=Bitcoin Up or Down&status=active|upcoming&limit=50`.
  - Optionally also `GET /v1/events?seriesId=…` (`POLY_SERIES_ID`) or `?tagSlug=…` (`POLY_TAG_SLUG`).
  - The result is filtered by text: BTC/Bitcoin + "up or down" + "15m/15 min", or explicit 15-minute times.
  - `POLY_SLUG_REGEX` replaces the heuristic outright.
- **Slug pattern:** unknown. The fixtures assume `btc-updown-15m-<startEpochSec>`, which is Polymarket
  international's style. Any slug works as long as the title or slug identifies the market.
- **Window start and end:** taken from `startTime`/`endTime` (market first, then event) when they are 15 minutes
  apart. Fallbacks, in order:
  1. An ET range in the title ("October 20, 10:30AM-10:45AM ET").
  2. A 10-digit epoch in the slug.
  3. `endTime − 15m`.
- **Price to beat:** read from the first numeric field ≥ 1000 among `POLY_PTB_FIELDS`. The defaults are
  `priceToBeat, price_to_beat, strikePrice, strike, threshold, line, referencePrice, openPrice, startPrice, targetPrice`.
  These are searched on the market, then on the event, including any nested `metadata` object. After that, the text
  is searched for "price to beat $X", "strike/opening price X" or "at or above $X".
  - If none of that is present, the tracker uses the BRTI proxy at window start (`priceAt(startTs)`).
  - Metadata always overrides the proxy value.

**To verify on the worker:**
```
curl -s 'https://gateway.polymarket.us/v1/search?query=Bitcoin%20Up%20or%20Down&status=active&limit=5'
```
Then save it as `backend/test/fixtures/polymarket/search-active.live.json`.

## Markets WebSocket — VERIFIED (SDK `src/websocket/{base,markets,types}.ts`)

**Auth is required.** The SDK signs the HTTP upgrade with the same `X-PM-*` headers, using method `GET` and path
`/v1/ws/markets`, and refuses to open the socket without keys. The Go client says the same.

The tracker's behavior depends on `POLY_WS_MODE`:
- `auto` (the default): uses the WebSocket only when `POLYMARKET_US_KEY_ID` and `POLYMARKET_US_SECRET` are set.
  Otherwise it polls `/bbo` every `pollMs` (1 s).
- `noauth`: tries the socket without headers.
- `off`: always polls.

REST polling also takes over whenever the WebSocket is down or quiet.

| Direction | Frame |
|---|---|
| Subscribe | `{"subscribe":{"requestId":"…","subscriptionType":"SUBSCRIPTION_TYPE_MARKET_DATA_LITE","marketSlugs":["…"]}}` |
| Unsubscribe | `{"unsubscribe":{"requestId":"…"}}` |
| `market_data_lite` (BBO) | `{ requestId, subscriptionType, marketDataLite: { marketSlug, bestBid?: Amount, bestAsk?: Amount, lastTradePx?: Amount } }` |
| `market_data` (book) | `{ …, marketData: { marketSlug, bids, offers, state, stats?, transactTime? } }` |
| `trade` | `{ …, trade: { marketSlug, price: Amount, quantity: Amount, tradeTime, maker: {side,intent}, taker: {side,intent} } }` |
| Heartbeat | `{ heartbeat: {} }` |
| Error | `{ requestId?, subscriptionType?, error: string }` |

- Subscription types: `SUBSCRIPTION_TYPE_MARKET_DATA`, `SUBSCRIPTION_TYPE_MARKET_DATA_LITE` and `SUBSCRIPTION_TYPE_TRADE`.
- Subscriptions do not survive a reconnect, so the tracker resubscribes on every open.
- The SDK's own tests sometimes send bare strings (`"0.55"`) instead of `Amount` objects. The parser accepts both.
- Reconnect backoff runs 1 s → 30 s with jitter. A socket idle for 20 s is terminated.

## Signing (trading and WebSockets) — VERIFIED (SDK `src/auth.ts`)

- Message: `timestampMs + METHOD + path`. The path is the URL pathname, with no query string and **no body**.
- Signature: Ed25519 over the UTF-8 bytes, encoded base64.
- Secret: base64. A 32-byte seed, or a 64-byte key whose first 32 bytes are used.
- Headers: `X-PM-Access-Key` (key ID, a UUID), `X-PM-Timestamp` (ms) and `X-PM-Signature`.
- Implemented in `backend/src/polymarket/auth.ts`.
- Unverified detail (third-party guides): the timestamp must be within about 30 s of server time.

## Trading endpoints (for later steps) — VERIFIED (SDK `src/resources/{orders,portfolio,account}.ts`)

| Action | Call |
|---|---|
| Place | `POST /v1/orders` with body `{ marketSlug, intent: ORDER_INTENT_{BUY,SELL}_{LONG,SHORT}, type?: ORDER_TYPE_LIMIT\|ORDER_TYPE_MARKET, price?: Amount, quantity?, tif?: TIME_IN_FORCE_{GOOD_TILL_CANCEL,GOOD_TILL_DATE,IMMEDIATE_OR_CANCEL,FILL_OR_KILL}, goodTillTime?, participateDontInitiate?, cashOrderQty?: Amount, slippageTolerance?: { currentPrice: Amount, bips?, ticks? }, synchronousExecution?, maxBlockTime?, manualOrderIndicator? }` → `{ id, executions? }` |
| Open orders | `GET /v1/orders/open?slugs=` → `{ orders: Order[] }` |
| One order | `GET /v1/order/{id}` → `{ order }` |
| Cancel | `POST /v1/order/{id}/cancel` with body `{ marketSlug }` |
| Modify | `POST /v1/order/{id}/modify` with body `{ marketSlug, price?, quantity?, tif?, … }` |
| Cancel all | `POST /v1/orders/open/cancel` with body `{ slugs? }` → `{ canceledOrderIds }` |
| Preview | `POST /v1/order/preview` |
| Close position | `POST /v1/order/close-position` with body `{ marketSlug, slippageTolerance?, … }` |
| Positions | `GET /v1/portfolio/positions?market=&limit=&cursor=` → `{ positions: Record<slug, UserPosition>, nextCursor?, eof? }` |
| Activities | `GET /v1/portfolio/activities` |
| Balances | `GET /v1/account/balances` → `{ balances: [{ currentBalance, buyingPower, currency, … }] }` |

- Batch variants of create, cancel and modify exist (up to 20 orders each). Their paths are not checked here.
- Order and execution fields include `commissionsBasisPoints`, `makerCommissionsBasisPoints`,
  `commissionNotionalTotalCollected` (order) and `commissionNotionalCollected` (execution).

**Private WebSocket** (`/v1/ws/private`, auth): subscriptions `SUBSCRIPTION_TYPE_ORDER` (updates only),
`SUBSCRIPTION_TYPE_ORDER_SNAPSHOT` (one-shot, ends with `eof: true`), `SUBSCRIPTION_TYPE_POSITION` (changes only,
no snapshot; use `/v1/portfolio/positions` for the initial state), `SUBSCRIPTION_TYPE_ACCOUNT_BALANCE` and
`SUBSCRIPTION_TYPE_RFQ`. Frame keys:
- `orderSubscriptionSnapshot` and `orderSubscriptionUpdate.execution`
- `positionSubscription.{beforePosition,afterPosition}`
- `accountBalancesSnapshot.balances` and `accountBalancesUpdate.balanceChange`

**Native bracket or stop orders: none found.** The SDK exposes only `ORDER_TYPE_LIMIT` and `ORDER_TYPE_MARKET`, with
`slippageTolerance` and the TIFs above. It has no stop, trigger, take-profit or OCO. TP/SL therefore stays in the
backend watcher, as SPEC.md requires.

## Fees — NOT CONFIRMED

No official fee schedule could be fetched: the docs host and the CFTC filings were unreachable. Third-party
sources contradict each other:
- A July 2026 schedule: taker rate × p × (1 − p), with 7% for crypto and makers free.
- A uniform taker fee of 0.05 plus a maker rebate.
- An earlier CFTC proposal of 30 bps taker / 20 bps maker rebate on premium.

So:
- `fees.ts` exports `FEE_SCHEDULE.confirmed = false`.
- `feeCents(price, contracts, override)` returns `null` without an override, so the UI shows `[FEE]`.
- The override is `Settings.feeCentsPerContract` (env `FEE_CENTS_PER_CONTRACT`).
- `feeCentsFromRate()` is available should the user choose to model the p(1 − p) curve.
- In live mode, use the exchange-reported `commissionNotionalCollected` on each execution.

## Environment variables read by `backend/src/polymarket/`

| Var | Default | Meaning |
|---|---|---|
| `POLY_GATEWAY_URL` | `https://gateway.polymarket.us` | public REST host |
| `POLY_WS_URL` | `wss://api.polymarket.us/v1/ws/markets` | markets WebSocket |
| `POLY_WS_MODE` | `auto` | `auto` (WebSocket if keys are set), `off` or `noauth` |
| `POLYMARKET_US_KEY_ID`, `POLYMARKET_US_SECRET` | unset | Ed25519 key ID and base64 secret (used only to sign the WebSocket upgrade here) |
| `POLY_TIMEOUT_MS` | `5000` | REST timeout |
| `POLY_SEARCH_QUERY` | `Bitcoin Up or Down` | discovery search text |
| `POLY_SERIES_ID` | unset | comma-separated series IDs for `/v1/events?seriesId=` |
| `POLY_TAG_SLUG` | unset | `/v1/events?tagSlug=` |
| `POLY_SLUG_REGEX` | unset | case-insensitive regex a window's slug must match (replaces the heuristic) |
| `POLY_PTB_FIELDS` | see above | comma-separated metadata keys for the price to beat |

The poll interval is the tracker's `pollMs` option; `config.ts` maps `POLY_POLL_MS` to it.
