"""Order approval limits (docs/TRADING.md "Approval"): every check, its reason code,
the per-mode lock under concurrency, and the ledger invariants."""
from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from host.errors import BadRequest, Conflict
from host.trading import ledger, orders, positions, views
from host.trading.assignments import create_assignment
from host.trading.limits import approve_order, losses_today, order_cost_cents
from tests.conftest import (
    GAME_ID, approve, approved_order, assignment_row, bankroll_of, enable_live, insert_game, insert_market, insert_model,
    insert_snapshot, make_assignment, order_events, order_row, post_loss, set_setting, trade_setup,
    worker_row,
)

FEES = {"taker_rate": 0.05, "half_spread": 0.01}


def cost(price: float, size: int) -> int:
    return order_cost_cents(price, size, FEES)[0]


def audit_actions(conn: psycopg.Connection) -> list[str]:
    return [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]


def run_threads(n: int, fn) -> list[Any]:
    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n
    errors: list[BaseException] = []

    def runner(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[i] = fn(i)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    return results


def test_approves_within_limits(conn):
    s = trade_setup(conn)
    body = s.body()
    decision = approve_order(conn, s.worker_row, body)
    assert decision["status"] == "approved" and decision["reason"] is None, decision
    row = order_row(conn, decision["order_id"])
    expected = cost(0.52, 10)
    assert expected == 532 and row["cost_cents"] == expected and row["fee_cents_est"] == 12
    assert row["status"] == "approved" and row["mode"] == "paper" and row["worker_id"] == s.worker.id
    assert row["snapshot_id"] == s.snapshot["id"] and row["job_id"] == s.job["id"] and row["rationale"].startswith("my 0.58")
    bank = bankroll_of(conn, s.assignment)
    assert (bank["available_cents"], bank["reserved_cents"]) == (10_000 - expected, expected)
    assert ledger.replay_problems(conn) == []
    assert order_events(conn, row["id"]) == ["approved"]
    again = approve_order(conn, s.worker_row, body)
    assert again["status"] == "approved" and again["order_id"] == decision["order_id"] and again.get("duplicate") is True
    assert conn.execute("SELECT count(*) AS n FROM orders").fetchone()["n"] == 1, "a duplicate client id is not a second order"


def test_rejects_over_max_bet(conn):
    s = trade_setup(conn)
    assert cost(0.52, 50) > 2500
    decision = approve(conn, s, size=50)
    assert decision == {"status": "rejected", "order_id": decision["order_id"], "reason": "max_bet"}
    assert order_row(conn, decision["order_id"])["reject_reason"] == "max_bet"
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == 0


def test_max_bet_counts_open_same_market_orders(conn):
    s = trade_setup(conn)
    first = approve(conn, s, size=30)
    assert first["status"] == "approved" and cost(0.52, 30) == 1597
    second = approve(conn, s, size=30)
    assert second["status"] == "rejected" and second["reason"] == "max_bet", "1597 + 1597 > 2500"
    other = insert_market(conn, GAME_ID, side="away")
    snap = insert_snapshot(conn, other["id"])
    third = approve_order(conn, s.worker_row, s.body(size=30, market_id=str(other["id"]), snapshot_id=snap["id"]))
    assert third["status"] == "approved", "a different market has its own max bet"
    orders.cancel_order(conn, first["order_id"], "test", "free the market")
    fourth = approve(conn, s, size=30)
    assert fourth["status"] == "approved", "a cancelled order no longer counts"


def test_assignment_max_bet_lowers_never_raises_global(conn):
    low = trade_setup(conn, max_bet_cents=1000)
    decision = approve(conn, low, size=20)
    assert cost(0.52, 20) == 1065 and decision["reason"] == "max_bet"
    assert approve(conn, low, size=18)["status"] == "approved"
    high = trade_setup(conn, max_bet_cents=5000, game_id="2026_05_BUF_MIA")
    decision = approve(conn, high, size=50)
    assert decision["reason"] == "max_bet", "an assignment cap above max_bet_cents does not raise it"


def test_rejects_cost_over_available(conn):
    s = trade_setup(conn, bankroll_cents=500)
    decision = approve(conn, s, size=10)
    assert decision["reason"] == "bankroll"
    assert approve(conn, s, size=9)["status"] == "approved", "479 fits in 500"
    assert approve(conn, s, size=1)["reason"] == "bankroll", "53 does not fit in the 21 left"


def test_one_live_per_game_and_n_paper(conn):
    insert_game(conn)
    insert_market(conn)
    set_setting(conn, "max_paper_models_per_game", 2)
    a = make_assignment(conn)
    assert a["bankroll"]["available_cents"] == 10_000
    with pytest.raises(Conflict, match="already has a paper assignment"):
        make_assignment(conn, model_id=a["model_id"], game_id=GAME_ID)
    b = make_assignment(conn)
    assert a["job_id"] != b["job_id"]
    with pytest.raises(Conflict, match="max_paper_models_per_game"):
        make_assignment(conn)
    conn.execute("UPDATE assignments SET status = 'halted' WHERE id = %s", (b["id"],))
    with pytest.raises(Conflict, match="max_paper_models_per_game"):
        make_assignment(conn)
    conn.execute("UPDATE assignments SET status = 'settled' WHERE id = %s", (b["id"],))
    assert make_assignment(conn)["status"] == "active", "settled ones no longer count"
    retired = insert_model(conn, status="retired", params={"k": 1.0})
    with pytest.raises(BadRequest, match="retired"):
        create_assignment(conn, GAME_ID, retired["id"], "paper", 100, "test")
    eligible = insert_model(conn, status="live_eligible", params={"k": 2.0})
    with pytest.raises(Conflict, match="live_enabled"):
        create_assignment(conn, GAME_ID, eligible["id"], "live", 100, "test")
    enable_live(conn)
    live = create_assignment(conn, GAME_ID, eligible["id"], "live", 5000, "owner")
    assert live["mode"] == "live" and live["status"] == "active"
    other = insert_model(conn, status="live_eligible", params={"k": 3.0})
    with pytest.raises(Conflict, match="live assignment"):
        create_assignment(conn, GAME_ID, other["id"], "live", 5000, "owner")
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (live["job_id"],)).fetchone()
    assert job["kind"] == "trade" and job["max_expiries"] is None and job["idempotency_key"] == f"assignment:{live['id']}"
    assert job["params"] == {"assignment_id": str(live["id"])}
    assert audit_actions(conn).count("assignment_created") == 4


