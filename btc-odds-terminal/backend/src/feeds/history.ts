import type { Bar } from "../../../shared/protocol.ts";

// Coinbase Exchange public REST candles:
//   https://docs.cdp.coinbase.com/exchange/reference/exchangerestproxy_getproductcandles
//   GET https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60&start=<ISO>&end=<ISO>
//   rows: [time(s), low, high, open, close, volume], newest first, max 300 rows per request.
// (Doc pages were unreachable from the authoring sandbox; format written from memory of the docs.)

const BASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles";
const GRANULARITY = 60;
const MAX_ROWS = 300;
const TIMEOUT_MS = 10_000;
const USER_AGENT = "btc-odds-terminal/0.1 (+history-seed)";

/** Parse Coinbase candle rows ([time, low, high, open, close, volume], time in seconds) to ascending 60s Bars. */
export function parseCandles(rows: unknown, durSec = GRANULARITY): Bar[] {
  if (!Array.isArray(rows)) return [];
  const byT = new Map<number, Bar>();
  for (const r of rows) {
    if (!Array.isArray(r) || r.length < 5) continue;
    const [time, low, high, open, close] = r.map(Number) as [number, number, number, number, number];
    if (![time, low, high, open, close].every(Number.isFinite)) continue;
    const t = time * 1000;
    byT.set(t, { t, d: durSec, o: open, h: high, l: low, c: close });
  }
  return [...byT.values()].sort((a, b) => a.t - b.t);
}

async function fetchChunk(startMs: number, endMs: number): Promise<Bar[]> {
  const url = `${BASE}?granularity=${GRANULARITY}&start=${new Date(startMs).toISOString()}&end=${new Date(endMs).toISOString()}`;
  let lastErr: unknown;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const res = await fetch(url, {
        headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      if (res.status === 429) {
        lastErr = new Error("coinbase candles: 429 rate limited");
        await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
        continue;
      }
      if (!res.ok) throw new Error(`coinbase candles: HTTP ${res.status}`);
      return parseCandles(await res.json());
    } catch (err) {
      lastErr = err;
      await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
}

/** Last `hours` hours of completed 60s bars, ascending, d=60. */
export async function fetchRecentBars(hours: number): Promise<Bar[]> {
  const endMs = Math.floor(Date.now() / 60_000) * 60_000;
  const startMs = endMs - Math.max(0, hours) * 3_600_000;
  const span = MAX_ROWS * GRANULARITY * 1000;
  const all = new Map<number, Bar>();
  for (let s = startMs; s < endMs; s += span) {
    const e = Math.min(endMs, s + span);
    for (const b of await fetchChunk(s, e)) {
      if (b.t >= startMs && b.t < endMs) all.set(b.t, b);
    }
    await new Promise((r) => setTimeout(r, 150)); // stay well under the public rate limit
  }
  return [...all.values()].sort((a, b) => a.t - b.t);
}
