// Live trading on Polymarket US. OWNER: live-backend.
//  - LiveExecutor (executor.ts): the exchange implementation of the Executor contract that trading's paper executor
//    also implements; services.live.executor() hands it out only while settings.mode === "live" and keys are set.
//  - LiveRestClient (rest.ts): Ed25519-signed REST (orders, cancel, cancel-all, open orders, positions, balances).
//  - PrivateStream (private-ws.ts): order executions / order snapshots / position changes / balances, reconnecting.
//  - Reconcile: GET /v1/portfolio/positions (+ balances) every LIVE_RECONCILE_MS, after the socket (re)opens, after
//    position frames and after fills -> services.trading.reconcile(remote). Skipped while a submit is in flight or
//    unconfirmed or a fill just landed (the positions endpoint may lag), then retried. One read is never trusted on
//    its own: a quantity that differs from the terminal's is passed on only after two reads >= CONFIRM_MS apart show
//    the same numbers and no fill landed in that market for RECENT_FILL_MS (until then trading sees its own numbers);
//    expired positions are never passed. Unparseable rows fail the reconcile only for markets the terminal trades.
//  - Leaving live mode cancels our resting live orders (one by one) and learns their final state before the
//    executor stops, so nothing keeps working on the exchange untracked.
//
// Live guard (all required): keys in the backend .env (engine.liveAvailable), settings.mode === "live" (the settings
// toggle; the engine refuses it without keys), and a session from POST /api/live/confirm sent as liveSession on every
// live order (trading answers 409 live_confirm_required otherwise; the UI shows its confirm dialog once per browser
// session). Switching back to paper drops every confirmed session.
//
// Keys come only from config (POLYMARKET_US_KEY_ID / _SECRET), are used only to sign requests, never logged and
// never sent to the frontend (LiveStatus carries no key material).
//
// Routes: GET /api/live/status, POST /api/live/confirm, POST /api/live/reconcile. Broadcasts `live` (LiveStatus)
// on change and `notice` for live-mode events.

import { randomUUID } from "node:crypto";
import type { LiveConfirmResponse, LiveStatus, Notice, Position, Settings } from "../../../shared/protocol.ts";
import type { BackendModule, ExternalOrderUpdate, LiveService, ModuleDeps, RemotePosition } from "../modules/types.ts";
import { parseInstrument } from "../polymarket/discovery.ts";
import { ApiError } from "../server/errors.ts";
import { loadLiveConfig, type LiveConfig } from "./config.ts";
import { LiveExecutor } from "./executor.ts";
import { PrivateStream, type PrivateStreamOptions } from "./private-ws.ts";
import { LiveHttpError, LiveRestClient } from "./rest.ts";
import type { PrivateFrame, WirePosition } from "./wire.ts";

export { LiveExecutor } from "./executor.ts";
export { LiveRestClient, LiveHttpError } from "./rest.ts";
export { PrivateStream } from "./private-ws.ts";
export { loadLiveConfig, type LiveConfig } from "./config.ts";

/** A confirmed live session stays valid this long (the browser re-confirms after a reload anyway). */
export const LIVE_SESSION_TTL_MS = 12 * 3600_000;
/** wait after the last fill / position frame before reading positions (the endpoint may lag the socket) */
const SETTLE_DELAY_MS = 3500;
/** a position discrepancy must persist this long (two reads) before trading is told */
export const CONFIRM_MS = 4000;
/** no discrepancy is acted on for a market with a fill this recent (exits and entries lag on the endpoint) */
export const RECENT_FILL_MS = 15_000;
/** consecutive balance read failures before the shown balance is dropped as stale */
const BALANCE_FAILS_TO_CLEAR = 3;
const EPS = 1e-6;

export interface LiveOptions {
  /** injectable fetch for every live REST call (tests run offline) */
  fetchImpl?: typeof fetch;
  env?: NodeJS.ProcessEnv;
  /** overrides over the .env-derived LiveConfig */
  config?: Partial<LiveConfig>;
  /** private stream option overrides (backoff etc.) */
  stream?: Partial<Omit<PrivateStreamOptions, "auth" | "logger">>;
  /** false = never open the private socket (REST + polling only) */
  privateWs?: boolean;
}

