"use client";
// Pair relationship tab: three plain-SVG cards (BTC vs strike -> Up price, Lag, Sensitivity) in an auto-fit grid
// (min 280px). Data: GET /api/analysis?scope=<sample filter> -> store.analysis[scope]. OWNER: tabs.
import { AlertTriangle } from "lucide-react";
import { useTerminal } from "@/lib/store";
import { LagCard } from "./pair/LagCard";
import { ScatterCard } from "./pair/ScatterCard";
import { SensitivityCard } from "./pair/SensitivityCard";
import { useAnalysis } from "./pair/useAnalysis";

export function PairRelationship() {
  const scope = useTerminal((s) => s.sampleScope);
  const { data, error } = useAnalysis(scope);
  // a stale payload of another scope must not be shown under this filter
  const payload = data && data.scope === scope ? data : null;
  return (
    <div className="p-2.5">
      {error && (
        <div role="status" className="mb-2 flex items-center gap-1.5 rounded-ctl border border-[#5a4316] bg-ptb-tint px-2 py-1 text-[12px] text-ptb">
          <AlertTriangle size={13} aria-hidden />
          <span>
            Analysis didn&apos;t refresh: {error}
            {payload ? " · showing the last result" : ""}
          </span>
        </div>
      )}
      <div className="grid gap-2.5" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
        <ScatterCard data={payload} />
        <LagCard data={payload} />
        <SensitivityCard data={payload} />
      </div>
    </div>
  );
}
