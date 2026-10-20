"use client";
// Positions. OWNER: trading-ui. Header = today's realized P&L (store.risk). One card per open position: side
// tag, size @ avg, live P&L (marked to this side's bid every quote), chips (mark / TP / SL / BTC stop / time
// exit), Edit bracket / Close. Resting entry orders list above the cards with Cancel.
import * as React from "react";
import type { Bracket, Order, Position } from "@shared/protocol";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { useOpenPositions, useOrders } from "@/lib/hooks";
import { useLiveMarket } from "@/lib/replay";
import { liveNow, selectBracketFor, selectOdds, useTerminal } from "@/lib/store";
import { btc, quoteCents, signedUsd } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Card } from "./Card";
import { EditBracketDialog } from "./EditBracketDialog";
import { ValueChip } from "./fields";
import { closePosition, errorText } from "./actions";
import { sideQuote } from "./ticket-math";

const pnlTone = (v: number | null | undefined) => (v == null || Math.abs(v) < 0.005 ? "text-text-2" : v > 0 ? "text-up" : "text-down");

export function SideTag({ side, className }: { side: Position["side"]; className?: string }) {
  return (
    <span
      className={cn(
        "num inline-flex h-5 items-center rounded-[5px] border px-1.5 text-[11px] font-bold tracking-wider",
        side === "up" ? "border-up/50 bg-up-tint text-up" : "border-down/50 bg-down-tint text-down",
        className,
      )}
    >
      {side === "up" ? "UP" : "DOWN"}
    </span>
  );
}

function storedBracket(brackets: Record<string, Bracket>, positionId: string): Bracket | null {
  for (const b of Object.values(brackets)) if (b.positionId === positionId && b.status === "active") return b;
  return null;
}

