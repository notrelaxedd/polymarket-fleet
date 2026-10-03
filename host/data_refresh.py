"""Periodic nflverse refresh: at startup when `games` is empty, then every
`nflverse_refresh_hours`. Runs in its own thread so a slow download never delays
the reaper and dispatcher loop; a failure is logged and retried after RETRY_SECONDS.

STATUS remembers the last outcome (success time and counts, or the error) of any
refresh in this process, periodic or owner-triggered, for the Settings page.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from host import nflverse
from host.settings import get_int_setting, get_setting

log = logging.getLogger(__name__)

CHECK_SECONDS = 60.0
RETRY_SECONDS = 15 * 60


@dataclass
class RefreshStatus:
    """The last refresh outcome of this host process (thread safe)."""

    last_success_at: datetime | None = None
    last_result: dict[str, Any] | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def succeeded(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.last_success_at = datetime.now(timezone.utc)
            self.last_result = dict(result)
            self.last_error = None
            self.last_error_at = None

    def failed(self, error: str) -> None:
        with self._lock:
            self.last_error = error
            self.last_error_at = datetime.now(timezone.utc)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"last_success_at": self.last_success_at, "last_result": self.last_result,
                    "last_error": self.last_error, "last_error_at": self.last_error_at}

    def reset(self) -> None:
        with self._lock:
            self.last_success_at = self.last_result = self.last_error = self.last_error_at = None


STATUS = RefreshStatus()


def refresh_now(conn: psycopg.Connection, url: str) -> dict[str, Any]:
    """An owner-triggered refresh: commit the request's open transaction first so the
    download (up to 60 s) runs outside it, then fetch and ingest; STATUS records the
    outcome either way and the exception (Upstream, BadRequest) is re-raised."""
    conn.commit()
    try:
        result = nflverse.ingest_text(conn, nflverse.fetch(url), url)
    except Exception as exc:  # noqa: BLE001 - recorded for the Settings page, then re-raised
        STATUS.failed(getattr(exc, "message", None) or str(exc))
        raise
    STATUS.succeeded(result)
    return result


class DataRefresher:
    """Decides when the next fetch is due and performs it (one object per host)."""

    def __init__(self, pool: ConnectionPool, clock: Any = time.monotonic) -> None:
        self.pool = pool
        self.clock = clock
        self.next_at: float | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None

    def _interval(self) -> float:
        with self.pool.connection() as conn:
            return max(1, get_int_setting(conn, "nflverse_refresh_hours", 6)) * 3600.0

    def due(self) -> bool:
        """True when a fetch should run now (first call: only when games is empty)."""
        now = self.clock()
        if self.next_at is None:
            with self.pool.connection() as conn:
                empty = nflverse.games_count(conn) == 0
            self.next_at = now if empty else now + self._interval()
        return now >= self.next_at

    def refresh(self) -> dict[str, Any]:
        """Download outside any transaction, then upsert; reschedules either way."""
        with self.pool.connection() as conn:
            url = str(get_setting(conn, "nflverse_url", nflverse.DEFAULT_URL))
        try:
            text = nflverse.fetch(url)
            with self.pool.connection() as conn:
                result = nflverse.ingest_text(conn, text, url)
        except Exception as exc:  # noqa: BLE001 - any failure is logged and retried
            self.last_error = getattr(exc, "message", None) or str(exc)
            STATUS.failed(self.last_error)
            self.next_at = self.clock() + RETRY_SECONDS
            log.warning("nflverse refresh failed (%s); retry in %d s", self.last_error, RETRY_SECONDS)
            return {"error": self.last_error}
        self.last_error = None
        self.last_result = result
        STATUS.succeeded(result)
        self.next_at = self.clock() + self._interval()
        log.info("nflverse refresh: %d rows, %d inserted, %d changed, %d skipped",
                 result["rows"], result["inserted"], result["changed"], result["skipped"])
        return result

    def tick(self) -> dict[str, Any] | None:
        """One pass: refresh when due."""
        return self.refresh() if self.due() else None


class DataRefreshThread(threading.Thread):
    """Calls DataRefresher.tick every CHECK_SECONDS until stopped; never dies on errors."""

    def __init__(self, pool: ConnectionPool, interval: float = CHECK_SECONDS) -> None:
        super().__init__(name="fleet-data", daemon=True)
        self.refresher = DataRefresher(pool)
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.refresher.tick()
            except Exception:  # noqa: BLE001 - keep the thread alive
                log.exception("nflverse refresh pass failed")
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()
