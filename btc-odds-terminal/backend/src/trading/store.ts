// SQLite persistence for orders / fills / positions / brackets (tables from migration v2) plus the trading
// module's own tables (created IF NOT EXISTS, prefixed trading_). OWNER: trading-backend.
//
// Rows hold what the protocol types hold, except Position.markCents / unrealizedPnlUsd, which depend on the
// live quote and are filled in by the service when a position is read or broadcast.

import type Database from "better-sqlite3";
import type {
  Bracket,
  BracketInput,
  BracketStatus,
  Fill,
  Order,
  OrderAction,
  OrderReason,
  OrderStatus,
  OrderType,
  Position,
  PositionStatus,
  Side,
  TradingMode,
} from "../../../shared/protocol.ts";

/** Order statuses that are still working on the exchange / in the paper book. */
export const WORKING_STATUSES: readonly OrderStatus[] = ["pending", "open", "partial"];

/**
 * clientOrderId is an idempotency key: at most one order row per key. Partial, so the many orders without one
 * (exits, legacy rows) are unconstrained. Created separately: an old database holding duplicate keys must not
 * stop the module from starting (TradingStore.clientIdUnique reports it).
 */
const CLIENT_ID_INDEX =
  "CREATE UNIQUE INDEX IF NOT EXISTS trading_orders_client_id ON orders (client_order_id) WHERE client_order_id IS NOT NULL";

const SCHEMA = `
  -- realized P&L ledger: one row per realized amount (fee, exit P&L, settlement), so daily (ET) and per-window
  -- realized P&L can be summed exactly (positions only hold lifetime totals)
  CREATE TABLE IF NOT EXISTS trading_pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    day TEXT NOT NULL,               -- America/New_York day of ts, YYYY-MM-DD
    mode TEXT NOT NULL,
    window_id TEXT NOT NULL,
    position_id TEXT NOT NULL,
    kind TEXT NOT NULL,              -- fee | exit | settle | reconcile | void
    amount_usd REAL NOT NULL,
    fee_unknown INTEGER NOT NULL DEFAULT 0
  );
  CREATE INDEX IF NOT EXISTS trading_pnl_day ON trading_pnl (day, mode);
  CREATE INDEX IF NOT EXISTS trading_pnl_window ON trading_pnl (window_id, mode);

  -- per-order extras the Order type does not carry: the bracket requested with an entry that has not filled
  -- yet (resting limit), and the reference price at submit (slippage of later fills)
  CREATE TABLE IF NOT EXISTS trading_order_meta (
    order_id TEXT PRIMARY KEY,
    bracket_json TEXT,
    ref_cents REAL
  );
`;

interface OrderRow {
  id: string;
  client_order_id: string | null;
  external_id: string | null;
  mode: string;
  window_id: string;
  instrument: string;
  side: string;
  action: string;
  type: string;
  limit_cents: number | null;
  max_slippage_cents: number | null;
  qty: number;
  notional_usd: number | null;
  filled_qty: number;
  avg_fill_cents: number | null;
  fees_usd: number;
  status: string;
  reason: string;
  bracket_id: string | null;
  position_id: string | null;
  error: string | null;
  created_ts: number;
  updated_ts: number;
}

interface FillRow {
  id: string;
  order_id: string;
  external_id: string | null;
  mode: string;
  window_id: string;
  instrument: string;
  side: string;
  action: string;
  price_cents: number;
  qty: number;
  fee_usd: number | null;
  slippage_cents: number | null;
  ts: number;
}

interface PositionRow {
  id: string;
  mode: string;
  window_id: string;
  instrument: string;
  side: string;
  qty: number;
  avg_cents: number;
  cost_usd: number;
  realized_pnl_usd: number;
  fees_usd: number;
  status: string;
  bracket_id: string | null;
  opened_ts: number;
  updated_ts: number;
  closed_ts: number | null;
}

interface BracketRow {
  id: string;
  position_id: string;
  window_id: string;
  tp_cents: number | null;
  sl_cents: number | null;
  btc_stop: number | null;
  time_exit_sec: number | null;
  max_slippage_cents: number | null;
  status: string;
  trigger_reason: string | null;
  triggered_ts: number | null;
  exit_order_id: string | null;
  created_ts: number;
  updated_ts: number;
}

