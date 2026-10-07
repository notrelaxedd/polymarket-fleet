"""The guardrail test helpers work against the 0010 schema (no workloads implementation needed)."""
from __future__ import annotations

import pytest

from host.workloads.manifest import Manifest
from tests.wl_helpers import (
    FakeSender, archive_manifest, assignment_of, database_text, demo_site_manifest, hello_manifest,
    insert_machine, insert_order, insert_outbound, insert_wl_job, insert_workload, lease_wl_job,
    link_worker, live_trader, machine_row, make_manifest, plain_manifest, polymarket_manifest,
    polymarket_snapshot, release_trade_lease, replace_manifest, set_assignment, set_worker_acked_idle,
    stop_trading, wl_events, wl_job,
)


def test_manifest_presets_validate_and_round_trip():
    for m in (hello_manifest(), polymarket_manifest(), archive_manifest(), demo_site_manifest(),
              plain_manifest("multi", "a", "b")):
        assert Manifest.from_json(m.to_json()) == m
    assert polymarket_manifest().can_trade and polymarket_manifest().resources.min_ram_mb == 3000
    assert archive_manifest().resources.write_heavy
    assert demo_site_manifest().resources.min_ram_mb == 8192


def test_workload_machine_and_assignment_rows(conn):
    insert_workload(conn, hello_manifest())
    insert_workload(conn, make_manifest("unpub"), published=False)
    row = conn.execute("SELECT * FROM workloads WHERE name = 'unpub'").fetchone()
    assert row["image_digest"] is None
    m = insert_machine(conn, "box1", workload="hello", epoch=4, state="running")
    assert machine_row(conn, m.id)["disk_type_detected"] == "flash"
    a = assignment_of(conn, m.id)
    assert (a["workload"], a["epoch"], a["state"]) == ("hello", 4, "running")
    a = set_assignment(conn, m.id, None, 5, "stopped")
    assert (a["workload"], a["epoch"], a["acked_epoch"]) == (None, 5, 5)
    replace_manifest(conn, make_manifest("hello", ram=256))
    stored = conn.execute("SELECT manifest->'resources'->>'min_ram_mb' AS r FROM workloads WHERE name='hello'").fetchone()
    assert stored["r"] == "256"


def test_live_trader_orders_and_stop(conn):
    m = insert_machine(conn, "trader-box")
    setup = live_trader(conn, m)
    assert machine_row(conn, m.id)["polymarket_worker_id"] == setup.worker.id
    assert setup.assignment["mode"] == "live" and setup.job["status"] == "leased"
    order = insert_order(conn, setup.worker.id, setup.market["id"], status="open", assignment_id=setup.assignment["id"])
    stop_trading(conn, setup)
    assert conn.execute("SELECT status FROM orders WHERE id = %s", (order["id"],)).fetchone()["status"] == "cancelled"
    assert conn.execute("SELECT status FROM assignments WHERE id = %s", (setup.assignment["id"],)).fetchone()["status"] == "settled"
    paper = live_trader(conn, insert_machine(conn, "paper-box"), mode="paper", leased=False)
    assert paper.assignment["mode"] == "paper"
    assert conn.execute("SELECT status FROM jobs WHERE id = %s", (paper.job["id"],)).fetchone()["status"] == "queued"
    set_worker_acked_idle(conn, paper.worker.id)
    link_worker(conn, m, paper.worker)
    release_trade_lease(conn, setup)
    snap = polymarket_snapshot(conn)
    assert snap["jobs"] and snap["orders"]


def test_workload_job_outbound_and_scan_helpers(conn):
    insert_workload(conn, plain_manifest("multi", "a", "b"))
    m = insert_machine(conn, "box2", workload="multi", epoch=2, state="running")
    job = insert_wl_job(conn, "multi", "a", params={"x": 1})
    leased = lease_wl_job(conn, job["id"], m, 2)
    assert leased["status"] == "leased" and leased["lease_token"] is not None
    assert wl_job(conn, job["id"])["lease_machine_id"] == m.id
    assert wl_events(conn, job["id"]) == []
    out = insert_outbound(conn, "multi", "log", status="approved", age_days=8)
    assert out["status"] == "approved"
    sender = FakeSender()
    assert sender.send({"id": out["id"]}, {"K": "v"}) == {"ok": True} and sender.ids == [str(out["id"])]
    text = database_text(conn)
    assert "multi" in text and "box2" in text
    with pytest.raises(RuntimeError):
        FakeSender(fail=RuntimeError("boom")).send({"id": 1}, {})


def test_a_live_order_can_be_approved_for_a_live_trader(conn):
    from tests.conftest import approved_order

    setup = live_trader(conn, insert_machine(conn, "approve-box"))
    order = approved_order(conn, setup)
    assert order["mode"] == "live" and order["worker_id"] == setup.worker.id and order["status"] == "approved"
