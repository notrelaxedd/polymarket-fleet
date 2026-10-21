import { EventEmitter } from "node:events";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  BarAggregator,
  BtcProxy,
  backoffMs,
  candleWindows,
  computeProxy,
  fetchRecentBars,
  median,
  parseBitstamp,
  parseCandles,
  parseCoinbase,
  parseKraken,
} from "../src/feeds/index.ts";
import type { Bar, ExchangeId } from "../../shared/protocol.ts";
import { log } from "../src/log.ts";

const NOW = 1_700_000_000_000;

describe("exchange parsers", () => {
  it("coinbase ticker", () => {
    const raw = JSON.stringify({
      type: "ticker", sequence: 37475248783, product_id: "BTC-USD", price: "21934.5",
      open_24h: "20000", volume_24h: "1", low_24h: "1", high_24h: "2", volume_30d: "3",
      best_bid: "21934.0", best_bid_size: "0.1", best_ask: "21934.5", best_ask_size: "0.2",
      side: "buy", time: "2022-10-19T23:28:22.061769Z", trade_id: 1, last_size: "0.01",
    });
    expect(parseCoinbase(raw, NOW)).toEqual({ ticks: [{ exchange: "coinbase", ts: NOW, price: 21934.5 }], alive: true });
  });
  it("coinbase heartbeat / subscriptions / error / junk", () => {
    expect(parseCoinbase('{"type":"heartbeat","product_id":"BTC-USD"}', NOW)).toEqual({ ticks: [], alive: true });
    expect(parseCoinbase('{"type":"subscriptions","channels":[]}', NOW).alive).toBe(true);
    expect(parseCoinbase('{"type":"error","message":"Failed to subscribe","reason":"BTC-XYZ is not a valid product"}', NOW).error).toBe(
      "Failed to subscribe: BTC-XYZ is not a valid product",
    );
    expect(parseCoinbase("not json", NOW)).toEqual({ ticks: [], alive: false });
    expect(parseCoinbase('{"type":"ticker","product_id":"ETH-USD","price":"1"}', NOW).ticks).toEqual([]);
  });

  it("kraken ticker update + snapshot", () => {
    const raw = JSON.stringify({
      channel: "ticker", type: "update",
      data: [{ symbol: "BTC/USD", bid: 21933.9, bid_qty: 1, ask: 21934.1, ask_qty: 1, last: 21934.0, volume: 10,
        vwap: 21900, low: 1, high: 2, change: 3, change_pct: 0.1, timestamp: "2023-10-06T17:35:55.440295Z" }],
    });
    expect(parseKraken(raw, NOW).ticks).toEqual([{ exchange: "kraken", ts: NOW, price: 21934 }]);
    expect(parseKraken(raw.replace("update", "snapshot"), NOW).ticks).toHaveLength(1);
  });
  it("kraken heartbeat / pong / ack", () => {
    expect(parseKraken('{"channel":"heartbeat"}', NOW)).toEqual({ ticks: [], alive: true });
    expect(parseKraken('{"method":"pong","req_id":1}', NOW).alive).toBe(true);
    expect(parseKraken('{"method":"subscribe","result":{"channel":"ticker","symbol":"BTC/USD"},"success":true}', NOW)).toEqual({ ticks: [], alive: true });
  });
  it("kraken rejected subscribe -> error", () => {
    expect(parseKraken('{"method":"subscribe","error":"Currency pair not supported","success":false}', NOW).error).toBe("Currency pair not supported");
  });

  it("bitstamp trade (price_str)", () => {
    const raw = JSON.stringify({
      data: { id: 1, timestamp: "1662629036", amount: 0.01, amount_str: "0.01000000", price: 19000.5, price_str: "19000.50",
        type: 0, microtimestamp: "1662629036532000", buy_order_id: 1, sell_order_id: 2 },
      channel: "live_trades_btcusd", event: "trade",
    });
    expect(parseBitstamp(raw, NOW).ticks).toEqual([{ exchange: "bitstamp", ts: NOW, price: 19000.5 }]);
  });
  it("bitstamp ack / reconnect request", () => {
    expect(parseBitstamp('{"event":"bts:subscription_succeeded","channel":"live_trades_btcusd","data":{}}', NOW)).toEqual({ ticks: [], alive: true });
    expect(parseBitstamp('{"event":"bts:request_reconnect","channel":"","data":""}', NOW).reconnect).toBe(true);
    expect(parseBitstamp('{"event":"bts:error","channel":"","data":{"code":null,"message":"Bad subscription string."}}', NOW).error).toBe(
      "Bad subscription string.",
    );
  });
});