def test_rejects_when_losses_today_plus_cost_exceed_daily_loss(conn):
    s = trade_setup(conn)
    set_setting(conn, "max_daily_loss_cents", {"live": 30000, "paper": 2000})
    post_loss(conn, s.assignment["bankroll"]["id"], 1500)
    assert losses_today(conn, "paper")["losses_cents"] == 1500
    assert approve(conn, s, size=10)["reason"] == "daily_loss", "1500 + 532 > 2000"
    ok = approve(conn, s, size=9)
    assert ok["status"] == "approved", "1500 + 479 <= 2000"
    assert approve(conn, s, size=1)["reason"] == "daily_loss", "the open order's 479 is already at risk"
    assert assignment_row(conn, s.assignment["id"])["status"] == "active"


def test_daily_loss_per_mode(conn):
    set_setting(conn, "max_daily_loss_cents", {"live": 2000, "paper": 2000})
    paper = trade_setup(conn)
    post_loss(conn, paper.assignment["bankroll"]["id"], 1900)
    assert approve(conn, paper, size=10)["reason"] == "daily_loss"
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id="2026_05_BUF_MIA")
    assert losses_today(conn, "live")["losses_cents"] == 0
    assert approve(conn, live, size=10)["status"] == "approved", "paper losses never count against live"


