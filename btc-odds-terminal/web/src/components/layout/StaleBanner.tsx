"use client";
import { TriangleAlert, WifiOff } from "lucide-react";
import type { FeedStatus } from "@shared/protocol";
import { useTerminal } from "@/lib/store";
import { ageLabel, feedLabel } from "@/lib/format";

const bad = (f: FeedStatus) => f.state !== "live";

export function StaleBanner() {
  const feeds = useTerminal((s) => s.feeds);
  const connected = useTerminal((s) => s.connected);

  const msgs: string[] = [];
  const ex = feeds.filter((f) => f.id !== "polymarket");
  const pm = feeds.find((f) => f.id === "polymarket");
  if (ex.length > 0 && ex.every(bad)) {
    msgs.push(`BRTI proxy is ${ex.some((f) => f.state === "down") && ex.every((f) => f.state === "down") ? "down" : "stale"} — all exchange feeds are ${ex.map((f) => `${feedLabel(f.id)} ${f.state}${f.state === "stale" ? ` ${ageLabel(f.ageMs)}` : ""}`).join(", ")}`);
  }
  if (pm && bad(pm)) {
    msgs.push(`Polymarket feed is ${pm.state}${pm.state === "stale" ? ` (${ageLabel(pm.ageMs)})` : ""} — odds and fair-value edge may be out of date`);
  }

  if (connected && msgs.length === 0) return null;
  if (!connected) {
    return (
      <div role="alert" className="flex shrink-0 items-center gap-2 border-b border-down/40 bg-down-tint px-3 py-1.5 text-[12px] font-medium text-down">
        <WifiOff size={14} aria-hidden /> Disconnected from backend — reconnecting…
      </div>
    );
  }
  return (
    <div role="alert" className="flex shrink-0 flex-col gap-0.5 border-b border-neg-edge/40 bg-ptb-tint px-3 py-1.5 text-[12px] font-medium text-ptb">
      {msgs.map((m) => (
        <div key={m} className="flex items-center gap-2">
          <TriangleAlert size={14} aria-hidden className="shrink-0" /> {m}
        </div>
      ))}
    </div>
  );
}
