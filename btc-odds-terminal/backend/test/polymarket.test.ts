import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  DEFAULT_DISCOVERY,
  extractPriceToBeat,
  extractTimes,
  nyWallToUtc,
  parseInstrument,
  parseTitleWindow,
  pickCurrentNext,
  ptbFromText,
  sidePrices,
  statusAt,
  windowsFromEvents,
  mergeWindow,
} from "../src/polymarket/discovery.ts";
import { FEE_SCHEDULE, feeCents, feeCentsFromRate } from "../src/polymarket/fees.ts";
import {
  bookToBbo,
  parseBbo,
  parseBook,
  parseSettlement,
  parseWsMessage,
  settlementToResult,
  toCents,
} from "../src/polymarket/normalize.ts";
import { buildOddsSnap } from "../src/polymarket/tracker.ts";
import { backoffMs } from "../src/polymarket/ws.ts";

const FIX = path.join(__dirname, "fixtures", "polymarket");
const fx = (name: string): any => JSON.parse(readFileSync(path.join(FIX, name), "utf8"));

const NOW = Date.parse("2026-10-20T14:37:00Z"); // 10:37 AM ET
const CUR = "btc-updown-15m-1792506600"; // 14:30–14:45Z
const NEXT = "btc-updown-15m-1792507500"; // 14:45–15:00Z
const AFTER = "btc-updown-15m-1792508400"; // 15:00–15:15Z

function allEvents() {
  return [...fx("search-active.json").events, ...fx("search-upcoming.json").events];
}

describe("discovery", () => {
  it("keeps only BTC 15m up/down windows (drops ETH and the daily BTC market)", () => {
    const ws = windowsFromEvents(allEvents(), NOW);
    expect(ws.map((w) => w.window.id)).toEqual([CUR, NEXT, AFTER]);
  });

  it("picks current and next for a fixed clock", () => {
    const ws = windowsFromEvents(allEvents(), NOW);
    const { current, next } = pickCurrentNext(ws, NOW);
    expect(current?.window.id).toBe(CUR);
    expect(current?.window.status).toBe("live");
    expect(current?.window.startTs).toBe(Date.parse("2026-10-20T14:30:00Z"));
    expect(current?.window.endTs).toBe(Date.parse("2026-10-20T14:45:00Z"));
    expect(next?.window.id).toBe(NEXT);
    expect(next?.window.status).toBe("upcoming");
  });

  it("single market: Up = long, Down = short of the same slug", () => {
    const [w] = windowsFromEvents(fx("search-active.json").events, NOW);
    expect(w!.window.upInstrument).toBe(`${CUR}:long`);
    expect(w!.window.downInstrument).toBe(`${CUR}:short`);
    expect(parseInstrument(w!.window.downInstrument)).toEqual({ slug: CUR, side: "short" });
    expect(w!.upSlug).toBe(CUR);
    expect(w!.downSlug).toBe(CUR);
  });

  it("two markets (Up / Down) in one event become one window", () => {
    const ev = fx("event-two-markets.json").event;
    const ws = windowsFromEvents([ev], NOW);
    expect(ws).toHaveLength(1);
    const w = ws[0]!;
    expect(w.window.id).toBe("btc-15m-oct-20-1030am-up");
    expect(w.window.upInstrument).toBe("btc-15m-oct-20-1030am-up:long");
    expect(w.window.downInstrument).toBe("btc-15m-oct-20-1030am-down:long");
    expect(w.window.priceToBeat).toBe(108412.37);
    expect(w.ptbFromMetadata).toBe(true);
  });

  it("rolls over exactly at the boundary", () => {
    const ws = windowsFromEvents(allEvents(), NOW);
    const end = Date.parse("2026-10-20T14:45:00Z");
    const before = pickCurrentNext(ws, end - 1);
    expect(before.current?.window.id).toBe(CUR);
    expect(before.next?.window.id).toBe(NEXT);
    const at = pickCurrentNext(ws, end);
    expect(at.current?.window.id).toBe(NEXT);
    expect(at.next?.window.id).toBe(AFTER);
    expect(statusAt(ws[0]!.window, end)).toBe("closed");
    expect(statusAt({ ...ws[0]!.window, result: "up" }, end)).toBe("settled");
  });

  it("no current window between listings -> current null, next = upcoming", () => {
    const ws = windowsFromEvents(fx("search-upcoming.json").events, NOW);
    const r = pickCurrentNext(ws, NOW);
    expect(r.current).toBeNull();
    expect(r.next?.window.id).toBe(NEXT);
  });

  it("merge keeps settlement and prefers metadata PTB over a proxy fallback", () => {
    const [w] = windowsFromEvents(fx("search-upcoming.json").events, NOW);
    const prev = { ...w!, window: { ...w!.window, priceToBeat: 108000, result: "down" as const } };
    const merged = mergeWindow(prev, w!, NOW);
    expect(merged.window.priceToBeat).toBe(108000); // incoming has no metadata PTB
    expect(merged.window.result).toBe("down");
    const withMeta = { ...w!, ptbFromMetadata: true, window: { ...w!.window, priceToBeat: 108111 } };
    expect(mergeWindow(prev, withMeta, NOW).window.priceToBeat).toBe(108111);
  });
});

