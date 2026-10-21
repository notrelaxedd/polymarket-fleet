import { EventEmitter } from "node:events";
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FeedStatus, MarketWindow, OddsSnap } from "../../shared/protocol.ts";
import type { Logger } from "../src/log.ts";
import { PolyHttpError, type MarketDataSource, type Query } from "../src/polymarket/client.ts";
import { parseBbo, type NormalizedBbo, type NormalizedBook, type NormalizedSettlement } from "../src/polymarket/normalize.ts";
import { MarketTracker, type TrackerOptions } from "../src/polymarket/tracker.ts";

const FIX = path.join(__dirname, "fixtures", "polymarket");
const fx = (name: string): any => JSON.parse(readFileSync(path.join(FIX, name), "utf8"));

const NOW = Date.parse("2026-10-20T14:37:00Z");
const BOUNDARY = Date.parse("2026-10-20T14:45:00Z");
const CUR = "btc-updown-15m-1792506600";
const NEXT = "btc-updown-15m-1792507500";

const quiet: Logger = {
  info: () => {},
  warn: () => {},
  error: () => {},
  debug: () => {},
  child: () => quiet,
} as unknown as Logger;

class FakeSource implements MarketDataSource {
  bboFail: Error | null = null;
  bboDelayMs = 0;
  bboNull = new Set<string>();
  settlements = new Map<string, NormalizedSettlement>();
  bboCalls: string[] = [];
  settleCalls: string[] = [];
  searchCalls: Query[] = [];
  /** override: events for a search query (default: the active/upcoming fixtures) */
  search: ((q: Query) => Record<string, unknown>[]) | null = null;
  async listEvents(): Promise<Record<string, unknown>[]> {
    return [];
  }
  async searchEvents(q: Query): Promise<Record<string, unknown>[]> {
    this.searchCalls.push(q);
    if (this.search) return this.search(q);
    return q.status === "upcoming" ? fx("search-upcoming.json").events : fx("search-active.json").events;
  }
  async getEventBySlug(): Promise<Record<string, unknown> | null> {
    return null;
  }
  async getBbo(slug: string): Promise<NormalizedBbo | null> {
    this.bboCalls.push(slug);
    if (this.bboDelayMs > 0) await new Promise((r) => setTimeout(r, this.bboDelayMs));
    if (this.bboFail) throw this.bboFail;
    if (this.bboNull.has(slug)) return null;
    return parseBbo({ ...fx("bbo.json"), marketData: { ...fx("bbo.json").marketData, marketSlug: slug } }, slug);
  }
  async getSettlement(slug: string) {
    this.settleCalls.push(slug);
    return this.settlements.get(slug) ?? null;
  }
}

/** FakeSource with GET /book (market state) */
class BookSource extends FakeSource {
  bookState: string | null = "MARKET_STATE_CLOSED";
  bookFail = false;
  async getBook(slug: string): Promise<NormalizedBook | null> {
    if (this.bookFail) throw new Error("timeout");
    if (this.bookState === null) return null; // 404
    return { slug, bids: [], asks: [], state: this.bookState, transactTime: null };
  }
}

/** Stands in for MarketsStream: same events ("bbo", "message", "open", "close", "serverError"). */
class FakeStream extends EventEmitter {
  connected = true;
  slugs: string[] = [];
  start(): void {}
  stop(): void {}
  setSlugs(slugs: string[]): void {
    this.slugs = slugs;
  }
  quote(slug: string, bid: number | null, ask: number | null): void {
    this.emit("message");
    this.emit("bbo", { slug, bid, ask, last: null, bidDepth: null, askDepth: null });
  }
}

const STALE_PLUS = 4000;

