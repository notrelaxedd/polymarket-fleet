"""Regression tests for the step 6C feed review: one row per observation, the
scoreboard fallback at the default rate, one ESPN budget shared with the scores task,
payloads the database refuses, postponed games, rescheduled games and requests
stamped when they leave."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host.exchange import feedlag, gamestate, main
from host.exchange.gamestate import PollerState, latest_state
from host.exchange.gamestate_parse import parse_summary, raw_fragment, status_of
from tests.test_gamestate import FIXTURES, T0, Feed, fixture, live_game, run

BOARD = (FIXTURES / "espn_scoreboard.json").read_text()
GAME = "2026_05_KC_LV"


def max_per_window(times: list[float], window: float = 1.0) -> int:
    return max((sum(1 for u in times if t - window < u <= t) for t in times), default=0)


def summary_rows(conn: Any) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM game_state ORDER BY id").fetchall()


# ------------------------------------------------- one row per observation (medium)

def test_a_quiet_game_stays_fresh_while_the_feed_answers(conn):
    live_game(conn)
    feed, state = Feed(), PollerState()
    for tick in range(0, 61, 4):
        assert run(conn, feed, T0 + timedelta(seconds=tick), state)["errors"] == []
    assert len(feed.calls) == 16
    latest = latest_state(conn, GAME, T0 + timedelta(seconds=61))
    assert latest["age_s"] == 1.0, "age is the time since the last good poll, not since the last change"
    situations = conn.execute("SELECT count(*) AS n FROM game_state WHERE play_id IS NULL").fetchone()["n"]
    assert situations == 16, "one situation row per observation"
    assert conn.execute("SELECT count(*) AS n FROM feed_lag").fetchone()["n"] == 0, "an unchanged row is no event"
    feed.answer = (500, "down")
    for tick in range(64, 101, 4):
        run(conn, feed, T0 + timedelta(seconds=tick), state)
    assert latest_state(conn, GAME, T0 + timedelta(seconds=101))["age_s"] == 41.0, "a dead feed still goes stale"


# --------------------------------------------- scoreboard fallback at 1 rps (medium)

def test_unparseable_summary_reaches_the_scoreboard_at_the_default_rate(conn):
    live_game(conn, "2026_05_PHI_DAL", espn="401800002")
    broken = [True]
    feed = Feed(lambda url: BOARD if "scoreboard" in url else ("<html>maintenance</html>" if broken[0] else fixture("in")))
    state = PollerState()
    for tick in range(4 * 60):
        run(conn, feed, T0 + timedelta(seconds=tick * 0.25), state)
    board = [c[0] for c in feed.calls if "scoreboard" in c[1]]
    assert len(board) >= 10, "the scoreboard is asked about once per per-game interval"
    assert board[0] == T0 + timedelta(seconds=1), "the pending fallback goes out on the next free second"
    gaps = [(b - a).total_seconds() for a, b in zip(board, board[1:])]
    assert min(gaps) >= 4.0, "never more often than the per-game interval: no tight retry"
    assert max_per_window([c[0].timestamp() for c in feed.calls]) <= 1, "summary and scoreboard share one window"
    rows = conn.execute("SELECT count(*) AS n FROM game_state WHERE source = 'espn_scoreboard'").fetchone()["n"]
    assert rows == len(board), "every scoreboard observation is stored"
    latest = latest_state(conn, "2026_05_PHI_DAL", T0 + timedelta(seconds=60))
    assert latest["source"] == "espn_scoreboard" and latest["age_s"] <= 4.0
    broken[0] = False
    end = T0 + timedelta(seconds=60)
    for tick in range(4 * 20):
        run(conn, feed, end + timedelta(seconds=tick * 0.25), state)
    assert not state.pending_fallback and len([c for c in feed.calls if "scoreboard" in c[1]]) <= len(board) + 1, \
        "a summary understood again ends the fallback"


# -------------------------------------------- one ESPN budget with the scores task (low)

def loop_on(pool: Any, monkeypatch: Any, answer: Any) -> tuple[main.ExchangeLoop, list[datetime], list[tuple[float, str, float]]]:
    clock = [T0]
    calls: list[tuple[float, str, float]] = []
    loop = main.ExchangeLoop(pool, clock=lambda: clock[0])

    def espn(url: str) -> tuple[int, str]:
        calls.append((clock[0].timestamp(), url, loop.gamestate_poller.backoff_until.get("espn", 0.0)))
        return answer(url)

    monkeypatch.setattr(gamestate, "default_fetch", espn)
    return loop, clock, calls


def tick(loop: main.ExchangeLoop, now: datetime) -> None:
    for name in ("gamestate", "scores"):
        if loop.due(name, now):
            loop.run_task(name, now)


def test_scores_task_never_requests_while_espn_backs_off(pool, monkeypatch):
    with pool.connection() as c:
        live_game(c)
    loop, clock, calls = loop_on(pool, monkeypatch, lambda url: (429, "slow down"))
    for second in range(300):
        clock[0] = T0 + timedelta(seconds=second)
        tick(loop, clock[0])
    assert calls and all(t >= until for t, _url, until in calls), "no request goes out while backing off"
    assert max_per_window([t for t, _u, _b in calls]) <= 1
    assert any("scoreboard" in url for _t, url, _b in calls), "the scores task still gets its turn"
    assert len(calls) <= 8, "five minutes of 429s cost a handful of requests"
    assert loop.gamestate_poller.failures["espn"] == len(calls), "every 429, the scoreboard's too, extends the backoff"


def test_scores_task_waits_for_a_free_slot_and_shares_the_answer(pool, monkeypatch):
    with pool.connection() as c:
        live_game(c)
    summary = json.dumps(fixture("in"))
    loop, clock, calls = loop_on(pool, monkeypatch, lambda url: (200, BOARD if "scoreboard" in url else summary))
    results = []
    for step in range(4 * 6):
        clock[0] = T0 + timedelta(seconds=step * 0.25)
        tick(loop, clock[0])
        results.append(loop.errors.get("scores"))
    with pool.connection() as c:
        game = c.execute("SELECT status, home_score, away_score FROM games WHERE game_id = %s", (GAME,)).fetchone()
    assert (game["status"], game["home_score"], game["away_score"]) == ("final", 17, 27)
    board = [t for t, url, _b in calls if "scoreboard" in url]
    assert board == [T0.timestamp() + 1], "deferred at 0 (the summary had the slot), asked by the feed at 1"
    assert max_per_window([t for t, _u, _b in calls]) <= 1 and results == [None] * len(results), "deferred is no error"


def test_scores_task_alone_still_settles(pool, monkeypatch):
    with pool.connection() as c:
        live_game(c)
        c.execute("INSERT INTO game_state (game_id, ts, source, status) VALUES (%s, %s, 'espn_summary', 'final')",
                  (GAME, T0 - timedelta(minutes=1)))
    loop, clock, calls = loop_on(pool, monkeypatch, lambda url: (200, BOARD))
    tick(loop, T0)
    assert [url for _t, url, _b in calls] == ["https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"]
    with pool.connection() as c:
        assert c.execute("SELECT status FROM games WHERE game_id = %s", (GAME,)).fetchone()["status"] == "final"


# ----------------------------------------------- payloads the database refuses (low)

def test_huge_scores_and_nul_characters_are_stored_safely(conn):
    live_game(conn)
    data = fixture("in")
    for entry in data["header"]["competitions"][0]["competitors"]:
        entry["score"] = "3000000000"
    plays = data["drives"]["current"]["plays"]
    plays[-1].update({"text": "Pass \u0000 incomplete", "homeScore": 4e9, "note\u0000": "x\u0000"})
    result = run(conn, Feed(data), T0, PollerState())
    assert result["errors"] == [] and result["rows"] == 6
    rows = summary_rows(conn)
    assert rows[-1]["home_score"] is None and rows[-2]["play_text"] == "Pass  incomplete"
    assert rows[-2]["raw"]["text"] == "Pass  incomplete" and rows[-2]["raw"]["note"] == "x"
    assert raw_fragment({"a": "\\u0000"}) == {"a": "\\u0000"}, "an escaped backslash is text, not a NUL"
    assert raw_fragment({"n": float("inf")})["truncated"] is True


def test_a_refused_game_costs_only_its_own_observation(pool, monkeypatch):
    with pool.connection() as c:
        live_game(c, "2026_05_A", espn="1")
        live_game(c, "2026_05_B", espn="2")
        c.execute("UPDATE settings SET value = %s WHERE key = 'gamestate_max_rps'", (Jsonb(2.0),))
    real = feedlag.record_events

    def refuse(conn: Any, game_id: str, *args: Any) -> int:
        if game_id == "2026_05_A":
            raise psycopg.errors.NumericValueOutOfRange("integer out of range")
        return real(conn, game_id, *args)

    monkeypatch.setattr(feedlag, "record_events", refuse)
    with pool.connection() as c:
        result = run(c, Feed(), T0, PollerState())
    assert result["polled"] == 2 and result["rows"] == 6 and "integer out of range" in result["errors"][0]
    with pool.connection() as c:
        stored = c.execute("SELECT game_id, count(*) AS n FROM game_state GROUP BY game_id").fetchall()
    assert [(r["game_id"], r["n"]) for r in stored] == [("2026_05_B", 6)]


# ------------------------------------------------- postponed and rescheduled (low)

def test_partial_postponed_or_cancelled_status_is_final_and_unknown_names_are_unparsed():
    for name in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_FORFEIT"):
        assert status_of({"type": {"name": name}}) == "final" and status_of({"type": {"name": name, "state": "pre"}}) == "final"
    for name in ("STATUS_SUSPENDED", "STATUS_DELAYED", "STATUS_SOMETHING_NEW"):
        assert status_of({"type": {"name": name}}) is None
        assert status_of({"type": {"name": name, "state": "in"}}) == "in"
    assert status_of({"type": {"name": "STATUS_IN_PROGRESS"}}) == "in"
    data = fixture("in")
    data["header"]["competitions"][0]["status"]["type"] = {"name": "STATUS_POSTPONED"}
    assert parse_summary(data)[-1]["status"] == "final"
    data["header"]["competitions"][0]["status"]["type"] = {"name": "STATUS_SUSPENDED"}
    assert parse_summary(data) == [], "unknown without a state: the scoreboard fallback, not a live game"


def test_a_rescheduled_game_is_polled_again_after_its_new_kickoff(conn):
    live_game(conn)
    post = copy.deepcopy(fixture("pre"))
    post["header"]["competitions"][0]["status"]["type"] = {"name": "STATUS_POSTPONED", "state": "post", "completed": False}
    feed, state = Feed(post), PollerState()
    run(conn, feed, T0, state)
    assert gamestate.live_games(conn, T0 + timedelta(seconds=10)) == [], "postponed: final for the feed"
    conn.execute("UPDATE games SET kickoff_at = %s WHERE game_id = %s", (T0 + timedelta(days=2), GAME))
    later = T0 + timedelta(days=2, minutes=10)
    assert [g["game_id"] for g in gamestate.live_games(conn, later)] == [GAME]
    feed.answer = fixture("in")
    assert run(conn, feed, later, state)["rows"] == 6
    assert latest_state(conn, GAME, later)["state"]["status"] == "in"


# ------------------------------------------- requests stamped when they leave (low)

def test_the_window_holds_in_wall_time_when_an_earlier_task_is_slow(pool, monkeypatch):
    with pool.connection() as c:
        for i in range(4):
            live_game(c, f"2026_05_H{i}", espn=str(200 + i))
    wall = [T0.timestamp()]
    calls: list[float] = []
    monkeypatch.setattr(gamestate, "default_fetch", lambda url: (calls.append(wall[0]), (500, "x"))[1])
    loop = main.ExchangeLoop(pool, clock=lambda: datetime.fromtimestamp(wall[0], T0.tzinfo))
    slow = True
    for _ in range(80):
        now = loop.clock()
        if loop.due("gamestate", now):
            if slow:
                wall[0] += 0.9  # a slow snapshots task: this summary leaves 0.9 s after the pass began
            slow = not slow
            loop.run_task("gamestate", now)
        wall[0] += 0.26
    assert len(calls) >= 15 and max_per_window(calls) <= 1, "at most one request in any wall-clock second"
