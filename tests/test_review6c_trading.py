"""Step 6C review, trading lens: a retired in-game lineage stops trading and can be
switched off from the dashboard; after kickoff a request is never approved outside the
in-game rules whatever its `ingame` flag; a contract belongs to the model that bought
it at settlement; order rows name the model that placed the order."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from host import eligibility, model_owner
from host.exchange import paper, settle
from host.exchange.executor import Executor
from host.trading import assignments_ingame, ledger
from host.trading.state import trade_state
from host.trading.views import list_orders
from host.trading.views_ingame import assignment_ingame
from tests.conftest import flash_cookie, insert_snapshot, order_row, set_setting, trade_setup
from tests.sell_helpers import bought, sold
from tests.test_exchange import make_assignment, make_model
from tests.test_ingame_approval import ask, detail, hold, ingame_setup, put_change, put_state
from tests.test_ingame_settlement import GAME, final_game, ingame_model, mark_ingame, score, with_ingame


def _assignment(conn, s) -> dict[str, Any]:
    return conn.execute("SELECT * FROM assignments WHERE id = %s", (s.assignment["id"],)).fetchone()


# ------------------------------------------------------------------ retired in-game lineage


def test_retiring_the_ingame_lineage_stops_ingame_trading(conn):
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    hold(conn, s, 2)
    open_order = ask(conn, s, size=1)
    Executor().tick(conn)
    assert order_row(conn, open_order["order_id"])["status"] == "open"
    mid = _assignment(conn, s)["ingame_model_id"]
    model_owner.retire(conn, mid, "retired", "owner")
    # The host refuses at once, and the worker is told in-game trading is off.
    assert ask(conn, s, size=1)["reason"] == "ingame_disabled"
    assert ask(conn, s, size=1, order_side="sell")["reason"] == "ingame_disabled"
    assert trade_state(conn, s.worker.id)["assignments"][0]["ingame"]["enabled"] is False
    assert assignment_ingame(conn, dict(_assignment(conn, s)), [], 30.0, {})["enabled"] is False
    # Retiring switches it off at once (audited) and cancels the open in-game order.
    a = _assignment(conn, s)
    assert (a["status"], a["trade_ingame"], a["ingame_model_id"]) == ("active", False, mid)
    assert order_row(conn, open_order["order_id"])["status"] == "cancelled"
    audit = conn.execute("SELECT after FROM audit_log WHERE action = 'assignment_ingame' ORDER BY id DESC LIMIT 1").fetchone()
    assert audit["after"]["trade_ingame"] is False and audit["after"]["orders_cancelled"] == 1
    retired = conn.execute("SELECT after FROM audit_log WHERE action = 'model_retired' ORDER BY id DESC LIMIT 1").fetchone()
    assert retired["after"]["ingame_disabled"] == [str(a["id"])]
    assert Executor().tick(conn)["ingame_retired"] == 0, "already off"
    assert ledger.replay_problems(conn) == []


def test_the_executor_turns_off_a_lineage_retired_elsewhere(conn):
    # A lineage retired without model_owner.retire (an older host, a manual update)
    # is still caught on the executor's next tick.
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    mid = _assignment(conn, s)["ingame_model_id"]
    conn.execute("UPDATE models SET status = 'retired' WHERE lineage_id = (SELECT lineage_id FROM models WHERE id = %s)", (mid,))
    assert Executor().tick(conn)["ingame_retired"] == 1
    assert _assignment(conn, s)["trade_ingame"] is False
    assert Executor().tick(conn)["ingame_retired"] == 0, "once"


def test_turn_off_retired_for_one_lineage(conn):
    s = ingame_setup(conn)
    mid = _assignment(conn, s)["ingame_model_id"]
    lineage = conn.execute("SELECT lineage_id FROM models WHERE id = %s", (mid,)).fetchone()["lineage_id"]
    other = make_model(conn)
    assert assignments_ingame.turn_off_retired(conn, "owner", lineage) == [], "not retired yet"
    conn.execute("UPDATE models SET status = 'retired' WHERE lineage_id = %s", (lineage,))
    assert assignments_ingame.turn_off_retired(conn, "owner", other["lineage_id"]) == []
    assert assignments_ingame.turn_off_retired(conn, "owner", lineage) == [str(s.assignment["id"])]
    assert _assignment(conn, s)["trade_ingame"] is False


def test_the_form_can_switch_off_a_retired_ingame_model(client, conn):
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    hold(conn, s, 2)  # in-game orders exist: the model can no longer change
    mid = str(_assignment(conn, s)["ingame_model_id"])
    conn.execute("UPDATE models SET status = 'retired' WHERE id = %s", (mid,))
    # The form always sends both keys: the kept (retired) model and an unticked box.
    r = client.post(f"/assignments/{s.assignment['id']}/ingame", data={"ingame_model_id": mid}, follow_redirects=False)
    assert r.status_code == 303 and "in-game trading off" in flash_cookie(r)
    a = _assignment(conn, s)
    assert (str(a["ingame_model_id"]), a["trade_ingame"]) == (mid, False)
    r = client.post(f"/assignments/{s.assignment['id']}/ingame", data={"ingame_model_id": mid, "trade_ingame": "on"},
                    follow_redirects=False)
    assert "in-game change refused: the in-game model's lineage is retired" in flash_cookie(r)
    assert _assignment(conn, s)["trade_ingame"] is False


# ------------------------------------------------------------------ after kickoff, the in-game rules


def test_untagged_request_in_play_runs_the_ingame_rules_when_not_pregame_only(conn):
    s = ingame_setup(conn)
    game = s.game["game_id"]
    put_state(conn, game, period=4, clock_seconds=30)
    put_change(conn, game, 1)
    assert ask(conn, s, size=1, ingame_flag=False)["reason"] == "kickoff", "pregame only: no pre-game order in play"
    set_setting(conn, "trade_pregame_only", False)
    assert ask(conn, s, size=1, ingame_flag=False)["reason"] == "ingame_quiet"
    conn.execute("DELETE FROM feed_lag")
    assert ask(conn, s, size=1, ingame_flag=False)["reason"] == "ingame_cutoff"
    put_state(conn, game, period=3, clock_seconds=600)
    conn.execute("UPDATE assignments SET trade_ingame = false WHERE id = %s", (s.assignment["id"],))
    assert ask(conn, s, size=1, ingame_flag=False)["reason"] == "ingame_disabled", "the trade_ingame switch applies"
    conn.execute("UPDATE assignments SET trade_ingame = true WHERE id = %s", (s.assignment["id"],))
    set_setting(conn, "ingame_max_bet_cents", 10)
    assert ask(conn, s, size=1, ingame_flag=False)["reason"] == "max_bet", "the in-game max bet applies"
    set_setting(conn, "ingame_max_bet_cents", 500)
    decision = ask(conn, s, size=1, ingame_flag=False)
    assert decision["status"] == "approved", decision
    row = order_row(conn, decision["order_id"])
    assert row["ingame"] is False, "the pre-game model's decision stays on its lineage"
    event = detail(conn, row["id"])
    assert event["in_play"] is True and "ingame" not in event and event["state_at_entry"]["period"] == 3
    # The executor and the paper simulator treat it as an in-game order.
    Executor().tick(conn)
    row = order_row(conn, row["id"])
    assert row["status"] == "open" and row["gtd_at"] == row["submitted_at"] + timedelta(seconds=60)
    assert Executor().tick(conn)["kickoff"] == 0 and paper.kickoff_bound(conn, row) is None
    insert_snapshot(conn, s.market["id"], bid=0.48, ask=0.50)
    assert paper.process(conn) >= 1 and order_row(conn, row["id"])["filled_size"] == 1
    assert ledger.replay_problems(conn) == []


def test_untagged_sell_in_play_runs_the_ingame_sell_rules(conn):
    s = ingame_setup(conn)
    game = s.game["game_id"]
    put_state(conn, game)
    hold(conn, s, 4)
    put_change(conn, game, 1)
    assert ask(conn, s, size=1, ingame_flag=False, order_side="sell", price=0.48)["reason"] == "kickoff"
    set_setting(conn, "trade_pregame_only", False)
    assert ask(conn, s, size=1, ingame_flag=False, order_side="sell", price=0.48)["reason"] == "ingame_quiet"


def test_live_request_after_kickoff_is_never_approved_untagged(conn):
    s = trade_setup(conn, mode="live", model_status="live_eligible")
    conn.execute("UPDATE games SET kickoff_at = now() - interval '1 hour' WHERE game_id = %s", (s.game["game_id"],))
    put_state(conn, s.game["game_id"], period=4, clock_seconds=30)
    assert ask(conn, s, size=1, ingame_flag=False)["reason"] == "kickoff"
    set_setting(conn, "trade_pregame_only", False)
    assert ask(conn, s, size=1, ingame_flag=False)["reason"] == "ingame_disabled"
    live = ingame_setup(conn, mode="live", model_status="live_eligible", game_id="2026_05_SF_SEA")
    put_state(conn, live.game["game_id"])
    assert ask(conn, live, size=1, ingame_flag=False)["reason"] == "ingame_paper_only"
    assert ask(conn, live, size=1, ingame_flag=False, order_side="sell", price=0.48)["reason"] == "ingame_paper_only"


def test_an_order_approved_before_kickoff_never_trades_in_play(conn):
    set_setting(conn, "trade_pregame_only", False)
    s = trade_setup(conn, kickoff_in_s=120)
    decision = ask(conn, s, size=2, ingame_flag=False)
    assert decision["status"] == "approved"
    Executor().tick(conn)
    row = order_row(conn, decision["order_id"])
    assert row["gtd_at"] == s.game["kickoff_at"], "capped at kickoff whatever trade_pregame_only says"
    conn.execute("UPDATE games SET kickoff_at = now() - interval '1 minute' WHERE game_id = %s", (s.game["game_id"],))
    insert_snapshot(conn, s.market["id"], bid=0.48, ask=0.50)
    assert paper.process(conn) == 0 and order_row(conn, row["id"])["filled_size"] == 0
    assert Executor().tick(conn)["kickoff"] == 1 and order_row(conn, row["id"])["status"] == "cancelled"


# ------------------------------------------------------------------ who owns a sold contract


def _bets(conn, a) -> dict[Any, dict[str, Any]]:
    return {r["order_id"]: r for r in conn.execute("SELECT * FROM bets WHERE assignment_id = %s", (a["id"],)).fetchall()}


def _check_ledger(conn, a, rows) -> None:
    bank = conn.execute("SELECT * FROM bankrolls WHERE assignment_id = %s", (a["id"],)).fetchone()
    assert bank["realized_pnl_cents"] == sum(r["pnl_cents"] for r in rows.values()), "bets add up to the ledger"
    assert (bank["open_cost_cents"], bank["reserved_cents"]) == (0, 0)
    assert ledger.replay_problems(conn) == []


def test_an_ingame_sell_of_the_pregame_pick_stays_on_the_pregame_lineage(conn):
    home = final_game(conn, 10, 24)  # home loses
    pre, wp = make_model(conn), ingame_model(conn)
    a = with_ingame(conn, make_assignment(conn, GAME, pre, bankroll_cents=10_000), wp)
    pre_buy = bought(conn, a, home, 0.60, 10, 12)
    in_sell = mark_ingame(conn, sold(conn, a, home, 0.20, 10, 4))
    settle.settle_game(conn, GAME, "test")
    rows = _bets(conn, a)
    p, s = rows[pre_buy["id"]], rows[in_sell["id"]]
    assert (p["model_id"], p["cost_cents"], p["fee_cents"], p["pnl_cents"]) == (pre["id"], 600, 16, 200 - 600 - 16)
    assert (s["model_id"], s["ingame"], s["cost_cents"], s["fee_cents"], s["pnl_cents"]) == (wp["id"], True, 0, 0, 0)
    assert eligibility.paper_stats(conn, pre["lineage_id"])["pnl_cents"] == -416
    stats = eligibility.paper_stats(conn, wp["lineage_id"])
    assert (stats["bets"], stats["pnl_cents"]) == (0, 0), "no P&L for a position the in-game model never bought"
    assert (score(conn, wp)["pnl_cents"], score(conn, wp)["ingame_pnl_cents"]) == (0, 0)
    _check_ledger(conn, a, rows)


def _mixed_lot(conn, home_score: int, away_score: int):
    home = final_game(conn, home_score, away_score)
    pre, wp = make_model(conn), ingame_model(conn)
    a = with_ingame(conn, make_assignment(conn, GAME, pre, bankroll_cents=10_000), wp)
    pre_buy = bought(conn, a, home, 0.60, 10, 10)
    in_buy = mark_ingame(conn, bought(conn, a, home, 0.30, 10, 8))
    in_sell = mark_ingame(conn, sold(conn, a, home, 0.40, 10, 6))  # pooled basis 450, own basis 300
    settle.settle_game(conn, GAME, "test")
    rows = _bets(conn, a)
    return a, pre, wp, rows[pre_buy["id"]], rows[in_buy["id"]], rows[in_sell["id"]], rows


def test_each_model_is_scored_at_its_own_cost_on_a_shared_market(conn):
    a, pre, wp, p, i, s, rows = _mixed_lot(conn, 24, 10)
    assert (s["cost_cents"], s["pnl_cents"]) == (300, 400 - 6 - 300), "the in-game model sold its own 10"
    assert (i["cost_cents"], i["pnl_cents"]) == (0, -8)
    assert (p["cost_cents"], p["pnl_cents"]) == (600, 1000 - 600 - 10), "the pre-game model's 10 win"
    assert score(conn, wp)["pnl_cents"] == 94 - 8 and score(conn, pre)["pnl_cents"] == 390
    _check_ledger(conn, a, rows)


def test_a_push_on_a_shared_market_still_adds_up_to_the_ledger(conn):
    a, pre, wp, p, i, s, rows = _mixed_lot(conn, 17, 17)
    assert (s["pnl_cents"], i["pnl_cents"]) == (94, -8)
    assert p["pnl_cents"] == 450 - 600 - 10, "a push pays back the position's pooled basis"
    _check_ledger(conn, a, rows)


# ------------------------------------------------------------------ order rows


def test_order_rows_name_the_model_that_placed_them(conn):
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    in_order = ask(conn, s, size=1)["order_id"]
    pre = trade_setup(conn, game_id="2026_05_SF_SEA")
    pre_order = ask(conn, pre, size=1, ingame_flag=False)["order_id"]
    rows = {str(r["id"]): r for r in list_orders(conn)}
    wp = _assignment(conn, s)["ingame_model_id"]
    assert (rows[in_order]["model_id"], rows[in_order]["family"]) == (wp, "ingame_wp")
    assert rows[pre_order]["model_id"] == pre.model["id"] and rows[pre_order]["family"] != "ingame_wp"
