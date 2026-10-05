"""Sell approval (host/trading/sells.py, docs/TRADING.md "Selling"): every reason code,
no reservation and no money checks for sells, idempotency, the per-mode lock under
concurrency, the API's order_side and the trade state payload additions."""
from __future__ import annotations

import threading
from typing import Any

import psycopg

from host.trading import ledger, orders
from host.trading.limits import approve_order
from host.trading.sells import approve_sell, bid_depth_at_or_above, sell_fee_cents
from host.trading.state import trade_state
from tests.conftest import (
    GAME_ID, TradeSetup, approve, approved_order, bankroll_of, insert_market, insert_snapshot, order_events, order_row,
    post_loss, set_setting, trade_setup, worker_row,
)

FEES = {"taker_rate": 0.05, "half_spread": 0.01}


def hold(conn: psycopg.Connection, s: TradeSetup, size: int = 10, price: float = 0.52, **kw: Any) -> dict[str, Any]:
    """Buy `size` contracts through the normal approval and fill them (paper)."""
    row = approved_order(conn, s, price=price, size=size, **kw)
    orders.set_status(conn, row["id"], "open", "test", None, expected=("approved",))
    fee = order_row(conn, row["id"])["fee_cents_est"]
    return orders.record_fill(conn, row["id"], price, size, int(fee), row["mode"], "test", snapshot_id=row["snapshot_id"])


def sell(conn: psycopg.Connection, s: TradeSetup, price: float = 0.50, size: int = 4, **kw: Any) -> dict[str, Any]:
    """Run a sell request through approve_order (the dispatch path the API uses)."""
    return approve_order(conn, worker_row(conn, s.worker.id), s.body(price=price, size=size, order_side="sell", **kw))


def ledger_count(conn: psycopg.Connection) -> int:
    return conn.execute("SELECT count(*) AS n FROM ledger").fetchone()["n"]


# ------------------------------------------------------------------ approval


def test_sell_is_approved_reserves_nothing_and_is_logged(conn):
    s = trade_setup(conn)
    hold(conn, s, 10)
    before, rows = dict(bankroll_of(conn, s.assignment)), ledger_count(conn)
    decision = sell(conn, s, size=4)
    assert decision == {"status": "approved", "order_id": decision["order_id"], "reason": None}
    row = order_row(conn, decision["order_id"])
    assert row["side"] == "sell" and row["status"] == "approved" and row["cost_cents"] == 0
    assert row["fee_cents_est"] == sell_fee_cents(0.50, 4, FEES) == 5
    assert row["price"] == 0.50 and row["size"] == 4 and row["snapshot_id"] == s.snapshot["id"]
    assert dict(bankroll_of(conn, s.assignment)) == before, "a sell reserves nothing"
    assert ledger_count(conn) == rows and ledger.replay_problems(conn) == []
    assert order_events(conn, row["id"]) == ["approved"]
    event = conn.execute("SELECT detail FROM order_events WHERE order_id = %s", (row["id"],)).fetchone()["detail"]
    assert event["order_side"] == "sell" and event["cost_cents"] == 0
    buy = conn.execute("SELECT side FROM orders WHERE id <> %s", (row["id"],)).fetchone()
    assert buy["side"] == "buy", "a buy stores side 'buy'"


def test_sell_client_request_id_is_idempotent(conn):
    s = trade_setup(conn)
    hold(conn, s, 10)
    body = s.body(price=0.50, size=4, order_side="sell")
    worker = worker_row(conn, s.worker.id)
    first = approve_order(conn, worker, body)
    again = approve_order(conn, worker, body)
    assert again["order_id"] == first["order_id"] and again["duplicate"] is True and again["status"] == "approved"
    direct = approve_sell(conn, worker, body)
    assert direct["order_id"] == first["order_id"] and direct["duplicate"] is True
    assert conn.execute("SELECT count(*) AS n FROM orders WHERE side = 'sell'").fetchone()["n"] == 1
    rejected = approve_order(conn, worker, s.body(price=0.50, size=40, order_side="sell", client_request_id="rej-1"))
    replay = approve_order(conn, worker, s.body(price=0.50, size=1, order_side="sell", client_request_id="rej-1"))
    assert rejected["reason"] == "sell_exceeds_position" and replay == dict(rejected, duplicate=True)


