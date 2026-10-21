"use client";
import { Activity } from "lucide-react";
import { useTerminal } from "@/lib/store";
import { useEdge, useFair, useOdds } from "@/lib/hooks";
import { BRTI_PROXY_NOTE, btc, quote, signedCents } from "@/lib/format";
import { cn } from "@/lib/utils";
import { InfoTip } from "@/components/ui/info-tip";
import { StatBlock } from "./StatBlock";
import { MarketSelector } from "./MarketSelector";
import { Countdown } from "./Countdown";
import { FeedDots } from "./FeedDots";
import { LastResults } from "./LastResults";
import { ModeBadge, SettingsMenu } from "./SettingsMenu";

function Pair({ a, b }: { a: number | null | undefined; b: number | null | undefined }) {
  return (
    <>
      {quote(a)} <span className="font-normal text-muted">/</span> {quote(b)}
      <span className="font-normal text-muted">¢</span>
    </>
  );
}

export function Header() {
  const ptb = useTerminal((s) => s.current?.priceToBeat ?? null);
  const price = useTerminal((s) => s.lastTick?.price ?? null);
  const odds = useOdds();
  const fair = useFair();
  const edge = useEdge();
  const delta = price != null && ptb != null ? price - ptb : null;
  const edgeNeg = edge != null && edge < 0;

  return (
    <header className="border-b border-border bg-surface">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2.5 px-3 py-2.5">
        <div className="flex items-center gap-2.5">
          <div
            aria-hidden
            className="flex h-8 w-8 items-center justify-center rounded-[9px] border border-odds/40 bg-odds-tint text-odds"
          >
            <Activity size={17} strokeWidth={2.4} />
          </div>
          <h1 className="whitespace-nowrap text-[14px] font-bold tracking-tight text-text">BTC Odds Terminal</h1>
        </div>

        <MarketSelector />
        <Countdown />

        <div className="flex flex-wrap items-center gap-x-3.5 gap-y-2">
          <StatBlock label="Price to beat" className="text-ptb">
            {ptb != null ? `$${btc(ptb)}` : "—"}
          </StatBlock>
          <StatBlock
            label={
              <span className="inline-flex items-center gap-1">
                BTC · BRTI proxy <InfoTip label={BRTI_PROXY_NOTE} />
              </span>
            }
          >
            <span className="text-text">{price != null ? `$${btc(price)}` : "—"}</span>
            {delta != null && (
              <span className={cn("text-[12px]", delta >= 0 ? "text-up" : "text-down")}>
                {delta < 0 ? "−" : "+"}${btc(Math.abs(delta))}
              </span>
            )}
          </StatBlock>
          <StatBlock label="Up bid / ask" className="text-odds-text">
            <Pair a={odds?.upBid} b={odds?.upAsk} />
          </StatBlock>
          <StatBlock label="Down bid / ask" className="text-down">
            <Pair a={odds?.downBid} b={odds?.downAsk} />
          </StatBlock>
          <StatBlock label="Fair · Edge">
            <span className="text-fair">{fair ? `${fair.fair.toFixed(1)}¢` : "—"}</span>
            <span className="text-muted">·</span>
            <span className={edgeNeg ? "text-neg-edge" : "text-odds-text"}>{edge != null ? signedCents(edge, 1) : "—"}</span>
          </StatBlock>
        </div>

        <div className="ml-auto flex flex-wrap items-center justify-end gap-x-3 gap-y-2">
          <LastResults />
          <FeedDots />
          <ModeBadge />
          <SettingsMenu />
        </div>
      </div>
    </header>
  );
}
