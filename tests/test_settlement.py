"""Settlement: win/loss/push, CLV sign and the closing price, bets rows, model_scores,
trade job completion, the paper eligibility gate (promotion and demotion) and
simulate-final's guard."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

from host import eligibility
from host.errors import Conflict, Forbidden, NotFound
from host.exchange import settle
from host.trading import ledger, orders
from tests.conftest import insert_model, job_row, model_row
from tests.test_exchange import (
    NOW, bankroll, make_assignment, make_game, make_market, make_model, make_order, order, snap,
)

KICKOFF = NOW - timedelta(hours=4)


def _game(conn, game_id="2026_05_KC_LV", home="LV", away="KC", home_score=17, away_score=27, status="final"):
    return make_game(conn, game_id, home, away, KICKOFF, status=status, home_score=home_score, away_score=away_score)


def _filled(conn, assignment, market, price, size, fill_price=None, my_p=0.6, market_p=0.5, edge=0.05, worker_id=None):
    """An open order with one fill at `fill_price` (default: the order price)."""
    o = make_order(conn, assignment, market, price, size, status="open", submitted_at=KICKOFF - timedelta(hours=1), my_p=my_p, market_p=market_p, edge=edge, worker_id=worker_id)
    fp = price if fill_price is None else fill_price
    return orders.record_fill(conn, o["id"], fp, size, int(round(0.05 * fp * (1 - fp) * size * 100)), assignment["mode"], "paper")


def _bets(conn, assignment_id):
    return conn.execute("SELECT * FROM bets WHERE assignment_id = %s ORDER BY id", (assignment_id,)).fetchall()


def test_settle_win_loss_push_with_clv_and_bets_rows(conn):
    _game(conn)
    home = make_market(conn, "2026_05_KC_LV", "home")
    away = make_market(conn, "2026_05_KC_LV", "away")
    snap(conn, away["id"], 0.60, 0.62, KICKOFF - timedelta(minutes=5))
    snap(conn, away["id"], 0.70, 0.72, KICKOFF + timedelta(minutes=5))
    snap(conn, home["id"], 0.36, 0.38, KICKOFF - timedelta(minutes=5))
    model = make_model(conn)
    assignment = make_assignment(conn, "2026_05_KC_LV", model, bankroll_cents=10_000)
    win = _filled(conn, assignment, away, 0.58, 20, fill_price=0.56)
    loss = _filled(conn, assignment, home, 0.40, 10)
    pending = make_order(conn, assignment, home, 0.39, 10, status="open", submitted_at=KICKOFF - timedelta(minutes=30))
    before = bankroll(conn, assignment)
    assert before["open_cost_cents"] == 20 * 56 + 10 * 40
    result = settle.settle_game(conn, "2026_05_KC_LV", "test")
    assert result["winner"] == "away" and result["assignments"] == 1 and result["bets"] == 2
    bank = bankroll(conn, assignment)
    assert bank["open_cost_cents"] == 0 and bank["reserved_cents"] == 0
    assert bank["available_cents"] == 10_000 - (20 * 56 + 10 * 40) - 25 - 12 + 20 * 100, "fees 25 (0.56) and 12 (0.40)"
    assert bank["initial_cents"] + bank["realized_pnl_cents"] == bank["available_cents"]
    assert ledger.replay_problems(conn) == []
    assert order(conn, pending["id"])["status"] == "cancelled", "open orders are cancelled with release"
    rows = {r["order_id"]: r for r in _bets(conn, assignment["id"])}
    w, l = rows[win["id"]], rows[loss["id"]]
    assert (w["result"], w["side"], w["cost_cents"], w["fee_cents"], w["stake_cents"]) == ("win", "away", 1120, 25, 1145)
    assert float(w["entry_price"]) == 0.56 and float(w["closing_price"]) == 0.61 and w["clv"] == pytest.approx(0.05)
    assert w["pnl_cents"] == 2000 - 1120 - 25 and (w["my_p"], w["market_p"], w["edge"]) == pytest.approx((0.6, 0.5, 0.05))
    assert (w["mode"], w["platform"], w["contract"], w["game_id"], w["lineage_id"]) == ("paper", "sim", away["title"], "2026_05_KC_LV", model["lineage_id"])
    assert str(w["date"]) == str(KICKOFF.date()) or w["date"] is not None
    assert (l["result"], l["pnl_cents"], float(l["closing_price"])) == ("loss", -(400 + 12), 0.37)
    assert l["clv"] == pytest.approx(-0.03), "bought at 0.40, closed at 0.37: negative CLV"
    markets = {r["side"]: r for r in conn.execute("SELECT side, status, resolved_yes FROM markets").fetchall()}
    assert markets["away"]["resolved_yes"] is True and markets["home"]["resolved_yes"] is False and markets["home"]["status"] == "resolved"
    score = conn.execute("SELECT * FROM model_scores WHERE model_id = %s", (model["id"],)).fetchone()
    assert (score["n_bets"], score["stake_cents"], score["pnl_cents"], score["mode"]) == (2, 1145 + 412, (2000 - 1145) - 412, "paper")
    assert score["avg_clv"] == pytest.approx((0.05 * 1145 - 0.03 * 412) / (1145 + 412), abs=1e-6)
    a = conn.execute("SELECT status, settled_at FROM assignments WHERE id = %s", (assignment["id"],)).fetchone()
    assert a["status"] == "settled" and a["settled_at"] is not None
    job = job_row(conn, assignment["job_id"])
    assert job["status"] == "succeeded" and job["result"]["n_bets"] == 2 and job["result"]["pnl_cents"] == score["pnl_cents"]
    assert conn.execute("SELECT count(*) AS n FROM job_events WHERE job_id = %s AND event = 'succeeded'", (job["id"],)).fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'assignment_settled'").fetchone()["n"] == 1
    again = settle.settle_game(conn, "2026_05_KC_LV", "test")
    assert again["assignments"] == 0 and len(_bets(conn, assignment["id"])) == 2, "settling twice changes nothing"


def test_push_returns_the_basis_and_scores_zero(conn):
    _game(conn, home_score=24, away_score=24)
    home = make_market(conn, "2026_05_KC_LV", "home")
    snap(conn, home["id"], 0.50, 0.52, KICKOFF - timedelta(minutes=1))
    assignment = make_assignment(conn, "2026_05_KC_LV", make_model(conn))
    o = _filled(conn, assignment, home, 0.50, 10)
    result = settle.settle_game(conn, "2026_05_KC_LV")
    assert result["winner"] is None
    bet = _bets(conn, assignment["id"])[0]
    assert bet["result"] == "push" and bet["pnl_cents"] == -bet["fee_cents"] and bet["clv"] == pytest.approx(0.01)
    bank = bankroll(conn, assignment)
    assert bank["open_cost_cents"] == 0 and bank["available_cents"] == 10_000 - bet["fee_cents"] and ledger.replay_problems(conn) == []
    assert conn.execute("SELECT resolved_yes FROM markets WHERE id = %s", (home["id"],)).fetchone()["resolved_yes"] is None
    assert order(conn, o["id"])["status"] == "filled"


def test_settlement_needs_a_final_and_completes_a_leased_job_through_the_fence(conn, make_worker):
    worker = make_worker("trader", role="trade")
    _game(conn, status="scheduled", home_score=None, away_score=None)
    with pytest.raises(Conflict):
        settle.settle_game(conn, "2026_05_KC_LV")
    with pytest.raises(NotFound):
        settle.settle_game(conn, "nope")
    assignment = make_assignment(conn, "2026_05_KC_LV", make_model(conn), job_status="leased", worker_id=worker.id)
    halted = make_assignment(conn, "2026_05_KC_LV", make_model(conn), status="halted", job_status="cancel_requested", worker_id=worker.id)
    conn.execute("UPDATE games SET status = 'final', home_score = 3, away_score = 0 WHERE game_id = '2026_05_KC_LV'")
    result = settle.settle_game(conn, "2026_05_KC_LV")
    assert result["assignments"] == 2 and result["bets"] == 0
    for a in (assignment, halted):
        job = job_row(conn, a["job_id"])
        assert job["status"] == "succeeded" and job["lease_worker_id"] is None and job["result"]["n_bets"] == 0
        assert conn.execute("SELECT status FROM assignments WHERE id = %s", (a["id"],)).fetchone()["status"] == "settled"
    assert conn.execute("SELECT count(*) AS n FROM model_scores").fetchone()["n"] == 2, "a game without bets still counts as played"


def test_settle_due_picks_up_every_final_game(conn):
    _game(conn)
    _game(conn, "2026_05_SF_SEA", "SEA", "SF", status="scheduled", home_score=None, away_score=None)
    a1 = make_assignment(conn, "2026_05_KC_LV", make_model(conn))
    a2 = make_assignment(conn, "2026_05_SF_SEA", make_model(conn))
    done = settle.settle_due(conn)
    assert [d["game_id"] for d in done] == ["2026_05_KC_LV"]
    statuses = {str(r["id"]): r["status"] for r in conn.execute("SELECT id, status FROM assignments").fetchall()}
    assert statuses[str(a1["id"])] == "settled" and statuses[str(a2["id"])] == "active"
    assert settle.settle_due(conn) == []


def test_simulate_final_is_refused_outside_sim_without_dev(conn, monkeypatch):
    _game(conn, status="scheduled", home_score=None, away_score=None)
    assignment = make_assignment(conn, "2026_05_KC_LV", make_model(conn))
    conn.execute("UPDATE settings SET value = %s WHERE key = 'market_source'", (Jsonb("polymarket_us"),))
    monkeypatch.delenv("FLEET_DEV", raising=False)
    with pytest.raises(Forbidden):
        settle.simulate_final(conn, "2026_05_KC_LV", 20, 10, "owner")
    monkeypatch.setenv("FLEET_DEV", "1")
    with pytest.raises(NotFound):
        settle.simulate_final(conn, "unknown", 20, 10, "owner")
    result = settle.simulate_final(conn, "2026_05_KC_LV", 20, 10, "owner")
    assert result["winner"] == "home" and result["assignments"] == 1
    game = conn.execute("SELECT * FROM games WHERE game_id = '2026_05_KC_LV'").fetchone()
    assert game["status"] == "final" and (game["home_score"], game["away_score"]) == (20, 10) and game["raw"]["score_source"] == "sim"
    assert conn.execute("SELECT status FROM assignments WHERE id = %s", (assignment["id"],)).fetchone()["status"] == "settled"
    conn.execute("UPDATE settings SET value = %s WHERE key = 'market_source'", (Jsonb("sim"),))
    monkeypatch.delenv("FLEET_DEV", raising=False)
    _game(conn, "2026_05_SF_SEA", "SEA", "SF", status="scheduled", home_score=None, away_score=None)
    assert settle.simulate_final(conn, "2026_05_SF_SEA", 0, 0, None)["winner"] is None, "sim source needs no FLEET_DEV"


# ------------------------------------------------------------ paper eligibility

LIMITS = {"min_games": 2, "min_bets": 3, "min_days": 0, "min_clv": 0.0, "min_pnl_cents": 1}


def _score(conn, model, game_id, n_bets, pnl, clv, stake=1000, mode="paper", first_bet_days_ago=30):
    conn.execute(
        """
        INSERT INTO model_scores (model_id, game_id, mode, lineage_id, n_bets, stake_cents, pnl_cents, avg_clv)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (model_id, game_id, mode) DO UPDATE SET n_bets = EXCLUDED.n_bets, pnl_cents = EXCLUDED.pnl_cents, avg_clv = EXCLUDED.avg_clv
        """,
        (model["id"], game_id, mode, model["lineage_id"], n_bets, stake, pnl, clv),
    )
    if n_bets:
        o = conn.execute(
            "INSERT INTO orders (client_request_id, market_id, mode, price, size, cost_cents, status) "
            "SELECT %s, id, %s, 0.5, 1, 50, 'filled' FROM markets LIMIT 1 RETURNING id",
            (uuid.uuid4().hex[:32], mode),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO bets (order_id, assignment_id, model_id, lineage_id, game_id, mode, date, event, platform, contract,
                              side, entry_price, cost_cents, stake_cents, result, pnl_cents, settled_at)
            SELECT %s, a.id, %s, %s, %s, %s, current_date, 'e', 'sim', 'c', 'home', 0.5, 50, 50, 'win', %s, now() - make_interval(days => %s)
              FROM assignments a WHERE a.model_id = %s LIMIT 1
            """,
            (o["id"], model["id"], model["lineage_id"], game_id, mode, pnl, first_bet_days_ago, model["id"]),
        )


