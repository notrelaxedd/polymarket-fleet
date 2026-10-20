"use client";
import { Info } from "lucide-react";
import { Tip } from "./tooltip";

/** Small focusable info icon with a tooltip, for caveats next to a label. */
export function InfoTip({ label, side = "bottom" }: { label: string; side?: "top" | "right" | "bottom" | "left" }) {
  return (
    <Tip label={label} side={side}>
      <button
        type="button"
        aria-label={label}
        // invisible padding enlarges the hit area without growing the label row
        className="relative inline-flex shrink-0 items-center text-muted before:absolute before:-inset-1.5 before:content-[''] [@media(pointer:coarse)]:before:-inset-[17px] hover:text-text focus-visible:text-text"
      >
        <Info size={11} aria-hidden />
      </button>
    </Tip>
  );
}
