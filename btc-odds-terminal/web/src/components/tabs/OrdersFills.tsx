"use client";
// Orders & fills table: one row per order (time, window, side, type/reason, limit, filled, avg, slippage vs the
// reference price and vs the limit, fees, status, cancel) with its fills expandable beneath. Follows the bottom-tab
// sample filter. Data: useOrders() + store.fills (snapshot + WS upserts) merged with GET /api/orders + /api/fills
// for the sample (useSampleTrades); a sample that is still not fully loaded is labelled, and an order whose fills
// are missing shows "—" for fees / slippage, never $0.00. cancel = api.cancelOrder(id). OWNER: tabs.
import * as React from "react";
import { ChevronRight, ClipboardList, X } from "lucide-react";
import type { Fill, Order } from "@shared/protocol";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { isApiError } from "@/lib/api-error";
import { etRangeShort, etTimeSec, quote, signedCents, usd } from "@/lib/format";
import { useOrders } from "@/lib/hooks";
import { liveNow, useTerminal } from "@/lib/store";
import { cn } from "@/lib/utils";
import { Empty } from "./Empty";
import { ModeTag, SideTag, StatusTag, Td, Th, etDayPrefix } from "./table-ui";
import {
  REASON_LABEL,
  TYPE_LABEL,
  cancellable,
  feeCell,
  fillSlippage,
  fillsByOrder,
  fillsMissing,
  mergeById,
  pageCovers,
  qtyText,
  scopeAnchor,
  slipVsLimit,
  summarizeOrders,
  windowsInScope,
} from "./table-math";
import { TRADES_FETCH_LIMIT, useSampleTrades } from "./use-sample-trades";

const slipClass = (v: number | null) => (v == null ? "text-muted" : v > 0.05 ? "text-down" : v < -0.05 ? "text-up" : "text-text-2");