describe("window times", () => {
  it("explicit startTime/endTime", () => {
    const t = extractTimes({ slug: "x", startTime: "2026-10-20T14:30:00Z", endTime: "2026-10-20T14:45:00Z" }, null, DEFAULT_DISCOVERY, NOW);
    expect(t).toEqual({ startTs: Date.parse("2026-10-20T14:30:00Z"), endTs: Date.parse("2026-10-20T14:45:00Z"), explicit15: true });
  });

  it("falls back to the slug epoch (window start)", () => {
    const t = extractTimes({ slug: "btc-updown-15m-1792506600" }, null, DEFAULT_DISCOVERY, NOW);
    expect(t?.startTs).toBe(1792506600_000);
    expect(t?.endTs).toBe(1792506600_000 + 900_000);
  });

  it("falls back to an ET title range (EDT and EST)", () => {
    expect(parseTitleWindow("Bitcoin Up or Down - October 20, 10:30AM-10:45AM ET", NOW)).toEqual({
      startTs: Date.parse("2026-10-20T14:30:00Z"),
      endTs: Date.parse("2026-10-20T14:45:00Z"),
    });
    expect(parseTitleWindow("Bitcoin Up or Down - December 1, 9:00-9:15AM ET", NOW)).toEqual({
      startTs: Date.parse("2026-12-01T14:00:00Z"),
      endTs: Date.parse("2026-12-01T14:15:00Z"),
    });
    expect(parseTitleWindow("Bitcoin Up or Down - October 20, 11:45PM-12:00AM ET", NOW)?.endTs).toBe(
      Date.parse("2026-10-21T04:00:00Z"),
    );
    expect(nyWallToUtc(2026, 6, 4, 12, 0)).toBe(Date.parse("2026-07-04T16:00:00Z"));
  });

  it("an end time alone implies start = end - 15m", () => {
    const t = extractTimes({ slug: "x", endTime: "2026-10-20T14:45:00Z", startTime: "2026-10-20T10:00:00Z" }, null, DEFAULT_DISCOVERY, NOW);
    expect(t?.startTs).toBe(Date.parse("2026-10-20T14:30:00Z"));
    expect(t?.explicit15).toBe(false);
  });
});

describe("price to beat", () => {
  it("parses description text", () => {
    const ev = fx("search-active.json").events[0];
    expect(extractPriceToBeat(ev.markets[0], ev)).toBe(108412.37);
    expect(ptbFromText("Price to beat: $108,412.37.")).toBe(108412.37);
    expect(ptbFromText("Resolves Up if BTC is at or above $97,250 at 10:45")).toBe(97250);
    expect(ptbFromText("opening price 101234.5")).toBe(101234.5);
  });

  it("parses configured fields incl. Amount objects and ignores non-BTC numbers", () => {
    expect(extractPriceToBeat({ strikePrice: { value: "108000.5", currency: "USD" } }, null)).toBe(108000.5);
    expect(extractPriceToBeat({ metadata: { priceToBeat: 99999 } }, null)).toBe(99999);
    expect(extractPriceToBeat({ line: 0.5, title: "Bitcoin Up or Down 15 minutes" }, null)).toBeNull();
    expect(extractPriceToBeat({ myStrike: "108000" }, null, ["myStrike"])).toBe(108000);
  });

  it("is null when metadata lacks it (tracker then uses the BRTI proxy)", () => {
    const ws = windowsFromEvents(fx("search-upcoming.json").events, NOW);
    expect(ws[0]!.window.priceToBeat).toBeNull();
    expect(ws[0]!.ptbFromMetadata).toBe(false);
  });
});

