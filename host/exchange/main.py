"""The exchange process: `python -m host.exchange.main` (docs/TRADING.md, "Processes").

One loop, every 250 ms, runs whichever tasks are due: the heartbeat (5 s), market
discovery (5 min), snapshots (the poller applies its own cadences and a 5 s budget per
pass, book fetches time out after 2 s), the executor outbox (250 ms), paper fills
(1 s), the scores poll (60 s, only while a game with an assignment awaits a final),
settlement (30 s) and retention (nightly). Every task runs in its own transaction and
an exception, or a soft error a task reports (failed book fetches, a game whose
settlement rolled back), is logged and remembered as `last_error` without stopping
the loop. `run_once(pool)` runs every task once for tests and the CLI.
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
from host.exchange import mapping, paper, retention, scores, settle, snapshots, state
from host.exchange.adapters import make_source
from host.exchange.adapters.base import MarketSource, PaperGateway, utcnow
from host.exchange.adapters.sim import SimSource
from host.exchange.executor import Executor
from host.exchange.ratelimit import RateLimiter
from host.settings import get_int_setting, get_setting

log = logging.getLogger(__name__)

INTERVALS: dict[str, float] = {
    "heartbeat": 5.0, "discover": 300.0, "snapshots": 1.0, "executor": 0.25, "fills": 1.0,
    "scores": 60.0, "settle": 30.0, "retention": 60.0,
}
ORDER = ("heartbeat", "discover", "snapshots", "executor", "fills", "scores", "settle", "retention")
LOOP_SLEEP = 0.25
RETENTION_HOUR = 3


class ExchangeLoop:
    """Holds the source, the gateway, the executor and the task clock between ticks."""

    def __init__(self, pool: ConnectionPool, clock: Callable[[], datetime] = utcnow) -> None:
        self.pool = pool
        self.clock = clock
        self.limiter = RateLimiter()
        self.executor = Executor(PaperGateway(), self.limiter)
        self.source: MarketSource | None = None
        self.source_name: str | None = None
        self.last_run: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.retention_day: Any = None

    @property
    def last_error(self) -> str | None:
        """The most recent failure of a task that has not succeeded since."""
        return next(reversed(self.errors.values())) if self.errors else None

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
        return self.source

    # ------------------------------------------------------------------ tasks

    def task_heartbeat(self, conn: Any, now: datetime) -> Any:
        state.heartbeat(conn, self.source_name or str(get_setting(conn, "market_source", "sim")), self.last_error, now)

    def task_discover(self, conn: Any, now: datetime) -> Any:
        return mapping.discover(conn, self.refresh_source(conn, now), now)

    def task_snapshots(self, conn: Any, now: datetime) -> Any:
        source = self.source or self.refresh_source(conn, now)
        return snapshots.poll(conn, source, self.limiter, now)

    def task_executor(self, conn: Any, now: datetime) -> Any:
        return self.executor.tick(conn, now)

    def task_fills(self, conn: Any, now: datetime) -> Any:
        return paper.process(conn, now)

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
        last = self.last_run.get(name)
        return last is None or now.timestamp() - last >= INTERVALS[name]

    def run_task(self, name: str, now: datetime) -> Any:
        """Run one task in its own transaction. A raised exception or a soft error
        (a dict result with a non-empty "error", such as failed book fetches or a
        game whose settlement rolled back) is remembered as last_error until the
        task next succeeds cleanly."""
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

    def run_due(self, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
        now = now or self.clock()
        results: dict[str, Any] = {}
        for name in ORDER:
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