def test_daily_loss_owner_tz_boundary(conn):
    s = trade_setup(conn)
    bank_id = s.assignment["bankroll"]["id"]
    start, end = positions.owner_day(conn)
    assert str(start.tzinfo) == "America/New_York" and start.hour == 0 and (end - start) >= timedelta(hours=23)
    post_loss(conn, bank_id, 700, ts=start - timedelta(seconds=1))
    assert losses_today(conn, "paper")["losses_cents"] == 0, "yesterday's loss in the owner's zone"
    assert losses_today(conn, "paper", now=start - timedelta(seconds=1))["losses_cents"] == 700
    post_loss(conn, bank_id, 300, ts=start + timedelta(seconds=1))
    assert losses_today(conn, "paper")["losses_cents"] == 300
    set_setting(conn, "tz", "UTC")
    utc_start, _ = positions.owner_day(conn)
    assert utc_start.hour == 0 and str(utc_start.tzinfo) == "UTC"


def test_live_daily_trip_halts_live_only_paper_continues(conn):
    set_setting(conn, "max_daily_loss_cents", {"live": 1000, "paper": 1000})
    live = trade_setup(conn, mode="live", model_status="live_eligible")
    paper = trade_setup(conn, game_id="2026_05_BUF_MIA")
    live_open = approved_order(conn, live, size=5)
    post_loss(conn, live.assignment["bankroll"]["id"], 1000)
    decision = approve(conn, live, size=1)
    assert decision["reason"] == "daily_loss"
    assert conn.execute("SELECT value FROM settings WHERE key = 'live_enabled'").fetchone()["value"] is False
    assert assignment_row(conn, live.assignment["id"])["status"] == "halted"
    assert order_row(conn, live_open["id"])["status"] == "cancelled", "approved, never submitted: cancelled with release"
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == 0
    assert audit_actions(conn).count("daily_loss_trip") == 1
    assert approve(conn, live, size=1)["reason"] == "assignment", "halted: no second trip"
    assert audit_actions(conn).count("daily_loss_trip") == 1
    # paper keeps rejecting until the day rolls, the assignment stays active
    assert approve(conn, paper, size=5)["status"] == "approved"
    post_loss(conn, paper.assignment["bankroll"]["id"], 1000)
    assert approve(conn, paper, size=1)["reason"] == "daily_loss"
    assert approve(conn, paper, size=1)["reason"] == "daily_loss"
    assert assignment_row(conn, paper.assignment["id"])["status"] == "active"
    assert "daily_loss_trip" not in audit_actions(conn)[audit_actions(conn).index("daily_loss_trip") + 1:]
    assert ledger.replay_problems(conn) == []


def test_exposure_off_by_default_enforced_when_set(conn):
    s = trade_setup(conn)
    assert approve(conn, s, size=30)["status"] == "approved"
    other = insert_market(conn, GAME_ID, side="away")
    snap = insert_snapshot(conn, other["id"])
    body = s.body(size=10, market_id=str(other["id"]), snapshot_id=snap["id"])
    set_setting(conn, "max_exposure_cents", {"live": 0, "paper": 2000})
    decision = approve_order(conn, s.worker_row, body)
    assert decision["reason"] == "exposure", "1597 reserved + 532 > 2000"
    set_setting(conn, "max_exposure_cents", {"live": 0, "paper": 0})
    body["client_request_id"] = "x" * 32
    assert approve_order(conn, s.worker_row, body)["status"] == "approved", "0 means off"


def test_rejects_below_floor_cited_or_newest(conn):
    s = trade_setup(conn, liquidity_usd_cents=10_000)
    assert approve(conn, s)["reason"] == "liquidity"
    good = insert_snapshot(conn, s.market["id"], liquidity_usd_cents=60_000)
    assert approve(conn, s, snapshot_id=good["id"])["status"] == "approved"
    insert_snapshot(conn, s.market["id"], liquidity_usd_cents=40_000)
    decision = approve(conn, s, snapshot_id=good["id"])
    assert decision["reason"] == "liquidity", "the newest book dried up even though the cited one was fine"


