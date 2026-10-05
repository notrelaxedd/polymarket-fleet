"""Feed lag: how far each game-state source is behind (or ahead of) the market
(docs/INGAME.md, "Feed latency").

A score change or a possession change seen first by a source becomes one `feed_lag`
row (game_id, event_kind, event_key, event_ts, source, feed_seen_at). Events are read
from the situation rows of a source (play_id NULL), compared with the source's previous
situation, so a play list that arrives late cannot fake a change. A score event needs
the score to grow (a corrected score is not an event); a possession event needs both
sides known (a halftime gap is not one).

Later passes fill `market_moved_at`: the first price snapshot, for either confirmed
market of the game, at or after (event_ts or feed_seen_at) - 120 s whose mid differs
by more than 0.03 from that market's last mid before the window start; `lag_s =
feed_seen_at - market_moved_at` (positive: the feed is behind the market). A row is
retried for 15 minutes, then left without a market move.

`lag_status` reads the last 20 measured rows: in-game buying is suspended while their
median lag exceeds `settings.ingame_max_lag_s`, and only once at least
`settings.ingame_lag_min_events` (default 5) rows are measured. Fewer rows are "not
enough data" (`enough_data` is false): reported, never a suspension.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta
from typing import Any

import psycopg

from host.settings import get_setting

log = logging.getLogger(__name__)

WINDOW_BEFORE = timedelta(seconds=120)
MOVE_THRESHOLD = 0.03
RETRY_FOR = timedelta(minutes=15)
LAST_N = 20
DEFAULT_MAX_LAG_S = 20.0
DEFAULT_MIN_EVENTS = 5


def baseline(conn: psycopg.Connection, game_id: str, source: str) -> dict[str, Any] | None:
    """The source's previous situation: {"home_score", "away_score", "possession"
    (the last one known)}; None before the source's first situation row."""
    row = conn.execute(
        """
        SELECT home_score, away_score,
               (SELECT possession FROM game_state p
                 WHERE p.game_id = s.game_id AND p.source = s.source AND p.play_id IS NULL AND p.possession IS NOT NULL
                 ORDER BY p.ts DESC, p.id DESC LIMIT 1) AS possession
          FROM game_state s
         WHERE s.game_id = %s AND s.source = %s AND s.play_id IS NULL
         ORDER BY s.ts DESC, s.id DESC LIMIT 1
        """,
        (game_id, source),
    ).fetchone()
    return None if row is None else dict(row)


