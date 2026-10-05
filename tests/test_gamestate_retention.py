"""Retention of the live game state (docs/INGAME.md, "Live game state"): the nightly
retention pass deletes game_state rows older than snapshot_retention_days but always
keeps each game's newest row, and a game settled after the prune still gets the state
its approval event recorded."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from host.exchange import retention, settle
from host.exchange.gamestate import latest_state
from host.trading import orders
from tests.sell_helpers import bought
from tests.test_exchange import NOW, make_assignment, make_game, make_market, make_model
from tests.test_ingame_settlement import bets_by_order, ingame_model, mark_ingame, with_ingame

GAME, OTHER = "2026_05_KC_LV", "2026_05_PHI_DAL"
ENTRY = {"period": 3, "clock_seconds": 252, "home_score": 17, "away_score": 14, "possession": "home"}


def state_row(conn: Any, game_id: str, ts: datetime, home: int, status: str = "in", play_id: str | None = None) -> int:
    return conn.execute(
        """
        INSERT INTO game_state (game_id, ts, source, status, period, clock_seconds, home_score, away_score, play_id)
        VALUES (%s, %s, 'espn_summary', %s, 3, 252, %s, 14, %s) RETURNING id
        """,
        (game_id, ts, status, home, play_id),
    ).fetchone()["id"]


def ids(conn: Any) -> set[int]:
    return {r["id"] for r in conn.execute("SELECT id FROM game_state").fetchall()}


def test_retention_prunes_old_game_states_but_keeps_each_games_newest_row(conn):
    make_game(conn, GAME, "LV", "KC", NOW - timedelta(days=20), status="final", home_score=27, away_score=17)
    make_game(conn, OTHER, "DAL", "PHI", NOW - timedelta(days=30), status="final", home_score=20, away_score=10)
    old = NOW - timedelta(days=20)
    state_row(conn, GAME, old, 14, play_id="401800001101")
    state_row(conn, GAME, old + timedelta(seconds=4), 17)
    recent = state_row(conn, GAME, NOW - timedelta(days=1), 27, "final")
    gone = NOW - timedelta(days=30)
    state_row(conn, OTHER, gone, 7)
    state_row(conn, OTHER, gone + timedelta(seconds=4), 10)
    newest = state_row(conn, OTHER, gone + timedelta(seconds=4), 20, "final")
    before = latest_state(conn, OTHER, NOW)
    result = retention.run(conn, NOW, days=14)
    assert result["game_states"] == 4
    assert ids(conn) == {recent, newest}, "recent rows stay; an old game keeps its newest row (ties by id)"
    assert latest_state(conn, OTHER, NOW) == before, "the current situation of a long finished game is unchanged"
    assert retention.run(conn, NOW, days=14)["game_states"] == 0, "idempotent"
    assert retention.run(conn, NOW, days=1)["game_states"] == 0, "a game's newest row is never pruned"


def test_a_game_settled_after_the_prune_keeps_the_state_recorded_at_approval(conn):
    kickoff = NOW - timedelta(days=20)
    make_game(conn, GAME, "LV", "KC", kickoff, status="final", home_score=27, away_score=17)
    home = make_market(conn, GAME, "home")
    wp = ingame_model(conn)
    a = with_ingame(conn, make_assignment(conn, GAME, make_model(conn)), wp)
    entered = kickoff + timedelta(hours=2)
    state_row(conn, GAME, entered - timedelta(seconds=3), 17)
    state_row(conn, GAME, kickoff + timedelta(hours=4), 27, "final")
    order = mark_ingame(conn, bought(conn, a, home, 0.40, 10, 12), created_at=entered)
    orders.add_order_event(conn, order["id"], None, "approved", "test", {"state_at_entry": ENTRY, "ingame": True})
    assert retention.run(conn, NOW, days=14)["game_states"] == 1
    settle.settle_game(conn, GAME, "test")
    row = bets_by_order(conn, a)[order["id"]]
    assert (row["ingame"], row["model_id"], row["state_at_entry"]) == (True, wp["id"], ENTRY)
