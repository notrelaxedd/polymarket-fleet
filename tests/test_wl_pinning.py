"""Pinning guardrail (docs/workloads-design.md sections 4.2 and 8): a machine that is trading live
cannot be reassigned, and nothing reaches the Polymarket worker's machine by accident.

Assumptions where the contract is silent:
- The orders rule matches `orders.worker_id` against the machine's linked worker.
- A wrong confirmation phrase while the machine is still live is refused with 400 or 409 (the
  contract orders neither); both leave everything unchanged.
- `assign` answers 200 (or 202) when it starts a drain; the contract fixes only the row changes.
- The `workload_assign` audit row is asserted only for direct assignments, not for the drain start.
"""
from __future__ import annotations

import logging

import pytest

from host.errors import BadRequest, Conflict
from host.workloads import loop as wl_loop
from host.workloads import pinning
from host.workloads.assign import assign, desired_run, finish_drains
from host.workloads.machines import link_polymarket_worker
from host.workloads.pinning import is_live_trading, pin, refresh_pins, unpin
from tests.conftest import approved_order, expire_lease, insert_worker, lease_job, set_heartbeat_age, worker_row
from tests.wl_helpers import (
    INTRUDER, OWNER, assignment_of, audit_count, audit_for, bearer, call, demo_site_manifest, hello_manifest,
    insert_machine, insert_order, insert_workload, link_worker, live_trader, machine_row, make_manifest, mint_run,
    polymarket_manifest, release_trade_lease, set_worker_acked_idle, stop_trading, strict_client,
    machine_heartbeat_body,
)

OPEN_ORDER_STATUSES = ("approved", "submitting", "open", "partial", "cancel_requested")
CLOSED_ORDER_STATUSES = ("rejected", "filled", "cancelled", "rejected_by_exchange", "expired")


@pytest.fixture
def workloads(conn):
    insert_workload(conn, polymarket_manifest(), size_mb=150)
    insert_workload(conn, hello_manifest(), size_mb=60)
    insert_workload(conn, demo_site_manifest(), size_mb=1200)
    insert_workload(conn, make_manifest("tiny"), size_mb=20)


@pytest.fixture
def box(conn, workloads):
    """A box running Polymarket in a container at epoch 3."""
    return insert_machine(conn, "box1", workload="polymarket", epoch=3, state="running", acked_epoch=3)


def live(pool, machine) -> bool:
    return call(pool, is_live_trading, machine.id)


def refresh(pool) -> list[str]:
    return call(pool, refresh_pins)


def pinned(conn, machine) -> bool:
    return machine_row(conn, machine.id)["pinned"]


# ------------------------------------------------------------------ what pins


def test_live_leased_trade_job_pins_the_machine_and_audits(pool, conn, box):
    setup = live_trader(conn, box)
    assert live(pool, box) is True
    assert refresh(pool) == [box.id]
    row = machine_row(conn, box.id)
    assert row["pinned"] is True and row["pinned_reason"] == "live trading" and row["pinned_at"] is not None
    rows = audit_for(conn, action="machine_pin", entity=box.id)
    assert len(rows) == 1 and rows[0]["actor"] == "system"
    assert refresh(pool) == [], "only machines newly pinned are returned"
    assert len(audit_for(conn, action="machine_pin", entity=box.id)) == 1, "no second audit row"
    assert setup.job["status"] == "leased"


def test_a_paper_only_trade_worker_is_not_pinned(pool, conn, box):
    live_trader(conn, box, mode="paper")
    assert live(pool, box) is False
    assert refresh(pool) == []
    assert pinned(conn, box) is False
    assert audit_for(conn, action="machine_pin") == []


def test_only_the_machine_of_the_live_worker_is_pinned(pool, conn, box):
    other = insert_machine(conn, "box2", workload="hello", epoch=2, state="running")
    paper_box = insert_machine(conn, "box3")
    live_trader(conn, box)
    live_trader(conn, paper_box, mode="paper")
    assert refresh(pool) == [box.id]
    assert pinned(conn, other) is False and pinned(conn, paper_box) is False
    assert live(pool, other) is False


def test_a_machine_without_a_linked_worker_is_never_live(pool, conn, workloads):
    unlinked = insert_machine(conn, "nolink")
    assert live(pool, unlinked) is False
    assert refresh(pool) == []


def test_a_live_trade_job_held_by_a_worker_of_another_machine_does_not_pin_this_one(pool, conn, box):
    other = insert_machine(conn, "elsewhere")
    live_trader(conn, other)
    assert live(pool, box) is False
    assert refresh(pool) == [other.id]
    assert pinned(conn, box) is False


