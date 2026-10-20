// Builds every steps 3-6 backend module with one shared deps object. OWNER: scaffold.
import type { FastifyInstance } from "fastify";
import type { ServerMsg, StateSnapshot } from "../../../shared/protocol.ts";
import { createAlerts } from "../alerts/index.ts";
import { createAnalysis } from "../analysis/index.ts";
import type { Config } from "../config.ts";
import type { TerminalDb } from "../db/index.ts";
import { createDrawings } from "../drawings/index.ts";
import { createLive } from "../live/index.ts";
import type { Logger } from "../log.ts";
import { createReplay } from "../replay/index.ts";
import { createRisk } from "../risk/index.ts";
import { createTrading } from "../trading/index.ts";
import type { BackendModule, EngineApi, ModuleDeps, Services } from "./types.ts";

export type * from "./types.ts";

export interface Modules {
  deps: ModuleDeps;
  services: Services;
  list: BackendModule<unknown>[];
  /** Fastify routes of every module (pass as buildServer opts.register) */
  register(app: FastifyInstance): void;
  start(): void;
  stop(): Promise<void>;
  /** merged snapshot fields (wired into engine.extendSnapshot by main.ts) */
  snapshot(): Partial<StateSnapshot>;
}

export interface ModulesInput {
  config: Config;
  db: TerminalDb;
  engine: EngineApi & { publish(msg: ServerMsg): void };
  logger: Logger;
  now?: () => number;
}

export function createModules(input: ModulesInput): Modules {
  const services = {} as Services;
  const deps: ModuleDeps = {
    config: input.config,
    db: input.db,
    engine: input.engine,
    broadcast: (msg) => input.engine.publish(msg),
    logger: input.logger,
    now: input.now ?? Date.now,
    services,
  };
  const child = (name: string): ModuleDeps => ({ ...deps, logger: input.logger.child({ component: name }) });

  const trading = createTrading(child("trading"));
  const risk = createRisk(child("risk"));
  const live = createLive(child("live"));
  const alerts = createAlerts(child("alerts"));
  const drawings = createDrawings(child("drawings"));
  const analysis = createAnalysis(child("analysis"));
  const replay = createReplay(child("replay"));
  Object.assign(services, {
    trading: trading.service,
    risk: risk.service,
    live: live.service,
    alerts: alerts.service,
    drawings: drawings.service,
    analysis: analysis.service,
    replay: replay.service,
  } satisfies Services);
  const list = [trading, risk, live, alerts, drawings, analysis, replay] as BackendModule<unknown>[];

  return {
    deps,
    services,
    list,
    register(app) {
      // the current window's Up book depth (the engine polls it every second); null when none is cached
      app.get("/api/book", async () => input.engine.currentBook() ?? null);
      for (const m of list) m.register(app);
    },
    start() {
      for (const m of list) {
        try {
          m.start?.();
        } catch (err) {
          input.logger.error({ err, module: m.name }, "module start failed");
        }
      }
    },
    async stop() {
      for (const m of [...list].reverse()) {
        try {
          await m.stop?.();
        } catch (err) {
          input.logger.error({ err, module: m.name }, "module stop failed");
        }
      }
    },
    snapshot() {
      const out: Partial<StateSnapshot> = {};
      for (const m of list) {
        if (!m.snapshot) continue;
        try {
          Object.assign(out, m.snapshot());
        } catch (err) {
          input.logger.warn({ err, module: m.name }, "module snapshot failed");
        }
      }
      return out;
    },
  };
}
