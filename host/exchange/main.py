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
before anything is submitted. A clock skew over `auto_kill.clock_skew_ms`, seen by
the auth probe or on any other live answer, pauses live placements (`live_paused`,
`Executor.live_blocked`) and auto-kills `clock_skew`; cancels, the open-order audit
and the fills poll keep running so the kill's cancels reach the exchange, and the
first live answer with the skew back in range clears the pause. `run_once(pool)`
runs every task once for tests and the CLI.
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
from host.exchange.credentials import CredentialsError
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
        self.credentials_error: str | None = None
        self.live_paused: str | None = None
        self.started = False
        self.source: MarketSource | None = None
        self.source_name: str | None = None
        self.intervals: dict[str, float] = dict(INTERVALS)
        self.skew_limit_ms = live_sync.DEFAULT_SKEW_LIMIT_MS
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
        """The default factory, sharing the loop's rate limiter and the skew limit."""
        return build_live_gateway(config, creds, self.limiter, self.skew_limit_ms)

    def start(self, conn: Any, now: datetime) -> None:
        """Build the live gateway once and, with credentials, run the auth probe, the
        reconciliation and the open-order audit before the first submission. A
        malformed secret is recorded (its secret-free message) as the last auth error."""
        self.started = True
        self.refresh_source(conn, now)
        self.skew_limit_ms = live_sync.skew_limit_ms(conn)
        try:
            creds = self.load_credentials()
        except CredentialsError as exc:
            creds, self.credentials_error = None, f"credentials malformed: {exc}"
        self.credentials_present = creds is not None
        self.live_gateway = self.gateway_factory(polymarket_us_config(conn), creds)
        self.executor = Executor(PaperGateway(), self.live_gateway, self.limiter)
        state.set_credentials_present(conn, self.credentials_present, self.credentials_error)
        log.info("live gateway: %s (credentials %s)", self.live_gateway.name, "present" if creds else "absent")

    def startup_sequence(self, now: datetime) -> dict[str, Any]:
        results = {"auth": self.run_task("auth", now)}
        if self.credentials_present:
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
        self.skew_limit_ms = live_sync.skew_limit_ms(conn)
        if getattr(self.live_gateway, "max_skew_ms", None) is not None:
            self.live_gateway.max_skew_ms = self.skew_limit_ms  # type: ignore[attr-defined]
        return self.source

    # ------------------------------------------------------------------ tasks

    def task_heartbeat(self, conn: Any, now: datetime) -> Any:
        state.heartbeat(conn, self.source_name or str(get_setting(conn, "market_source", "sim")), self.last_error, now)

    def task_auth(self, conn: Any, now: datetime) -> Any:
        result = live_sync.auth_check(conn, self.live_gateway, now, self.credentials_present, self.credentials_error)
        if result.get("auth_ok"):
            self._set_paused("clock_skew" if result.get("skew_over_limit") else None)
        return result

    def _set_paused(self, reason: str | None) -> None:
        if reason != self.live_paused:
            log.warning("live placements %s", f"paused: {reason}" if reason else "resumed")
        self.live_paused = reason
        self.executor.live_blocked = reason

    def check_skew(self, conn: Any) -> None:
        """After a live task: the skew measured on its answers pauses placements
        and auto-kills when over the limit, and clears the pause once back in range
        (the auth probe is not the only answer that carries a Date header)."""
        skew = getattr(self.live_gateway, "last_skew_ms", None)
        if not self.credentials_present or not isinstance(skew, int):
            return
        if abs(skew) > self.skew_limit_ms:
            if self.live_paused is None:
                state.set_clock_skew(conn, skew)
                self._set_paused("clock_skew")
                live_sync.maybe_auto_kill(conn, "clock_skew", {"skew_ms": skew, "limit_ms": self.skew_limit_ms})
        elif self.live_paused == "clock_skew":
            state.set_clock_skew(conn, skew)
            self._set_paused(None)

    def task_startup_reconcile(self, conn: Any, now: datetime) -> Any:
        return live_sync.startup_reconcile(conn, self.live_gateway, now)

    def task_discover(self, conn: Any, now: datetime) -> Any:
        return mapping.discover(conn, self.refresh_source(conn, now), now)

    def task_snapshots(self, conn: Any, now: datetime) -> Any:
        source = self.source or self.refresh_source(conn, now)
        return snapshots.poll(conn, source, self.limiter, now)

    def task_open_orders_audit(self, conn: Any, now: datetime) -> Any:
        if not self.credentials_present:
            return None
        result = live_sync.audit_open_orders(conn, self.live_gateway, now)
        self.check_skew(conn)
        return result

    def task_executor(self, conn: Any, now: datetime) -> Any:
        result = self.executor.tick(conn, now)
        self.check_skew(conn)
        return result

    def task_fills(self, conn: Any, now: datetime) -> Any:
        return paper.process(conn, now)

    def task_live_fills(self, conn: Any, now: datetime) -> Any:
        if not self.credentials_present:
            return None
        result = live_sync.poll_fills(conn, self.live_gateway, now)
        self.check_skew(conn)
        return result

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
    environment variables are missing. A present but malformed secret raises
    CredentialsError (its message never carries the secret) after logging it, so
    the owner can tell a bad format from a missing file. The values are never logged."""
    try:
        from host.exchange.credentials import load
    except ImportError:
        log.warning("host.exchange.credentials is not available: no live gateway")
        return None
    try:
        return load()
    except CredentialsError as exc:
        log.error("credentials could not be loaded: %s", exc)
        raise
    except Exception as exc:  # noqa: BLE001 - anything else must not stop the loop
        log.error("credentials could not be loaded: %s", exc.__class__.__name__)
        return None


def build_live_gateway(config: dict[str, Any], creds: Any, limiter: RateLimiter | None = None, max_skew_ms: int | None = None) -> OrderGateway:
    """The real LiveGateway with credentials (imported lazily), otherwise a gateway
    that rejects every live call as not configured. `max_skew_ms` is the skew over
    which the gateway refuses to place."""
    if creds is None:
        return OrderGateway()
    from host.exchange.adapters.polymarket_us_live import LiveGateway

    return LiveGateway(creds, config, limiter=limiter, max_skew_ms=max_skew_ms)


def polymarket_us_config(conn: Any) -> dict[str, Any]:
    """The market_source_config.polymarket_us block (defaults are applied by the gateway)."""
    config = get_setting(conn, "market_source_config", {}) or {}
    block = config.get("polymarket_us") if isinstance(config, dict) else None
    return dict(block or {})


def live_gateway_from_settings(conn: Any) -> tuple[OrderGateway, Any]:
    """(gateway, credentials) for the CLI commands that talk to the exchange directly
    (CredentialsError for a malformed secret, so the command can say so)."""
    creds = load_credentials()
    return build_live_gateway(polymarket_us_config(conn), creds, max_skew_ms=live_sync.skew_limit_ms(conn)), creds


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
