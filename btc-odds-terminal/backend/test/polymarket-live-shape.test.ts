import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { extractPriceToBeat, extractTimes, DEFAULT_DISCOVERY, isBtc15mEvent, pickCurrentNext, slugEpochMs, windowsFromEvents } from "../src/polymarket/discovery.ts";

// Real gateway response captured 2026-10-21 (see fixtures README).
const live = JSON.parse(readFileSync(path.join(__dirname, "fixtures/polymarket/search-live-2026-10-21.json"), "utf8"));
const NOW = Date.parse("2026-10-21T14:28:30Z");

describe("Polymarket US live response shape (2026-10-21)", () => {
  it("parses the UTC date-time token in the slug as the window start", () => {
    expect(slugEpochMs("btc-updown-15m-2026-10-21-1415z")).toBe(Date.parse("2026-10-21T14:15:00Z"));
    expect(slugEpochMs("cpc-btc-updown-15m-2026-10-21-1415z")).toBe(Date.parse("2026-10-21T14:15:00Z"));
  });

  it("recognizes the event as a BTC 15m up/down market", () => {
    expect(isBtc15mEvent(live.events[0], undefined, NOW)).toBe(true);
  });

  it("takes the window from the event, not the market's creation/close dates", () => {
    const ws = windowsFromEvents(live.events, NOW);
    expect(ws).toHaveLength(1);
    const w = ws[0]!.window;
    expect(w.id).toBe("cpc-btc-updown-15m-2026-10-21-1415z");
    expect(new Date(w.startTs).toISOString()).toBe("2026-10-21T14:15:00.000Z");
    expect(new Date(w.endTs).toISOString()).toBe("2026-10-21T14:30:00.000Z");
    expect(w.status).toBe("live");
    expect(w.priceToBeat).toBeNull(); // not in metadata: filled from the proxy's minute average
  });

  it("picks it as the current window", () => {
    const { current } = pickCurrentNext(windowsFromEvents(live.events, NOW), NOW);
    expect(current?.window.id).toBe("cpc-btc-updown-15m-2026-10-21-1415z");
  });

  it("reads the official price to beat and window from assetPriceTerms (seen live)", () => {
    const market = {
      slug: "cpc-btc-updown-15m-2026-10-21-1545z",
      status: "MARKET_STATUS_OPEN",
      startDate: "2026-10-21T03:46:01Z",
      endDate: "2026-10-21T16:30:00Z",
      assetPriceTerms: {
        marketType: "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
        asset: { assetClass: "ASSET_CLASS_CRYPTO", symbol: "btc" },
        indexSymbol: "BRTI",
        horizon: "15m",
        windowStart: "2026-10-21T15:45:00Z",
        windowEnd: "2026-10-21T16:00:00Z",
        priceToBeat: { value: "84422.25", currency: "USD" },
        strike: null,
      },
    };
    expect(extractPriceToBeat(market, null)).toBe(84422.25);
    const t = extractTimes(market, null, DEFAULT_DISCOVERY, NOW)!;
    expect(new Date(t.startTs).toISOString()).toBe("2026-10-21T15:45:00.000Z");
    expect(new Date(t.endTs).toISOString()).toBe("2026-10-21T16:00:00.000Z");
    // recognized from assetPriceTerms alone, whatever the title says
    expect(isBtc15mEvent({ slug: "x", title: "Something", markets: [market] }, undefined, NOW)).toBe(true);
    const ws = windowsFromEvents([{ slug: "x", title: "Something", markets: [market] }], NOW);
    expect(ws[0]!.ptbFromMetadata).toBe(true);
    expect(ws[0]!.window.priceToBeat).toBe(84422.25);
  });
});
