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
}

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
    for (const c of this.wss.clients as Set<Client>) this.send(c, data);
  }

  private send(c: Client, data: string): void {
    if (c.readyState !== WebSocket.OPEN) return;
    if (c.bufferedAmount > this.opts.maxBufferedBytes) {
      // slow consumer: drop rather than buffer without bound
      c.dropped = (c.dropped ?? 0) + 1;
      if (c.dropped === 1 || c.dropped % 500 === 0) {
        this.opts.logger?.warn({ dropped: c.dropped, buffered: c.bufferedAmount }, "ws client backpressure, dropping");
      }
      return;
    }
    c.send(data);
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
    try {
      this.send(ws, JSON.stringify({ type: "snapshot", data: this.engine.snapshot() } satisfies ServerMsg));
    } catch (err) {
      this.opts.logger?.warn({ err }, "snapshot failed");
      ws.close(1011, "snapshot failed");
    }
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
