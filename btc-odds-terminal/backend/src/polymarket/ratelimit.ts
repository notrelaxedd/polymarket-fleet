// One request budget for the Polymarket US public gateway, shared by every client in the process.
// Seen live 2026-10-21: polling BBO + book at 1 Hz each (plus discovery) trips Cloudflare's
// rate limit (HTTP 429, "Error 1015"), and the feed flaps stale for ~10 s at a time. Requests are
// spaced by a minimum gap; "high" (the BBO poll) goes before "low" (book, discovery, settlement).
// A 429 doubles the gap and pauses until Retry-After; each success shrinks the gap back by 1%.

export type Priority = "high" | "low";

export class RateLimiter {
  private gapMs: number;
  private nextAt = 0;
  private blockedUntil = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly queues: Record<Priority, Array<() => void>> = { high: [], low: [] };

  constructor(
    private readonly baseGapMs: number,
    private readonly maxGapMs = 8_000,
  ) {
    this.gapMs = baseGapMs;
  }

  /** Resolves when this request may go out. */
  acquire(priority: Priority = "low"): Promise<void> {
    return new Promise((resolve) => {
      this.queues[priority].push(resolve);
      this.pump();
    });
  }

  /** The gateway said 429: back off. */
  penalize(retryAfterMs: number | null, now = Date.now()): void {
    this.gapMs = Math.min(this.maxGapMs, this.gapMs * 2);
    this.blockedUntil = Math.max(this.blockedUntil, now + (retryAfterMs ?? 10_000));
  }

  /** A request succeeded: creep back toward the base rate. */
  relax(): void {
    this.gapMs = Math.max(this.baseGapMs, this.gapMs * 0.99);
  }

  currentGapMs(): number {
    return this.gapMs;
  }

  private pump(): void {
    if (this.timer) return;
    if (this.queues.high.length === 0 && this.queues.low.length === 0) return;
    const now = Date.now();
    const at = Math.max(this.nextAt, this.blockedUntil);
    if (now < at) {
      this.timer = setTimeout(() => {
        this.timer = null;
        this.pump();
      }, at - now);
      return;
    }
    const go = this.queues.high.shift() ?? this.queues.low.shift()!;
    this.nextAt = now + this.gapMs;
    go();
    this.pump();
  }
}

const shared = new Map<string, RateLimiter>();

/** The process-wide limiter for a gateway host. */
export function gatewayLimiter(baseUrl: string, minGapMs: number): RateLimiter {
  const key = new URL(baseUrl).host;
  let l = shared.get(key);
  if (!l) {
    l = new RateLimiter(minGapMs);
    shared.set(key, l);
  }
  return l;
}
