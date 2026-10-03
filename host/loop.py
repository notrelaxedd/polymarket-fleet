"""The background reaper + dispatcher loop."""
from __future__ import annotations

import logging
import threading
from typing import Any

from psycopg_pool import ConnectionPool

from host import queue

log = logging.getLogger(__name__)


def run_once(pool: ConnectionPool) -> dict[str, Any]:
    """One reaper pass then one dispatcher pass, each in its own transaction."""
    with pool.connection() as conn:
        reaped = queue.reap(conn)
    with pool.connection() as conn:
        dispatched = queue.dispatch(conn)
    if reaped or dispatched:
        log.info("loop: reaped=%d dispatched=%d", len(reaped), len(dispatched))
    return {"reaped": len(reaped), "dispatched": len(dispatched)}


class LoopThread(threading.Thread):
    """Runs run_once every `interval` seconds until stopped; never dies on errors."""

    def __init__(self, pool: ConnectionPool, interval: float) -> None:
        super().__init__(name="fleet-loop", daemon=True)
        self.pool = pool
        self.interval = interval
        # Not named _stop: threading.Thread has a private _stop() that join() calls.
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                run_once(self.pool)
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("background loop iteration failed")
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        """Ask the thread to exit after the current iteration."""
        self._stop_event.set()
