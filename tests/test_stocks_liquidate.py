"""Step 9 fixes on /stocks: "Sell all" of a halted assignment (the way out for the shares
of a retired or demoted model, so it can be closed), the gate verdict in words on a
model row, the decision window check of the Stocks settings group, the pattern day
trader chip, the average fill price in dollars and the newest informational backtest
in a model row's menu. Rows are inserted straight into the tables."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from psycopg.types.json import Jsonb

from host.exchange.stock_fills import book_fill
from tests.conftest import flash_cookie
from tests.pagecheck import page
from tests.test_stocks_page import add_assignment, add_model, add_order, add_symbol, now, set_broker
from tests.test_stocks_settings_page import GOOD


def holding(conn, mode: str = "paper", qty: int = 40) -> dict[str, Any]:
    """A broker of `mode`, SPY and QQQ with bars, a model with an active assignment that
    holds `qty` SPY, then the model retired from the dashboard (the assignment halts)."""
    add_symbol(conn, "SPY", [440.0, 450.0])
    add_symbol(conn, "QQQ", [370.0, 380.0])
    set_broker(conn, environment=mode)
    model = add_model(conn, "live_eligible" if mode == "live" else "paper_ok")
    aid = add_assignment(conn, model, mode=mode)
    conn.execute("INSERT INTO stock_positions (assignment_id, symbol, qty, cost_cents) VALUES (%s, 'SPY', %s, %s)",
                 (aid, qty, qty * 44000))
    return {"model": model, "assignment": aid}


def retire(client, conn, model: int, aid: int) -> None:
    r = client.post(f"/stocks/models/{model}/retire", follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == f"stock model {model} retired"
    assert conn.execute("SELECT status FROM stock_assignments WHERE id = %s", (aid,)).fetchone()["status"] == "halted"


def sells(conn, aid: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM stock_orders WHERE assignment_id = %s AND side = 'sell' ORDER BY created_at", (aid,)).fetchall()]


def test_sell_all_unblocks_a_retired_models_assignment(client, conn):
    h = holding(conn)
    aid = h["assignment"]
    retire(client, conn, h["model"], aid)
    r = client.post(f"/stocks/assignments/{aid}/resume", follow_redirects=False)
    assert flash_cookie(r).startswith("resume refused: "), "a retired model never trades again"
    r = client.post(f"/stocks/assignments/{aid}/close", follow_redirects=False)
    assert "sell them first" in flash_cookie(r)
    row = page(client.get("/stocks").text).row("stock-assignment", aid)
    assert [a.attrs["data-action"] for a in row.select("[data-action]")] == ["resume", "liquidate", "close"]
    assert "SPY" in row.one('[data-action="liquidate"]').attrs["data-confirm"]

    r = client.post(f"/stocks/assignments/{aid}/liquidate", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/stocks#assignments"
    assert flash_cookie(r).startswith(f"stock assignment {aid}: selling 40 SPY at the close of ")
    [o] = sells(conn, aid)
    session = conn.execute("SELECT session_date FROM stock_broker_state WHERE id = 1").fetchone()["session_date"]
    assert o["status"] == "approved" and o["qty"] == 40 and o["reserved_cents"] == 0
    assert o["client_request_id"] == f"liquidate:{session.isoformat()}:SPY" and o["session_date"] == session
    assert o["ref_price_cents"] == 45000 and o["rationale"] == "owner liquidation"
    event = conn.execute("SELECT actor, to_status FROM stock_order_events WHERE order_id = %s", (o["id"],)).fetchone()
    assert event["actor"] == "owner:dev" and event["to_status"] == "approved"
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'stock_assignment_liquidate'"
                        " AND entity = %s", (f"stock_assignment:{aid}",)).fetchone()["n"] == 1

    again = client.post(f"/stocks/assignments/{aid}/liquidate", follow_redirects=False)
    assert "already in an open sell" in flash_cookie(again) and len(sells(conn, aid)) == 1, "a double submit adds nothing"

    # the executor places it, Alpaca fills it at the close, the fill is booked: Close succeeds
    conn.execute("UPDATE stock_orders SET status = 'open', exchange_order_id = 'x1' WHERE id = %s", (o["id"],))
    assert book_fill(conn, o["id"], 40, 451.25, "x1:40", now(conn))
    assert conn.execute("SELECT status FROM stock_orders WHERE id = %s", (o["id"],)).fetchone()["status"] == "filled"
    r = client.post(f"/stocks/assignments/{aid}/close", follow_redirects=False)
    assert flash_cookie(r) == f"stock assignment {aid} closed"
    a = conn.execute("SELECT status, cash_cents FROM stock_assignments WHERE id = %s", (aid,)).fetchone()
    assert a["status"] == "closed" and a["cash_cents"] == 700_000 + 40 * 45125


def test_sell_all_keeps_the_other_gates(client, conn):
    h = holding(conn)
    aid = h["assignment"]
    r = client.post(f"/stocks/assignments/{aid}/liquidate", follow_redirects=False)
    assert "only a halted assignment can be sold out; halt it first" in flash_cookie(r)
    retire(client, conn, h["model"], aid)
    conn.execute("UPDATE settings SET value = 'true' WHERE key = 'kill_switch'")
    assert not page(client.get("/stocks").text).row("stock-assignment", aid).has('[data-action="liquidate"]')
    r = client.post(f"/stocks/assignments/{aid}/liquidate", follow_redirects=False)
    assert flash_cookie(r) == "sell all refused: the kill switch is on; reset it first"
    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'kill_switch'")
    set_broker(conn, next_close=now(conn) + timedelta(minutes=5))
    r = client.post(f"/stocks/assignments/{aid}/liquidate", follow_redirects=False)
    assert flash_cookie(r) == "sell all refused: the sell of 40 SPY fails the moc_cutoff check"
    set_broker(conn, market_open=False)
    r = client.post(f"/stocks/assignments/{aid}/liquidate", follow_redirects=False)
    assert flash_cookie(r) == "sell all refused: the sell of 40 SPY fails the market_closed check"
    set_broker(conn, checked_at=now(conn) - timedelta(hours=1))
    r = client.post(f"/stocks/assignments/{aid}/liquidate", follow_redirects=False)
    assert flash_cookie(r) == "sell all refused: the sell of 40 SPY fails the broker_stale check"
    assert sells(conn, aid) == [], "a refused sell all stores nothing"
    # a halted assignment without shares is closed, not sold
    empty = add_assignment(conn, add_model(conn), "halted")
    r = client.post(f"/stocks/assignments/{empty}/liquidate", follow_redirects=False)
    assert flash_cookie(r) == f"sell all refused: stock assignment {empty} holds no shares; close it instead"


def test_sell_all_live_needs_the_live_switch(client, conn):
    h = holding(conn, "live")
    aid = h["assignment"]
    retire(client, conn, h["model"], aid)
    r = client.post(f"/api/stocks/assignments/{aid}/liquidate")
    assert r.status_code == 409 and "live_disabled" in r.text
    conn.execute("UPDATE settings SET value = 'true' WHERE key = 'live_enabled'")
    r = client.post(f"/api/stocks/assignments/{aid}/liquidate")
    assert r.status_code == 200, r.text
    [o] = r.json()["orders"]
    assert o["symbol"] == "SPY" and o["qty"] == 40 and o["status"] == "approved"
    assert sells(conn, aid)[0]["mode"] == "live", "not_live_eligible is skipped for a retired model's live shares"


# ------------------------------------------------------------------ model rows: the verdict in words


def model_with(conn, status: str, bt: dict[str, Any], val: dict[str, Any] | None) -> int:
    mid = add_model(conn, status)
    conn.execute("UPDATE stock_models SET backtest_metrics = %s, validation_metrics = %s WHERE id = %s",
                 (Jsonb(bt), Jsonb(val) if val is not None else None, mid))
    return mid


def test_a_failing_model_says_why_on_its_row(client, conn):
    few = model_with(conn, "candidate", {"sharpe": 0.6, "max_drawdown": 0.1, "trades": 25}, {"sharpe": 0.2})
    weak = model_with(conn, "candidate", {"sharpe": 0.3, "max_drawdown": 0.1, "trades": 80}, {"sharpe": 0.2})
    unval = model_with(conn, "candidate", {"sharpe": 0.9, "max_drawdown": 0.1, "trades": 80}, None)
    paper = model_with(conn, "paper_ok", {"sharpe": 0.9, "max_drawdown": 0.1, "trades": 80}, {"sharpe": 0.4})
    live = model_with(conn, "live_eligible", {"sharpe": 0.9, "max_drawdown": 0.1, "trades": 80}, {"sharpe": 0.4})
    p = page(client.get("/stocks").text)
    assert p.row("stock-model", few).one("[data-verdict]").text == "needs 30 trades (25)"
    assert p.row("stock-model", weak).one("[data-verdict]").text == "needs Sharpe 0.50 (0.30)"
    assert p.row("stock-model", unval).one("[data-verdict]").text == "not validated: press Validate"
    assert p.row("stock-model", paper).one("[data-verdict]").text == (
        "not yet live eligible: needs 20 paper sessions (0 so far)")
    assert not p.row("stock-model", live).has("[data-verdict]")


# ------------------------------------------------------------------ Settings: the decision window


def test_settings_refuse_a_decision_window_shorter_than_two_ticks(client, conn):
    r = client.post("/settings/stocks", data={**GOOD, "stock_decision_lead_min": "12", "stock_trade_tick_s": "120"},
                    follow_redirects=False)
    assert r.status_code == 400
    assert "must cover at least two stock trade ticks" in page(r.text).form("stocks").text
    assert client.get("/api/settings").json()["stock_decision_lead_min"] == 20, "nothing saved"
    ok = client.post("/settings/stocks", data={**GOOD, "stock_decision_lead_min": "13", "stock_trade_tick_s": "60"},
                     follow_redirects=False)
    assert ok.status_code == 303, "two minutes of window holds two 60 s ticks"
    help_text = page(client.get("/settings").text).field("stock_decision_lead_min").text
    assert "15:49" in help_text and "two stock trade ticks" in help_text


# ------------------------------------------------------------------ small page fixes


def test_pdt_chip_avg_fill_and_last_backtest(client, conn):
    add_symbol(conn, "SPY", [440.0, 450.0])
    set_broker(conn, pattern_day_trader=True, daytrade_count=4)
    mid = add_model(conn)
    aid = add_assignment(conn, mid)
    oid = add_order(conn, aid, "partial")
    conn.execute("UPDATE stock_orders SET filled_qty = 2, avg_fill_price = 512.3456 WHERE id = %s", (oid,))
    job = conn.execute(
        "INSERT INTO jobs (kind, role, params, status, result) VALUES ('stock_backtest', 'backtest', %s, 'succeeded', %s)"
        " RETURNING id",
        (Jsonb({"model_id": mid}), Jsonb({"model_id": mid, "backtest_metrics": {"sharpe": 0.91, "cagr": 0.07}})),
    ).fetchone()["id"]
    p = page(client.get("/stocks").text)
    assert p.one('[data-chip="pdt"]').text == "pattern day trader"
    assert "(2 filled avg $512.35)" in p.row("stock-order", oid).text
    last = p.row("stock-model", mid).one("[data-last-backtest]")
    assert "Sharpe 0.91, CAGR +7.0%" in last.text and last.one("a").target == f"/jobs/{job}"
