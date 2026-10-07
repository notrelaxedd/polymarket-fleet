"""Background threads of the workloads platform, apart from the Polymarket loop.

host/loop.py (reaper, dispatcher, orphan cancel) is untouched: these run in their own
threads with their own pool, so nothing here (an SMTP server that hangs, a slow query)
can delay a Polymarket loop pass. Outbound sending has a thread of its own as well, so a
slow send never delays pins or drains.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

import psycopg
from psycopg_pool import ConnectionPool

from host.workloads import assign, machines, outbound, pinning, queue
from host.workloads.senders import default_senders

log = logging.getLogger(__name__)
LOG_LINES_PER_MACHINE = 5000
LOG_MAX_AGE_DAYS = 3
SEND_BATCH = 5  # approved actions sent per sender pass


def prune_logs(conn: psycopg.Connection) -> int:
    """Delete machine_logs older than 3 days and all but the newest 5000 lines per machine."""
    old = conn.execute(
        "DELETE FROM machine_logs WHERE ts < now() - make_interval(days => %s) RETURNING id", (LOG_MAX_AGE_DAYS,)
    ).fetchall()
    extra = conn.execute(
        """
        DELETE FROM machine_logs WHERE id IN (
          SELECT id FROM (SELECT id, row_number() OVER (PARTITION BY machine_id ORDER BY id DESC) AS rn
                            FROM machine_logs) t WHERE rn > %s) RETURNING id
        """,
        (LOG_LINES_PER_MACHINE,),
    ).fetchall()
    return len(old) + len(extra)


def _steps() -> list[tuple[str, Callable[[psycopg.Connection], Any]]]:
    return [
        ("reap", queue.reap),
        ("pins", pinning.refresh_pins),
        ("links", machines.refresh_links),
        ("drains", assign.finish_drains),
        ("outbound_expire", outbound.expire_old),
        ("log_prune", prune_logs),
    ]


def run_once(pool: ConnectionPool) -> None:
    """One pass: job reaper, pins, worker links, drains, outbound expiry, log pruning.

    Each step runs in its own transaction and a failure is logged without stopping the rest.
    """
    for name, step in _steps():
        try:
            with pool.connection() as conn:
                step(conn)
        except Exception:  # noqa: BLE001 - one failing step must not stop the others
            log.exception("workloads loop step %s failed", name)


def send_once(pool: ConnectionPool, limit: int = SEND_BATCH) -> int:
    """Send at most `limit` approved outbound actions; returns how many were sent."""
    with pool.connection() as conn:
        return outbound.send_approved(conn, default_senders(conn), limit=limit)


class _Every(threading.Thread):
    """Calls `fn(pool)` every `interval` seconds until stopped; never dies on errors."""

    def __init__(self, name: str, fn: Callable[[ConnectionPool], Any], pool: ConnectionPool, interval: float) -> None:
        super().__init__(name=name, daemon=True)
        self.fn, self.pool, self.interval = fn, pool, interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.fn(self.pool)
            except Exception:  # noqa: BLE001 - keep the thread alive
                log.exception("%s iteration failed", self.name)
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()


def start_threads(pool: ConnectionPool, interval: float) -> list[_Every]:
    """Start the workloads loop and the outbound sender; host/main.py stops them on exit."""
    threads = [_Every("workloads-loop", run_once, pool, interval), _Every("outbound-sender", send_once, pool, interval)]
    for t in threads:
        t.start()
    return threads
