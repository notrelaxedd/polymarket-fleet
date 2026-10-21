"use client";
// Orders + fills for the bottom-tab sample, beyond what the snapshot / WS hold. The snapshot carries at most 200
// orders and 200 fills from the last 24h and the store caps fills at 500, so "Last 20" / "All" need
// GET /api/orders + GET /api/fills (newest first). The pages are merged with the store locally (the store keeps
// winning for rows it has: WS upserts are fresher) and `complete` says whether the sample is fully loaded.
// OWNER: tabs.
import * as React from "react";
import type { AnalysisScope, Fill, Order } from "@shared/protocol";
import { api } from "@/lib/api";

/** rows per page (the backend's clamp) */
export const TRADES_FETCH_LIMIT = 1000;

export interface SampleTrades {
  orders: Order[];
  fills: Fill[];
  /** REST pages fetched for this scope / window (false while loading, after an error, or in no-fetch cases) */
  loaded: boolean;
  loading: boolean;
  error: string | null;
  retry: () => void;
}

interface Page {
  key: string;
  orders: Order[];
  fills: Fill[];
}

/** `anchorId`: the window the sample is anchored to (re-fetched on rollover). */
export function useSampleTrades(scope: AnalysisScope, anchorId: string | null): SampleTrades {
  const [page, setPage] = React.useState<Page | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [nonce, setNonce] = React.useState(0);
  const key = `${scope}:${anchorId ?? ""}`;

  React.useEffect(() => {
    if (scope === "window" && !anchorId) return;
    let alive = true;
    const q = scope === "window" ? { limit: TRADES_FETCH_LIMIT, windowId: anchorId ?? undefined } : { limit: TRADES_FETCH_LIMIT };
    setLoading(true);
    Promise.all([api.orders(q), api.fills(q)])
      .then(([orders, fills]) => {
        if (!alive) return;
        setPage({ key, orders, fills });
        setError(null);
      })
      .catch(() => {
        if (!alive) return;
        setPage(null);
        setError("Couldn't load older orders and fills — showing loaded orders only.");
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- key covers scope + anchorId
  }, [key, nonce]);

  const retry = React.useCallback(() => setNonce((n) => n + 1), []);
  const cur = page && page.key === key ? page : null;
  return { orders: cur?.orders ?? EMPTY_O, fills: cur?.fills ?? EMPTY_F, loaded: cur != null, loading, error, retry };
}

const EMPTY_O: Order[] = [];
const EMPTY_F: Fill[] = [];
