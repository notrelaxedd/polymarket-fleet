"use client";
// New / edit alert dialog, bound to store.newAlert (openNewAlert(prefill?, editId?) / closeNewAlert()); opened
// from the Alerts tab, the tools rail "Add alert" and the chart. Submit = api.createAlert / api.updateAlert;
// asks for notification permission + unlocks audio from the submit gesture. An edit sends only the fields the
// user changed (alertEditPatch), so a backend change made while the dialog is open (a "once" alert auto-disabled
// when it fired) is not overwritten. "Now" / defaults / hints read the LIVE market (useLiveMarket), never the
// replayed cursor. OWNER: tabs.
import * as React from "react";
import type { AlertInput, AlertKind } from "@shared/protocol";
import { ALERT_KIND_LABELS } from "@shared/alerts";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { isApiError } from "@/lib/api-error";
import { btc, signedCents } from "@/lib/format";
import { requestNotifyPermission, unlockAudio } from "@/lib/notify";
import { useLiveMarket } from "@/lib/replay";
import { selectFair, selectOdds, useTerminal } from "@/lib/store";
import { cn } from "@/lib/utils";
import {
  ALERT_KIND_GROUPS,
  DEFAULT_ALERT_FORM,
  alertEditPatch,
  NOTE_MAX,
  alertUnit,
  crossingHint,
  formFromInput,
  liveValueFor,
  parseThreshold,
  thresholdText,
  validateAlertForm,
  type AlertForm,
} from "./alert-form";

const inputCls =
  "touch-target h-8 w-full rounded-ctl border border-border-strong bg-inset px-2.5 text-[13px] text-text placeholder:text-muted focus:border-odds focus:outline-none";

/** Live market values (the backend evaluates alerts live; during a replay the store holds the replayed cursor). */
function useLiveValues() {
  const btcNow = useLiveMarket((m) => m.lastTick?.price ?? null);
  const upMid = useLiveMarket((m) => selectOdds(m)?.upMid ?? null);
  const edge = useLiveMarket((m) => {
    const f = selectFair(m);
    if (f?.edge != null) return f.edge;
    const mid = selectOdds(m)?.upMid;
    return f && mid != null ? f.fair - mid : null;
  });
  const ptb = useLiveMarket((m) => m.current?.priceToBeat ?? null);
  return React.useMemo(() => ({ btc: btcNow, upMid, edge, ptb }), [btcNow, upMid, edge, ptb]);
}

function liveText(kind: AlertKind, v: number | null): string {
  if (v == null) return "—";
  switch (alertUnit(kind)) {
    case "usd":
      return `$${btc(v)}`;
    case "none":
      return `${v < 0 ? "−" : "+"}$${btc(Math.abs(v))} vs PTB`;
    default:
      return kind.startsWith("edge_") ? signedCents(v, 1) : `${v.toFixed(1)}¢`;
  }
}

