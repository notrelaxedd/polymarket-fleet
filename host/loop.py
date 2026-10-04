"""The background reaper + dispatcher + orphan-order loop."""
from __future__ import annotations

import logging
import threading
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from host import queue
from host.settings import get_int_setting
from host.trading import orders

log = logging.getLogger(__name__)
ORPHAN_ACTOR = "orphan"


def cancel_orphan_orders(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """The orphan rule of docs/TRADING.md: a worker silent for `orphan_cancel_after_s`
    with active orders gets them cancelled the same way a release would (paper at
    once, live `cancel_requested`); its trade jobs follow the normal lease expiry."""
    after = get_int_setting(conn, "orphan_cancel_after_s", 30)
    rows = conn.execute(
        """
        SELECT o.id, o.worker_id FROM orders o
          JOIN workers w ON w.id = o.worker_id
         WHERE o.status IN ('approved', 'submitting', 'open', 'partial')
           AND (w.last_heartbeat_at IS NULL OR w.last_heartbeat_at < now() - make_interval(secs => %s))
         ORDER BY o.created_at
        """,
        (after,),
    ).fetchall()
    out = []
    for row in rows:
        status = orders.cancel_order(conn, row["id"], ORPHAN_ACTOR, "worker silent")
        out.append({"id": str(row["id"]), "worker_id": row["worker_id"], "status": status})
    return out


def run_once(pool: ConnectionPool) -> dict[str, Any]:
    """One reaper pass, one dispatcher pass, one orphan pass, each in its own transaction."""
    with pool.connection() as conn:
        reaped = queue.reap(conn)
    with pool.connection() as conn:
        dispatched = queue.dispatch(conn)
    with pool.connection() as conn:
        orphaned = cancel_orphan_orders(conn)
    if reaped or dispatched or orphaned:
        log.info("loop: reaped=%d dispatched=%d orphaned=%d", len(reaped), len(dispatched), len(orphaned))
    return {"reaped": len(reaped), "dispatched": len(dispatched), "orphaned": len(orphaned)}


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
