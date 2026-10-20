"use client";
// Up book depth. OWNER: trading-ui. 4 asks (red, worst on top) over the "88¢ mid" divider over 4 bids (green),
// depth bars behind the sizes, spread in the header. Falls back to top of book (OddsSnap) until depth arrives.
import type { BookLevel } from "@shared/protocol";
import { useOdds } from "@/lib/hooks";
import { useTerminal } from "@/lib/store";
import { quote } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Card } from "./Card";

const LEVELS = 4;
const sizeFmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

function Row({ level, max, tone }: { level: BookLevel | null; max: number; tone: "ask" | "bid" }) {
  const pct = level && max > 0 ? Math.max(2, (level.size / max) * 100) : 0;
  return (
    <div role="row" className="relative flex h-[22px] items-center justify-between px-2 text-[12px]">
      {level && (
        <span
          aria-hidden
          className={cn("absolute inset-y-[2px] right-0 rounded-[3px]", tone === "ask" ? "bg-down/[0.16]" : "bg-up/[0.16]")}
          style={{ width: `${pct}%` }}
        />
      )}
      <span role="cell" className={cn("num relative font-medium", level ? (tone === "ask" ? "text-down" : "text-up") : "text-label")}>
        {level ? `${quote(level.price)}¢` : "—"}
      </span>
      <span role="cell" className="num relative text-text-2">
        {level && level.size > 0 ? sizeFmt.format(level.size) : ""}
      </span>
    </div>
  );
}

export function UpBook() {
  const odds = useOdds();
  const current = useTerminal((s) => s.current);
  const raw = useTerminal((s) => s.book);
  const book = raw && current && raw.windowId === current.id ? raw : null;

  // depth when we have it, else the best bid/ask from the quote (size unknown -> no bar)
  const asks: (BookLevel | null)[] = book
    ? book.asks.slice(0, LEVELS)
    : odds?.upAsk != null
      ? [{ price: odds.upAsk, size: 0 }]
      : [];
  const bids: (BookLevel | null)[] = book
    ? book.bids.slice(0, LEVELS)
    : odds?.upBid != null
      ? [{ price: odds.upBid, size: 0 }]
      : [];
  const bestAsk = asks[0]?.price ?? null;
  const bestBid = bids[0]?.price ?? null;
  const mid = bestAsk != null && bestBid != null ? (bestAsk + bestBid) / 2 : odds?.upMid ?? null;
  const spread = bestAsk != null && bestBid != null ? bestAsk - bestBid : null;
  const max = Math.max(0, ...[...asks, ...bids].map((l) => l?.size ?? 0));
  while (asks.length < LEVELS) asks.push(null);
  while (bids.length < LEVELS) bids.push(null);
  const closed = book?.state && book.state !== "MARKET_STATE_OPEN" ? book.state.replace("MARKET_STATE_", "").toLowerCase() : null;

  return (
    <Card
      title="Up book"
      right={
        <span className="num text-[11px] text-muted">
          {closed && <span className="mr-2 uppercase text-ptb">{closed}</span>}
          {spread != null ? `spread ${quote(spread)}¢` : "—"}
        </span>
      }
    >
      {bestAsk == null && bestBid == null ? (
        <div className="px-2 py-6 text-center text-[12px] text-muted">Waiting for the book for this window…</div>
      ) : (
        <div role="table" aria-label="Up order book" className="-mx-1">
          <div role="row" className="flex justify-between px-2 pb-1">
            <span role="columnheader" className="label">
              Price
            </span>
            <span role="columnheader" className="label">
              Size
            </span>
          </div>
          {/* worst ask on top so the best ask sits next to the mid */}
          {[...asks].reverse().map((l, i) => (
            <Row key={`a${i}`} level={l} max={max} tone="ask" />
          ))}
          <div role="row" className="my-1 flex items-center gap-2 px-2">
            <span aria-hidden className="h-px flex-1 bg-border-strong" />
            <span role="cell" className="num text-[12px] font-semibold text-odds-text">
              {mid != null ? `${quote(mid)}¢` : "—"} <span className="font-normal text-muted">mid</span>
            </span>
            <span aria-hidden className="h-px flex-1 bg-border-strong" />
          </div>
          {bids.map((l, i) => (
            <Row key={`b${i}`} level={l} max={max} tone="bid" />
          ))}
          {!book && <p className="px-2 pt-1.5 text-[11px] text-muted">Top of book only: depth not received yet.</p>}
        </div>
      )}
    </Card>
  );
}
