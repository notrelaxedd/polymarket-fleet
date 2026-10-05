"""Settlement after sales (docs/TRADING.md "Selling (step 6 Part B)"): a sell order's
own `sold` bets row, buy rows that cover only the contracts still held (pro rata, the
rounding residual on the last row), a ledger settle on the remaining basis, and
model_scores with sells in the P&L but only buys in n_bets and the CLV. Totals are
checked by hand to the cent."""
from __future__ import annotations

from datetime import timedelta

import pytest

from host.exchange import settle, settle_sells
from host.trading import ledger
from tests.sell_helpers import bought, sold
from tests.test_exchange import NOW, bankroll, make_assignment, make_game, make_market, make_model, snap

GAME = "2026_05_KC_LV"
KICKOFF = NOW - timedelta(hours=4)


def _final(conn, home_score: int, away_score: int):
    make_game(conn, GAME, "LV", "KC", KICKOFF, status="final", home_score=home_score, away_score=away_score)
    home = make_market(conn, GAME, "home")
    snap(conn, home["id"], 0.54, 0.56, KICKOFF - timedelta(minutes=5))
    model = make_model(conn)
    assignment = make_assignment(conn, GAME, model, bankroll_cents=10_000)
    return home, model, assignment


def _bets(conn, assignment):
    return {r["order_id"]: r for r in conn.execute("SELECT * FROM bets WHERE assignment_id = %s ORDER BY id", (assignment["id"],)).fetchall()}


def _settle_rows(conn, assignment):
    return conn.execute(
        "SELECT * FROM ledger WHERE kind = 'settle' AND ref_id = %s ORDER BY id", (str(assignment["id"]),)
    ).fetchall()


def test_split_puts_the_residual_on_the_last_row():
    assert settle_sells.split(390, [400, 250]) == [240, 150]
    assert settle_sells.split(100, [1, 1, 1]) == [33, 33, 34]
    assert settle_sells.split(0, [5, 7]) == [0, 0]
    assert settle_sells.split(7, [0, 0]) == [3, 4], "equal shares when every weight is zero"
    assert settle_sells.split(5, []) == []


def test_win_after_a_partial_sale_has_exact_totals(conn):
    home, model, assignment = _final(conn, 27, 17)
    lot_a = bought(conn, assignment, home, 0.40, 10, 12, my_p=0.6, market_p=0.5, edge=0.05)
    lot_b = bought(conn, assignment, home, 0.50, 5, 6)
    sale = sold(conn, assignment, home, 0.60, 6, 7)
    assert bankroll(conn, assignment)["open_cost_cents"] == 650 - 260, "650 * 6 / 15 = 260 sold"
    result = settle.settle_game(conn, GAME, "test")
    assert result["bets"] == 2 and result["pnl_cents"] == 348 + 144 + 93
    rows = _bets(conn, assignment)
    a, b, s = rows[lot_a["id"]], rows[lot_b["id"]], rows[sale["id"]]
    # Remaining 9 contracts, basis 390: split 240/150 by bought basis, payout 900 split 600/300 by contracts.
    assert (a["order_side"], a["result"], a["cost_cents"], a["fee_cents"], a["stake_cents"], a["pnl_cents"]) == ("buy", "win", 240, 12, 412, 600 - 240 - 12)
    assert (b["order_side"], b["result"], b["cost_cents"], b["fee_cents"], b["stake_cents"], b["pnl_cents"]) == ("buy", "win", 150, 6, 256, 300 - 150 - 6)
    assert float(a["entry_price"]) == 0.40 and a["clv"] == pytest.approx(0.55 - 0.40)
    assert (s["order_side"], s["result"], s["cost_cents"], s["fee_cents"], s["stake_cents"], s["pnl_cents"]) == ("sell", "sold", 260, 7, 0, 360 - 7 - 260)
    assert float(s["entry_price"]) == 0.60 and s["clv"] is None and s["side"] == "home"
    settle_row = _settle_rows(conn, assignment)
    assert len(settle_row) == 1 and (settle_row[0]["d_open"], settle_row[0]["d_available"]) == (-390, 900)
    bank = bankroll(conn, assignment)
    assert bank["open_cost_cents"] == 0 and bank["reserved_cents"] == 0
    assert bank["realized_pnl_cents"] == 585 == sum(r["pnl_cents"] for r in rows.values()), "the bets add up to the ledger"
    assert bank["available_cents"] == 10_000 + 585
    assert ledger.replay_problems(conn) == []
    score = conn.execute("SELECT * FROM model_scores WHERE model_id = %s", (model["id"],)).fetchone()
    assert (score["n_bets"], score["stake_cents"], score["pnl_cents"]) == (2, 412 + 256, 585)
    assert score["avg_clv"] == pytest.approx((0.15 * 412 + 0.05 * 256) / (412 + 256), abs=1e-6), "CLV over buys only"


def test_loss_and_push_after_partial_sales(conn):
    home, model, assignment = _final(conn, 17, 27)
    lot = bought(conn, assignment, home, 0.52, 3, 4)
    sale = sold(conn, assignment, home, 0.58, 1, 1)
    settle.settle_game(conn, GAME, "test")
    rows = _bets(conn, assignment)
    assert rows[sale["id"]]["cost_cents"] == 52 and rows[sale["id"]]["pnl_cents"] == 58 - 1 - 52
    assert (rows[lot["id"]]["result"], rows[lot["id"]]["cost_cents"], rows[lot["id"]]["pnl_cents"]) == ("loss", 104, -104 - 4)
    bank = bankroll(conn, assignment)
    assert bank["realized_pnl_cents"] == sum(r["pnl_cents"] for r in rows.values()) == 5 - 108
    assert ledger.replay_problems(conn) == []

    other = "2026_05_NE_NYJ"
    make_game(conn, other, "NYJ", "NE", KICKOFF, status="final", home_score=20, away_score=20)
    market = make_market(conn, other, "home")
    tie = make_assignment(conn, other, make_model(conn), bankroll_cents=10_000)
    lot = bought(conn, tie, market, 0.50, 4, 5)
    sold(conn, tie, market, 0.45, 1, 1)
    settle.settle_game(conn, other, "test")
    row = _bets(conn, tie)[lot["id"]]
    assert (row["result"], row["cost_cents"], row["pnl_cents"]) == ("push", 150, -5), "a push returns the remaining basis"
    assert bankroll(conn, tie)["realized_pnl_cents"] == -5 + (45 - 1 - 50)
    assert ledger.replay_problems(conn) == []


def test_fully_sold_position_settles_without_a_settle_row(conn):
    home, model, assignment = _final(conn, 27, 17)
    lot = bought(conn, assignment, home, 0.40, 5, 6)
    sale = sold(conn, assignment, home, 0.70, 5, 5)
    settle.settle_game(conn, GAME, "test")
    rows = _bets(conn, assignment)
    assert (rows[lot["id"]]["cost_cents"], rows[lot["id"]]["pnl_cents"]) == (0, -6), "nothing held: only the buy fee"
    assert rows[sale["id"]]["pnl_cents"] == 350 - 5 - 200
    assert _settle_rows(conn, assignment) == [], "no remaining basis, no payout"
    bank = bankroll(conn, assignment)
    assert bank["realized_pnl_cents"] == 139 and bank["open_cost_cents"] == 0
    score = conn.execute("SELECT n_bets, pnl_cents FROM model_scores WHERE model_id = %s", (model["id"],)).fetchone()
    assert (score["n_bets"], score["pnl_cents"]) == (1, 139)
    assert ledger.replay_problems(conn) == []