@pytest.mark.parametrize(
    "job_status, assignment_status, expected",
    [
        ("leased", "active", True),
        ("leased", "halted", True),
        ("cancel_requested", "active", True),
        ("cancel_requested", "halted", True),
        ("leased", "settled", False),
        ("leased", "cancelled", False),
        ("queued", "active", False),
        ("succeeded", "active", False),
        ("failed", "active", False),
        ("cancelled", "active", False),
    ],
)
def test_the_live_rule_looks_at_the_job_status_and_the_assignment_status(
    pool, conn, box, job_status, assignment_status, expected
):
    setup = live_trader(conn, box)
    if job_status in ("leased", "cancel_requested"):
        conn.execute("UPDATE jobs SET status = %s WHERE id = %s", (job_status, setup.job["id"]))
    else:
        conn.execute(
            "UPDATE jobs SET status = %s, lease_worker_id = NULL, lease_token = NULL, lease_expires_at = NULL WHERE id = %s",
            (job_status, setup.job["id"]),
        )
    conn.execute("UPDATE assignments SET status = %s WHERE id = %s", (assignment_status, setup.assignment["id"]))
    assert live(pool, box) is expected
    assert refresh(pool) == ([box.id] if expected else [])
    assert pinned(conn, box) is expected


def test_a_leased_job_that_is_not_a_trade_job_does_not_pin(pool, conn, box):
    """A backtest job that merely carries a live assignment's id in its params is not trading."""
    trader = live_trader(conn, insert_machine(conn, "trader-elsewhere"), leased=False)
    worker = insert_worker(conn, "w-backtest", role="backtest")
    link_worker(conn, box, worker)
    lease_job(conn, worker, kind="backtest", params={"assignment_id": str(trader.assignment["id"])})
    assert live(pool, box) is False
    assert refresh(pool) == []


def test_a_live_assignment_whose_job_is_queued_does_not_pin_by_itself(pool, conn, box):
    live_trader(conn, box, leased=False)
    assert live(pool, box) is False
    assert refresh(pool) == []


@pytest.mark.parametrize("status", OPEN_ORDER_STATUSES)
def test_an_open_live_order_pins_even_without_a_leased_job(pool, conn, box, status):
    setup = live_trader(conn, box, leased=False)
    insert_order(conn, setup.worker.id, setup.market["id"], mode="live", status=status, assignment_id=setup.assignment["id"])
    assert live(pool, box) is True
    assert refresh(pool) == [box.id]
    assert machine_row(conn, box.id)["pinned_reason"] == "live trading"


@pytest.mark.parametrize("status", CLOSED_ORDER_STATUSES)
def test_a_closed_live_order_does_not_pin(pool, conn, box, status):
    setup = live_trader(conn, box, leased=False)
    insert_order(conn, setup.worker.id, setup.market["id"], mode="live", status=status, assignment_id=setup.assignment["id"])
    assert live(pool, box) is False
    assert refresh(pool) == []


def test_an_open_paper_order_does_not_pin(pool, conn, box):
    setup = live_trader(conn, box, mode="paper", leased=False)
    insert_order(conn, setup.worker.id, setup.market["id"], mode="paper", status="open", assignment_id=setup.assignment["id"])
    assert live(pool, box) is False
    assert refresh(pool) == []


def test_an_open_live_order_of_another_worker_does_not_pin_this_machine(pool, conn, box):
    mine = live_trader(conn, box, mode="paper", leased=False)
    stranger = live_trader(conn, insert_machine(conn, "stranger-box"), leased=False)
    insert_order(conn, stranger.worker.id, stranger.market["id"], mode="live", status="open", assignment_id=stranger.assignment["id"])
    assert live(pool, box) is False
    assert mine.worker.id != stranger.worker.id


def test_a_real_approved_live_order_pins_after_the_lease_is_gone(pool, conn, box):
    setup = live_trader(conn, box)
    order = approved_order(conn, setup)
    assert order["mode"] == "live" and order["worker_id"] == setup.worker.id
    release_trade_lease(conn, setup)
    assert live(pool, box) is True, "the approved live order alone keeps the machine live"
    assert refresh(pool) == [box.id]


# ------------------------------------------------------------------ sticky pins, the loop


def test_pins_are_sticky_after_trading_stops(pool, conn, box):
    setup = live_trader(conn, box)
    refresh(pool)
    stop_trading(conn, setup)
    assert live(pool, box) is False
    assert refresh(pool) == []
    assert pinned(conn, box) is True
    wl_loop.run_once(pool)
    assert pinned(conn, box) is True


def test_the_loop_pass_pins_a_live_machine_and_leaves_a_paper_machine_alone(pool, conn, box):
    paper_box = insert_machine(conn, "paperbox")
    live_trader(conn, box)
    live_trader(conn, paper_box, mode="paper")
    wl_loop.run_once(pool)
    assert pinned(conn, box) is True
    assert machine_row(conn, box.id)["pinned_reason"] == "live trading"
    assert pinned(conn, paper_box) is False
    assert len(audit_for(conn, action="machine_pin", entity=box.id)) == 1
    wl_loop.run_once(pool)
    assert len(audit_for(conn, action="machine_pin", entity=box.id)) == 1


