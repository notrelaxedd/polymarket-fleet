"use client";
// Alert delivery in the browser: desktop notification + a short WebAudio beep + an in-app toast. OWNER: tabs.
// ingest.ts calls notifyAlert() for every `alertFired` message. Preferences are per browser (localStorage,
// wrapped in try/catch). Permission is requested from a user gesture (New alert dialog submit, the Alerts tab's
// "Enable notifications" button); browsers ignore requests made outside one. Audio is unlocked by the first
// pointer / key / touch anywhere on the page (installAudioUnlock, a one-time capture listener), so a beep fired
// from a WebSocket message is not silently dropped; when it still is, the toast says so. A desktop notification
// that cannot be shown (Android Chrome has no Notification constructor) falls back to the service worker
// registration and then to the in-app toast — an alert is never dropped silently.

import type { AlertEvent } from "@shared/protocol";
import { useTerminal } from "./store";

export interface NotifyPrefs {
  desktop: boolean;
  sound: boolean;
}

export type NotifyPermission = NotificationPermission | "unsupported";

const KEY = "bot.notify";
const DEFAULT_PREFS: NotifyPrefs = { desktop: true, sound: true };
/** several alerts firing in the same second make one beep, not a chord */
export const BEEP_MIN_GAP_MS = 700;

type Listener = (p: NotifyPrefs) => void;
const listeners = new Set<Listener>();

export function getNotifyPrefs(): NotifyPrefs {
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(KEY) : null;
    if (raw) {
      const p = JSON.parse(raw) as Partial<NotifyPrefs>;
      return { desktop: p.desktop !== false, sound: p.sound !== false };
    }
  } catch {}
  return { ...DEFAULT_PREFS };
}

export function setNotifyPrefs(p: Partial<NotifyPrefs>): NotifyPrefs {
  const next = { ...getNotifyPrefs(), ...p };
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {}
  for (const l of listeners) l(next);
  return next;
}

/** Subscribe to preference changes (Alerts tab toggles). Returns the unsubscribe function. */
export function subscribeNotifyPrefs(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** Current Notification permission without prompting. */
export function notifyPermission(): NotifyPermission {
  if (typeof Notification === "undefined") return "unsupported";
  return Notification.permission;
}

/** Ask for Notification permission (call from a user gesture, e.g. the New alert dialog). */
export async function requestNotifyPermission(): Promise<NotifyPermission> {
  if (typeof Notification === "undefined") return "unsupported";
  if (Notification.permission !== "default") return Notification.permission;
  try {
    // Safari < 16 only supports the callback form
    return await new Promise<NotificationPermission>((resolve) => {
      const r = Notification.requestPermission(resolve);
      if (r && typeof (r as Promise<NotificationPermission>).then === "function") void (r as Promise<NotificationPermission>).then(resolve);
    });
  } catch {
    return Notification.permission;
  } finally {
    for (const l of listeners) l(getNotifyPrefs());
  }
}

// ---- sound ----

let audio: AudioContext | null = null;
let lastBeep = 0;

function audioCtx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (audio && audio.state !== "closed") return audio;
  const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctx) return null;
  try {
    audio = new Ctx();
  } catch {
    audio = null;
  }
  return audio;
}

/**
 * Create / resume the shared AudioContext from a user gesture so later beeps (fired from a WebSocket message,
 * not a gesture) are allowed by the autoplay policy.
 */
export function unlockAudio(): void {
  const ctx = audioCtx();
  if (ctx && ctx.state === "suspended") void ctx.resume().catch(() => {});
}

const UNLOCK_EVENTS = ["pointerdown", "keydown", "touchend"] as const;
let unlockInstalled = false;

/**
 * Unlock audio on the first user gesture anywhere on the page (capture phase, so a handler that stops
 * propagation cannot hide it). The listeners remove themselves once the context is running. Idempotent;
 * called when this module loads in the browser.
 */
export function installAudioUnlock(target: Pick<Window, "addEventListener" | "removeEventListener"> | null = typeof window === "undefined" ? null : window): void {
  if (!target || unlockInstalled) return;
  unlockInstalled = true;
  const onGesture = () => {
    unlockAudio();
    const ctx = audio;
    const done = () => {
      for (const t of UNLOCK_EVENTS) target.removeEventListener(t, onGesture, true);
    };
    if (!ctx) return done(); // no WebAudio: nothing to unlock
    if (ctx.state === "running") done();
    else void ctx.resume().then(() => ctx.state === "running" && done(), () => {});
  };
  for (const t of UNLOCK_EVENTS) target.addEventListener(t, onGesture, true);
}

