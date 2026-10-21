"use client";
// Order ticket. OWNER: trading-ui. The draft lives in store.ticket so the chart's pre-entry TP/SL lines
// (chart/positions) share it. Estimates walk the selected side's book (ticket-math.estimateTicket); submit goes
// through actions.submitOrder (LiveConfirmDialog on the first live order of the browser session).
import * as React from "react";
import type { OrderType, PlaceOrderResponse, Side } from "@shared/protocol";
import { Segmented } from "@/components/ui/segmented";
import { liveNow, useTerminal } from "@/lib/store";
import { quoteCents, signedCents, signedUsd, usd } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useCountdownMs, useFair, useOdds } from "@/lib/hooks";
import { Card, SumRow } from "./Card";
import { BracketRows } from "./BracketRows";
import { Check, NumInput } from "./fields";
import { CancelledError, errorText, newClientOrderId, refreshOrders, submitOrder } from "./actions";
import { OrderKey, isAmbiguousFailure } from "./order-safety";
import {
  QUICK_AMOUNTS,
  blockReason,
  buildOrderRequest,
  estimateTicket,
  parseNum,
  resuggestBracket,
  sideQuote,
  suggestBracket,
  validateTicket,
  type BracketBasis,
  type BracketFields,
  type TicketErrors,
} from "./ticket-math";

const RESULT_MS = 7000;

function resultText(r: PlaceOrderResponse): { tone: "ok" | "warn" | "error"; text: string } {
  const o = r.order;
  const side = o.side === "up" ? "Up" : "Down";
  const avg = o.avgFillCents != null ? quoteCents(o.avgFillCents) : null;
  switch (o.status) {
    case "filled":
      return { tone: "ok", text: `Filled ${o.filledQty.toFixed(1)} ${side} @ ${avg}${r.bracket ? " · bracket armed" : ""}` };
    case "partial":
      return { tone: "warn", text: `Partly filled ${o.filledQty.toFixed(1)} of ${o.qty.toFixed(1)} ${side} @ ${avg}; the rest rests at ${quoteCents(o.limitCents)}` };
    case "open":
    case "pending":
      return { tone: "ok", text: `Resting: ${o.qty.toFixed(1)} ${side} @ ${quoteCents(o.limitCents)}` };
    case "cancelled":
      return o.filledQty > 0
        ? { tone: "warn", text: `Filled ${o.filledQty.toFixed(1)} of ${o.qty.toFixed(1)} ${side} @ ${avg}; the rest was beyond the slippage cap` }
        : { tone: "warn", text: "Nothing filled within the slippage cap." };
    case "rejected":
      return { tone: "error", text: o.error ?? "Order rejected." };
  }
}