def detect(base: dict[str, Any] | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The events in situation `rows` (oldest first) after `base`: dicts with "kind",
    "home_score", "away_score", "period", "possession", "event_ts"."""
    if base is None:
        return []
    home, away, possession = base.get("home_score"), base.get("away_score"), base.get("possession")
    events: list[dict[str, Any]] = []
    for row in rows:
        h, a = row.get("home_score"), row.get("away_score")
        if None not in (h, a, home, away) and h >= home and a >= away and (h, a) != (home, away):
            events.append({**_event(row), "kind": "score"})
        if None not in (h, a):
            home, away = h, a
        new = row.get("possession")
        if new is not None and possession is not None and new != possession:
            events.append({**_event(row), "kind": "possession"})
        possession = new if new is not None else possession
    return events


def _event(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in ("home_score", "away_score", "period", "possession", "event_ts")}


def event_key(conn: psycopg.Connection, game_id: str, source: str, event: dict[str, Any]) -> str:
    """"score:H-A" (scores only grow, so it is unique), or "possession:P:side:H-A:k"
    with k counting the earlier changes of the same period, side and score."""
    score = f"{event['home_score']}-{event['away_score']}"
    if event["kind"] == "score":
        return f"score:{score}"
    prefix = f"possession:{event['period']}:{event['possession']}:{score}:"
    row = conn.execute(
        "SELECT count(*) AS n FROM feed_lag WHERE game_id = %s AND source = %s AND event_key LIKE %s",
        (game_id, source, prefix + "%"),
    ).fetchone()
    return prefix + str(row["n"])


def record_events(conn: psycopg.Connection, game_id: str, source: str, base: dict[str, Any] | None,
                  rows: list[dict[str, Any]], seen_at: datetime) -> int:
    """Insert the feed_lag rows for the events in the new situation rows; how many."""
    stored = 0
    for event in detect(base, rows):
        key = event_key(conn, game_id, source, event)
        done = conn.execute(
            """
            INSERT INTO feed_lag (game_id, event_kind, event_key, event_ts, source, feed_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (game_id, event_key, source) DO NOTHING RETURNING id
            """,
            (game_id, event["kind"], key, event["event_ts"], source, seen_at),
        ).fetchone()
        stored += done is not None
    return stored


def market_moved_at(conn: psycopg.Connection, game_id: str, anchor: datetime, now: datetime) -> datetime | None:
    """The first snapshot at or after `anchor` (up to `now`), for either confirmed market
    of the game, whose mid differs by more than 0.03 from the market's last mid before
    `anchor`; None when no market has moved (or has no mid before the window)."""
    row = conn.execute(
        """
        SELECT min(moved.ts) AS ts
          FROM markets m
          CROSS JOIN LATERAL (
                SELECT b.mid FROM price_snapshots b
                 WHERE b.market_id = m.id AND b.ts < %(anchor)s AND b.mid IS NOT NULL
                 ORDER BY b.ts DESC, b.id DESC LIMIT 1) base
          CROSS JOIN LATERAL (
                SELECT s.ts FROM price_snapshots s
                 WHERE s.market_id = m.id AND s.ts >= %(anchor)s AND s.ts <= %(now)s AND s.mid IS NOT NULL
                   AND abs(s.mid - base.mid) > %(threshold)s
                 ORDER BY s.ts, s.id LIMIT 1) moved
         WHERE m.game_id = %(game)s AND m.mapping_confirmed
        """,
        {"anchor": anchor, "now": now, "threshold": MOVE_THRESHOLD, "game": game_id},
    ).fetchone()
    return None if row is None else row["ts"]


def fill_market_moves(conn: psycopg.Connection, now: datetime) -> int:
    """Fill market_moved_at and lag_s of the unmeasured rows seen in the last 15
    minutes; how many were filled."""
    rows = conn.execute(
        """
        SELECT id, game_id, event_ts, feed_seen_at FROM feed_lag
         WHERE market_moved_at IS NULL AND feed_seen_at >= %s ORDER BY feed_seen_at
        """,
        (now - RETRY_FOR,),
    ).fetchall()
    filled = 0
    for row in rows:
        anchor = (row["event_ts"] or row["feed_seen_at"]) - WINDOW_BEFORE
        moved = market_moved_at(conn, row["game_id"], anchor, now)
        if moved is None:
            continue
        lag = (row["feed_seen_at"] - moved).total_seconds()
        conn.execute("UPDATE feed_lag SET market_moved_at = %s, lag_s = %s WHERE id = %s", (moved, lag, row["id"]))
        filled += 1
    return filled


def max_lag_s(conn: psycopg.Connection) -> float:
    value = get_setting(conn, "ingame_max_lag_s", DEFAULT_MAX_LAG_S)
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_LAG_S


def min_events(conn: psycopg.Connection) -> int:
    """settings.ingame_lag_min_events: measured events needed before the lag can suspend."""
    value = get_setting(conn, "ingame_lag_min_events", DEFAULT_MIN_EVENTS)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_MIN_EVENTS


def enough_data(summary: dict[str, Any], needed: int) -> bool:
    """True when a lag summary rests on at least `needed` measured events (else the
    dashboard says "not enough data")."""
    return int(summary.get("n") or 0) >= needed


def _summary(lags: list[float], limit: float, needed: int) -> dict[str, Any]:
    median = float(statistics.median(lags)) if lags else None
    suspended = median is not None and len(lags) >= needed and median > limit
    return {"suspended": suspended, "median_lag_s": median, "n": len(lags)}


def _lags(conn: psycopg.Connection, source: str | None) -> list[float]:
    rows = conn.execute(
        """
        SELECT lag_s FROM feed_lag
         WHERE market_moved_at IS NOT NULL AND lag_s IS NOT NULL AND (%(source)s::text IS NULL OR source = %(source)s)
         ORDER BY feed_seen_at DESC, id DESC LIMIT %(n)s
        """,
        {"source": source, "n": LAST_N},
    ).fetchall()
    return [float(r["lag_s"]) for r in rows]


def lag_status(conn: psycopg.Connection, source: str | None = None) -> dict[str, Any]:
    """{"suspended", "median_lag_s", "n", "by_source": {source: {"suspended",
    "median_lag_s", "n"}}} over the last 20 measured rows (of `source` when given).
    Suspended only with at least ingame_lag_min_events measured rows."""
    limit, needed = max_lag_s(conn), min_events(conn)
    out = _summary(_lags(conn, source), limit, needed)
    names = conn.execute(
        """
        SELECT DISTINCT source FROM feed_lag
         WHERE market_moved_at IS NOT NULL AND (%(source)s::text IS NULL OR source = %(source)s) ORDER BY source
        """,
        {"source": source},
    ).fetchall()
    out["by_source"] = {r["source"]: _summary(_lags(conn, r["source"]), limit, needed) for r in names}
    return out