export function NewAlertDialog() {
  const ui = useTerminal((s) => s.newAlert);
  const close = useTerminal((s) => s.closeNewAlert);
  // stable: the Dialog's focus effect depends on it (an inline arrow re-ran it on every live tick and pulled
  // focus back to the Condition select while typing)
  const onOpenChange = React.useCallback((o: boolean) => {
    if (!o) close();
  }, [close]);
  const replayActive = useTerminal((s) => s.replay.active);
  const editing = useTerminal((s) => (ui.editId ? s.alerts.find((a) => a.id === ui.editId) ?? null : null));
  /** the edited alert as the dialog opened it (validated form round trip): the base of alertEditPatch */
  const initialRef = React.useRef<AlertInput | null>(null);
  const live = useLiveValues();
  const [form, setForm] = React.useState<AlertForm>(DEFAULT_ALERT_FORM);
  const [error, setError] = React.useState<{ field: "threshold" | "note" | "form"; text: string } | null>(null);
  const [busy, setBusy] = React.useState(false);
  const thresholdId = React.useId();
  const kindId = React.useId();
  const noteId = React.useId();
  const errId = React.useId();

  // (re)initialise every time the dialog opens
  React.useEffect(() => {
    if (!ui.open) return;
    const base = editing ? formFromInput(editing) : DEFAULT_ALERT_FORM;
    const init = editing ? validateAlertForm(base) : null;
    initialRef.current = init?.ok ? init.input : null;
    const f = formFromInput(ui.prefill, base);
    // a new price alert without a value starts at the current price, rounded
    if (!editing && !f.threshold && alertUnit(f.kind) !== "none") {
      const v = liveValueFor(f.kind, live);
      if (v != null) f.threshold = thresholdText(f.kind, alertUnit(f.kind) === "usd" ? Math.round(v / 10) * 10 : Math.round(v));
    }
    setForm(f);
    setError(null);
    setBusy(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- snapshot live values only when opening
  }, [ui.open, ui.prefill, ui.editId]);

  // the backend changed `enabled` while the dialog is open (a once alert fired): follow it unless the user
  // already toggled the checkbox
  const liveEnabled = editing?.enabled;
  React.useEffect(() => {
    const init = initialRef.current;
    if (!ui.open || liveEnabled == null || !init || liveEnabled === init.enabled) return;
    setForm((f) => (f.enabled === init.enabled ? { ...f, enabled: liveEnabled } : f));
    initialRef.current = { ...init, enabled: liveEnabled };
  }, [ui.open, liveEnabled]);

  const unit = alertUnit(form.kind);
  const current = liveValueFor(form.kind, live);
  const hint = crossingHint(form.kind, parseThreshold(form.threshold), current);

  const setKind = (kind: AlertKind) => {
    setError(null);
    setForm((f) => {
      const sameUnit = alertUnit(kind) === alertUnit(f.kind) && (kind.startsWith("edge_") === f.kind.startsWith("edge_"));
      if (sameUnit) return { ...f, kind };
      const v = liveValueFor(kind, live);
      const u = alertUnit(kind);
      return { ...f, kind, threshold: u === "none" || v == null ? "" : thresholdText(kind, u === "usd" ? Math.round(v / 10) * 10 : Math.round(v)) };
    });
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const r = validateAlertForm(form);
    if (!r.ok) {
      setError({ field: r.field, text: r.error });
      return;
    }
    // user gesture: ask for permission + allow the beep before awaiting anything
    unlockAudio();
    void requestNotifyPermission();
    setBusy(true);
    try {
      let saved;
      if (ui.editId) {
        const patch = initialRef.current ? alertEditPatch(initialRef.current, r.input) : r.input;
        if (!Object.keys(patch).length) {
          close();
          return;
        }
        saved = await api.updateAlert(ui.editId, patch);
      } else {
        saved = await api.createAlert(r.input);
      }
      const st = useTerminal.getState();
      st.setAlerts(st.alerts.some((a) => a.id === saved.id) ? st.alerts.map((a) => (a.id === saved.id ? saved : a)) : [...st.alerts, saved]);
      close();
    } catch (err) {
      setError({ field: "form", text: isApiError(err) ? err.message : "Couldn't save the alert. Check the connection to the worker." });
      setBusy(false);
    }
  };

  const title = ui.editId ? "Edit alert" : "New alert";
  return (
    <Dialog
      open={ui.open}
      onOpenChange={onOpenChange}
      title={title}
      description="Evaluated by the backend every second, even with the browser closed. Fires when the value crosses the threshold; delivered as a browser notification + sound and logged in the Alerts tab."
    >
      <form onSubmit={submit} noValidate className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor={kindId} className="label">
            Condition
          </label>
          <select
            id={kindId}
            data-autofocus
            value={form.kind}
            onChange={(e) => setKind(e.target.value as AlertKind)}
            className={cn(inputCls, "appearance-none pr-8")}
            style={{
              colorScheme: "dark",
              backgroundColor: "#0A0D12",
              backgroundRepeat: "no-repeat",
              backgroundPosition: "right 10px center",
              backgroundSize: "12px",
              backgroundImage:
                "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23A3ABB9' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E\")",
            }}
          >
            {ALERT_KIND_GROUPS.map((g) => (
              <optgroup key={g.label} label={g.label}>
                {g.kinds.map((k) => (
                  <option key={k} value={k}>
                    {ALERT_KIND_LABELS[k]}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </div>

        {unit !== "none" ? (
          <div className="flex flex-col gap-1">
            <div className="flex items-baseline justify-between">
              <label htmlFor={thresholdId} className="label">
                {unit === "usd" ? "BTC price (USD)" : form.kind.startsWith("edge_") ? "Edge (¢, fair − market)" : "Mid price (¢)"}
              </label>
              <button
                type="button"
                disabled={current == null}
                onClick={() => current != null && setForm((f) => ({ ...f, threshold: thresholdText(f.kind, current) }))}
                className="text-[12px] text-muted hover:text-text disabled:opacity-50"
              >
                Now <span className="num text-text-2">{liveText(form.kind, current)}</span>
              </button>
            </div>
            <div className="relative">
              {unit === "usd" && <span className="num pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted">$</span>}
              <input
                id={thresholdId}
                inputMode="decimal"
                autoComplete="off"
                value={form.threshold}
                onChange={(e) => {
                  setError(null);
                  setForm((f) => ({ ...f, threshold: e.target.value }));
                }}
                aria-invalid={error?.field === "threshold" || undefined}
                aria-describedby={error?.field === "threshold" ? errId : undefined}
                className={cn(inputCls, "num", unit === "usd" ? "pl-6" : "pr-7", error?.field === "threshold" && "border-down")}
                placeholder={unit === "usd" ? "67,250" : form.kind.startsWith("edge_") ? "3" : "60"}
              />
              {unit === "cents" && <span className="num pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-muted">¢</span>}
            </div>
            {hint && <p className="text-[12px] text-ptb">{hint}</p>}
            {replayActive && <p className="text-[12px] text-muted">Replay running: &ldquo;Now&rdquo; is the live market, not the replayed moment.</p>}
          </div>
        ) : (
          <p className="rounded-ctl border border-border bg-inset px-2.5 py-2 text-[12px] text-text-2">
            Fires when BTC crosses the current window&apos;s price to beat, in either direction. Now{" "}
            <span className="num">{liveText(form.kind, current)}</span>.
          </p>
        )}

        <div className="flex flex-col gap-1">
          <label htmlFor={noteId} className="label">
            Note <span className="normal-case tracking-normal text-muted">(optional)</span>
          </label>
          <input
            id={noteId}
            value={form.note}
            maxLength={NOTE_MAX + 20}
            onChange={(e) => {
              setError(null);
              setForm((f) => ({ ...f, note: e.target.value }));
            }}
            aria-invalid={error?.field === "note" || undefined}
            aria-describedby={error?.field === "note" ? errId : undefined}
            className={cn(inputCls, error?.field === "note" && "border-down")}
            placeholder="e.g. take profit zone"
          />
        </div>

        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <Check checked={form.once} onChange={(once) => setForm((f) => ({ ...f, once }))} label="Disable after it fires once" />
          <Check checked={form.enabled} onChange={(enabled) => setForm((f) => ({ ...f, enabled }))} label="Enabled" />
        </div>

        {error && (
          <p id={errId} role="alert" className="text-[12px] text-down">
            {error.text}
          </p>
        )}

        <div className="mt-1 flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={close}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy ? "Saving…" : ui.editId ? "Save alert" : "Create alert"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function Check({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="touch-target inline-flex cursor-pointer items-center gap-2 text-[13px] text-text-2">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="h-3.5 w-3.5 accent-[#5B9CFF]" />
      {label}
    </label>
  );
}
