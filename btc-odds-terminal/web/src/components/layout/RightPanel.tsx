"use client";
import { LiveConfirmDialog } from "@/components/panels/LiveConfirmDialog";
import { OrderTicket } from "@/components/panels/OrderTicket";
import { Positions } from "@/components/panels/Positions";
import { Risk } from "@/components/panels/Risk";
import { UpBook } from "@/components/panels/UpBook";

/** Right panel (owned by trading-ui): order ticket, Up book, positions, risk. */
export function RightPanel() {
  return (
    <aside aria-label="Trading panel" className="flex min-h-0 flex-col gap-3 overflow-y-auto border-border bg-bg p-3 lg2:border-l">
      <OrderTicket />
      <UpBook />
      <Positions />
      <Risk />
      <LiveConfirmDialog />
      <p className="px-1 pb-1 text-center text-[11px] leading-4 text-muted">
        Settlement uses CF Benchmarks BRTI. The BRTI proxy shown here can differ when price is near the strike.
      </p>
    </aside>
  );
}
