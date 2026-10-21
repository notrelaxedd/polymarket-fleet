"use client";
// Empty-state block shared by the bottom tabs. OWNER: tabs.
import * as React from "react";

export function Empty({ icon, title, hint, children }: { icon: React.ReactNode; title: string; hint: string; children?: React.ReactNode }) {
  return (
    <div className="flex h-full min-h-[120px] flex-col items-center justify-center gap-1.5 px-4 py-6 text-center">
      <div className="text-muted">{icon}</div>
      <div className="text-[13px] font-medium text-text-2">{title}</div>
      <div className="text-[12px] text-muted">{hint}</div>
      {children}
    </div>
  );
}

