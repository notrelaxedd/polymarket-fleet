# Claude Code Build Prompt — BTC Odds Terminal (v2)

## Goal
Build **BTC Odds Terminal**, a TradingView-style trading dashboard for the **Polymarket US "BTC Up or Down: 15 min"** markets. One chart shows live BTC price with the current 15m market's Up odds overlaid on the same time axis. I can place trades, set take-profit / stop-loss brackets by dragging lines on the chart, draw annotations and stickers, and study the relationship between BTC's distance from the price to beat and where the odds trade.

Build it as a working product, not a demo. Ask me before adding any paid data source. Match the design spec below closely — it comes from an approved mockup (design reference: https://claude.ai/artifact/CXqKz5PEDN8K8pjs3HxhtD — it may be private; the written spec below is the source of truth).

---

## Stack
- **Frontend:** Next.js 14 App Router, TypeScript, Tailwind, shadcn/ui. Dark only.
- **Charting:** TradingView **Lightweight Charts v5** (`lightweight-charts`, Apache-2.0). Use multiple price scales (left = odds, right = BTC) and panes (main + edge pane). Build drawings, stickers, position lines, window bands and the tooltip as custom **series primitives / plugins** — Lightweight Charts has no built-in drawing tools. Do NOT use the licensed TradingView Advanced Charts library.
- **Small analysis charts** (pair relationship tab): plain SVG React components, no extra chart library.
- **Backend:** long-running Node/TypeScript service (Fastify + `ws`) on one of my polymarket-fleet worker machines (Debian, headless). It must keep running with the browser closed because it enforces TP/SL and alerts. Next.js talks to it over REST + one WebSocket.
- **Storage:** SQLite (better-sqlite3) on the worker: ticks, odds snapshots, windows, orders, fills, positions, brackets, drawings, alerts, settings.
- If my existing 15m BTC Up/Down bot repo is available, reuse its market discovery and fair-value code.

## Data sources — verify every endpoint against live docs before coding; never guess field names or slugs
1. **Polymarket US market data** (public, no key): `https://gateway.polymarket.us` → `GET /v1/markets/{slug}/bbo`, `/book`, `/settlement`, plus events/markets listing for discovery. Docs: https://docs.polymarket.us/api-reference/introduction. Check the markets WebSocket (`wss://api.polymarket.us/v1/ws/markets` — `market_data_lite`, trades, BBO); use it if available, otherwise poll BBO ~1s.
2. **Polymarket US trading:** authenticated host `api.polymarket.us`, **Ed25519** keys (key ID + secret from polymarket.us/developer). `POST /v1/orders`, open orders, cancel, cancel-all, positions, balances; private WebSocket for order/fill updates. Keys live only in the backend `.env`.
3. **BTC price:** these markets settle on **CF Benchmarks BRTI**, which isn't freely streamable. Build a BRTI proxy = median of Coinbase `BTC-USD`, Kraken and Bitstamp WebSockets. Label it "BTC · BRTI proxy" everywhere and note that settlement can differ when price sits right at the strike.
4. **Fees:** look up the Polymarket US fee schedule and use it in the ticket and P&L. If you can't confirm it, show `[FEE]` and make it a setting — don't invent a number.

## Market lifecycle
- Auto-discover the active 15m BTC market and the next one; read price to beat, window start/end and the Up/Down instruments from metadata.
- Roll over at each boundary with no reload. Record final settlement per window.
- Store every window for scrollback and **Replay window** (replays ticks + odds at 1×/4×/16×).

## Fair value
P(Up) = Φ( ln(S/K) / (σ·√t) ), S = BRTI proxy, K = price to beat, t = seconds left, σ = rolling realized vol per √second from the proxy (configurable lookback, default 30 min). Prefer my bot's model if the repo exists. **Edge = fair − Up mid.**

---

## Design spec (match the mockup)

### Tokens
| Token | Value | Use |
|---|---|---|
| bg | `#0A0D12` | page |
| surface | `#0E1218` | header, toolbar, cards |
| surface-2 | `#151B24` | popovers, tooltip, chips |
| inset | `#0A0D12` | inputs, summary boxes |
| border | `#1E2530` / strong `#2A3442` | dividers / controls |
| text | `#E6E9EF`, secondary `#C9CED8`, muted `#A3ABB9`, labels `#8A93A3` | |
| up / bull | `#34D399` (tint `#123528`) | BTC up candles, Buy Up, TP, positive P&L |
| down / bear | `#F87171` (tint `#3A1818`) | down candles, Buy Down, SL, losses |
| odds | `#5B9CFF` (text `#7FB2FF`, tint `#13213A`) | Up odds line, bid/ask band (16% fill), entry line, positive edge |
| fair | `#C29BFF` | fair-value dashed line, lag peak |
| price to beat | `#F5B942` (tint `#2A2111`) | PTB dashed line, countdown, PAPER badge |
| negative edge | `#F59E0B` | edge pane bars below zero |

Fonts: **Inter** (UI) and **JetBrains Mono** (every number, price, time, ticker). Labels are 11px, 600 weight, uppercase, letter-spaced. Base text 13px. Radius 8px controls / 10px cards. All text must pass 4.5:1 contrast.

### Layout (desktop ≥1280px)
```
┌──────────────────────────── Header ─────────────────────────────┐
├──┬────────────────────── Chart column ─────────────┬─ Right ────┤
│T │ chart toolbar                                    │ Order      │
│o │ legend row                                       │ ticket     │
│o │ MAIN CHART (BTC right / odds left)               │ Up book    │
│l │ EDGE PANE                                        │ Positions  │
│s │ ─ tabs: Pair relationship │ Orders & fills │ …   │ Risk       │
│  │ analysis cards (3 across)                        │            │
└──┴──────────────────────────────────────────────────┴────────────┘
```
- Tools rail 52px; right panel 320–360px; chart column fills the rest.
- Below ~1100px the right panel wraps under the chart; on phones everything stacks and the tools rail becomes a horizontal scroll strip. Touch targets ≥44px.

### Header (single row, wraps)
Left → right: logo + "BTC Odds Terminal" · market selector button ("BTC Up or Down · 15m", "10:30–10:45 AM ET", chevron) · countdown pill (amber, mono, mm:ss) · stat blocks: PRICE TO BEAT (amber), BTC · BRTI PROXY (value + signed change vs PTB, green/red), UP BID / ASK (blue), DOWN BID / ASK, FAIR · EDGE (purple · blue) · spacer · **LAST 8** results as small U/D tiles (green/red) · feed status dots per source (green live, amber >2s stale with age shown, red down) · **PAPER** (amber) / **LIVE** (red) badge · settings button.

### Tools rail (left)
Icon buttons, top to bottom: cursor, trend line, ray, horizontal line, vertical line, rectangle, fib retracement, text note, price-range measure, **stickers** · divider · add alert, lock drawings, undo, delete all. Active tool gets a filled background. Every button has an `aria-label` and tooltip.
**Sticker picker:** popover beside the rail, 4×2 grid of round badge stickers — target, rocket, warning, check, cross, bull (up-trend arrow), bear (down-trend arrow), lightning — plus "Click the chart to place · drag to move". Stickers are SVG badges, not emoji.

### Chart toolbar
Timeframe segmented control (1s / 5s / **15s** / 1m / 5m) · Candles | Line · overlay toggle chips: Up odds, Down odds, Fair value, Price to beat, Windows, Edge pane, Per-exchange (chips carry their series color swatch when on) · spacer · "Replay window" button.

### Legend row
"BTC · BRTI proxy (median CB/KR/BS) · 15s" then O/H/L/C of the hovered (or latest) candle, Up mid, Fair, PTB — each in its series color.

### Main chart
- **Right scale:** BTC candles. **Left scale:** 0–100¢ Up odds as a 2px line with a shaded bid/ask band.
- **Window bands:** previous window slightly darker, current window slightly lighter; label at the bottom of each band ("10:15–10:30 · SETTLED UP", "10:30–10:45 · LIVE"). Strong vertical lines at boundaries.
- **Price to beat:** amber dashed line across the current window with an amber tag on the right axis and a small "Price to beat" label; previous window's PTB at 45% opacity with its value.
- **Now line:** dotted vertical line with "NOW · 6:45 LEFT" at the bottom; empty space to the right until the window ends.
- **Position lines (TradingView style):** entry = blue dashed line + triangle marker at the fill, label "UP 96.2 @ 26¢ +$59.64"; **TP** green solid line with label "TP 96¢ +$67.33"; **SL** red solid line with label "SL 70¢ +$42.31". TP/SL labels have a drag grip and an ✕ to remove; dragging updates the bracket live (debounced) and shows projected P&L while dragging.
- **Axis tags:** current Up mid on the left axis (blue), current BTC on the right (green/red by last candle), PTB on the right (amber), crosshair values in gray.
- **Crosshair tooltip** (card, mono): BTC · Δ PTB (signed, colored) · Up b/a · Fair · Edge · Time left.
- **Drawings:** white trend lines with round handles when selected, dotted horizontal lines with an inline label, text notes as small dark cards, stickers as round badges. Selectable, draggable, recolorable, lockable, deletable; Undo/redo; Esc, Del, Ctrl/Cmd+Z.
- **Edge pane** under the main chart: label "EDGE · FAIR − MARKET", zero line, bars per candle (blue above zero, amber below), right-axis ±10¢.

### Bottom tabs
Tabs: **Pair relationship** (default) · Orders & fills · Window history · Alerts (count badge). Right side: sample filter segmented control — This window / **Last 20** / All. Pair relationship shows three cards (auto-fit grid, min 280px):
1. **BTC vs strike → Up price** — scatter, x = (BTC − PTB) in σ units −2…+2, y = Up mid 0–100¢; points colored by time left in 4 steps (dark slate → light blue); dashed purple fair curve for the current time left; current point = amber ring + "now". Legend + one-line takeaway ("At +1.3σ the market sits 2¢ under fair.").
2. **Lag: odds vs BTC** — bars of cross-correlation between BTC returns and later odds changes at lags 0–30s (2s steps); peak bar purple with label "4s · ρ 0.61"; takeaway line.
3. **Sensitivity** — ¢ of Up per $10 BTC over the window: market (solid blue) vs fair (dashed purple), current values labeled at "now", rest of window shaded; takeaway line.
Takeaway sentences are generated from the live numbers.

Orders & fills: table of orders, fills, slippage vs limit, fees, status. Window history: one row per window — time, PTB, settle, result U/D, my P&L, max edge seen; click to replay. Alerts: list with enable toggle, condition, last fired; "New alert" button.

### Right panel (top → bottom)
1. **Order ticket:** Buy Up / Buy Down toggle buttons showing current ask (selected = filled tint + colored border) · Limit | Marketable segmented control · AMOUNT input in USD with quick chips $10/$25/$50/$100 · Max slippage (¢) · **BRACKET · OCO** section with checkbox + value rows: Take profit (¢), Stop loss (¢), Stop if BTC < (price), Time exit at T− (s), and the hint "Drag the TP/SL lines on the chart to adjust after entry." · summary box: Est. avg fill, Contracts, Fees, Max loss, Payout if Up/Down (+profit), Edge vs fair · big submit button "Buy Up · Paper" (green; red for Down; label shows Paper/Live).
2. **Up book:** 4 asks (red) over a "88¢ mid" divider over 4 bids (green), with depth bars behind sizes and "spread 2¢".
3. **Positions:** header with today's realized P&L; card per position — side tag, size @ avg, live P&L, chips (mark, TP, SL, time exit), buttons Edit bracket / Close.
4. **Risk:** progress bars for Daily loss limit and Open exposure, Max loss per window, and the **Kill switch · cancel all & flatten** button (red outline, confirm dialog).
Footer note: "Settlement uses CF Benchmarks BRTI".

---

## Trading behavior
- **Paper mode is the default.** Live requires the settings toggle plus a confirm dialog on first live order each session. Paper fills simulate against the real book (walk the levels, apply slippage cap and fees).
- Brackets are **OCO and enforced by the backend watcher**: TP and SL on contract price, optional BTC-price stop, optional time exit. Exits use marketable-limit orders capped by max slippage; log slippage. If Polymarket US offers native bracket/stop orders, use those instead — check the docs.
- Risk: max position size, max loss per window, daily loss limit that auto-disables trading until the next day, kill switch = cancel all + flatten.
- Positions and P&L reconcile with the private WebSocket / positions endpoint in live mode.

## Alerts
On BTC price, Up/Down odds, edge threshold, or "BTC crosses price to beat". Evaluated by the backend; delivered as browser notification + sound, logged in the Alerts tab.

## Engineering requirements
- Reconnect with backoff on every WebSocket; mark a feed stale after 3s (amber dot + age) and show a banner if the BRTI proxy or Polymarket feed is stale.
- Times stored in UTC, displayed in America/New_York.
- Chart must stay smooth at 1s candles with a few hours loaded (batch updates per animation frame).
- Unit tests: market discovery/rollover, OCO bracket logic, fair-value math, paper-fill simulation, lag/sensitivity calculations.
- `README`: worker setup (systemd service), `.env.example`, how to switch paper → live, how to run the frontend locally or on Vercel pointed at the worker.
- Seed script that backfills recent BTC history so the chart isn't empty on first load.

## Build order
1. Backend: feeds (BRTI proxy + Polymarket US BBO), market discovery/rollover, SQLite recording, WebSocket to frontend.
2. App shell + design tokens + header + main chart (BTC + odds overlay, PTB line, window bands, now line, edge pane, tooltip).
3. Paper trading: order ticket, book, positions, TP/SL watcher, draggable position lines.
4. Drawing tools + sticker picker.
5. Pair relationship tab + other bottom tabs.
6. Alerts, risk controls, replay, live trading.

**Stop after step 2 and show me before continuing.**
