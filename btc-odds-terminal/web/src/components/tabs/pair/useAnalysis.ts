"use client";
// Loads GET /api/analysis for the bottom-tab sample filter into store.analysis[scope] and keeps it fresh.
// Mock mode answers the same call from lib/mock/analysis.ts (computed from the simulator's series). OWNER: tabs.
import * as React from "react";
import type { AnalysisPayload, AnalysisScope } from "@shared/protocol";
import { api } from "@/lib/api";
import { isApiError } from "@/lib/api-error";
import { useTerminal } from "@/lib/store";

/** Refresh period per scope: the current window changes every second; longer samples move slowly. */
export const ANALYSIS_REFRESH_MS: Record<AnalysisScope, number> = { window: 3_000, last20: 10_000, all: 30_000 };

export interface AnalysisState {
  data: AnalysisPayload | null;
  error: string | null;
  loading: boolean;
}

export function useAnalysis(scope: AnalysisScope): AnalysisState {
  const data = useTerminal((s) => s.analysis[scope] ?? null);
  const setAnalysis = useTerminal((s) => s.setAnalysis);
  // refetch right away when the market rolls over (new window = new sample)
  const windowId = useTerminal((s) => (s.replay.active ? null : s.current?.id ?? null));
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);

  React.useEffect(() => {
    let alive = true;
    let inflight = false;
    const load = async () => {
      if (inflight || (typeof document !== "undefined" && document.visibilityState === "hidden")) return;
      inflight = true;
      setLoading(true);
      try {
        const p = await api.analysis(scope);
        if (!alive) return;
        setAnalysis(scope, p);
        setError(null);
      } catch (e) {
        if (alive) setError(isApiError(e) ? e.message : "Analysis unavailable");
      } finally {
        inflight = false;
        if (alive) setLoading(false);
      }
    };
    void load();
    const id = setInterval(load, ANALYSIS_REFRESH_MS[scope]);
    const onVis = () => document.visibilityState === "visible" && void load();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive = false;
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [scope, windowId, setAnalysis]);

  return { data, error, loading };
}