def test_no_position(conn):
    s = trade_setup(conn)
    assert sell(conn, s)["reason"] == "no_position"
    approved_order(conn, s, size=2)  # an unfilled buy is not a position
    assert sell(conn, s)["reason"] == "no_position"
    other = insert_market(conn, GAME_ID, side="away")
    snap = insert_snapshot(conn, other["id"])
    hold(conn, s, 3, market_id=str(other["id"]), snapshot_id=snap["id"])
    assert sell(conn, s)["reason"] == "no_position", "a position on another market does not count"
    decision = sell(conn, s, size=3, market_id=str(other["id"]), snapshot_id=snap["id"])
    assert decision["status"] == "approved", decision


def test_sell_exceeds_position_and_open_sell_exists(conn):
    s = trade_setup(conn)
    hold(conn, s, 10)
    assert sell(conn, s, size=11)["reason"] == "sell_exceeds_position", "no shorting"
    first = sell(conn, s, size=3)
    assert first["status"] == "approved"
    assert sell(conn, s, size=8)["reason"] == "sell_exceeds_position", "10 held - 3 offered = 7"
    assert sell(conn, s, size=2)["reason"] == "open_sell_exists", "one open sell per market"
    orders.cancel_order(conn, first["order_id"], "test", "free the market")
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == 0, "cancelling a sell releases nothing"
    assert ledger.replay_problems(conn) == []
    assert sell(conn, s, size=10)["status"] == "approved", "the whole position may be sold"


def test_kill_lease_assignment_and_market(conn):
    s = trade_setup(conn)
    hold(conn, s, 10)
    set_setting(conn, "kill_switch", True)
    assert sell(conn, s)["reason"] == "killed"
    set_setting(conn, "kill_switch", False)
    assert sell(conn, s, lease_token="00000000-0000-0000-0000-000000000000")["reason"] == "lease"
    conn.execute("UPDATE assignments SET status = 'halted' WHERE id = %s", (s.assignment["id"],))
    assert sell(conn, s)["reason"] == "assignment"
    conn.execute("UPDATE assignments SET status = 'active' WHERE id = %s", (s.assignment["id"],))
    conn.execute("UPDATE markets SET mapping_confirmed = false WHERE id = %s", (s.market["id"],))
    assert sell(conn, s)["reason"] == "market"
    conn.execute("UPDATE markets SET mapping_confirmed = true, status = 'resolved' WHERE id = %s", (s.market["id"],))
    assert sell(conn, s)["reason"] == "market"
    conn.execute("UPDATE markets SET status = 'open' WHERE id = %s", (s.market["id"],))
    assert sell(conn, s)["status"] == "approved"
    rejected = conn.execute("SELECT reject_reason FROM orders WHERE side = 'sell' AND status = 'rejected' ORDER BY created_at").fetchall()
    assert [r["reject_reason"] for r in rejected] == ["killed", "lease", "assignment", "market", "market"], "rejections are stored"


def test_kickoff_cutoff_applies_to_sells(conn):
    s = trade_setup(conn)
    hold(conn, s, 10)
    conn.execute("UPDATE games SET kickoff_at = now() - interval '1 minute' WHERE game_id = %s", (GAME_ID,))
    assert sell(conn, s)["reason"] == "kickoff"


def test_mode_gate_for_live_sells(conn):
    s = trade_setup(conn, mode="live", model_status="live_eligible")
    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'live_enabled'")
    assert sell(conn, s)["reason"] == "mode"


def test_stale_book(conn):
    s = trade_setup(conn)
    hold(conn, s, 10)
    old = insert_snapshot(conn, s.market["id"], age_s=600)
    assert sell(conn, s, snapshot_id=old["id"])["reason"] == "stale_book"
    conn.execute("UPDATE price_snapshots SET ts = now() - interval '10 minutes' WHERE market_id = %s", (s.market["id"],))
    assert sell(conn, s)["reason"] == "stale_book", "the newest book is still a dead book"


def test_participation_is_on_the_bid_side(conn):
    s = trade_setup(conn)
    hold(conn, s, 10)
    thin = insert_snapshot(conn, s.market["id"], bid_depth=[[0.50, 6], [0.49, 1000]], ask_depth=[[0.52, 10_000]])
    assert sell(conn, s, size=4, snapshot_id=thin["id"])["reason"] == "participation", "half of the 6 bid at 0.50"
    assert sell(conn, s, size=3, snapshot_id=thin["id"])["status"] == "approved"


