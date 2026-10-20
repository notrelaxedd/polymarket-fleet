"use client";
// Settings popover: mode (paper / live), vol lookback, fee override, risk limits. OWNER: trading-ui.
// Save = PUT /api/risk (changed limits) + PUT /api/settings (changed mode / vol / fee only), ordered so a
// failure never leaves live on with the old limits (settings-form.saveSettingsForm). Live needs settings.liveAvailable
// (keys in the backend .env); the first live ORDER of each browser session still opens LiveConfirmDialog.
import * as React from "react";
import { Settings as Cog } from "lucide-react";
import type { TradingMode } from "@shared/protocol";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tip } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { NumInput } from "@/components/panels/fields";
import { errorText } from "@/components/panels/actions";
import { RISK_FIELDS, saveFailureText, saveSettingsForm, toForm, validateSettingsForm, type Form } from "@/components/panels/settings-form";
import { api } from "@/lib/api";
import { useTerminal } from "@/lib/store";
import { usd } from "@/lib/format";
import { cn } from "@/lib/utils";

export function ModeBadge() {
  const mode = useTerminal((s) => s.settings?.mode ?? "paper");
  return mode === "live" ? (
    <span className="num rounded-ctl border border-down/50 bg-down-tint px-2.5 py-1 text-[12px] font-bold tracking-wider text-down">LIVE</span>
  ) : (
    <span className="num rounded-ctl border border-ptb/40 bg-ptb-tint px-2.5 py-1 text-[12px] font-bold tracking-wider text-ptb">PAPER</span>
  );
}

