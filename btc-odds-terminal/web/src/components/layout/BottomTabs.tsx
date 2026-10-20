"use client";
import * as React from "react";
import { Bell, ClipboardList, History } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Segmented } from "@/components/ui/segmented";
import { useTerminal } from "@/lib/store";
import { btc, etRange } from "@/lib/format";
import { cn } from "@/lib/utils";

type Sample = "this" | "last20" | "all";

function Empty({ icon, title, hint }: { icon: React.ReactNode; title: string; hint: string }) {
  return (
    <div className="flex h-full min-h-[120px] flex-col items-center justify-center gap-1.5 px-4 py-6 text-center">
      <div className="text-muted">{icon}</div>
      <div className="text-[13px] font-medium text-text-2">{title}</div>
      <div className="text-[12px] text-muted">{hint}</div>
    </div>
  );
}

function PairPlaceholder() {
  const cards = [
    { t: "BTC vs strike → Up price", d: "Scatter of Up price against BTC distance from price to beat, in σ units." },
    { t: "Lag: odds vs BTC", d: "Cross-correlation of BTC returns with later odds changes, 0–30s." },
    { t: "Sensitivity", d: "¢ of Up per $10 BTC over the window — market vs fair." },
  ];
  return (
    <div className="grid gap-3 p-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
      {cards.map((c) => (
        <div key={c.t} className="rounded-card border border-border bg-inset p-3">
          <div className="label pb-1">{c.t}</div>
          <div className="flex h-[120px] items-center justify-center rounded-ctl border border-dashed border-border-strong px-4 text-center text-[12px] text-muted">
            {c.d} <br />
            Arrives in step 5.
          </div>
        </div>
      ))}
    </div>
  );
}

function WindowHistory({ sample }: { sample: Sample }) {
  const current = useTerminal((s) => s.current);
  const recent = useTerminal((s) => s.recent);
  const windows = useTerminal((s) => s.windows);
  let rows = recent;
  if (sample === "this") rows = current ? [current] : [];
  else if (sample === "last20") rows = [...(current ? [current] : []), ...recent].slice(0, 20);
  else {
    rows = Object.values(windows).sort((a, b) => b.startTs - a.startTs);
  }
  if (!rows.length) return <Empty icon={<History size={20} />} title="No windows yet" hint="Windows appear here as they settle." />;
  return (
    <table className="w-full min-w-[460px] border-separate border-spacing-0 text-left">
      <thead>
        <tr className="sticky top-0 bg-surface">
          {["Window (ET)", "Price to beat", "Settle", "Δ", "Result"].map((h) => (
            <th key={h} className="label border-b border-border px-3 py-1.5 font-semibold">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((w) => {
          const live = w.status === "live" || w.status === "upcoming";
          const d = w.settlePrice != null && w.priceToBeat != null ? w.settlePrice - w.priceToBeat : null;
          return (
            <tr key={w.id} className="hover:bg-surface-2/60">
              <td className="num border-b border-border px-3 py-1.5 text-text">{etRange(w.startTs, w.endTs)}</td>
              <td className="num border-b border-border px-3 py-1.5 text-ptb">{w.priceToBeat != null ? `$${btc(w.priceToBeat)}` : "—"}</td>
              <td className="num border-b border-border px-3 py-1.5 text-text-2">{w.settlePrice != null ? `$${btc(w.settlePrice)}` : "—"}</td>
              <td className={cn("num border-b border-border px-3 py-1.5", d == null ? "text-muted" : d >= 0 ? "text-up" : "text-down")}>
                {d == null ? "—" : `${d < 0 ? "−" : "+"}${btc(Math.abs(d))}`}
              </td>
              <td className="border-b border-border px-3 py-1.5">
                {live ? (
                  <span className="num rounded bg-ptb-tint px-1.5 py-0.5 text-[11px] font-bold text-ptb">{w.status === "live" ? "LIVE" : "NEXT"}</span>
                ) : w.result ? (
                  <span
                    className={cn(
                      "num inline-flex h-[18px] w-[18px] items-center justify-center rounded-[4px] text-[11px] font-bold",
                      w.result === "up" ? "bg-up-tint text-up" : "bg-down-tint text-down",
                    )}
                  >
                    {w.result === "up" ? "U" : "D"}
                  </span>
                ) : (
                  <span className="text-muted">—</span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function BottomTabs() {
  const [sample, setSample] = React.useState<Sample>("last20");
  const alerts = 0;
  return (
    <Tabs defaultValue="pair" className="flex min-h-0 flex-1 flex-col bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-x-3 border-b border-border px-2">
        <TabsList aria-label="Analysis tabs">
          <TabsTrigger value="pair">Pair relationship</TabsTrigger>
          <TabsTrigger value="orders">Orders &amp; fills</TabsTrigger>
          <TabsTrigger value="history">Window history</TabsTrigger>
          <TabsTrigger value="alerts">
            Alerts
            <span className="num rounded-full bg-surface-2 px-1.5 text-[11px] font-semibold text-text-2">{alerts}</span>
          </TabsTrigger>
        </TabsList>
        <Segmented<Sample>
          ariaLabel="Sample filter"
          size="sm"
          value={sample}
          onChange={setSample}
          options={[
            { value: "this", label: "This window" },
            { value: "last20", label: "Last 20" },
            { value: "all", label: "All" },
          ]}
          className="my-1"
        />
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        <TabsContent value="pair" className="h-full">
          <PairPlaceholder />
        </TabsContent>
        <TabsContent value="orders" className="h-full">
          <Empty icon={<ClipboardList size={20} />} title="No orders yet" hint="Orders, fills, slippage and fees will list here once paper trading is on (step 3)." />
        </TabsContent>
        <TabsContent value="history" className="h-full">
          <WindowHistory sample={sample} />
        </TabsContent>
        <TabsContent value="alerts" className="h-full">
          <Empty icon={<Bell size={20} />} title="No alerts" hint="Price, odds, edge and strike-cross alerts arrive in step 6." />
        </TabsContent>
      </div>
    </Tabs>
  );
}