def test_rejects_stale_book(conn):
    s = trade_setup(conn)
    old = insert_snapshot(conn, s.market["id"], age_s=120)
    assert approve(conn, s, snapshot_id=old["id"])["reason"] == "stale_book", "120 s old and not the latest"
    assert approve(conn, s, snapshot_id=None)["reason"] == "stale_book"
    assert approve(conn, s, snapshot_id=s.snapshot["id"] + 999_999)["reason"] == "stale_book"
    recent = insert_snapshot(conn, s.market["id"], age_s=30)
    insert_snapshot(conn, s.market["id"], age_s=1)
    assert approve(conn, s, snapshot_id=recent["id"])["status"] == "approved", "30 s old is within book_max_age_s"
    lonely = trade_setup(conn, game_id="2026_05_BUF_MIA")
    conn.execute("UPDATE price_snapshots SET ts = now() - interval '10 minutes' WHERE id = %s", (lonely.snapshot["id"],))
    assert approve(conn, lonely)["reason"] == "stale_book", "the latest snapshot is stale too once it is older than book_max_age_s"
    conn.execute("UPDATE price_snapshots SET ts = now() - interval '50 seconds' WHERE id = %s", (lonely.snapshot["id"],))
    assert approve(conn, lonely)["status"] == "approved", "within book_max_age_s again"


def test_rejects_participation_cap(conn):
    s = trade_setup(conn, bankroll_cents=1_000_000)
    set_setting(conn, "max_bet_cents", 100_000)
    assert approve(conn, s, size=251)["reason"] == "participation", "half of the 500 at 0.52"
    assert approve(conn, s, size=250)["status"] == "approved"
    deep = approve(conn, s, size=300, price=0.53)
    assert deep["status"] == "approved", "0.53 reaches 1000 contracts of depth"
    empty = insert_snapshot(conn, s.market["id"], ask_depth=[])
    assert approve(conn, s, size=1, snapshot_id=empty["id"])["reason"] == "participation"


def test_rejects_unconfirmed_mapping(conn):
    s = trade_setup(conn)
    loose = insert_market(conn, GAME_ID, side="away", confirmed=False)
    snap = insert_snapshot(conn, loose["id"])
    assert approve(conn, s, market_id=str(loose["id"]), snapshot_id=snap["id"])["reason"] == "market"
    insert_game(conn, "2026_05_BUF_MIA")
    foreign = insert_market(conn, "2026_05_BUF_MIA")
    snap = insert_snapshot(conn, foreign["id"])
    assert approve(conn, s, market_id=str(foreign["id"]), snapshot_id=snap["id"])["reason"] == "market"
    conn.execute("UPDATE markets SET status = 'resolved' WHERE id = %s", (s.market["id"],))
    assert approve(conn, s)["reason"] == "market"
    nowhere = approve(conn, s, market_id=str(s.assignment["id"]))
    assert nowhere == {"status": "rejected", "order_id": None, "reason": "market"}


def test_rejects_after_kickoff(conn):
    s = trade_setup(conn, kickoff_in_s=-60)
    assert approve(conn, s)["reason"] == "kickoff"
    set_setting(conn, "trade_pregame_only", False)
    assert approve(conn, s)["status"] == "approved"
    conn.execute("UPDATE games SET status = 'final' WHERE game_id = %s", (GAME_ID,))
    assert approve(conn, s)["reason"] == "kickoff", "a final game is never traded"


def test_rejects_live_when_switch_off(conn):
    s = trade_setup(conn, mode="live", model_status="live_eligible")
    assert approve(conn, s)["status"] == "approved"
    set_setting(conn, "live_enabled", False)
    assert approve(conn, s)["reason"] == "mode"
    set_setting(conn, "live_enabled", True)
    conn.execute("UPDATE exchange_state SET auth_ok = false")
    assert approve(conn, s)["reason"] == "mode", "no exchange auth, no live order"


def test_rejects_live_when_lineage_not_eligible(conn):
    s = trade_setup(conn, mode="live", model_status="live_eligible")
    conn.execute("UPDATE models SET status = 'paper_ok' WHERE lineage_id = %s", (s.model["lineage_id"],))
    assert approve(conn, s)["reason"] == "mode"


def test_rejects_when_worker_role_not_trade(conn):
    s = trade_setup(conn)
    conn.execute("UPDATE workers SET desired_role = 'backtest', reported_role = 'backtest' WHERE id = %s", (s.worker.id,))
    assert approve(conn, s)["reason"] == "lease"