const toOrder = (r: OrderRow): Order => ({
  id: r.id,
  clientOrderId: r.client_order_id,
  externalId: r.external_id,
  mode: r.mode as TradingMode,
  windowId: r.window_id,
  instrument: r.instrument,
  side: r.side as Side,
  action: r.action as OrderAction,
  type: r.type as OrderType,
  limitCents: r.limit_cents,
  maxSlippageCents: r.max_slippage_cents,
  qty: r.qty,
  notionalUsd: r.notional_usd,
  filledQty: r.filled_qty,
  avgFillCents: r.avg_fill_cents,
  feesUsd: r.fees_usd,
  status: r.status as OrderStatus,
  reason: r.reason as OrderReason,
  bracketId: r.bracket_id,
  positionId: r.position_id,
  error: r.error,
  createdTs: r.created_ts,
  updatedTs: r.updated_ts,
});

const toFill = (r: FillRow): Fill => ({
  id: r.id,
  orderId: r.order_id,
  externalId: r.external_id,
  mode: r.mode as TradingMode,
  windowId: r.window_id,
  instrument: r.instrument,
  side: r.side as Side,
  action: r.action as OrderAction,
  priceCents: r.price_cents,
  qty: r.qty,
  feeUsd: r.fee_usd,
  slippageCents: r.slippage_cents,
  ts: r.ts,
});

const toPosition = (r: PositionRow): Position => ({
  id: r.id,
  mode: r.mode as TradingMode,
  windowId: r.window_id,
  instrument: r.instrument,
  side: r.side as Side,
  qty: r.qty,
  avgCents: r.avg_cents,
  costUsd: r.cost_usd,
  realizedPnlUsd: r.realized_pnl_usd,
  feesUsd: r.fees_usd,
  status: r.status as PositionStatus,
  markCents: null,
  unrealizedPnlUsd: null,
  bracketId: r.bracket_id,
  openedTs: r.opened_ts,
  updatedTs: r.updated_ts,
  closedTs: r.closed_ts,
});

const toBracket = (r: BracketRow): Bracket => ({
  id: r.id,
  positionId: r.position_id,
  windowId: r.window_id,
  tpCents: r.tp_cents,
  slCents: r.sl_cents,
  btcStop: r.btc_stop,
  timeExitSec: r.time_exit_sec,
  maxSlippageCents: r.max_slippage_cents,
  status: r.status as BracketStatus,
  triggerReason: (r.trigger_reason as OrderReason | null) ?? null,
  triggeredTs: r.triggered_ts,
  exitOrderId: r.exit_order_id,
  createdTs: r.created_ts,
  updatedTs: r.updated_ts,
});

export interface PnlEntry {
  ts: number;
  day: string;
  mode: TradingMode;
  windowId: string;
  positionId: string;
  /** void = a position closed at cost because its window never settled */
  kind: "fee" | "exit" | "settle" | "reconcile" | "void";
  amountUsd: number;
  feeUnknown?: boolean;
}

/** better-sqlite3 error for a UNIQUE constraint (the clientOrderId index). */
export function isUniqueViolation(err: unknown): boolean {
  const e = err as { code?: unknown; message?: unknown } | null;
  return e?.code === "SQLITE_CONSTRAINT_UNIQUE" || (typeof e?.message === "string" && e.message.includes("UNIQUE constraint failed"));
}

export class TradingStore {
  private readonly s;
  /** false when the clientOrderId UNIQUE index could not be created (pre-existing duplicate keys) */
  readonly clientIdUnique: boolean;