def test_paper_stats_and_thresholds(conn):
    assert eligibility.paper_thresholds(conn) == {"min_games": 10, "min_bets": 40, "min_days": 21, "min_clv": 0.0, "min_pnl_cents": 1}
    conn.execute("UPDATE settings SET value = %s WHERE key = 'thresholds_paper'", (Jsonb({"min_games": 2, "min_bets": 3}),))
    assert eligibility.paper_thresholds(conn)["min_games"] == 2 and eligibility.paper_thresholds(conn)["min_days"] == 21
    assert not eligibility.meets_paper_thresholds({"games": 5, "bets": 5, "days": 5, "avg_clv": None, "pnl_cents": 5}, LIMITS), "no CLV yet"
    ok = {"games": 2, "bets": 3, "days": 0, "avg_clv": 0.0, "pnl_cents": 1}
    assert eligibility.meets_paper_thresholds(ok, LIMITS), "inclusive at every boundary"
    for key, bad in (("games", 1), ("bets", 2), ("avg_clv", -0.001), ("pnl_cents", 0)):
        assert not eligibility.meets_paper_thresholds({**ok, key: bad}, LIMITS), key
    assert not eligibility.meets_paper_thresholds({**ok, "days": 20}, {**LIMITS, "min_days": 21})
    assert eligibility.paper_stats(conn, uuid.uuid4()) == {"games": 0, "bets": 0, "pnl_cents": 0, "avg_clv": None, "days": 0.0}