def test_the_host_loop_runs_the_workloads_pass_after_the_polymarket_steps(pool, conn, box):
    from host import loop as host_loop

    live_trader(conn, box)
    result = host_loop.run_once(pool)
    assert isinstance(result, dict)
    assert pinned(conn, box) is True


def test_a_workloads_failure_cannot_stop_the_polymarket_reaper(pool, conn, monkeypatch):
    from host import loop as host_loop
    worker = insert_worker(conn, "reap-w", role="backtest")
    job = lease_job(conn, worker, kind="backtest")
    expire_lease(conn, job["id"])

    def boom(*_a, **_k):
        raise RuntimeError("workloads exploded")

    monkeypatch.setattr(wl_loop, "run_once", boom)
    result = host_loop.run_once(pool)
    assert result["reaped"] == 1


def test_one_failing_step_does_not_stop_the_other_steps_of_the_workloads_pass(pool, conn, box, monkeypatch, caplog):
    from host.workloads import queue as wl_queue

    calls = []

    def boom(*_a, **_k):
        calls.append(1)
        raise RuntimeError("reaper exploded")

    monkeypatch.setattr(wl_queue, "reap", boom)
    live_trader(conn, box)
    with caplog.at_level(logging.WARNING):
        wl_loop.run_once(pool)
    assert pinned(conn, box) is True, "pinning still ran"
    if calls:
        assert any("reaper exploded" in (r.getMessage() + str(r.exc_info)) or r.levelno >= logging.WARNING for r in caplog.records)


def test_a_pinning_failure_does_not_stop_the_drain_step(pool, conn, workloads, monkeypatch):
    """finish_drains still runs when refresh_pins raises."""
    drain = insert_machine(conn, "drain1", workload="polymarket", epoch=3, state="running", acked_epoch=3)
    setup = live_trader(conn, drain, mode="paper")
    call(pool, assign, drain.id, "hello", "owner", None)
    release_trade_lease(conn, setup)
    set_worker_acked_idle(conn, setup.worker.id)

    def boom(*_a, **_k):
        raise RuntimeError("pin exploded")

    monkeypatch.setattr(pinning, "refresh_pins", boom)
    wl_loop.run_once(pool)
    assert assignment_of(conn, drain.id)["workload"] == "hello"


def test_the_loop_links_a_machine_to_its_worker_by_boot_id_and_then_pins_it(pool, conn, workloads):
    box = insert_machine(conn, "linkme", workload="polymarket", epoch=3, state="running", boot_id="boot-live-1")
    setup = live_trader(conn, insert_machine(conn, "scratch"))
    conn.execute("UPDATE machines SET polymarket_worker_id = NULL")
    conn.execute("UPDATE workers SET boot_id = 'boot-live-1' WHERE id = %s", (setup.worker.id,))
    wl_loop.run_once(pool)
    wl_loop.run_once(pool)
    assert machine_row(conn, box.id)["polymarket_worker_id"] == setup.worker.id
    assert pinned(conn, box) is True, "the live worker's machine is pinned once the link exists"


def test_link_polymarket_worker_needs_a_unique_boot_id_match(pool, conn, workloads):
    box = insert_machine(conn, "linkbox", boot_id="boot-a")
    w1 = live_trader(conn, insert_machine(conn, "tmp1"), mode="paper").worker
    w2 = live_trader(conn, insert_machine(conn, "tmp2"), mode="paper").worker
    conn.execute("UPDATE machines SET polymarket_worker_id = NULL WHERE id <> %s", (box.id,))
    conn.execute("UPDATE workers SET boot_id = 'boot-a' WHERE id = %s", (w1.id,))
    assert call(pool, link_polymarket_worker, machine_row(conn, box.id)) == w1.id
    assert machine_row(conn, box.id)["polymarket_worker_id"] == w1.id
    # A second worker with the same boot id makes the match ambiguous: no link is made.
    conn.execute("UPDATE machines SET polymarket_worker_id = NULL WHERE id = %s", (box.id,))
    conn.execute("UPDATE workers SET boot_id = 'boot-a' WHERE id = %s", (w2.id,))
    assert call(pool, link_polymarket_worker, machine_row(conn, box.id)) is None
    assert machine_row(conn, box.id)["polymarket_worker_id"] is None
    # No boot id on the machine, nothing to match.
    nobody = insert_machine(conn, "noboot", boot_id=None)
    assert call(pool, link_polymarket_worker, machine_row(conn, nobody.id)) is None


# ------------------------------------------------------------------ assign on a pinned machine


