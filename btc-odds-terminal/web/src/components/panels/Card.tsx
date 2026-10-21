"use client";
// Shared right-panel card + summary row (from the step 2 RightPanel). OWNER: trading-ui.
import * as React from "react";
import { cn } from "@/lib/utils";

export function Card({ title, right, children, className }: { title: string; right?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("rounded-card border border-border bg-surface", className)}>
      <header className="flex items-center justify-between gap-2 px-3 pt-2.5">
        <h2 className="label">{title}</h2>
        {right}
      </header>
      <div className="p-3 pt-2">{children}</div>
    </section>
  );
}

export function SumRow({ k, v, color }: { k: string; v: React.ReactNode; color?: string }) {
  return (
    <div className="flex items-center justify-between py-[3px] text-[12px]">
      <span className="text-muted">{k}</span>
      <span className="num font-medium" style={{ color }}>
        {v}
      </span>
    </div>
  );
}
