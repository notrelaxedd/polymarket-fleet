"use client";
// Alerts tab: the alert list (enable toggle, condition, note, last fired, edit / delete), recent alert events,
// delivery controls (desktop notification permission, sound) and the "New alert" button -> NewAlertDialog.
// Data: store.alerts / store.alertEvents (snapshot + WS); api.updateAlert / api.deleteAlert. OWNER: tabs.
import * as React from "react";
import { Bell, BellOff, Pencil, Plus, Trash2, Volume2, VolumeX } from "lucide-react";
import type { Alert } from "@shared/protocol";
import { Button } from "@/components/ui/button";
import { InfoTip } from "@/components/ui/info-tip";
import { api } from "@/lib/api";
import { isApiError } from "@/lib/api-error";
import { etTimeSec } from "@/lib/format";
import {
  getNotifyPrefs,
  notifyPermission,
  requestNotifyPermission,
  setNotifyPrefs,
  subscribeNotifyPrefs,
  unlockAudio,
  playAlertSound,
  type NotifyPermission,
  type NotifyPrefs,
} from "@/lib/notify";
import { liveNow, useTerminal } from "@/lib/store";
import { cn } from "@/lib/utils";
import { alertCondition } from "./alert-form";
import { Empty } from "./Empty";
import { Td, Th, etDayPrefix } from "./table-ui";

function errorNotice(title: string, e: unknown) {
  useTerminal.getState().pushNotice({
    id: `${title}-${Date.now()}`,
    ts: Date.now(),
    level: "error",
    title,
    message: isApiError(e) ? e.message : "The request failed. Check the connection to the worker.",
  });
}

/** Optimistically patch one alert in the store (the WS `alerts` echo replaces the list afterwards). */
function patchAlert(id: string, patch: Partial<Alert> | null) {
  const st = useTerminal.getState();
  st.setAlerts(patch ? st.alerts.map((a) => (a.id === id ? { ...a, ...patch } : a)) : st.alerts.filter((a) => a.id !== id));
}

