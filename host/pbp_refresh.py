"""Weekly in-season refresh of the in-game training rows (docs/INGAME.md, contract
section 3): host/pbp_rows.py streams the current season's nflverse play_by_play file
into `pbp_rows`, so the worker feed (GET /api/v1/data/pbp) carries last week's plays.

In season means a game kicking off within IN_SEASON_BEFORE days before now or
IN_SEASON_AFTER days after it (the bye week before the Super Bowl included). The first
refresh runs FIRST_DELAY after start when the season is on, then every WEEK; out of
season the check repeats every OFF_SEASON_RECHECK. A failure is logged, recorded in
PBP_STATUS and retried after RETRY_SECONDS. A backfill of earlier seasons is the CLI's
job (`ingest-pbp-rows --season 2012-2025`). Ticked by host.data_refresh's thread.
"""
from __future__ import annotations

import logging
import time
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import psycopg

from host import pbp_rows

log = logging.getLogger(__name__)

WEEK = 7 * 24 * 3600.0
FIRST_DELAY = 10 * 60.0
RETRY_SECONDS = 30 * 60.0
OFF_SEASON_RECHECK = 24 * 3600.0
IN_SEASON_BEFORE = timedelta(days=14)
IN_SEASON_AFTER = timedelta(days=7)
Connect = Callable[[], AbstractContextManager[psycopg.Connection]]
Ingest = Callable[[psycopg.Connection, int], dict[str, Any]]


def in_season(conn: psycopg.Connection, now: datetime | None = None) -> int | None:
    """The season of the newest game kicking off around now (see the module
    docstring), or None out of season."""
    now = now or datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT max(season) AS s FROM games WHERE kickoff_at BETWEEN %s AND %s",
        (now - IN_SEASON_BEFORE, now + IN_SEASON_AFTER),
    ).fetchone()
    return None if row is None or row["s"] is None else int(row["s"])


class PbpRowsRefresher:
    """Decides when the weekly ingest is due and performs it (one object per host)."""

    def __init__(self, connect: Connect, clock: Any = time.monotonic, ingest: Ingest | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self.connect = connect
        self.clock = clock
        self.ingest = ingest or (lambda conn, season: pbp_rows.ingest_season(conn, season))
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.next_at: float | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None

    def due(self) -> bool:
        """True when a pass should run now (the first pass FIRST_DELAY after start)."""
        now = self.clock()
        if self.next_at is None:
            self.next_at = now + FIRST_DELAY
        return now >= self.next_at

    def refresh(self) -> dict[str, Any]:
        """Ingest the current season when in season; reschedule either way."""
        with self.connect() as conn:
            season = in_season(conn, self.now())
        if season is None:
            self.next_at = self.clock() + OFF_SEASON_RECHECK
            return {"skipped": "off season"}
        try:
            with self.connect() as conn:
                result = self.ingest(conn, season)
        except Exception as exc:  # noqa: BLE001 - any failure is logged and retried
            self.last_error = getattr(exc, "message", None) or str(exc)
            self.next_at = self.clock() + RETRY_SECONDS
            log.warning("pbp_rows refresh of %d failed (%s); retry in %d s", season, self.last_error, RETRY_SECONDS)
            return {"season": season, "error": self.last_error}
        self.last_error = None
        self.last_result = {"season": season, **{k: result.get(k) for k in ("rows", "inserted", "changed", "games")}}
        self.next_at = self.clock() + WEEK
        log.info("pbp_rows refresh of %d: %s", season, self.last_result)
        return self.last_result

    def tick(self) -> dict[str, Any] | None:
        return self.refresh() if self.due() else None
