import type { Bar, BtcTick, FairSnap, MarketWindow, OddsSnap } from "../../shared/protocol.ts";
import type { TerminalDb, WriteBatch } from "./db/index.ts";
import type { Logger } from "./log.ts";

export interface RecorderOptions {
  logger?: Logger;
  /** buffer flush interval (default 1000 ms) */
  flushMs?: number;
  /** retention prune interval (default 1 h) */
  pruneMs?: number;
  /** flush early when this many rows are buffered (default 5000) */
  maxBuffered?: number;
}

/**
 * Buffers market-data rows in memory and writes them to SQLite in one transaction
 * every ~1s (instead of one statement per tick). Also runs hourly retention pruning.
 * Window upserts are buffered too, but the latest version per id wins.
 */
export class Recorder {
  private ticks: BtcTick[] = [];
  private bars: Bar[] = [];
  private odds: OddsSnap[] = [];
  private fair: FairSnap[] = [];
  private windows = new Map<string, MarketWindow>();
  private flushTimer: NodeJS.Timeout | null = null;
  private pruneTimer: NodeJS.Timeout | null = null;
  private readonly flushMs: number;
  private readonly pruneMs: number;
  private readonly maxBuffered: number;
  private readonly logger?: Logger;
  private failures = 0;

  constructor(
    private readonly db: TerminalDb,
    opts: RecorderOptions = {},
  ) {
    this.logger = opts.logger;
    this.flushMs = opts.flushMs ?? 1000;
    this.pruneMs = opts.pruneMs ?? 3_600_000;
    this.maxBuffered = opts.maxBuffered ?? 5000;
  }

  start(): void {
    if (this.flushTimer) return;
    this.flushTimer = setInterval(() => this.flush(), this.flushMs);
    this.flushTimer.unref();
    // prune once shortly after start, then hourly
    const first = setTimeout(() => this.prune(), 30_000);
    first.unref();
    this.pruneTimer = setInterval(() => this.prune(), this.pruneMs);
    this.pruneTimer.unref();
  }

  /** Stop timers and flush whatever is buffered. */
  stop(): void {
    if (this.flushTimer) clearInterval(this.flushTimer);
    if (this.pruneTimer) clearInterval(this.pruneTimer);
    this.flushTimer = this.pruneTimer = null;
    this.flush();
  }

  addTick(t: BtcTick): void {
    this.ticks.push(t);
    this.maybeEarlyFlush();
  }
  addBar(b: Bar): void {
    this.bars.push(b);
    this.maybeEarlyFlush();
  }
  addOdds(o: OddsSnap): void {
    this.odds.push(o);
    this.maybeEarlyFlush();
  }
  addFair(f: FairSnap): void {
    this.fair.push(f);
    this.maybeEarlyFlush();
  }
  addWindow(w: MarketWindow): void {
    this.windows.set(w.id, w);
  }

  pending(): number {
    return this.ticks.length + this.bars.length + this.odds.length + this.fair.length + this.windows.size;
  }

  /** Write all buffered rows in one transaction. Returns rows written. */
  flush(): number {
    const n = this.pending();
    if (n === 0) return 0;
    const batch: WriteBatch = {
      ticks: this.ticks,
      bars: this.bars,
      odds: this.odds,
      fair: this.fair,
      windows: [...this.windows.values()],
    };
    this.ticks = [];
    this.bars = [];
    this.odds = [];
    this.fair = [];
    this.windows = new Map();
    try {
      this.db.writeBatch(batch);
      this.failures = 0;
      return n;
    } catch (err) {
      this.failures++;
      this.logger?.error({ err, rows: n, failures: this.failures }, "recorder flush failed");
      // keep windows (small, important); drop market-data rows if we keep failing to avoid unbounded memory
      for (const w of batch.windows ?? []) if (!this.windows.has(w.id)) this.windows.set(w.id, w);
      if (this.failures < 5) {
        this.ticks = batch.ticks!.concat(this.ticks);
        this.bars = batch.bars!.concat(this.bars);
        this.odds = batch.odds!.concat(this.odds);
        this.fair = batch.fair!.concat(this.fair);
      }
      return 0;
    }
  }

  prune(now = Date.now()): void {
    try {
      const r = this.db.prune(now);
      if (r.ticks || r.bars || r.odds || r.fair) this.logger?.info(r, "pruned old rows");
    } catch (err) {
      this.logger?.error({ err }, "prune failed");
    }
  }

  private maybeEarlyFlush(): void {
    if (this.ticks.length + this.bars.length + this.odds.length + this.fair.length >= this.maxBuffered) this.flush();
  }
}
