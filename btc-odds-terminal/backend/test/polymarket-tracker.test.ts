import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FeedStatus, MarketWindow, OddsSnap } from "../../shared/protocol.ts";
import type { Logger } from "../src/log.ts";
import type { MarketDataSource, Query } from "../src/polymarket/client.ts";
import { parseBbo, type NormalizedSettlement } from "../src/polymarket/normalize.ts";
import { MarketTracker } from "../src/polymarket/tracker.ts";

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
  bboFail = false;
  settlements = new Map<string, NormalizedSettlement>();
  bboCalls: string[] = [];
  async listEvents(): Promise<Record<string, unknown>[]> {
    return [];
  }
  async searchEvents(q: Query): Promise<Record<string, unknown>[]> {
    return q.status === "upcoming" ? fx("search-upcoming.json").events : fx("search-active.json").events;
  }
  async getEventBySlug(): Promise<Record<string, unknown> | null> {
    return null;
  }
  async getBbo(slug: string) {
    this.bboCalls.push(slug);
    if (this.bboFail) throw new Error("boom");
    return parseBbo({ ...fx("bbo.json"), marketData: { ...fx("bbo.json").marketData, marketSlug: slug } }, slug);
  }
  async getSettlement(slug: string) {
    return this.settlements.get(slug) ?? null;
  }
}

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

  beforeEach(() => {
    vi.useFakeTimers({ now: NOW });
    src = new FakeSource();
    for (const k of Object.keys(events) as (keyof typeof events)[]) events[k].length = 0;
    tracker = new MarketTracker({
      logger: quiet,
      pollMs: 1000,
      client: src,
      config: { wsMode: "off" },
      priceAt: (ts) => (ts === BOUNDARY ? 108500.5 : null),
    });
    tracker.on("window", (w) => events.window.push(w));
    tracker.on("rollover", (r) => events.rollover.push(r));
    tracker.on("odds", (o) => events.odds.push(o));
    tracker.on("status", (s) => events.status.push(s));
    tracker.on("settled", (w) => events.settled.push(w));
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

    // settlement appears -> "window" + "settled"
    src.settlements.set(CUR, { slug: CUR, value: 1 });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(events.settled).toHaveLength(1);
    expect(events.settled[0]).toMatchObject({ id: CUR, result: "up", status: "settled" });
    expect(events.window.some((w) => w.id === CUR && w.result === "up")).toBe(true);
  });

  it("goes stale/down when BBO polling fails", async () => {
    tracker.start();
    await vi.advanceTimersByTimeAsync(1000);
    expect(tracker.status().state).toBe("live");
    src.bboFail = true;
    await vi.advanceTimersByTimeAsync(STALE_PLUS);
    expect(tracker.status().state).not.toBe("live");
    expect(events.status.at(-1)!.state).not.toBe("live");
    src.bboFail = false;
    await vi.advanceTimersByTimeAsync(1000);
    expect(tracker.status().state).toBe("live");
  });
});

const STALE_PLUS = 4000;
