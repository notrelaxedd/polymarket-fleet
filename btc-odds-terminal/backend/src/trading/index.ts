// Paper trading + order routing + OCO bracket watcher. OWNER: trading-backend.
//
// Routes: POST /api/orders, GET /api/orders, DELETE /api/orders/:id, GET /api/fills, GET /api/positions,
//         POST /api/positions/:id/close, PUT /api/positions/:id/bracket, GET /api/brackets.
// Broadcasts: orders / fills / positions / brackets (upserts), notice. Calls services.risk.check() before
// every non-reducing order and services.risk.refresh() after fills; uses services.live.executor() in live mode
// (and services.live.isSessionConfirmed(req.liveSession) -> else 409 live_confirm_required). Exits (close,
// bracket legs, kill) reduce risk and never need a live session.
// Engine hooks: "odds" / "tick" / "book" drive the bracket watcher (plus a 1s timer for time exits, retries,
// window expiry, orphaned pending orders and ended windows whose settlement was missed or never comes);
// "settled" settles positions ($1 / $0 per contract); "rollover" expires resting orders; "settings" warns when
// live mode is left with live positions open.
// The watcher runs in the worker: brackets are enforced with no browser open.

import type { BookLevels, MarketWindow, Settings } from "../../../shared/protocol.ts";
import type { BackendModule, ModuleDeps, TradingService } from "../modules/types.ts";
import { TradingCore } from "./service.ts";
import {
  parseBracket,
  parseBracketsQuery,
  parseClose,
  parseFillsQuery,
  parseOrdersQuery,
  parsePlaceOrder,
  parsePositionsQuery,
} from "./validate.ts";

export { TradingCore } from "./service.ts";
export { PaperExecutor } from "./paper.ts";

/** Watcher heartbeat: time exits, exit retries, mark broadcasts, expiry of resting orders. */
export const WATCH_INTERVAL_MS = 1_000;

export function createTrading(deps: ModuleDeps): BackendModule<TradingService> & { core: TradingCore } {
  const core = new TradingCore(deps);

  const service: TradingService = {
    placeOrder: (req, opts) => core.placeOrder(req, opts),
    cancelOrder: (id) => core.cancelOrder(id),
    cancelAll: (reason) => core.cancelAll(reason),
    closePosition: (id, req) => core.closePosition(id, req),
    flattenAll: (reason) => core.flattenAll(reason),
    setBracket: (positionId, b) => core.setBracket(positionId, b),
    orders: (q) => core.orders(q),
    fills: (q) => core.fills(q),
    positions: (q) => core.positions(q),
    brackets: (q) => core.brackets(q),
    riskInputs: (windowId) => core.riskInputs(windowId),
    applyExternal: (u) => core.applyExternal(u),
    reconcile: (remote) => core.reconcile(remote),
  };

  const guard = (what: string, fn: () => void) => {
    try {
      fn();
    } catch (err) {
      deps.logger.error({ err }, `trading ${what} failed`);
    }
  };
  const onOdds = () => guard("odds", () => core.onQuote());
  const onTick = () => guard("tick", () => core.evaluate());
  const onBook = (b: BookLevels) => guard("book", () => core.onBook(b));
  const onSettled = (w: MarketWindow) => guard("settle", () => core.settle(w));
  const onSettings = (s: Settings) => guard("settings", () => core.onSettings(s));
  const onRollover = () =>
    guard("rollover", () => {
      core.expireOrders();
      core.evaluate();
    });
  let timer: NodeJS.Timeout | null = null;

  return {
    name: "trading",
    service,
    core,
    register(app) {
      app.post("/api/orders", async (req) => service.placeOrder(parsePlaceOrder(req.body)));
      app.get("/api/orders", async (req) => service.orders(parseOrdersQuery(req.query)));
      app.delete("/api/orders/:id", async (req) => service.cancelOrder((req.params as { id: string }).id));
      app.get("/api/fills", async (req) => service.fills(parseFillsQuery(req.query)));
      app.get("/api/positions", async (req) => service.positions(parsePositionsQuery(req.query)));
      app.post("/api/positions/:id/close", async (req) =>
        service.closePosition((req.params as { id: string }).id, parseClose(req.body)),
      );
      app.put("/api/positions/:id/bracket", async (req) =>
        service.setBracket((req.params as { id: string }).id, parseBracket(req.body)),
      );
      app.get("/api/brackets", async (req) => service.brackets(parseBracketsQuery(req.query)));
    },
    start() {
      guard("recover", () => core.recover());
      core.initMode();
      deps.engine.addSettingsGuard?.((next, prev) => core.modeSwitchBlocker(next, prev));
      deps.engine.on("odds", onOdds);
      deps.engine.on("tick", onTick);
      deps.engine.on("book", onBook);
      deps.engine.on("settled", onSettled);
      deps.engine.on("rollover", onRollover);
      deps.engine.on("settings", onSettings);
      timer = setInterval(
        () =>
          guard("watch", () => {
            core.evaluate();
            core.expireOrders();
            core.settleEnded();
            core.maybeBroadcastMarks();
          }),
        WATCH_INTERVAL_MS,
      );
      timer.unref?.();
    },
    async stop() {
      if (timer) clearInterval(timer);
      timer = null;
      deps.engine.off("odds", onOdds);
      deps.engine.off("tick", onTick);
      deps.engine.off("book", onBook);
      deps.engine.off("settled", onSettled);
      deps.engine.off("rollover", onRollover);
      deps.engine.off("settings", onSettings);
      await core.idle();
    },
    snapshot: () => core.snapshot(),
  };
}
