"use client";
import * as React from "react";
import { cn } from "@/lib/utils";

export interface SegmentedOption<T extends string | number> {
  value: T;
  label: React.ReactNode;
  ariaLabel?: string;
}

/** Radio-group style segmented control. */
export function Segmented<T extends string | number>({
  value,
  onChange,
  options,
  className,
  size = "md",
  ariaLabel,
}: {
  value: T;
  onChange: (v: T) => void;
  options: SegmentedOption<T>[];
  className?: string;
  size?: "sm" | "md";
  ariaLabel?: string;
}) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={cn("inline-flex rounded-ctl border border-border-strong bg-inset p-0.5", className)}
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={String(o.value)}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={o.ariaLabel}
            onClick={() => onChange(o.value)}
            className={cn(
              "touch-target rounded-[6px] px-2.5 text-[12px] font-medium transition-colors",
              size === "sm" ? "h-6" : "h-7",
              active ? "bg-surface-2 text-text shadow-[inset_0_0_0_1px_#2A3442]" : "text-muted hover:text-text",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