def snapshot(conn, machine):
    a = assignment_of(conn, machine.id)
    return {k: a[k] for k in ("workload", "epoch", "state", "run_token_hash", "draining_to", "draining_to_set")}


def test_assign_on_a_pinned_machine_is_409_and_changes_nothing(client, pool, conn, box):
    live_trader(conn, box)
    refresh(pool)
    token = mint_run(pool, box, 3)
    before = snapshot(conn, box)
    audits = audit_count(conn)
    r = client.post(f"/api/machines/{box.id}/assign", json={"workload": "hello"})
    assert r.status_code == 409, r.text
    assert "pinned" in r.json()["detail"] and "live trading" in r.json()["detail"]
    assert snapshot(conn, box) == before
    assert before["run_token_hash"] is not None and token
    assert audit_count(conn) == audits
    assert audit_for(conn, action="workload_assign") == []


def test_assigning_nothing_to_a_pinned_machine_is_409_too(client, pool, conn, box):
    live_trader(conn, box)
    refresh(pool)
    before = snapshot(conn, box)
    assert client.post(f"/api/machines/{box.id}/assign", json={"workload": None}).status_code == 409
    assert snapshot(conn, box) == before


def test_the_function_raises_conflict_on_a_pinned_machine(pool, conn, box):
    live_trader(conn, box)
    refresh(pool)
    with pytest.raises(Conflict):
        call(pool, assign, box.id, "hello", "owner", None)
    assert assignment_of(conn, box.id)["epoch"] == 3


def test_the_same_workload_is_a_no_op_even_on_a_pinned_machine(client, pool, conn, box):
    """Contract order: the same-workload no-op comes before the pinned check."""
    live_trader(conn, box)
    refresh(pool)
    before = snapshot(conn, box)
    r = client.post(f"/api/machines/{box.id}/assign", json={"workload": "polymarket"})
    assert r.status_code == 200, r.text
    assert snapshot(conn, box) == before
    assert audit_for(conn, action="workload_assign") == []


def test_pinned_is_checked_before_the_workload_is_looked_up_or_placed(client, pool, conn, box):
    live_trader(conn, box)
    refresh(pool)
    assert client.post(f"/api/machines/{box.id}/assign", json={"workload": "no-such"}).status_code == 409
    assert client.post(f"/api/machines/{box.id}/assign", json={"workload": "demo-site"}).status_code == 409


def test_assign_on_a_manually_pinned_machine_is_409(client, pool, conn, workloads):
    m = insert_machine(conn, "manual", workload="hello", epoch=2, state="running")
    r = client.post(f"/api/machines/{m.id}/pin", json={"reason": "demo in progress"})
    assert r.status_code == 200, r.text
    row = machine_row(conn, m.id)
    assert row["pinned"] is True and row["pinned_reason"] == "demo in progress"
    r = client.post(f"/api/machines/{m.id}/assign", json={"workload": "tiny"})
    assert r.status_code == 409 and "demo in progress" in r.json()["detail"]
    assert assignment_of(conn, m.id)["workload"] == "hello"
    assert len(audit_for(conn, action="machine_pin", entity=m.id)) == 1


def test_pin_function_records_reason_actor_and_audit(pool, conn, workloads):
    m = insert_machine(conn, "pinme")
    call(pool, pin, m.id, "owner says so", "owner@example.com", "100.64.0.1")
    row = machine_row(conn, m.id)
    assert row["pinned"] is True and row["pinned_reason"] == "owner says so" and row["pinned_at"] is not None
    audit = audit_for(conn, action="machine_pin", entity=m.id)
    assert len(audit) == 1 and audit[0]["actor"] == "owner@example.com"


# ------------------------------------------------------------------ gaps found in review, now required


def test_assign_also_checks_live_trading_directly_not_only_the_pin_flag(client, pool, conn, box):
    live_trader(conn, box)
    assert pinned(conn, box) is False, "refresh_pins has not run yet"
    assert live(pool, box) is True
    r = client.post(f"/api/machines/{box.id}/assign", json={"workload": "hello"})
    assert r.status_code == 409, "a machine that is trading live right now must not be reassigned"
    assert assignment_of(conn, box.id)["workload"] == "polymarket"


def test_a_machine_that_is_trading_live_cannot_be_disabled(client, pool, conn, box):
    live_trader(conn, box)
    refresh(pool)
    r = client.post(f"/api/machines/{box.id}/enabled", json={"enabled": False})
    assert r.status_code == 409
    assert machine_row(conn, box.id)["enabled"] is True


