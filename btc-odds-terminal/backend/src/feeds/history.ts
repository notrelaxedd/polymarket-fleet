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

const BUCKET_MS = GRANULARITY * 1000;
const RETRIES = 3;
const CHUNK_GAP_MS = 150; // stay well under the public rate limit

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const snippet = (v: unknown) => (typeof v === "string" ? v : String(JSON.stringify(v))).slice(0, 200);

/**
 * Request windows [start, end] (bucket start times, ms) covering the minute buckets in [startMs, endMs),
 * newest first. `end` is inclusive on Coinbase's side (ccxt sends end = since + (limit-1)*tf), so each
 * window spans at most maxRows buckets = maxRows - 1 intervals; Coinbase rejects > 300 with HTTP 400.
 */
export function candleWindows(startMs: number, endMs: number, maxRows = MAX_ROWS): Array<[number, number]> {
  const first = Math.ceil(startMs / BUCKET_MS) * BUCKET_MS;
  const last = Math.ceil(endMs / BUCKET_MS) * BUCKET_MS - BUCKET_MS; // newest bucket starting before endMs
  const out: Array<[number, number]> = [];
  for (let e = last; e >= first; e -= maxRows * BUCKET_MS) out.push([Math.max(first, e - (maxRows - 1) * BUCKET_MS), e]);
  return out;
}

async function fetchChunk(startMs: number, endMs: number): Promise<Bar[]> {
  const url = `${BASE}?granularity=${GRANULARITY}&start=${new Date(startMs).toISOString()}&end=${new Date(endMs).toISOString()}`;
  let lastErr = new Error("coinbase candles: not attempted");
  for (let attempt = 1; attempt <= RETRIES; attempt++) {
    let delayMs = 500 * attempt;
    try {
      const res = await fetch(url, {
        headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      if (res.ok) {
        const body: unknown = await res.json();
        if (Array.isArray(body)) return parseCandles(body);
        lastErr = new Error(`coinbase candles: expected an array, got ${snippet(body)}`);
      } else {
        lastErr = new Error(`coinbase candles: HTTP ${res.status} ${snippet(await res.text().catch(() => ""))}`);
        if (res.status === 429) delayMs = 1000 * attempt;
        else if (res.status < 500) break; // other 4xx (bad range/params) won't fix themselves on retry
      }
    } catch (err) {
      lastErr = err instanceof Error ? err : new Error(String(err));
    }
    if (attempt < RETRIES) await sleep(delayMs);
  }
  throw lastErr;
}

/**
 * Last `hours` hours of completed 60s bars, ascending, d=60. Chunks are fetched newest first and each
 * chunk's bars are handed to `onChunk` as soon as it arrives, so a caller that stores them per chunk keeps
 * the most recent history even if a later (older) chunk fails; the failure is still thrown.
 */
export async function fetchRecentBars(hours: number, opts: { onChunk?: (bars: Bar[]) => void } = {}): Promise<Bar[]> {
  const endMs = Math.floor(Date.now() / BUCKET_MS) * BUCKET_MS;
  const startMs = endMs - Math.max(0, hours) * 3_600_000;
  const all = new Map<number, Bar>();
  for (const [i, [s, e]] of candleWindows(startMs, endMs).entries()) {
    if (i > 0) await sleep(CHUNK_GAP_MS);
    const bars = (await fetchChunk(s, e)).filter((b) => b.t >= startMs && b.t < endMs);
    for (const b of bars) all.set(b.t, b);
    opts.onChunk?.(bars);
  }
  return [...all.values()].sort((a, b) => a.t - b.t);
}