export function OrdersFills() {
  const storeOrders = useOrders();
  const storeFills = useTerminal((s) => s.fills);
  const scope = useTerminal((s) => s.sampleScope);
  const current = useTerminal((s) => s.current);
  const windows = useTerminal((s) => s.windows);
  const replayActive = useTerminal((s) => s.replay.active);
  const [expanded, setExpanded] = React.useState<Record<string, boolean>>({});
  const [allOpen, setAllOpen] = React.useState(false);

  const anchor = scopeAnchor(current, windows, replayActive);
  const fetched = useSampleTrades(scope, anchor?.id ?? null);
  const allOrders = React.useMemo(
    () => mergeById(storeOrders, fetched.orders).sort((a, b) => b.createdTs - a.createdTs || (a.id < b.id ? 1 : -1)),
    [storeOrders, fetched.orders],
  );
  const fills = React.useMemo(() => mergeById(storeFills, fetched.fills), [storeFills, fetched.fills]);

  const scopeWindows = React.useMemo(
    () => (scope === "all" ? null : windowsInScope(scope, anchor, Object.values(windows), liveNow())),
    [scope, anchor, windows],
  );
  const scopeIds = React.useMemo(() => (scopeWindows ? new Set(scopeWindows.map((w) => w.id)) : null), [scopeWindows]);
  const orders = React.useMemo(() => (scopeIds ? allOrders.filter((o) => scopeIds.has(o.windowId)) : allOrders), [allOrders, scopeIds]);
  const byOrder = React.useMemo(() => fillsByOrder(fills), [fills]);
  const sum = React.useMemo(() => summarizeOrders(orders, byOrder), [orders, byOrder]);
  // is every order of the sample loaded? ("window": fetched by window id; else newest-first pages back to the
  // oldest window of the sample, or the whole history for "All")
  const complete = React.useMemo(() => {
    if (!fetched.loaded) return false;
    if (scope === "window") return fetched.orders.length < TRADES_FETCH_LIMIT;
    const since = scopeWindows?.length ? Math.min(...scopeWindows.map((w) => w.startTs)) : null;
    return pageCovers(
      fetched.orders.map((o) => o.createdTs),
      TRADES_FETCH_LIMIT,
      since,
    );
  }, [fetched.loaded, fetched.orders, scope, scopeWindows]);

  if (!orders.length) {
    return (
      <Empty
        icon={<ClipboardList size={20} />}
        title={allOrders.length ? "No orders in this sample" : "No orders yet"}
        hint={
          fetched.error ??
          (allOrders.length ? "Switch the sample filter to Last 20 or All to see older orders." : "Orders, fills, slippage and fees list here.")
        }
      >
        {fetched.error && (
          <Button size="sm" variant="outline" onClick={fetched.retry}>
            Retry
          </Button>
        )}
      </Empty>
    );
  }
  const now = liveNow();
  const fillsPartial = sum.fillsMissing > 0;
  const toggle = (id: string) => setExpanded((e) => ({ ...e, [id]: !(e[id] ?? allOpen) }));

  return (
    <div className="flex min-w-0 flex-col">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-1.5 text-[12px] text-muted">
        <span>
          <span className="num text-text-2">{sum.orders}</span> {complete ? "orders" : "loaded orders"} ·{" "}
          <span className="num text-text-2">{sum.fills}</span> {fillsPartial ? "loaded fills" : "fills"}
        </span>
        <span title={fillsPartial ? "Over the loaded fills only" : undefined}>
          Avg slippage <span className={cn("num", slipClass(sum.avgSlippage))}>{sum.avgSlippage == null ? "—" : signedCents(sum.avgSlippage, 2)}</span>
        </span>
        <span title={sum.feesUnknown ? "Fee schedule unconfirmed: some fees are unknown" : fillsPartial ? "Some orders' fills are not loaded" : undefined}>
          Fees{" "}
          <span className={cn("num", sum.feesUnknown ? "text-ptb" : "text-text-2")}>
            {sum.feesUnknown ? "[FEE]" : fillsPartial ? "—" : usd(sum.feesUsd)}
          </span>
        </span>
        {!complete && (
          <span role="status" className={cn("inline-flex items-center gap-1.5", fetched.error ? "text-ptb" : "text-muted")}>
            {fetched.error ?? (fetched.loading ? "Loading older orders…" : `Loaded orders only (newest ${TRADES_FETCH_LIMIT})`)}
            {fetched.error && (
              <button type="button" onClick={fetched.retry} className="touch-target font-semibold text-text-2 underline-offset-2 hover:text-text hover:underline">
                Retry
              </button>
            )}
          </span>
        )}
        {fillsPartial && (
          <span className="text-muted">
            <span className="num text-text-2">{sum.fillsMissing}</span> order{sum.fillsMissing === 1 ? "" : "s"} without loaded fills
          </span>
        )}
        <Button
          size="sm"
          variant="ghost"
          className="ml-auto"
          aria-pressed={allOpen}
          onClick={() => {
            setAllOpen((v) => !v);
            setExpanded({});
          }}
        >
          {allOpen ? "Hide fills" : "Show fills"}
        </Button>
      </div>
      <div className="min-w-0 overflow-x-auto">
        <table className="w-full min-w-[920px] border-separate border-spacing-0 text-left">
          <caption className="sr-only">Orders and their fills, newest first</caption>
          <thead>
            <tr>
              <Th className="w-[28px] px-1">
                <span className="sr-only">Fills</span>
              </Th>
              <Th>Time (ET)</Th>
              <Th>Window</Th>
              <Th>Side</Th>
              <Th>Order</Th>
              <Th align="right">Limit</Th>
              <Th align="right">Filled</Th>
              <Th align="right">Avg</Th>
              <Th align="right" title="Quantity-weighted fill price vs the best price at submit (positive = worse)">
                Slip
              </Th>
              <Th align="right" title="Average fill vs the order's limit (positive = worse than the limit)">
                vs limit
              </Th>
              <Th align="right">Fees</Th>
              <Th>Status</Th>
              <Th>
                <span className="sr-only">Actions</span>
              </Th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => {
              const fs = byOrder.get(o.id) ?? [];
              const open = fs.length > 0 && (expanded[o.id] ?? allOpen);
              return <OrderRows key={o.id} order={o} fills={fs} open={open} onToggle={() => toggle(o.id)} windowLabel={windowLabel(windows[o.windowId])} now={now} />;
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const windowLabel = (w: { startTs: number; endTs: number } | undefined) => (w ? etRangeShort(w.startTs, w.endTs) : "—");

function OrderRows({ order: o, fills, open, onToggle, windowLabel, now }: { order: Order; fills: Fill[]; open: boolean; onToggle: () => void; windowLabel: string; now: number }) {
  const [busy, setBusy] = React.useState(false);
  const fee = feeCell(o, fills);
  // fills not (all) loaded: their slippage is unknown, not the loaded subset's
  const slip = fillsMissing(o, fills) ? null : fillSlippage(fills);
  const vsLimit = slipVsLimit(o);
  const fillsId = `fills-${o.id}`;

  const cancel = async () => {
    setBusy(true);
    try {
      const res = await api.cancelOrder(o.id);
      useTerminal.getState().upsertOrders([res]);
    } catch (e) {
      useTerminal.getState().pushNotice({
        id: `cancel-${o.id}-${Date.now()}`,
        ts: Date.now(),
        level: "error",
        title: "Cancel failed",
        message: isApiError(e) ? e.message : "The order could not be cancelled.",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <tr className="hover:bg-surface-2/60">
        <Td className="px-1">
          {fills.length > 0 && (
            <button
              type="button"
              onClick={onToggle}
              aria-expanded={open}
              aria-controls={fillsId}
              aria-label={`${open ? "Hide" : "Show"} ${fills.length} fill${fills.length === 1 ? "" : "s"}`}
              className="touch-target inline-flex h-5 w-5 items-center justify-center rounded text-muted hover:bg-surface-2 hover:text-text"
            >
              <ChevronRight size={13} aria-hidden className={cn("transition-transform", open && "rotate-90")} />
            </button>
          )}
        </Td>
        <Td num className="text-text">
          {etDayPrefix(o.createdTs, now)}
          {etTimeSec(o.createdTs)}
        </Td>
        <Td num className="text-text-2">
          {windowLabel}
        </Td>
        <Td>
          <SideTag side={o.side} />
          <ModeTag live={o.mode === "live"} />
        </Td>
        <Td className="text-text-2">
          <span className="text-text">{o.action === "buy" ? "Buy" : "Sell"}</span> · {TYPE_LABEL[o.type]}
          {o.reason !== "manual" && (
            <span className={cn("ml-1.5 rounded-[4px] bg-surface-2 px-1 text-[11px] font-semibold", o.reason === "tp" ? "text-up" : o.reason === "sl" || o.reason === "kill" ? "text-down" : "text-ptb")}>
              {REASON_LABEL[o.reason]}
            </span>
          )}
        </Td>
        <Td num align="right" className="text-text-2">
          {o.limitCents == null ? "—" : `${quote(o.limitCents)}¢`}
        </Td>
        <Td num align="right" className="text-text">
          {qtyText(o.filledQty)}
          <span className="text-muted"> / {qtyText(o.qty)}</span>
        </Td>
        <Td num align="right" className="text-text">
          {o.avgFillCents == null ? "—" : `${quote(o.avgFillCents)}¢`}
        </Td>
        <Td num align="right" className={slipClass(slip)}>
          {slip == null ? "—" : signedCents(slip, 1)}
        </Td>
        <Td num align="right" className={slipClass(vsLimit)}>
          {vsLimit == null ? "—" : signedCents(vsLimit, 1)}
        </Td>
        <Td num align="right" className={fee.unknown ? "text-ptb" : fee.missing ? "text-muted" : "text-text-2"} title={fee.missing ? "Fills not loaded" : undefined}>
          {fee.unknown ? "[FEE]" : fee.missing || fee.usd == null ? "—" : usd(fee.usd)}
        </Td>
        <Td>
          <StatusTag status={o.status} title={o.error ?? undefined} />
          {o.status === "rejected" && o.error && <span className="ml-1.5 inline-block max-w-[180px] truncate align-middle text-[11px] text-down">{o.error}</span>}
        </Td>
        <Td align="right">
          {cancellable(o.status) && (
            <Button size="sm" variant="outline" disabled={busy} onClick={cancel} aria-label={`Cancel order ${o.side} ${qtyText(o.qty)} @ ${quote(o.limitCents)}¢`}>
              <X size={12} aria-hidden />
              Cancel
            </Button>
          )}
        </Td>
      </tr>
      {open &&
        fills.map((f, i) => (
          <tr key={f.id} id={i === 0 ? fillsId : undefined} className="bg-bg/60 text-[12px]">
            <Td className="px-1" />
            <Td num className="text-muted">
              ↳ {etTimeSec(f.ts)}
            </Td>
            <Td className="text-muted">Fill {i + 1}</Td>
            <Td />
            <Td className="text-muted">{f.action === "buy" ? "Bought" : "Sold"}</Td>
            <Td />
            <Td num align="right" className="text-text-2">
              {qtyText(f.qty)}
            </Td>
            <Td num align="right" className="text-text-2">
              {quote(f.priceCents)}¢
            </Td>
            <Td num align="right" className={slipClass(f.slippageCents)}>
              {f.slippageCents == null ? "—" : signedCents(f.slippageCents, 1)}
            </Td>
            <Td />
            <Td num align="right" className={f.feeUsd == null ? "text-ptb" : "text-text-2"}>
              {f.feeUsd == null ? "[FEE]" : usd(f.feeUsd)}
            </Td>
            <Td />
            <Td />
          </tr>
        ))}
    </>
  );
}
