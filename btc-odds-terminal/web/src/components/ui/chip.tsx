"use client";
import * as React from "react";
import { cn } from "@/lib/utils";

/** Toggle chip with optional color swatch shown when on. */
export const Chip = React.forwardRef<
  HTMLButtonElement,
  Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "onChange"> & {
    on: boolean;
    swatch?: string;
    dashed?: boolean;
  }
>(({ on, swatch, dashed, className, children, ...props }, ref) => (
  <button
    ref={ref}
    type="button"
    aria-pressed={on}
    className={cn(
      "touch-target inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-ctl border px-2.5 text-[12px] font-medium transition-colors",
      on
        ? "border-border-strong bg-surface-2 text-text"
        : "border-border bg-transparent text-muted hover:border-border-strong hover:text-text",
      className,
    )}
    {...props}
  >
    {swatch && (
      <span
        aria-hidden
        className="inline-block h-[3px] w-3.5 rounded-full"
        style={{
          background: on
            ? dashed
              ? `repeating-linear-gradient(90deg, ${swatch} 0 4px, transparent 4px 6px)`
              : swatch
            : "#4a5465",
        }}
      />
    )}
    {children}
  </button>
));
Chip.displayName = "Chip";
