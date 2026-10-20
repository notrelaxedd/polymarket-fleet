"use client";
import * as React from "react";
import { Segmented } from "@/components/ui/segmented";
import { Tip } from "@/components/ui/tooltip";
import { useTerminal } from "@/lib/store";
import { quoteCents, signedCents, spreadCents, usd } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useEdge, useOdds } from "@/lib/hooks";

function Card({ title, right, children, className }: { title: string; right?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("rounded-card border border-border bg-surface", className)}>
      <header className="flex items-center justify-between gap-2 px-3 pt-2.5">
        <h2 className="label">{title}</h2>
        {right}
      </header>
      <div className="p-3 pt-2">{children}</div>
    </section>
  );
}

function SumRow({ k, v, color }: { k: string; v: React.ReactNode; color?: string }) {
  return (
    <div className="flex items-center justify-between py-[3px] text-[12px]">
      <span className="text-muted">{k}</span>
      <span className="num font-medium" style={{ color }}>
        {v}
      </span>
    </div>
  );
}

function OrderTicket() {
  const odds = useOdds();
  const mode = useTerminal((s) => s.settings?.mode ?? "paper");
  const fee = useTerminal((s) => s.settings?.feeCentsPerContract ?? null);
  const edge = useEdge();
  const [side, setSide] = React.useState<"up" | "down">("up");
  const [type, setType] = React.useState<"limit" | "market">("limit");
  const [amount, setAmount] = React.useState("25");
  const [slip, setSlip] = React.useState("2");
  const ask = side === "up" ? odds?.upAsk : odds?.downAsk;
  const amt = Number(amount) || 0;
  const contracts = ask ? amt / (ask / 100) : null;
  const modeLabel = mode === "live" ? "Live" : "Paper";

  const sideBtn = (s: "up" | "down") => {
    const on = side === s;
    const a = s === "up" ? odds?.upAsk : odds?.downAsk;
    const tone = s === "up" ? { c: "text-up", b: "border-up", t: "bg-up-tint" } : { c: "text-down", b: "border-down", t: "bg-down-tint" };
    return (
      <button
        type="button"
        aria-pressed={on}
        onClick={() => setSide(s)}
        className={cn(
          "touch-target flex h-11 flex-1 flex-col items-center justify-center rounded-ctl border text-[12px] font-semibold transition-colors",
          on ? `${tone.t} ${tone.b} ${tone.c}` : "border-border-strong bg-inset text-muted hover:text-text",
        )}
      >
        <span>Buy {s === "up" ? "Up" : "Down"}</span>
        <span className="num text-[14px]">{quoteCents(a)}</span>
      </button>
    );
  };

  const fieldCls =
    "num h-9 w-full rounded-ctl border border-border-strong bg-inset px-2.5 text-right text-[13px] text-text outline-none focus:border-odds touch-target";

  return (
    <Card title="Order ticket" right={<span className="num text-[11px] font-semibold uppercase text-ptb">{modeLabel}</span>}>
      <div className="flex gap-2">
        {sideBtn("up")}
        {sideBtn("down")}
      </div>
      <div className="mt-2.5">
        <Segmented<"limit" | "market">
          ariaLabel="Order type"
          className="w-full [&>button]:flex-1"
          value={type}
          onChange={setType}
          options={[
            { value: "limit", label: "Limit" },
            { value: "market", label: "Marketable" },
          ]}
        />
      </div>

      <label className="label mt-3 block" htmlFor="amount">
        Amount (USD)
      </label>
      <input id="amount" inputMode="decimal" className={cn(fieldCls, "mt-1")} value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^0-9.]/g, ""))} />
      <div className="mt-1.5 flex gap-1.5">
        {[10, 25, 50, 100].map((v) => (
          <button
            key={v}
            type="button"
            onClick={() => setAmount(String(v))}
            className="num touch-target h-7 flex-1 rounded-ctl border border-border-strong bg-surface-2 text-[12px] text-text-2 hover:text-text"
          >
            ${v}
          </button>
        ))}
      </div>

      <label className="label mt-3 block" htmlFor="slip">
        Max slippage (¢)
      </label>
      <input id="slip" inputMode="decimal" className={cn(fieldCls, "mt-1")} value={slip} onChange={(e) => setSlip(e.target.value.replace(/[^0-9.]/g, ""))} />

      <div className="mt-3 border-t border-border pt-2.5">
        <div className="label pb-1">Bracket · OCO</div>
        {[
          ["Take profit (¢)", "96"],
          ["Stop loss (¢)", "70"],
          ["Stop if BTC <", "price"],
          ["Time exit at T− (s)", "30"],
        ].map(([k, v]) => (
          <div key={k} className="flex items-center gap-2 py-1 text-[12px] text-muted opacity-70">
            <input type="checkbox" disabled aria-label={k} className="h-3.5 w-3.5 accent-[#5B9CFF]" />
            <span className="flex-1">{k}</span>
            <span className="num w-16 rounded-ctl border border-border bg-inset px-2 py-1 text-right text-muted">{v}</span>
          </div>
        ))}
        <p className="pt-1 text-[11px] leading-4 text-muted">Drag the TP/SL lines on the chart to adjust after entry.</p>
      </div>

      <div className="mt-2.5 rounded-ctl border border-border bg-inset px-2.5 py-1.5">
        <SumRow k="Est. avg fill" v={quoteCents(ask)} />
        <SumRow k="Contracts" v={contracts != null ? contracts.toFixed(1) : "—"} />
        <SumRow k="Fees" v={fee == null ? "[FEE]" : usd((fee * (contracts ?? 0)) / 100)} />
        <SumRow k="Max loss" v={usd(amt)} color="#F87171" />
        <SumRow k={`Payout if ${side === "up" ? "Up" : "Down"}`} v={contracts != null ? `${usd(contracts)} (+${usd(contracts - amt)})` : "—"} color="#34D399" />
        <SumRow k="Edge vs fair" v={edge != null ? signedCents(side === "up" ? edge : -edge, 1) : "—"} color={edge != null && (side === "up" ? edge : -edge) < 0 ? "#F59E0B" : "#7FB2FF"} />
      </div>

      <Tip label="Order placement arrives in step 3" side="top">
        <span className="mt-3 block" tabIndex={0}>
          <button
            type="button"
            disabled
            className={cn(
              "touch-target h-11 w-full rounded-ctl text-[14px] font-bold disabled:opacity-60",
              side === "up" ? "bg-up text-[#06251a]" : "bg-down text-[#2b0a0a]",
            )}
          >
            Buy {side === "up" ? "Up" : "Down"} · {modeLabel}
          </button>
        </span>
      </Tip>
    </Card>
  );
}

