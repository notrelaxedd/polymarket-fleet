"""The executor's live path and the live sync tasks (docs/LIVE.md "Executor live
path", "Authentication probe and auto-kill") on the scripted FakeLiveGateway."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from host import kill
from host.exchange import live_sync
from host.exchange.adapters.base import PaperGateway
from host.exchange.executor import Executor
from host.trading import ledger, orders
from host.trading.live import live_state
from tests.conftest import (
    approve, approved_order, assignment_row, audit_rows, auth_state, bankroll_of, enable_live, order_events, order_row,
    set_setting, trade_setup,
)
from tests.fake_gateway import FakeAuthError, FakeLiveGateway, live_loop
from tests.pagecheck import page

NOW = datetime.now(timezone.utc).replace(microsecond=0)
LIVE_GAME = "2026_05_BUF_MIA"


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


@pytest.fixture
def gw() -> FakeLiveGateway:
    return FakeLiveGateway(clock=lambda: NOW)


@pytest.fixture
def live(conn):
    return trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME)


def fills_of(conn, order_id) -> list[dict]:
    return conn.execute("SELECT * FROM fills WHERE order_id = %s ORDER BY id", (order_id,)).fetchall()


def test_place_ok_opens_with_exchange_id(conn, gw, live):
    row = approved_order(conn, live, size=5)
    ex = Executor(PaperGateway(), gw)
    counts = ex.tick(conn, NOW)
    assert counts["submitted"] == 1 and gw.placed == [row["client_request_id"]]
    after = order_row(conn, row["id"])
    assert after["status"] == "open" and after["exchange_order_id"] == "ex-1" and after["submitted_at"] == NOW
    assert order_events(conn, row["id"]) == ["approved", "submitting", "open"]
    assert [o["client_order_id"] for o in gw.open_orders()] == [row["client_request_id"]]
    assert ex.tick(conn, at(1))["submitted"] == 0 and gw.calls["place"] == 1
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == row["cost_cents"] and ledger.replay_problems(conn) == []


def test_place_timeout_reconciled_from_open_orders(conn, gw, live):
    row = approved_order(conn, live, size=5)
    gw.place_mode = "timeout"
    ex = Executor(PaperGateway(), gw)
    ex.tick(conn, NOW)
    assert order_row(conn, row["id"])["status"] == "submitting" and gw.calls["place"] == 1
    ex.tick(conn, at(1))
    assert order_row(conn, row["id"])["status"] == "submitting" and gw.calls["open_orders"] == 0, "5 s before the first look"
    ex.tick(conn, at(6))
    after = order_row(conn, row["id"])
    assert after["status"] == "open" and after["exchange_order_id"] == "ex-1", "adopted from open_orders by client id"
    assert gw.calls["place"] == 1 and gw.calls["open_orders"] == 1
    assert order_events(conn, row["id"]) == ["approved", "submitting", "open"]


def test_place_timeout_reconciled_from_fills(conn, gw, live):
    row = approved_order(conn, live, size=5)
    gw.place_mode = "lost"
    ex = Executor(PaperGateway(), gw)
    ex.tick(conn, NOW)
    assert order_row(conn, row["id"])["status"] == "submitting"
    gw.add_fill(row["client_request_id"], 0.52, 2, fee_cents=1, exchange_order_id="ex-filled", fill_id="f-1")
    ex.tick(conn, at(6))
    after = order_row(conn, row["id"])
    assert after["status"] == "partial" and after["filled_size"] == 2, "the fill was recorded against the submitting row"
    assert [f["exchange_fill_id"] for f in fills_of(conn, row["id"])] == ["f-1"]
    gw.add_fill(row["client_request_id"], 0.52, 3, fee_cents=1, exchange_order_id="ex-filled", fill_id="f-2")
    # a partial row that is missing remotely is closed by the audit, from its fills
    live_sync.audit_open_orders(conn, gw, at(7))
    after = order_row(conn, row["id"])
    assert after["status"] == "filled" and after["filled_size"] == 5
    bank = bankroll_of(conn, live.assignment)
    assert bank["reserved_cents"] == 0 and bank["open_cost_cents"] == 260 and ledger.replay_problems(conn) == []
    assert gw.calls["place"] == 1


def test_place_timeout_expires_after_grace_with_release(conn, gw, live):
    row = approved_order(conn, live, size=5)
    gw.place_mode = "lost"
    ex = Executor(PaperGateway(), gw)
    ex.tick(conn, NOW)
    for seconds in (6, 30, 59):
        ex.tick(conn, at(seconds))
        assert order_row(conn, row["id"])["status"] == "submitting", seconds
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == row["cost_cents"]
    ex.tick(conn, at(61))
    after = order_row(conn, row["id"])
    assert after["status"] == "expired" and order_events(conn, row["id"])[-1] == "expired"
    bank = bankroll_of(conn, live.assignment)
    assert bank["reserved_cents"] == 0 and bank["available_cents"] == 10_000 and ledger.replay_problems(conn) == []
    set_setting(conn, "submitting_grace_s", 5)
    second = approved_order(conn, live, size=1)
    ex.tick(conn, at(100))
    ex.tick(conn, at(106))
    assert order_row(conn, second["id"])["status"] == "expired", "the grace comes from settings"


def test_never_resubmits_blind(conn, gw, live):
    row = approved_order(conn, live, size=5)
    gw.place_mode = "timeout"
    ex = Executor(PaperGateway(), gw)
    for seconds in (0, 0.5, 1, 3, 4.9):
        ex.tick(conn, at(seconds))
    assert gw.placed == [row["client_request_id"]] and order_row(conn, row["id"])["status"] == "submitting"
    restarted = Executor(PaperGateway(), gw)
    restarted.tick(conn, at(5))
    assert gw.placed == [row["client_request_id"]], "a new executor (a restart) reconciles, never re-places"
    assert order_row(conn, row["id"])["status"] == "open"
    gw.fail_next("open_orders", RuntimeError("listing down"))
    lost = approved_order(conn, live, size=1)
    gw.place_mode = "lost"
    restarted.tick(conn, at(10))
    restarted.tick(conn, at(16))
    assert order_row(conn, lost["id"])["status"] == "submitting" and gw.placed.count(lost["client_request_id"]) == 1
    assert gw.calls["place"] == 2


def test_fills_poll_is_idempotent(conn, gw, live):
    row = approved_order(conn, live, size=5)
    Executor(PaperGateway(), gw).tick(conn, NOW)
    assert live_sync.poll_fills(conn, gw, at(1)) == 0 and gw.calls["fills"] == 1
    gw.add_fill(row["client_request_id"], 0.52, 3, fee_cents=2, fill_id="f-a")
    assert live_sync.poll_fills(conn, gw, at(2)) == 1
    assert live_sync.poll_fills(conn, gw, at(4)) == 0, "the same fill again: nothing recorded twice"
    after = order_row(conn, row["id"])
    assert after["status"] == "partial" and after["filled_size"] == 3 and len(fills_of(conn, row["id"])) == 1
    gw.add_fill(row["client_request_id"], 0.52, 2, fee_cents=1, fill_id="f-b")
    assert live_sync.poll_fills(conn, gw, at(6)) == 1
    after = order_row(conn, row["id"])
    assert after["status"] == "filled" and after["filled_size"] == 5
    bank = bankroll_of(conn, live.assignment)
    assert bank["open_cost_cents"] == 260 and bank["reserved_cents"] == 0 and bank["realized_pnl_cents"] == -3
    assert ledger.replay_problems(conn) == []
    assert gw.calls["fills"] == 4
    assert live_sync.poll_fills(conn, gw, at(8)) == 0 and gw.calls["fills"] == 5, "just closed: the poll still looks for a last fill"
    conn.execute("UPDATE orders SET updated_at = %s WHERE id = %s", (at(9) - timedelta(minutes=3), row["id"]))
    assert live_sync.poll_fills(conn, gw, at(9)) == 0 and gw.calls["fills"] == 5, "no active or recently closed live order: no call"


def test_unknown_fill_auto_kills(conn, gw, live):
    row = approved_order(conn, live, size=5)
    Executor(PaperGateway(), gw).tick(conn, NOW)
    gw.add_fill("not-ours", 0.40, 1, exchange_order_id="ex-foreign", fill_id="f-x")
    assert live_sync.poll_fills(conn, gw, at(2)) == 0
    assert kill.is_killed(conn) and live_state(conn)["auto_kill_reasons"] == ["unknown_fill"]
    rows = audit_rows(conn, "auto_kill")
    assert rows[-1]["actor"] == "auto:unknown_fill" and rows[-1]["after"]["fill"]["client_order_id"] == "not-ours"
    assert conn.execute("SELECT value FROM settings WHERE key = 'live_enabled'").fetchone()["value"] is False
    assert order_row(conn, row["id"])["status"] == "cancel_requested", "the kill sweeps the live order"
    assert live_sync.poll_fills(conn, gw, at(4)) == 0 and len(audit_rows(conn, "auto_kill")) == 1, "no spam while killed"


def test_open_order_audit_unknown_remote_cancels_and_auto_kills(conn, gw, live):
    row = approved_order(conn, live, size=5)
    Executor(PaperGateway(), gw).tick(conn, NOW)
    stranger = gw.add_remote_order(client_id="someone-else", exchange_order_id="ex-stranger")
    result = live_sync.audit_open_orders(conn, gw, at(1))
    assert result["unknown"] == [{"client_order_id": "someone-else", "exchange_order_id": "ex-stranger"}]
    assert result["auto_killed"] == "unknown_order" and gw.cancelled == ["ex-stranger"]
    assert stranger["exchange_order_id"] not in gw.remote and kill.is_killed(conn)
    assert audit_rows(conn, "auto_kill")[-1]["after"]["orders"][0]["exchange_order_id"] == "ex-stranger"
    assert live_state(conn)["auto_kill_reasons"] == ["unknown_order"]
    state = conn.execute("SELECT open_orders_checked_at FROM exchange_state").fetchone()
    assert state["open_orders_checked_at"] == at(1)
    assert order_row(conn, row["id"])["status"] == "cancel_requested", "ours is untouched by the audit, swept by the kill"


def test_missing_remote_order_closed_from_fills(conn, gw, live):
    filled = approved_order(conn, live, size=2)
    expired = approved_order(conn, live, size=3)
    dropped = approved_order(conn, live, size=4)
    ex = Executor(PaperGateway(), gw)
    ex.tick(conn, NOW)
    conn.execute("UPDATE orders SET gtd_at = %s WHERE id = %s", (at(-1), expired["id"]))
    gw.add_fill(filled["client_request_id"], 0.52, 2, fee_cents=1, fill_id="f-1")  # fully filled: the fake drops it
    gw.remote.pop(order_row(conn, expired["id"])["exchange_order_id"])
    gw.remote.pop(order_row(conn, dropped["id"])["exchange_order_id"])
    result = live_sync.audit_open_orders(conn, gw, at(5))
    assert result["unknown"] == [] and result["fills"] == 1 and not kill.is_killed(conn)
    assert order_row(conn, filled["id"])["status"] == "filled"
    assert order_row(conn, expired["id"])["status"] == "expired"
    assert order_row(conn, dropped["id"])["status"] == "cancelled"
    assert set(result["closed"].values()) == {"filled", "expired", "cancelled"}
    bank = bankroll_of(conn, live.assignment)
    assert bank["reserved_cents"] == 0 and bank["open_cost_cents"] == 104 and ledger.replay_problems(conn) == []
    assert order_events(conn, dropped["id"])[-1] == "cancelled"


def test_cancel_retries_until_confirmed(conn, gw, live):
    row = approved_order(conn, live, size=5)
    ex = Executor(PaperGateway(), gw)
    ex.tick(conn, NOW)
    orders.cancel_order(conn, row["id"], "owner", "owner cancel")
    assert order_row(conn, row["id"])["status"] == "cancel_requested"
    gw.cancel_results = [RuntimeError("down"), "ack_only", True]
    ex.tick(conn, at(1))
    assert gw.calls["cancel"] == 1 and order_row(conn, row["id"])["status"] == "cancel_requested"
    ex.tick(conn, at(1.5))
    assert gw.calls["cancel"] == 1, "waits 1 s"
    ex.tick(conn, at(2))
    assert gw.calls["cancel"] == 2 and order_row(conn, row["id"])["status"] == "cancel_requested", "ok answered but still listed"
    assert gw.calls["open_orders"] >= 1
    ex.tick(conn, at(3))
    assert gw.calls["cancel"] == 2, "waits 2 s after the unconfirmed ok"
    ex.tick(conn, at(4))
    assert gw.calls["cancel"] == 3
    after = order_row(conn, row["id"])
    assert after["status"] == "cancelled" and gw.remote == {}
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []
    assert order_events(conn, row["id"])[-2:] == ["cancel_requested", "cancelled"]


def test_cancel_confirmation_absorbs_a_last_fill(conn, gw, live):
    row = approved_order(conn, live, size=5)
    ex = Executor(PaperGateway(), gw)
    ex.tick(conn, NOW)
    orders.cancel_order(conn, row["id"], "owner", "owner cancel")
    gw.add_fill(row["client_request_id"], 0.52, 1, fee_cents=1, fill_id="f-last")
    ex.tick(conn, at(1))
    after = order_row(conn, row["id"])
    assert after["status"] == "cancelled" and after["filled_size"] == 1
    bank = bankroll_of(conn, live.assignment)
    assert bank["open_cost_cents"] == 52 and bank["reserved_cents"] == 0 and ledger.replay_problems(conn) == []


def test_cancel_all_direct_cancels_everything_and_updates_rows(conn, gw, live):
    a = approved_order(conn, live, size=2)
    b = approved_order(conn, live, size=3)
    ex = Executor(PaperGateway(), gw)
    ex.tick(conn, NOW)
    orders.cancel_order(conn, b["id"], "owner", "owner cancel")
    gw.add_remote_order(client_id="stranger", exchange_order_id="ex-s")
    gw.cancel_results = [RuntimeError("down"), True]
    sleeps: list[float] = []
    result = live_sync.cancel_all_direct(conn, gw, at(1), sleeps.append, "cli")
    assert [r["exchange_order_id"] for r in result["remote"]] == ["ex-1", "ex-2", "ex-s"]
    assert all(r["cancelled"] for r in result["remote"]) and result["remote"][0]["attempts"] == 2 and sleeps == [1.0]
    assert result["remote"][2]["order_id"] is None and result["remote"][2]["row_status"] == "unknown"
    assert gw.remote == {} and result["still_open"] == 0
    for row in (a, b):
        assert order_row(conn, row["id"])["status"] == "cancelled"
        assert order_events(conn, row["id"])[-1] == "cancelled"
    assert result["rows_closed"] == {str(a["id"]): "cancelled", str(b["id"]): "cancelled"}
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []
    audit = audit_rows(conn, "cancel_all")[-1]
    assert audit["actor"] == "cli" and audit["after"] == {
        "direct": True, "remote": 3, "cancelled": 3, "rows_closed": 2, "live_was_on": True, "assignments_halted": 1,
        "approved_cancelled": 0, "left_for_exchange": 0, "error": None,
    }
    assert result["live_off"]["was_on"] and assignment_row(conn, live.assignment["id"])["status"] == "halted"
    assert conn.execute("SELECT value FROM settings WHERE key = 'live_enabled'").fetchone()["value"] is False, "live off first"


def test_auth_failures_auto_kill_after_three_and_reset_on_success(conn, gw):
    enable_live(conn)
    for n in (1, 2):
        gw.fail_next("balance", FakeAuthError("401 unauthorized"))
        result = live_sync.auth_check(conn, gw, at(n), True)
        assert result["auth_ok"] is False and result["failures"] == n and result["auto_killed"] is None
        state = conn.execute("SELECT * FROM exchange_state").fetchone()
        assert state["auth_failures"] == n and state["auth_ok"] is False and "401" in state["last_auth_error"]
        assert not kill.is_killed(conn)
    gw.fail_next("balance", FakeAuthError("401 unauthorized"))
    result = live_sync.auth_check(conn, gw, at(3), True)
    assert result["auto_killed"] == "auth_failures" and kill.is_killed(conn)
    assert audit_rows(conn, "auto_kill")[-1]["after"] == {"reason": "auth_failures", "failures": 3, "last_error": "401 unauthorized"}
    assert audit_rows(conn, "kill")[-1]["actor"] == "auto:auth_failures"
    assert conn.execute("SELECT value FROM settings WHERE key = 'live_enabled'").fetchone()["value"] is False
    gw.fail_next("balance", RuntimeError("timeout"))
    assert live_sync.auth_check(conn, gw, at(4), True)["failures"] == 4 and len(audit_rows(conn, "auto_kill")) == 1
    kill.reset_kill(conn, "owner", "RESUME")
    gw.balance_payload = {"balance_cents": 12_345, "buying_power_cents": 10_000, "server_time": None}
    gw.set_skew_ms(250)
    result = live_sync.auth_check(conn, gw, at(5), True)
    assert result["auth_ok"] is True and result["skew_ms"] == 250
    state = conn.execute("SELECT * FROM exchange_state").fetchone()
    assert state["auth_failures"] == 0 and state["auth_ok"] and state["last_auth_error"] is None
    assert state["balance_cents"] == 12_345 and state["buying_power_cents"] == 10_000 and state["clock_skew_ms"] == 250
    assert state["auth_checked_at"] == at(5) == state["balance_checked_at"] and state["credentials_present"] is True
    assert not kill.is_killed(conn)
    set_setting(conn, "auto_kill", {"auth_failures": 1, "clock_skew_ms": 30_000})
    gw.fail_next("balance", FakeAuthError("403"))
    assert live_sync.auth_check(conn, gw, at(6), True)["auto_killed"] == "auth_failures", "the threshold comes from settings"
    assert live_sync.auth_check(conn, gw, at(7), False)["credentials_present"] is False
    assert conn.execute("SELECT credentials_present, auth_ok FROM exchange_state").fetchone() == {"credentials_present": False, "auth_ok": False}


def test_clock_skew_auto_kills_and_stops_placing(pool, conn, gw):
    """A skew over the limit auto-kills and pauses live placements only: the
    audit, the fills poll and the cancels keep going, so the kill's cancels reach
    the exchange instead of resting there until GTD."""
    enable_live(conn)
    lv = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME)
    resting = approved_order(conn, lv, size=2)
    Executor(PaperGateway(), gw).tick(conn, at(-10))
    assert order_row(conn, resting["id"])["status"] == "open" and "ex-1" in gw.remote
    gw.set_skew_ms(45_000)
    loop = live_loop(pool, gw, clock=lambda: NOW)
    results = loop.run_due(NOW)
    assert results["auth"]["skew_over_limit"] is True and results["auth"]["auto_killed"] == "clock_skew"
    assert "startup_reconcile" in results and kill.is_killed(conn), "the startup audit runs even while paused"
    assert loop.live_paused == "clock_skew" and loop.executor.live_blocked == "clock_skew"
    assert conn.execute("SELECT clock_skew_ms FROM exchange_state").fetchone()["clock_skew_ms"] == 45_000
    assert live_state(conn)["auto_kill_reasons"] == ["clock_skew"]
    assert order_row(conn, resting["id"])["status"] == "cancelled" and gw.remote == {} and gw.calls["cancel"] == 1, \
        "the kill's cancel reached the exchange in the same pass"
    assert gw.calls["open_orders"] >= 1 and gw.calls["place"] == 1, "only the setup's place"
    loop.run_due(at(1), force=True)
    assert gw.calls["place"] == 1 and gw.calls["balance"] == 2, "the auth probe keeps running: it is how the loop learns the skew recovered"
    kill.reset_kill(conn, "owner", "RESUME")
    loop.run_task("auth", at(2))
    assert kill.is_killed(conn) and len(audit_rows(conn, "auto_kill")) == 2, "a reset with the skew still over the limit kills again"
    kill.reset_kill(conn, "owner", "RESUME")
    enable_live(conn)
    conn.execute("UPDATE assignments SET status = 'active' WHERE id = %s", (lv.assignment["id"],))
    row = approved_order(conn, lv, size=1)
    # held back while paused: the executor does not place a live order
    loop.executor.tick(conn, at(3))
    assert gw.calls["place"] == 1 and order_row(conn, row["id"])["status"] == "approved"
    gw.set_skew_ms(100)
    assert loop.run_task("auth", at(4))["skew_over_limit"] is False and loop.live_paused is None
    assert loop.executor.live_blocked is None and not kill.is_killed(conn)
    loop.run_due(at(5), force=True)
    assert gw.calls["place"] == 2 and order_row(conn, row["id"])["status"] == "open"


def wall() -> datetime:
    """The clock now: approval judges buying power age against the database clock, and
    NOW (fixed at import) can be minutes old by the time a long suite gets here."""
    return datetime.now(timezone.utc)


def test_buying_power_stale_rejects(conn, live):
    assert approve(conn, live, size=1)["status"] == "approved"
    auth_state(conn, balance_checked_at=wall() - timedelta(seconds=301))
    assert approve(conn, live, size=1)["reason"] == "buying_power", "older than buying_power_max_age_s"
    set_setting(conn, "buying_power_max_age_s", 600)
    assert approve(conn, live, size=1)["status"] == "approved"
    auth_state(conn, balance_checked_at=None)
    assert approve(conn, live, size=1)["reason"] == "buying_power"
    auth_state(conn, balance_checked_at=wall(), buying_power_cents=None)
    assert approve(conn, live, size=1)["reason"] == "buying_power", "a missing figure rejects"
    paper = trade_setup(conn)
    assert approve(conn, paper, size=1)["status"] == "approved", "paper never looks at buying power"


def test_buying_power_counts_reserved_live(conn, live):
    auth_state(conn, buying_power_cents=1100, balance_checked_at=wall())
    first = approved_order(conn, live, size=10)
    assert first["cost_cents"] == 532 == bankroll_of(conn, live.assignment)["reserved_cents"]
    assert approve(conn, live, size=10)["status"] == "approved", "532 + 532 <= 1100"
    assert approve(conn, live, size=1)["reason"] == "buying_power", "1064 reserved + 54 > 1100"
    orders.cancel_order(conn, first["id"], "owner", "test")
    assert approve(conn, live, size=1)["status"] == "approved", "a release frees buying power"
    assert live_state(conn)["buying_power_cents"] == 1100


def test_startup_runs_auth_reconcile_audit_first(pool, conn, gw):
    enable_live(conn)
    lv = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME)
    stuck = approved_order(conn, lv, size=2)
    orders.set_status(conn, stuck["id"], "submitting", "executor", expected=("approved",), submitted_at=at(-30), gtd_at=at(900))
    gw.add_remote_order(client_id=stuck["client_request_id"], exchange_order_id="ex-old")
    gw.add_remote_order(client_id="ghost", exchange_order_id="ex-ghost")
    fresh = approved_order(conn, lv, size=1)
    loop = live_loop(pool, gw, clock=lambda: NOW)
    results = loop.run_due(NOW)
    assert list(results)[:2] == ["auth", "startup_reconcile"]
    assert results["auth"]["auth_ok"] is True and results["startup_reconcile"]["reconciled"]["opened"] == 1
    assert gw.log[:3] == ["balance", "open_orders", "open_orders"], "auth, reconciliation and the audit before anything else"
    assert order_row(conn, stuck["id"])["exchange_order_id"] == "ex-old"
    assert "ex-ghost" in gw.cancelled and kill.is_killed(conn), "the ghost order auto-killed before any submission"
    assert gw.calls["place"] == 0 and order_row(conn, fresh["id"])["status"] == "cancelled", "the kill swept the approved row"
    assert order_row(conn, stuck["id"])["status"] == "cancelled" and "ex-old" in gw.cancelled, "cancel_requested by the kill, confirmed by the outbox"
    state = conn.execute("SELECT credentials_present, auth_ok, open_orders_checked_at FROM exchange_state").fetchone()
    assert state["credentials_present"] and state["auth_ok"] and state["open_orders_checked_at"] == NOW
    assert loop.started and loop.credentials_present and loop.executor.live_gateway is gw


def test_startup_without_credentials_runs_no_live_task(pool, conn, gw):
    from host.exchange.main import ExchangeLoop

    loop = ExchangeLoop(pool, clock=lambda: NOW, gateway_factory=lambda config, creds: gw)
    loop.load_credentials = lambda: None  # type: ignore[method-assign]
    results = loop.run_due(NOW, force=True)
    assert results["auth"]["credentials_present"] is False and "startup_reconcile" not in results
    assert results["open_orders_audit"] is None and results["live_fills"] is None and gw.calls == {}
    state = conn.execute("SELECT credentials_present, auth_ok FROM exchange_state").fetchone()
    assert state == {"credentials_present": False, "auth_ok": False}


def test_live_off_cancels_live_orders_through_gateway(conn, gw, live):
    row = approved_order(conn, live, size=5)
    paper = trade_setup(conn)
    p = approved_order(conn, paper, size=1)
    ex = Executor(PaperGateway(), gw)
    ex.tick(conn, NOW)
    assert order_row(conn, row["id"])["status"] == "open" and order_row(conn, p["id"])["status"] == "open"
    result = kill.live_off(conn, "owner", "owner")
    assert result["was_on"] and result["assignments_halted"] == [str(live.assignment["id"])]
    assert order_row(conn, row["id"])["status"] == "cancel_requested" and gw.calls["cancel"] == 0
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == row["cost_cents"], "released only when the exchange confirms"
    ex.tick(conn, at(1))
    assert gw.calls["cancel"] == 1 and gw.remote == {}
    assert order_row(conn, row["id"])["status"] == "cancelled" and order_row(conn, p["id"])["status"] == "open"
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []
    assert assignment_row(conn, paper.assignment["id"])["status"] == "active" and not kill.is_killed(conn)
    assert audit_rows(conn, "live_off")[-1]["after"]["orders_cancel_requested"] == 1, "one live order awaits the exchange"


def test_auto_kill_reason_shown_until_reset(client, conn):
    enable_live(conn)
    assert kill.auto_kill(conn, "unknown_order", {"count": 1}) is True
    assert kill.auto_kill(conn, "auth_failures", {"failures": 3}) is False, "already killed: audit only"
    state = client.get("/api/live").json()
    assert state["killed"] is True and state["auto_kill_reasons"] == ["unknown_order", "auth_failures"]
    assert state["live_enabled"] is False
    assert [r["actor"] for r in audit_rows(conn, "kill")] == ["auto:unknown_order", "auto:auth_failures"]
    assert page(client.get("/fragments/topbar").text).has('[data-killed="1"]')
    assert client.post("/api/kill/reset", json={"confirm": "RESUME"}).status_code == 200
    state = client.get("/api/live").json()
    assert state["killed"] is False and state["auto_kill_reasons"] == [] and state["live_enabled"] is False
    kill.auto_kill(conn, "clock_skew", {"skew_ms": 50_000})
    assert client.get("/api/live").json()["auto_kill_reasons"] == ["clock_skew"], "a new kill after the reset shows again"