describe("settlement", () => {
  it("parses the numeric settlement shape", () => {
    expect(parseSettlement(fx("settlement-up.json"))).toEqual({ slug: "btc-updown-15m-1792505700", value: 1 });
    expect(parseSettlement(fx("settlement-down.json"))?.value).toBe(0);
    expect(parseSettlement(fx("settlement-legacy.json"))?.value).toBe(1);
    expect(parseSettlement(null)).toBeNull();
    expect(parseSettlement({ slug: "x" })).toBeNull();
    expect(parseSettlement({ slug: "x", settlement: 100 })?.value).toBe(1); // defensive cents
  });

  it("maps to a window result", () => {
    expect(settlementToResult(1)).toBe("up");
    expect(settlementToResult(0)).toBe("down");
    expect(settlementToResult(0.5)).toBeNull();
    expect(settlementToResult(1, false)).toBe("down");
    expect(settlementToResult(parseSettlement(fx("settlement-void.json"))!.value)).toBeNull();
  });
});

describe("BBO / book normalization (cents)", () => {
  it("BBO Amounts -> cents", () => {
    expect(parseBbo(fx("bbo.json"))).toEqual({ slug: CUR, bid: 61, ask: 63, last: 62, bidDepth: 4, askDepth: 5 });
    const empty = parseBbo(fx("bbo-empty.json"))!;
    expect([empty.bid, empty.ask, empty.last]).toEqual([null, null, null]);
    expect(toCents("0.555")).toBe(55.5);
    expect(toCents(0.07)).toBe(7);
    expect(toCents({ value: "1", currency: "USD" })).toBe(100);
    expect(toCents("abc")).toBeNull();
    expect(parseBbo({ nope: 1 })).toBeNull();
  });

  it("short side mirrors the long book", () => {
    expect(sidePrices({ bid: 61, ask: 63 }, "short")).toEqual({ bid: 37, ask: 39 });
    expect(sidePrices({ bid: null, ask: 63 }, "short")).toEqual({ bid: 37, ask: null });
    const snap = buildOddsSnap(NOW, CUR, { bid: 61, ask: 63 }, sidePrices({ bid: 61, ask: 63 }, "short"));
    expect(snap).toEqual({ ts: NOW, windowId: CUR, upBid: 61, upAsk: 63, downBid: 37, downAsk: 39, upMid: 62 });
    expect(buildOddsSnap(NOW, CUR, { bid: null, ask: 40 }, { bid: null, ask: null }).upMid).toBe(40);
  });

  it("book levels sorted best-first", () => {
    const b = parseBook(fx("book.json"))!;
    expect(b.bids.map((l) => l.price)).toEqual([61, 60]);
    expect(b.asks.map((l) => l.price)).toEqual([63, 64]);
    expect(b.asks[0]!.qty).toBe(80);
    expect(b.state).toBe("MARKET_STATE_OPEN");
    expect(bookToBbo(b)).toMatchObject({ bid: 61, ask: 63 });
  });

  it("WS frames", () => {
    const frames = fx("ws-frames.json").map((f: unknown) => parseWsMessage(JSON.stringify(f)));
    expect(frames[0]).toMatchObject({ kind: "bbo", bbo: { slug: CUR, bid: 58, ask: 60, last: 59 } });
    expect(frames[1]).toMatchObject({ kind: "bbo", bbo: { bid: 55, ask: 56 } }); // bare-string prices (SDK test shape)
    expect(frames[2]).toEqual({ kind: "heartbeat" });
    expect(frames[3]).toMatchObject({ kind: "error", requestId: "bot-bbo-2" });
    expect(frames[4]).toMatchObject({ kind: "trade", price: 59, qty: 25 });
    expect(parseWsMessage("not json")).toEqual({ kind: "unknown" });
  });
});

describe("fees", () => {
  it("unconfirmed schedule -> null without override", () => {
    expect(FEE_SCHEDULE.confirmed).toBe(false);
    expect(feeCents(50, 10, null)).toBeNull();
  });
  it("override is cents per contract", () => {
    expect(feeCents(50, 10, 1.5)).toBe(15);
    expect(feeCents(50, 0, 2)).toBe(0);
    expect(feeCents(50, -1, 2)).toBeNull();
  });
  it("rate helper: C x rate x p x (1-p)", () => {
    expect(feeCentsFromRate(50, 100, 0.07)).toBe(175);
    expect(feeCentsFromRate(99, 100, 0.07)).toBeCloseTo(6.93, 2);
  });
});

describe("ws backoff", () => {
  it("1s -> 30s with jitter", () => {
    expect(backoffMs(0, 1000, 30000, () => 1)).toBe(1000);
    expect(backoffMs(0, 1000, 30000, () => 0)).toBe(500);
    expect(backoffMs(3, 1000, 30000, () => 1)).toBe(8000);
    expect(backoffMs(20, 1000, 30000, () => 1)).toBe(30000);
  });
});