function UpBook() {
  const odds = useOdds();
  const bid = odds?.upBid ?? null;
  const ask = odds?.upAsk ?? null;
  const mid = odds?.upMid ?? null;
  const spread = spreadCents(bid, ask);
  // Only the best bid/ask are known in step 2 (OddsSnap). Never synthesize deeper levels.
  const row = (side: "Best ask" | "Best bid", p: number | null, color: string) => (
    <div className="flex items-center justify-between px-2 py-[3px] text-[12px]">
      <span className="text-muted">{side}</span>
      <span className="num font-medium" style={{ color: p != null ? color : undefined }}>
        {quoteCents(p)}
      </span>
    </div>
  );
  return (
    <Card title="Up book" right={<span className="num text-[11px] text-muted">{spread != null ? `spread ${spread}` : "—"}</span>}>
      {odds ? (
        <div>
          {row("Best ask", ask, "#F87171")}
          <div className="my-1 flex items-center justify-between rounded-[6px] bg-inset px-2 py-1">
            <span className="text-[12px] text-muted">Mid</span>
            <span className="num text-[13px] font-semibold text-odds-text">{quoteCents(mid)}</span>
          </div>
          {row("Best bid", bid, "#34D399")}
        </div>
      ) : (
        <div className="px-2 py-3 text-center text-[12px] text-muted">Waiting for a quote for this window…</div>
      )}
      <p className="mt-1.5 text-[11px] text-muted">Top of book only. Depth and sizes arrive with the order book in step 3.</p>
    </Card>
  );
}

function Positions() {
  return (
    <Card title="Positions" right={<span className="num text-[11px] text-muted">Today <span className="font-semibold text-text-2">+$0.00</span></span>}>
      <div className="rounded-ctl border border-dashed border-border-strong px-3 py-4 text-center text-[12px] text-muted">
        No open positions.
        <br />
        Brackets and live P&amp;L appear here in step 3.
      </div>
    </Card>
  );
}

function Bar({ label, value, max, pct }: { label: string; value: string; max: string; pct: number }) {
  return (
    <div className="py-1.5">
      <div className="flex items-center justify-between text-[12px]">
        <span className="text-muted">{label}</span>
        <span className="num text-text-2">
          {value} <span className="text-muted">/ {max}</span>
        </span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-inset">
        <div className="h-full rounded-full bg-odds" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function Risk() {
  return (
    <Card title="Risk">
      <Bar label="Daily loss limit" value="$0" max="—" pct={0} />
      <Bar label="Open exposure" value="$0" max="—" pct={0} />
      <div className="flex items-center justify-between py-1.5 text-[12px]">
        <span className="text-muted">Max loss per window</span>
        <span className="num text-text-2">—</span>
      </div>
      <Tip label="Kill switch arrives in step 6" side="top">
        <span className="mt-1 block" tabIndex={0}>
          <button
            type="button"
            disabled
            className="touch-target h-10 w-full rounded-ctl border border-down text-[13px] font-semibold text-down disabled:opacity-60"
          >
            Kill switch · cancel all &amp; flatten
          </button>
        </span>
      </Tip>
    </Card>
  );
}

export function RightPanel() {
  return (
    <aside aria-label="Trading panel" className="flex min-h-0 flex-col gap-3 overflow-y-auto border-border bg-bg p-3 lg2:border-l">
      <OrderTicket />
      <UpBook />
      <Positions />
      <Risk />
      <p className="px-1 pb-1 text-center text-[11px] leading-4 text-muted">
        Settlement uses CF Benchmarks BRTI. The BRTI proxy shown here can differ when price is near the strike.
      </p>
    </aside>
  );
}
