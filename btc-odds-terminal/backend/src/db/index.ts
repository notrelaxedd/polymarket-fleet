import Database from "better-sqlite3";
import fs from "node:fs";
import path from "node:path";
import type {
  Bar,
  BtcTick,
  FairSnap,
  MarketWindow,
  OddsSnap,
  WindowResult,
  WindowStatus,
} from "../../../shared/protocol.ts";
import { migrate } from "./migrations.ts";

export { MIGRATIONS, SCHEMA_VERSION, migrate } from "./migrations.ts";

export const DAY_MS = 86_400_000;
/** Retention windows used by prune(). */
export const RETENTION = {
  ticksMs: 3 * DAY_MS,
  barsMs: 30 * DAY_MS,
  oddsMs: 30 * DAY_MS,
  fairMs: 30 * DAY_MS,
} as const;

export interface WriteBatch {
  ticks?: BtcTick[];
  bars?: Bar[];
  odds?: OddsSnap[];
  fair?: FairSnap[];
  windows?: MarketWindow[];
}

export interface PruneResult {
  ticks: number;
  bars: number;
  odds: number;
  fair: number;
}

interface WindowRow {
  id: string;
  title: string;
  start_ts: number;
  end_ts: number;
  ptb: number | null;
  up_instrument: string;
  down_instrument: string;
  status: string;
  result: string | null;
  settle_price: number | null;
  updated_ts: number;
}

interface OddsRow {
  ts: number;
  window_id: string;
  up_bid: number | null;
  up_ask: number | null;
  down_bid: number | null;
  down_ask: number | null;
  up_mid: number | null;
}

interface FairRow {
  ts: number;
  window_id: string;
  fair: number;
  sigma: number;
  t_left: number;
  edge: number | null;
}

interface TickRow {
  ts: number;
  price: number;
  coinbase: number | null;
  kraken: number | null;
  bitstamp: number | null;
}

function rowToWindow(r: WindowRow): MarketWindow {
  return {
    id: r.id,
    title: r.title,
    startTs: r.start_ts,
    endTs: r.end_ts,
    priceToBeat: r.ptb,
    upInstrument: r.up_instrument,
    downInstrument: r.down_instrument,
    status: r.status as WindowStatus,
    result: (r.result as WindowResult | null) ?? null,
    settlePrice: r.settle_price,
  };
}

function rowToOdds(r: OddsRow): OddsSnap {
  return {
    ts: r.ts,
    windowId: r.window_id,
    upBid: r.up_bid,
    upAsk: r.up_ask,
    downBid: r.down_bid,
    downAsk: r.down_ask,
    upMid: r.up_mid,
  };
}

function rowToFair(r: FairRow): FairSnap {
  return { ts: r.ts, windowId: r.window_id, fair: r.fair, sigma: r.sigma, tLeftSec: r.t_left, edge: r.edge };
}

function rowToTick(r: TickRow): BtcTick {
  const sources: BtcTick["sources"] = {};
  if (r.coinbase != null) sources.coinbase = r.coinbase;
  if (r.kraken != null) sources.kraken = r.kraken;
  if (r.bitstamp != null) sources.bitstamp = r.bitstamp;
  return { ts: r.ts, price: r.price, sources };
}

const finite = (n: number | null | undefined): number | null =>
  typeof n === "number" && Number.isFinite(n) ? n : null;

