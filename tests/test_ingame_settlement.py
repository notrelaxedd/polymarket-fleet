"""Settlement with in-game orders (contract section 11, docs/TRADING.md "In-game trading
(step 6 Part C)"): the money splits as for any order (checked to the cent), an in-game
row carries ingame, a null CLV and the game state at entry, and is attributed to the
assignment's in-game model, so model_scores and eligibility cover both lineages; CLV
and the paper gate never see in-game rows; the ledger identity holds."""
from __future__ import annotations

import itertools
from datetime import timedelta
from typing import Any

import pytest

from host import eligibility
from host.exchange import settle
from host.trading import ledger
from tests.conftest import insert_model
from tests.sell_helpers import bought, sold
from tests.test_exchange import NOW, bankroll, make_assignment, make_game, make_market, make_model, snap

GAME = "2026_05_KC_LV"
KICKOFF = NOW - timedelta(hours=4)
_SEQ = itertools.count()


def ingame_model(conn) -> dict[str, Any]:
    params = {"l2": 0.5 + next(_SEQ) / 100, "time_scale": 1.0, "fp_scale": 1.0}
    return insert_model(conn, family="ingame_wp", params=params, artifact={"coef": [0.0] * 10},
                        metrics={"era": "search", "log_loss": 0.44})


def with_ingame(conn, assignment: dict[str, Any], model: dict[str, Any] | None) -> dict[str, Any]:
    conn.execute("UPDATE assignments SET ingame_model_id = %s, trade_ingame = true WHERE id = %s",
                 (None if model is None else model["id"], assignment["id"]))
    return assignment


def mark_ingame(conn, order: dict[str, Any], created_at=None) -> dict[str, Any]:
    conn.execute("UPDATE orders SET ingame = true, created_at = COALESCE(%s, created_at) WHERE id = %s",
                 (created_at, order["id"]))
    return order


def game_state(conn, ts, period: int, clock: int, home: int, away: int, possession: str | None) -> None:
    conn.execute(
        """
        INSERT INTO game_state (game_id, ts, source, status, period, clock_seconds, home_score, away_score, possession,
                                down, distance, yardline_100, home_timeouts, away_timeouts)
        VALUES (%s, %s, 'espn_summary', 'in', %s, %s, %s, %s, %s, 1, 10, 75, 3, 3)
        """,
        (GAME, ts, period, clock, home, away, possession),
    )


def final_game(conn, home_score: int, away_score: int):
    make_game(conn, GAME, "LV", "KC", KICKOFF, status="final", home_score=home_score, away_score=away_score)
    home = make_market(conn, GAME, "home")
    snap(conn, home["id"], 0.54, 0.56, KICKOFF - timedelta(minutes=5))  # closing price 0.55
    return home


def bets_by_order(conn, assignment) -> dict[Any, dict[str, Any]]:
    rows = conn.execute("SELECT * FROM bets WHERE assignment_id = %s ORDER BY id", (assignment["id"],)).fetchall()
    return {r["order_id"]: r for r in rows}


def score(conn, model, mode: str = "paper") -> dict[str, Any]:
    return conn.execute("SELECT * FROM model_scores WHERE model_id = %s AND game_id = %s AND mode = %s",
                        (model["id"], GAME, mode)).fetchone()