def test_paper_gate_promotes_pooled_lineage_and_demotion_halts_live(conn):
    conn.execute("UPDATE settings SET value = %s WHERE key = 'thresholds_paper'", (Jsonb(LIMITS),))
    _game(conn)
    _game(conn, "2026_05_SF_SEA", "SEA", "SF")
    make_market(conn, "2026_05_KC_LV", "home")
    root = make_model(conn)
    child = insert_model(conn, parent=root, trained_through=[2026, 4])
    make_assignment(conn, "2026_05_KC_LV", root)
    make_assignment(conn, "2026_05_SF_SEA", child)
    _score(conn, root, "2026_05_KC_LV", 2, 300, 0.02)
    assert eligibility.recompute_paper(conn, root["lineage_id"]) == "paper_ok", "one game, two bets: not yet"
    _score(conn, child, "2026_05_SF_SEA", 1, -100, 0.01)
    stats = eligibility.paper_stats(conn, root["lineage_id"])
    assert (stats["games"], stats["bets"], stats["pnl_cents"]) == (2, 3, 200) and stats["avg_clv"] == pytest.approx(0.015) and stats["days"] >= 29.9
    assert eligibility.recompute_paper(conn, root["lineage_id"]) == "live_eligible", "pooled over the lineage"
    assert {model_row(conn, m["id"])["status"] for m in (root, child)} == {"live_eligible"}
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'eligibility_changed'").fetchone()["n"] == 1
    # A live assignment on the now-eligible lineage, then the record sours: demoted, live halted, its orders cancelled.
    live = make_assignment(conn, "2026_05_KC_LV", child, mode="live", bankroll_cents=20_000)
    conn.execute("UPDATE games SET status = 'scheduled', home_score = NULL, away_score = NULL")
    live_order = make_order(conn, live, conn.execute("SELECT * FROM markets LIMIT 1").fetchone(), 0.5, 10)
    _score(conn, child, "2026_05_SF_SEA", 1, -400, -0.05)
    assert eligibility.recompute_paper(conn, root["lineage_id"]) == "paper_ok"
    assert {model_row(conn, m["id"])["status"] for m in (root, child)} == {"paper_ok"}
    assert conn.execute("SELECT status FROM assignments WHERE id = %s", (live["id"],)).fetchone()["status"] == "halted"
    assert order(conn, live_order["id"])["status"] == "cancelled" and bankroll(conn, live)["reserved_cents"] == 0
    assert ledger.replay_problems(conn) == []
    # The backtest gate still comes first: a lineage whose backtest fails is a candidate whatever its paper record.
    _score(conn, child, "2026_05_SF_SEA", 1, 500, 0.05)
    conn.execute("UPDATE models SET backtest_metrics = %s WHERE id = %s", (Jsonb({"n_bets": 1, "roi": 0.0, "max_drawdown": 0.9}), root["id"]))
    assert eligibility.recompute_paper(conn, root["lineage_id"]) == "candidate"
    assert eligibility.recompute_paper(conn, uuid.uuid4()) is None