function prepareStatements(db: Database.Database) {
  return {
    insTick: db.prepare(
      "INSERT INTO ticks (ts, price, coinbase, kraken, bitstamp) VALUES (@ts, @price, @coinbase, @kraken, @bitstamp)",
    ),
    upsertBar: db.prepare(
      "INSERT OR REPLACE INTO bars (t, d, o, h, l, c) VALUES (@t, @d, @o, @h, @l, @c)",
    ),
    insBarIgnore: db.prepare("INSERT OR IGNORE INTO bars (t, d, o, h, l, c) VALUES (@t, @d, @o, @h, @l, @c)"),
    insOdds: db.prepare(
      `INSERT INTO odds (ts, window_id, up_bid, up_ask, down_bid, down_ask, up_mid)
       VALUES (@ts, @windowId, @upBid, @upAsk, @downBid, @downAsk, @upMid)`,
    ),
    insFair: db.prepare(
      `INSERT INTO fair (ts, window_id, fair, sigma, t_left, edge)
       VALUES (@ts, @windowId, @fair, @sigma, @tLeftSec, @edge)`,
    ),
    upsertWindow: db.prepare(
      `INSERT INTO windows (id, title, start_ts, end_ts, ptb, up_instrument, down_instrument, status, result, settle_price, updated_ts)
       VALUES (@id, @title, @startTs, @endTs, @priceToBeat, @upInstrument, @downInstrument, @status, @result, @settlePrice, @updatedTs)
       ON CONFLICT(id) DO UPDATE SET
         title = excluded.title,
         start_ts = excluded.start_ts,
         end_ts = excluded.end_ts,
         ptb = COALESCE(excluded.ptb, windows.ptb),
         up_instrument = excluded.up_instrument,
         down_instrument = excluded.down_instrument,
         status = CASE WHEN windows.status = 'settled' THEN 'settled' ELSE excluded.status END,
         result = COALESCE(excluded.result, windows.result),
         settle_price = COALESCE(excluded.settle_price, windows.settle_price),
         updated_ts = excluded.updated_ts`,
    ),
    // Live 1s bars win; a seeded 60s bar is returned only where no 1s bar falls inside its minute.
    barsRange: db.prepare(
      `SELECT t, d, o, h, l, c FROM bars b
       WHERE b.t >= ? AND b.t <= ?
         AND (b.d = 1 OR NOT EXISTS (
           SELECT 1 FROM bars x WHERE x.d = 1 AND x.t >= b.t AND x.t < b.t + b.d * 1000))
       ORDER BY b.t, b.d`,
    ),
    bars1sSince: db.prepare("SELECT t, d, o, h, l, c FROM bars WHERE d = 1 AND t >= ? ORDER BY t"),
    nearestBar1s: db.prepare(
      "SELECT t, d, o, h, l, c FROM bars WHERE d = 1 AND t >= ? AND t <= ? ORDER BY abs(t + 1000 - ?) LIMIT 1",
    ),
    countBars: db.prepare("SELECT count(*) AS n FROM bars WHERE d = ?"),
    ticksRange: db.prepare("SELECT ts, price, coinbase, kraken, bitstamp FROM ticks WHERE ts >= ? AND ts <= ? ORDER BY ts"),
    oddsRange: db.prepare("SELECT * FROM odds WHERE ts >= ? AND ts <= ? ORDER BY ts"),
    oddsRangeWin: db.prepare("SELECT * FROM odds WHERE window_id = ? AND ts >= ? AND ts <= ? ORDER BY ts"),
    fairRange: db.prepare("SELECT * FROM fair WHERE ts >= ? AND ts <= ? ORDER BY ts"),
    fairRangeWin: db.prepare("SELECT * FROM fair WHERE window_id = ? AND ts >= ? AND ts <= ? ORDER BY ts"),
    getWindow: db.prepare("SELECT * FROM windows WHERE id = ?"),
    listWindows: db.prepare("SELECT * FROM windows ORDER BY start_ts DESC LIMIT ?"),
    recentFinished: db.prepare(
      `SELECT * FROM windows WHERE status IN ('closed', 'settled') OR end_ts <= ?
       ORDER BY end_ts DESC LIMIT ?`,
    ),
    getSetting: db.prepare("SELECT value FROM settings WHERE key = ?"),
    setSetting: db.prepare(
      "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
    ),
    allSettings: db.prepare("SELECT key, value FROM settings"),
    pruneTicks: db.prepare("DELETE FROM ticks WHERE ts < ?"),
    pruneBars: db.prepare("DELETE FROM bars WHERE t < ?"),
    pruneOdds: db.prepare("DELETE FROM odds WHERE ts < ?"),
    pruneFair: db.prepare("DELETE FROM fair WHERE ts < ?"),
  };
}
type Statements = ReturnType<typeof prepareStatements>;

/** Typed repository over the terminal's SQLite database. */
export class TerminalDb {
  readonly raw: Database.Database;
  readonly schemaVersion: number;

  private readonly s: Statements;

  constructor(raw: Database.Database) {
    this.raw = raw;
    this.schemaVersion = migrate(raw);
    this.s = prepareStatements(raw);
  }


  // ---------------------------------------------------------------- writes

  /** Write a batch of buffered rows in a single transaction. */
  writeBatch(batch: WriteBatch, now = Date.now()): void {
    const s = this.s;
    this.raw.transaction(() => {
      for (const t of batch.ticks ?? []) {
        s.insTick.run({
          ts: t.ts,
          price: t.price,
          coinbase: finite(t.sources.coinbase),
          kraken: finite(t.sources.kraken),
          bitstamp: finite(t.sources.bitstamp),
        });
      }
      for (const b of batch.bars ?? []) s.upsertBar.run(b);
      for (const o of batch.odds ?? []) s.insOdds.run(o);
      for (const f of batch.fair ?? []) s.insFair.run(f);
      for (const w of batch.windows ?? []) s.upsertWindow.run({ ...w, updatedTs: now });
    })();
  }

  insertTicks(ticks: BtcTick[]): void {
    this.writeBatch({ ticks });
  }
  upsertBars(bars: Bar[]): void {
    this.writeBatch({ bars });
  }
  insertOdds(odds: OddsSnap[]): void {
    this.writeBatch({ odds });
  }
  insertFair(fair: FairSnap[]): void {
    this.writeBatch({ fair });
  }
  upsertWindow(w: MarketWindow, now = Date.now()): void {
    this.writeBatch({ windows: [w] }, now);
  }

