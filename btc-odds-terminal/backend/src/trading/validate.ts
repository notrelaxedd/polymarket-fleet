// Request validation for the trading routes (zod). OWNER: trading-backend.

import { z } from "zod";
import type { BracketInput, ClosePositionRequest, OrderStatus, PlaceOrderRequest } from "../../../shared/protocol.ts";
import { zodError } from "../server/errors.ts";

/** Biggest single paper/live ticket accepted (USD); risk limits are normally far lower. */
export const MAX_ORDER_USD = 100_000;

/**
 * Smallest time exit (seconds before the window ends). The watcher runs every second and the exit refreshes the
 * book before it sells, so a smaller value (0 = at expiry) could never get out before trading closes.
 */
export const MIN_TIME_EXIT_SEC = 3;

const cents = z.number().finite().gt(0).lt(100);
const slippage = z.number().finite().min(0).max(99);

export const BracketInputSchema = z
  .object({
    tpCents: cents.nullable().optional(),
    slCents: cents.nullable().optional(),
    btcStop: z.number().finite().gt(0).nullable().optional(),
    timeExitSec: z
      .number()
      .finite()
      .min(MIN_TIME_EXIT_SEC, {
        message: `time exit must be at least ${MIN_TIME_EXIT_SEC} s before the window ends (the watcher checks once a second and the exit needs time to fill); turn it off with null`,
      })
      .max(3600)
      .nullable()
      .optional(),
    maxSlippageCents: slippage.nullable().optional(),
  })
  .refine((b) => b.tpCents == null || b.slCents == null || b.tpCents > b.slCents, {
    message: "take profit must be above stop loss",
    path: ["tpCents"],
  });

const normBracket = (b: z.infer<typeof BracketInputSchema>): BracketInput => ({
  tpCents: b.tpCents ?? null,
  slCents: b.slCents ?? null,
  btcStop: b.btcStop ?? null,
  timeExitSec: b.timeExitSec ?? null,
  maxSlippageCents: b.maxSlippageCents ?? null,
});

const PlaceOrderSchema = z
  .object({
    side: z.enum(["up", "down"]),
    type: z.enum(["limit", "marketable"]),
    usd: z.number().finite().gt(0).max(MAX_ORDER_USD),
    limitCents: cents.nullable().optional(),
    maxSlippageCents: slippage,
    bracket: BracketInputSchema.nullable().optional(),
    liveSession: z.string().max(200).nullable().optional(),
    clientOrderId: z.string().min(1).max(128).nullable().optional(),
  })
  .refine((o) => o.type !== "limit" || o.limitCents != null, {
    message: "limitCents is required for a limit order",
    path: ["limitCents"],
  });

const ClosePositionSchema = z
  .object({
    maxSlippageCents: slippage.nullable().optional(),
    liveSession: z.string().max(200).nullable().optional(),
  })
  .nullable()
  .optional();

export function parsePlaceOrder(body: unknown): PlaceOrderRequest {
  const r = PlaceOrderSchema.safeParse(body);
  if (!r.success) throw zodError(r.error.issues);
  const o = r.data;
  return {
    side: o.side,
    type: o.type,
    usd: o.usd,
    limitCents: o.limitCents ?? null,
    maxSlippageCents: o.maxSlippageCents,
    bracket: o.bracket ? normBracket(o.bracket) : null,
    liveSession: o.liveSession ?? null,
    clientOrderId: o.clientOrderId ?? null,
  };
}

/** null / undefined / every leg null = remove the bracket (returns null). */
export function parseBracket(body: unknown): BracketInput | null {
  if (body == null) return null;
  const r = BracketInputSchema.safeParse(body);
  if (!r.success) throw zodError(r.error.issues);
  return normBracket(r.data);
}

export function parseClose(body: unknown): ClosePositionRequest {
  const r = ClosePositionSchema.safeParse(body ?? undefined);
  if (!r.success) throw zodError(r.error.issues);
  return { maxSlippageCents: r.data?.maxSlippageCents ?? null, liveSession: r.data?.liveSession ?? null };
}

const ORDER_STATUSES = ["pending", "open", "partial", "filled", "cancelled", "rejected"] as const;
const intParam = (max: number) => z.coerce.number().int().min(1).max(max);

const OrdersQuery = z.object({
  limit: intParam(1000).optional(),
  windowId: z.string().min(1).max(300).optional(),
  status: z.enum(ORDER_STATUSES).optional(),
});
const FillsQuery = z.object({ limit: intParam(1000).optional(), windowId: z.string().min(1).max(300).optional() });
const PositionsQuery = z.object({ status: z.enum(["open", "all"]).optional(), limit: intParam(1000).optional() });
const BracketsQuery = z.object({ status: z.enum(["active", "all"]).optional() });

function parseQuery<S extends z.ZodTypeAny>(schema: S, q: unknown): z.infer<S> {
  const r = schema.safeParse(q ?? {});
  if (!r.success) throw zodError(r.error.issues);
  return r.data;
}

export const parseOrdersQuery = (q: unknown): { limit?: number; windowId?: string; status?: OrderStatus } =>
  parseQuery(OrdersQuery, q);
export const parseFillsQuery = (q: unknown): { limit?: number; windowId?: string } => parseQuery(FillsQuery, q);
export const parsePositionsQuery = (q: unknown): { status?: "open" | "all"; limit?: number } =>
  parseQuery(PositionsQuery, q);
export const parseBracketsQuery = (q: unknown): { status?: "active" | "all" } => parseQuery(BracketsQuery, q);
