"use client";
import * as React from "react";
import type { FeedId, FeedStatus } from "@shared/protocol";
import { Tip } from "@/components/ui/tooltip";
import { useTerminal, serverNow } from "@/lib/store";
import { useServerNow } from "@/lib/hooks";
import { ageLabel, feedLabel, feedShort } from "@/lib/format";
import { cn } from "@/lib/utils";

const ORDER: FeedId[] = ["coinbase", "kraken", "bitstamp", "polymarket"];

export function FeedDots() {
  const feeds = useTerminal((s) => s.feeds);
  const connected = useTerminal((s) => s.connected);
  useServerNow(1000);
  const now = serverNow();
  const byId = new Map(feeds.map((f) => [f.id, f] as const));
  return (
    <div className="flex items-center gap-2" role="group" aria-label="Feed status">
      {ORDER.map((id) => {
        const f: FeedStatus | undefined = byId.get(id);
        const state = !connected ? "down" : (f?.state ?? "down");
        const age = f ? (f.lastMsgTs != null ? Math.max(0, now - f.lastMsgTs) : f.ageMs) : null;
        const showAge = connected && state === "stale";
        const text = !connected
          ? "offline (no backend connection)"
          : !f
            ? "waiting for data"
            : state === "live"
              ? `live · last update ${ageLabel(age)} ago`
              : state === "stale"
                ? `stale · no update for ${ageLabel(age)}`
                : `down${f.detail ? ` · ${f.detail}` : ""}`;
        return (
          <Tip key={id} label={`${feedLabel(id)}: ${text}`}>
            <button
              type="button"
              aria-label={`${feedLabel(id)} feed ${state}`}
              className="touch-target flex items-center gap-1 rounded px-0.5 text-[11px] font-semibold text-muted"
            >
              <span
                aria-hidden
                className={cn(
                  "inline-block h-2 w-2 rounded-full",
                  state === "live" && "bg-up",
                  state === "stale" && "bg-neg-edge",
                  state === "down" && "bg-down",
                )}
              />
              {showAge && (
                <span className="num text-neg-edge">
                  {feedShort(id)} {ageLabel(age)}
                </span>
              )}
            </button>
          </Tip>
        );
      })}
    </div>
  );
}
