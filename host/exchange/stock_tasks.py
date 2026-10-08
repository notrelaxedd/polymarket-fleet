"""The stock trading tasks of the exchange process (contract section 6): stock_broker
(every stock_broker_poll_s: the check, the splits of host/exchange/stock_splits.py, the
reconciliation), stock_executor (every second for the outbox, the Alpaca poll of our
open orders every stock_orders_poll_s) and stock_marks (every minute, a session is
marked once, host/exchange/stock_marks.py).

They run on a thread of their own beside the NFL loop (host/exchange/main.py), so a
slow or hanging Alpaca answer never delays an NFL task; each task runs in its own
transaction and a failure is logged and remembered without stopping the others (the
exchange loop's last_error, so the heartbeat and the Trading page show it; a failed
reconciliation also goes to stock_broker_state.last_error for /stocks).
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

from host.exchange import alpaca_credentials, stock_bars, stock_broker, stock_marks, stock_splits
from host.exchange.adapters.base import utcnow
from host.exchange.alpaca_trading import AlpacaTrading
from host.exchange.stock_executor import StockExecutor
from host.settings import get_int_setting

log = logging.getLogger(__name__)

ORDER = ("stock_broker", "stock_executor", "stock_marks")
INTERVALS: dict[str, float] = {"stock_broker": 30.0, "stock_executor": 1.0, "stock_marks": 60.0}
LOOP_SLEEP = 0.25
NO_KEYS = "no Alpaca keys in exchange.env"


def _commit(conn: Any) -> None:
    if not conn.autocommit:
        conn.commit()


def _rollback(conn: Any) -> None:
    if not conn.autocommit:
        conn.rollback()


def default_client() -> AlpacaTrading | None:
    creds = alpaca_credentials.load()
    return AlpacaTrading(creds) if creds is not None else None


class StockTasks:
    """The client, the executor, the reconciler and the marker between runs."""

    def __init__(self, pool: ConnectionPool, clock: Callable[[], datetime] = utcnow,
                 client_factory: Callable[[], Any] | None = None, data_factory: Callable[[], Any] | None = None) -> None:
        self.pool = pool
        self.clock = clock
        self.client_factory = client_factory or default_client
        self.data_factory = data_factory or stock_bars.StockFeed.default_client
        self.client: Any = None
        self.data: Any = None
        self.data_loaded = False
        self.config_error: str | None = None
        self.loaded = False
        self.executor = StockExecutor()
        self.reconciler = stock_broker.Reconciler()
        self.marker = stock_marks.Marker()
        self.splits = stock_splits.SplitWatcher()
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

    def get_data(self) -> Any:
        """The market data client (corporate actions), None without keys."""
        if not self.data_loaded:
            self.data_loaded = True
            try:
                self.data = self.data_factory()
            except alpaca_credentials.AlpacaConfigError:
                self.data = None
        return self.data

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
        _commit(conn)  # the broker row stands even when the reconciliation fails
        try:
            splits = self.splits.run(conn, self.get_data(), now)
        except Exception as exc:  # noqa: BLE001 - retried next poll, reported as the task's error
            _rollback(conn)
            log.exception("stock splits failed")
            splits = {"applied": [], "pending": set(), "error": f"applying splits: {exc}"[:300]}
        _commit(conn)
        try:
            recon = self.reconciler.run(conn, client, now, skip=frozenset(splits["pending"]))
        except Exception as exc:  # noqa: BLE001 - shown on /stocks, retried next poll
            _rollback(conn)
            log.warning("stock reconciliation failed: %s", exc)
            error = f"reconciliation failed: {exc}"[:500]
            conn.execute("UPDATE stock_broker_state SET last_error = %s WHERE id = 1", (error,))
            return {"error": error}
        out = {"checked_at": now, "market_open": result["market_open"], **recon,
               "splits_applied": [s["symbol"] for s in splits["applied"]]}
        if splits["error"]:
            out["error"] = f"splits: {splits['error']}"
        return out

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
