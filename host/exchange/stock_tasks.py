"""The stock trading tasks of the exchange process (contract section 6): stock_broker
(every stock_broker_poll_s), stock_executor (every second for the outbox, the Alpaca
poll of our open orders every stock_orders_poll_s) and stock_marks (every minute, a
session is marked once).

They run on a thread of their own beside the NFL loop (host/exchange/main.py), so a
slow or hanging Alpaca answer never delays an NFL task; each task runs in its own
transaction and a failure is logged and remembered without stopping the others.
Without the Alpaca keys every task returns {"skipped": ...} without any request (the
broker row is set to keys_present false). `run_due(now, force=True)` runs them once
inline (run_once, tests).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable

from psycopg_pool import ConnectionPool

from host.exchange import alpaca_credentials, stock_broker
from host.exchange.adapters.base import utcnow
from host.exchange.alpaca_trading import AlpacaTrading
from host.exchange.stock_executor import StockExecutor
from host.settings import get_int_setting

log = logging.getLogger(__name__)

ORDER = ("stock_broker", "stock_executor", "stock_marks")
INTERVALS: dict[str, float] = {"stock_broker": 30.0, "stock_executor": 1.0, "stock_marks": 60.0}
LOOP_SLEEP = 0.25
NO_KEYS = "no Alpaca keys in exchange.env"


def default_client() -> AlpacaTrading | None:
    creds = alpaca_credentials.load()
    return AlpacaTrading(creds) if creds is not None else None


class StockTasks:
    """The client, the executor, the reconciler and the marker between runs."""

    def __init__(self, pool: ConnectionPool, clock: Callable[[], datetime] = utcnow,
                 client_factory: Callable[[], Any] | None = None) -> None:
        self.pool = pool
        self.clock = clock
        self.client_factory = client_factory or default_client
        self.client: Any = None
        self.config_error: str | None = None
        self.loaded = False
        self.executor = StockExecutor()
        self.reconciler = stock_broker.Reconciler()
        self.marker = stock_broker.Marker()
        self.intervals = dict(INTERVALS)
        self.orders_poll_s = 5.0
        self.last_poll = 0.0
        self.last_run: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.thread: threading.Thread | None = None

    def get_client(self) -> Any:
        """The trading client (built once), None without keys or with a bad base URL."""
        if not self.loaded:
            self.loaded = True
            try:
                self.client = self.client_factory()
            except alpaca_credentials.AlpacaConfigError as exc:
                self.config_error = str(exc)
            log.info("stock trading: %s", f"Alpaca {self.client.environment}" if self.client else self.config_error or NO_KEYS)
        return self.client

    def _skip(self) -> dict[str, Any]:
        return {"skipped": self.config_error or NO_KEYS}

    # ------------------------------------------------------------------ tasks

    def task_stock_broker(self, conn: Any, now: datetime) -> Any:
        self.intervals["stock_broker"] = float(get_int_setting(conn, "stock_broker_poll_s", 30))
        self.orders_poll_s = float(get_int_setting(conn, "stock_orders_poll_s", 5))
        client = self.get_client()
        if client is None:
            stock_broker.mark_keys_absent(conn, self.config_error)
            return self._skip()
        result = stock_broker.check(conn, client, now)
        if result.get("error"):
            return result
        if not conn.autocommit:
            conn.commit()  # the broker row stands even when the reconciliation fails
        return {"checked_at": now, "market_open": result["market_open"], **self.reconciler.run(conn, client, now)}

    def task_stock_executor(self, conn: Any, now: datetime) -> Any:
        client = self.get_client()
        if client is None:
            return self._skip()
        poll = now.timestamp() - self.last_poll >= self.orders_poll_s
        if poll:
            self.last_poll = now.timestamp()
        return self.executor.tick(conn, client, now, poll=poll)

    def task_stock_marks(self, conn: Any, now: datetime) -> Any:
        client = self.get_client()
        if client is None:
            return self._skip()
        return self.marker.run(conn, client, now)

    # ------------------------------------------------------------------- loop

    def due(self, name: str, now: datetime) -> bool:
        last = self.last_run.get(name)
        return last is None or now.timestamp() - last >= self.intervals[name]

    def run_task(self, name: str, now: datetime) -> Any:
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
            log.exception("stock task %s failed", name)
            self.errors[name] = f"{name}: {exc}"[:500]
            return None

    def run_due(self, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
        now = now or self.clock()
        return {name: self.run_task(name, now) for name in ORDER if force or self.due(name, now)}

    def run_forever(self, stop: threading.Event, sleep: Callable[[float], None] = time.sleep) -> None:
        while not stop.is_set():
            started = time.monotonic()
            self.run_due()
            sleep(max(0.0, LOOP_SLEEP - (time.monotonic() - started)))

    def start_thread(self, stop: threading.Event) -> threading.Thread:
        """Run the stock tasks on a daemon thread until `stop` is set."""
        self.thread = threading.Thread(target=self.run_forever, args=(stop,), name="stock-tasks", daemon=True)
        self.thread.start()
        return self.thread
