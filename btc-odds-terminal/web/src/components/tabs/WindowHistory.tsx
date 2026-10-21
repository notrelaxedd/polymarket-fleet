"use client";
// Window history: one row per window (time, price to beat, settle, result U/D, my P&L, max edge seen); click a
// finished row (or its Replay button) -> startReplay(windowId). Follows the bottom-tab sample filter. OWNER: tabs.
// My P&L is for the current trading mode only and flags [FEE] when a fee may be unknown; P&L and max edge read
// "n/a" (not "—") for windows older than the loaded positions / fair history.
import * as React from "react";
import { AlertTriangle, History, Play } from "lucide-react";
import type { MarketWindow, Position } from "@shared/protocol";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { btc, etRange, signedCents, signedUsd } from "@/lib/format";
import { startReplay } from "@/lib/replay";
import { liveNow, useTerminal } from "@/lib/store";
import { cn } from "@/lib/utils";
import { Empty } from "./Empty";
import { ResultTile, Td, Th, etDayPrefix, signClass } from "./table-ui";
import { earliestTs, maxEdgeByWindow, mergeById, mergeWindows, pnlByWindow, positionsCoverFrom, scopeAnchor, windowsInScope } from "./table-math";

const FETCH_LIMIT = { window: 2, last20: 25, all: 200 } as const;
/** GET /api/positions page (the backend's clamp) */
const POSITIONS_LIMIT = 1000;
/** automatic retry after a failed load */
const RETRY_MS = 30_000;
/** max edge is recomputed from the (in-place mutated) fair series this often */
const EDGE_REFRESH_MS = 5_000;
const NA_EDGE = "Older than the loaded fair history (about the last 3–4h, since this page loaded)";
const NA_PNL = "Older than the loaded positions";

