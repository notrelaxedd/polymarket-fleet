"use client";
// Small form pieces shared by the order ticket, Edit bracket and settings. OWNER: trading-ui.
import * as React from "react";
import { cn } from "@/lib/utils";
import { sanitizeDecimal } from "./ticket-math";

export const fieldCls =
  "num touch-target h-9 w-full rounded-ctl border border-border-strong bg-inset px-2.5 text-right text-[13px] text-text outline-none placeholder:text-label focus:border-odds disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-down";

/** Decimal text input: digits + one dot, mono, right aligned, with an optional unit suffix and error line. */
export function NumInput({
  id,
  value,
  onChange,
  suffix,
  prefix,
  error,
  integer,
  className,
  inputClassName,
  ...rest
}: Omit<React.InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "prefix"> & {
  id: string;
  value: string;
  onChange: (v: string) => void;
  suffix?: string;
  prefix?: string;
  error?: string | null;
  integer?: boolean;
  inputClassName?: string;
}) {
  const errId = `${id}-err`;
  return (
    <div className={className}>
      <div className="relative">
        {prefix && (
          <span aria-hidden className="num pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-[13px] text-label">
            {prefix}
          </span>
        )}
        <input
          id={id}
          inputMode={integer ? "numeric" : "decimal"}
          autoComplete="off"
          spellCheck={false}
          value={value}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errId : undefined}
          onChange={(e) => onChange(integer ? e.target.value.replace(/[^0-9]/g, "") : sanitizeDecimal(e.target.value))}
          className={cn(fieldCls, prefix && "pl-6", suffix && "pr-7", inputClassName)}
          {...rest}
        />
        {suffix && (
          <span aria-hidden className="num pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[12px] text-label">
            {suffix}
          </span>
        )}
      </div>
      {error && (
        <p id={errId} className="mt-1 text-[11px] leading-4 text-down">
          {error}
        </p>
      )}
    </div>
  );
}

/** Checkbox styled for the dark theme (native input: keyboard + screen readers for free). */
export function Check({
  id,
  checked,
  onChange,
  disabled,
  label,
  className,
}: {
  id: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  label?: string;
  className?: string;
}) {
  return (
    <input
      id={id}
      type="checkbox"
      checked={checked}
      disabled={disabled}
      aria-label={label}
      onChange={(e) => onChange(e.target.checked)}
      className={cn("h-4 w-4 shrink-0 cursor-pointer accent-[#5B9CFF] disabled:cursor-not-allowed", className)}
    />
  );
}

/** Mono value chip (positions: mark / TP / SL / time exit). */
export function ValueChip({ k, v, tone }: { k: string; v: React.ReactNode; tone?: "up" | "down" | "odds" | "ptb" | "muted" }) {
  const c = tone === "up" ? "text-up" : tone === "down" ? "text-down" : tone === "odds" ? "text-odds-text" : tone === "ptb" ? "text-ptb" : "text-text-2";
  return (
    <span className="inline-flex h-6 items-center gap-1 rounded-[6px] border border-border bg-surface-2 px-1.5 text-[11px]">
      <span className="text-label">{k}</span>
      <span className={cn("num font-medium", c)}>{v}</span>
    </span>
  );
}
