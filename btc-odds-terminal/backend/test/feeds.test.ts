import { describe, expect, it } from "vitest";
import {
  BarAggregator,
  backoffMs,
  computeProxy,
  median,
  parseBitstamp,
  parseCandles,
  parseCoinbase,
  parseKraken,
} from "../src/feeds/index.ts";

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
    expect(parseCoinbase('{"type":"error","message":"x"}', NOW).alive).toBe(false);
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
    expect(parseKraken('{"method":"subscribe","result":{"channel":"ticker","symbol":"BTC/USD"},"success":true}', NOW).alive).toBe(true);
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
      coinbase: { price: 100, msgTs: NOW }, kraken: { price: 110, msgTs: NOW - 500 }, bitstamp: { price: 300, msgTs: NOW - 100 },
    }, NOW)!;
    expect(t.price).toBe(110);
    expect(t.ts).toBe(NOW);
    expect(Object.keys(t.sources).sort()).toEqual(["bitstamp", "coinbase", "kraken"]);
  });
  it("2 live -> mean, 1 -> itself, 0 -> null; stale excluded", () => {
    const stale = { price: 999, msgTs: NOW - 3001 };
    expect(computeProxy({ coinbase: { price: 100, msgTs: NOW }, kraken: { price: 200, msgTs: NOW }, bitstamp: stale }, NOW)!.price).toBe(150);
    const one = computeProxy({ coinbase: { price: 100, msgTs: NOW }, kraken: stale }, NOW)!;
    expect(one.price).toBe(100);
    expect(one.sources).toEqual({ coinbase: 100 });
    expect(computeProxy({ coinbase: stale }, NOW)).toBeNull();
    expect(computeProxy({}, NOW)).toBeNull();
  });
  it("exactly STALE_AFTER_MS is still live", () => {
    expect(computeProxy({ coinbase: { price: 5, msgTs: NOW - 3000 } }, NOW)!.price).toBe(5);
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