def test_disabling_the_polymarket_workload_does_not_stop_a_live_trading_machine(client, pool, conn, box):
    """Section 5.1 lists when `run` is null (nothing assigned, draining, machine disabled, image not
    published); a disabled workload is a placement matter only and must not stop a live trader."""
    live_trader(conn, box)
    refresh(pool)
    running = {"workload": "polymarket", "epoch": 3, "state": "running", "container_id": "abc", "exit_code": None,
               "restarts": 0, "cpu_pct": 1.0, "mem_mb": 300, "error": None}
    assert client.post("/api/workloads/polymarket/enabled", json={"enabled": False}).status_code == 200
    r = client.post(f"/api/v1/machines/{box.id}/heartbeat", json=machine_heartbeat_body(3, container=running), headers=box.headers)
    assert r.status_code == 200 and r.json()["run"] is not None


# ------------------------------------------------------------------ native fleet-worker


def test_assign_is_409_while_the_native_worker_runs(client, conn, workloads):
    m = insert_machine(conn, "native", native="active")
    r = client.post(f"/api/machines/{m.id}/assign", json={"workload": "hello"})
    assert r.status_code == 409, r.text
    assert "native" in r.json()["detail"]
    assert assignment_of(conn, m.id)["epoch"] == 1 and assignment_of(conn, m.id)["workload"] is None
    assert audit_for(conn, action="workload_assign") == []


def test_stopping_a_machine_with_an_active_native_worker_is_refused_too(client, conn, workloads):
    m = insert_machine(conn, "native2", native="active", workload="hello", epoch=2, state="running")
    assert client.post(f"/api/machines/{m.id}/assign", json={"workload": None}).status_code == 409
    assert assignment_of(conn, m.id)["workload"] == "hello"


def test_native_check_comes_before_the_workload_lookup_and_placement(client, conn, workloads):
    m = insert_machine(conn, "native3", native="active")
    assert client.post(f"/api/machines/{m.id}/assign", json={"workload": "no-such"}).status_code == 409
    assert client.post(f"/api/machines/{m.id}/assign", json={"workload": "demo-site"}).status_code == 409


@pytest.mark.parametrize("native", ["inactive", "absent"])
def test_an_inactive_or_absent_native_worker_does_not_block(client, conn, workloads, native):
    m = insert_machine(conn, "native-off", native=native)
    r = client.post(f"/api/machines/{m.id}/assign", json={"workload": "hello"})
    assert r.status_code == 200, r.text
    assert assignment_of(conn, m.id)["workload"] == "hello"


# ------------------------------------------------------------------ unpin


def pinned_idle_box(pool, conn, box):
    """A pinned box whose live trading has since stopped."""
    setup = live_trader(conn, box)
    refresh(pool)
    return setup


@pytest.mark.parametrize(
    "phrase",
    ["", "unpin box1", "UNPIN", "UNPIN  box1", "UNPIN box1 ", " UNPIN box1", "UNPIN box2", "Unpin box1", "box1", "RESUME", "UNPIN box1\n"],
)
def test_unpin_with_a_wrong_phrase_is_400_and_changes_nothing(client, pool, conn, box, phrase):
    setup = pinned_idle_box(pool, conn, box)
    stop_trading(conn, setup)
    audits = audit_count(conn)
    r = client.post(f"/api/machines/{box.id}/unpin", json={"confirm": phrase})
    assert r.status_code == 400, (phrase, r.text)
    assert pinned(conn, box) is True
    assert audit_count(conn) == audits


def test_unpin_without_a_phrase_is_400(client, pool, conn, box):
    stop_trading(conn, pinned_idle_box(pool, conn, box))
    assert client.post(f"/api/machines/{box.id}/unpin", json={}).status_code == 400
    assert client.post(f"/api/machines/{box.id}/unpin", json={"confirm": None}).status_code == 400
    assert pinned(conn, box) is True


def test_the_phrase_is_per_machine(client, pool, conn, box, workloads):
    other = insert_machine(conn, "box-two", pinned=True, pinned_reason="manual")
    stop_trading(conn, pinned_idle_box(pool, conn, box))
    r = client.post(f"/api/machines/{box.id}/unpin", json={"confirm": f"UNPIN {other.name}"})
    assert r.status_code == 400
    assert pinned(conn, box) is True and pinned(conn, other) is True


def test_unpin_with_the_right_phrase_while_still_live_is_409(client, pool, conn, box):
    pinned_idle_box(pool, conn, box)
    audits = audit_count(conn)
    r = client.post(f"/api/machines/{box.id}/unpin", json={"confirm": "UNPIN box1"})
    assert r.status_code == 409, r.text
    assert pinned(conn, box) is True
    assert audit_count(conn) == audits
    assert audit_for(conn, action="machine_unpin") == []


