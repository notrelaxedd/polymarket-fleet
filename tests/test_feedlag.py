"""Feed lag: score and possession events from the game-state feed, the market move
found in price snapshots, lag_s and the suspension rule of lag_status.

The ESPN payloads are the UNVERIFIED fixtures of tests/test_gamestate.py (written from
ESPN's documented summary shapes; the owner confirms them with probe-gamestate).
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from host.exchange import feedlag, gamestate
from host.exchange.feedlag import detect, fill_market_moves, lag_status
from host.exchange.gamestate import PollerState, latest_state
from tests.test_exchange import make_market
from tests.test_gamestate import T0, Feed, fixture, live_game, run, with_score

GAME = "2026_05_KC_LV"


def with_possession(data: dict[str, Any], team_id: str | None, text: str = "LV 25") -> dict[str, Any]:
    data = copy.deepcopy(data)
    if team_id is None:
        data["situation"].pop("possession", None)
    else:
        data["situation"].update({"possession": team_id, "possessionText": text, "down": 1, "distance": 10})
    return data


def lag_rows(conn: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM feed_lag ORDER BY id").fetchall()]


def snapshot(conn: Any, market_id: Any, ts: datetime, mid: float) -> None:
    conn.execute("INSERT INTO price_snapshots (market_id, ts, bid, ask, mid) VALUES (%s, %s, %s, %s, %s)",
                 (market_id, ts, mid - 0.01, mid + 0.01, mid))


def lag_row(conn: Any, source: str, seen: datetime, lag: float | None, key: str, game: str = GAME,
            event_ts: datetime | None = None) -> None:
    moved = None if lag is None else seen - timedelta(seconds=lag)
    conn.execute(
        """
        INSERT INTO feed_lag (game_id, event_kind, event_key, event_ts, source, feed_seen_at, market_moved_at, lag_s)
        VALUES (%s, 'score', %s, %s, %s, %s, %s, %s)
        """,
        (game, key, event_ts, source, seen, moved, lag),
    )


def test_detect_needs_a_baseline_a_growing_score_and_two_known_sides():
    rows = [{"home_score": 0, "away_score": 7, "possession": "away", "period": 1, "event_ts": None}]
    assert detect(None, rows) == [], "the first observation is no event"
    base = {"home_score": 0, "away_score": 0, "possession": "away"}
    assert [e["kind"] for e in detect(base, rows)] == ["score"]
    assert detect(base, [{**rows[0], "away_score": 0}]) == []
    assert detect({**base, "away_score": 10}, rows) == [], "a corrected (lower) score is no event"
    flips = [{**rows[0], "away_score": 0, "possession": p} for p in ("home", None, "away", "away")]
    assert [(e["kind"], e["possession"]) for e in detect(base, flips)] == [("possession", "home"), ("possession", "away")]
    assert detect({**base, "possession": None}, flips[:1]) == [], "the first known possession is no change"


def test_poll_records_score_and_possession_events_seen_first_by_the_source(conn):
    live_game(conn)
    state, feed = PollerState(), Feed()
    run(conn, feed, T0, state)
    assert lag_rows(conn) == [], "first observation"
    steps = [
        with_score(fixture("in"), 17, 27),
        with_possession(with_score(fixture("in"), 17, 27), "13"),
        with_possession(with_score(fixture("in"), 17, 27), None),
        with_possession(with_score(fixture("in"), 17, 27), "12", "LV 40"),
        with_possession(with_score(fixture("in"), 17, 27), "13", "LV 20"),
        with_possession(with_score(fixture("in"), 17, 24), "13", "LV 20"),
        with_possession(with_score(fixture("in"), 17, 27), "13", "LV 20"),
    ]
    for i, payload in enumerate(steps, start=1):
        feed.answer = payload
        run(conn, feed, T0 + timedelta(seconds=4 * i), state)
    rows = lag_rows(conn)
    assert [(r["event_kind"], r["event_key"]) for r in rows] == [
        ("score", "score:17-27"),
        ("possession", "possession:3:home:17-27:0"),
        ("possession", "possession:3:away:17-27:0"),
        ("possession", "possession:3:home:17-27:1"),
    ], "no event for the missing possession, the corrected score or the score seen again"
    score = rows[0]
    assert score["source"] == "espn_summary" and score["feed_seen_at"] == T0 + timedelta(seconds=4)
    assert score["event_ts"] == datetime.fromisoformat("2026-10-05T22:43:10+00:00"), "the newest play's wallclock"
    assert score["market_moved_at"] is None and score["lag_s"] is None
    latest = latest_state(conn, GAME, T0 + timedelta(seconds=30))
    assert latest["last_change"] == {"kind": "possession", "ts": T0 + timedelta(seconds=20)}
    assert latest["state"]["possession"] == "home" and latest["state"]["yardline_100"] == 80


def test_market_move_is_the_first_mid_more_than_3_cents_from_the_window_baseline(conn):
    live_game(conn)
    home, away = make_market(conn, GAME, "home"), make_market(conn, GAME, "away")
    loose = make_market(conn, GAME, "home", platform="other", confirmed=False)
    event = T0 - timedelta(seconds=30)
    lag_row(conn, "espn_summary", T0, None, "score:0-7", event_ts=event)
    assert fill_market_moves(conn, T0 + timedelta(seconds=5)) == 0, "no snapshots yet"
    for ts, mid in ((-300, 0.40), (-200, 0.41), (-140, 0.43), (-100, 0.44), (-60, 0.45)):
        snapshot(conn, home["id"], T0 + timedelta(seconds=ts), mid)
    for ts, mid in ((-300, 0.59), (-80, 0.55)):
        snapshot(conn, away["id"], T0 + timedelta(seconds=ts), mid)
    snapshot(conn, loose["id"], T0 - timedelta(seconds=400), 0.50)
    snapshot(conn, loose["id"], T0 - timedelta(seconds=149), 0.90)
    assert feedlag.market_moved_at(conn, GAME, event - timedelta(seconds=120), T0) == T0 - timedelta(seconds=80)
    assert fill_market_moves(conn, T0 + timedelta(seconds=5)) == 1
    row = lag_rows(conn)[0]
    assert row["market_moved_at"] == T0 - timedelta(seconds=80) and row["lag_s"] == 80.0, "positive: the feed is behind"
    conn.execute("DELETE FROM price_snapshots WHERE market_id = %s", (away["id"],))
    assert feedlag.market_moved_at(conn, GAME, event - timedelta(seconds=120), T0) == T0 - timedelta(seconds=60), \
        "0.44 is exactly 3 cents from 0.41: not a move; 0.45 is"


def test_a_market_moving_after_the_feed_is_a_negative_lag_and_old_rows_are_given_up(conn):
    live_game(conn)
    home = make_market(conn, GAME, "home")
    lag_row(conn, "espn_summary", T0, None, "score:3-0")
    lag_row(conn, "espn_summary", T0 - timedelta(minutes=20), None, "score:0-0")
    snapshot(conn, home["id"], T0 - timedelta(minutes=30), 0.50)
    snapshot(conn, home["id"], T0 + timedelta(seconds=30), 0.56)
    assert fill_market_moves(conn, T0 + timedelta(seconds=10)) == 0, "the move is not in the past yet"
    assert fill_market_moves(conn, T0 + timedelta(seconds=40)) == 1
    rows = lag_rows(conn)
    assert rows[0]["lag_s"] == -30.0, "negative: the feed was ahead of the market"
    assert rows[1]["market_moved_at"] is None, "older than 15 minutes: no longer retried"
    lag_row(conn, "espn_summary", T0, None, "score:6-0", game=GAME)
    conn.execute("DELETE FROM price_snapshots WHERE ts < %s", (T0,))
    assert fill_market_moves(conn, T0 + timedelta(seconds=40)) == 0, "no mid before the window: not measurable"


def test_lag_status_median_of_the_last_20_and_the_suspension_rule(conn):
    live_game(conn)
    empty = lag_status(conn)
    assert empty == {"suspended": False, "median_lag_s": None, "n": 0, "by_source": {}}
    for i in range(5):
        lag_row(conn, "espn_summary", T0 - timedelta(minutes=60 - i), 1.0, f"old:{i}")
    lags = [25.0] * 11 + [15.0] * 9
    for i, lag in enumerate(lags):
        lag_row(conn, "espn_summary", T0 - timedelta(minutes=30 - i), lag, f"new:{i}")
    lag_row(conn, "espn_summary", T0, None, "unmeasured")
    for i in range(3):
        lag_row(conn, "yahoo", T0 - timedelta(minutes=i), -5.0, f"y:{i}")
    status = lag_status(conn, "espn_summary")
    assert status["n"] == 20 and status["median_lag_s"] == 25.0 and status["suspended"], "median 25 s > 20 s"
    assert set(status["by_source"]) == {"espn_summary"}
    overall = lag_status(conn)
    assert overall["n"] == 20 and set(overall["by_source"]) == {"espn_summary", "yahoo"}
    assert overall["by_source"]["yahoo"] == {"suspended": False, "median_lag_s": -5.0, "n": 3}
    conn.execute("UPDATE settings SET value = %s WHERE key = 'ingame_max_lag_s'", (Jsonb(25),))
    assert not lag_status(conn, "espn_summary")["suspended"], "the limit is a strict 'exceeds'"
    assert gamestate.lag_status is lag_status