  /** INSERT OR IGNORE (used by the seed script). Returns the number of rows actually inserted. */
  insertBarsIgnore(bars: Bar[]): number {
    let n = 0;
    this.raw.transaction(() => {
      for (const b of bars) n += this.s.insBarIgnore.run(b).changes;
    })();
    return n;
  }

  // ---------------------------------------------------------------- reads

  /** Bars in [from, to] ascending. 1s live bars plus 60s seeded bars for minutes without live coverage. */
  bars(from: number, to: number): Bar[] {
    return this.s.barsRange.all(from, to) as Bar[];
  }

  /** 1s bars with t >= since, ascending (vol warm-up). */
  bars1sSince(since: number): Bar[] {
    return this.s.bars1sSince.all(since) as Bar[];
  }

  /** The 1s bar whose close time (t+1s) is nearest ts, within ±tolMs. */
  nearestBar1s(ts: number, tolMs = 10_000): Bar | null {
    return (this.s.nearestBar1s.get(ts - 1000 - tolMs, ts - 1000 + tolMs, ts) as Bar | undefined) ?? null;
  }

  countBars(d: number): number {
    return (this.s.countBars.get(d) as { n: number }).n;
  }

  ticks(from: number, to: number): BtcTick[] {
    return (this.s.ticksRange.all(from, to) as TickRow[]).map(rowToTick);
  }

  odds(from: number, to: number, windowId?: string): OddsSnap[] {
    const rows = windowId
      ? (this.s.oddsRangeWin.all(windowId, from, to) as OddsRow[])
      : (this.s.oddsRange.all(from, to) as OddsRow[]);
    return rows.map(rowToOdds);
  }

  fair(from: number, to: number, windowId?: string): FairSnap[] {
    const rows = windowId
      ? (this.s.fairRangeWin.all(windowId, from, to) as FairRow[])
      : (this.s.fairRange.all(from, to) as FairRow[]);
    return rows.map(rowToFair);
  }

  getWindow(id: string): MarketWindow | null {
    const r = this.s.getWindow.get(id) as WindowRow | undefined;
    return r ? rowToWindow(r) : null;
  }

  /** All windows, newest (by start) first. */
  listWindows(limit = 50): MarketWindow[] {
    return (this.s.listWindows.all(limit) as WindowRow[]).map(rowToWindow);
  }

  /** Finished windows (closed/settled or past end), newest first. */
  recentFinishedWindows(limit = 20, now = Date.now()): MarketWindow[] {
    return (this.s.recentFinished.all(now, limit) as WindowRow[]).map(rowToWindow);
  }

  // ---------------------------------------------------------------- settings

  getSetting<T = unknown>(key: string): T | undefined {
    const r = this.s.getSetting.get(key) as { value: string } | undefined;
    if (!r) return undefined;
    try {
      return JSON.parse(r.value) as T;
    } catch {
      return undefined;
    }
  }

  setSetting(key: string, value: unknown): void {
    this.s.setSetting.run(key, JSON.stringify(value ?? null));
  }

  allSettings(): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const r of this.s.allSettings.all() as { key: string; value: string }[]) {
      try {
        out[r.key] = JSON.parse(r.value);
      } catch {
        /* skip corrupt value */
      }
    }
    return out;
  }

  // ---------------------------------------------------------------- maintenance

  prune(now = Date.now()): PruneResult {
    return this.raw.transaction(() => ({
      ticks: this.s.pruneTicks.run(now - RETENTION.ticksMs).changes,
      bars: this.s.pruneBars.run(now - RETENTION.barsMs).changes,
      odds: this.s.pruneOdds.run(now - RETENTION.oddsMs).changes,
      fair: this.s.pruneFair.run(now - RETENTION.fairMs).changes,
    }))();
  }

  close(): void {
    if (this.raw.open) {
      try {
        this.raw.pragma("wal_checkpoint(TRUNCATE)");
      } catch {
        /* in-memory or read-only */
      }
      this.raw.close();
    }
  }
}

/** Open (creating dirs as needed) and migrate the database. Use ":memory:" for tests. */
export function openDb(dbPath: string): TerminalDb {
  if (dbPath !== ":memory:" && !dbPath.startsWith("file:")) {
    fs.mkdirSync(path.dirname(path.resolve(dbPath)), { recursive: true });
  }
  const raw = new Database(dbPath);
  raw.pragma("journal_mode = WAL");
  raw.pragma("synchronous = NORMAL");
  raw.pragma("foreign_keys = ON");
  raw.pragma("busy_timeout = 5000");
  return new TerminalDb(raw);
}