def test_unpin_with_the_right_phrase_while_an_order_is_still_open_is_409(client, pool, conn, box):
    setup = pinned_idle_box(pool, conn, box)
    release_trade_lease(conn, setup)
    conn.execute("UPDATE assignments SET status = 'settled' WHERE id = %s", (setup.assignment["id"],))
    insert_order(conn, setup.worker.id, setup.market["id"], mode="live", status="partial", assignment_id=setup.assignment["id"])
    assert client.post(f"/api/machines/{box.id}/unpin", json={"confirm": "UNPIN box1"}).status_code == 409
    assert pinned(conn, box) is True


def test_a_wrong_phrase_while_live_is_refused_and_changes_nothing(client, pool, conn, box):
    # Assumption: 400 (phrase) or 409 (live); the contract orders neither check.
    pinned_idle_box(pool, conn, box)
    r = client.post(f"/api/machines/{box.id}/unpin", json={"confirm": "nope"})
    assert r.status_code in (400, 409)
    assert pinned(conn, box) is True


def test_unpin_after_trading_stopped_works_and_audits_the_typed_phrase(client, pool, conn, box):
    stop_trading(conn, pinned_idle_box(pool, conn, box))
    r = client.post(f"/api/machines/{box.id}/unpin", json={"confirm": "UNPIN box1"}, headers=OWNER)
    assert r.status_code == 200, r.text
    assert pinned(conn, box) is False
    rows = audit_for(conn, action="machine_unpin", entity=box.id)
    assert len(rows) == 1
    assert rows[0]["confirmation_text"] == "UNPIN box1"
    assert rows[0]["actor"] == "owner@example.com"
    assert refresh(pool) == [], "nothing is live, so it is not pinned again"
    assert pinned(conn, box) is False


def test_an_unpinned_machine_can_be_reassigned_and_a_new_live_trade_pins_it_again(client, pool, conn, box):
    stop_trading(conn, pinned_idle_box(pool, conn, box))
    assert client.post(f"/api/machines/{box.id}/unpin", json={"confirm": "UNPIN box1"}).status_code == 200
    again = live_trader(conn, box)
    assert refresh(pool) == [box.id]
    assert client.post(f"/api/machines/{box.id}/assign", json={"workload": "hello"}).status_code == 409
    assert again.job["status"] == "leased"


def test_unpin_function_uses_the_current_machine_name(pool, conn, workloads):
    m = insert_machine(conn, "box.with-dots_1", pinned=True, pinned_reason="manual")
    with pytest.raises(BadRequest):
        call(pool, unpin, m.id, "UNPIN box", "owner", None)
    assert machine_row(conn, m.id)["pinned"] is True
    call(pool, unpin, m.id, "UNPIN box.with-dots_1", "owner", None)
    assert machine_row(conn, m.id)["pinned"] is False


# ------------------------------------------------------------------ who may pin, unpin and assign


def test_a_non_owner_cannot_assign_pin_or_unpin(config, conn, box):
    m = insert_machine(conn, "pinned-box", pinned=True, pinned_reason="manual", workload="hello", epoch=2, state="running")
    with strict_client(config) as c:
        for path, body in (("assign", {"workload": "tiny"}), ("pin", {"reason": "x"}), ("unpin", {"confirm": "UNPIN pinned-box"})):
            r = c.post(f"/api/machines/{m.id}/{path}", json=body, headers=INTRUDER)
            assert r.status_code == 401, (path, r.text)
            r = c.post(f"/api/machines/{m.id}/{path}", json=body)
            assert r.status_code == 401, (path, r.text)
    assert machine_row(conn, m.id)["pinned"] is True and assignment_of(conn, m.id)["workload"] == "hello"


def test_a_registered_machine_cannot_unpin_itself(config, conn, box):
    """Even with the owner's Tailscale login, a request from a machine's own IP is refused."""
    m = insert_machine(conn, "selfpin", pinned=True, pinned_reason="manual", remote_ip="100.64.0.7")
    with strict_client(config) as c:
        headers = {**OWNER, "X-Forwarded-For": "9.9.9.9, 100.64.0.7"}
        for path, body in (("unpin", {"confirm": "UNPIN selfpin"}), ("assign", {"workload": "hello"}), ("pin", {"reason": "x"})):
            r = c.post(f"/api/machines/{m.id}/{path}", json=body, headers=headers)
            assert r.status_code == 403, (path, r.text)
        ok = c.post(f"/api/machines/{m.id}/unpin", json={"confirm": "UNPIN selfpin"}, headers={**OWNER, "X-Forwarded-For": "100.64.0.99"})
        assert ok.status_code == 200, ok.text
    assert machine_row(conn, m.id)["pinned"] is False


def test_machine_and_run_tokens_are_not_owner_credentials(config, pool, conn, box):
    m = insert_machine(conn, "tokbox", pinned=True, pinned_reason="manual", workload="hello", epoch=2, state="running")
    run = mint_run(pool, m, 2)
    with strict_client(config) as c:
        for headers in (m.headers, bearer(run)):
            r = c.post(f"/api/machines/{m.id}/unpin", json={"confirm": "UNPIN tokbox"}, headers=headers)
            assert r.status_code == 401
    assert machine_row(conn, m.id)["pinned"] is True


