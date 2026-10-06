"""The workloads step of the background loop (host/loop.py calls run_once after its own steps)."""
from __future__ import annotations

import logging
from typing import Any, Callable

import psycopg
from psycopg_pool import ConnectionPool

from host.workloads import assign, machines, outbound, pinning, queue
from host.workloads.senders import default_senders

log = logging.getLogger(__name__)
LOG_LINES_PER_MACHINE = 5000
LOG_MAX_AGE_DAYS = 3


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
        ("outbound_send", lambda conn: outbound.send_approved(conn, default_senders(conn))),
        ("log_prune", prune_logs),
    ]


def run_once(pool: ConnectionPool) -> None:
    """One pass: job reaper, pins, worker links, drains, outbound, log pruning.

    Each step runs in its own transaction and a failure is logged without stopping the rest.
    """
    for name, step in _steps():
        try:
            with pool.connection() as conn:
                step(conn)
        except Exception:  # noqa: BLE001 - one failing step must not stop the others
            log.exception("workloads loop step %s failed", name)
