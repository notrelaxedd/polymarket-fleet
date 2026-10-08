"""Kill switch, live off, demotion, halt and the worker release on the stock tables
(contract sections 2B and 5): approved orders cancelled with the reservation released,
orders that may be at Alpaca cancel_requested, positions untouched."""
from __future__ import annotations

from host import kill
from host.errors import Conflict
from host.stocks import assignments, eligibility, views_api
from tests.stock_host_helpers import (
    assignment_row, audit, events, one, order_row, set_order_status, set_position, set_setting, stock_setup,
)

import pytest


def _orders(conn, s):
    """Four orders on the assignment: approved, submitting, open, partial (and a filled one)."""
    set_position(conn, s.assignment["id"], "SPY", 3, 120_000)
    ids = [one(conn, s, qty=1)["order_id"] for _ in range(5)]
    for oid, status in zip(ids[1:], ("submitting", "open", "partial", "filled")):
        set_order_status(conn, oid, status, filled=1 if status in ("partial", "filled") else 0)
    return ids


def test_kill_cancels_stock_orders_and_halts_every_stock_assignment(conn):
    s = stock_setup(conn)
    approved, submitting, opened, partial, filled = _orders(conn, s)
    before = assignment_row(conn, s.assignment["id"])
    assert before["reserved_cents"] == 5 * 10_500
    with conn.transaction():
        assert kill.set_kill(conn, "owner@example.com") is True
    assert order_row(conn, approved)["status"] == "cancelled" and order_row(conn, approved)["reserved_cents"] == 0
    for oid in (submitting, opened, partial):
        row = order_row(conn, oid)
        assert row["status"] == "cancel_requested" and row["reserved_cents"] == 10_500, "released when Alpaca confirms"
    assert order_row(conn, filled)["status"] == "filled"
    a = assignment_row(conn, s.assignment["id"])
    assert a["status"] == "halted" and a["halt_reason"] == "kill"
    assert a["reserved_cents"] == before["reserved_cents"] - 10_500 and a["cash_cents"] == before["cash_cents"] + 10_500
    assert conn.execute("SELECT qty FROM stock_positions WHERE assignment_id = %s", (a["id"],)).fetchone()["qty"] == 3
    ev = events(conn, approved)[-1]
    assert ev["from_status"] == "approved" and ev["to_status"] == "cancelled" and ev["actor"] == "owner:owner@example.com"
    after = audit(conn, "kill_cancel_all")[-1]["after"]
    assert after["stock_assignments_halted"] == [s.assignment["id"]] and after["stock_orders_cancelled"] == [approved]
    assert sorted(after["stock_orders_cancel_requested"]) == sorted([submitting, opened, partial])
    assert one(conn, s)["reason"] == "kill", "nothing is approved under the kill"


def test_kill_is_idempotent_and_auto_kill_actor(conn):
    s = stock_setup(conn)
    approved = one(conn, s)["order_id"]
    with conn.transaction():
        kill.auto_kill(conn, "stock_position_mismatch", {"symbol": "SPY"})
    assert events(conn, approved)[-1]["actor"] == "auto:stock_position_mismatch"
    with conn.transaction():
        assert kill.set_kill(conn, "owner@example.com") is False
    assert len(events(conn, approved)) == 2


def test_resume_refused_under_kill_and_after_reset_works(conn):
    s = stock_setup(conn)
    with conn.transaction():
        kill.set_kill(conn, "owner@example.com")
    with pytest.raises(Conflict, match="kill"):
        with conn.transaction():
            assignments.resume_assignment(conn, s.assignment["id"], "owner@example.com")
    with conn.transaction():
        kill.reset_kill(conn, "owner@example.com", "RESUME")
    assert assignment_row(conn, s.assignment["id"])["status"] == "halted", "a reset leaves assignments halted"
    with conn.transaction():
        row = assignments.resume_assignment(conn, s.assignment["id"], "owner@example.com")
    assert row["status"] == "active" and row["halt_reason"] is None


def test_live_off_halts_live_stock_assignments_only(conn):
    paper = stock_setup(conn)
    paper_order = one(conn, paper)["order_id"]
    live = stock_setup(conn, mode="live", symbols=("SPY",))
    approved = one(conn, live, symbol="SPY")["order_id"]
    opened = one(conn, live, symbol="SPY")["order_id"]
    set_order_status(conn, opened, "open")
    with conn.transaction():
        result = kill.live_off(conn, "owner@example.com", "owner")
    assert result["stock"]["stock_assignments_halted"] == [live.assignment["id"]]
    assert assignment_row(conn, live.assignment["id"])["status"] == "halted"
    assert order_row(conn, approved)["status"] == "cancelled" and order_row(conn, opened)["status"] == "cancel_requested"
    assert assignment_row(conn, paper.assignment["id"])["status"] == "active"
    assert order_row(conn, paper_order)["status"] == "approved", "paper orders are untouched by live off"
    assert audit(conn, "live_off")[-1]["after"]["stock"]["stock_orders_cancel_requested"] == [opened]


