// PaperExecutor: simulates fills against the REAL Up/Down book (engine.refreshBook()). OWNER: trading-backend.
//
//   marketable buy   spend order.notionalUsd walking the asks up to limitCents (ask + max slippage); IOC.
//   marketable sell  sell order.qty walking the bids down to limitCents (bid - max slippage); IOC.
//   limit buy        take asks <= limitCents up to order.qty now (taker, at the book's prices); the rest
//                    rests and fills AT the limit when a later book crosses it (matchResting()), from liquidity
//                    that is new since the order last saw the book (no snapshot's size is used twice).
// Fees: feeCents() with Settings.feeCentsPerContract as the override; null = unknown ([FEE], P&L counts 0).

import type { BookLevels, Order } from "../../../shared/protocol.ts";
import { sideBook } from "../../../shared/paper.ts";
import type { ExecContext, ExecFill, ExecResult, Executor } from "../modules/types.ts";
import { feeCents } from "../polymarket/fees.ts";
import { freshCrossingQty, levelKey, round, walkBook } from "./fill.ts";

/** Book states paper (and the exchange) will trade in. null = state unknown (treated as open). */
export function bookTradeable(book: Pick<BookLevels, "state">): boolean {
  return book.state == null || book.state === "MARKET_STATE_OPEN";
}

export function paperFee(priceCents: number, qty: number, feeCentsPerContract: number | null): number | null {
  const c = feeCents(priceCents, qty, feeCentsPerContract);
  return c == null ? null : round(c / 100, 6);
}

/** Liquidity resting orders took from ONE book snapshot, per side then level (see freshCrossingQty). */
export type SnapshotUse = Map<Order["side"], Map<number, number>>;

export class PaperExecutor implements Executor {
  readonly mode = "paper" as const;
  /** resting order id -> crossing level sizes it last saw (its taker walk / last match consumed them) */
  private readonly seen = new Map<string, Map<number, number>>();

  constructor(private readonly now: () => number) {}

  async submit(order: Order, ctx: ExecContext): Promise<ExecResult> {
    return this.execute(order, ctx);
  }

  /** Synchronous core of submit() (tests and the watcher use it directly). */
  execute(order: Order, ctx: ExecContext): ExecResult {
    const book = ctx.book;
    if (!book || book.windowId !== order.windowId) return reject("no order book for this window");
    if (!bookTradeable(book)) return reject(`market not open (${book.state})`);
    if (order.limitCents == null) return reject("order has no limit price");
    const sb = sideBook(book, order.side);
    const levels = order.action === "buy" ? sb.asks : sb.bids;
    const ts = this.now();
    const remaining = round(order.qty - order.filledQty, 6);

    const walk =
      order.action === "buy" && order.type === "marketable"
        ? walkBook({ action: "buy", levels, limitCents: order.limitCents, budgetUsd: order.notionalUsd ?? 0 })
        : walkBook({ action: order.action, levels, limitCents: order.limitCents, qty: remaining });
    const fills: ExecFill[] = walk.executions.map((e) => ({
      externalId: null,
      priceCents: e.price,
      qty: e.size,
      feeUsd: paperFee(e.price, e.size, ctx.feeCentsPerContract),
      ts,
    }));

    if (order.type === "marketable") {
      // IOC: whatever did not fill within the cap is cancelled
      if (walk.stoppedBy === "done") return { externalId: null, status: "filled", fills };
      const side = order.side === "up" ? "Up" : "Down";
      const what = order.action === "buy" ? "ask" : "bid";
      const why =
        walk.stoppedBy === "limit"
          ? `no more ${side} ${what}s within the ${order.maxSlippageCents ?? 0}¢ slippage cap (limit ${order.limitCents}¢)`
          : `the ${side} book ran out of ${what}s`;
      return {
        externalId: null,
        status: "cancelled",
        fills,
        error: fills.length ? `partially filled; remainder cancelled: ${why}` : `nothing filled: ${why}`,
      };
    }
    // limit: what crossed now filled as a taker; the rest rests
    if (walk.stoppedBy === "done") return { externalId: null, status: "filled", fills };
    // the walk took every crossing level of this book: that size is not there for the resting remainder
    const base = new Map<number, number>();
    for (const l of levels) {
      const crosses = order.action === "buy" ? l.price <= order.limitCents : l.price >= order.limitCents;
      if (!crosses) break;
      if (l.size > 0) base.set(levelKey(l.price), l.size);
    }
    this.seen.set(order.id, base);
    return { externalId: null, status: fills.length ? "partial" : "open", fills };
  }

  async cancel(order: Order): Promise<void> {
    // nothing rests anywhere but in our own orders table
    this.seen.delete(order.id);
  }

  /** Drop the book baselines of orders that are no longer working. */
  prune(working: ReadonlySet<string>): void {
    for (const id of this.seen.keys()) if (!working.has(id)) this.seen.delete(id);
  }

  /**
   * A resting paper limit order against a new book snapshot: fills AT the limit price, from crossing size that
   * is new since the order last saw the book and not already taken by an older order from the same snapshot
   * (`use`, one per book event). null = nothing new crosses the limit.
   */
  matchResting(order: Order, book: BookLevels, feeCentsPerContract: number | null, use: SnapshotUse = new Map()): ExecFill | null {
    if (order.type !== "limit" || order.limitCents == null || order.mode !== "paper") return null;
    if (book.windowId !== order.windowId || !bookTradeable(book)) return null;
    const sb = sideBook(book, order.side);
    const levels = order.action === "buy" ? sb.asks : sb.bids;
    let used = use.get(order.side);
    if (!used) use.set(order.side, (used = new Map()));
    const m = freshCrossingQty(order.action, levels, order.limitCents, round(order.qty - order.filledQty, 6), this.seen.get(order.id) ?? new Map(), used);
    this.seen.set(order.id, m.seen);
    const q = m.qty;
    if (!(q > 0)) return null;
    return {
      externalId: null,
      priceCents: order.limitCents,
      qty: q,
      feeUsd: paperFee(order.limitCents, q, feeCentsPerContract),
      ts: this.now(),
    };
  }
}

const reject = (error: string): ExecResult => ({ externalId: null, status: "rejected", fills: [], error });