export function SettingsMenu() {
  const s = useTerminal((st) => st.settings);
  const live = useTerminal((st) => st.live);
  const setRisk = useTerminal((st) => st.setRisk);
  const [open, setOpen] = React.useState(false);
  const [form, setForm] = React.useState<Form>(() => toForm(s));
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [tried, setTried] = React.useState(false);

  // load the form from the current settings each time it opens (not on every settings echo while editing)
  const wasOpen = React.useRef(false);
  React.useEffect(() => {
    if (open && !wasOpen.current) {
      setForm(toForm(s));
      setError(null);
      setTried(false);
    }
    wasOpen.current = open;
  }, [open, s]);

  const liveAvailable = !!s?.liveAvailable;
  const errors = validateSettingsForm(form, liveAvailable);
  const valid = Object.keys(errors).length === 0;
  const set = (patch: Partial<Form>) => setForm((f) => ({ ...f, ...patch }));
  const err = (k: keyof Form) => (tried || form[k] !== "" ? errors[k] : undefined);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setTried(true);
    if (!valid || !s || busy) return;
    setBusy(true);
    setError(null);
    try {
      const out = await saveSettingsForm(form, s, {
        putSettings: api.putSettings,
        putRisk: api.putRisk,
        onSettings: (next) => useTerminal.setState({ settings: next }),
        // keep Settings.risk in step so the next diff (and a retry after a partial failure) sees the new limits
        onRisk: (next) => {
          setRisk(next);
          const cur = useTerminal.getState().settings;
          if (cur) useTerminal.setState({ settings: { ...cur, risk: next.limits } });
        },
      });
      if (out.failed) setError(saveFailureText(out, errorText(out.error)));
      else setOpen(false);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  // radiogroup: roving tabindex (only the checked option is tabbable), arrow keys move + select, live skipped
  // while unavailable
  const modeRefs = React.useRef<Partial<Record<TradingMode, HTMLButtonElement | null>>>({});
  const MODES: TradingMode[] = ["paper", "live"];
  const enabledModes = MODES.filter((m) => m !== "live" || liveAvailable);
  const onModeKey = (e: React.KeyboardEvent) => {
    const step = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const i = Math.max(0, enabledModes.indexOf(form.mode));
    const next = enabledModes[(i + step + enabledModes.length) % enabledModes.length];
    set({ mode: next });
    modeRefs.current[next]?.focus();
  };
  const modeBtn = (m: TradingMode) => {
    const on = form.mode === m;
    const dis = m === "live" && !liveAvailable;
    return (
      <button
        type="button"
        role="radio"
        ref={(el) => {
          modeRefs.current[m] = el;
        }}
        aria-checked={on}
        aria-disabled={dis || undefined}
        tabIndex={on ? 0 : -1}
        onClick={() => !dis && set({ mode: m })}
        className={cn(
          "touch-target h-7 flex-1 rounded-[6px] px-3 text-[12px] font-semibold transition-colors",
          on ? (m === "live" ? "bg-down-tint text-down shadow-[inset_0_0_0_1px_#F87171]" : "bg-ptb-tint text-ptb shadow-[inset_0_0_0_1px_#F5B942]") : "text-muted hover:text-text",
          dis && "cursor-not-allowed opacity-50 hover:text-muted",
        )}
      >
        {m === "live" ? "Live" : "Paper"}
      </button>
    );
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <Tip label="Settings">
        <PopoverTrigger asChild>
          <Button variant="ghost" size="icon" aria-label="Settings" className="border border-border">
            <Cog size={16} />
          </Button>
        </PopoverTrigger>
      </Tip>
      <PopoverContent align="end" className="max-h-[min(640px,calc(100vh-80px))] w-[min(340px,calc(100vw-24px))] overflow-y-auto">
        <form onSubmit={save} noValidate aria-label="Settings">
          <div className="label pb-2">Settings</div>

          <div className="flex items-center justify-between gap-3 py-1.5">
            <span id="st-mode" className="text-[12px] text-muted">
              Trading mode
            </span>
            <div role="radiogroup" aria-labelledby="st-mode" onKeyDown={onModeKey} className="flex w-[170px] rounded-ctl border border-border-strong bg-inset p-0.5">
              {modeBtn("paper")}
              {modeBtn("live")}
            </div>
          </div>
          {!liveAvailable ? (
            <p className="pb-1 text-[11px] leading-4 text-muted">Live needs POLYMARKET_US_KEY_ID and SECRET in the worker&apos;s .env.</p>
          ) : form.mode === "live" ? (
            <p role="note" className="mb-1 rounded-ctl border border-down/50 bg-down-tint px-2 py-1.5 text-[11px] leading-4 text-text-2">
              Live sends real orders to Polymarket US. The first live order of each browser session asks you to confirm.
              {live && (
                <span className="num mt-1 block text-muted">
                  {live.connected ? "Private feed connected" : "Private feed offline"}
                  {live.balanceUsd != null && ` · balance ${usd(live.balanceUsd)}`}
                </span>
              )}
            </p>
          ) : null}
          {errors.mode && <p className="text-[11px] text-down">{errors.mode}</p>}

          <Row id="st-vol" label="Vol lookback (min)" hint="Realized-vol window for fair value.">
            <NumInput id="st-vol" integer className="w-24" value={form.vol} onChange={(v) => set({ vol: v })} error={err("vol")} />
          </Row>
          <Row id="st-fee" label="Fee override (¢ / contract)" hint={s?.feeNote || "Blank = schedule unconfirmed, shown as [FEE]."}>
            <NumInput id="st-fee" className="w-24" value={form.fee} placeholder="[FEE]" suffix="¢" onChange={(v) => set({ fee: v })} error={err("fee")} />
          </Row>

          <div className="label pb-1 pt-3">Risk limits</div>
          {RISK_FIELDS.map((f) => (
            <Row key={f.key} id={`st-${f.key}`} label={f.label}>
              <NumInput id={`st-${f.key}`} className="w-24" prefix="$" value={form[f.key]} onChange={(v) => set({ [f.key]: v } as Partial<Form>)} error={err(f.key)} />
            </Row>
          ))}
          <p className="pt-1 text-[11px] leading-4 text-muted">Hitting the daily loss limit disables trading until midnight ET.</p>

          {error && (
            <p role="alert" className="mt-2 text-[12px] text-down">
              {error}
            </p>
          )}
          <div className="mt-3 flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" size="sm" variant={form.mode === "live" ? "down" : "default"} disabled={busy || !s}>
              {busy ? "Saving…" : "Save"}
            </Button>
          </div>
        </form>
      </PopoverContent>
    </Popover>
  );
}

function Row({ id, label, hint, children }: { id: string; label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-1">
      <div className="min-w-0 pt-2">
        <label htmlFor={id} className="block text-[12px] leading-4 text-text-2">
          {label}
        </label>
        {hint && <p className="text-[11px] leading-4 text-muted">{hint}</p>}
      </div>
      {children}
    </div>
  );
}
