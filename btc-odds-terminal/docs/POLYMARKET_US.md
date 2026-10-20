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
- **UNVERIFIED:** what an unsettled market returns (404, an error, or a proto3-style default `0`; the SDK types
  `settlement: number` as non-optional). A result cannot be undone once recorded, so the tracker
  (`tracker.ts: trySettle`) accepts a value (0 **or** 1) only when:
  1. two reads at least 30 s apart return the same value (a value that changes restarts the clock);
  2. `/book` is readable and not `MARKET_STATE_OPEN`/`PREOPEN`. A `/book` error (timeout, 5xx) means "unknown,
     retry later". A 404 or a book without a state does not block;
  3. the book state is terminal (`EXPIRED`, `TERMINATED`, `CLOSED`) and the BRTI proxy at the window end does not
     clearly contradict the value (proxy vs PTB gap > 0.05 %). Otherwise the value is held, and is accepted only
     after it has been stable for 10 minutes (logged with the doubt).
- A confirmed non-binary value (`0.5`, void) leaves the window without a result and stops polling it.
- Settlement polling starts at end + 5 s and slows down with time since the end: every 5 s (first 2 min), 15 s
  (to 10 min), 60 s (to 30 min), then 5 min, until 6 h after the end.
- **Restart recovery:** `MarketTracker` takes `unsettled: () => MarketWindow[]` (e.g. `db.listWindows(200)`). On
  `start()` it re-adopts the windows without a result that ended less than 6 h ago (instruments rebuilt with
  `parseInstrument`) and settles them through the same loop. Discovery never returns ended windows, so without
  this a window whose settlement was missed by a restart or an outage would stay `result = NULL`.
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
- **How the series is found** (`tracker.ts: fetchEvents`):
  1. Series: `POLY_SERIES_ID`, or else resolved automatically every 30 min from `GET /v1/series?active=true`
     (paged by `offset`): BTC/Bitcoin in the slug or title and "15m"/"15 min" in the slug/title or 15/fifteen/quarter
     in `recurrence`; series that also read "up or down" are preferred. The series is then listed with
     `GET /v1/events?seriesId=…&active=true&closed=false` (and `?tagSlug=` for `POLY_TAG_SLUG`), paged by `offset`
     (100 per page, at most 5 pages) until a short page.
  2. Fallback, only when step 1 did not yield both the live and the next window:
     `GET /v1/search?query=Bitcoin Up or Down&status=active|upcoming&limit=50`, paged. The page base (0 or 1) is
     UNVERIFIED, so the first request has no `page`, then `page=1, 2, …`, deduplicated by slug. Paging stops at a
     short page, a page with nothing new, the needed window (live / next), or 5 requests.
  - Events listed without nested markets are fetched by slug (`/v1/events/slug/{slug}`), at most 8 per cycle,
    nearest to now first.
  - The result is filtered by text: BTC/Bitcoin + "up or down" + "15m/15 min", **or** an ET title range spanning
    15 minutes ("October 20, 10:30AM-10:45AM ET"), or explicit 15-minute `startTime`/`endTime`.
  - `POLY_SLUG_REGEX` replaces the heuristic outright.
  - Empty or failed discovery backs off (5 s → 60 s with jitter) instead of retrying every 5 s.
- **Slug pattern:** unknown. The fixtures assume `btc-updown-15m-<startEpochSec>`, which is Polymarket
  international's style. Any slug works as long as the title or slug identifies the market.
- **Window start and end:** taken from `startTime`/`endTime` (market first, then event) when they are 15 minutes
  apart. Fallbacks, in order:
  1. An ET range in the title ("October 20, 10:30AM-10:45AM ET"). Without a year in the title, the year is the
     New York calendar year (±1) that puts the window closest to now, so a "December 31, 11:30PM" window read at
     04:40Z on January 1 stays in the old year. If the title disagrees by an hour or more with the slug epoch or
     `endTime − 15m`, those win (the 1:00–2:00 AM ET hour on the fall-back Sunday happens twice; the title
     parser returns the first, EDT, instant).
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
- `auto` (the default): uses the WebSocket as soon as `POLYMARKET_US_KEY_ID` and `POLYMARKET_US_SECRET` are set,
  **also in paper mode** (the keys are not only for live trading). Otherwise it polls `/bbo` every `pollMs` (1 s).
- `noauth`: tries the socket without headers.
- `off`: always polls, even with keys set.

Quote freshness and liveness (`tracker.ts`):
- Every WS frame, heartbeats included, counts as liveness. A quote delivered by the socket stays valid while the
  socket is alive (the stream pushes changes), so a quiet book stays `live`.
- REST polling takes over for a window whenever one of its sides has had no fresh quote for 1.5 × `pollMs`
  (WebSocket down, silent, or not delivering that market).
- A side whose quote is older than max(3 s, 3 × `pollMs`) and not backed by a live socket is published as `null`.
  A BBO that names the market but has no price keys is an empty book (both sides `null`), not a dropped frame.
- At a rollover the new window's cached quotes are published at once and a REST poll runs immediately. A poll that
  was in flight across the boundary is discarded.
- WS server errors (error frames, `upgrade rejected: HTTP 401`) show in the feed status detail, e.g.
  `poll (ws: upgrade rejected: HTTP 401)`.