  constructor(private readonly db: Database.Database) {
    db.exec(SCHEMA);
    let unique = true;
    try {
      db.exec(CLIENT_ID_INDEX);
    } catch {
      unique = false;
    }
    this.clientIdUnique = unique;
    this.s = {
      insertOrder: db.prepare(
        `INSERT INTO orders (id, client_order_id, external_id, mode, window_id, instrument, side, action, type,
           limit_cents, max_slippage_cents, qty, notional_usd, filled_qty, avg_fill_cents, fees_usd, status, reason,
           bracket_id, position_id, error, created_ts, updated_ts)
         VALUES (@id, @clientOrderId, @externalId, @mode, @windowId, @instrument, @side, @action, @type,
           @limitCents, @maxSlippageCents, @qty, @notionalUsd, @filledQty, @avgFillCents, @feesUsd, @status, @reason,
           @bracketId, @positionId, @error, @createdTs, @updatedTs)`,
      ),
      updateOrder: db.prepare(
        `UPDATE orders SET external_id = @externalId, limit_cents = @limitCents, qty = @qty, filled_qty = @filledQty,
           avg_fill_cents = @avgFillCents, fees_usd = @feesUsd, status = @status, bracket_id = @bracketId,
           position_id = @positionId, error = @error, updated_ts = @updatedTs
         WHERE id = @id`,
      ),
      getOrder: db.prepare("SELECT * FROM orders WHERE id = ?"),
      orderByClientId: db.prepare("SELECT * FROM orders WHERE client_order_id = ? ORDER BY created_ts DESC LIMIT 1"),
      orderByExternalId: db.prepare("SELECT * FROM orders WHERE external_id = ?"),
      workingOrders: db.prepare(
        "SELECT * FROM orders WHERE status IN ('pending', 'open', 'partial') ORDER BY created_ts",
      ),
      workingSells: db.prepare(
        `SELECT * FROM orders WHERE position_id = ? AND action = 'sell' AND status IN ('pending', 'open', 'partial')
         ORDER BY created_ts`,
      ),
      lastEntryForPosition: db.prepare(
        "SELECT * FROM orders WHERE position_id = ? AND action = 'buy' ORDER BY created_ts DESC LIMIT 1",
      ),
      recentOrders: db.prepare(
        `SELECT * FROM orders WHERE created_ts >= ? OR status IN ('pending', 'open', 'partial')
         ORDER BY created_ts DESC, rowid DESC LIMIT ?`,
      ),

      insertFill: db.prepare(
        `INSERT INTO fills (id, order_id, external_id, mode, window_id, instrument, side, action, price_cents, qty,
           fee_usd, slippage_cents, ts)
         VALUES (@id, @orderId, @externalId, @mode, @windowId, @instrument, @side, @action, @priceCents, @qty,
           @feeUsd, @slippageCents, @ts)`,
      ),
      fillByExternalId: db.prepare("SELECT 1 FROM fills WHERE external_id = ? AND order_id = ? LIMIT 1"),
      fillsForOrder: db.prepare("SELECT * FROM fills WHERE order_id = ? ORDER BY ts, rowid"),
      recentFills: db.prepare("SELECT * FROM fills WHERE ts >= ? ORDER BY ts DESC, rowid DESC LIMIT ?"),

      insertPosition: db.prepare(
        `INSERT INTO positions (id, mode, window_id, instrument, side, qty, avg_cents, cost_usd, realized_pnl_usd,
           fees_usd, status, bracket_id, opened_ts, updated_ts, closed_ts)
         VALUES (@id, @mode, @windowId, @instrument, @side, @qty, @avgCents, @costUsd, @realizedPnlUsd, @feesUsd,
           @status, @bracketId, @openedTs, @updatedTs, @closedTs)`,
      ),
      updatePosition: db.prepare(
        `UPDATE positions SET qty = @qty, avg_cents = @avgCents, cost_usd = @costUsd, realized_pnl_usd = @realizedPnlUsd,
           fees_usd = @feesUsd, status = @status, bracket_id = @bracketId, updated_ts = @updatedTs, closed_ts = @closedTs
         WHERE id = @id`,
      ),
      getPosition: db.prepare("SELECT * FROM positions WHERE id = ?"),
      openPositionFor: db.prepare(
        `SELECT * FROM positions WHERE mode = ? AND window_id = ? AND side = ? AND status = 'open'
         ORDER BY opened_ts DESC LIMIT 1`,
      ),
      openPositions: db.prepare("SELECT * FROM positions WHERE status = 'open' ORDER BY opened_ts DESC"),
      recentPositions: db.prepare(
        `SELECT * FROM positions WHERE status = 'open' OR updated_ts >= ?
         ORDER BY opened_ts DESC, rowid DESC LIMIT ?`,
      ),

      insertBracket: db.prepare(
        `INSERT INTO brackets (id, position_id, window_id, tp_cents, sl_cents, btc_stop, time_exit_sec,
           max_slippage_cents, status, trigger_reason, triggered_ts, exit_order_id, created_ts, updated_ts)
         VALUES (@id, @positionId, @windowId, @tpCents, @slCents, @btcStop, @timeExitSec, @maxSlippageCents, @status,
           @triggerReason, @triggeredTs, @exitOrderId, @createdTs, @updatedTs)`,
      ),
      updateBracket: db.prepare(
        `UPDATE brackets SET tp_cents = @tpCents, sl_cents = @slCents, btc_stop = @btcStop, time_exit_sec = @timeExitSec,
           max_slippage_cents = @maxSlippageCents, status = @status, trigger_reason = @triggerReason,
           triggered_ts = @triggeredTs, exit_order_id = @exitOrderId, updated_ts = @updatedTs
         WHERE id = @id`,
      ),
      getBracket: db.prepare("SELECT * FROM brackets WHERE id = ?"),
      liveBrackets: db.prepare(
        "SELECT * FROM brackets WHERE status IN ('active', 'triggered') ORDER BY created_ts",
      ),
      recentBrackets: db.prepare(
        `SELECT * FROM brackets WHERE status IN ('active', 'triggered') OR updated_ts >= ?
         ORDER BY created_ts DESC, rowid DESC`,
      ),

      insertPnl: db.prepare(
        `INSERT INTO trading_pnl (ts, day, mode, window_id, position_id, kind, amount_usd, fee_unknown)
         VALUES (@ts, @day, @mode, @windowId, @positionId, @kind, @amountUsd, @feeUnknown)`,
      ),
      pnlForDay: db.prepare("SELECT COALESCE(SUM(amount_usd), 0) AS v FROM trading_pnl WHERE day = ? AND mode = ?"),
      pnlForWindow: db.prepare(
        "SELECT COALESCE(SUM(amount_usd), 0) AS v FROM trading_pnl WHERE window_id = ? AND mode = ?",
      ),

      getMeta: db.prepare("SELECT * FROM trading_order_meta WHERE order_id = ?"),
      upsertMeta: db.prepare(
        `INSERT INTO trading_order_meta (order_id, bracket_json, ref_cents) VALUES (@orderId, @bracketJson, @refCents)
         ON CONFLICT(order_id) DO UPDATE SET bracket_json = excluded.bracket_json, ref_cents = excluded.ref_cents`,
      ),
    };
  }

