"use client";
// Risk card + kill switch. OWNER: trading-ui. Data: store.risk (RiskState). Kill = api.kill() behind a confirm
// Dialog; while the kill switch is engaged a Re-arm button sends PUT /api/risk {killSwitch:false}. A daily-loss
// disable can be lifted by hand behind a confirm (PUT /api/risk {clearDisable:true}, optionally with a higher
// daily loss limit; the backend refuses while today's loss is still at the limit).
import * as React from "react";
import { OctagonX } from "lucide-react";
import { DEFAULT_RISK_LIMITS } from "@shared/protocol";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { useOpenPositions, useOrders } from "@/lib/hooks";
import { liveNow, useTerminal } from "@/lib/store";
import { etTime, usd } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Card } from "./Card";
import { errorText } from "./actions";
import { newerOnly, stillOpenText } from "./order-safety";
import { riskBar, type BarTone } from "./ticket-math";

const TONE: Record<BarTone, string> = { ok: "bg-odds", warn: "bg-ptb", danger: "bg-down" };

function Meter({ id, label, used, limit }: { id: string; label: string; used: number; limit: number }) {
  const { pct, tone } = riskBar(used, limit);
  return (
    <div className="py-1.5">
      <div className="flex items-center justify-between text-[12px]">
        <span id={id} className="text-muted">
          {label}
        </span>
        <span className="num text-text-2">
          {usd(used, used >= 1000 ? 0 : 2)} <span className="text-muted">/ {usd(limit, 0)}</span>
        </span>
      </div>
      <div
        role="progressbar"
        aria-labelledby={id}
        aria-valuemin={0}
        aria-valuemax={limit}
        aria-valuenow={Math.round(used * 100) / 100}
        aria-valuetext={`${usd(used)} of ${usd(limit, 0)}`}
        className="mt-1 h-1.5 overflow-hidden rounded-full bg-inset"
      >
        <div className={cn("h-full rounded-full transition-[width]", TONE[tone])} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export function Risk() {
  const risk = useTerminal((s) => s.risk);
  const setRisk = useTerminal((s) => s.setRisk);
  const fallback = useTerminal((s) => s.settings?.risk ?? DEFAULT_RISK_LIMITS);
  const positions = useOpenPositions();
  const orders = useOrders();
  const [confirm, setConfirm] = React.useState(false);
  const [lift, setLift] = React.useState(false);
  const [newLimit, setNewLimit] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [done, setDone] = React.useState<string | null>(null);
  /** positions the kill switch could not flatten (no bid, slippage cap, closed window): never an "all clear" */
  const [leftOpen, setLeftOpen] = React.useState<string | null>(null);

  const limits = risk?.limits ?? fallback;
  const resting = orders.filter((o) => o.status === "open" || o.status === "partial" || o.status === "pending").length;
  const disabledUntil = risk?.tradingDisabledUntil != null && risk.tradingDisabledUntil > liveNow() ? risk.tradingDisabledUntil : null;

  const closeConfirm = React.useCallback((o: boolean) => {
    if (!o) setConfirm(false);
  }, []);

  const kill = async () => {
    setBusy(true);
    setError(null);
    setLeftOpen(null);
    const openAtClick = positions.length;
    try {
      const r = await api.kill();
      setRisk(r.risk);
      setDone(`Cancelled ${r.cancelledOrders} order${r.cancelledOrders === 1 ? "" : "s"}, flattened ${r.closedPositions} position${r.closedPositions === 1 ? "" : "s"}.`);
      // what is still open after the flatten (a partly filled exit counts in closedPositions but leaves contracts)
      const still = await api
        .positions({ status: "open" })
        .then((list) => {
          const st = useTerminal.getState();
          const fresh = newerOnly(st.positions, list);
          if (fresh.length) st.upsertPositions(fresh);
          // count against the merged store (a WS close newer than this list wins)
          const merged = useTerminal.getState().positions;
          return list.filter((p) => merged[p.id]?.status === "open" && merged[p.id].qty > 0).length;
        })
        .catch(() => Math.max(0, openAtClick - r.closedPositions));
      setLeftOpen(stillOpenText(still));
      setConfirm(false);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const lossUsed = risk?.dailyLossUsedUsd ?? 0;
  const atLimit = lossUsed > 1e-9 && lossUsed >= limits.dailyLossLimitUsd - 1e-9;
  const newLimitNum = newLimit.trim() === "" ? null : Number(newLimit);
  const newLimitErr =
    newLimitNum == null
      ? atLimit
        ? `Today's loss (${usd(lossUsed)}) is at the limit: enter a higher limit.`
        : null
      : !Number.isFinite(newLimitNum) || newLimitNum <= 0
        ? "Enter a dollar amount above $0."
        : newLimitNum <= lossUsed
          ? `Must be above today's loss (${usd(lossUsed)}).`
          : null;
  const openLift = () => {
    setError(null);
    setNewLimit(atLimit ? String(Math.ceil(lossUsed + Math.max(10, limits.dailyLossLimitUsd * 0.25))) : "");
    setLift(true);
  };
  const closeLift = React.useCallback((o: boolean) => {
    if (!o) setLift(false);
  }, []);
  const liftDisable = async () => {
    if (newLimitErr) return;
    setBusy(true);
    setError(null);
    try {
      setRisk(await api.putRisk(newLimitNum != null ? { clearDisable: true, dailyLossLimitUsd: newLimitNum } : { clearDisable: true }));
      setLift(false);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const rearm = async () => {
    setBusy(true);
    setError(null);
    try {
      setRisk(await api.putRisk({ killSwitch: false }));
      setDone(null);
      setLeftOpen(null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Risk">
      {(risk?.killSwitch || disabledUntil) && (
        <div role="status" className="mb-2 rounded-ctl border border-down/50 bg-down-tint px-2.5 py-2 text-[12px] leading-4 text-text">
          <div className="font-semibold text-down">{risk?.killSwitch ? "Kill switch engaged" : "Trading disabled"}</div>
          <div className="text-text-2">
            {risk?.killSwitch
              ? "New orders are blocked until you re-arm."
              : `${risk?.disabledReason ?? "Daily loss limit reached"} · until ${etTime(disabledUntil!)} ET`}
          </div>
          {risk?.killSwitch && (
            <Button size="sm" variant="outline" className="mt-1.5" onClick={rearm} disabled={busy}>
              Re-arm trading
            </Button>
          )}
          {!risk?.killSwitch && disabledUntil && (
            <Button size="sm" variant="outline" className="mt-1.5" onClick={openLift} disabled={busy}>
              Lift today&apos;s disable…
            </Button>
          )}
        </div>
      )}
      <Meter id="rk-daily" label="Daily loss limit" used={risk?.dailyLossUsedUsd ?? 0} limit={limits.dailyLossLimitUsd} />
      <Meter id="rk-exposure" label="Open exposure" used={risk?.openExposureUsd ?? 0} limit={limits.maxExposureUsd} />
      <div className="flex items-center justify-between py-1.5 text-[12px]">
        <span className="text-muted">Max loss per window</span>
        <span className="num text-text-2">
          {usd(risk?.windowLossUsedUsd ?? 0)} <span className="text-muted">/ {usd(limits.maxLossPerWindowUsd, 0)}</span>
        </span>
      </div>
      <button
        type="button"
        onClick={() => {
          setError(null);
          setConfirm(true);
        }}
        className="touch-target mt-1.5 flex h-10 w-full items-center justify-center gap-2 rounded-ctl border border-down text-[13px] font-semibold text-down transition-colors hover:bg-down-tint"
      >
        <OctagonX size={15} aria-hidden />
        Kill switch · cancel all &amp; flatten
      </button>
      {done && (
        <p role="status" className="mt-1.5 text-[11px] leading-4 text-muted">
          {done}
        </p>
      )}
      {leftOpen && (
        <p role="alert" className="mt-1.5 rounded-ctl border border-ptb/40 bg-ptb-tint px-2 py-1.5 text-[11px] leading-4 text-ptb">
          {leftOpen}
        </p>
      )}
      {error && !confirm && !lift && (
        <p role="alert" className="mt-1.5 text-[11px] leading-4 text-down">
          {error}
        </p>
      )}
      <Dialog
        open={confirm}
        onOpenChange={closeConfirm}
        tone="danger"
        title="Cancel all & flatten?"
        description={
          <>
            Cancels {resting} resting order{resting === 1 ? "" : "s"} and closes {positions.length} open position{positions.length === 1 ? "" : "s"} with
            marketable orders, then blocks new orders until you re-arm.
          </>
        }
        footer={
          <>
            <Button variant="outline" onClick={() => setConfirm(false)} data-autofocus>
              Keep positions
            </Button>
            <Button variant="down" onClick={kill} disabled={busy}>
              {busy ? "Flattening…" : "Kill switch"}
            </Button>
          </>
        }
      >
        {error && (
          <p role="alert" className="text-[12px] text-down">
            {error}
          </p>
        )}
      </Dialog>
      <Dialog
        open={lift}
        onOpenChange={closeLift}
        tone="danger"
        title="Lift today's daily-loss disable?"
        description={
          <>
            Trading was disabled after today&apos;s loss reached the daily limit ({usd(lossUsed)} of {usd(limits.dailyLossLimitUsd, 0)}). Lifting it
            allows new entries again today{atLimit ? "; the limit must first be raised above today's loss" : ""}.
          </>
        }
        footer={
          <>
            <Button variant="outline" onClick={() => setLift(false)} data-autofocus>
              Keep disabled
            </Button>
            <Button variant="down" onClick={liftDisable} disabled={busy || newLimitErr != null}>
              {busy ? "Lifting…" : "Lift disable"}
            </Button>
          </>
        }
      >
        <label htmlFor="rk-new-limit" className="label block">
          Daily loss limit {atLimit ? "(required)" : "(optional)"}
        </label>
        <div className="mt-1 flex items-center gap-1.5">
          <span className="text-muted">$</span>
          <input
            id="rk-new-limit"
            inputMode="decimal"
            value={newLimit}
            placeholder={String(limits.dailyLossLimitUsd)}
            onChange={(e) => setNewLimit(e.target.value.replace(/[^0-9.]/g, ""))}
            aria-invalid={newLimitErr != null}
            aria-describedby={newLimitErr ? "rk-new-limit-err" : undefined}
            className="num h-9 w-28 rounded-ctl border border-border-strong bg-inset px-2 text-[13px] text-text outline-none focus-visible:border-odds"
          />
        </div>
        {newLimitErr && (
          <p id="rk-new-limit-err" className="mt-1 text-[12px] text-down">
            {newLimitErr}
          </p>
        )}
        {error && (
          <p role="alert" className="mt-1 text-[12px] text-down">
            {error}
          </p>
        )}
      </Dialog>
    </Card>
  );
}
