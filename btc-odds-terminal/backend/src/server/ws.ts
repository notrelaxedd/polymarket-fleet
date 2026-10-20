import type { IncomingMessage, Server } from "node:http";
import type { Duplex } from "node:stream";
import { WebSocket, WebSocketServer } from "ws";
import type { ServerMsg } from "../../../shared/protocol.ts";
import type { EngineLike } from "./types.ts";

export interface WsHubOptions {
  path?: string;
  /** allowed Origin headers; "*" = any */
  allowedOrigins: string[] | "*";
  feedsIntervalMs: number;
  heartbeatMs: number;
  maxBufferedBytes: number;
  logger?: { warn: (...a: any[]) => void; debug: (...a: any[]) => void };
}

interface Client extends WebSocket {
  isAlive?: boolean;
  dropped?: number;
  /** a high-rate message was dropped: send a fresh snapshot once the socket drains */
  needsResync?: boolean;
}

/**
 * High-rate, self-superseding message types: safe to drop for a slow consumer (the client
 * gets a fresh snapshot once it drains). Everything else (snapshot, rollover, window,
 * settings) is structural and always delivered.
 */
const DROPPABLE = new Set<ServerMsg["type"]>(["btc", "odds", "fair", "feeds"]);
/** A client whose buffer grows past this many × maxBufferedBytes is terminated (it reconnects and resyncs). */
const HARD_LIMIT_FACTOR = 8;

/**
 * WebSocket fan-out on /ws (noServer mode, attached to the HTTP server's 'upgrade').
 * New clients get a `snapshot`, then every engine "msg" plus `feeds` each second.
 */
export class WsHub {
  readonly wss: WebSocketServer;
  private readonly path: string;
  private feedsTimer: NodeJS.Timeout | null = null;
  private heartbeatTimer: NodeJS.Timeout | null = null;
  private attached: Server | null = null;

  constructor(
    private readonly engine: EngineLike,
    private readonly opts: WsHubOptions,
  ) {
    this.path = opts.path ?? "/ws";
    this.wss = new WebSocketServer({ noServer: true, maxPayload: 64 * 1024, perMessageDeflate: false });
    this.wss.on("connection", (ws: Client) => this.onConnection(ws));
  }

  attach(server: Server): void {
    if (this.attached) return;
    this.attached = server;
    server.on("upgrade", this.onUpgrade);
    this.engine.on("msg", this.onEngineMsg);
    this.feedsTimer = setInterval(() => {
      if (this.wss.clients.size === 0) return;
      try {
        this.broadcast({ type: "feeds", data: this.engine.feeds() });
      } catch (err) {
        this.opts.logger?.warn({ err }, "feeds broadcast failed");
      }
    }, this.opts.feedsIntervalMs);
    this.heartbeatTimer = setInterval(() => this.heartbeat(), this.opts.heartbeatMs);
  }

  close(): void {
    if (this.feedsTimer) clearInterval(this.feedsTimer);
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    this.feedsTimer = this.heartbeatTimer = null;
    this.engine.off("msg", this.onEngineMsg);
    this.attached?.off("upgrade", this.onUpgrade);
    this.attached = null;
    for (const c of this.wss.clients) c.terminate();
    this.wss.close();
  }

  clientCount(): number {
    return this.wss.clients.size;
  }

  broadcast(msg: ServerMsg): void {
    if (this.wss.clients.size === 0) return;
    const data = JSON.stringify(msg);
    for (const c of this.wss.clients as Set<Client>) this.send(c, data, msg.type);
  }

  private send(c: Client, data: string, type: ServerMsg["type"]): void {
    if (c.readyState !== WebSocket.OPEN) return;
    const max = this.opts.maxBufferedBytes;
    if (c.bufferedAmount > max * HARD_LIMIT_FACTOR) {
      this.opts.logger?.warn({ buffered: c.bufferedAmount }, "ws client hopelessly behind, terminating");
      c.terminate();
      return;
    }
    if (c.bufferedAmount > max) {
      if (DROPPABLE.has(type)) {
        // slow consumer: drop high-rate updates rather than buffer without bound, resync later
        c.dropped = (c.dropped ?? 0) + 1;
        c.needsResync = true;
        if (c.dropped === 1 || c.dropped % 500 === 0) {
          this.opts.logger?.warn({ dropped: c.dropped, buffered: c.bufferedAmount }, "ws client backpressure, dropping");
        }
        return;
      }
      // structural messages (rollover / window / settings / snapshot) are always delivered
    } else if (c.needsResync && type !== "snapshot") {
      // drained after dropping updates: bring the client back to the current state first
      c.needsResync = false;
      if (!this.sendSnapshot(c)) return;
    }
    c.send(data);
  }

  /** Send a fresh snapshot; on failure close the client (it reconnects and gets one). */
  private sendSnapshot(c: Client): boolean {
    let data: string;
    try {
      data = JSON.stringify({ type: "snapshot", data: this.engine.snapshot() } satisfies ServerMsg);
    } catch (err) {
      this.opts.logger?.warn({ err }, "snapshot failed");
      c.close(1011, "snapshot failed");
      return false;
    }
    c.needsResync = false;
    c.send(data);
    return true;
  }

  private readonly onEngineMsg = (msg: ServerMsg): void => {
    this.broadcast(msg);
  };

  private readonly onUpgrade = (req: IncomingMessage, socket: Duplex, head: Buffer): void => {
    let pathname: string;
    try {
      pathname = new URL(req.url ?? "/", "http://localhost").pathname;
    } catch {
      pathname = "";
    }
    if (pathname !== this.path) {
      socket.write("HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n");
      socket.destroy();
      return;
    }
    const origin = req.headers.origin;
    if (this.opts.allowedOrigins !== "*" && origin && !this.opts.allowedOrigins.includes(origin)) {
      socket.write("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
      socket.destroy();
      return;
    }
    this.wss.handleUpgrade(req, socket, head, (ws) => this.wss.emit("connection", ws, req));
  };

  private onConnection(ws: Client): void {
    ws.isAlive = true;
    ws.on("pong", () => (ws.isAlive = true));
    ws.on("message", () => {
      // only ClientMsg is {type:"ping"}; any traffic counts as liveness
      ws.isAlive = true;
    });
    ws.on("error", (err) => this.opts.logger?.debug({ err }, "ws client error"));
    if (ws.readyState === WebSocket.OPEN) this.sendSnapshot(ws);
  }

  private heartbeat(): void {
    for (const c of this.wss.clients as Set<Client>) {
      if (c.isAlive === false) {
        c.terminate();
        continue;
      }
      c.isAlive = false;
      try {
        c.ping();
      } catch {
        c.terminate();
      }
    }
  }
}