  /** Run fn in one SQLite transaction (nested calls join the outer one). */
  tx<T>(fn: () => T): T {
    if (this.db.inTransaction) return fn();
    return this.db.transaction(fn)();
  }

  // ---------------------------------------------------------------- orders

  insertOrder(o: Order): void {
    this.s.insertOrder.run(o);
  }

  updateOrder(o: Order): void {
    this.s.updateOrder.run(o);
  }

  order(id: string): Order | null {
    const r = this.s.getOrder.get(id) as OrderRow | undefined;
    return r ? toOrder(r) : null;
  }

  orderByClientId(clientOrderId: string): Order | null {
    const r = this.s.orderByClientId.get(clientOrderId) as OrderRow | undefined;
    return r ? toOrder(r) : null;
  }

  orderByExternalId(externalId: string): Order | null {
    const r = this.s.orderByExternalId.get(externalId) as OrderRow | undefined;
    return r ? toOrder(r) : null;
  }

  /** pending / open / partial orders, oldest first */
  workingOrders(): Order[] {
    return (this.s.workingOrders.all() as OrderRow[]).map(toOrder);
  }

  /** working (pending / open / partial) exit (sell) orders of a position */
  workingSells(positionId: string): Order[] {
    return (this.s.workingSells.all(positionId) as OrderRow[]).map(toOrder);
  }

  lastEntryOrder(positionId: string): Order | null {
    const r = this.s.lastEntryForPosition.get(positionId) as OrderRow | undefined;
    return r ? toOrder(r) : null;
  }

  orders(q: { limit: number; windowId?: string; status?: OrderStatus; sinceTs?: number }): Order[] {
    if (q.sinceTs != null && !q.windowId && !q.status) {
      return (this.s.recentOrders.all(q.sinceTs, q.limit) as OrderRow[]).map(toOrder);
    }
    const where: string[] = [];
    const args: unknown[] = [];
    if (q.windowId) {
      where.push("window_id = ?");
      args.push(q.windowId);
    }
    if (q.status) {
      where.push("status = ?");
      args.push(q.status);
    }
    if (q.sinceTs != null) {
      where.push("created_ts >= ?");
      args.push(q.sinceTs);
    }
    const sql = `SELECT * FROM orders ${where.length ? `WHERE ${where.join(" AND ")}` : ""} ORDER BY created_ts DESC, rowid DESC LIMIT ?`;
    return (this.db.prepare(sql).all(...args, q.limit) as OrderRow[]).map(toOrder);
  }