export interface LiveModuleService extends LiveService {
  /** reconcile positions + balance now (resolves with the new status; deferred = trading was not reconciled this time) */
  reconcileNow(reason?: string): Promise<LiveStatus & { deferred: boolean }>;
}

export function createLive(deps: ModuleDeps, opts: LiveOptions = {}): BackendModule<LiveModuleService> {
  const cfg: LiveConfig = { ...loadLiveConfig(deps.config, opts.env), ...opts.config };
  const log = deps.logger;
  const sessions = new Map<string, number>(); // session -> expiresTs

  let active = false;
  let rest: LiveRestClient | null = null;
  let executor: LiveExecutor | null = null;
  let stream: PrivateStream | null = null;
  let reconcileTimer: NodeJS.Timeout | null = null;
  let soonTimer: NodeJS.Timeout | null = null;
  let reconciling: Promise<boolean> | null = null;
  /** instrument -> a discrepancy seen and not yet confirmed */
  const pendingDiffs = new Map<string, { remote: number; local: number; firstTs: number }>();
  let balanceFails = 0;

  const st = {
    connected: false,
    lastReconcileTs: null as number | null,
    balanceUsd: null as number | null,
    buyingPowerUsd: null as number | null,
    restError: null as string | null,
    balanceError: null as string | null,
    wsError: null as string | null,
    setupError: null as string | null,
  };

  const modeLive = () => deps.engine.getSettings().mode === "live";

  const status = (): LiveStatus => ({
    available: deps.engine.liveAvailable(),
    enabled: modeLive(),
    connected: active && st.connected,
    lastReconcileTs: st.lastReconcileTs,
    balanceUsd: st.balanceUsd,
    buyingPowerUsd: st.buyingPowerUsd,
    error: st.setupError ?? st.restError ?? st.balanceError ?? st.wsError,
  });

  let lastSent = "";
  const publish = (force = false) => {
    const s = status();
    const key = JSON.stringify(s);
    if (!force && key === lastSent) return;
    lastSent = key;
    deps.broadcast({ type: "live", data: s });
  };

  const notice = (level: Notice["level"], title: string, message: string) =>
    deps.broadcast({ type: "notice", data: { id: randomUUID(), ts: deps.now(), level, title, message } });

  // ---------------------------------------------------------------- exchange <-> trading

  const exToSide = (cents: number, side: "long" | "short") =>
    side === "short" && cfg.shortPriceQuote === "long" ? Math.round((100 - cents) * 10) / 10 : cents;

  /** exchange positions -> RemotePosition (instrument "<slug>:long|short", as MarketWindow up/downInstrument) */
  const toRemote = (positions: WirePosition[]): RemotePosition[] => {
    const out: RemotePosition[] = [];
    for (const p of positions) {
      // expired markets settle through trading's settlement path: never re-import them as open positions
      if (!p.slug || p.expired || Math.abs(p.net) < 1e-9) continue;
      const side = p.net > 0 ? "long" : "short";
      const avg = p.avgCents === null ? null : p.avgHeldSide ? p.avgCents : exToSide(p.avgCents, side);
      out.push({ instrument: `${p.slug}:${side}`, qty: Math.abs(p.net), avgCents: avg });
    }
    return out;
  };

  /** the terminal's open live positions (empty when trading cannot say) */
  const localLive = (): Position[] => {
    try {
      return deps.services.trading.positions({ status: "open" }).filter((p) => p.mode === "live" && p.status === "open");
    } catch {
      return [];
    }
  };

  /**
   * What trading may act on: instruments that agree pass through; a discrepancy only once two reads >= CONFIRM_MS
   * apart show the same remote and local quantities and the market had no fill in RECENT_FILL_MS. An unconfirmed
   * discrepancy is passed as the local numbers (no change) or left out (a remote-only position).
   */
  const confirmDiffs = (remote: RemotePosition[], ex: LiveExecutor): { out: RemotePosition[]; pending: number } => {
    const now = deps.now();
    const localBy = new Map(localLive().map((p) => [p.instrument, p]));
    const remoteBy = new Map(remote.map((r) => [r.instrument, r]));
    const out: RemotePosition[] = [];
    const all = new Set([...localBy.keys(), ...remoteBy.keys()]);
    let pending = 0;
    for (const inst of all) {
      const l = localBy.get(inst);
      const r = remoteBy.get(inst);
      const lq = l?.qty ?? 0;
      const rq = r?.qty ?? 0;
      if (Math.abs(lq - rq) <= EPS) {
        pendingDiffs.delete(inst);
        if (r) out.push(r);
        continue;
      }
      const prev = pendingDiffs.get(inst);
      let confirmed = false;
      if (prev && Math.abs(prev.remote - rq) <= EPS && Math.abs(prev.local - lq) <= EPS) {
        const recentFill = now - ex.lastFillTs(parseInstrument(inst).slug) < RECENT_FILL_MS;
        confirmed = now - prev.firstTs >= CONFIRM_MS && !recentFill;
      } else {
        pendingDiffs.set(inst, { remote: rq, local: lq, firstTs: now });
      }
      if (confirmed) {
        // keep the entry: the same numbers on later reads pass straight through (no re-confirm loop for a
        // discrepancy trading itself ignores, e.g. another market's position)
        if (r) out.push(r);
      } else {
        pending++;
        if (l) out.push({ instrument: inst, qty: l.qty, avgCents: l.avgCents });
      }
    }
    for (const k of [...pendingDiffs.keys()]) if (!all.has(k)) pendingDiffs.delete(k);
    return { out, pending };
  };

  /** slugs the terminal trades now: an unparseable row for one of these must fail the reconcile */
  const tradedSlugs = (): Set<string> => {
    const out = new Set<string>();
    for (const w of [deps.engine.currentWindow(), deps.engine.nextWindow()]) {
      if (!w) continue;
      out.add(parseInstrument(w.upInstrument).slug);
      out.add(parseInstrument(w.downInstrument).slug);
    }
    for (const p of localLive()) out.add(parseInstrument(p.instrument).slug);
    return out;
  };

  const applyExternal = (u: ExternalOrderUpdate) => {
    try {
      deps.services.trading.applyExternal(u);
    } catch (err) {
      log.error({ err: (err as Error).message, externalId: u.externalId }, "live: trading.applyExternal failed");
    }
    if (u.fill) reconcileSoon(SETTLE_DELAY_MS);
    if (u.status === "rejected" && u.error) notice("error", "Live order rejected", u.error);
  };

  const reconcileSoon = (ms: number) => {
    if (!active || soonTimer) return;
    soonTimer = setTimeout(() => {
      soonTimer = null;
      void reconcile("soon");
    }, ms);
    soonTimer.unref?.();
  };

  /** resolves true when trading was reconciled with a fresh read */
  const reconcile = async (reason: string): Promise<boolean> => {
    if (!active || !rest || !executor) return false;
    if (reconciling) return reconciling;
    if (executor.busy()) {
      reconcileSoon(SETTLE_DELAY_MS);
      return false;
    }
    const r = rest;
    const ex = executor;
    reconciling = (async () => {
      let ran = false;
      try {
        const [positions, balance] = await Promise.all([
          r.positions().then(
            (v) => ({ ok: true as const, v }),
            (err: Error) => ({ ok: false as const, err }),
          ),
          r.balance().then(
            (v) => ({ ok: true as const, v }),
            (err: Error) => ({ ok: false as const, err }),
          ),
        ]);
        if (!active) return false;
        if (balance.ok) {
          balanceFails = 0;
          st.balanceError = null;
          if (balance.v) {
            st.balanceUsd = balance.v.balanceUsd;
            st.buyingPowerUsd = balance.v.buyingPowerUsd;
          }
        } else {
          balanceFails++;
          st.balanceError = `balance read failed: ${balance.err.message}`;
          log.warn({ err: balance.err.message, fails: balanceFails }, "live: balance read failed");
          if (balanceFails >= BALANCE_FAILS_TO_CLEAR) st.balanceUsd = st.buyingPowerUsd = null; // do not show it as current
        }
        try {
          if (!positions.ok) throw positions.err;
          const traded = tradedSlugs();
          const bad = positions.v.unparseable.filter((slug) => traded.has(slug));
          if (bad.length > 0) throw new Error(`positions: ${bad.join(", ")} has an unparseable netPosition`);
          if (positions.v.unparseable.length > 0) log.debug({ slugs: positions.v.unparseable }, "live: skipped unparseable positions of other markets");
          if (ex.busy()) {
            // an order went out while we were reading: this view may predate its fills
            reconcileSoon(SETTLE_DELAY_MS);
          } else {
            const { out, pending } = confirmDiffs(toRemote(positions.v.positions), ex);
            deps.services.trading.reconcile(out);
            st.lastReconcileTs = deps.now();
            ran = true;
            if (pending > 0) reconcileSoon(CONFIRM_MS + 250); // re-read to confirm or drop the discrepancy
          }
          st.restError = null;
        } catch (err) {
          const msg = (err as Error).message;
          st.restError = `reconcile failed: ${msg}`;
          log.warn({ err: msg, reason }, "live: reconcile failed");
        }
        // independent of the positions read: order polling (socket down) and ledger pruning must keep running
        try {
          await ex.pollOpen();
        } catch (err) {
          log.warn({ err: (err as Error).message }, "live: order poll failed");
        }
      } finally {
        reconciling = null;
        publish();
      }
      return ran;
    })();
    return reconciling;
  };

  const onFrame = (f: PrivateFrame) => {
    if (!executor) return;
    if (st.wsError && f.kind !== "error" && f.kind !== "heartbeat" && f.kind !== "other") {
      st.wsError = null; // a good data frame: the earlier server error is over
      publish();
    }
    switch (f.kind) {
      case "execution":
        executor.onExecution(f.execution);
        break;
      case "orderSnapshot":
        executor.onOrderSnapshot(f.orders, f.eof);
        break;
      case "position":
        reconcileSoon(1500);
        break;
      case "balance":
        if (f.balance) {
          st.balanceUsd = f.balance.balanceUsd ?? st.balanceUsd;
          st.buyingPowerUsd = f.balance.buyingPowerUsd ?? st.buyingPowerUsd;
          publish();
        }
        break;
      default:
        break;
    }
  };

  // ---------------------------------------------------------------- lifecycle

  const activate = (): boolean => {
    if (active) return true;
    if (st.setupError) return false; // the keys come from .env: a malformed secret will not fix itself
    if (!modeLive() || !deps.engine.liveAvailable() || !cfg.keyId || !cfg.secretKey) return false;
    try {
      rest = new LiveRestClient({ baseUrl: cfg.apiUrl, keyId: cfg.keyId, secretKey: cfg.secretKey, timeoutMs: cfg.timeoutMs, fetchImpl: opts.fetchImpl, now: deps.now });
    } catch {
      st.setupError = "POLYMARKET_US_SECRET is not a valid base64 Ed25519 key (32 or 64 bytes)";
      log.error("live: invalid POLYMARKET_US_SECRET; live trading stays off");
      publish();
      return false;
    }
    st.setupError = null;
    const r = rest;
    const ex = new LiveExecutor({
      rest: r,
      cfg,
      logger: log,
      now: deps.now,
      emit: applyExternal,
      requestReconcile: (why) => {
        log.warn({ why }, "live: reconcile requested");
        reconcileSoon(1000);
      },
      streamConnected: () => !!stream?.connected,
      localPosition: (instrument) => {
        const p = localLive().find((x) => x.instrument === instrument);
        return p ? { qty: p.qty, costUsd: p.costUsd } : null;
      },
      notify: notice,
    });
    executor = ex;
    try {
      ex.seed(deps.services.trading.orders({ limit: 500 }));
    } catch (err) {
      log.warn({ err: (err as Error).message }, "live: could not seed open orders from trading");
    }
    active = true;
    if (opts.privateWs !== false) {
      const s = new PrivateStream({
        url: cfg.privateWsUrl,
        logger: log,
        auth: { keyId: cfg.keyId, secretKey: cfg.secretKey },
        signNow: () => deps.now() + r.clockOffsetMs,
        ...opts.stream,
      });
      s.on("open", () => {
        st.connected = true;
        st.wsError = null;
        publish();
        reconcileSoon(500);
      });
      s.on("close", () => {
        st.connected = false;
        publish();
      });
      s.on("serverError", (e: string) => {
        st.wsError = `private ws: ${e}`;
        publish();
      });
      s.on("frame", onFrame);
      stream = s;
      s.start();
    }
    reconcileTimer = setInterval(() => void reconcile("timer"), cfg.reconcileMs);
    reconcileTimer.unref?.();
    void reconcile("activate");
    log.info({ api: cfg.apiUrl, privateWs: opts.privateWs !== false }, "live trading active");
    publish();
    return true;
  };

  const deactivate = (why: string) => {
    sessions.clear();
    if (!active) return;
    active = false;
    if (reconcileTimer) clearInterval(reconcileTimer);
    if (soonTimer) clearTimeout(soonTimer);
    reconcileTimer = soonTimer = null;
    stream?.removeAllListeners();
    stream?.stop();
    stream = null;
    st.connected = false;
    st.wsError = null;
    pendingDiffs.clear();
    const ex = executor;
    executor = null;
    rest = null;
    const resting = ex?.openOrderCount() ?? 0;
    if (ex && resting > 0 && why === "settings") {
      // leaving live must not leave live exposure behind: cancel ours and learn their final state, then stop
      void ex
        .drain()
        .then((r) => {
          if (r.failed.length > 0) {
            notice("error", "Live orders still resting", `${r.failed.length} live order(s) could not be cancelled on Polymarket US after leaving live mode: ${r.failed[0]}`);
          } else {
            notice("warn", "Live orders cancelled", `${r.cancelled} resting live order(s) were cancelled on Polymarket US when leaving live mode${r.unknown > 0 ? `; ${r.unknown} unconfirmed order(s) need checking on the exchange` : ""}.`);
          }
        })
        .catch((err: Error) => {
          log.error({ err: err.message }, "live: cancelling resting orders failed");
          notice("error", "Live orders still resting", `${resting} live order(s) may remain on Polymarket US: ${err.message}`);
        })
        .finally(() => ex.stop());
    } else {
      ex?.stop();
    }
    log.info({ why, resting }, "live trading inactive");
  };

  const onSettings = (s: Settings) => {
    if (s.mode === "live") activate();
    else deactivate("settings");
    publish();
  };

  // ---------------------------------------------------------------- service

  const service: LiveModuleService = {
    status,
    isSessionConfirmed(session) {
      if (!session) return false;
      const exp = sessions.get(session);
      if (exp === undefined) return false;
      if (exp < deps.now()) {
        sessions.delete(session);
        return false;
      }
      return true;
    },
    executor() {
      if (!modeLive()) return null;
      return activate() ? executor : null;
    },
    async reconcileNow(reason = "manual") {
      const ran = await reconcile(reason);
      return { ...status(), deferred: !ran };
    },
  };

  return {
    name: "live",
    service,
    register(app) {
      app.get("/api/live/status", async () => status());
      app.post("/api/live/confirm", async (): Promise<LiveConfirmResponse> => {
        const s = status();
        if (!s.available) throw new ApiError(409, "live_unavailable", "live trading needs POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET in the backend .env");
        if (!s.enabled) throw new ApiError(409, "live_unavailable", "switch settings to live mode first");
        if (!activate() || !rest) throw new ApiError(409, "live_unavailable", st.setupError ?? "live trading could not start");
        // prove the key works before arming a session (a cheap signed read)
        try {
          const b = await rest.balance();
          if (b) {
            st.balanceUsd = b.balanceUsd;
            st.buyingPowerUsd = b.buyingPowerUsd;
          }
          st.restError = null;
        } catch (err) {
          const msg = (err as Error).message;
          st.restError = msg;
          publish();
          if (err instanceof LiveHttpError && (err.status === 401 || err.status === 403)) {
            throw new ApiError(409, "live_unavailable", `Polymarket US rejected the API key (${err.status}); check the key and the system clock`);
          }
          throw new ApiError(502, "exchange_error", `Polymarket US is not reachable: ${msg}`);
        }
        const session = randomUUID();
        const expiresTs = deps.now() + LIVE_SESSION_TTL_MS;
        sessions.set(session, expiresTs);
        log.info({ expiresTs }, "live trading confirmed for a browser session");
        publish();
        return { session, expiresTs };
      });
      app.post("/api/live/reconcile", async () => {
        if (!active) throw new ApiError(409, "live_unavailable", "live trading is not active");
        return service.reconcileNow("manual");
      });
    },
    start() {
      deps.engine.on("settings", onSettings);
      if (modeLive()) activate();
    },
    stop() {
      deps.engine.off("settings", onSettings);
      deactivate("stop");
    },
    snapshot: () => ({ live: status() }),
  };
}
