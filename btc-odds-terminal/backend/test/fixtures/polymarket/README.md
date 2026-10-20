# Polymarket US fixtures — DERIVED FROM DOCS/SDK, NOT CAPTURED LIVE

The build sandbox could not reach docs.polymarket.us or gateway.polymarket.us, so these files are
hand-written to mirror the wire shapes in the OFFICIAL SDKs:

- https://github.com/Polymarket/polymarket-us-typescript (commit abdcaae, 2026-10-08): src/types/{events,markets,common}.ts,
  src/websocket/types.ts, tests/markets.test.ts
- https://github.com/Polymarket/polymarket-us-python (commit 6fde8f2, 2026-10-08): README "Market response type migration"

VERIFIED shapes: `{ events: Event[] }`, `{ marketData: MarketBBO }` with `Amount = { value: "0.55", currency: "USD" }`,
`{ marketData: MarketBook }` (bids/offers of `{ px: Amount, qty: string }`), `{ slug, settlement: number }`,
WS frames `{ requestId, subscriptionType, marketDataLite: {...} }`, `{ heartbeat: {} }`, `{ error }`.

HYPOTHETICAL (UNVERIFIED) content: the BTC 15m event titles/slugs (`btc-updown-15m-<startEpoch>`), the
market `outcome` values, and where the price to beat appears (description text in search-active.json,
a `priceToBeat` field in event-two-markets.json). Replace with a live capture when the gateway is reachable:

    curl -s 'https://gateway.polymarket.us/v1/search?query=Bitcoin%20Up%20or%20Down&status=active&limit=50' > search-active.live.json

Fixed clock used by the tests: 2026-10-20T14:37:00Z (10:37 AM ET) -> current window 14:30–14:45Z, next 14:45–15:00Z.