  // ---------------------------------------------------------------- fills

  insertFill(f: Fill): void {
    this.s.insertFill.run(f);
  }

  hasExternalFill(orderId: string, externalId: string): boolean {
    return this.s.fillByExternalId.get(externalId, orderId) !== undefined;
  }

  fillsForOrder(orderId: string): Fill[] {
    return (this.s.fillsForOrder.all(orderId) as FillRow[]).map(toFill);
  }

  fills(q: { limit: number; windowId?: string; sinceTs?: number }): Fill[] {
    if (!q.windowId) return (this.s.recentFills.all(q.sinceTs ?? 0, q.limit) as FillRow[]).map(toFill);
    const sql = `SELECT * FROM fills WHERE window_id = ? AND ts >= ? ORDER BY ts DESC, rowid DESC LIMIT ?`;
    return (this.db.prepare(sql).all(q.windowId, q.sinceTs ?? 0, q.limit) as FillRow[]).map(toFill);
  }

  // ---------------------------------------------------------------- positions

  insertPosition(p: Position): void {
    this.s.insertPosition.run(p);
  }

  updatePosition(p: Position): void {
    this.s.updatePosition.run(p);
  }

  position(id: string): Position | null {
    const r = this.s.getPosition.get(id) as PositionRow | undefined;
    return r ? toPosition(r) : null;
  }

  openPositionFor(mode: TradingMode, windowId: string, side: Side): Position | null {
    const r = this.s.openPositionFor.get(mode, windowId, side) as PositionRow | undefined;
    return r ? toPosition(r) : null;
  }

  openPositions(): Position[] {
    return (this.s.openPositions.all() as PositionRow[]).map(toPosition);
  }

  /** open positions plus positions updated since `sinceTs`, newest first */
  recentPositions(sinceTs: number, limit: number): Position[] {
    return (this.s.recentPositions.all(sinceTs, limit) as PositionRow[]).map(toPosition);
  }

  // ---------------------------------------------------------------- brackets

  insertBracket(b: Bracket): void {
    this.s.insertBracket.run(b);
  }

  updateBracket(b: Bracket): void {
    this.s.updateBracket.run(b);
  }

  bracket(id: string): Bracket | null {
    const r = this.s.getBracket.get(id) as BracketRow | undefined;
    return r ? toBracket(r) : null;
  }

  /** active + triggered brackets, oldest first */
  liveBrackets(): Bracket[] {
    return (this.s.liveBrackets.all() as BracketRow[]).map(toBracket);
  }

  /** active / triggered plus brackets changed since `sinceTs`, newest first */
  recentBrackets(sinceTs: number): Bracket[] {
    return (this.s.recentBrackets.all(sinceTs) as BracketRow[]).map(toBracket);
  }

  // ---------------------------------------------------------------- P&L ledger

  addPnl(e: PnlEntry): void {
    this.s.insertPnl.run({ ...e, feeUnknown: e.feeUnknown ? 1 : 0 });
  }

  realizedForDay(day: string, mode: TradingMode): number {
    return (this.s.pnlForDay.get(day, mode) as { v: number }).v;
  }

  realizedForWindow(windowId: string, mode: TradingMode): number {
    return (this.s.pnlForWindow.get(windowId, mode) as { v: number }).v;
  }

  // ---------------------------------------------------------------- order meta

  orderMeta(orderId: string): { bracket: BracketInput | null; refCents: number | null } {
    const r = this.s.getMeta.get(orderId) as { bracket_json: string | null; ref_cents: number | null } | undefined;
    if (!r) return { bracket: null, refCents: null };
    let bracket: BracketInput | null = null;
    if (r.bracket_json) {
      try {
        bracket = JSON.parse(r.bracket_json) as BracketInput;
      } catch {
        bracket = null;
      }
    }
    return { bracket, refCents: r.ref_cents };
  }

  setOrderMeta(orderId: string, meta: { bracket: BracketInput | null; refCents: number | null }): void {
    this.s.upsertMeta.run({
      orderId,
      bracketJson: meta.bracket ? JSON.stringify(meta.bracket) : null,
      refCents: meta.refCents,
    });
  }
}
