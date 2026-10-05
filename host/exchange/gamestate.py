"""The live game-state feed (docs/INGAME.md, "Live game state" and "Feed latency").

The exchange task "gamestate" (every second) polls the ESPN summary
(`settings.espn_summary_url`, `{event_id}` = games.raw->>'espn') of every live
assigned game every max(gamestate_poll_s, n_live / gamestate_max_rps) seconds. One
sliding window and one jittered 429/403 backoff (15 s doubling to 300 s, held by
PollerState in host/exchange/gamestate_rate.py) cover every ESPN request: summaries,
the scoreboard fallback for a summary not understood, and the scores task's
scoreboard (scores_fetch). Requests are stamped as they leave. New plays are stored
once (by play id per source) and the current situation on every successful
observation, so the newest row is the current situation and its age is the time since
the last good poll. Score and possession changes go to feed_lag
(host/exchange/feedlag.py). Yahoo is opt-in and not fetched until its parser exists.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from host.exchange import feedlag
from host.exchange.adapters.base import http_get, truncate, utcnow
from host.exchange.gamestate_parse import (  # noqa: F401  (re-exported: the public API lives here)
    STATE_KEYS,
    parse_scoreboard_states,
    parse_summary,
    parse_summary_rows,
    raw_fragment,
)
from host.exchange.gamestate_rate import (  # noqa: F401  (re-exported: the public API lives here)
    BACKOFF_MAX_S,
    BACKOFF_START_S,
    Answer,
    Fetch,
    PollerState,
)
from host.exchange.scores import DEFAULT_URL as SCOREBOARD_URL, Deferred
from host.settings import get_setting

__all__ = ["PollerState", "lag_status", "latest_state", "parse_scoreboard_states", "parse_summary", "poll", "scores_fetch"]

log = logging.getLogger(__name__)

DEFAULT_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={event_id}"
POLL_RANGE = (3.0, 5.0)
RPS_RANGE = (0.05, 10.0)
LIVE_WINDOW = timedelta(hours=8)
FETCH_TIMEOUT_S = 3.0
BOARD_FRESH_S = 5.0
lag_status = feedlag.lag_status
DEFAULT_STATE = PollerState()
DEFAULT_RNG = random.Random("gamestate")


def default_fetch(url: str) -> tuple[int, str]:
    return http_get(url, FETCH_TIMEOUT_S)


def _float(conn: psycopg.Connection, key: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(get_setting(conn, key, default))
    except (TypeError, ValueError):
        value = default
    return min(hi, max(lo, value)) if value == value else default


def cadence(conn: psycopg.Connection, n_live: int) -> tuple[float, float]:
    """(per-game interval, max requests per second)."""
    poll_s = _float(conn, "gamestate_poll_s", 4.0, *POLL_RANGE)
    rps = _float(conn, "gamestate_max_rps", 1.0, *RPS_RANGE)
    return max(poll_s, n_live / rps), rps


def live_games(conn: psycopg.Connection, now: datetime) -> list[dict[str, Any]]:
    """Games to poll: an active or halted assignment, kicked off within 8 hours, not
    final in games nor in their newest game_state since kickoff (a game postponed and
    rescheduled under the same event id is polled again), with an ESPN event id."""
    rows = conn.execute(
        """
        SELECT g.game_id, g.raw->>'espn' AS espn FROM games g
         WHERE g.status <> 'final' AND g.kickoff_at <= %s AND g.kickoff_at >= %s
           AND coalesce(g.raw->>'espn', '') <> ''
           AND EXISTS (SELECT 1 FROM assignments a WHERE a.game_id = g.game_id AND a.status IN ('active', 'halted'))
           AND coalesce((SELECT s.status FROM game_state s WHERE s.game_id = g.game_id AND s.ts >= g.kickoff_at
                          ORDER BY s.ts DESC, s.id DESC LIMIT 1), '') <> 'final'
         ORDER BY g.kickoff_at, g.game_id
        """,
        (now, now - LIVE_WINDOW),
    ).fetchall()
    return [dict(r) for r in rows]


def _state(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in STATE_KEYS}


def _insert(conn: psycopg.Connection, game_id: str, source: str, row: dict[str, Any], now: datetime) -> bool:
    cols = ("game_id", "ts", "source", "event_ts", *STATE_KEYS, "play_id", "play_text", "raw")
    values = (game_id, now, source, row.get("event_ts"), *(row.get(k) for k in STATE_KEYS),
              row.get("play_id"), row.get("play_text"), Jsonb(raw_fragment(row.get("raw"))))
    done = conn.execute(
        f"INSERT INTO game_state ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
        " ON CONFLICT (game_id, source, play_id) WHERE play_id IS NOT NULL DO NOTHING RETURNING id",
        values,
    ).fetchone()
    return done is not None


def store(conn: psycopg.Connection, game_id: str, source: str, rows: list[dict[str, Any]], now: datetime) -> int:
    """Insert the new plays and the current situation (one row per observation, so
    latest_state's age is the time since the last successful poll), record the feed
    events; how many rows were stored."""
    base = feedlag.baseline(conn, game_id, source)
    stored = [row for row in rows if _insert(conn, game_id, source, row, now)]
    feedlag.record_events(conn, game_id, source, base, [r for r in stored if r.get("play_id") is None], now)
    return len(stored)


def _store_safely(conn: psycopg.Connection, game_id: str, source: str, rows: list[dict[str, Any]], now: datetime,
                  out: dict[str, Any]) -> int:
    """store() inside a savepoint: a row the database refuses costs that game's
    observation, never the rest of the pass."""
    try:
        with conn.transaction():
            return store(conn, game_id, source, rows, now)
    except psycopg.Error as exc:
        message = f"{game_id}: {source} observation not stored: {truncate(str(exc), 300)}"
        log.warning("game state %s", message)
        out["errors"].append(message)
        return 0


def poll(conn: psycopg.Connection, now: datetime | None = None, fetch: Fetch | None = None,
         rng: random.Random | None = None, state: PollerState | None = None) -> dict[str, Any]:
    """One pass: {"polled", "rows", "backoff_until" (iso or None), "errors"}. A pending
    scoreboard request (a summary not understood, or the scores task waiting) goes
    first, then the due summaries, the game polled longest ago first."""
    now = now or utcnow()
    fetch, rng, state = fetch or default_fetch, rng or DEFAULT_RNG, state or DEFAULT_STATE
    now_ts = now.timestamp()
    out: dict[str, Any] = {"polled": 0, "rows": 0, "backoff_until": None, "errors": []}
    _note_yahoo(conn, state, out)
    games = live_games(conn, now)
    interval, rps = cadence(conn, len(games))
    live = {g["game_id"]: g for g in games}
    state.pending_fallback = {k: v for k, v in state.pending_fallback.items() if k in live}
    board_url = str(get_setting(conn, "scores_url", SCOREBOARD_URL) or SCOREBOARD_URL)
    asked = _board_due(state, now_ts, interval) and _scoreboard(conn, board_url, now, fetch, rng, state, rps, out)
    template = str(get_setting(conn, "espn_summary_url", DEFAULT_SUMMARY_URL) or DEFAULT_SUMMARY_URL)
    due = [g for g in games if now_ts - state.last_poll.get(g["game_id"], float("-inf")) >= interval]
    if due and "{event_id}" not in template:
        out["errors"].append("espn_summary_url has no {event_id} placeholder: not polled")
        due = []
    due.sort(key=lambda g: state.last_poll.get(g["game_id"], float("-inf")))
    for game in due:
        answer = state.send(fetch, template.replace("{event_id}", str(game["espn"])), now_ts, rps, rng)
        if answer is None:
            break
        state.last_poll[game["game_id"]] = now_ts
        out["polled"] += 1
        if not _answer_ok(answer, out, game["game_id"]):
            continue
        rows = parse_summary_rows(answer[1])
        if not rows:
            out["errors"].append(f"{game['game_id']}: summary payload not understood")
            state.pending_fallback[game["game_id"]] = game
            continue
        state.pending_fallback.pop(game["game_id"], None)
        out["rows"] += _store_safely(conn, game["game_id"], "espn_summary", rows, now, out)
    if not asked and _board_due(state, now_ts, interval):
        _scoreboard(conn, board_url, now, fetch, rng, state, rps, out)
    feedlag.fill_market_moves(conn, now)
    until = state.backoff_until.get("espn", 0.0)
    if until > state.stamp(now_ts):
        out["backoff_until"] = datetime.fromtimestamp(until, tz=now.tzinfo).isoformat()
    return out


def _answer_ok(answer: Answer, out: dict[str, Any], what: str) -> bool:
    """True for a 200; otherwise log and report (send() already backed off a 429 or 403)."""
    status, _text, error, _ts = answer
    if status == 200:
        return True
    if status in (429, 403):
        message = f"{what}: espn answered {status}, backing off"
    else:
        message = f"{what}: {error}" if error else f"{what}: espn answered {status}"
    log.warning("game state %s", message)
    out["errors"].append(message)
    return False


def _board_due(state: PollerState, now_ts: float, interval: float) -> bool:
    """The scores task waits for a scoreboard, or a summary was not understood and the
    last scoreboard request is at least one per-game interval old (no tight retry)."""
    return state.board_wanted or (bool(state.pending_fallback) and now_ts - state.last_board >= interval)


def _scoreboard(conn: psycopg.Connection, url: str, now: datetime, fetch: Fetch, rng: random.Random,
                state: PollerState, rps: float, out: dict[str, Any]) -> bool:
    """One scoreboard request for the games whose summary was not understood (and for
    the scores task, through state.board); whether it went out."""
    answer = state.ask_board(fetch, url, now.timestamp(), rps, rng)
    if answer is None:
        return False
    state.last_board = now.timestamp()
    out["polled"] += 1
    if not _answer_ok(answer, out, "scoreboard"):
        return True
    states = parse_scoreboard_states(answer[1])
    games, state.pending_fallback = state.pending_fallback, {}
    for game in games.values():
        found = states.get(str(game["espn"]))
        if found is not None:
            row = {**found, "play_id": None, "play_text": None, "event_ts": None, "raw": {"scoreboard": game["espn"]}}
            out["rows"] += _store_safely(conn, game["game_id"], "espn_scoreboard", [row], now, out)
    return True


def scores_fetch(conn: psycopg.Connection, state: PollerState, now: datetime, fetch: Fetch | None = None,
                 rng: random.Random | None = None) -> Callable[[str], str]:
    """The scores task's fetch, sharing the feed's window and backoff: the scoreboard
    answer of the last BOARD_FRESH_S seconds, else one request when the window and the
    backoff allow. Raises scores.Deferred when nothing may go out now (the feed's next
    pass then asks the scoreboard before any summary) and RuntimeError on a non-200."""
    fetch, rng, now_ts = fetch or default_fetch, rng or DEFAULT_RNG, now.timestamp()

    def get(url: str) -> str:
        fresh = state.board is not None and state.stamp(now_ts) - state.board[0] <= BOARD_FRESH_S
        if not fresh and state.ask_board(fetch, url, now_ts, cadence(conn, 0)[1], rng) is None:
            state.board_wanted = True
            raise Deferred("espn is backing off or its rate window is full; the feed asks the scoreboard next")
        _ts, status, text = state.board  # type: ignore[misc]
        if status != 200:
            raise RuntimeError(f"scoreboard: {text}")
        return text

    return get


def _note_yahoo(conn: psycopg.Connection, state: PollerState, out: dict[str, Any]) -> None:
    sources = get_setting(conn, "gamestate_sources", ["espn"])
    if isinstance(sources, list) and "yahoo" in sources and str(get_setting(conn, "yahoo_pbp_url", "") or ""):
        message = "yahoo is listed but its parser awaits the owner's probe (probe-gamestate --yahoo); not polled"
        out["errors"].append(message)
        if not state.yahoo_noted:
            log.warning(message)
            state.yahoo_noted = True


def latest_state(conn: psycopg.Connection, game_id: str, now: datetime | None = None) -> dict[str, Any] | None:
    """The newest row as {"state", "ts", "age_s", "source", "last_change": {"kind",
    "ts"} | None}; last_change is the newest feed event, at the time a source first saw it."""
    row = conn.execute(
        "SELECT * FROM game_state WHERE game_id = %s ORDER BY ts DESC, id DESC LIMIT 1", (game_id,)
    ).fetchone()
    if row is None:
        return None
    now = now or utcnow()
    change = conn.execute(
        """
        SELECT f.event_kind AS kind,
               (SELECT min(o.feed_seen_at) FROM feed_lag o WHERE o.game_id = f.game_id AND o.event_key = f.event_key) AS ts
          FROM feed_lag f WHERE f.game_id = %s ORDER BY f.feed_seen_at DESC, f.id DESC LIMIT 1
        """,
        (game_id,),
    ).fetchone()
    return {
        "state": _state(dict(row)),
        "ts": row["ts"],
        "age_s": round((now - row["ts"]).total_seconds(), 3),
        "source": row["source"],
        "last_change": None if change is None else {"kind": change["kind"], "ts": change["ts"]},
    }