def test_live_off_without_stock_changes_keeps_its_result_shape(conn):
    with conn.transaction():
        result = kill.live_off(conn, "owner@example.com", "owner")
    assert "stock" not in result


def test_demotion_out_of_live_eligible_halts_live_assignments(conn):
    live = stock_setup(conn, mode="live", symbols=("SPY",))
    opened = one(conn, live, symbol="SPY")["order_id"]
    set_order_status(conn, opened, "open")
    # the paper gate: no paper marks at all, so the recompute leaves live_eligible
    with conn.transaction():
        assert eligibility.recompute(conn, live.model["id"]) == "paper_ok"
    assert assignment_row(conn, live.assignment["id"])["status"] == "halted"
    assert order_row(conn, opened)["status"] == "cancel_requested"
    changed = audit(conn, "stock_eligibility_changed")[-1]
    assert changed["before"] == {"status": "live_eligible"} and changed["after"]["live_assignments_halted"] == [live.assignment["id"]]


def test_thresholds_change_demotes_through_recompute_all(conn, client):
    s = stock_setup(conn)
    r = client.post("/api/settings", json={"thresholds_stock_backtest": {
        "min_sharpe": 5.0, "max_drawdown": 0.3, "min_trades": 30, "min_validation_sharpe": 0.0}})
    assert r.status_code == 200, r.text
    assert conn.execute("SELECT status FROM stock_models WHERE id = %s", (s.model["id"],)).fetchone()["status"] == "candidate"


def test_halt_cancels_approved_and_requests_the_rest(conn):
    s = stock_setup(conn)
    approved, submitting, *_ = _orders(conn, s)
    with conn.transaction():
        row = assignments.halt_assignment(conn, s.assignment["id"], "owner halt", "owner@example.com")
    assert row["status"] == "halted" and row["halt_reason"] == "owner halt"
    assert order_row(conn, approved)["status"] == "cancelled" and order_row(conn, submitting)["status"] == "cancel_requested"
    with conn.transaction():
        assert assignments.halt_assignment(conn, s.assignment["id"], "again", "owner@example.com")["status"] == "halted"


def test_worker_release_cancels_only_approved_and_hands_the_job_back(conn):
    s = stock_setup(conn)
    approved, submitting, opened, *_ = _orders(conn, s)
    worker = conn.execute("SELECT * FROM workers WHERE id = %s", (s.worker.id,)).fetchone()
    with conn.transaction():
        out = views_api.release_jobs(conn, worker, [s.job_id])
    assert out == {"cancelled": 1, "released": [s.job_id]}
    assert order_row(conn, approved)["status"] == "cancelled"
    assert order_row(conn, submitting)["status"] == "submitting" and order_row(conn, opened)["status"] == "open"
    assert events(conn, approved)[-1]["actor"] == f"worker:{s.worker.id}"
    assert conn.execute("SELECT status FROM jobs WHERE id = %s", (s.job_id,)).fetchone()["status"] == "queued"
    with conn.transaction():
        assert views_api.release_jobs(conn, worker, [s.job_id]) == {"cancelled": 0, "released": []}, "idempotent"


def test_cancelling_the_stock_trade_job_halts_the_assignment(conn):
    from host import queue

    s = stock_setup(conn)
    approved = one(conn, s)["order_id"]
    with conn.transaction():
        queue.cancel_job(conn, s.job_id, "owner@example.com")
    assert assignment_row(conn, s.assignment["id"])["status"] == "halted"
    assert order_row(conn, approved)["status"] == "cancelled"


def test_retire_halts_the_models_assignments(conn):
    from host.stocks import models

    s = stock_setup(conn)
    with conn.transaction():
        row = models.retire_model(conn, s.model["id"], "owner@example.com")
    assert row["status"] == "retired" and assignment_row(conn, s.assignment["id"])["status"] == "halted"
    with conn.transaction():
        assert eligibility.recompute(conn, s.model["id"]) == "retired", "retired is final"
    set_setting(conn, "kill_switch", False)
    with pytest.raises(Conflict):
        with conn.transaction():
            assignments.resume_assignment(conn, s.assignment["id"], "owner@example.com")