export function WindowHistory() {
  const scope = useTerminal((s) => s.sampleScope);
  const current = useTerminal((s) => s.current);
  const windows = useTerminal((s) => s.windows);
  const positions = useTerminal((s) => s.positions);
  const fills = useTerminal((s) => s.fills);
  const mode = useTerminal((s) => s.settings?.mode ?? "paper");
  const feeKnown = useTerminal((s) => s.settings?.feeCentsPerContract != null);
  const fair = useTerminal((s) => s.series.fair);
  const replay = useTerminal((s) => s.replay);
  const [fetched, setFetched] = React.useState<MarketWindow[]>([]);
  const [posPage, setPosPage] = React.useState<{ rows: Position[]; limit: number } | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [nonce, setNonce] = React.useState(0);

  // older windows than the snapshot's `recent` come from GET /api/windows, their positions from
  // GET /api/positions (refreshed on rollover; retried on failure)
  const currentId = replay.active ? null : current?.id ?? null;
  React.useEffect(() => {
    if (scope === "window") return;
    let alive = true;
    void Promise.allSettled([api.windows(FETCH_LIMIT[scope]), api.positions({ status: "all", limit: POSITIONS_LIMIT })]).then(([ws, ps]) => {
      if (!alive) return;
      if (ws.status === "fulfilled") setFetched(ws.value);
      setPosPage(ps.status === "fulfilled" ? { rows: ps.value, limit: POSITIONS_LIMIT } : null);
      setError(
        ws.status === "rejected"
          ? "Couldn't load older windows — showing loaded windows only."
          : ps.status === "rejected"
            ? "Couldn't load older positions — P&L is shown for recent windows only."
            : null,
      );
    });
    return () => {
      alive = false;
    };
  }, [scope, currentId, nonce]);
  React.useEffect(() => {
    if (!error) return;
    const t = setTimeout(() => setNonce((n) => n + 1), RETRY_MS);
    return () => clearTimeout(t);
  }, [error]);

  // max edge per window from the loaded fair series; kept across a replay (which swaps the series out). The
  // series is mutated in place by ingest, so it is re-read on a timer as well. `from` = earliest fair seen.
  const [edgeTick, setEdgeTick] = React.useState(0);
  React.useEffect(() => {
    const t = setInterval(() => setEdgeTick((n) => n + 1), EDGE_REFRESH_MS);
    return () => clearInterval(t);
  }, []);
  const edgeCache = React.useRef<{ map: Map<string, number>; from: number | null }>({ map: new Map(), from: null });
  const maxEdge = React.useMemo(() => {
    if (!replay.active) {
      const prev = edgeCache.current;
      const t = earliestTs(fair);
      edgeCache.current = {
        map: maxEdgeByWindow(fair, new Map(prev.map)),
        from: t == null ? prev.from : prev.from == null ? t : Math.min(prev.from, t),
      };
    }
    return edgeCache.current;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- edgeTick re-reads the in-place series
  }, [fair, replay.active, edgeTick]);

  const allPositions = React.useMemo(() => mergeById(Object.values(positions), posPage?.rows ?? []), [positions, posPage]);
  const pnl = React.useMemo(() => pnlByWindow(allPositions, mode, fills, feeKnown), [allPositions, mode, fills, feeKnown]);
  const pnlFrom = React.useMemo(
    () => (scope === "window" ? null : positionsCoverFrom(posPage, Object.values(positions), liveNow())),
    [scope, posPage, positions],
  );

  const rows = React.useMemo(() => {
    const all = mergeWindows(Object.values(windows), fetched);
    return windowsInScope(scope, scopeAnchor(current, windows, replay.active), all, liveNow()).filter((w) => w.status !== "upcoming");
  }, [scope, current, windows, fetched, replay.active]);

  const retry = () => setNonce((n) => n + 1);
  if (!rows.length)
    return (
      <Empty icon={<History size={20} />} title="No windows yet" hint={error ?? "Windows appear here as they settle."}>
        {error && (
          <Button size="sm" variant="outline" onClick={retry}>
            Retry
          </Button>
        )}
      </Empty>
    );

  const now = liveNow();
  const modeLabel = mode === "live" ? "Live" : "Paper";
  return (
    <div className="min-w-0 overflow-x-auto">
      {error && (
        <div role="alert" className="flex items-center gap-2 border-b border-border bg-ptb-tint/40 px-3 py-1.5 text-[12px] text-ptb">
          <AlertTriangle size={13} aria-hidden className="shrink-0" />
          <span>{error}</span>
          <button type="button" onClick={retry} className="touch-target ml-auto font-semibold text-text-2 hover:text-text">
            Retry
          </button>
        </div>
      )}
      <table className="w-full min-w-[720px] border-separate border-spacing-0 text-left">
        <caption className="sr-only">Window history, newest first. Use a row&apos;s Replay button to replay that window.</caption>
        <thead>
          <tr>
            <Th>Window (ET)</Th>
            <Th align="right">Price to beat</Th>
            <Th align="right">Settle</Th>
            <Th align="right">Δ</Th>
            <Th align="center">Result</Th>
            <Th align="right" title={`${modeLabel} positions only (the current trading mode); net of known fees`}>
              My P&amp;L · {modeLabel}
            </Th>
            <Th align="right" title="Largest fair − market gap recorded in the window">
              Max edge
            </Th>
            <Th>
              <span className="sr-only">Replay</span>
            </Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((w) => {
            const live = w.status === "live";
            const d = w.settlePrice != null && w.priceToBeat != null ? w.settlePrice - w.priceToBeat : null;
            const myPnl = pnl.get(w.id);
            const pnlNa = pnlFrom != null && w.startTs < pnlFrom;
            const edge = maxEdge.map.get(w.id);
            const edgeNa = edge == null && !live && (maxEdge.from == null || w.endTs <= maxEdge.from);
            const replaying = replay.active && replay.windowId === w.id;
            const label = etRange(w.startTs, w.endTs);
            return (
              <tr
                key={w.id}
                onClick={(e) => {
                  // the row is a mouse shortcut for its Replay button: not for the live window, not while text is
                  // being selected, not for clicks on the button itself
                  if (live || replaying || (e.target as HTMLElement).closest("button")) return;
                  if (typeof window !== "undefined" && (window.getSelection()?.toString() ?? "") !== "") return;
                  void startReplay(w.id);
                }}
                className={cn(!live && "cursor-pointer", "hover:bg-surface-2/60", replaying && "bg-ptb-tint/60 hover:bg-ptb-tint")}
                aria-current={replaying ? "true" : undefined}
              >
                <Td num className={cn("text-text", replaying && "shadow-[inset_2px_0_0_#F5B942]")}>
                  {etDayPrefix(w.startTs, now)}
                  {label}
                </Td>
                <Td num align="right" className="text-ptb">
                  {w.priceToBeat != null ? `$${btc(w.priceToBeat)}` : "—"}
                </Td>
                <Td num align="right" className="text-text-2">
                  {w.settlePrice != null ? `$${btc(w.settlePrice)}` : "—"}
                </Td>
                <Td num align="right" className={d == null ? "text-muted" : d >= 0 ? "text-up" : "text-down"}>
                  {d == null ? "—" : `${d < 0 ? "−" : "+"}${btc(Math.abs(d))}`}
                </Td>
                <Td align="center">
                  {live ? (
                    <span className="num rounded bg-ptb-tint px-1.5 py-0.5 text-[11px] font-bold text-ptb">LIVE</span>
                  ) : w.result ? (
                    <ResultTile result={w.result} />
                  ) : (
                    <span className="text-muted" aria-label="Not settled yet">
                      —
                    </span>
                  )}
                </Td>
                <Td
                  num
                  align="right"
                  className={pnlNa ? "text-muted" : signClass(myPnl?.usd)}
                  title={pnlNa ? NA_PNL : myPnl?.feeUnknown ? "Some fees are unknown ([FEE], counted as $0): P&L may be overstated" : undefined}
                >
                  {pnlNa ? "n/a" : myPnl == null ? "—" : signedUsd(myPnl.usd)}
                  {!pnlNa && myPnl?.feeUnknown && <span className="ml-1 text-[11px] text-ptb">[FEE]</span>}
                </Td>
                <Td
                  num
                  align="right"
                  className={edge == null ? "text-muted" : edge >= 0 ? "text-odds-text" : "text-neg-edge"}
                  title={edgeNa ? NA_EDGE : edge != null && maxEdge.from != null && w.startTs < maxEdge.from ? "Partial: the loaded history starts mid-window" : undefined}
                >
                  {edge != null ? signedCents(edge, 1) : edgeNa ? "n/a" : "—"}
                </Td>
                <Td align="right" className="py-1">
                  <button
                    type="button"
                    onClick={() => void startReplay(w.id)}
                    aria-label={`Replay ${label}`}
                    className={cn(
                      "touch-target inline-flex h-6 items-center gap-1 rounded-ctl border px-2 text-[11px] font-semibold",
                      replaying ? "border-ptb/60 text-ptb" : "border-border-strong text-muted hover:text-text",
                    )}
                  >
                    <Play size={11} aria-hidden />
                    {replaying ? "Replaying" : "Replay"}
                  </button>
                </Td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