describe("MarketTracker", () => {
  let src: FakeSource;
  let tracker: MarketTracker;
  const events: { window: MarketWindow[]; rollover: any[]; odds: OddsSnap[]; status: FeedStatus[]; settled: MarketWindow[] } = {
    window: [],
    rollover: [],
    odds: [],
    status: [],
    settled: [],
  };

  let proxyAtBoundary: number | null;
  function make(over: Partial<TrackerOptions> = {}): MarketTracker {
    tracker?.stop();
    tracker = new MarketTracker({
      logger: quiet,
      pollMs: 1000,
      client: src,
      config: { wsMode: "off" },
      priceAt: (ts) => (ts === BOUNDARY ? proxyAtBoundary : null),
      ...over,
    });
    tracker.on("window", (w) => events.window.push(w));
    tracker.on("rollover", (r) => events.rollover.push(r));
    tracker.on("odds", (o) => events.odds.push(o));
    tracker.on("status", (s) => events.status.push(s));
    tracker.on("settled", (w) => events.settled.push(w));
    return tracker;
  }

  beforeEach(() => {
    vi.useFakeTimers({ now: NOW });
    src = new FakeSource();
    proxyAtBoundary = 108500.5;
    for (const k of Object.keys(events) as (keyof typeof events)[]) events[k].length = 0;
    make();
  });

  afterEach(() => {
    tracker.stop();
    vi.useRealTimers();
  });

  it("discovers current + next on start and emits odds in cents", async () => {
    tracker.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(tracker.current()?.id).toBe(CUR);
    expect(tracker.next()?.id).toBe(NEXT);
    expect(tracker.current()?.priceToBeat).toBe(108412.37);
    expect(events.rollover[0].current.id).toBe(CUR);

    await vi.advanceTimersByTimeAsync(1000);
    const o = tracker.lastOdds()!;
    expect(o).toMatchObject({ windowId: CUR, upBid: 61, upAsk: 63, downBid: 37, downAsk: 39, upMid: 62 });
    expect(events.odds.length).toBeGreaterThan(0);
    expect(tracker.status()).toMatchObject({ id: "polymarket", state: "live" });
  });

  it("rolls over at the boundary without restart, fills PTB from the proxy, then settles", async () => {
    tracker.start();
    await vi.advanceTimersByTimeAsync(0);
    events.rollover.length = 0;

    await vi.advanceTimersByTimeAsync(BOUNDARY - NOW + 100);
    expect(tracker.current()?.id).toBe(NEXT);
    expect(events.rollover).toHaveLength(1);
    expect(events.rollover[0].current.id).toBe(NEXT);
    expect(events.window.some((w) => w.id === CUR && w.status === "closed")).toBe(true);

    // next window had no PTB in metadata -> BRTI-proxy price at its start
    await vi.advanceTimersByTimeAsync(2000);
    expect(tracker.current()?.priceToBeat).toBe(108500.5);
    expect(tracker.lastOdds()?.windowId).toBe(NEXT);

    // settlement appears -> "window" + "settled" once two reads >= 30s apart agree
    src.settlements.set(CUR, { slug: CUR, value: 1 });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(events.settled).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(events.settled).toHaveLength(1);
    expect(events.settled[0]).toMatchObject({ id: CUR, result: "up", status: "settled" });
    expect(events.window.some((w) => w.id === CUR && w.result === "up")).toBe(true);
  });

  it("goes stale/down when BBO polling fails", async () => {
    tracker.start();
    await vi.advanceTimersByTimeAsync(1000);
    expect(tracker.status().state).toBe("live");
    src.bboFail = new Error("boom");
    await vi.advanceTimersByTimeAsync(STALE_PLUS);
    expect(tracker.status().state).not.toBe("live");
    expect(events.status.at(-1)!.state).not.toBe("live");
    src.bboFail = null;
    await vi.advanceTimersByTimeAsync(6000); // the poll backs off after 3 failures
    expect(tracker.status().state).toBe("live");
  });

  it("backs the BBO poll off while it keeps failing and honours Retry-After on 429", async () => {
    tracker.start();
    await vi.advanceTimersByTimeAsync(1000);
    src.bboFail = new Error("boom");
    src.bboCalls.length = 0;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(src.bboCalls.length).toBeLessThan(20); // fixed 1 Hz would be 60
    src.bboFail = new PolyHttpError(429, "/v1/markets/x/bbo", "429 slow down", 20_000);
    const before = src.bboCalls.length;
    for (let i = 0; i < 20 && src.bboCalls.length === before; i++) await vi.advanceTimersByTimeAsync(1000);
    const n = src.bboCalls.length; // the 429 just came back
    expect(n).toBe(before + 1);
    await vi.advanceTimersByTimeAsync(18_000);
    expect(src.bboCalls.length).toBe(n);
  });

  // ---------------------------------------------------------------- discovery robustness

  it("empty discovery: no window, status says so, and re-discovery backs off", async () => {
    src.search = () => [];
    tracker.start();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(tracker.current()).toBeNull();
    expect(tracker.status()).toMatchObject({ state: "down", detail: "no active window" });
    // unbacked: ~13 discoveries x 2 searches in a minute
    expect(src.searchCalls.length).toBeLessThan(20);
  });

  it("a throwing search reports a discovery error", async () => {
    src.search = () => {
      throw new Error("503 /v1/search");
    };
    tracker.start();
    await vi.advanceTimersByTimeAsync(1000);
    expect(tracker.current()).toBeNull();
    expect(tracker.status().state).toBe("down");
    expect(tracker.status().detail).toMatch(/discovery: 503/);
  });

  it("partial failure: active ok, upcoming failing -> current found", async () => {
    src.search = (q) => {
      if (q.status === "upcoming") throw new Error("timeout /v1/search");
      return fx("search-active.json").events;
    };
    tracker.start();
    await vi.advanceTimersByTimeAsync(1000);
    expect(tracker.current()?.id).toBe(CUR);
    expect(tracker.next()).toBeNull();
    expect(tracker.status().state).toBe("live");
  });

  it("overlapping live windows: the latest start is current", async () => {
    const late = {
      slug: "btc-updown-15m-1792506900",
      title: "Bitcoin Up or Down 15m",
      startTime: "2026-10-20T14:35:00Z",
      endTime: "2026-10-20T14:50:00Z",
      markets: [{ slug: "btc-updown-15m-1792506900", title: "Bitcoin Up or Down 15m", outcome: "Up" }],
    };
    src.search = (q) => (q.status === "upcoming" ? [] : [...fx("search-active.json").events, late]);
    tracker.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(tracker.current()?.id).toBe("btc-updown-15m-1792506900");
  });

  it("pages the search: the live window on page 2 is found", async () => {
    const filler = Array.from({ length: 50 }, (_, i) => ({
      slug: `bitcoin-above-${100 + i}k-on-october-20`,
      title: `Bitcoin above $${100 + i},000 on October 20?`,
      markets: [{ slug: `bitcoin-above-${100 + i}k-on-october-20`, title: "Yes", outcome: "Yes" }],
    }));
    src.search = (q) => {
      if (q.status === "upcoming") return fx("search-upcoming.json").events;
      return q.page === 2 ? fx("search-active.json").events : filler; // 1-based: default == page 1
    };
    tracker.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(tracker.current()?.id).toBe(CUR);
    expect(src.searchCalls.filter((q) => q.status === "active").map((q) => q.page)).toEqual([undefined, 1, 2]);
  });

  it("resolves the BTC 15m series via GET /v1/series and skips the text search", async () => {
    const listed: Query[] = [];
    const s2 = Object.assign(src, {
      listSeries: async () => fx("series.json").series,
      listEvents: async (q: Query) => {
        listed.push(q);
        return [...fx("search-active.json").events, ...fx("search-upcoming.json").events];
      },
    });
    make({ client: s2 });
    tracker.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(tracker.current()?.id).toBe(CUR);
    expect(tracker.next()?.id).toBe(NEXT);
    expect(listed[0]).toMatchObject({ seriesId: [77], active: true, closed: false });
    expect(src.searchCalls).toHaveLength(0);
  });

  // ---------------------------------------------------------------- settlement

  async function toSettlePhase(): Promise<void> {
    tracker.start();
    await vi.advanceTimersByTimeAsync(BOUNDARY - NOW + 100);
    expect(tracker.current()?.id).toBe(NEXT);
  }

  it("settlement 0 with a CLOSED book: one read is not enough; a BRTI disagreement holds it", async () => {
    src = new BookSource();
    make();
    src.settlements.set(CUR, { slug: CUR, value: 0 });
    await toSettlePhase();
    await vi.advanceTimersByTimeAsync(40_000);
    expect(events.settled).toHaveLength(0); // proxy 108500.5 > PTB 108412.37 says Up: held
    await vi.advanceTimersByTimeAsync(11 * 60_000);
    expect(events.settled).toHaveLength(1); // stable for 10 min: the exchange wins
    expect(events.settled[0]).toMatchObject({ id: CUR, result: "down" });
  });

  it("settlement 0 confirmed by a second read and the proxy -> down", async () => {
    src = new BookSource();
    proxyAtBoundary = 108300;
    make();
    src.settlements.set(CUR, { slug: CUR, value: 0 });
    await toSettlePhase();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(events.settled).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(events.settled[0]).toMatchObject({ id: CUR, result: "down", status: "settled" });
  });

  it("an unsettled value that later changes never sticks", async () => {
    src = new BookSource();
    make();
    src.settlements.set(CUR, { slug: CUR, value: 0 }); // proto default while BRTI is pending
    await toSettlePhase();
    await vi.advanceTimersByTimeAsync(20_000);
    src.settlements.set(CUR, { slug: CUR, value: 1 });
    await vi.advanceTimersByTimeAsync(45_000);
    expect(events.settled).toHaveLength(1);
    expect(events.settled[0]).toMatchObject({ id: CUR, result: "up" });
  });

  it("an OPEN book or a /book error blocks settlement; a 404 book does not", async () => {
    const bs = new BookSource();
    src = bs;
    make();
    src.settlements.set(CUR, { slug: CUR, value: 1 });
    bs.bookState = "MARKET_STATE_OPEN";
    await toSettlePhase();
    await vi.advanceTimersByTimeAsync(90_000);
    expect(events.settled).toHaveLength(0);
    bs.bookState = "MARKET_STATE_EXPIRED";
    bs.bookFail = true;
    await vi.advanceTimersByTimeAsync(90_000);
    expect(events.settled).toHaveLength(0);
    bs.bookFail = false;
    bs.bookState = null; // 404
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    expect(events.settled[0]).toMatchObject({ id: CUR, result: "up" });
  });

  it("a confirmed 0.5 (void) gives no result and stops the settlement polling", async () => {
    src.settlements.set(CUR, { slug: CUR, value: 0.5 });
    await toSettlePhase();
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    const n = src.settleCalls.filter((s) => s === CUR).length;
    await vi.advanceTimersByTimeAsync(30 * 60_000);
    expect(src.settleCalls.filter((s) => s === CUR).length).toBe(n);
    expect(events.settled.filter((w) => w.id === CUR)).toHaveLength(0);
    expect(tracker.windowsList().find((w) => w.id === CUR)?.result).toBeNull();
  });

  it("restart recovery: re-adopts a stored unsettled window and settles it", async () => {
    const old = "btc-updown-15m-1792502100"; // 13:15-13:30Z, ended before this process started
    const stored: MarketWindow = {
      id: old,
      title: "Bitcoin Up or Down 15m",
      startTs: 1792502100_000,
      endTs: 1792502100_000 + 900_000,
      priceToBeat: 108000,
      upInstrument: `${old}:long`,
      downInstrument: `${old}:short`,
      status: "closed",
      result: null,
      settlePrice: null,
    };
    const settledAlready = { ...stored, id: "btc-updown-15m-1792501200", result: "up" as const };
    make({ unsettled: () => [stored, settledAlready] });
    src.settlements.set(old, { slug: old, value: 0 });
    tracker.start();
    await vi.advanceTimersByTimeAsync(6 * 60_000);
    expect(events.settled.map((w) => [w.id, w.result])).toEqual([[old, "down"]]);
    expect(src.settleCalls).not.toContain("btc-updown-15m-1792501200");
  });

  // ---------------------------------------------------------------- quotes / WS

  it("WS on a quiet book stays live (heartbeats count) without REST polling", async () => {
    const stream = new FakeStream();
    make({ stream });
    tracker.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(stream.slugs).toContain(CUR);
    stream.quote(CUR, 61, 63);
    src.bboCalls.length = 0;
    const states = new Set<string>();
    for (let i = 0; i < 100; i++) {
      await vi.advanceTimersByTimeAsync(100);
      if (i % 10 === 0) stream.emit("message"); // heartbeat every second, BBO unchanged
      states.add(tracker.status().state);
    }
    expect([...states]).toEqual(["live"]);
    expect(tracker.status().detail).toBe("ws");
    expect(src.bboCalls).toHaveLength(0);
    expect(tracker.lastOdds()).toMatchObject({ windowId: CUR, upBid: 61, upAsk: 63 });
  });

  it("WS connected but silent: REST takes over before the feed goes stale", async () => {
    const stream = new FakeStream();
    make({ stream });
    tracker.start();
    await vi.advanceTimersByTimeAsync(0);
    stream.quote(CUR, 61, 63);
    const states = new Set<string>();
    for (let i = 0; i < 100; i++) {
      await vi.advanceTimersByTimeAsync(100);
      states.add(tracker.status().state);
    }
    expect([...states]).toEqual(["live"]);
    expect(src.bboCalls.length).toBeGreaterThan(0);
  });

  it("surfaces a WS server error (e.g. 401 on the upgrade) in the status detail", async () => {
    const stream = new FakeStream();
    stream.connected = false;
    make({ stream });
    tracker.start();
    await vi.advanceTimersByTimeAsync(1000);
    stream.emit("serverError", "upgrade rejected: HTTP 401");
    expect(tracker.status()).toMatchObject({ state: "live", detail: "poll (ws: upgrade rejected: HTTP 401)" });
  });

  it("WS mode: the new window's cached odds are published at the rollover", async () => {
    const stream = new FakeStream();
    make({ stream });
    tracker.start();
    await vi.advanceTimersByTimeAsync(0);
    stream.quote(CUR, 97, 99);
    stream.quote(NEXT, 48, 50); // "next" is subscribed too
    const hb = setInterval(() => stream.emit("message"), 1000);
    await vi.advanceTimersByTimeAsync(BOUNDARY - NOW + 50);
    clearInterval(hb);
    const first = events.odds.find((o) => o.windowId === NEXT)!;
    expect(first).toMatchObject({ upBid: 48, upAsk: 50 });
    expect(first.ts).toBeLessThanOrEqual(BOUNDARY + 50);
    expect(tracker.lastOdds()?.windowId).toBe(NEXT);
  });

  it("an empty-book WS frame clears the old quote", async () => {
    const stream = new FakeStream();
    make({ stream });
    tracker.start();
    await vi.advanceTimersByTimeAsync(0);
    stream.quote(CUR, 61, 63);
    stream.emit("bbo", parseBbo({ marketDataLite: { marketSlug: CUR } }));
    expect(tracker.lastOdds()).toMatchObject({ windowId: CUR, upBid: null, upAsk: null, downBid: null, downAsk: null });
  });

  it("a poll in flight across the rollover never publishes an all-null snapshot for the new window", async () => {
    src.bboDelayMs = 500;
    tracker.start();
    await vi.advanceTimersByTimeAsync(BOUNDARY - NOW + 3000);
    const forNext = events.odds.filter((o) => o.windowId === NEXT);
    expect(forNext.length).toBeGreaterThan(0);
    for (const o of forNext) expect(o.upBid).not.toBeNull();
  });

  it("two-market window: a side whose BBO disappears is dropped, not frozen", async () => {
    src.search = (q) => (q.status === "upcoming" ? [] : [fx("event-two-markets.json").event]);
    tracker.start();
    await vi.advanceTimersByTimeAsync(1000);
    expect(tracker.lastOdds()).toMatchObject({ upBid: 61, downBid: 61 });
    src.bboNull.add("btc-15m-oct-20-1030am-down");
    await vi.advanceTimersByTimeAsync(1000);
    expect(tracker.lastOdds()).toMatchObject({ upBid: 61, downBid: null, downAsk: null });
  });
});