export function Alerts() {
  const alerts = useTerminal((s) => s.alerts);
  const events = useTerminal((s) => s.alertEvents);
  const unseen = useTerminal((s) => s.unseenAlertEvents);
  const openNewAlert = useTerminal((s) => s.openNewAlert);
  const markSeen = useTerminal((s) => s.markAlertsSeen);

  // the tab content is mounted only while visible: viewing it marks events seen
  React.useEffect(() => {
    if (unseen > 0) markSeen();
  }, [unseen, markSeen]);

  const sorted = React.useMemo(() => [...alerts].sort((a, b) => b.createdTs - a.createdTs), [alerts]);
  const enabled = alerts.filter((a) => a.enabled).length;
  const now = liveNow();

  return (
    <div className="flex min-h-full flex-col">
      <div className="flex flex-wrap items-center gap-2 px-3 py-1.5">
        <span className="text-[12px] text-muted">
          <span className="num text-text-2">{alerts.length}</span> alert{alerts.length === 1 ? "" : "s"} ·{" "}
          <span className="num text-text-2">{enabled}</span> on
        </span>
        <DeliveryControls />
        <Button size="sm" className="ml-auto" onClick={() => openNewAlert()}>
          <Plus size={13} aria-hidden />
          New alert
        </Button>
      </div>
      {alerts.length === 0 && events.length === 0 ? (
        <Empty icon={<Bell size={20} />} title="No alerts" hint="BTC price, Up/Down odds, edge threshold or BTC crossing the price to beat." />
      ) : (
        <div className="flex min-w-0 flex-wrap items-start gap-x-3 gap-y-2 px-3 pb-3">
          <div className="min-w-0 flex-[2_1_440px] overflow-x-auto rounded-ctl border border-border">
            {sorted.length === 0 ? (
              <p className="px-3 py-4 text-center text-[12px] text-muted">No alerts set. Use “New alert” to add one.</p>
            ) : (
              <table className="w-full min-w-[440px] border-separate border-spacing-0 text-left">
                <caption className="sr-only">Alerts</caption>
                <thead>
                  <tr>
                    <Th className="w-[52px]">On</Th>
                    <Th>Condition</Th>
                    <Th>Last fired (ET)</Th>
                    <Th>
                      <span className="sr-only">Actions</span>
                    </Th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((a) => (
                    <AlertRow key={a.id} alert={a} now={now} onEdit={() => openNewAlert(a, a.id)} />
                  ))}
                </tbody>
              </table>
            )}
          </div>
          <section aria-labelledby="alert-events-h" className="min-w-0 flex-[1_1_260px] rounded-ctl border border-border">
            <h3 id="alert-events-h" className="label border-b border-border bg-surface px-2.5 py-1.5">
              Recent events
            </h3>
            {events.length === 0 ? (
              <p className="px-2.5 py-3 text-[12px] text-muted">Nothing fired yet.</p>
            ) : (
              <ol className="max-h-[220px] overflow-y-auto" aria-label="Alert events, newest first">
                {events.slice(0, 50).map((e) => (
                  <li key={e.id} className="flex gap-2 border-b border-border px-2.5 py-1 text-[12px] last:border-b-0">
                    <span className="num shrink-0 text-muted">
                      {etDayPrefix(e.ts, now)}
                      {etTimeSec(e.ts)}
                    </span>
                    <span className="min-w-0 text-text-2">{e.message}</span>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

function AlertRow({ alert: a, now, onEdit }: { alert: Alert; now: number; onEdit: () => void }) {
  const [confirming, setConfirming] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    if (!confirming) return;
    const id = setTimeout(() => setConfirming(false), 4000);
    return () => clearTimeout(id);
  }, [confirming]);
  const condition = alertCondition(a);

  const toggle = async () => {
    const next = !a.enabled;
    if (next) unlockAudio();
    patchAlert(a.id, { enabled: next });
    try {
      const res = await api.updateAlert(a.id, { enabled: next });
      patchAlert(a.id, res);
    } catch (e) {
      patchAlert(a.id, { enabled: a.enabled });
      errorNotice("Alert not updated", e);
    }
  };
  const remove = async () => {
    if (!confirming) {
      setConfirming(true);
      return;
    }
    setBusy(true);
    try {
      await api.deleteAlert(a.id);
      patchAlert(a.id, null);
    } catch (e) {
      errorNotice("Alert not deleted", e);
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <tr className={cn("hover:bg-surface-2/60", !a.enabled && "text-muted")}>
      <Td>
        <Switch checked={a.enabled} onChange={toggle} label={`${a.enabled ? "Disable" : "Enable"} alert: ${condition}`} />
      </Td>
      <Td className="whitespace-normal">
        <span className={cn("num", a.enabled ? "text-text" : "text-muted")}>{condition}</span>
        {a.once && <span className="ml-1.5 rounded-[4px] bg-surface-2 px-1 text-[10px] font-semibold uppercase tracking-wide text-muted">once</span>}
        {a.note && <span className="ml-1.5 text-[12px] text-muted">· {a.note}</span>}
      </Td>
      <Td num className={a.lastFiredTs ? "text-text-2" : "text-muted"}>
        {a.lastFiredTs ? `${etDayPrefix(a.lastFiredTs, now)}${etTimeSec(a.lastFiredTs)}` : "Never"}
      </Td>
      <Td align="right" className="py-1">
        <div className="inline-flex items-center gap-1">
          <Button size="icon" variant="ghost" className="h-7 w-7" onClick={onEdit} aria-label={`Edit alert: ${condition}`}>
            <Pencil size={13} aria-hidden />
          </Button>
          <Button
            size={confirming ? "sm" : "icon"}
            variant={confirming ? "danger" : "ghost"}
            className={confirming ? "h-7" : "h-7 w-7"}
            disabled={busy}
            onClick={remove}
            aria-label={confirming ? `Confirm delete alert: ${condition}` : `Delete alert: ${condition}`}
          >
            <Trash2 size={13} aria-hidden />
            {confirming && "Delete?"}
          </Button>
        </div>
      </Td>
    </tr>
  );
}

/** Accessible on/off switch (role="switch"). */
export function Switch({ checked, onChange, label, disabled }: { checked: boolean; onChange: () => void; label: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={onChange}
      className={cn(
        "touch-target relative inline-flex h-[18px] w-[32px] shrink-0 items-center rounded-full border transition-colors disabled:opacity-50",
        checked ? "border-odds bg-odds-tint" : "border-border-strong bg-inset",
      )}
    >
      <span
        aria-hidden
        className={cn("absolute h-[12px] w-[12px] rounded-full transition-transform", checked ? "translate-x-[15px] bg-odds" : "translate-x-[2px] bg-[#4A5465]")}
      />
    </button>
  );
}

function useNotifyState(): [NotifyPrefs, NotifyPermission, () => void] {
  const [prefs, setPrefs] = React.useState<NotifyPrefs>({ desktop: true, sound: true });
  const [perm, setPerm] = React.useState<NotifyPermission>("default");
  React.useEffect(() => {
    const sync = () => {
      setPrefs(getNotifyPrefs());
      setPerm(notifyPermission());
    };
    sync();
    const un = subscribeNotifyPrefs(sync);
    window.addEventListener("focus", sync);
    return () => {
      un();
      window.removeEventListener("focus", sync);
    };
  }, []);
  return [prefs, perm, () => setPerm(notifyPermission())];
}

/** Desktop-notification permission + sound toggle. */
function DeliveryControls() {
  const [prefs, perm, refresh] = useNotifyState();
  const desktopOn = prefs.desktop && perm === "granted";
  const onDesktop = async () => {
    if (perm === "default") {
      const p = await requestNotifyPermission();
      if (p === "granted") setNotifyPrefs({ desktop: true });
      refresh();
      return;
    }
    if (perm === "granted") setNotifyPrefs({ desktop: !prefs.desktop });
  };
  const onSound = () => {
    const sound = !prefs.sound;
    setNotifyPrefs({ sound });
    if (sound) {
      unlockAudio();
      playAlertSound();
    }
  };
  return (
    <div className="flex items-center gap-1.5">
      {perm === "unsupported" ? (
        <span className="text-[12px] text-muted">Desktop notifications unsupported</span>
      ) : perm === "denied" ? (
        <span className="inline-flex items-center gap-1 text-[12px] text-muted">
          <BellOff size={13} aria-hidden /> Notifications blocked
          <InfoTip label="This browser blocks notifications for this site. Allow them in the site settings (lock icon in the address bar) to get desktop alerts. Sound and in-app toasts still work." />
        </span>
      ) : (
        <button
          type="button"
          onClick={onDesktop}
          aria-pressed={perm === "granted" ? desktopOn : undefined}
          className={cn(
            "touch-target inline-flex h-7 items-center gap-1.5 rounded-ctl border px-2.5 text-[12px] font-medium",
            desktopOn ? "border-border-strong bg-surface-2 text-text" : "border-border text-muted hover:text-text",
          )}
        >
          {desktopOn ? <Bell size={13} aria-hidden /> : <BellOff size={13} aria-hidden />}
          {perm === "default" ? "Enable desktop notifications" : "Desktop"}
        </button>
      )}
      <button
        type="button"
        onClick={onSound}
        aria-pressed={prefs.sound}
        className={cn(
          "touch-target inline-flex h-7 items-center gap-1.5 rounded-ctl border px-2.5 text-[12px] font-medium",
          prefs.sound ? "border-border-strong bg-surface-2 text-text" : "border-border text-muted hover:text-text",
        )}
      >
        {prefs.sound ? <Volume2 size={13} aria-hidden /> : <VolumeX size={13} aria-hidden />}
        Sound
      </button>
    </div>
  );
}
