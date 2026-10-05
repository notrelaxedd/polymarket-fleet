"""The in-game additions to the trade state payload (host/trading/state.py, step 6C
contract section 8) and the minimum-events rule of the feed-lag suspension
(host/exchange/feedlag.py, contract section 6)."""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from fleet.sim.odds import devig
from host.exchange import feedlag
from host.exchange.feedlag import enough_data, lag_status
from host.trading.state import STATE_SETTINGS, pregame_p_home, trade_state
from tests.conftest import insert_market, set_setting
from tests.test_ingame_approval import ask, ingame_setup, put_change, put_state

INGAME_SETTINGS = ("ingame_tick_s", "ingame_max_state_age_s", "ingame_quiet_seconds", "ingame_cutoff_seconds",
                   "ingame_dead_zone", "ingame_min_edge", "ingame_max_bet_cents", "ingame_gtd_seconds")


def entry_of(conn: psycopg.Connection, worker_id: str) -> dict[str, Any]:
    body = trade_state(conn, worker_id)
    assert len(body["assignments"]) == 1
    return body["assignments"][0]


def test_settings_carry_the_ingame_rules(conn):
    s = ingame_setup(conn)
    assert set(INGAME_SETTINGS) <= set(STATE_SETTINGS)
    settings = trade_state(conn, s.worker.id)["settings"]
    assert {k: settings[k] for k in INGAME_SETTINGS} == {
        "ingame_tick_s": 5, "ingame_max_state_age_s": 30, "ingame_quiet_seconds": 20, "ingame_cutoff_seconds": 120,
        "ingame_dead_zone": 0.03, "ingame_min_edge": 0.05, "ingame_max_bet_cents": 500, "ingame_gtd_seconds": 60,
    }


def test_ingame_block_with_model_state_prior_and_lag(conn):
    s = ingame_setup(conn)
    game = s.game["game_id"]
    put_state(conn, game, age_s=3.0)
    put_change(conn, game, 40.0, key="score:17-14")
    block = entry_of(conn, s.worker.id)["ingame"]
    assert set(block) == {"enabled", "model", "game_state", "pregame_p_home", "lag"}
    assert block["enabled"] is True
    assert block["model"]["family"] == "ingame_wp" and set(block["model"]) == {"id", "family", "params", "artifact"}
    assert block["model"]["params"] == {"l2": 1.0, "time_scale": 1.0, "fp_scale": 1.0}
    state = block["game_state"]
    assert set(state) == {"state", "ts", "age_s", "source", "last_change"}
    assert state["state"]["period"] == 3 and state["state"]["home_score"] == 17 and state["source"] == "espn_summary"
    assert 3.0 <= state["age_s"] < 10.0
    assert state["last_change"]["kind"] == "score"
    assert block["pregame_p_home"] == pytest.approx(devig(-150, 130)), "the devigged closing moneyline first"
    assert block["lag"] == {"suspended": False, "median_lag_s": None, "n": 0}


def test_disabled_block_without_flag_or_model_and_no_state(conn):
    s = ingame_setup(conn, enabled=False)
    block = entry_of(conn, s.worker.id)["ingame"]
    assert block["enabled"] is False and block["model"] is not None and block["game_state"] is None
    conn.execute("UPDATE assignments SET trade_ingame = true, ingame_model_id = NULL WHERE id = %s", (s.assignment["id"],))
    block = entry_of(conn, s.worker.id)["ingame"]
    assert block["enabled"] is False and block["model"] is None


def test_lag_summary_in_the_block(conn):
    s = ingame_setup(conn)
    for i in range(5):
        put_change(conn, s.game["game_id"], 600 + i, key=f"e:{i}", lag=25.0)
    assert entry_of(conn, s.worker.id)["ingame"]["lag"] == {"suspended": True, "median_lag_s": 25.0, "n": 5}


def test_open_orders_carry_the_ingame_flag(conn):
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    decision = ask(conn, s, size=2)
    assert decision["status"] == "approved", decision
    open_orders = entry_of(conn, s.worker.id)["open_orders"]
    assert [(str(o["id"]), o["ingame"]) for o in open_orders] == [(decision["order_id"], True)]


def test_pregame_p_home_falls_back_to_the_frozen_closing_mid(conn):
    s = ingame_setup(conn)
    game = s.game["game_id"]
    conn.execute("UPDATE games SET home_moneyline = NULL WHERE game_id = %s", (game,))
    row = dict(conn.execute("SELECT * FROM games WHERE game_id = %s", (game,)).fetchone())
    assert pregame_p_home(conn, row) is None, "no line and no frozen price"
    away = insert_market(conn, game, side="away")
    conn.execute("UPDATE markets SET closing_price = 0.40 WHERE id = %s", (away["id"],))
    assert pregame_p_home(conn, row) == pytest.approx(0.60), "only the away market frozen: its complement"
    conn.execute("UPDATE markets SET closing_price = 0.57 WHERE id = %s", (s.market["id"],))
    assert pregame_p_home(conn, row) == pytest.approx(0.57), "the home market's closing mid"
    assert entry_of(conn, s.worker.id)["ingame"]["pregame_p_home"] == pytest.approx(0.57)
    assert pregame_p_home(conn, None) is None


# ------------------------------------------------------------------ lag_status minimum events


def test_lag_suspends_only_with_the_minimum_number_of_measured_events(conn):
    s = ingame_setup(conn)
    game = s.game["game_id"]
    for i in range(4):
        put_change(conn, game, 600 + i, key=f"e:{i}", lag=40.0)
    put_change(conn, game, 500, key="unmeasured")  # not measured: never counted
    status = lag_status(conn)
    assert status["n"] == 4 and status["median_lag_s"] == 40.0 and status["suspended"] is False
    assert status["by_source"]["espn_summary"]["suspended"] is False
    assert not enough_data(status, feedlag.min_events(conn)), "reported as not enough data"
    put_change(conn, game, 700, key="e:4", lag=40.0)
    status = lag_status(conn)
    assert status["n"] == 5 and status["suspended"] is True and enough_data(status, feedlag.min_events(conn))
    assert status["by_source"]["espn_summary"] == {"suspended": True, "median_lag_s": 40.0, "n": 5}
    set_setting(conn, "ingame_lag_min_events", 6)
    assert lag_status(conn)["suspended"] is False and feedlag.min_events(conn) == 6
    set_setting(conn, "ingame_lag_min_events", "junk")
    assert feedlag.min_events(conn) == feedlag.DEFAULT_MIN_EVENTS and lag_status(conn)["suspended"] is True
    set_setting(conn, "ingame_lag_min_events", 1)
    set_setting(conn, "ingame_max_lag_s", 40)
    assert lag_status(conn)["suspended"] is False, "the limit stays a strict 'exceeds'"