export function OrderTicket() {
  const odds = useOdds();
  const fairSnap = useFair();
  const current = useTerminal((s) => s.current);
  const bookRaw = useTerminal((s) => s.book);
  const mode = useTerminal((s) => s.settings?.mode ?? "paper");
  const fee = useTerminal((s) => s.settings?.feeCentsPerContract ?? null);
  const limits = useTerminal((s) => s.risk?.limits ?? s.settings?.risk ?? null);
  const risk = useTerminal((s) => s.risk);
  const replayActive = useTerminal((s) => s.replay.active);
  const btcPrice = useTerminal((s) => s.lastTick?.price ?? null);
  const ticket = useTerminal((s) => s.ticket);
  const setTicket = useTerminal((s) => s.setTicket);
  const msLeft = useCountdownMs(current?.endTs ?? null, 1000);

  const [busy, setBusy] = React.useState(false);
  const [attempted, setAttempted] = React.useState(false);
  const [result, setResult] = React.useState<{ tone: "ok" | "warn" | "error"; text: string } | null>(null);
  React.useEffect(() => {
    if (!result) return;
    const t = setTimeout(() => setResult(null), RESULT_MS);
    return () => clearTimeout(t);
  }, [result]);

  const book = bookRaw && current && bookRaw.windowId === current.id ? bookRaw : null;
  const { side, type } = ticket;
  const est = React.useMemo(
    () =>
      estimateTicket({
        side,
        type,
        usd: parseNum(ticket.usd),
        limitCents: parseNum(ticket.limitCents),
        maxSlippageCents: parseNum(ticket.maxSlippageCents),
        book,
        odds,
        fairUp: fairSnap?.fair ?? null,
        feeCentsPerContract: fee,
      }),
    [side, type, ticket.usd, ticket.limitCents, ticket.maxSlippageCents, book, odds, fairSnap?.fair, fee],
  );
  const secLeft = msLeft == null ? null : msLeft / 1000;
  const errors: TicketErrors = validateTicket(ticket, { est, btc: btcPrice, secLeft, limits });
  const hasErrors = Object.keys(errors).length > 0;
  const block = blockReason({
    replayActive,
    hasWindow: !!current && current.status === "live",
    hasQuote: sideQuote(odds, side).ask != null || (type === "limit" && parseNum(ticket.limitCents) != null),
    risk,
    now: liveNow(),
  });
  const modeLabel = mode === "live" ? "Live" : "Paper";
  const sideLabel = side === "up" ? "Up" : "Down";
  // an emptied amount field only complains after a submit attempt
  const showErr = (k: keyof TicketErrors) => (k === "usd" && !attempted && ticket.usd === "" ? undefined : errors[k]);

  // The bracket values belong to the window + side they were suggested for (basis). A new window (the BTC stop
  // defaults to its price to beat), a side flip or a submitted order re-suggests TP / SL / BTC stop.
  const entryCents = est?.avgCents ?? est?.ask ?? null;
  const ptb = current?.priceToBeat ?? null;
  const windowId = current?.id ?? null;
  const sideBid = sideQuote(odds, side).bid;
  const basis = React.useRef<BracketBasis | null>(ticket.bracketOn && windowId ? { windowId, side } : null);
  const [resuggestTick, setResuggestTick] = React.useState(0);
  React.useEffect(() => {
    if (!ticket.bracketOn) return;
    const r = resuggestBracket(basis.current, { windowId, side, entryCents, ptb, bid: sideBid });
    if (!r) return;
    basis.current = r.basis;
    setTicket(r.patch);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticket.bracketOn, windowId, side, entryCents == null ? null : Math.round(entryCents), ptb, resuggestTick, setTicket]);

  const setBracketOn = (on: boolean) => {
    if (!on) return setTicket({ bracketOn: false });
    const r = resuggestBracket(basis.current, { windowId, side, entryCents, ptb, bid: sideBid });
    if (r) {
      basis.current = r.basis;
      return setTicket({ bracketOn: true, ...r.patch, timeExitSec: ticket.timeExitSec || "30" });
    }
    // same window and side: keep what the user set, fill blanks with suggestions
    const s = suggestBracket(entryCents, ptb, sideBid);
    setTicket({
      bracketOn: true,
      tpCents: ticket.tpCents || s.tpCents,
      slCents: ticket.slCents || s.slCents,
      btcStop: ticket.btcStop || s.btcStop,
      timeExitSec: ticket.timeExitSec || s.timeExitSec,
    });
  };

  // clientOrderId of the current draft: reused when a submit fails ambiguously (timeout / network / 5xx) so a retry
  // cannot place a second order; rotated after a definitive answer or when the draft changes.
  const orderKey = React.useRef<OrderKey | null>(null);
  orderKey.current ??= new OrderKey(newClientOrderId);

  const submit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    setAttempted(true);
    if (busy || block || hasErrors || !est) return;
    setBusy(true);
    setResult(null);
    const keys = orderKey.current!;
    const clientOrderId = keys.for(JSON.stringify([mode, ticket]));
    try {
      const req = buildOrderRequest(ticket, { clientOrderId, fallbackLimitCents: est.limitCents });
      const r = await submitOrder(mode, req);
      keys.rotate();
      setResult(resultText(r));
      setAttempted(false);
      // the next order gets a fresh bracket from its own entry
      basis.current = null;
      setResuggestTick((t) => t + 1);
    } catch (err) {
      if (!isAmbiguousFailure(err)) {
        keys.rotate();
        setResult({ tone: err instanceof CancelledError ? "warn" : "error", text: errorText(err) });
        return;
      }
      // the server may have accepted it: look for the order before suggesting a retry
      const landed = await refreshOrders()
        .then((list) => list.find((o) => o.clientOrderId === clientOrderId) ?? null)
        .catch(() => null);
      if (landed) {
        keys.rotate();
        const t = resultText({ order: landed, fills: [], position: null, bracket: null });
        setResult({ tone: t.tone, text: `The first attempt went through. ${t.text}` });
        setAttempted(false);
      } else {
        setResult({ tone: "error", text: `${errorText(err)} Submitting again reuses the same order id, so it cannot fill twice.` });
      }
    } finally {
      setBusy(false);
    }
  };

  const sideBtn = (s: Side) => {
    const on = side === s;
    const a = sideQuote(odds, s).ask;
    const tone = s === "up" ? "border-up bg-up-tint text-up" : "border-down bg-down-tint text-down";
    return (
      <button
        type="button"
        aria-pressed={on}
        onClick={() => setTicket({ side: s })}
        className={cn(
          "touch-target flex h-12 flex-1 flex-col items-center justify-center gap-0.5 rounded-ctl border text-[12px] font-semibold transition-colors",
          on ? tone : "border-border-strong bg-inset text-muted hover:border-[#3a4658] hover:text-text",
        )}
      >
        <span>Buy {s === "up" ? "Up" : "Down"}</span>
        <span className={cn("num text-[15px] leading-[18px]", !on && "text-text-2")}>
          {quoteCents(a)}
          <span className="sr-only"> ask</span>
        </span>
      </button>
    );
  };

  const edge = est?.edgeCents ?? null;
  const submitDisabled = busy || !!block || hasErrors || !est;

  return (
    <Card
      title="Order ticket"
      right={
        <span className={cn("num text-[11px] font-bold uppercase tracking-wider", mode === "live" ? "text-down" : "text-ptb")}>{modeLabel}</span>
      }
    >
      <form onSubmit={submit} noValidate aria-label="Order ticket">
        <div role="group" aria-label="Side" className="flex gap-2">
          {sideBtn("up")}
          {sideBtn("down")}
        </div>
        <div className="mt-2.5">
          <Segmented<OrderType>
            ariaLabel="Order type"
            className="w-full [&>button]:flex-1"
            value={type}
            onChange={(v) => setTicket({ type: v })}
            options={[
              { value: "limit", label: "Limit" },
              { value: "marketable", label: "Marketable" },
            ]}
          />
        </div>

        {type === "limit" && (
          <div className="mt-3">
            <label className="label block" htmlFor="tk-limit">
              Limit price (¢)
            </label>
            <NumInput
              id="tk-limit"
              className="mt-1"
              value={ticket.limitCents}
              onChange={(v) => setTicket({ limitCents: v })}
              placeholder={est?.ask != null ? `${est.ask} · ask` : "price"}
              suffix="¢"
              error={errors.limitCents}
            />
          </div>
        )}

        <label className="label mt-3 block" htmlFor="tk-usd">
          Amount (USD)
        </label>
        <NumInput
          id="tk-usd"
          className="mt-1"
          prefix="$"
          value={ticket.usd}
          onChange={(v) => setTicket({ usd: v })}
          error={showErr("usd")}
        />
        <div role="group" aria-label="Quick amounts" className="mt-1.5 flex gap-1.5">
          {QUICK_AMOUNTS.map((v) => {
            const on = parseNum(ticket.usd) === v;
            return (
              <button
                key={v}
                type="button"
                aria-pressed={on}
                onClick={() => setTicket({ usd: String(v) })}
                className={cn(
                  "num touch-target h-7 flex-1 rounded-ctl border text-[12px] transition-colors",
                  on ? "border-odds/60 bg-odds-tint text-odds-text" : "border-border-strong bg-surface-2 text-text-2 hover:text-text",
                )}
              >
                ${v}
              </button>
            );
          })}
        </div>

        <label className="label mt-3 block" htmlFor="tk-slip">
          Max slippage (¢)
        </label>
        <NumInput
          id="tk-slip"
          className="mt-1"
          suffix="¢"
          value={ticket.maxSlippageCents}
          onChange={(v) => setTicket({ maxSlippageCents: v })}
          error={errors.maxSlippageCents}
        />

        <fieldset className="mt-3 border-t border-border pt-2">
          <legend className="sr-only">Bracket, one cancels other</legend>
          <div className="flex items-center gap-2 pb-1">
            <label htmlFor="tk-bracket" className="label touch-target flex flex-1 cursor-pointer items-center gap-2">
              <Check id="tk-bracket" checked={ticket.bracketOn} onChange={setBracketOn} />
              Bracket · OCO
            </label>
            {ticket.bracketOn && <span className="text-[11px] text-up">armed on fill</span>}
          </div>
          <BracketRows
            idPrefix="tk"
            side={side}
            value={ticket as BracketFields}
            onChange={(p) => setTicket(p)}
            errors={errors}
            disabled={!ticket.bracketOn}
          />
          <p className="pt-1 text-[11px] leading-4 text-muted">Drag the TP/SL lines on the chart to adjust after entry.</p>
        </fieldset>

        <div className="mt-2.5 rounded-ctl border border-border bg-inset px-2.5 py-1.5" aria-label="Order estimate" role="group">
          <SumRow
            k="Est. avg fill"
            v={
              est?.avgCents != null ? (
                <>
                  {quoteCents(est.avgCents)}
                  {est.fillQty === 0 && est.restQty > 0 && <span className="text-muted"> · rests</span>}
                </>
              ) : (
                "—"
              )
            }
          />
          <SumRow
            k="Contracts"
            v={
              est && est.qty > 0 ? (
                <>
                  {est.qty.toFixed(1)}
                  {est.restQty > 0 && est.fillQty > 0 && <span className="text-muted"> ({est.restQty.toFixed(1)} rest)</span>}
                </>
              ) : (
                "—"
              )
            }
          />
          <SumRow
            k="Fees"
            v={
              fee == null ? (
                <span className="text-ptb" title="Fee schedule unconfirmed. Set a fee override in Settings to include it.">
                  [FEE]<span className="sr-only"> fee schedule unconfirmed; set a fee override in Settings</span>
                </span>
              ) : (
                usd(est?.feeUsd ?? 0)
              )
            }
          />
          <SumRow k="Max loss" v={est && est.qty > 0 ? usd(est.maxLossUsd) : "—"} color="#F87171" />
          <SumRow
            k={`Payout if ${sideLabel}`}
            v={
              est && est.qty > 0 ? (
                <>
                  {usd(est.payoutUsd)} <span className="text-[11px]">({signedUsd(est.profitUsd)})</span>
                </>
              ) : (
                "—"
              )
            }
            color="#34D399"
          />
          <SumRow k="Edge vs fair" v={edge != null ? signedCents(edge, 1) : "—"} color={edge == null ? undefined : edge < 0 ? "#F59E0B" : "#7FB2FF"} />
          {est?.partial && <p className="pt-0.5 text-[11px] leading-4 text-ptb">Book depth within the slippage cap is short; the rest will not fill.</p>}
          {est && !est.depth && est.ask != null && <p className="pt-0.5 text-[11px] leading-4 text-muted">Top of book only (no depth yet).</p>}
        </div>

        <button
          type="submit"
          aria-disabled={submitDisabled}
          aria-describedby={block ? "tk-block" : undefined}
          className={cn(
            "touch-target mt-3 h-11 w-full rounded-ctl text-[14px] font-bold transition-[filter,opacity]",
            side === "up" ? "bg-up text-[#06251a]" : "bg-down text-[#2b0a0a]",
            submitDisabled ? "cursor-not-allowed opacity-50" : "hover:brightness-110",
          )}
        >
          {busy ? "Placing…" : `Buy ${sideLabel} · ${modeLabel}`}
        </button>
        {block && (
          <p id="tk-block" className="mt-1.5 text-center text-[11px] leading-4 text-ptb">
            {block}
          </p>
        )}
        {attempted && !block && hasErrors && <p className="mt-1.5 text-center text-[11px] leading-4 text-down">Fix the highlighted fields.</p>}
        <p
          role="status"
          aria-live="polite"
          className={cn(
            "text-center text-[12px] leading-4",
            result ? "mt-1.5" : "sr-only",
            result?.tone === "ok" ? "text-up" : result?.tone === "warn" ? "text-ptb" : "text-down",
          )}
        >
          {result?.text ?? ""}
        </p>
      </form>
    </Card>
  );
}