- BBO polling backs off (2 × `pollMs` → 15 s with jitter) after 3 consecutive failures. A 429, or any response
  with `Retry-After`, pauses every REST loop for the requested time (default 10 s, at most 5 min).

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
- Reconnect backoff runs 1 s → 30 s with jitter. It resets only after a data frame or 30 s of uptime, so a server
  that accepts the upgrade and then drops the socket does not cause a reconnect storm. Two or more 401/403 upgrade
  rejections in a row (bad keys, or clock skew: the timestamp is signed with the local clock) back off to
  1–10 minutes. A socket idle for 20 s is terminated.

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
| `POLYMARKET_US_KEY_ID`, `POLYMARKET_US_SECRET` | unset | Ed25519 key ID and base64 secret. Here they only sign the WebSocket upgrade, but setting them switches market data to the WebSocket even in paper mode (`POLY_WS_MODE=off` forces polling) |
| `POLY_TIMEOUT_MS` | `5000` | REST timeout |
| `POLY_SEARCH_QUERY` | `Bitcoin Up or Down` | discovery search text |
| `POLY_SERIES_ID` | unset | comma-separated series IDs for `/v1/events?seriesId=` |
| `POLY_TAG_SLUG` | unset | `/v1/events?tagSlug=` |
| `POLY_SLUG_REGEX` | unset | case-insensitive regex a window's slug must match (replaces the heuristic) |
| `POLY_PTB_FIELDS` | see above | comma-separated metadata keys for the price to beat |

The poll interval is the tracker's `pollMs` option; `config.ts` maps `POLY_POLL_MS` to it.

### Recovery procedure when the terminal shows "no active window"

Every discovery heuristic above is a guess until checked against a live response. On the worker:
1. `curl -s 'https://gateway.polymarket.us/v1/series?active=true&limit=100'`. Find the BTC 15-minute series and
   set `POLY_SERIES_ID=<id>`.
2. If there is no such series, `curl -s 'https://gateway.polymarket.us/v1/search?query=Bitcoin%20Up%20or%20Down&status=active&limit=50'`.
   Adjust `POLY_SEARCH_QUERY` until the live window appears, or set `POLY_TAG_SLUG`.
3. If the windows are returned but not recognised, set `POLY_SLUG_REGEX` to a pattern that matches their slugs.
4. If the price to beat is missing, add the field name to `POLY_PTB_FIELDS`.
5. If the feed status shows `ws: upgrade rejected: HTTP 401`, check the keys and the clock, or set `POLY_WS_MODE=off`.

## Live trading (`backend/src/live/`) — env vars and routes

How the terminal uses the trading endpoints above (step 6). Nothing in this section has been exercised against the
real exchange yet: every live HTTP call in the tests goes through an injected fetch, and the private WebSocket
tests use a local server. Treat the first live session as a test with a small amount.

| Var | Default | Meaning |
|---|---|---|
| `POLY_API_URL` | `https://api.polymarket.us` | signed trading REST host (VERIFIED: SDK `API_BASE_URL`) |
| `POLY_PRIVATE_WS_URL` | `wss://api.polymarket.us/v1/ws/private` | private WebSocket (orders, positions, balances) |
| `LIVE_RECONCILE_MS` | `15000` (min 2000) | how often positions + balances are re-read and reconciled while live |
| `POLY_SHORT_PRICE_QUOTE` | `short` | **UNVERIFIED.** `short` = a SHORT-intent (Down) price is the Down instrument's own price; `long` = it is quoted as the Up price (100 − Down). If this is wrong, Down orders go out at the wrong price: check it with a tiny Down order (or the order preview endpoint) before trading Down |
| `POLY_QTY_STEP` | `1` | **UNVERIFIED.** order quantity step in contracts (quantities round down to it) |
| `POLY_TICK_CENTS` | `1` | **UNVERIFIED.** price tick in cents (buys round down, sells round up: never worse than asked) |

Routes (all under `/api`):
- `GET /live/status` → `LiveStatus` (keys present, live mode on, private WS connected, last reconcile, balance).
- `POST /live/confirm` → `{ session, expiresTs }`. Checks the key with a signed balance read first; 409
  `live_unavailable` unless settings mode is live and both keys are set. Every live order and close must carry the
  session as `liveSession` (else 409 `live_confirm_required`; the UI then shows its confirm dialog again).
- `POST /live/reconcile` → `LiveStatus & { deferred }`: reconcile with the exchange now (`deferred: true` when it had
  to wait, e.g. an order was in flight).

Behaviour worth knowing before going live:
- A failed or ambiguous submit (timeout, network error, 5xx, unparseable 2xx) is never resent. The order stays
  `pending` under `externalId: "unconfirmed:<id>"` (the UI shows "confirming…") for up to ~30 s while the executor
  looks for it in the socket, `GET /v1/orders/open` and the positions delta.
- "Marketable" orders are limit orders at the slippage cap with `IMMEDIATE_OR_CANCEL`. TP / SL / BTC stop / time
  exit are enforced by the backend watcher (no native bracket orders exist).
- Leaving live mode cancels our resting live orders one by one. It is refused (400) while a live position of the
  current window is still open, because its bracket, Close and the kill switch need the live executor.
- The kill switch also calls the exchange's cancel-all (`POST /v1/orders/open/cancel`) for the current and next
  market, as a backstop for orders the terminal never tracked. This cancels every open order in those markets,
  including ones placed from other clients.
- Fees: the execution's `commissionNotionalCollected` when the exchange reports it, else the settings override,
  else `[FEE]` (null).