def test_ask_depth_does_not_limit_a_sell(conn):
    s = trade_setup(conn)
    hold(conn, s, 10)
    snap = insert_snapshot(conn, s.market["id"], ask_depth=[[0.52, 1]], bid_depth=[[0.50, 20]])
    assert sell(conn, s, size=10, snapshot_id=snap["id"])["status"] == "approved"
    assert bid_depth_at_or_above([[0.50, 20], [0.49, 5], {"price": 0.51, "size": 2}, ["x"]], 0.50) == 22


def test_price_band_is_bid_minus_five_cents(conn):
    s = trade_setup(conn)
    hold(conn, s, 10)
    assert sell(conn, s, price=0.44)["reason"] == "price_band", "0.44 < 0.50 - 0.05"
    assert sell(conn, s, price=0.455)["reason"] == "price_band", "off the tick"
    decision = sell(conn, s, price=0.45, size=4)
    assert decision["status"] == "approved", "0.45 is inside the band"


def test_no_money_checks_on_sells(conn):
    """No reservation, so no max bet, bankroll, daily-loss, exposure or liquidity check."""
    s = trade_setup(conn)
    hold(conn, s, 10)
    bank = bankroll_of(conn, s.assignment)
    post_loss(conn, bank["id"], int(bank["available_cents"]))
    set_setting(conn, "max_bet_cents", 1)
    set_setting(conn, "max_exposure_cents", {"live": 1, "paper": 1})
    set_setting(conn, "liquidity_floor_cents", 10**9)
    buy = approve(conn, s, size=1)
    assert buy["status"] == "rejected" and buy["reason"] in ("liquidity", "max_bet", "bankroll", "daily_loss")
    decision = sell(conn, s, size=10)
    assert decision["status"] == "approved", decision
    assert ledger.replay_problems(conn) == []


def test_buys_keep_their_checks_with_an_explicit_order_side(conn):
    s = trade_setup(conn)
    decision = approve(conn, s, size=50, order_side="buy")
    assert decision["reason"] == "max_bet"
    assert order_row(conn, decision["order_id"])["side"] == "buy"


def test_concurrent_sells_cannot_oversell(pool, conn):
    s = trade_setup(conn)
    hold(conn, s, 10)
    worker = worker_row(conn, s.worker.id)
    bodies = [s.body(price=0.50, size=6, order_side="sell") for _ in range(8)]
    results: list[Any] = [None] * 8
    barrier = threading.Barrier(8)

    def run(i: int) -> None:
        barrier.wait(timeout=10)
        with pool.connection() as c:
            results[i] = approve_order(c, worker, bodies[i])

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sum(1 for r in results if r and r["status"] == "approved") == 1
    assert {r["reason"] for r in results if r and r["status"] == "rejected"} <= {"sell_exceeds_position", "open_sell_exists"}


# ------------------------------------------------------------------ API and state


def test_order_side_over_http(client, conn):
    s = trade_setup(conn)
    hold(conn, s, 10)
    r = client.post("/api/v1/orders/request", json=s.body(price=0.50, size=4, order_side="sell", side="home"),
                    headers=s.worker.headers)
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    assert order_row(conn, r.json()["order_id"])["side"] == "sell"
    bad = client.post("/api/v1/orders/request", json=s.body(order_side="short"), headers=s.worker.headers)
    assert bad.status_code in (400, 422)
    plain = client.post("/api/v1/orders/request", json=s.body(size=1), headers=s.worker.headers)
    assert plain.status_code == 200 and order_row(conn, plain.json()["order_id"])["side"] == "buy", "default is buy"


def test_state_payload_carries_bid_depth_order_side_positions_and_signals(conn):
    s = trade_setup(conn)
    hold(conn, s, 10)
    open_sell = sell(conn, s, size=4)
    state = trade_state(conn, s.worker.id)
    a = state["assignments"][0]
    market = a["markets"][0]
    assert market["bid_depth"] == [[0.50, 500], [0.49, 500]]
    assert [(str(o["id"]), o["side"]) for o in a["open_orders"]] == [(open_sell["order_id"], "sell")]
    assert len(a["positions"]) == 1
    position = a["positions"][0]
    assert str(position["market_id"]) == str(s.market["id"]) and position["side"] == "home" and position["size"] == 10
    assert position["basis_cents"] == 520
    assert set(a["game"]) >= {"signals", "team_stats"}, "host.signals.game_signals is merged into the game"
    assert set(a["game"]["team_stats"]) == {"home", "away"}
