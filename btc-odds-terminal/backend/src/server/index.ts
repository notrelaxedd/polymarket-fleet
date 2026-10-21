import cors from "@fastify/cors";
import Fastify, { LogController, type FastifyBaseLogger, type FastifyInstance } from "fastify";
import type { HistoryQuery } from "../../../shared/protocol.ts";
import { ApiError } from "./errors.ts";
import type { EngineLike, HistoryStore, ServerConfig, ServerOptions } from "./types.ts";
import { WsHub } from "./ws.ts";

export { ApiError, badRequest, notFound, zodError } from "./errors.ts";

export type { EngineLike, HistoryStore, ServerConfig, ServerOptions } from "./types.ts";
export { WsHub } from "./ws.ts";

declare module "fastify" {
  interface FastifyInstance {
    wsHub: WsHub;
  }
}

const HOUR_MS = 3_600_000;
export const DEFAULT_RANGE_MS = 3 * HOUR_MS;
export const MAX_RANGE_MS = 24 * HOUR_MS;

class HttpError extends Error {
  constructor(
    readonly statusCode: number,
    message: string,
  ) {
    super(message);
  }
}

function parseMs(raw: unknown, name: string): number | undefined {
  if (raw === undefined || raw === null || raw === "") return undefined;
  const s = Array.isArray(raw) ? raw[raw.length - 1] : raw;
  const n = typeof s === "number" ? s : Number(String(s).trim());
  if (!Number.isFinite(n) || n < 0 || n > 8.64e15) throw new HttpError(400, `${name} must be epoch milliseconds`);
  return Math.floor(n);
}

/** Validate ?from&to: defaults to the last 3h, ranges longer than 24h are capped (keeping `to`). */
export function parseRange(query: unknown, now = Date.now()): HistoryQuery {
  const q = (query ?? {}) as Record<string, unknown>;
  const fromQ = parseMs(q.from, "from");
  const toQ = parseMs(q.to, "to");
  let to = toQ ?? (fromQ !== undefined ? Math.min(now, fromQ + DEFAULT_RANGE_MS) : now);
  let from = fromQ ?? to - DEFAULT_RANGE_MS;
  if (from > to) throw new HttpError(400, "from must be <= to");
  if (to - from > MAX_RANGE_MS) from = to - MAX_RANGE_MS;
  if (from < 0) from = 0;
  return { from, to };
}

function parseLimit(raw: unknown, def: number, max: number): number {
  if (raw === undefined || raw === "") return def;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 1) throw new HttpError(400, "limit must be a positive integer");
  return Math.min(n, max);
}

/**
 * Build the HTTP + WebSocket server. Pure factory: no listening, so tests can
 * use app.inject() (or listen on port 0 for the WebSocket) with fake engines/stores.
 */
export function buildServer(
  engine: EngineLike,
  db: HistoryStore,
  config: ServerConfig,
  opts: ServerOptions = {},
): FastifyInstance {
  const app = Fastify({
    ...(opts.logger ? { loggerInstance: opts.logger as FastifyBaseLogger } : { logger: false }),
    logController: new LogController({ disableRequestLogging: true }),
    bodyLimit: 64 * 1024,
  });

  app.register(cors, {
    origin: config.corsOrigin === "*" ? "*" : config.corsOrigin,
    methods: ["GET", "PUT", "POST", "DELETE", "OPTIONS"],
  });

  app.setErrorHandler((err: Error & { statusCode?: number; validation?: unknown }, req, reply) => {
    const status = err.statusCode && err.statusCode >= 400 && err.statusCode < 600 ? err.statusCode : 500;
    if (status >= 500 && !(err instanceof ApiError)) req.log.error({ err, url: req.url }, "request failed");
    if (err instanceof ApiError) {
      reply.code(status).send({ error: err.message, code: err.code });
      return;
    }
    reply.code(status).send({ error: status >= 500 ? "internal error" : err.message });
  });

  // ---- REST ----
  app.get("/api/health", async () => ({ ok: true as const, serverTime: Date.now() }));

  app.get("/api/state", async () => engine.snapshot());

  app.get("/api/history/bars", async (req) => {
    const { from, to } = parseRange(req.query);
    return db.bars(from, to);
  });

  app.get("/api/history/odds", async (req) => {
    const { from, to } = parseRange(req.query);
    return db.odds(from, to);
  });

  app.get("/api/history/fair", async (req) => {
    const { from, to } = parseRange(req.query);
    return db.fair(from, to);
  });

  app.get("/api/windows", async (req) => {
    const limit = parseLimit((req.query as Record<string, unknown> | undefined)?.limit, 50, 1000);
    return db.listWindows(limit);
  });

  app.get("/api/settings", async () => engine.getSettings());

  app.put("/api/settings", async (req) => {
    const body = req.body;
    if (body === null || typeof body !== "object" || Array.isArray(body)) {
      throw new HttpError(400, "body must be a JSON object (partial Settings)");
    }
    return engine.updateSettings(body);
  });

  // ---- steps 3-6: backend modules (trading, risk, live, alerts, drawings, analysis, replay) ----
  opts.register?.(app);

  // ---- WebSocket /ws ----
  const hub = new WsHub(engine, {
    path: "/ws",
    allowedOrigins: config.corsOrigin,
    feedsIntervalMs: opts.feedsIntervalMs ?? 1000,
    heartbeatMs: opts.heartbeatMs ?? 15_000,
    maxBufferedBytes: opts.maxBufferedBytes ?? 1024 * 1024,
    logger: app.log,
  });
  hub.attach(app.server);
  app.decorate("wsHub", hub);
  // terminate sockets before the HTTP server waits for connections to drain
  app.addHook("preClose", async () => hub.close());

  return app;
}
