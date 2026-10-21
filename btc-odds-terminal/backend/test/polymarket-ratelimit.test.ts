import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PolymarketClient, PolyHttpError } from "../src/polymarket/client.ts";
import { RateLimiter } from "../src/polymarket/ratelimit.ts";

describe("RateLimiter (shared Polymarket gateway budget)", () => {
  beforeEach(() => vi.useFakeTimers({ now: 1_000_000 }));
  afterEach(() => vi.useRealTimers());

  it("spaces requests by the minimum gap", async () => {
    const l = new RateLimiter(1000);
    const at: number[] = [];
    for (let i = 0; i < 3; i++) void l.acquire().then(() => at.push(Date.now()));
    await vi.advanceTimersByTimeAsync(5000);
    expect(at).toEqual([1_000_000, 1_001_000, 1_002_000]);
  });

  it("serves the BBO poll (high) before book/discovery (low)", async () => {
    const l = new RateLimiter(1000);
    const order: string[] = [];
    void l.acquire("low").then(() => order.push("first"));
    void l.acquire("low").then(() => order.push("low"));
    void l.acquire("high").then(() => order.push("high"));
    await vi.advanceTimersByTimeAsync(5000);
    expect(order).toEqual(["first", "high", "low"]);
  });

  it("a 429 pauses until Retry-After and doubles the gap; successes shrink it back", async () => {
    const l = new RateLimiter(1000);
    l.penalize(5000);
    expect(l.currentGapMs()).toBe(2000);
    const at: number[] = [];
    void l.acquire().then(() => at.push(Date.now()));
    void l.acquire().then(() => at.push(Date.now()));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(at).toEqual([1_005_000, 1_007_000]);
    for (let i = 0; i < 200; i++) l.relax();
    expect(l.currentGapMs()).toBe(1000);
  });

  it("the client reports a 429 clearly and backs its limiter off", async () => {
    const limiter = new RateLimiter(100);
    const fetchImpl = (async () =>
      new Response('{"title":"Error 1015: You are being rate limited"}', { status: 429 })) as unknown as typeof fetch;
    const c = new PolymarketClient({ baseUrl: "https://gateway.example", fetchImpl, limiter });
    const err = await c.getBbo("x").catch((e) => e);
    expect(err).toBeInstanceOf(PolyHttpError);
    expect((err as PolyHttpError).status).toBe(429);
    expect((err as Error).message).toMatch(/rate limited by Polymarket/);
    expect(limiter.currentGapMs()).toBe(200);
  });
});