def test_settlement_recomputes_eligibility_and_min_days_gate(conn):
    conn.execute("UPDATE settings SET value = %s WHERE key = 'thresholds_paper'", (Jsonb({**LIMITS, "min_games": 1, "min_bets": 1, "min_days": 10}),))
    _game(conn)
    away = make_market(conn, "2026_05_KC_LV", "away")
    snap(conn, away["id"], 0.60, 0.62, KICKOFF - timedelta(minutes=5))
    model = make_model(conn)
    assignment = make_assignment(conn, "2026_05_KC_LV", model)
    _filled(conn, assignment, away, 0.58, 10)
    result = settle.settle_game(conn, "2026_05_KC_LV")
    assert result["lineages"] == [{"lineage_id": str(model["lineage_id"]), "status": "paper_ok"}], "won with positive CLV but the first bet is minutes old"
    conn.execute("UPDATE bets SET settled_at = now() - interval '11 days'")
    assert eligibility.recompute_paper(conn, model["lineage_id"]) == "live_eligible"


def test_simulate_final_before_kickoff_still_freezes_the_closing_price(conn):
    """A simulated final on a game whose kickoff is still ahead: the closing price falls
    back to the last snapshot (the game was "played"), so every bet gets a CLV."""
    game = make_game(conn, "2026_05_SF_SEA", "SEA", "SF", NOW + timedelta(days=2), status="scheduled")
    assert game["kickoff_at"] > NOW
    away = make_market(conn, "2026_05_SF_SEA", "away")
    snap(conn, away["id"], 0.50, 0.52, NOW - timedelta(minutes=10))
    snap(conn, away["id"], 0.60, 0.62, NOW - timedelta(minutes=1))
    assignment = make_assignment(conn, "2026_05_SF_SEA", make_model(conn), bankroll_cents=10_000)
    won = _filled(conn, assignment, away, 0.52, 10)
    result = settle.simulate_final(conn, "2026_05_SF_SEA", 10, 20, "owner")
    assert result["winner"] == "away" and result["bets"] == 1
    market = conn.execute("SELECT closing_price FROM markets WHERE id = %s", (away["id"],)).fetchone()
    assert float(market["closing_price"]) == 0.61, "the last snapshot, there being none before the (future) kickoff"
    bet = _bets(conn, assignment["id"])[0]
    assert bet["order_id"] == won["id"] and bet["clv"] == pytest.approx(0.09) and bet["result"] == "win"
    assert ledger.replay_problems(conn) == []
