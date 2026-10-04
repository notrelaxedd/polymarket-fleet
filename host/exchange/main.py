"""The exchange process: `python -m host.exchange.main` (docs/TRADING.md, "Processes";
docs/LIVE.md for the live tasks).

One loop, every 250 ms, runs whichever tasks are due: the heartbeat (5 s), the auth
probe (`auth_probe_interval_s`, and at start), market discovery (5 min), snapshots
(the poller applies its own cadences and a 5 s budget per pass), the open-order audit
(`open_orders_audit_s`, and at start), the executor outbox (250 ms), paper fills
(1 s), live fills (`live_fills_poll_s`, only while live orders are active), the
scores poll (60 s), settlement (30 s) and retention (nightly). Every task runs in its
own transaction and an exception, or a soft error a task reports, is logged and
remembered as `last_error` without stopping the loop. With credentials the loop runs
the auth probe, the reconciliation of `submitting` rows and the open-order audit
before anything is submitted. `run_once(pool)` runs every task once for tests and
the CLI.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from psycopg_pool import ConnectionPool

from host import db
from host.config import Config
from host.exchange import live_sync, mapping, paper, retention, scores, settle, snapshots, state
from host.exchange.adapters import make_source
from host.exchange.adapters.base import MarketSource, OrderGateway, PaperGateway, utcnow
from host.exchange.adapters.sim import SimSource
from host.exchange.executor import Executor
from host.exchange.ratelimit import RateLimiter
from host.settings import get_int_setting, get_setting

log = logging.getLogger(__name__)

INTERVALS: dict[str, float] = {
    "heartbeat": 5.0, "auth": 300.0, "discover": 300.0, "snapshots": 1.0, "open_orders_audit": 60.0,
    "executor": 0.25, "fills": 1.0, "live_fills": 2.0, "scores": 60.0, "settle": 30.0, "retention": 60.0,
}
ORDER = ("heartbeat", "auth", "discover", "snapshots", "open_orders_audit", "executor", "fills", "live_fills",
         "scores", "settle", "retention")
LIVE_TASKS = ("open_orders_audit", "live_fills")
SETTING_INTERVALS = {"auth": "auth_probe_interval_s", "open_orders_audit": "open_orders_audit_s", "live_fills": "live_fills_poll_s"}
LOOP_SLEEP = 0.25
RETENTION_HOUR = 3

GatewayFactory = Callable[[dict[str, Any], Any], OrderGateway]


class ExchangeLoop:
    """Holds the source, the gateways, the executor and the task clock between ticks."""

    def __init__(self, pool: ConnectionPool, clock: Callable[[], datetime] = utcnow, gateway_factory: GatewayFactory | None = None) -> None:
        self.pool = pool
        self.clock = clock
        self.limiter = RateLimiter()
        self.gateway_factory: GatewayFactory = gateway_factory or self.build_live_gateway
        self.live_gateway: OrderGateway = OrderGateway()
        self.executor = Executor(PaperGateway(), self.live_gateway, self.limiter)
        self.credentials_present = False
        self.live_paused: str | None = None
        self.started = False
        self.source: MarketSource | None = None
        self.source_name: str | None = None
        self.intervals: dict[str, float] = dict(INTERVALS)
        self.last_run: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.retention_day: Any = None

    @property
    def last_error(self) -> str | None:
        """The most recent failure of a task that has not succeeded since."""
        return next(reversed(self.errors.values())) if self.errors else None

    # ------------------------------------------------------------- credentials

    def load_credentials(self) -> Any:
        """The module-level loader (a test replaces this method to inject credentials)."""
        return load_credentials()

    def build_live_gateway(self, config: dict[str, Any], creds: Any) -> OrderGateway:
        """The default factory, sharing the loop's rate limiter."""
        return build_live_gateway(config, creds, self.limiter)

    def start(self, conn: Any, now: datetime) -> None:
        """Build the live gateway once and, with credentials, run the auth probe, the
        reconciliation and the open-order audit before the first submission."""
        self.started = True
        self.refresh_source(conn, now)
        creds = self.load_credentials()
        self.credentials_present = creds is not None
        self.live_gateway = self.gateway_factory(polymarket_us_config(conn), creds)
        self.executor = Executor(PaperGateway(), self.live_gateway, self.limiter)
        state.set_credentials_present(conn, self.credentials_present)
        log.info("live gateway: %s (credentials %s)", self.live_gateway.name, "present" if creds else "absent")

    def startup_sequence(self, now: datetime) -> dict[str, Any]:
        results = {"auth": self.run_task("auth", now)}
        if self.credentials_present and not self.live_paused:
            results["startup_reconcile"] = self.run_task("startup_reconcile", now)
            self.last_run["open_orders_audit"] = now.timestamp()
        return results

    # ----------------------------------------------------------------- source

    def refresh_source(self, conn: Any, now: datetime) -> MarketSource:
        name = str(get_setting(conn, "market_source", "sim") or "sim")
        config = get_setting(conn, "market_source_config", {}) or {}
        if self.source is None or name != self.source_name:
            self.source = make_source(name, config if isinstance(config, dict) else {})
            self.source_name = name
            log.info("market source: %s", name)
        if isinstance(self.source, SimSource):
            lookahead = get_int_setting(conn, "market_lookahead_days", 8)
            for game in mapping.upcoming_games(conn, now, lookahead):
                self.source.games[game["game_id"]] = game
        limits = get_setting(conn, "rate_limits", None)
        self.limiter.update_limits(limits if isinstance(limits, dict) else None, now)
        for task, key in SETTING_INTERVALS.items():
            self.intervals[task] = float(get_int_setting(conn, key, int(INTERVALS[task])))
        return self.source

    # ------------------------------------------------------------------ tasks

    def task_heartbeat(self, conn: Any, now: datetime) -> Any:
        state.heartbeat(conn, self.source_name or str(get_setting(conn, "market_source", "sim")), self.last_error, now)

    def task_auth(self, conn: Any, now: datetime) -> Any:
        result = live_sync.auth_check(conn, self.live_gateway, now, self.credentials_present)
        if result.get("auth_ok"):
            self.live_paused = "clock_skew" if result.get("skew_over_limit") else None
            self.executor.live_blocked = self.live_paused
        return result

    def task_startup_reconcile(self, conn: Any, now: datetime) -> Any:
        return live_sync.startup_reconcile(conn, self.live_gateway, now)

    def task_discover(self, conn: Any, now: datetime) -> Any:
        return mapping.discover(conn, self.refresh_source(conn, now), now)

    def task_snapshots(self, conn: Any, now: datetime) -> Any:
        source = self.source or self.refresh_source(conn, now)
        return snapshots.poll(conn, source, self.limiter, now)

    def task_open_orders_audit(self, conn: Any, now: datetime) -> Any:
        if not self.credentials_present or self.live_paused:
            return None
        return live_sync.audit_open_orders(conn, self.live_gateway, now)

    def task_executor(self, conn: Any, now: datetime) -> Any:
        return self.executor.tick(conn, now)

    def task_fills(self, conn: Any, now: datetime) -> Any:
        return paper.process(conn, now)

    def task_live_fills(self, conn: Any, now: datetime) -> Any:
        if not self.credentials_present or self.live_paused:
            return None
        return live_sync.poll_fills(conn, self.live_gateway, now)

    def task_scores(self, conn: Any, now: datetime) -> Any:
        return scores.poll(conn, now)

    def task_settle(self, conn: Any, now: datetime) -> Any:
        errors: list[str] = []
        settled = settle.settle_due(conn, errors=errors)
        return {"settled": settled, "error": "; ".join(errors) or None}

    def task_retention(self, conn: Any, now: datetime) -> Any:
        tz = str(get_setting(conn, "tz", "America/New_York") or "America/New_York")
        try:
            local = now.astimezone(ZoneInfo(tz))
        except Exception:  # noqa: BLE001 - a bad tz setting must not stop retention
            local = now
        if local.hour != RETENTION_HOUR or self.retention_day == local.date():
            return None
        self.retention_day = local.date()
        return retention.run(conn, now)

    # ------------------------------------------------------------------- loop

    def due(self, name: str, now: datetime) -> bool:
        if name in LIVE_TASKS and not self.credentials_present:
            return False
        last = self.last_run.get(name)
        return last is None or now.timestamp() - last >= self.intervals.get(name, INTERVALS.get(name, 1.0))

    def run_task(self, name: str, now: datetime) -> Any:
        """Run one task in its own transaction. A raised exception or a soft error
        (a dict result with a non-empty "error") is remembered as last_error until
        the task next succeeds cleanly."""
        task = getattr(self, f"task_{name}")
        self.last_run[name] = now.timestamp()
        try:
            with self.pool.connection() as conn:
                result = task(conn, now)
            self.errors.pop(name, None)
            if isinstance(result, dict) and result.get("error"):
                self.errors[name] = f"{name}: {result['error']}"[:500]
            return result
        except Exception as exc:  # noqa: BLE001 - log, remember, carry on
            log.exception("exchange task %s failed", name)
            self.errors.pop(name, None)
            self.errors[name] = f"{name}: {exc}"[:500]
            return None

    def ensure_started(self, now: datetime) -> dict[str, Any]:
        if self.started:
            return {}
        with self.pool.connection() as conn:
            self.start(conn, now)
        return self.startup_sequence(now)

    def run_due(self, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
        now = now or self.clock()
        results: dict[str, Any] = self.ensure_started(now)
        for name in ORDER:
            if name in results:
                continue
            if force or self.due(name, now):
                results[name] = self.run_task(name, now)
        return results

    def run_forever(self, stop: threading.Event, sleep: Callable[[float], None] = time.sleep) -> None:
        while not stop.is_set():
            started = time.monotonic()
            self.run_due()
            spent = time.monotonic() - started
            sleep(max(0.0, LOOP_SLEEP - spent))


def run_once(pool: ConnectionPool, now: datetime | None = None, loop: ExchangeLoop | None = None) -> dict[str, Any]:
    """Every task once, in order, each in its own transaction (tests and the CLI)."""
    loop = loop or ExchangeLoop(pool)
    now = now or loop.clock()
    with pool.connection() as conn:
        loop.refresh_source(conn, now)
    results = loop.run_due(now, force=True)
    results["last_error"] = loop.last_error
    return results


def load_credentials() -> Any:
    """host.exchange.credentials.load(), imported lazily; None when the module or the
    environment variables are missing. The values are never logged."""
    try:
        from host.exchange.credentials import load
    except ImportError:
        log.warning("host.exchange.credentials is not available: no live gateway")
        return None
    try:
        return load()
    except Exception as exc:  # noqa: BLE001 - a malformed secret must not stop the loop
        log.error("credentials could not be loaded: %s", exc.__class__.__name__)
        return None


def build_live_gateway(config: dict[str, Any], creds: Any, limiter: RateLimiter | None = None) -> OrderGateway:
    """The real LiveGateway with credentials (imported lazily), otherwise a gateway
    that rejects every live call as not configured."""
    if creds is None:
        return OrderGateway()
    from host.exchange.adapters.polymarket_us_live import LiveGateway

    return LiveGateway(creds, config, limiter=limiter)


def polymarket_us_config(conn: Any) -> dict[str, Any]:
    """The market_source_config.polymarket_us block (defaults are applied by the gateway)."""
    config = get_setting(conn, "market_source_config", {}) or {}
    block = config.get("polymarket_us") if isinstance(config, dict) else None
    return dict(block or {})


def live_gateway_from_settings(conn: Any) -> tuple[OrderGateway, Any]:
    """(gateway, credentials) for the CLI commands that talk to the exchange directly."""
    creds = load_credentials()
    return build_live_gateway(polymarket_us_config(conn), creds), creds


def main() -> None:
    """python -m host.exchange.main"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    pool = db.make_pool(config.database_url, min_size=1, max_size=4)
    loop = ExchangeLoop(pool)
    stop = threading.Event()
    log.info("exchange process starting")
    try:
        loop.run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        pool.close()


if __name__ == "__main__":
    main()
