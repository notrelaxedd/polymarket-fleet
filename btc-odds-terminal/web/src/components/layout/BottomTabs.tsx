"use client";
import type { AnalysisScope } from "@shared/protocol";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Segmented } from "@/components/ui/segmented";
import { Alerts } from "@/components/tabs/Alerts";
import { NewAlertDialog } from "@/components/tabs/NewAlertDialog";
import { OrdersFills } from "@/components/tabs/OrdersFills";
import { PairRelationship } from "@/components/tabs/PairRelationship";
import { WindowHistory } from "@/components/tabs/WindowHistory";
import { useTerminal } from "@/lib/store";

/** Bottom tabs container (owned by tabs; PairRelationship content by analysis). */
export function BottomTabs() {
  const sample = useTerminal((s) => s.sampleScope);
  const setSample = useTerminal((s) => s.setSampleScope);
  const alerts = useTerminal((s) => s.alerts.length);
  const unseen = useTerminal((s) => s.unseenAlertEvents);
  return (
    <Tabs defaultValue="pair" className="flex min-h-0 flex-1 flex-col bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-x-3 border-b border-border px-2">
        <TabsList aria-label="Analysis tabs">
          <TabsTrigger value="pair">Pair relationship</TabsTrigger>
          <TabsTrigger value="orders">Orders &amp; fills</TabsTrigger>
          <TabsTrigger value="history">Window history</TabsTrigger>
          <TabsTrigger value="alerts">
            Alerts
            <span
              aria-label={unseen > 0 ? `${alerts} alerts, ${unseen} new` : `${alerts} alerts`}
              className={
                unseen > 0
                  ? "num rounded-full bg-odds-tint px-1.5 text-[11px] font-semibold text-odds-text"
                  : "num rounded-full bg-surface-2 px-1.5 text-[11px] font-semibold text-text-2"
              }
            >
              {unseen > 0 ? `${alerts} · ${unseen} new` : alerts}
            </span>
          </TabsTrigger>
        </TabsList>
        <Segmented<AnalysisScope>
          ariaLabel="Sample filter"
          size="sm"
          value={sample}
          onChange={setSample}
          options={[
            { value: "window", label: "This window" },
            { value: "last20", label: "Last 20" },
            { value: "all", label: "All" },
          ]}
          className="my-1"
        />
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        <TabsContent value="pair" className="h-full">
          <PairRelationship />
        </TabsContent>
        <TabsContent value="orders" className="h-full">
          <OrdersFills />
        </TabsContent>
        <TabsContent value="history" className="h-full">
          <WindowHistory />
        </TabsContent>
        <TabsContent value="alerts" className="h-full">
          <Alerts />
        </TabsContent>
      </div>
      <NewAlertDialog />
    </Tabs>
  );
}
