"""Sells under the kill switch and on the live path (docs/TRADING.md "Selling (step 6
Part B)", docs/LIVE.md): a kill cancels open sells like buys (paper at once, live
`cancel_requested`) with nothing to release; the live gateway sends `side_sell`
("SELL" by default) for a sell order and the executor hands it the order's side; a
live sell fill is booked as a sale."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from host import kill
from host.exchange import live_sync
from host.exchange.adapters.base import NotConfigured
from host.exchange.adapters.polymarket_us import live_config_with_defaults
from host.exchange.executor import Executor
from host.trading import ledger, orders, positions
from tests.sell_helpers import bought, ledger_rows, sell_order
from tests.test_exchange import NOW, bankroll, make_assignment, make_game, make_market, make_model, make_order
from tests.test_live_gateway import FakeHttp, fixture, gateway, order

GAME = "2026_05_KC_LV"


def _held(conn, mode: str = "paper", holds: int = 20):
    make_game(conn)
    market = make_market(conn, GAME, "home", platform="polymarket_us" if mode == "live" else "sim")
    assignment = make_assignment(conn, GAME, make_model(conn), mode=mode, bankroll_cents=10_000)
    bought(conn, assignment, market, 0.50, holds, 5, submitted_at=NOW - timedelta(minutes=5))
    return market, assignment


def test_kill_cancels_open_sells_like_buys_with_nothing_to_release(conn):
    market, assignment = _held(conn)
    open_sell = sell_order(conn, assignment, market, 0.55, 10, status="open", submitted_at=NOW)
    approved_sell = sell_order(conn, assignment, market, 0.56, 5, status="approved")
    partial = sell_order(conn, assignment, market, 0.55, 4, status="open", submitted_at=NOW)
    orders.record_fill(conn, partial["id"], 0.55, 2, 1, "paper", "test")
    buy = make_order(conn, assignment, market, 0.50, 4, status="open", submitted_at=NOW)
    live_market = make_market(conn, GAME, "away", platform="polymarket_us")
    live_assignment = make_assignment(conn, GAME, make_model(conn), mode="live", bankroll_cents=10_000)
    bought(conn, live_assignment, live_market, 0.40, 10, 3)
    live_sell = sell_order(conn, live_assignment, live_market, 0.45, 10, status="open", submitted_at=NOW)
    reserved_before = bankroll(conn, assignment)["reserved_cents"]

    assert kill.set_kill(conn, "owner") is True
    status = {o["id"]: orders.get_order(conn, o["id"])["status"] for o in (open_sell, approved_sell, partial, buy, live_sell)}
    assert status[open_sell["id"]] == status[approved_sell["id"]] == status[partial["id"]] == "cancelled"
    assert status[buy["id"]] == "cancelled" and status[live_sell["id"]] == "cancel_requested"
    for o in (open_sell, approved_sell, live_sell):
        assert ledger_rows(conn, o["id"]) == [], "a sell reserved nothing, so the kill releases nothing"
    assert [r["kind"] for r in ledger_rows(conn, partial["id"])] == ["sell"], "the part sold stays sold"
    assert bankroll(conn, assignment)["reserved_cents"] == reserved_before - order_reservation(conn, buy)
    assert positions.held(conn, assignment["id"], market["id"])[0] == 18, "kill means cancel only: the position stays"
    events = conn.execute("SELECT detail FROM order_events WHERE order_id = %s ORDER BY id DESC LIMIT 1", (open_sell["id"],)).fetchone()
    assert events["detail"] == {"reason": "kill"}
    assert ledger.replay_problems(conn) == []


def order_reservation(conn, buy) -> int:
    """What the buy's kill release gave back."""
    return sum(-r["d_reserved"] for r in ledger_rows(conn, buy["id"], "release"))


def test_live_gateway_sends_side_sell_for_a_sell():
    assert live_config_with_defaults({})["live"]["side_sell"] == "SELL"
    http = FakeHttp((200, fixture("place")))
    gw = gateway(http)
    assert gw.place(order(side="sell")) == "ord-7f3a9c"
    assert json.loads(http.last["body"])["side"] == "SELL"
    gw.place(order(side="buy"))
    assert json.loads(http.last["body"])["side"] == "BUY"
    gw.place(order())
    assert json.loads(http.last["body"])["side"] == "BUY", "a row without a side is a buy"
    renamed = gateway(http, {"live": {"side_sell": "ask", "side_buy": "bid"}})
    renamed.place(order(side="sell"))
    assert json.loads(http.last["body"])["side"] == "ask"
    calls = len(http.calls)
    with pytest.raises(NotConfigured):
        gateway(http, {"live": {"side_sell": None}}).place(order(side="sell"))
    assert len(http.calls) == calls, "an unconfigured sell side is never sent"


def test_executor_places_a_live_sell_with_side_sell(conn):
    market, assignment = _held(conn, mode="live")
    row = sell_order(conn, assignment, market, 0.55, 5, status="approved")
    http = FakeHttp((200, fixture("place")))
    executor = Executor(live_gateway=gateway(http))
    assert executor.submit_approved(conn, NOW) == 1
    body = json.loads(http.last["body"])
    assert body["side"] == "SELL" and body["size"] == 5 and body["market_id"] == market["market_ref"]
    placed = orders.get_order(conn, row["id"])
    assert placed["status"] == "open" and placed["exchange_order_id"] == "ord-7f3a9c"
    assert ledger_rows(conn, row["id"]) == [], "placing a sell moves no money"


def test_live_sell_fill_is_booked_as_a_sale(conn):
    market, assignment = _held(conn, mode="live", holds=10)
    row = sell_order(conn, assignment, market, 0.60, 4, status="open", submitted_at=NOW)
    out = live_sync.record_fills(conn, [{"client_order_id": row["client_request_id"], "exchange_fill_id": "lf-1", "price": 0.61, "size": 4, "fee_cents": 5}])
    assert out["recorded"] == 1 and out["late"] == []
    sale = ledger_rows(conn, row["id"], "sell")
    assert len(sale) == 1 and (sale[0]["d_open"], sale[0]["d_available"], sale[0]["d_realized"]) == (-200, 244 - 5, 244 - 5 - 200)
    assert positions.held(conn, assignment["id"], market["id"]) == (6, 300)
    assert ledger.replay_problems(conn) == []
