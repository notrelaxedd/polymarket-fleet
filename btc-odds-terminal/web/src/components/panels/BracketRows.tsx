"use client";
// The four OCO bracket legs (checkbox + value row each). Used by the order ticket and Edit bracket. OWNER: trading-ui.
import type { Side } from "@shared/protocol";
import { cn } from "@/lib/utils";
import { Check, NumInput } from "./fields";
import type { BracketFields, TicketErrors } from "./ticket-math";

type OnKey = "tpOn" | "slOn" | "btcStopOn" | "timeExitOn";
type ValKey = "tpCents" | "slCents" | "btcStop" | "timeExitSec";

interface Row {
  on: OnKey;
  val: ValKey;
  label: string;
  /** accessible name of the value input */
  name: string;
  suffix?: string;
  prefix?: string;
  integer?: boolean;
  tone: string;
}

export function bracketRowsFor(side: Side): Row[] {
  return [
    { on: "tpOn", val: "tpCents", label: "Take profit (¢)", name: "Take profit price, cents", suffix: "¢", tone: "text-up" },
    { on: "slOn", val: "slCents", label: "Stop loss (¢)", name: "Stop loss price, cents", suffix: "¢", tone: "text-down" },
    {
      on: "btcStopOn",
      val: "btcStop",
      label: side === "up" ? "Stop if BTC <" : "Stop if BTC >",
      name: side === "up" ? "Stop if BTC falls below, USD" : "Stop if BTC rises above, USD",
      prefix: "$",
      tone: "text-text-2",
    },
    { on: "timeExitOn", val: "timeExitSec", label: "Time exit at T− (s)", name: "Time exit, seconds before window end", suffix: "s", integer: true, tone: "text-ptb" },
  ];
}

export function BracketRows({
  idPrefix,
  side,
  value,
  onChange,
  errors,
  disabled,
}: {
  idPrefix: string;
  side: Side;
  value: BracketFields;
  onChange: (patch: Partial<BracketFields>) => void;
  errors: TicketErrors;
  disabled?: boolean;
}) {
  return (
    <div role="group" aria-label="Bracket legs" className="flex flex-col">
      {bracketRowsFor(side).map((r) => {
        const on = value[r.on];
        const cbId = `${idPrefix}-${r.on}`;
        const inId = `${idPrefix}-${r.val}`;
        return (
          <div key={r.on} className="py-[3px]">
            <div className="flex items-center gap-2">
              <label htmlFor={cbId} className={cn("touch-target flex flex-1 cursor-pointer items-center gap-2 text-[12px]", disabled || !on ? "text-muted" : "text-text-2")}>
                <Check id={cbId} checked={on} disabled={disabled} onChange={(v) => onChange({ [r.on]: v } as Partial<BracketFields>)} />
                <span aria-hidden className={cn("inline-block h-[3px] w-2.5 shrink-0 rounded-full bg-current", on && !disabled ? r.tone : "text-label")} />
                {r.label}
              </label>
              <NumInput
                id={inId}
                aria-label={r.name}
                className="w-[104px]"
                inputClassName="h-8"
                value={value[r.val]}
                onChange={(v) => onChange({ [r.val]: v } as Partial<BracketFields>)}
                disabled={disabled || !on}
                suffix={r.suffix}
                prefix={r.prefix}
                integer={r.integer}
                aria-invalid={on && errors[r.val] ? true : undefined}
                aria-describedby={on && errors[r.val] ? `${inId}-err-row` : undefined}
              />
            </div>
            {on && !disabled && errors[r.val] && (
              <p id={`${inId}-err-row`} className="pl-6 pt-0.5 text-[11px] leading-4 text-down">
                {errors[r.val]}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}
