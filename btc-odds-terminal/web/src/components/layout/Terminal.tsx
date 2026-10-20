"use client";
import dynamic from "next/dynamic";
import { Header } from "@/components/header/Header";
import { ToolsRail } from "./ToolsRail";
import { ChartToolbar } from "./ChartToolbar";
import { LegendRow } from "./LegendRow";
import { StaleBanner } from "./StaleBanner";
import { BottomTabs } from "./BottomTabs";
import { RightPanel } from "./RightPanel";

// lightweight-charts is client-only
const MainChart = dynamic(() => import("@/components/chart/MainChart"), {
  ssr: false,
  loading: () => <div className="flex h-full items-center justify-center text-muted">Loading chart…</div>,
});

export function Terminal() {
  return (
    // Desktop locks the terminal to the viewport, but never below 720px: on short laptop screens the page
    // scrolls a little instead of crushing the toolbar/legend or clipping the bottom tabs.
    <div className="flex min-h-screen flex-col lg2:h-screen lg2:min-h-[720px]">
      <Header />
      <div className="grid min-h-0 flex-1 grid-cols-1 sm:grid-cols-[52px_minmax(0,1fr)] lg2:grid-cols-[52px_minmax(0,1fr)_clamp(320px,25vw,360px)]">
        <div className="sm:row-span-2 sm:row-start-1 lg2:row-span-1 sm:bg-surface">
          <ToolsRail />
        </div>

        <main className="flex min-h-0 min-w-0 flex-col sm:col-start-2 sm:row-start-1">
          <StaleBanner />
          <ChartToolbar />
          <LegendRow />
          <div className="relative h-[420px] min-h-[320px] border-b border-border bg-bg sm:h-[520px] lg2:h-auto lg2:flex-1">
            <div className="absolute inset-0">
              <MainChart />
            </div>
          </div>
          <div className="flex h-[300px] shrink-0 flex-col lg2:h-[clamp(190px,28vh,250px)]">
            <BottomTabs />
          </div>
        </main>

        <div className="min-h-0 sm:col-start-2 sm:row-start-2 lg2:col-start-3 lg2:row-start-1 lg2:flex lg2:flex-col lg2:overflow-hidden lg2:[&>aside]:flex-1">
          <RightPanel />
        </div>
      </div>
    </div>
  );
}