def test_ingame_buy_and_sell_settle_to_the_cent_and_score_both_lineages(conn):
    home = final_game(conn, 27, 17)
    pre, wp = make_model(conn), ingame_model(conn)
    a = with_ingame(conn, make_assignment(conn, GAME, pre, bankroll_cents=10_000), wp)
    game_state(conn, NOW - timedelta(minutes=10), 3, 252, 17, 14, "home")
    game_state(conn, NOW - timedelta(minutes=5), 4, 600, 24, 14, "away")
    game_state(conn, NOW - timedelta(minutes=1), 4, 30, 27, 17, None)
    pre_buy = bought(conn, a, home, 0.40, 10, 12)
    in_buy = mark_ingame(conn, bought(conn, a, home, 0.60, 5, 6), NOW - timedelta(minutes=7))
    in_sell = mark_ingame(conn, sold(conn, a, home, 0.70, 6, 7), NOW - timedelta(minutes=2))
    result = settle.settle_game(conn, GAME, "test")
    # 15 bought (basis 700), 6 sold at average cost 280; 9 held (basis 420) win 900:
    # basis split 240/180 by bought basis, payout 600/300 by bought contracts.
    rows = bets_by_order(conn, a)
    p, i, s = rows[pre_buy["id"]], rows[in_buy["id"]], rows[in_sell["id"]]
    assert (p["ingame"], p["model_id"], p["lineage_id"], p["state_at_entry"]) == (False, pre["id"], pre["lineage_id"], None)
    assert (p["cost_cents"], p["stake_cents"], p["pnl_cents"]) == (240, 412, 600 - 240 - 12)
    assert p["clv"] == pytest.approx(0.15)
    assert (i["ingame"], i["model_id"], i["lineage_id"], i["clv"]) == (True, wp["id"], wp["lineage_id"], None)
    assert (i["order_side"], i["result"], i["cost_cents"], i["stake_cents"], i["pnl_cents"]) == ("buy", "win", 180, 306, 300 - 180 - 6)
    assert i["state_at_entry"] == {"period": 3, "clock_seconds": 252, "home_score": 17, "away_score": 14, "possession": "home"}
    assert (s["ingame"], s["model_id"], s["lineage_id"], s["clv"]) == (True, wp["id"], wp["lineage_id"], None)
    assert (s["order_side"], s["result"], s["cost_cents"], s["stake_cents"], s["pnl_cents"]) == ("sell", "sold", 280, 0, 420 - 7 - 280)
    assert s["state_at_entry"] == {"period": 4, "clock_seconds": 600, "home_score": 24, "away_score": 14, "possession": "away"}
    total = 348 + 114 + 133
    bank = bankroll(conn, a)
    assert bank["realized_pnl_cents"] == total == sum(r["pnl_cents"] for r in rows.values()), "bets add up to the ledger"
    assert (bank["available_cents"], bank["open_cost_cents"], bank["reserved_cents"]) == (10_000 + total, 0, 0)
    assert ledger.replay_problems(conn) == []
    ps, ws = score(conn, pre), score(conn, wp)
    assert (ps["n_bets"], ps["stake_cents"], ps["pnl_cents"], ps["ingame_n_bets"], ps["ingame_pnl_cents"]) == (1, 412, 348, 0, 0)
    assert ps["avg_clv"] == pytest.approx(0.15, abs=1e-6) and ps["lineage_id"] == pre["lineage_id"]
    assert (ws["n_bets"], ws["stake_cents"], ws["pnl_cents"], ws["ingame_n_bets"], ws["ingame_pnl_cents"]) == (1, 306, 247, 1, 247)
    assert ws["avg_clv"] is None and ws["lineage_id"] == wp["lineage_id"], "CLV excludes in-game rows"
    assert result["bets"] == 2 and result["pnl_cents"] == total
    assert {x["lineage_id"] for x in result["lineages"]} == {str(pre["lineage_id"]), str(wp["lineage_id"])}
    job = conn.execute("SELECT status, result FROM jobs WHERE id = %s", (a["job_id"],)).fetchone()
    assert job["status"] == "succeeded"
    assert {k: job["result"][k] for k in ("n_bets", "pnl_cents", "ingame_n_bets", "ingame_pnl_cents")} == {
        "n_bets": 2, "pnl_cents": total, "ingame_n_bets": 1, "ingame_pnl_cents": 247}
    # The pre-game lineage's paper record and CLV interval never see the in-game rows.
    stats = eligibility.paper_stats(conn, pre["lineage_id"])
    assert (stats["bets"], stats["pnl_cents"]) == (1, 348) and stats["avg_clv"] == pytest.approx(0.15, abs=1e-6)
    in_stats = eligibility.paper_stats(conn, wp["lineage_id"])
    assert (in_stats["bets"], in_stats["pnl_cents"], in_stats["avg_clv"]) == (1, 247, None)
    n_clv = conn.execute("SELECT count(*) AS n FROM bets WHERE lineage_id = %s AND clv IS NOT NULL", (wp["lineage_id"],)).fetchone()
    assert n_clv["n"] == 0


def test_two_assignments_sharing_an_ingame_model_on_a_loss(conn):
    home = final_game(conn, 10, 24)
    wp = ingame_model(conn)
    pre_a, pre_b = make_model(conn), make_model(conn)
    a = with_ingame(conn, make_assignment(conn, GAME, pre_a), wp)
    b = with_ingame(conn, make_assignment(conn, GAME, pre_b), wp)
    order_a = mark_ingame(conn, bought(conn, a, home, 0.45, 4, 3))
    order_b = mark_ingame(conn, bought(conn, b, home, 0.30, 10, 5))
    settle.settle_game(conn, GAME, "test")
    ra, rb = bets_by_order(conn, a)[order_a["id"]], bets_by_order(conn, b)[order_b["id"]]
    assert (ra["result"], ra["pnl_cents"], ra["model_id"], ra["state_at_entry"]) == ("loss", -180 - 3, wp["id"], None), "no state yet"
    assert (rb["result"], rb["pnl_cents"], rb["model_id"]) == ("loss", -300 - 5, wp["id"])
    ws = score(conn, wp)
    assert (ws["n_bets"], ws["stake_cents"], ws["pnl_cents"], ws["ingame_n_bets"], ws["ingame_pnl_cents"]) == (
        2, 183 + 305, -488, 2, -488), "recomputed over both assignments, not overwritten by the second"
    for pre in (pre_a, pre_b):
        ps = score(conn, pre)
        assert (ps["n_bets"], ps["pnl_cents"], ps["ingame_n_bets"], ps["avg_clv"]) == (0, 0, 0, None)
    assert bankroll(conn, a)["realized_pnl_cents"] + bankroll(conn, b)["realized_pnl_cents"] == -488
    assert ledger.replay_problems(conn) == []


def test_push_and_an_ingame_order_without_an_ingame_model(conn):
    home = final_game(conn, 20, 20)
    pre = make_model(conn)
    a = with_ingame(conn, make_assignment(conn, GAME, pre), None)
    game_state(conn, NOW - timedelta(minutes=3), 2, 45, 10, 10, "away")
    order = mark_ingame(conn, bought(conn, a, home, 0.50, 6, 4))
    settle.settle_game(conn, GAME, "test")
    row = bets_by_order(conn, a)[order["id"]]
    assert (row["result"], row["cost_cents"], row["pnl_cents"], row["clv"]) == ("push", 300, -4, None)
    assert (row["ingame"], row["model_id"]) == (True, pre["id"]), "falls back to the pre-game model"
    assert row["state_at_entry"]["possession"] == "away"
    ps = score(conn, pre)
    assert (ps["n_bets"], ps["pnl_cents"], ps["ingame_n_bets"], ps["ingame_pnl_cents"], ps["avg_clv"]) == (1, -4, 1, -4, None)
    assert bankroll(conn, a)["realized_pnl_cents"] == -4 and ledger.replay_problems(conn) == []
