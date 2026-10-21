"use client";
import * as React from "react";
import { TooltipProvider } from "@/components/ui/tooltip";
import { startBackend } from "@/lib/ws";

/** Boots the data backend (WS or mock) once on the client and wraps the tree with tooltip context. */
export function Providers({ children }: { children: React.ReactNode }) {
  React.useEffect(() => startBackend(), []);
  return <TooltipProvider>{children}</TooltipProvider>;
}