describe("backoff", () => {
  it("grows 1s -> 30s with jitter", () => {
    expect(backoffMs(0, () => 0.5)).toBe(1000);
    expect(backoffMs(3, () => 0.5)).toBe(8000);
    expect(backoffMs(20, () => 1)).toBe(30000);
    expect(backoffMs(0, () => 0)).toBe(750);
  });
});

describe("computeProxy", () => {
  it("median helpers", () => {
    expect(median([3, 1, 2])).toBe(2);
    expect(median([1, 3])).toBe(2);
  });
  it("3 live -> median", () => {
    const t = computeProxy({
      coinbase: { price: 100, priceTs: NOW }, kraken: { price: 110, priceTs: NOW - 500 }, bitstamp: { price: 300, priceTs: NOW - 100 },
    }, NOW)!;
    expect(t.price).toBe(110);
    expect(t.ts).toBe(NOW);
    expect(Object.keys(t.sources).sort()).toEqual(["bitstamp", "coinbase", "kraken"]);
  });
  it("2 live -> mean, 1 -> itself, 0 -> null; stale excluded", () => {
    const stale = { price: 999, priceTs: NOW - 30_001 };
    expect(computeProxy({ coinbase: { price: 100, priceTs: NOW }, kraken: { price: 200, priceTs: NOW }, bitstamp: stale }, NOW)!.price).toBe(150);
    const one = computeProxy({ coinbase: { price: 100, priceTs: NOW }, kraken: stale }, NOW)!;
    expect(one.price).toBe(100);
    expect(one.sources).toEqual({ coinbase: 100 });
    expect(computeProxy({ coinbase: stale }, NOW)).toBeNull();
    expect(computeProxy({}, NOW)).toBeNull();
  });
  it("a quiet but connected venue keeps counting for PROXY_PRICE_MAX_AGE_MS (trade-driven feeds)", () => {
    expect(computeProxy({ coinbase: { price: 5, priceTs: NOW - 30_000 } }, NOW)!.price).toBe(5);
    // seen live: Kraken printing every ~1s, Bitstamp every ~5s -> both must count, not one at a time
    const t = computeProxy({ kraken: { price: 84478.4, priceTs: NOW - 400 }, bitstamp: { price: 84468.31, priceTs: NOW - 5000 } }, NOW)!;
    expect(Object.keys(t.sources).sort()).toEqual(["bitstamp", "kraken"]);
    expect(t.price).toBeCloseTo(84473.355, 3);
  });
});

describe("BarAggregator", () => {
  it("rolls over 1s buckets and returns the closed bar", () => {
    const a = new BarAggregator(1);
    expect(a.current()).toBeNull();
    expect(a.push(1000, 10)).toBeNull();
    expect(a.push(1400, 12)).toBeNull();
    expect(a.push(1900, 9)).toBeNull();
    expect(a.push(1950, 11)).toBeNull();
    expect(a.current()).toEqual({ t: 1000, d: 1, o: 10, h: 12, l: 9, c: 11 });
    const closed = a.push(2001, 20);
    expect(closed).toEqual({ t: 1000, d: 1, o: 10, h: 12, l: 9, c: 11 });
    expect(a.current()).toEqual({ t: 2000, d: 1, o: 20, h: 20, l: 20, c: 20 });
  });
  it("skips empty buckets and supports other durations", () => {
    const a = new BarAggregator(60);
    a.push(61_000, 1);
    const closed = a.push(300_000, 2);
    expect(closed).toMatchObject({ t: 60_000, d: 60, o: 1, c: 1 });
    expect(a.current()!.t).toBe(300_000);
  });
  it("folds late points into the current bar", () => {
    const a = new BarAggregator(1);
    a.push(5000, 10);
    expect(a.push(4000, 8)).toBeNull();
    expect(a.current()).toMatchObject({ t: 5000, l: 8, c: 8 });
  });
});