# ------------------------------------------------------------------ leaving polymarket: draining


@pytest.fixture
def draining(pool, conn, box):
    """A box in a Polymarket container with a paper trade worker, after `assign hello`."""
    setup = live_trader(conn, box, mode="paper")
    before = worker_row(conn, setup.worker.id)
    call(pool, assign, box.id, "hello", "owner@example.com", None)
    return box, setup, before


def test_leaving_polymarket_starts_a_drain_and_does_not_move_the_epoch(draining, conn):
    box, setup, before = draining
    a = assignment_of(conn, box.id)
    assert a["state"] == "draining" and a["draining_to"] == "hello" and a["draining_to_set"] is True
    assert a["workload"] == "polymarket" and a["epoch"] == 3, "the epoch moves only when the drain finishes"


def test_leaving_polymarket_calls_the_existing_role_change(draining, conn):
    box, setup, before = draining
    after = worker_row(conn, setup.worker.id)
    assert before["desired_role"] == "trade" and after["desired_role"] == "idle"
    assert after["role_epoch"] == before["role_epoch"] + 1
    assert after["acked_epoch"] < after["role_epoch"], "the worker has not acked yet"
    assert audit_for(conn, action="set_role", entity=setup.worker.id), "set_role wrote its audit row"
    job = conn.execute("SELECT preempt_requested FROM jobs WHERE id = %s", (setup.job["id"],)).fetchone()
    assert job["preempt_requested"] is True, "the trade release handshake was started for the leased job"


def test_assign_via_the_api_starts_the_drain(client, pool, conn, box):
    setup = live_trader(conn, box, mode="paper")
    r = client.post(f"/api/machines/{box.id}/assign", json={"workload": "hello"})
    assert r.status_code in (200, 202), r.text
    assert assignment_of(conn, box.id)["state"] == "draining"
    assert worker_row(conn, setup.worker.id)["desired_role"] == "idle"


def test_finish_drains_waits_for_the_worker_to_ack_idle(draining, pool, conn):
    box, setup, _ = draining
    assert call(pool, finish_drains) == 0
    a = assignment_of(conn, box.id)
    assert a["state"] == "draining" and a["epoch"] == 3 and a["workload"] == "polymarket"


def test_finish_drains_waits_while_a_job_is_still_leased(draining, pool, conn):
    box, setup, _ = draining
    set_worker_acked_idle(conn, setup.worker.id)
    assert conn.execute("SELECT status FROM jobs WHERE id = %s", (setup.job["id"],)).fetchone()["status"] == "leased"
    assert call(pool, finish_drains) == 0
    assert assignment_of(conn, box.id)["state"] == "draining"


def test_finish_drains_waits_while_a_cancel_is_pending(draining, pool, conn):
    box, setup, _ = draining
    set_worker_acked_idle(conn, setup.worker.id)
    conn.execute("UPDATE jobs SET status = 'cancel_requested' WHERE id = %s", (setup.job["id"],))
    assert call(pool, finish_drains) == 0
    assert assignment_of(conn, box.id)["state"] == "draining"


def test_finish_drains_waits_when_the_worker_has_acked_but_still_reports_trade(draining, pool, conn):
    box, setup, _ = draining
    release_trade_lease(conn, setup)
    conn.execute("UPDATE workers SET acked_epoch = role_epoch, last_heartbeat_at = now() WHERE id = %s", (setup.worker.id,))
    assert worker_row(conn, setup.worker.id)["reported_role"] == "trade"
    assert call(pool, finish_drains) == 0


def test_finish_drains_completes_once_the_worker_acked_idle_with_no_leased_jobs(draining, pool, conn):
    box, setup, _ = draining
    release_trade_lease(conn, setup)
    set_worker_acked_idle(conn, setup.worker.id)
    assert call(pool, finish_drains) == 1
    a = assignment_of(conn, box.id)
    assert (a["workload"], a["epoch"], a["state"]) == ("hello", 4, "pending")
    assert a["draining_to"] is None and a["draining_to_set"] is False and a["run_token_hash"] is None
    assert call(pool, finish_drains) == 0, "nothing left to finish"


def test_the_loop_finishes_the_drain(draining, pool, conn):
    box, setup, _ = draining
    release_trade_lease(conn, setup)
    set_worker_acked_idle(conn, setup.worker.id)
    wl_loop.run_once(pool)
    assert assignment_of(conn, box.id)["workload"] == "hello"
    assert assignment_of(conn, box.id)["epoch"] == 4


