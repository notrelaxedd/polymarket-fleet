import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { isBtc15mEvent, pickCurrentNext, slugEpochMs, windowsFromEvents } from "../src/polymarket/discovery.ts";

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
});
