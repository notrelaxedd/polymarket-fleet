"use client";
// Toasts for server / client notices (store.notices: bracket triggered, order rejected, trading disabled…).
// OWNER: trading-ui. Bottom-right stack. info / success auto-dismiss after 6s, warn after 10s; errors stay until
// dismissed. The countdown only runs while the page is visible (a toast pushed in a background tab is still
// there when the user comes back) and pauses while the pointer is over the stack.
import * as React from "react";
import { X } from "lucide-react";
import { useTerminal } from "@/lib/store";
import { cn } from "@/lib/utils";

const TONE = { info: "border-odds/50", success: "border-up/60", warn: "border-ptb/60", error: "border-down/60" } as const;
const TTL_MS = { info: 6000, success: 6000, warn: 10_000, error: null } as const;
const MAX_SHOWN = 5;

function usePageVisible(): boolean {
  const [visible, setVisible] = React.useState(true);
  React.useEffect(() => {
    const on = () => setVisible(!document.hidden);
    on();
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return visible;
}

export function Notices() {
  const notices = useTerminal((s) => s.notices);
  const dismiss = useTerminal((s) => s.dismissNotice);
  const visible = usePageVisible();
  const [hover, setHover] = React.useState(false);
  // per-notice remaining time, kept across pauses
  const left = React.useRef(new Map<string, number>());

  React.useEffect(() => {
    const ids = new Set(notices.map((n) => n.id));
    for (const id of left.current.keys()) if (!ids.has(id)) left.current.delete(id);
    if (!visible || hover) return;
    const started = Date.now();
    const timers = notices.flatMap((n) => {
      const ttl = TTL_MS[n.level];
      if (ttl == null) return [];
      const remain = left.current.get(n.id) ?? ttl;
      return [{ id: n.id, remain, t: setTimeout(() => dismiss(n.id), remain) }];
    });
    return () => {
      const spent = Date.now() - started;
      for (const x of timers) {
        clearTimeout(x.t);
        left.current.set(x.id, Math.max(500, x.remain - spent));
      }
    };
  }, [notices, dismiss, visible, hover]);

  if (!notices.length) return null;
  const shown = notices.slice(-MAX_SHOWN);
  return (
    <div
      aria-live="polite"
      onPointerEnter={() => setHover(true)}
      onPointerLeave={() => setHover(false)}
      className="pointer-events-none fixed bottom-3 right-3 z-40 flex w-[min(320px,calc(100vw-32px))] flex-col gap-2"
    >
      {notices.length > MAX_SHOWN && (
        <div className="pointer-events-auto self-end rounded-card bg-surface-2 px-2 py-1 text-[11px] text-text-2">
          +{notices.length - MAX_SHOWN} older
        </div>
      )}
      {shown.map((n) => (
        <div
          key={n.id}
          role={n.level === "error" ? "alert" : undefined}
          className={cn("pointer-events-auto flex items-start gap-2 rounded-card border bg-surface-2 py-2 pl-3 pr-1 text-[12px]", TONE[n.level])}
        >
          <div className="min-w-0 flex-1">
            <div className="font-semibold text-text">{n.title}</div>
            <div className="break-words text-text-2">{n.message}</div>
          </div>
          <button
            type="button"
            aria-label="Dismiss notice"
            onClick={() => dismiss(n.id)}
            className="-my-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted hover:bg-border hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-odds [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11"
          >
            <X className="h-3.5 w-3.5" aria-hidden />
          </button>
        </div>
      ))}
    </div>
  );
}
