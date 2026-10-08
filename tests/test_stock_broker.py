"""The broker side of stocks on Alpaca (contract section 6): the trading client's
errors and backoff, the account and clock into stock_broker_state, the position and
open-order reconciliation (warnings on paper, auto-kill on live), the daily marks after
the close, and the CLI commands stock-smoke and stock-cancel-all --direct."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from host.exchange import cli, stock_broker, stock_marks, stock_ops, stock_tasks
from host.exchange.alpaca_trading import AlpacaAuthError, AlpacaRateLimited, AlpacaTimeout
from host.exchange.stock_executor import StockExecutor
from tests.fake_alpaca import KEY, SECRET, FakeAlpaca, ny
from tests.stock_host_helpers import (add_bars, assignment_row, audit, db_now, one, order_row, set_position,
                                      set_setting, stock_setup)

DAY = date(2027, 3, 4)  # a Thursday


def killed(conn) -> bool:
    return conn.execute("SELECT value FROM settings WHERE key = 'kill_switch'").fetchone()["value"] is True


def broker(conn) -> dict[str, Any]:
    return conn.execute("SELECT * FROM stock_broker_state").fetchone()


def test_the_client_redacts_backs_off_and_names_its_errors() -> None:
    fake = FakeAlpaca(now=ny(DAY, 11))
    client = fake.client()
    fake.force["/v2/account"] = 401
    with pytest.raises(AlpacaAuthError) as err:
        client.account()
    assert KEY not in str(err.value) and SECRET not in str(err.value) and "***" in str(err.value)
    fake.force = {"/v2/clock": 429}
    with pytest.raises(AlpacaRateLimited):
        client.clock()
    calls = len(fake.calls)
    with pytest.raises(AlpacaRateLimited):
        client.positions()
    assert len(fake.calls) == calls and 2.9 < client.backoff_remaining() <= 3.0
    client.backoff_until, fake.force = 0.0, {}
    assert client.order_by_client_id("nope") is None and client.order("nope") is None
    eid = client.place({"id": "c1", "symbol": "SPY", "qty": 1, "side": "buy"})
    fake.now = ny(DAY, 15, 51)
    assert client.cancel(eid) is False, "422 after 15:50"
    fake.timeout_next = "before"
    with pytest.raises(AlpacaTimeout):
        client.place({"id": "c2", "symbol": "SPY", "qty": 1, "side": "buy"})


def test_the_check_stores_cents_half_up_and_the_session(conn) -> None:
    fake = FakeAlpaca(now=ny(DAY, 15, 40), cash="1234.565")
    now = db_now(conn)
    stock_broker.check(conn, fake.client(), now)
    b = broker(conn)
    assert (b["environment"], b["keys_present"], b["cash_cents"], b["buying_power_cents"]) == ("paper", True, 123_457, 246_913)
    assert b["market_open"] is True and b["session_date"] == DAY and b["next_close"] == ny(DAY, 16)
    assert b["checked_at"] == now and b["last_error"] is None
    fake.now = ny(date(2027, 3, 5), 17)  # Friday evening: the next session is Monday
    stock_broker.check(conn, fake.client(), now)
    assert broker(conn)["market_open"] is False and broker(conn)["session_date"] == date(2027, 3, 8)
    fake.force["/v2/account"] = 500
    result = stock_broker.check(conn, fake.client(), now + timedelta(seconds=30))
    b = broker(conn)
    assert "500" in result["error"] and KEY not in b["last_error"] and b["checked_at"] == now


def test_paper_reconciliation_warns_about_positions_and_foreign_orders(conn) -> None:
    s = stock_setup(conn)
    set_position(conn, s.assignment["id"], "AAPL", 10)
    set_position(conn, s.assignment["id"], "SPY", 2)
    fake = FakeAlpaca(now=ny(DAY, 11))
    fake.positions = {"AAPL": Decimal(7), "SPY": Decimal(2), "MSFT": Decimal(1)}
    fake.place_direct("TSLA", 1)
    fake.place_direct("SPY", 1, client_id=stock_broker.SMOKE_PREFIX + "abc")
    rec = stock_broker.Reconciler()
    result = rec.run(conn, fake.client(), db_now(conn))
    assert [m["symbol"] for m in result["mismatches"]] == ["AAPL", "MSFT"] and len(result["unknown_orders"]) == 1
    kinds = [w["kind"] for w in broker(conn)["warnings"]]
    assert kinds == ["position_mismatch", "position_mismatch", "unknown_order"] and not killed(conn)
    rec.run(conn, fake.client(), db_now(conn))
    assert len(broker(conn)["warnings"]) == 3 and not killed(conn), "replaced, not appended; paper never kills"
    oid = one(conn, s, symbol="AAPL", side="sell", qty=3)["order_id"]
    conn.execute("UPDATE stock_orders SET status = 'open' WHERE id = %s", (order_row(conn, oid)["id"],))
    fake.orders.clear()
    result = rec.run(conn, fake.client(), db_now(conn))
    assert [m["symbol"] for m in result["mismatches"]] == ["MSFT"], "a symbol with an order in flight is not compared"


def test_an_open_order_of_a_closed_row_an_old_smoke_or_the_other_mode_is_not_ours(conn) -> None:
    s = stock_setup(conn, mode="live")
    oid = one(conn, s, qty=2)["order_id"]
    conn.execute("UPDATE stock_orders SET status = 'expired' WHERE id = %s", (order_row(conn, oid)["id"],))
    fake = FakeAlpaca(now=ny(DAY, 11), environment="live")
    fake.place_direct("AAPL", 2, client_id=str(oid))  # Alpaca shows it late: it would fill unbooked
    result = stock_broker.Reconciler().run(conn, fake.client(), db_now(conn))
    assert [u["client_order_id"] for u in result["unknown_orders"]] == [str(oid)]
    assert result["auto_killed"] == "unknown_order" and killed(conn)
    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'kill_switch'")
    paper = FakeAlpaca(now=ny(DAY, 11))
    smoke = paper.place_direct("SPY", 1, client_id=stock_broker.SMOKE_PREFIX + "old")
    smoke["created_at"] = (db_now(conn) - timedelta(minutes=20)).isoformat()
    young = paper.place_direct("TSLA", 1, client_id=stock_broker.SMOKE_PREFIX + "new")
    young["created_at"] = db_now(conn).isoformat()
    conn.execute("UPDATE stock_orders SET status = 'open' WHERE id = %s", (order_row(conn, oid)["id"],))
    result = stock_broker.Reconciler().run(conn, paper.client(), db_now(conn))
    assert [u["client_order_id"] for u in result["unknown_orders"]] == [stock_broker.SMOKE_PREFIX + "old"]
    assert result["other_mode_orders"] == 1 and not killed(conn)
    message = next(w["message"] for w in broker(conn)["warnings"] if w["kind"] == "other_mode_orders")
    assert "1 live order(s) are active but the keys are paper" in message


def test_live_reconciliation_auto_kills_on_a_repeated_mismatch_and_an_unknown_order(conn) -> None:
    s = stock_setup(conn, mode="live")
    set_position(conn, s.assignment["id"], "AAPL", 4)
    fake = FakeAlpaca(now=ny(DAY, 11), environment="live")
    rec = stock_broker.Reconciler()
    assert rec.run(conn, fake.client(), db_now(conn))["auto_killed"] is None and not killed(conn)
    assert rec.run(conn, fake.client(), db_now(conn))["auto_killed"] == "stock_position_mismatch" and killed(conn)
    assert audit(conn, "auto_kill")[-1]["entity"] == "stock_position_mismatch"
    assert assignment_row(conn, s.assignment["id"])["status"] == "halted"
    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'kill_switch'")
    fake.positions = {"AAPL": Decimal(4)}
    fake.place_direct("TSLA", 1)
    assert stock_broker.Reconciler().run(conn, fake.client(), db_now(conn))["auto_killed"] == "unknown_order" and killed(conn)


def _mark_setup(conn) -> Any:
    s = stock_setup(conn, bankroll=1_000_000)
    conn.execute("UPDATE stock_assignments SET created_at = %s WHERE id = %s", (ny(DAY, 10), s.assignment["id"]))
    set_position(conn, s.assignment["id"], "AAPL", 10, 100_000)
    set_position(conn, s.assignment["id"], "SPY", 2, 80_000)
    conn.execute("UPDATE stock_assignments SET cash_cents = 820_000 WHERE id = %s", (s.assignment["id"],))
    return s


def test_marks_wait_for_the_sessions_bars_and_fall_back_to_alpaca_after_the_deadline(conn, monkeypatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr("host.stocks.eligibility.recompute", lambda c, model_id, *a, **k: calls.append(model_id))
    s = _mark_setup(conn)
    add_bars(conn, "AAPL", [DAY], close=105.0, fetched_at=ny(DAY, 18))
    fake = FakeAlpaca(now=ny(DAY, 16, 20))
    fake.positions, fake.current_prices = {"AAPL": Decimal(10), "SPY": Decimal(2)}, {"SPY": Decimal("410.5")}
    marker = stock_marks.Marker()
    assert marker.run(conn, fake.client(), fake.now)["marked"] == 0, "the previous session predates the assignment"
    fake.now = ny(DAY, 16, 31)
    result = marker.run(conn, fake.client(), fake.now)
    assert (result["session"], result["marked"], result["waiting_for_bars"]) == (DAY.isoformat(), 0, ["SPY"])
    fake.now = ny(DAY, 16) + stock_marks.MARK_DEADLINE + timedelta(minutes=1)
    result = marker.run(conn, fake.client(), fake.now)
    assert result["session"] == DAY.isoformat() and result["marked"] == 1
    assert result["price_sources"] == {"AAPL": "bar", "SPY": "alpaca"} and calls == [s.model["id"]]
    mark = conn.execute("SELECT * FROM stock_marks WHERE assignment_id = %s", (s.assignment["id"],)).fetchone()
    assert (mark["session_date"], mark["positions_cents"], mark["equity_cents"]) == (DAY, 105_000 + 82_100, 820_000 + 187_100)
    assert marker.run(conn, fake.client(), fake.now + timedelta(minutes=5))["marked"] == 0 and len(calls) == 1
    half = date(2027, 3, 5)
    fake.half_days, fake.now = {half}, ny(half, 13, 31)
    assert stock_marks.Marker().run(conn, fake.client(), fake.now)["session"] == half.isoformat()


def test_marks_use_complete_bars_only_and_wait_for_the_sessions_fills(conn, monkeypatch) -> None:
    monkeypatch.setattr("host.stocks.eligibility.recompute", lambda *a, **k: None)
    s = _mark_setup(conn)
    add_bars(conn, "AAPL", [DAY], close=99.0, fetched_at=ny(DAY, 12))  # an intraday fetch: a partial bar
    add_bars(conn, "SPY", [DAY], close=410.0, fetched_at=ny(DAY, 18))
    oid = one(conn, s, symbol="SPY", qty=1)["order_id"]
    conn.execute("UPDATE stock_orders SET status = 'open', session_date = %s WHERE id = %s", (DAY, order_row(conn, oid)["id"]))
    fake = FakeAlpaca(now=ny(DAY, 16, 31))
    result = stock_marks.mark_session(conn, fake.client(), DAY, ny(DAY, 16), fake.now)
    assert result == {"session": DAY.isoformat(), "marked": 0}, "an order of the session is in flight"
    conn.execute("UPDATE stock_orders SET status = 'filled' WHERE id = %s", (order_row(conn, oid)["id"],))
    result = stock_marks.mark_session(conn, fake.client(), DAY, ny(DAY, 16), fake.now)
    assert result["waiting_for_bars"] == ["AAPL"], "the partial bar is not the session's close"
    add_bars(conn, "AAPL", [], fetched_at=ny(DAY, 18))
    conn.execute("UPDATE stock_bars SET close = 105 WHERE symbol = 'AAPL'")
    result = stock_marks.mark_session(conn, fake.client(), DAY, ny(DAY, 16), fake.now)
    assert result["marked"] == 1 and result["price_sources"] == {"AAPL": "bar", "SPY": "bar"}


def _cli_setup(monkeypatch, config, fake: FakeAlpaca, now: datetime) -> None:
    monkeypatch.setenv("DATABASE_URL", config.database_url)
    monkeypatch.setattr(stock_tasks, "default_client", fake.client)
    monkeypatch.setattr(stock_ops, "utcnow", lambda: now)


def test_stock_smoke_places_and_cancels_one_share_with_the_gates(conn, config, monkeypatch, capsys) -> None:
    fake = FakeAlpaca(now=ny(DAY, 11))
    _cli_setup(monkeypatch, config, fake, fake.now)
    phrase = stock_ops.expected_phrase(conn, fake.now)
    assert cli.main(["stock-smoke", "--confirm", "STOCK SMOKE 2000-01-01", "--hold", "0"]) == 1 and not fake.orders
    assert cli.main(["stock-smoke", "--confirm", phrase, "--hold", "0"]) == 0
    (order,) = fake.orders.values()
    assert (order["symbol"], order["qty"], order["time_in_force"], order["status"]) == ("SPY", "1", "cls", "canceled")
    assert order["client_order_id"].startswith(stock_broker.SMOKE_PREFIX) and audit(conn, "stock_smoke")
    assert KEY not in capsys.readouterr().out
    late = ny(DAY, 15, 46)
    _cli_setup(monkeypatch, config, fake, late)
    assert cli.main(["stock-smoke", "--confirm", stock_ops.expected_phrase(conn, late), "--hold", "0"]) == 1
    assert "too late" in capsys.readouterr().err and len(fake.orders) == 1
    live = FakeAlpaca(now=ny(DAY, 11), environment="live")
    _cli_setup(monkeypatch, config, live, live.now)
    assert cli.main(["stock-smoke", "--confirm", phrase, "--hold", "0"]) == 1 and not live.orders
    assert "live trading is off" in capsys.readouterr().err
    set_setting(conn, "kill_switch", True)
    _cli_setup(monkeypatch, config, fake, fake.now)
    assert cli.main(["stock-smoke", "--confirm", phrase, "--hold", "0"]) == 1 and len(fake.orders) == 1


def test_stock_cancel_all_direct_cancels_everything_at_alpaca_and_closes_our_rows(conn, pool, config, monkeypatch, capsys) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    placed = one(conn, s, qty=2)["order_id"]
    fake = FakeAlpaca(now=ny(DAY, 11))
    with pool.connection() as c:
        StockExecutor().tick(c, fake.client(), db_now(conn))
    approved = one(conn, s, qty=1)["order_id"]
    foreign = fake.place_direct("TSLA", 1)
    _cli_setup(monkeypatch, config, fake, db_now(conn))
    monkeypatch.setattr(stock_ops.time, "sleep", lambda s: None)
    assert cli.main(["stock-cancel-all", "--direct"]) == 0
    assert all(o["status"] == "canceled" for o in fake.orders.values()) and foreign["status"] == "canceled"
    assert order_row(conn, placed)["status"] == "cancelled" and order_row(conn, approved)["status"] == "cancelled"
    a = assignment_row(conn, s.assignment["id"])
    assert (a["status"], a["cash_cents"], a["reserved_cents"]) == ("halted", 1_000_000, 0)
    assert audit(conn, "cancel_all")[-1]["entity"] == "stocks:paper" and '"cancelled": 2' in capsys.readouterr().out


def test_stock_cancel_all_without_direct_only_touches_the_database(conn, pool, config, monkeypatch, capsys) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    placed = one(conn, s, qty=2)["order_id"]
    fake = FakeAlpaca(now=ny(DAY, 11))
    with pool.connection() as c:
        StockExecutor().tick(c, fake.client(), db_now(conn))
    approved = one(conn, s, qty=1)["order_id"]
    _cli_setup(monkeypatch, config, fake, db_now(conn))
    calls = len(fake.calls)
    assert cli.main(["stock-cancel-all"]) == 0 and len(fake.calls) == calls
    assert "cancelled=1 requested=1" in capsys.readouterr().out
    assert order_row(conn, placed)["status"] == "cancel_requested" and order_row(conn, approved)["status"] == "cancelled"
    assert assignment_row(conn, s.assignment["id"])["status"] == "active"


def test_stock_smoke_recovers_a_timed_out_place_and_a_failed_cancel_and_always_audits(conn) -> None:
    fake = FakeAlpaca(now=ny(DAY, 11))
    client, phrase, naps = fake.client(), stock_ops.expected_phrase(conn, fake.now), []
    real_cancel, failures = client.cancel, [AlpacaTimeout("DELETE timed out")]

    def flaky_cancel(eid: str) -> bool:
        if failures:
            raise failures.pop()
        return real_cancel(eid)

    client.cancel = flaky_cancel  # type: ignore[method-assign]
    fake.timeout_next = "after"  # the order reached Alpaca, the answer did not
    result = stock_ops.run_stock_smoke(conn, client, phrase, 0, fake.now, sleep=naps.append)
    (order,) = fake.orders.values()
    assert result["status"] == "canceled" and result["exchange_order_id"] == order["id"] and order["status"] == "canceled"
    steps = [t["step"] for t in result["timeline"]]
    assert steps[:3] == ["place failed, looking it up", "found at Alpaca", "cancel failed"] and "cancel accepted" in steps
    assert audit(conn, "stock_smoke_result")[-1]["after"]["status"] == "canceled"
    fake.timeout_next = "before"  # nothing reached Alpaca: never placed again, reported unknown
    result = stock_ops.run_stock_smoke(conn, client, phrase, 0, fake.now, sleep=naps.append)
    assert result["status"] == "unknown" and len(fake.orders) == 1 and posts_of(fake) == 2
    assert [a["after"]["status"] for a in audit(conn, "stock_smoke_result")] == ["canceled", "unknown"]


def posts_of(fake: FakeAlpaca) -> int:
    return sum(1 for m, p in fake.calls if m == "POST")