def test_a_silent_worker_does_not_hold_the_drain_forever(draining, pool, conn):
    box, setup, _ = draining
    set_heartbeat_age(conn, setup.worker.id, 3600)
    assert call(pool, finish_drains) == 1
    assert assignment_of(conn, box.id)["workload"] == "hello"
    assert assignment_of(conn, box.id)["epoch"] == 4


def test_a_worker_that_just_heartbeated_without_acking_still_holds_the_drain(draining, pool, conn):
    box, setup, _ = draining
    set_heartbeat_age(conn, setup.worker.id, 1)
    assert call(pool, finish_drains) == 0


def test_only_the_ready_drain_finishes(pool, conn, workloads):
    a = insert_machine(conn, "drain-a", workload="polymarket", epoch=3, state="running", acked_epoch=3)
    b = insert_machine(conn, "drain-b", workload="polymarket", epoch=5, state="running", acked_epoch=5)
    sa = live_trader(conn, a, mode="paper")
    sb = live_trader(conn, b, mode="paper")
    call(pool, assign, a.id, "hello", "owner", None)
    call(pool, assign, b.id, "tiny", "owner", None)
    release_trade_lease(conn, sa)
    set_worker_acked_idle(conn, sa.worker.id)
    assert call(pool, finish_drains) == 1
    assert assignment_of(conn, a.id)["workload"] == "hello" and assignment_of(conn, a.id)["epoch"] == 4
    assert assignment_of(conn, b.id)["state"] == "draining" and assignment_of(conn, b.id)["epoch"] == 5
    assert sb.job["status"] == "leased"


def test_leaving_polymarket_without_a_linked_worker_moves_the_epoch_at_once(pool, conn, box):
    call(pool, assign, box.id, "hello", "owner", None)
    a = assignment_of(conn, box.id)
    assert (a["workload"], a["epoch"], a["state"]) == ("hello", 4, "pending")
    assert a["draining_to"] is None and a["draining_to_set"] is False
    assert len(audit_for(conn, action="workload_assign", entity=box.id)) == 1


def test_only_leaving_polymarket_drains(pool, conn, workloads):
    m = insert_machine(conn, "from-hello", workload="hello", epoch=2, state="running", acked_epoch=2)
    stale = live_trader(conn, m, mode="paper")
    call(pool, assign, m.id, "tiny", "owner", None)
    a = assignment_of(conn, m.id)
    assert (a["workload"], a["epoch"], a["state"]) == ("tiny", 3, "pending")
    assert worker_row(conn, stale.worker.id)["desired_role"] == "trade", "the worker's role was left alone"


def test_entering_polymarket_does_not_drain(pool, conn, workloads):
    m = insert_machine(conn, "to-pm", workload="hello", epoch=2, state="running", acked_epoch=2)
    call(pool, assign, m.id, "polymarket", "owner", None)
    a = assignment_of(conn, m.id)
    assert (a["workload"], a["epoch"]) == ("polymarket", 3)


def test_a_draining_machine_keeps_its_polymarket_container_until_the_drain_ends(client, pool, conn, box):
    setup = live_trader(conn, box, mode="paper")
    running = {"workload": "polymarket", "epoch": 3, "state": "running", "container_id": "abc123", "exit_code": None,
               "restarts": 0, "cpu_pct": 1.0, "mem_mb": 300, "error": None}
    body = machine_heartbeat_body(3, container=running)
    r = client.post(f"/api/v1/machines/{box.id}/heartbeat", json=body, headers=box.headers)
    assert r.status_code == 200, r.text
    assert r.json()["run"] is not None, "a normal assigned machine is told what to run"
    assert assignment_of(conn, box.id)["state"] == "running"
    call(pool, assign, box.id, "hello", "owner", None)
    r = client.post(f"/api/v1/machines/{box.id}/heartbeat", json=body, headers=box.headers)
    assert r.status_code == 200, r.text
    assert r.json()["run"] is not None and r.json()["workload"] == "polymarket", \
        "the container stays up until the worker finished the trade release handshake"
    assert r.json()["epoch"] == 3
    assert assignment_of(conn, box.id)["state"] == "draining", "a heartbeat does not end a drain"
    assert setup.worker.id


def test_desired_run_survives_a_drain_and_is_none_for_a_disabled_machine(pool, conn, box):
    live_trader(conn, box, mode="paper")
    row = machine_row(conn, box.id)
    assert call(pool, desired_run, row, assignment_of(conn, box.id)) is not None
    call(pool, assign, box.id, "hello", "owner", None)
    assert assignment_of(conn, box.id)["state"] == "draining"
    assert call(pool, desired_run, machine_row(conn, box.id), assignment_of(conn, box.id)) is not None
    other = insert_machine(conn, "disabled-box", workload="hello", epoch=2, state="running", enabled=False)
    assert call(pool, desired_run, machine_row(conn, other.id), assignment_of(conn, other.id)) is None
