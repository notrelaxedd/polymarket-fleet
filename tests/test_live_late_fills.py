"""Live fills the ledger cannot book leave nothing behind (docs/TRADING.md "Selling
(step 6 Part B)", docs/LIVE.md fills poll): the fills row, the order update and any
ledger row roll back together, the fill is reported `late` and auto-kills, and after
RESUME the next poll reports the same fill late again (it is not skipped as seen)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from host import kill
from host.errors import Conflict
from host.exchange import live_sync, settle
from host.trading import ledger, orders, positions
from tests.sell_helpers import bought, ledger_rows, sell_order
from tests.test_exchange import NOW, bankroll, make_assignment, make_game, make_market, make_model, make_order

GAME = "2026_05_KC_LV"


def _fills(conn, order_id):
    return conn.execute("SELECT * FROM fills WHERE order_id = %s", (order_id,)).fetchall()


def _settled_with_open_sell(conn):
    """A live assignment holding 10 @ 0.50 with a live sell of 4 still on the exchange
    when the game settles (settlement leaves it cancel_requested)."""
    make_game(conn)
    market = make_market(conn, GAME, "home", platform="polymarket_us")
    a = make_assignment(conn, GAME, make_model(conn), mode="live", bankroll_cents=10_000)
    bought(conn, a, market, 0.50, 10, 5, submitted_at=NOW - timedelta(minutes=5))
    sell = sell_order(conn, a, market, 0.60, 4, status="open", submitted_at=NOW)
    conn.execute("UPDATE games SET status = 'final', home_score = 30, away_score = 10 WHERE game_id = %s", (GAME,))
    settle.settle_game(conn, GAME)
    assert orders.get_order(conn, sell["id"])["status"] == "cancel_requested"
    assert bankroll(conn, a)["open_cost_cents"] == 0
    return market, a, sell


def test_a_sell_fill_after_settlement_is_late_and_leaves_no_fills_row(conn, pool):
    market, a, sell = _settled_with_open_sell(conn)
    before = bankroll(conn, a)
    fill = {"client_order_id": sell["client_request_id"], "exchange_fill_id": "lf-1", "price": 0.61, "size": 4, "fee_cents": 5}
    with pool.connection() as c2:  # a pool connection commits on exit, as in the exchange task
        out = live_sync.record_fills(c2, [fill])
    assert out["recorded"] == 0 and out["late"] == ["lf-1"]
    assert _fills(conn, sell["id"]) == []
    assert ledger_rows(conn, sell["id"], "sell") == []
    assert orders.get_order(conn, sell["id"])["filled_size"] == 0
    assert positions.held(conn, a["id"], market["id"]) == (10, 500)
    after = bankroll(conn, a)
    assert {k: after[k] for k in ("available_cents", "open_cost_cents", "realized_pnl_cents")} == \
        {k: before[k] for k in ("available_cents", "open_cost_cents", "realized_pnl_cents")}
    assert kill.is_killed(conn)
    assert ledger.replay_problems(conn) == []

    kill.reset_kill(conn, "owner", "RESUME")
    assert not kill.is_killed(conn)
    with pool.connection() as c2:
        again = live_sync.record_fills(c2, [fill])
    assert again["late"] == ["lf-1"], "the refused fill is not skipped as already seen"
    assert kill.is_killed(conn), "a persisting late fill kills again after RESUME"
    assert _fills(conn, sell["id"]) == []


def test_record_fill_refuses_a_sell_on_a_resolved_market(conn):
    _, _, sell = _settled_with_open_sell(conn)
    with pytest.raises(Conflict, match="resolved"):
        orders.record_fill(conn, sell["id"], 0.61, 4, 5, "live", "test", exchange_fill_id="lf-2")
    assert _fills(conn, sell["id"]) == []


def test_a_buy_fill_whose_fee_overdraws_the_reservation_leaves_no_fills_row(conn, pool):
    make_game(conn, kickoff=NOW + timedelta(hours=4))
    home = make_market(conn, GAME, "home")
    a = make_assignment(conn, GAME, make_model(conn, status="live_eligible"), mode="live", bankroll_cents=10_000)
    o = make_order(conn, a, home, 0.40, 10, status="open", submitted_at=NOW - timedelta(minutes=5))
    conn.execute("UPDATE orders SET exchange_order_id = 'EXB' WHERE id = %s", (o["id"],))
    before = bankroll(conn, a)
    fill = {"exchange_fill_id": "FB", "exchange_order_id": "EXB", "size": 10, "price": 0.40, "fee_cents": 500}
    with pool.connection() as c2:
        out = live_sync.record_fills(c2, [fill])
    assert out["recorded"] == 0 and out["late"] == ["FB"]
    assert _fills(conn, o["id"]) == []
    assert ledger_rows(conn, o["id"], "fill") == []
    order = orders.get_order(conn, o["id"])
    assert (order["status"], order["filled_size"]) == ("cancel_requested", 0), "unfilled; the auto-kill asked to cancel it"
    assert positions.held(conn, a["id"], home["id"]) == (0, 0)
    after = bankroll(conn, a)
    assert (after["reserved_cents"], after["open_cost_cents"]) == (before["reserved_cents"], before["open_cost_cents"])
    assert kill.is_killed(conn)
    assert ledger.replay_problems(conn) == []


def test_record_fill_rolls_back_its_fills_row_when_the_ledger_refuses(conn):
    """The savepoint is in record_fill itself, so any caller that catches the Conflict
    and commits is safe, not only the fills poll."""
    make_game(conn, kickoff=NOW + timedelta(hours=4))
    home = make_market(conn, GAME, "home")
    a = make_assignment(conn, GAME, make_model(conn, status="live_eligible"), mode="live", bankroll_cents=10_000)
    o = make_order(conn, a, home, 0.40, 10, status="open", submitted_at=NOW - timedelta(minutes=5))
    with conn.transaction():  # stands in for a caller's own transaction
        with pytest.raises(ledger.LedgerError):
            orders.record_fill(conn, o["id"], 0.40, 10, 500, "live", "test", exchange_fill_id="FX")
        assert _fills(conn, o["id"]) == []
    assert _fills(conn, o["id"]) == []
    assert orders.get_order(conn, o["id"])["filled_size"] == 0

