// Builds every mock feature module with one MockCtx and registers their routes. OWNER: scaffold.
import { createMockAlerts } from "./alerts";
import { createMockAnalysis } from "./analysis";
import { createMockDrawings } from "./drawings";
import { createMockLive } from "./live";
import { createMockReplay } from "./replay";
import { createMockRisk } from "./risk";
import { setMockRoutes } from "./router";
import { createMockTrading } from "./trading";
import type { MockCtx, MockModule, MockModules } from "./types";

export type MockCoreCtx = Omit<MockCtx, "modules">;

export function createMockModules(core: MockCoreCtx): { modules: MockModules; list: MockModule[] } {
  const ctx = core as MockCtx;
  ctx.modules = {} as MockModules;
  const modules: MockModules = {
    trading: createMockTrading(ctx),
    risk: createMockRisk(ctx),
    live: createMockLive(ctx),
    alerts: createMockAlerts(ctx),
    drawings: createMockDrawings(ctx),
    analysis: createMockAnalysis(ctx),
    replay: createMockReplay(ctx),
  };
  Object.assign(ctx.modules, modules);
  const coreRoutes: MockModule = { name: "core", routes: [{ method: "GET", path: "/book", handler: () => ctx.book() }] };
  const list: MockModule[] = [coreRoutes, ...Object.values(modules)];
  setMockRoutes(list);
  return { modules, list };
}
