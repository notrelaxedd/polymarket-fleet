"""Periodic nflverse refresh: games at startup when `games` is empty, then every
`nflverse_refresh_hours`; injuries and play-by-play (docs/ROBUSTNESS.md B2) of the
current and the previous season every `signals_refresh_hours`, first STARTUP_DELAY
seconds after start when either table is empty. Runs in its own thread so a slow
download never delays the reaper and dispatcher loop; a failure is logged and retried
after RETRY_SECONDS. A full backfill of every season is the CLI's job
(`ingest-injuries --season all`, `ingest-pbp --season all`). Step 6 Part C: the same
thread refreshes the in-game training rows (pbp_rows) of the current season weekly in
season (host/pbp_refresh.py).

STATUS remembers the last outcome (success time and counts, or the error) of any
games refresh in this process, periodic or owner-triggered, for the Settings page;
SIGNALS_STATUS does the same for the injuries and play-by-play refresh.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import psycopg
from psycopg_pool import ConnectionPool

from host import ingest_injuries, ingest_pbp, nflverse
from host.pbp_refresh import PbpRowsRefresher
from host.errors import BadRequest, Upstream
from host.settings import get_int_setting, get_setting

log = logging.getLogger(__name__)

CHECK_SECONDS = 60.0
RETRY_SECONDS = 15 * 60
STARTUP_DELAY = 5 * 60
SIGNAL_KINDS = ("injuries", "pbp")
FIRST_SEASON = {"injuries": ingest_injuries.FIRST_SEASON, "pbp": ingest_pbp.FIRST_SEASON}
URL_SETTING = {"injuries": "nflverse_injuries_url", "pbp": "nflverse_pbp_url"}
DEFAULT_URL = {"injuries": ingest_injuries.DEFAULT_URL, "pbp": ingest_pbp.DEFAULT_URL}
Connect = Callable[[], AbstractContextManager[psycopg.Connection]]


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
SIGNALS_STATUS = RefreshStatus()


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


def current_season(conn: psycopg.Connection, today: datetime | None = None) -> int:
    """The newest season with a game kicking off within 30 days of today; without
    games, the calendar (a season starts in September, so Jan-Feb belong to last year)."""
    now = today or datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT max(season) AS s FROM games WHERE kickoff_at <= %s + interval '30 days'", (now,)
    ).fetchone()
    if row["s"] is not None:
        return int(row["s"])
    return now.year if now.month >= 3 else now.year - 1


def signal_template(conn: psycopg.Connection, kind: str) -> str:
    """The download URL template (with {season}) of one kind from settings."""
    return str(get_setting(conn, URL_SETTING[kind], DEFAULT_URL[kind]))


def ingest_season(connect: Connect, kind: str, template: str, season: int) -> dict[str, Any]:
    """Download one season of `kind` outside any transaction, then upsert it in its own
    connection (committed when the block exits)."""
    url = template.replace("{season}", str(int(season)))
    if kind == "injuries":
        text = nflverse.fetch(url, max_bytes=ingest_injuries.MAX_BYTES)
        with connect() as conn:
            return ingest_injuries.ingest_text(conn, text, url)
    agg = ingest_pbp.aggregate_url(url)
    with connect() as conn:
        return ingest_pbp.ingest_aggregate(conn, agg, url)


def ingest_seasons(connect: Connect, kinds: tuple[str, ...], seasons: list[int],
                   templates: dict[str, str]) -> dict[str, Any]:
    """Every (kind, season) in order; one failure is recorded and the rest still run.
    {"results": {"injuries:2024": counts, ...}, "errors": {"pbp:2025": message, ...}}."""
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for kind in kinds:
        for season in seasons:
            key = f"{kind}:{season}"
            try:
                result = ingest_season(connect, kind, templates[kind], season)
            except Exception as exc:  # noqa: BLE001 - one season's failure never stops the rest
                errors[key] = getattr(exc, "message", None) or str(exc)
                log.warning("%s refresh of %d failed: %s", kind, season, errors[key])
                continue
            results[key] = {k: result[k] for k in ("rows", "inserted", "changed", "skipped")}
    return {"results": results, "errors": errors}


class SignalsRefresher:
    """Refreshes injuries and play-by-play of the current and previous season every
    signals_refresh_hours (one object per host)."""

    def __init__(self, pool: ConnectionPool, clock: Any = time.monotonic) -> None:
        self.pool = pool
        self.clock = clock
        self.next_at: float | None = None
        self.last_result: dict[str, Any] | None = None

    def _interval(self) -> float:
        with self.pool.connection() as conn:
            return max(1, get_int_setting(conn, "signals_refresh_hours", 24)) * 3600.0

    def due(self) -> bool:
        """True when a refresh should run now (first call: STARTUP_DELAY away when a
        table is empty, else a full interval away)."""
        now = self.clock()
        if self.next_at is None:
            with self.pool.connection() as conn:
                empty = conn.execute(
                    "SELECT NOT EXISTS (SELECT 1 FROM injuries) OR NOT EXISTS (SELECT 1 FROM team_game_stats) AS e"
                ).fetchone()["e"]
            self.next_at = now + (STARTUP_DELAY if empty else self._interval())
        return now >= self.next_at

    def refresh(self) -> dict[str, Any]:
        """Both kinds for the current and the previous season; reschedules either way
        (RETRY_SECONDS when nothing succeeded)."""
        with self.pool.connection() as conn:
            season = current_season(conn)
            templates = {kind: signal_template(conn, kind) for kind in SIGNAL_KINDS}
        result = ingest_seasons(self.pool.connection, SIGNAL_KINDS, [season - 1, season], templates)
        self.last_result = result
        if result["errors"] and not result["results"]:
            SIGNALS_STATUS.failed("; ".join(f"{k}: {v}" for k, v in result["errors"].items()))
            self.next_at = self.clock() + RETRY_SECONDS
        else:
            SIGNALS_STATUS.succeeded(result)
            self.next_at = self.clock() + self._interval()
        log.info("signals refresh: %d loaded, %d failed", len(result["results"]), len(result["errors"]))
        return result

    def tick(self) -> dict[str, Any] | None:
        return self.refresh() if self.due() else None


class DataRefreshThread(threading.Thread):
    """Ticks the games and the signals refreshers every CHECK_SECONDS until stopped;
    never dies on errors."""

    def __init__(self, pool: ConnectionPool, interval: float = CHECK_SECONDS) -> None:
        super().__init__(name="fleet-data", daemon=True)
        self.refresher = DataRefresher(pool)
        self.signals = SignalsRefresher(pool)
        self.pbp_rows = PbpRowsRefresher(pool.connection)
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            for refresher in (self.refresher, self.signals, self.pbp_rows):
                try:
                    refresher.tick()
                except Exception:  # noqa: BLE001 - keep the thread alive
                    log.exception("nflverse refresh pass failed")
                if self._stop_event.is_set():
                    break
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()


def backfill(connect: Connect, kind: str, season: str | None, source: str | None = None) -> list[str]:
    """The CLI's ingest of one kind: a local file, one season, or every season from
    the kind's first through the current one (`all`). One line per season; BadRequest
    when nothing was asked, Upstream when any season failed (after the others ran)."""
    if source:
        module = ingest_injuries if kind == "injuries" else ingest_pbp
        with connect() as conn:
            result = module.ingest(conn, None, source)
        return [f"{kind} {source}: {result['rows']} rows, {result['inserted']} inserted, {result['changed']} changed"]
    with connect() as conn:
        template = signal_template(conn, kind)
        current = current_season(conn)
    if season == "all":
        seasons = list(range(FIRST_SEASON[kind], current + 1))
    elif season and season.strip().isdigit():
        seasons = [int(season)]
    else:
        raise BadRequest("give --season <year>, --season all or --file")
    out = ingest_seasons(connect, (kind,), seasons, {kind: template})
    lines = [f"{key}: {r['rows']} rows, {r['inserted']} inserted, {r['changed']} changed, {r['skipped']} skipped"
             for key, r in out["results"].items()]
    lines += [f"{key}: failed: {message}" for key, message in out["errors"].items()]
    if out["errors"]:
        raise Upstream("\n".join(lines))
    return lines