def test_rejects_when_job_preempted_or_cancel_requested(conn):
    s = trade_setup(conn)
    conn.execute("UPDATE jobs SET preempt_requested = true WHERE id = %s", (s.job["id"],))
    assert approve(conn, s)["reason"] == "lease"
    conn.execute("UPDATE jobs SET preempt_requested = false, status = 'cancel_requested' WHERE id = %s", (s.job["id"],))
    assert approve(conn, s)["reason"] == "lease"


def test_rejects_stale_lease_token(conn):
    s = trade_setup(conn)
    assert approve(conn, s, lease_token=str(s.assignment["id"]))["reason"] == "lease"
    assert approve(conn, s, lease_token="not-a-uuid")["reason"] == "lease"
    conn.execute("UPDATE jobs SET lease_token = gen_random_uuid() WHERE id = %s", (s.job["id"],))
    assert approve(conn, s)["reason"] == "lease", "the token was rotated"
    other = trade_setup(conn, game_id="2026_05_BUF_MIA")
    stolen = approve_order(conn, other.worker_row, s.body(lease_token=str(conn.execute(
        "SELECT lease_token FROM jobs WHERE id = %s", (s.job["id"],)).fetchone()["lease_token"])))
    assert stolen["reason"] == "lease", "another worker cannot use this job's token"
    row = order_row(conn, stolen["order_id"])
    assert row["worker_id"] == other.worker.id and row["status"] == "rejected"


def test_mode_from_assignment_not_request(conn):
    s = trade_setup(conn)
    decision = approve(conn, s, mode="live", live_enabled=True)
    assert decision["status"] == "approved"
    assert order_row(conn, decision["order_id"])["mode"] == "paper"
    assert ledger.get_bankroll(conn, s.assignment["bankroll"]["id"])["mode"] == "paper"


def test_payload_limit_fields_ignored(conn):
    s = trade_setup(conn)
    tricks = {"cost_cents": 1, "fee_cents_est": 0, "max_bet_cents": 10**9, "max_daily_loss_cents": 10**9,
              "participation": 1.0, "liquidity_floor_cents": 0, "available_cents": 10**9, "kill_switch": False}
    decision = approve(conn, s, size=10, **tricks)
    assert decision["status"] == "approved"
    assert order_row(conn, decision["order_id"])["cost_cents"] == 532, "the host computes the cost"
    assert approve(conn, s, size=50, **tricks)["reason"] == "max_bet"
    with pytest.raises(BadRequest):
        approve(conn, s, price=1.5)
    with pytest.raises(BadRequest):
        approve(conn, s, size=0)


def test_price_out_of_band(conn):
    s = trade_setup(conn)
    assert approve(conn, s, price=0.995)["reason"] == "price_band"
    cheap = insert_snapshot(conn, s.market["id"], bid=0.001, ask=0.005, ask_depth=[[0.005, 500]])
    assert approve(conn, s, price=0.005, snapshot_id=cheap["id"])["reason"] == "price_band"
    insert_snapshot(conn, s.market["id"])
    assert approve(conn, s, price=0.58)["reason"] == "price_band", "more than 5 cents through the ask"
    assert approve(conn, s, price=0.525)["reason"] == "price_band", "off the market tick"
    assert approve(conn, s, price=0.57)["status"] == "approved"


def test_concurrent_requests_cannot_oversubscribe_bankroll(pool, conn):
    s = trade_setup(conn, bankroll_cents=600)
    worker = worker_row(conn, s.worker.id)
    bodies = [s.body(size=10) for _ in range(10)]

    def request(i: int) -> dict[str, Any]:
        with pool.connection() as c:
            return approve_order(c, worker, bodies[i])

    decisions = run_threads(10, request)
    assert sum(1 for d in decisions if d["status"] == "approved") == 1
    assert {d["reason"] for d in decisions if d["status"] == "rejected"} == {"bankroll"}
    bank = bankroll_of(conn, s.assignment)
    assert bank["available_cents"] == 600 - 532 and bank["reserved_cents"] == 532
    assert ledger.replay_problems(conn) == []
    assert conn.execute("SELECT count(*) AS n FROM orders WHERE status = 'rejected'").fetchone()["n"] == 9