describe("parseCandles", () => {
  it("[time, low, high, open, close, volume] newest-first -> ascending Bars", () => {
    const rows = [
      [1700000060, 99, 105, 100, 104, 3.2],
      [1700000000, 98, 101, 99, 100, 1.1],
      [1700000000, 98, 101, 99, 100, 1.1], // duplicate
      ["bad"],
    ];
    expect(parseCandles(rows)).toEqual([
      { t: 1700000000_000, d: 60, o: 99, h: 101, l: 98, c: 100 },
      { t: 1700000060_000, d: 60, o: 100, h: 105, l: 99, c: 104 },
    ]);
    expect(parseCandles({ message: "err" })).toEqual([]);
  });
});

class FakeFeed extends EventEmitter {
  constructor(readonly id: ExchangeId) {
    super();
  }
  start(): void {}
  stop(): void {}
  connect(): void {
    this.emit("state", this.id, true);
  }
  drop(): void {
    this.emit("state", this.id, false, "closed (1006)");
  }
  tick(price: number): void {
    this.emit("tick", { exchange: this.id, ts: Date.now(), price });
  }
  heartbeat(): void {
    this.emit("alive", this.id, Date.now());
  }
}

describe("BtcProxy (fake feeds, fake clock)", () => {
  afterEach(() => vi.useRealTimers());

  function setup(ids: ExchangeId[] = ["coinbase", "kraken", "bitstamp"]) {
    vi.useFakeTimers({ now: NOW });
    const fakes = new Map<ExchangeId, FakeFeed>();
    const proxy = new BtcProxy({
      logger: log,
      exchanges: ids,
      createFeed: (spec) => {
        const f = new FakeFeed(spec.id);
        fakes.set(spec.id, f);
        return f;
      },
    });
    proxy.start();
    const f = (id: ExchangeId) => fakes.get(id)!;
    const state = (id: ExchangeId) => proxy.status().find((s) => s.id === id)!;
    return { proxy, f, state };
  }

  it("a heartbeat-only feed goes stale after PROXY_PRICE_MAX_AGE_MS and leaves the median", () => {
    const { proxy, f, state } = setup(["coinbase", "kraken"]);
    f("coinbase").connect();
    f("kraken").connect();
    f("coinbase").tick(100);
    f("kraken").tick(110);
    vi.advanceTimersByTime(200);
    expect(proxy.last()!.price).toBe(105);
    for (let i = 0; i < 8; i++) {
      vi.advanceTimersByTime(500);
      f("coinbase").heartbeat(); // socket alive, but no trades
      f("kraken").tick(110);
    }
    // a few seconds without a trade is normal for a trade-driven feed: still counted
    expect(state("coinbase").state).toBe("live");
    expect(proxy.last()!.sources).toEqual({ coinbase: 100, kraken: 110 });
    for (let i = 0; i < 54; i++) {
      vi.advanceTimersByTime(500);
      f("coinbase").heartbeat();
      f("kraken").tick(110);
    }
    expect(state("coinbase")).toMatchObject({ state: "stale", lastMsgTs: NOW });
    expect(state("coinbase").ageMs).toBeGreaterThan(30_000);
    expect(state("kraken").state).toBe("live");
    vi.advanceTimersByTime(600); // housekeeping re-evaluates the source set
    expect(proxy.last()!.sources).toEqual({ kraken: 110 });
    proxy.stop();
  });

  it("a pre-disconnect price is not reused after reconnect until a new trade arrives", () => {
    const { proxy, f, state } = setup(["coinbase", "bitstamp"]);
    f("coinbase").connect();
    f("bitstamp").connect();
    f("coinbase").tick(200);
    f("bitstamp").tick(100);
    f("bitstamp").drop();
    vi.advanceTimersByTime(1000);
    f("bitstamp").connect();
    f("bitstamp").heartbeat(); // subscription ack after reconnect
    f("coinbase").tick(201);
    vi.advanceTimersByTime(200);
    expect(state("bitstamp")).toMatchObject({ state: "down", lastMsgTs: null, ageMs: null });
    expect(proxy.last()!.sources).toEqual({ coinbase: 201 });
    f("bitstamp").tick(150);
    vi.advanceTimersByTime(200);
    expect(state("bitstamp").state).toBe("live");
    expect(proxy.last()!.price).toBe(175.5);
    proxy.stop();
  });

  it("connected but never priced (rejected/ignored subscribe) is reported down, not live", () => {
    const { proxy, f, state } = setup(["kraken"]);
    f("kraken").connect();
    for (let i = 0; i < 5; i++) {
      vi.advanceTimersByTime(1000);
      f("kraken").heartbeat();
    }
    expect(state("kraken")).toMatchObject({ state: "down", detail: "connected, no price yet" });
    expect(proxy.last()).toBeNull();
    proxy.stop();
  });

  it("throttles emits to <= 10/s with a trailing emit of the latest price", () => {
    const { proxy, f } = setup(["coinbase"]);
    const seen: number[] = [];
    proxy.on("tick", (t) => seen.push(t.price));
    f("coinbase").connect();
    for (let i = 1; i <= 10; i++) f("coinbase").tick(i); // same instant
    expect(seen).toEqual([1]);
    vi.advanceTimersByTime(100);
    expect(seen).toEqual([1, 10]);
    proxy.stop();
  });
});

