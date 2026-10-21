import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Logger } from "../src/log.ts";
import type { MarketDataSource, Query } from "../src/polymarket/client.ts";
import { eventSlugFor, eventSlugsAround } from "../src/polymarket/discovery.ts";
import { DEFAULT_EVENT_SLUG_TEMPLATE } from "../src/polymarket/config.ts";
import { parseBbo, type NormalizedBbo } from "../src/polymarket/normalize.ts";
import { MarketTracker } from "../src/polymarket/tracker.ts";

const FIX = path.join(__dirname, "fixtures", "polymarket");
const live = JSON.parse(readFileSync(path.join(FIX, "search-live-2026-10-21.json"), "utf8")).events[0];
const bbo = JSON.parse(readFileSync(path.join(FIX, "bbo.json"), "utf8"));
const W = 15 * 60_000;
const quiet = { info: () => {}, warn: () => {}, error: () => {}, debug: () => {}, child: () => quiet } as unknown as Logger;

/** The live event shape, re-dated to the window that starts at `start` (as Polymarket US names it). */
function liveEventAt(start: number): Record<string, unknown> {
  const iso = (t: number) => new Date(t).toISOString().replace(".000Z", "Z");
  const slug = eventSlugFor(DEFAULT_EVENT_SLUG_TEMPLATE, start);
  return {
    ...live,
    slug,
    ticker: slug,
    startDate: iso(start),
    startTime: iso(start),
    endDate: iso(start + W),
    markets: [
      {
        ...live.markets[0],
        slug: `cpc-${slug}`,
        startDate: iso(start - 12 * 3600_000), // creation time, like the real data
        endDate: iso(start + W + 30 * 60_000), // window end + 30 min, like the real data
        gameStartTime: iso(start),
      },
    ],
  };
}

/** Search finds nothing (the failure seen live); only the by-slug lookup works. */
class SlugOnlySource implements MarketDataSource {
  slugCalls: string[] = [];
  async listEvents(): Promise<Record<string, unknown>[]> {
    return [];
  }
  async searchEvents(_q: Query): Promise<Record<string, unknown>[]> {
    return [];
  }
  async getEventBySlug(slug: string): Promise<Record<string, unknown> | null> {
    this.slugCalls.push(slug);
    const m = /-(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})z$/.exec(slug);
    return m ? liveEventAt(Date.UTC(+m[1]!, +m[2]! - 1, +m[3]!, +m[4]!, +m[5]!)) : null;
  }
  async getBbo(slug: string): Promise<NormalizedBbo | null> {
    return parseBbo({ ...bbo, marketData: { ...bbo.marketData, marketSlug: slug } }, slug);
  }
  async getSettlement() {
    return null;
  }
}

describe("discovery by predictable event slug", () => {
  let tracker: MarketTracker | null = null;
  afterEach(() => {
    tracker?.stop();
    vi.useRealTimers();
  });

  it("builds the slugs around now in UTC", () => {
    const now = Date.parse("2026-10-21T14:28:30Z");
    expect(eventSlugsAround(DEFAULT_EVENT_SLUG_TEMPLATE, now, W)).toEqual([
      "btc-updown-15m-2026-10-21-1400z",
      "btc-updown-15m-2026-10-21-1415z",
      "btc-updown-15m-2026-10-21-1430z",
      "btc-updown-15m-2026-10-21-1445z",
    ]);
  });

  it("finds the live window and rolls over to the next one when search returns nothing", async () => {
    vi.useFakeTimers({ now: Date.parse("2026-10-21T14:28:30Z") });
    const src = new SlugOnlySource();
    tracker = new MarketTracker({ logger: quiet, pollMs: 1000, client: src, config: { wsMode: "off" }, priceAt: () => 67000 });
    tracker.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(tracker.current()?.id).toBe("cpc-btc-updown-15m-2026-10-21-1415z");
    expect(tracker.next()?.id).toBe("cpc-btc-updown-15m-2026-10-21-1430z");
    await vi.advanceTimersByTimeAsync(1000);
    expect(tracker.lastOdds()?.windowId).toBe("cpc-btc-updown-15m-2026-10-21-1415z");

    await vi.advanceTimersByTimeAsync(Date.parse("2026-10-21T14:30:00Z") - Date.now() + 2000);
    expect(tracker.current()?.id).toBe("cpc-btc-updown-15m-2026-10-21-1430z");
    expect(tracker.lastOdds()?.windowId).toBe("cpc-btc-updown-15m-2026-10-21-1430z");
    expect(tracker.current()?.priceToBeat).toBe(67000);

    // two windows later it is still tracking (discovery keeps looking ahead by slug)
    await vi.advanceTimersByTimeAsync(2 * W);
    expect(tracker.current()?.id).toBe("cpc-btc-updown-15m-2026-10-21-1500z");
    expect(tracker.status().state).toBe("live");
  });
});