def test_concurrent_approvals_cannot_exceed_daily_loss_across_games(pool, conn):
    set_setting(conn, "max_daily_loss_cents", {"live": 30000, "paper": 2000})
    a = trade_setup(conn)
    b = trade_setup(conn, game_id="2026_05_BUF_MIA", worker=a.worker)
    post_loss(conn, a.assignment["bankroll"]["id"], 1468)
    worker = worker_row(conn, a.worker.id)
    bodies = [(a if i % 2 else b).body(size=10) for i in range(10)]

    def request(i: int) -> dict[str, Any]:
        with pool.connection() as c:
            return approve_order(c, worker, bodies[i])

    decisions = run_threads(10, request)
    assert sum(1 for d in decisions if d["status"] == "approved") == 1, "1468 + 532 = 2000 fits exactly one"
    assert {d["reason"] for d in decisions if d["status"] == "rejected"} == {"daily_loss"}
    today = losses_today(conn, "paper")
    assert today["losses_cents"] + today["reserved_cents"] == 2000


def test_rejections_persisted_with_reason(conn):
    s = trade_setup(conn)
    rejected = approve(conn, s, size=50)
    row = order_row(conn, rejected["order_id"])
    assert row["status"] == "rejected" and row["reject_reason"] == "max_bet" and row["client_request_id"]
    events = conn.execute("SELECT * FROM order_events WHERE order_id = %s", (row["id"],)).fetchall()
    assert len(events) == 1 and events[0]["to_status"] == "rejected" and events[0]["detail"] == {"reason": "max_bet"}
    assert events[0]["actor"] == s.worker.id
    approved = order_row(conn, approve(conn, s)["order_id"])
    assert approved["reject_reason"] is None
    listed = views.list_orders(conn, status="rejected")
    assert [r["id"] for r in listed] == [row["id"]] and listed[0]["worker_name"] == s.worker_row["name"]
    assert approve(conn, s, client_request_id=row["client_request_id"]) == {
        "status": "rejected", "order_id": str(row["id"]), "reason": "max_bet", "duplicate": True}


def test_ledger_append_only_trigger(conn):
    s = trade_setup(conn)
    approve(conn, s)
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE ledger SET d_reserved = 0 WHERE kind = 'reserve'")
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM ledger")
    assert conn.execute("SELECT count(*) AS n FROM ledger").fetchone()["n"] == 2, "fund + reserve survive"


def test_ledger_replay_matches_columns_with_open_positions(conn):
    s = trade_setup(conn)
    row = approved_order(conn, s, size=10)
    orders.set_status(conn, row["id"], "open", "executor", expected=("approved",))
    orders.record_fill(conn, row["id"], 0.52, 4, 10, "paper", "paper-sim", snapshot_id=s.snapshot["id"])
    pos = positions.positions(conn, s.assignment["id"])
    assert pos == [{"market_id": s.market["id"], "side": "home", "size": 4, "avg_price": 0.52, "basis_cents": 208}]
    assert ledger.replay_problems(conn) == []
    bank = bankroll_of(conn, s.assignment)
    assert bank["open_cost_cents"] == 208 and bank["realized_pnl_cents"] == -10
    assert bank["initial_cents"] + bank["realized_pnl_cents"] == bank["available_cents"] + bank["reserved_cents"] + bank["open_cost_cents"]
    insert_snapshot(conn, s.market["id"], bid=0.40, ask=0.42)
    today = losses_today(conn, "paper")
    assert today["unrealized_cents"] == round(0.41 * 4 * 100) - 208 == -44 and today["realized_cents"] == -10
    assert today["losses_cents"] == 54
    orders.record_fill(conn, row["id"], 0.52, 6, 2, "paper", "paper-sim")
    assert ledger.replay_problems(conn) == []
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == 0, "the fee estimate surplus went back"
