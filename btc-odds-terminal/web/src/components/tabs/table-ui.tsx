"use client";
// Table cells, tags and ET date helpers shared by the bottom-tab tables. OWNER: tabs.
import * as React from "react";
import type { OrderStatus, Side, WindowResult } from "@shared/protocol";
import { cn } from "@/lib/utils";
import { STATUS_LABEL } from "./table-math";

const TZ = "America/New_York";
const fmtDay = new Intl.DateTimeFormat("en-US", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
const fmtMonthDay = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric" });

/** ET calendar day key, e.g. "10/20/2026". */
export const etDayKey = (ms: number) => fmtDay.format(ms);

/** "Oct 19 · " when `ms` is not on the same ET day as `now`, else "". */
export function etDayPrefix(ms: number, now: number): string {
  return etDayKey(ms) === etDayKey(now) ? "" : `${fmtMonthDay.format(ms)} · `;
}

export function Th({ children, className, align = "left", title }: { children?: React.ReactNode; className?: string; align?: "left" | "right" | "center"; title?: string }) {
  return (
    <th
      scope="col"
      title={title}
      className={cn(
        "label sticky top-0 z-[1] whitespace-nowrap border-b border-border bg-surface px-2.5 py-1.5 font-semibold",
        align === "right" ? "text-right" : align === "center" ? "text-center" : "text-left",
        className,
      )}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  className,
  align = "left",
  num,
  title,
}: {
  children?: React.ReactNode;
  className?: string;
  align?: "left" | "right" | "center";
  num?: boolean;
  title?: string;
}) {
  return (
    <td
      title={title}
      className={cn(
        "whitespace-nowrap border-b border-border px-2.5 py-1.5 text-[12px]",
        num && "num",
        align === "right" ? "text-right" : align === "center" ? "text-center" : "text-left",
        className,
      )}
    >
      {children}
    </td>
  );
}

export function SideTag({ side }: { side: Side }) {
  return (
    <span
      className={cn(
        "num inline-flex h-[18px] items-center rounded-[4px] px-1.5 text-[11px] font-bold",
        side === "up" ? "bg-up-tint text-up" : "bg-down-tint text-down",
      )}
    >
      {side === "up" ? "UP" : "DOWN"}
    </span>
  );
}

export function ModeTag({ live }: { live: boolean }) {
  if (!live) return null;
  return <span className="num ml-1 rounded-[4px] border border-down/60 px-1 text-[10px] font-bold leading-[14px] text-down">LIVE</span>;
}

const STATUS_CLASS: Record<OrderStatus, string> = {
  pending: "text-muted border-border-strong",
  open: "text-odds-text border-odds/50 bg-odds-tint",
  partial: "text-odds-text border-odds/50 bg-odds-tint",
  filled: "text-up border-up/40 bg-up-tint",
  cancelled: "text-muted border-border-strong",
  rejected: "text-down border-down/50 bg-down-tint",
};

export function StatusTag({ status, title }: { status: OrderStatus; title?: string }) {
  return (
    <span title={title} className={cn("inline-flex h-[18px] items-center rounded-[4px] border px-1.5 text-[11px] font-semibold", STATUS_CLASS[status])}>
      {STATUS_LABEL[status]}
    </span>
  );
}

export function ResultTile({ result }: { result: WindowResult }) {
  return (
    <span
      aria-label={result === "up" ? "Settled up" : "Settled down"}
      className={cn(
        "num inline-flex h-[18px] w-[18px] items-center justify-center rounded-[4px] text-[11px] font-bold",
        result === "up" ? "bg-up-tint text-up" : "bg-down-tint text-down",
      )}
    >
      {result === "up" ? "U" : "D"}
    </span>
  );
}

/** Text color for a signed value (positive green, negative red, ~0 muted). */
export function signClass(v: number | null | undefined, eps = 0.005): string {
  if (v == null || !Number.isFinite(v) || Math.abs(v) < eps) return "text-muted";
  return v > 0 ? "text-up" : "text-down";
}