if (typeof window !== "undefined") installAudioUnlock();

/** "played" | "blocked" (the browser has not allowed audio yet: no gesture since load) | "skipped" (throttled / no WebAudio) */
export type SoundResult = "played" | "blocked" | "skipped";

/** Short two-tone beep via WebAudio (no asset). Throttled to one per BEEP_MIN_GAP_MS. */
export function playAlertSound(): SoundResult {
  const nowMs = Date.now();
  if (nowMs - lastBeep < BEEP_MIN_GAP_MS) return "skipped";
  const ctx = audioCtx();
  if (!ctx) return "skipped";
  if (ctx.state === "suspended") {
    // outside a gesture this usually stays suspended; try anyway (some browsers allow it after an earlier one)
    void ctx.resume().catch(() => {});
    return "blocked";
  }
  lastBeep = nowMs;
  try {
    const t0 = ctx.currentTime + 0.01;
    const tone = (freq: number, start: number, dur: number) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = "sine";
      o.frequency.setValueAtTime(freq, start);
      g.gain.setValueAtTime(0.0001, start);
      g.gain.exponentialRampToValueAtTime(0.18, start + 0.015);
      g.gain.exponentialRampToValueAtTime(0.0001, start + dur);
      o.connect(g).connect(ctx.destination);
      o.start(start);
      o.stop(start + dur + 0.02);
    };
    tone(880, t0, 0.14);
    tone(1320, t0 + 0.16, 0.2);
    return "played";
  } catch {
    return "blocked";
  }
}

// ---- delivery ----

export interface DeliveryPlan {
  sound: boolean;
  desktop: boolean;
  toast: boolean;
}

/**
 * What to do for one fired alert. The in-app toast shows whenever the desktop notification does not (no
 * permission, turned off) and also while the page has focus; a desktop notification only when the page is not
 * focused (a focused user sees the toast) and permission is granted.
 */
export function deliveryPlan(prefs: NotifyPrefs, permission: NotifyPermission, focused: boolean): DeliveryPlan {
  const desktop = prefs.desktop && permission === "granted" && !focused;
  return { sound: prefs.sound, desktop, toast: focused || !desktop };
}

export const SOUND_BLOCKED_HINT = "Click anywhere to enable alert sound.";

/** Toast text for a fired alert; mentions a blocked beep so a silent alert is not mistaken for "sound off". */
export function alertToastMessage(message: string, sound: SoundResult | null): string {
  return sound === "blocked" ? `${message} · ${SOUND_BLOCKED_HINT}` : message;
}

const NOTIFY_TITLE = "BTC Odds Terminal · alert";

/**
 * Show a desktop notification: the constructor, else (it throws on Android Chrome and some embedded browsers)
 * the service worker registration. Resolves false when neither worked.
 */
export async function showDesktopNotification(body: string, tag: string): Promise<boolean> {
  try {
    const n = new Notification(NOTIFY_TITLE, { body, tag });
    n.onclick = () => {
      window.focus();
      n.close();
    };
    return true;
  } catch {}
  try {
    const reg = typeof navigator !== "undefined" ? await navigator.serviceWorker?.getRegistration?.() : undefined;
    if (reg) {
      await reg.showNotification(NOTIFY_TITLE, { body, tag });
      return true;
    }
  } catch {}
  return false;
}

/** Called by ingest for every `alertFired` message. */
export function notifyAlert(e: AlertEvent): void {
  if (typeof window === "undefined") return;
  const focused = typeof document !== "undefined" && document.visibilityState === "visible" && (document.hasFocus?.() ?? true);
  const plan = deliveryPlan(getNotifyPrefs(), notifyPermission(), focused);
  const sound = plan.sound ? playAlertSound() : null;
  const toast = () =>
    useTerminal.getState().pushNotice({ id: `alert-${e.id}`, ts: e.ts, level: "info", title: "Alert", message: alertToastMessage(e.message, sound) });
  if (plan.desktop) {
    // the desktop notification is the only visible channel here: fall back to the toast when it fails
    void showDesktopNotification(e.message, `alert-${e.alertId}-${e.id}`).then((ok) => {
      if (!ok && !plan.toast) toast();
    });
  }
  if (plan.toast) toast();
}
