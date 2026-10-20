import type {
  Bar,
  FairSnap,
  FeedStatus,
  MarketWindow,
  OddsSnap,
  ServerMsg,
  Settings,
  StateSnapshot,
} from "../../../shared/protocol.ts";

/** What the HTTP/WS layer needs from the engine (Engine implements it; tests pass fakes). */
export interface EngineLike {
  snapshot(): StateSnapshot;
  feeds(): FeedStatus[];
  getSettings(): Settings;
  /** Apply a partial update; throw an Error with `statusCode: 400` for invalid input. */
  updateSettings(patch: unknown): Settings;
  on(event: "msg", listener: (msg: ServerMsg) => void): unknown;
  off(event: "msg", listener: (msg: ServerMsg) => void): unknown;
}

/** History queries (TerminalDb implements it). */
export interface HistoryStore {
  bars(from: number, to: number): Bar[];
  odds(from: number, to: number): OddsSnap[];
  fair(from: number, to: number): FairSnap[];
  listWindows(limit: number): MarketWindow[];
}

export interface ServerConfig {
  corsOrigin: string[] | "*";
}

export interface ServerOptions {
  /** pino logger instance for Fastify, or false (default) */
  logger?: unknown;
  /** feeds broadcast interval (default 1000 ms) */
  feedsIntervalMs?: number;
  /** ws heartbeat interval (default 15000 ms) */
  heartbeatMs?: number;
  /** skip sends to clients whose socket buffer exceeds this (default 1 MB) */
  maxBufferedBytes?: number;
}