describe("candle backfill", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  const MIN = 60_000;

  it("windows hold <= 300 inclusive minute buckets, cover the range exactly, newest first", () => {
    const end = 1_700_000_040_000 - (1_700_000_040_000 % MIN);
    const start = end - 24 * 3_600_000;
    const w = candleWindows(start, end);
    expect(w[0]![1]).toBe(end - MIN);
    expect(w.at(-1)![0]).toBe(start);
    let buckets = 0;
    for (const [i, [s, e]] of w.entries()) {
      const n = (e - s) / MIN + 1;
      expect(n).toBeLessThanOrEqual(300);
      buckets += n;
      if (i > 0) expect(w[i - 1]![0] - e).toBe(MIN); // contiguous, no overlap
    }
    expect(buckets).toBe(24 * 60);
    expect(candleWindows(start, start)).toEqual([]);
  });

  function stubCandles(handler: (start: number, end: number, call: number) => Response) {
    const calls: Array<[number, number]> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const q = new URL(url).searchParams;
        const s = Date.parse(q.get("start")!);
        const e = Date.parse(q.get("end")!);
        calls.push([s, e]);
        return handler(s, e, calls.length);
      }),
    );
    return calls;
  }
  const rows = (s: number, e: number) => {
    const out: number[][] = [];
    for (let t = e; t >= s; t -= MIN) out.push([t / 1000, 1, 3, 2, 2.5, 1]);
    return out;
  };
  /** run with fake setTimeout so retry/chunk sleeps are instant */
  async function run<T>(p: Promise<T>): Promise<T | Error> {
    const settled = p.then(
      (v) => v,
      (e: Error) => e,
    );
    await vi.runAllTimersAsync();
    return settled;
  }

  it("fetches 24h in <= 300-candle requests", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    const calls = stubCandles((s, e) => Response.json(rows(s, e)));
    const bars = (await run(fetchRecentBars(24))) as Bar[];
    expect(calls).toHaveLength(5);
    for (const [s, e] of calls) expect((e - s) / MIN + 1).toBeLessThanOrEqual(300);
    expect(bars).toHaveLength(24 * 60);
    expect(bars.every((b, i) => i === 0 || b.t - bars[i - 1]!.t === MIN)).toBe(true);
  });

  it("a non-array body is an error, and earlier chunks survive via onChunk", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    const calls = stubCandles((s, e, call) => (call === 1 ? Response.json(rows(s, e)) : Response.json({ message: "oops" })));
    const kept: Bar[] = [];
    const res = await run(fetchRecentBars(24, { onChunk: (b) => kept.push(...b) }));
    expect(res).toBeInstanceOf(Error);
    expect((res as Error).message).toContain("oops");
    expect(calls).toHaveLength(1 + 3); // second chunk retried 3x
    expect(kept).toHaveLength(300); // the newest chunk
    expect(kept.at(-1)!.t).toBe(calls[0]![1]);
  });

  it("does not retry a 400", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    const calls = stubCandles(() => Response.json({ message: "granularity too small for the requested time range" }, { status: 400 }));
    const res = await run(fetchRecentBars(1));
    expect((res as Error).message).toMatch(/HTTP 400 .*granularity too small/);
    expect(calls).toHaveLength(1);
  });
});