/** whole seconds left until endTs on the LIVE clock (a replay moves serverNow(), not the real window) */
function useLiveSecondsLeft(endTs: number | null | undefined): number | null {
  const [sec, setSec] = React.useState<number | null>(null);
  React.useEffect(() => {
    if (endTs == null) {
      setSec(null);
      return;
    }
    const tick = () => setSec(Math.max(0, Math.ceil((endTs - liveNow()) / 1000)));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [endTs]);
  return sec;
}

function PositionCard({ p, secLeft, isCurrent }: { p: Position; secLeft: number | null; isCurrent: boolean }) {
  // the live market even during a replay: marks / P&L / bracket checks are about the real position
  const odds = useLiveMarket(selectOdds);
  const shown = useTerminal((s) => selectBracketFor(s, p.id));
  const stored = useTerminal((s) => storedBracket(s.brackets, p.id));
  // a replay shows recorded marks: a Close / bracket edit would hit the real market the user believes is past
  const replayActive = useTerminal((s) => s.replay.active);
  const [editing, setEditing] = React.useState(false);
  const [closing, setClosing] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  // mark to this side's bid on every quote of the position's window; otherwise the server's last mark
  const liveMark = isCurrent ? sideQuote(odds, p.side).bid : null;
  const mark = liveMark ?? p.markCents;
  const pnl = liveMark != null ? (p.qty * (liveMark - p.avgCents)) / 100 : p.unrealizedPnlUsd;
  const pnlPct = pnl != null && p.costUsd > 0 ? (pnl / p.costUsd) * 100 : null;
  const triggered = stored == null && p.bracketId != null;

  const onClose = async () => {
    setClosing(true);
    setError(null);
    try {
      // null slippage: the server uses the bracket's, else the entry order's cap
      await closePosition(p.mode, p.id, { maxSlippageCents: null });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setClosing(false);
    }
  };
  const closeEdit = React.useCallback(() => setEditing(false), []);
  const label = `${p.side === "up" ? "Up" : "Down"} ${p.qty.toFixed(1)} at ${quoteCents(p.avgCents)}`;
  const lockedWhy = replayActive ? "Exit replay to manage positions." : !isCurrent ? "This window has closed; the position settles at expiry." : undefined;

  return (
    <li aria-label={`Position ${label}`} className="rounded-ctl border border-border-strong bg-inset p-2.5">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <SideTag side={p.side} />
          <span className="num text-[13px] font-semibold text-text">
            {p.qty.toFixed(1)} <span className="font-normal text-muted">@</span> {quoteCents(p.avgCents)}
          </span>
          {p.mode === "live" && <span className="num rounded-[4px] bg-down-tint px-1 text-[10px] font-bold text-down">LIVE</span>}
        </div>
        <div className="text-right">
          <div className={cn("num text-[14px] font-semibold leading-[18px]", pnlTone(pnl))} aria-label={`Unrealized P and L ${signedUsd(pnl)}`}>
            {signedUsd(pnl)}
          </div>
          {pnlPct != null && <div className={cn("num text-[11px]", pnlTone(pnl))}>{`${pnlPct >= 0 ? "+" : "−"}${Math.abs(pnlPct).toFixed(1)}%`}</div>}
        </div>
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        <ValueChip k="Mark" v={quoteCents(mark)} tone="odds" />
        {shown?.tpCents != null && <ValueChip k="TP" v={quoteCents(shown.tpCents)} tone="up" />}
        {shown?.slCents != null && <ValueChip k="SL" v={quoteCents(shown.slCents)} tone="down" />}
        {shown?.btcStop != null && <ValueChip k={p.side === "up" ? "BTC <" : "BTC >"} v={`$${btc(shown.btcStop, 0)}`} />}
        {shown?.timeExitSec != null && <ValueChip k="Exit" v={`T−${shown.timeExitSec}s`} tone="ptb" />}
        {!shown && <ValueChip k="Bracket" v={triggered ? "triggered" : "none"} tone="muted" />}
        {!isCurrent && <ValueChip k="Window" v="settling" tone="ptb" />}
      </div>
      <div className="mt-2 flex gap-1.5">
        <Button
          size="sm"
          variant="outline"
          className="flex-1"
          onClick={() => setEditing(true)}
          disabled={replayActive || !isCurrent}
          title={lockedWhy}
          aria-label={`Edit bracket, ${label}`}
        >
          Edit bracket
        </Button>
        <Button
          size="sm"
          variant="danger"
          className="flex-1"
          onClick={onClose}
          disabled={closing || replayActive || !isCurrent}
          title={lockedWhy}
          aria-label={`Close position, ${label}`}
        >
          {closing ? "Closing…" : "Close"}
        </Button>
      </div>
      {replayActive && <p className="mt-1.5 text-[11px] leading-4 text-muted">Replay active: exit replay to close or edit.</p>}
      {error && (
        <p role="alert" className="mt-1.5 text-[11px] leading-4 text-down">
          {error}
        </p>
      )}
      <EditBracketDialog open={editing && !replayActive} onClose={closeEdit} position={p} bracket={stored} markCents={mark} secLeft={secLeft} />
    </li>
  );
}

function WorkingOrder({ o }: { o: Order }) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const upsertOrders = useTerminal((s) => s.upsertOrders);
  const cancel = async () => {
    setBusy(true);
    setError(null);
    try {
      upsertOrders([await api.cancelOrder(o.id)]);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const left = Math.max(0, o.qty - o.filledQty);
  // a live submit whose outcome is still being resolved (timeout / network): not a failure, not cancellable yet
  const confirming = o.status === "pending" && (o.externalId == null || o.externalId.startsWith("unconfirmed:"));
  return (
    <li className="flex flex-wrap items-center gap-2 rounded-ctl border border-dashed border-border-strong px-2.5 py-1.5 text-[12px]">
      <SideTag side={o.side} />
      <span className="num flex-1 text-text-2">
        {left.toFixed(1)} @ {quoteCents(o.limitCents)}{" "}
        {confirming ? (
          <span className="text-ptb" title={o.error ?? "Waiting for the exchange to confirm this order"}>
            confirming…
          </span>
        ) : (
          <span className="text-muted">resting</span>
        )}
      </span>
      <Button size="sm" variant="ghost" onClick={cancel} disabled={busy || confirming} aria-label={`Cancel resting order ${left.toFixed(1)} at ${quoteCents(o.limitCents)}`}>
        {busy ? "Cancelling…" : "Cancel"}
      </Button>
      {error && (
        <p role="alert" className="w-full text-[11px] text-down">
          {error}
        </p>
      )}
    </li>
  );
}

export function Positions() {
  const open = useOpenPositions();
  const orders = useOrders();
  const realized = useTerminal((s) => s.risk?.dailyRealizedPnlUsd ?? null);
  const current = useLiveMarket((m) => m.current);
  const secLeft = useLiveSecondsLeft(current?.endTs ?? null);
  const working = React.useMemo(
    () => orders.filter((o) => o.action === "buy" && (o.status === "open" || o.status === "partial" || o.status === "pending")),
    [orders],
  );

  return (
    <Card
      title="Positions"
      right={
        <span className="num text-[11px] text-muted">
          Today <span className={cn("font-semibold", pnlTone(realized))}>{signedUsd(realized ?? 0)}</span>
        </span>
      }
    >
      {open.length === 0 && working.length === 0 ? (
        <div className="rounded-ctl border border-dashed border-border-strong px-3 py-4 text-center text-[12px] text-muted">
          No open positions.
          <br />
          Place an order above; brackets and live P&amp;L show here.
        </div>
      ) : (
        <>
          {working.length > 0 && (
            <ul aria-label="Resting orders" className="mb-2 flex flex-col gap-1.5">
              {working.map((o) => (
                <WorkingOrder key={o.id} o={o} />
              ))}
            </ul>
          )}
          {open.length > 0 && (
            <ul aria-label="Open positions" className="flex flex-col gap-2">
              {open.map((p) => (
                <PositionCard key={p.id} p={p} isCurrent={p.windowId === current?.id} secLeft={secLeft} />
              ))}
            </ul>
          )}
        </>
      )}
    </Card>
  );
}
