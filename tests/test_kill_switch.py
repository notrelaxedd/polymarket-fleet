"""The kill switch: flag, audit, scope (trade only), reset confirmation, CLI and dashboard."""
from __future__ import annotations

import os
import re
import threading
from typing import Callable

import pytest

from host import kill
from fastapi.testclient import TestClient

from host.cli import main
from host.errors import BadRequest
from host.settings import ROLES
from tests.conftest import flash_cookie, heartbeat_body, insert_job, job_row


def flag(conn) -> object:
    return conn.execute("SELECT value FROM settings WHERE key = 'kill_switch'").fetchone()["value"]


def audit(conn, action: str) -> list[dict]:
    return conn.execute(
        "SELECT actor, before, after, confirmation_text FROM audit_log WHERE action = %s ORDER BY id", (action,)
    ).fetchall()


def test_set_kill_sets_the_flag_and_audits(pool, conn):
    with pool.connection() as c:
        assert kill.is_killed(c) is False
        assert kill.set_kill(c, "owner") is True
        assert kill.is_killed(c) is True
        assert kill.set_kill(c, "owner") is False, "idempotent"
    assert flag(conn) is True
    rows = audit(conn, "kill")
    assert [(r["before"], r["after"]) for r in rows] == [
        ({"kill_switch": False}, {"kill_switch": True}),
        ({"kill_switch": True}, {"kill_switch": True}),
    ]
    assert all(r["actor"] == "owner" for r in rows)


def test_heartbeat_replies_carry_kill_for_every_role(client, conn, make_worker):
    client.post("/api/kill")
    for role in ROLES:
        w = make_worker(f"w-{role}", role=role)
        r = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body(role), headers=w.headers)
        assert r.status_code == 200 and r.json()["kill"] is True, role
    r = client.post("/api/v1/workers/register", json={"worker_id": w.id, "worker_token": w.token, "hostname": "box"})
    assert r.json()["kill"] is True
    client.post("/api/kill/reset", json={"confirm": "RESUME"})
    r = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("trade"),
                    headers={"Authorization": "Bearer " + r.json()["worker_token"]})
    assert r.json()["kill"] is False


def test_kill_blocks_trade_claims_but_not_backtest(client, conn, make_worker):
    trader = make_worker("trader", role="trade")
    tester = make_worker("tester", role="backtest")
    insert_job(conn, "trade")
    sleep = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 5}}).json()
    assert client.post("/api/kill").status_code == 200
    r = client.post(f"/api/v1/workers/{trader.id}/heartbeat", json=heartbeat_body("trade"), headers=trader.headers)
    assert r.json()["kill"] is True and r.json()["claimed"] == []
    r = client.post(f"/api/v1/workers/{tester.id}/heartbeat", json=heartbeat_body("backtest"), headers=tester.headers)
    assert r.json()["kill"] is True
    assert [c["id"] for c in r.json()["claimed"]] == [sleep["id"]], "batch roles keep claiming under kill"
    assert job_row(conn, sleep["id"])["status"] == "leased"


def test_api_kill_is_idempotent_and_reset_needs_resume(client, conn):
    for _ in range(3):
        r = client.post("/api/kill")
        assert r.status_code == 200 and r.json() == {"kill_switch": True}
    assert flag(conn) is True
    assert len(audit(conn, "kill")) == 3
    for body in ({"confirm": "resume"}, {"confirm": "RESUME "}, {"confirm": ""}, {}, {"confirm": None}, {"confirm": "yes"}):
        r = client.post("/api/kill/reset", json=body)
        assert r.status_code == 400, body
        assert "RESUME" in r.json()["detail"]
        assert flag(conn) is True, body
    assert client.post("/api/kill/reset", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert flag(conn) is True and audit(conn, "kill_reset") == []
    r = client.post("/api/kill/reset", json={"confirm": "RESUME"})
    assert r.status_code == 200 and r.json() == {"kill_switch": False}
    assert flag(conn) is False
    rows = audit(conn, "kill_reset")
    assert len(rows) == 1 and rows[0]["confirmation_text"] == "RESUME"
    assert rows[0]["before"] == {"kill_switch": True} and rows[0]["after"] == {"kill_switch": False}
    assert client.get("/api/settings").json()["kill_switch"] is False
    assert client.get("/api/fleet").json()["settings"]["kill_switch"] is False


def test_kill_pressed_during_an_open_reset_wins(pool, conn):
    """MEDIUM: set_kill locks the row, so a press that overlaps a reset transaction
    waits for it and re-applies; the final flag is true and the audit order matches."""
    with pool.connection() as c:
        kill.set_kill(c, "owner")
    reset_conn = pool.getconn()
    kill.reset_kill(reset_conn, "owner", "RESUME")  # written, not committed
    pressed = threading.Event()

    def press() -> None:
        with pool.connection() as c:
            kill.set_kill(c, "owner")
        pressed.set()

    presser = threading.Thread(target=press, daemon=True)
    presser.start()
    assert not pressed.wait(0.5), "the press must block on the open reset"
    reset_conn.commit()
    pool.putconn(reset_conn)
    assert pressed.wait(5)
    presser.join(5)
    assert flag(conn) is True
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]
    assert actions == ["kill", "kill_reset", "kill"]
    assert audit(conn, "kill")[-1]["before"] == {"kill_switch": False}


def test_reset_kill_direct(pool, conn):
    with pool.connection() as c:
        kill.set_kill(c, "owner")
    with pool.connection() as c:
        with pytest.raises(BadRequest):
            kill.reset_kill(c, "owner", "Resume")
        assert kill.is_killed(c) is True
        kill.reset_kill(c, "owner", "RESUME")
        assert kill.is_killed(c) is False
        kill.reset_kill(c, "owner", "RESUME")
    assert flag(conn) is False and len(audit(conn, "kill_reset")) == 2


@pytest.fixture
def cli(test_db_url: str, monkeypatch, capsys) -> Callable[..., tuple[int, str, str]]:
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("FLEET_DEV", "1")

    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


def test_cli_kill_and_reset(cli, conn, monkeypatch):
    code, out, _ = cli("kill")
    assert code == 0 and out.strip() == "kill_switch=true"
    assert flag(conn) is True
    code, out, _ = cli("kill")
    assert code == 0 and out.strip() == "kill_switch=true (already set)"
    monkeypatch.setattr("builtins.input", lambda prompt="": "nope")
    code, _, err = cli("kill-reset")
    assert code == 1 and "RESUME" in err and flag(conn) is True
    code, out, _ = cli("kill-reset", "--yes")
    assert code == 0 and out.strip() == "kill_switch=false" and flag(conn) is False
    cli("kill")
    monkeypatch.setattr("builtins.input", lambda prompt="": "RESUME")
    code, out, _ = cli("kill-reset")
    assert code == 0 and flag(conn) is False
    rows = audit(conn, "kill_reset")
    assert [r["actor"] for r in rows] == ["cli", "cli"] and rows[-1]["confirmation_text"] == "RESUME"


def test_dashboard_shows_the_killed_bar_and_the_reset_form(client, conn):
    home = client.get("/").text
    assert 'class="topbar"' in home and 'data-kill="1"' in home and "KILL" in home
    assert "TRADING KILLED" not in home
    settings = client.get("/settings").text
    assert 'action="/kill/reset"' not in settings
    r = client.post("/kill", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and flash_cookie(r).startswith("Trading killed")
    assert flag(conn) is True
    home = client.get("/").text
    assert 'class="topbar killed"' in home
    assert "TRADING KILLED. Reset in" in home and "Settings" in home
    assert 'data-kill="1"' not in home and ">KILLED<" in home
    # MEDIUM: the mode pill and the P&L stay visible under kill
    killed_status = re.search(r'id="topbar-status".*?</div>', home, re.S).group(0)
    assert ">PAPER<" in killed_status and "today $0.00 &middot; all $0.00" in killed_status
    topbar = client.get("/fragments/topbar").text
    assert 'data-killed="1"' in topbar and "<html" not in topbar
    settings = client.get("/settings").text
    assert 'action="/kill/reset"' in settings and 'name="confirm"' in settings
    r = client.post("/kill/reset", data={"confirm": "resume"}, follow_redirects=False)
    assert r.status_code == 400 and "RESUME" in r.text and 'action="/kill/reset"' in r.text
    assert flag(conn) is True
    r = client.post("/kill/reset", data={"confirm": " RESUME "}, follow_redirects=False)
    assert r.status_code == 400 and flag(conn) is True, "the form matches exactly, like the API"
    r = client.post("/kill/reset", data={"confirm": "RESUME"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings" and flash_cookie(r).startswith("Kill switch reset")
    assert flag(conn) is False
    assert "TRADING KILLED" not in client.get("/").text
    confirm = client.get("/kill/confirm").text
    assert 'method="post" action="/kill"' in confirm and "KILL TRADING" in confirm


# ------------------------------------------------------------------ step 4: the full kill

from datetime import datetime, timedelta, timezone  # noqa: E402

from fleet.models.base import Model  # noqa: E402
from host.api.app import create_app  # noqa: E402
from host.exchange import executor  # noqa: E402
from host.exchange.adapters.base import PaperGateway  # noqa: E402
from host.trading import ledger, orders, views  # noqa: E402
from host.trading.limits import approve_order  # noqa: E402
from tests.conftest import (  # noqa: E402
    approved_order, assignment_row, bankroll_of, enable_live, insert_snapshot, order_events, order_row,
    set_setting, trade_setup, worker_row,
)

PAPER_GAME, LIVE_GAME = "2026_05_KC_LV", "2026_05_BUF_MIA"


def setting(conn, key: str):
    return conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()["value"]


def test_kill_sets_flag_and_disables_live(pool, conn):
    enable_live(conn)
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME)
    with pool.connection() as c:
        assert kill.set_kill(c, "owner") is True
    assert flag(conn) is True and setting(conn, "live_enabled") is False
    assert assignment_row(conn, live.assignment["id"])["status"] == "halted"
    rows = conn.execute("SELECT action, after FROM audit_log ORDER BY id").fetchall()
    assert [r["action"] for r in rows][-2:] == ["kill", "kill_cancel_all"]
    assert rows[-1]["after"]["live_enabled"] is False and rows[-1]["after"]["assignments_halted"] == [str(live.assignment["id"])]
    with pool.connection() as c:
        assert kill.set_kill(c, "owner") is False
    assert [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()][-1] == "kill", \
        "a second press with nothing left to cancel writes only the kill row"


def test_kill_rejects_new_requests_first(pool, conn):
    s = trade_setup(conn)
    with pool.connection() as c:
        kill.set_kill(c, "owner")
    decision = approve_order(conn, worker_row(conn, s.worker.id), s.body())
    assert decision["status"] == "rejected" and decision["reason"] == "killed"
    assert order_row(conn, decision["order_id"])["reject_reason"] == "killed"
    with pool.connection() as c:
        kill.reset_kill(c, "owner", "RESUME")
    assert approve_order(conn, worker_row(conn, s.worker.id), s.body())["reason"] == "assignment", "halted by the kill"


def test_approved_unsubmitted_go_straight_to_cancelled_and_release_reserve(pool, conn):
    paper = trade_setup(conn)
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME, worker=paper.worker)
    p = approved_order(conn, paper, size=10)
    lv = approved_order(conn, live, size=10)
    assert bankroll_of(conn, paper.assignment)["reserved_cents"] == 532 == bankroll_of(conn, live.assignment)["reserved_cents"]
    with pool.connection() as c:
        kill.set_kill(c, "owner")
    for row in (p, lv):
        assert order_row(conn, row["id"])["status"] == "cancelled", "never submitted: nothing to ask the exchange"
        assert order_events(conn, row["id"]) == ["approved", "cancelled"]
    for a in (paper.assignment, live.assignment):
        bank = bankroll_of(conn, a)
        assert bank["reserved_cents"] == 0 and bank["available_cents"] == 10_000
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM ledger ORDER BY id").fetchall()]
    assert kinds.count("release") == 2 and ledger.replay_problems(conn) == []


def test_paper_open_orders_cancelled_in_the_kill_transaction(pool, conn):
    s = trade_setup(conn)
    row = approved_order(conn, s, size=10)
    orders.set_status(conn, row["id"], "open", "executor", expected=("approved",))
    orders.record_fill(conn, row["id"], 0.52, 4, 10, "paper", "paper-sim")
    killer = pool.getconn()
    kill.set_kill(killer, "owner")  # written, not committed
    assert flag(conn) is False and order_row(conn, row["id"])["status"] == "partial", "nothing visible before the commit"
    assert assignment_row(conn, s.assignment["id"])["status"] == "active"
    killer.commit()
    pool.putconn(killer)
    assert flag(conn) is True
    after = order_row(conn, row["id"])
    assert after["status"] == "cancelled" and after["filled_size"] == 4
    bank = bankroll_of(conn, s.assignment)
    assert bank["reserved_cents"] == 0 and bank["open_cost_cents"] == 208 and bank["available_cents"] == 10_000 - 218
    assert assignment_row(conn, s.assignment["id"])["status"] == "halted"
    assert ledger.replay_problems(conn) == []


def test_live_open_orders_become_cancel_requested(pool, conn):
    s = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME)
    rows = {}
    for status, size in (("submitting", 3), ("open", 4), ("partial", 5)):
        row = approved_order(conn, s, size=size)
        if status == "partial":
            orders.set_status(conn, row["id"], "open", "executor", expected=("approved",))
            orders.record_fill(conn, row["id"], 0.52, 1, 1, "live", "exchange", exchange_fill_id=f"f-{size}")
        else:
            orders.set_status(conn, row["id"], status, "executor", expected=("approved",))
        rows[status] = row
    reserved_before = bankroll_of(conn, s.assignment)["reserved_cents"]
    with pool.connection() as c:
        kill.set_kill(c, "owner")
    for status, row in rows.items():
        after = order_row(conn, row["id"])
        assert after["status"] == "cancel_requested", status
        assert order_events(conn, row["id"])[-1] == "cancel_requested"
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == reserved_before, "released only when the exchange confirms"
    orders.confirm_cancelled(conn, rows["open"]["id"], "exchange")
    assert order_row(conn, rows["open"]["id"])["status"] == "cancelled"
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == reserved_before - 213
    assert ledger.replay_problems(conn) == []


def test_kill_update_is_scoped(pool, conn):
    s = trade_setup(conn)
    untouched = {}
    for status in ("filled", "rejected", "cancelled", "expired", "rejected_by_exchange"):
        row = approved_order(conn, s, size=1)
        if status == "filled":
            orders.set_status(conn, row["id"], "open", "executor", expected=("approved",))
            orders.record_fill(conn, row["id"], 0.52, 1, 0, "paper", "paper-sim")
        else:
            orders.release_unfilled(conn, row, note="test")
            orders.set_status(conn, row["id"], status, "test")
        untouched[status] = order_row(conn, row["id"])
    live_order = approved_order(conn, s, size=2)
    with pool.connection() as c:
        kill.set_kill(c, "owner")
    for status, before in untouched.items():
        after = order_row(conn, before["id"])
        assert after["status"] == status and after["updated_at"] == before["updated_at"], status
        assert order_events(conn, before["id"])[-1] == status, "no kill event on a terminal order"
    assert order_row(conn, live_order["id"])["status"] == "cancelled"
    assert ledger.replay_problems(conn) == []


class SpyGateway(PaperGateway):
    """A paper gateway that records every place and lets a test run code inside it."""

    name = "spy"

    def __init__(self, on_place: Callable[[dict], None] | None = None, cancel_results: list | None = None) -> None:
        self.on_place = on_place
        self.cancel_results = list(cancel_results or [])
        self.placed: list[str] = []
        self.cancels = 0

    def place(self, order: dict) -> str:
        self.placed.append(order["client_request_id"])
        if self.on_place is not None:
            self.on_place(order)
        return "spy:" + order["client_request_id"]

    def cancel(self, order: dict) -> bool:
        self.cancels += 1
        outcome = self.cancel_results.pop(0) if self.cancel_results else True
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_executor_racing_kill_cannot_open(pool, conn):
    """The real kill lands while the executor is placing a `submitting` order: the kill's
    scoped update (under the approval locks) cancels the paper row with its release, and
    the executor, finding the row no longer `submitting`, tells the exchange to cancel by
    client id instead of opening it."""
    paper = trade_setup(conn)
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME, worker=paper.worker)
    p = approved_order(conn, paper, size=4)
    lv = approved_order(conn, live, size=4)

    def press_kill(order: dict) -> None:
        if order["id"] == lv["id"]:
            with pool.connection() as c:
                kill.set_kill(c, "owner")

    # The outbox is ordered by creation: the paper row opens first, then the kill lands
    # while the live row is at the gateway.
    gateway = SpyGateway(on_place=press_kill)
    with pool.connection() as c:
        counts = executor.Executor(gateway).tick(c)
    assert counts["submitted"] == 2 and gateway.placed == [p["client_request_id"], lv["client_request_id"]]
    assert flag(conn) is True
    paper_after, live_after = order_row(conn, p["id"]), order_row(conn, lv["id"])
    assert paper_after["status"] == "cancelled", "opened before the kill, cancelled by it (paper: at once)"
    assert order_events(conn, p["id"]) == ["approved", "submitting", "open", "cancelled"]
    assert live_after["status"] == "cancelled" and live_after["exchange_order_id"] == "spy:" + lv["client_request_id"]
    assert ("submitting", "open") not in [
        (e["from_status"], e["to_status"]) for e in
        conn.execute("SELECT from_status, to_status FROM order_events WHERE order_id = %s", (lv["id"],)).fetchall()
    ], "the executor never opened the row the kill had taken"
    assert order_events(conn, lv["id"]) == ["approved", "submitting", "cancel_requested", "cancelled"]
    assert gateway.cancels == 1, "the live row is cancelled on the exchange by client id"
    for a in (paper.assignment, live.assignment):
        assert bankroll_of(conn, a)["reserved_cents"] == 0 and assignment_row(conn, a["id"])["status"] == "halted"
    assert ledger.replay_problems(conn) == []


def test_executor_never_submits_while_killed(pool, conn):
    """After a real kill nothing approved reaches the gateway: the kill already cancelled
    the approved rows, a row that slips in `approved` under the flag is cancelled with
    its release by the executor itself, and nothing is placed until the reset."""
    s = trade_setup(conn)
    before = approved_order(conn, s, size=3)
    with pool.connection() as c:
        kill.set_kill(c, "owner")
    assert order_row(conn, before["id"])["status"] == "cancelled"
    # A row that slipped through as `approved` under the flag (as if a stale approval
    # had committed late): the executor must not place it either.
    slipped = conn.execute(
        """
        INSERT INTO orders (client_request_id, assignment_id, worker_id, job_id, market_id, mode, price, size,
                            cost_cents, fee_cents_est, snapshot_id, status)
        VALUES (%s, %s, %s, %s, %s, 'paper', 0.52, 2, 106, 2, %s, 'approved') RETURNING *
        """,
        ("slipped-" + s.job["lease_token"].hex[:8], s.assignment["id"], s.worker.id, s.job["id"], s.market["id"], s.snapshot["id"]),
    ).fetchone()
    ledger.reserve(conn, bankroll_of(conn, s.assignment)["id"], 106, slipped["id"])
    gateway = SpyGateway()
    ex = executor.Executor(gateway)
    with pool.connection() as c:
        for _ in range(3):
            counts = ex.tick(c)
            assert counts["submitted"] == 0
    assert gateway.placed == [], "nothing is placed while the flag is on"
    assert order_row(conn, slipped["id"])["status"] == "cancelled"
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []
    with pool.connection() as c:
        kill.reset_kill(c, "owner", "RESUME")
    with pool.connection() as c:
        assert ex.tick(c)["submitted"] == 0, "the reset places nothing by itself: the cancelled rows stay cancelled"
    assert gateway.placed == []


def test_kill_vs_approval_race(pool, conn):
    s = trade_setup(conn, bankroll_cents=1_000_000)
    conn.execute("UPDATE settings SET value = '1000000' WHERE key = 'max_bet_cents'")
    worker = worker_row(conn, s.worker.id)
    bodies = [s.body(size=1) for _ in range(20)]
    barrier = threading.Barrier(21)
    decisions: list[dict] = []
    errors: list[BaseException] = []

    def request(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            with pool.connection() as c:
                decisions.append(approve_order(c, worker, bodies[i]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def press() -> None:
        try:
            barrier.wait(timeout=10)
            with pool.connection() as c:
                kill.set_kill(c, "owner")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=request, args=(i,)) for i in range(20)] + [threading.Thread(target=press)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert len(decisions) == 20 and flag(conn) is True
    live_now = conn.execute(
        "SELECT count(*) AS n FROM orders WHERE status IN ('approved', 'submitting', 'open', 'partial', 'cancel_requested')"
    ).fetchone()["n"]
    assert live_now == 0, "after the kill nothing is approved or open"
    statuses = {r["status"] for r in conn.execute("SELECT status FROM orders").fetchall()}
    assert statuses <= {"rejected", "cancelled"}
    reasons = {d["reason"] for d in decisions if d["status"] == "rejected"}
    assert reasons <= {"killed", "assignment"}
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []


def test_kill_during_submitting_cancels_by_client_id(pool, conn):
    """Host side: a `submitting` order keeps its client_request_id (the exchange reconciles
    by it); paper is cancelled at once, live becomes cancel_requested. The exchange's
    own reconciliation is the exchange agent's test."""
    paper = trade_setup(conn)
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME, worker=paper.worker)
    p = approved_order(conn, paper, size=2)
    lv = approved_order(conn, live, size=2)
    for row in (p, lv):
        orders.set_status(conn, row["id"], "submitting", "executor", expected=("approved",))
    with pool.connection() as c:
        kill.set_kill(c, "owner")
    p_after, lv_after = order_row(conn, p["id"]), order_row(conn, lv["id"])
    assert p_after["status"] == "cancelled" and p_after["client_request_id"] == p["client_request_id"]
    assert lv_after["status"] == "cancel_requested" and lv_after["client_request_id"] == lv["client_request_id"]
    assert bankroll_of(conn, paper.assignment)["reserved_cents"] == 0
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == lv["cost_cents"] == 106


def test_live_cancel_retries_until_confirmed(pool, conn):
    """A live order open on the exchange becomes cancel_requested in the kill transaction;
    the exchange then retries the cancel at 1, 2, 4, 8 s (then every 8 s) until the
    gateway confirms, and only then releases the reservation and closes the row."""
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME)
    row = approved_order(conn, live, size=5)
    orders.set_status(conn, row["id"], "open", "executor", expected=("approved",), submitted_at=datetime.now(timezone.utc))
    with pool.connection() as c:
        kill.set_kill(c, "owner")
    assert order_row(conn, row["id"])["status"] == "cancel_requested"
    reserved = bankroll_of(conn, live.assignment)["reserved_cents"]
    assert reserved == row["cost_cents"] > 0, "released only when the exchange confirms"
    gateway = SpyGateway(cancel_results=[RuntimeError("down"), False, RuntimeError("down"), RuntimeError("down"), True])
    ex = executor.Executor(gateway)
    t0 = datetime.now(timezone.utc)
    expected = [(0, 1), (0.5, 1), (1, 2), (2, 2), (3, 3), (6, 3), (7, 4), (14, 4), (15, 5)]
    with pool.connection() as c:
        for offset, cancels in expected:
            ex.tick(c, t0 + timedelta(seconds=offset))
            assert gateway.cancels == cancels, f"at +{offset} s"
            if cancels < 5:
                assert order_row(conn, row["id"])["status"] == "cancel_requested"
    after = order_row(conn, row["id"])
    assert after["status"] == "cancelled" and order_events(conn, row["id"])[-2:] == ["cancel_requested", "cancelled"]
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []


def test_heartbeat_carries_kill(client, conn):
    s = trade_setup(conn)
    client.post("/api/kill")
    r = client.post(f"/api/v1/workers/{s.worker.id}/heartbeat", json=heartbeat_body("trade", want_jobs=2), headers=s.worker.headers)
    assert r.status_code == 200 and r.json()["kill"] is True and r.json()["claimed"] == []
    state = client.get("/api/v1/trade/state", headers=s.worker.headers).json()
    assert state["kill"] is True and state["assignments"][0]["status"] == "halted"


def test_kill_idempotent_and_persists_across_restart(client, config, conn):
    s = trade_setup(conn)
    approved_order(conn, s, size=1)
    for _ in range(3):
        assert client.post("/api/kill").json() == {"kill_switch": True}
    assert len(audit(conn, "kill")) == 3 and len(audit(conn, "kill_cancel_all")) == 1
    with TestClient(create_app(config)) as restarted:
        assert restarted.get("/api/settings").json()["kill_switch"] is True
        r = restarted.post(f"/api/v1/workers/{s.worker.id}/heartbeat", json=heartbeat_body("trade", want_jobs=1), headers=s.worker.headers)
        assert r.json()["kill"] is True and r.json()["claimed"] == []
        assert restarted.get("/api/v1/trade/state", headers=s.worker.headers).json()["kill"] is True


def test_cli_kill_works_without_api(cli, conn):
    s = trade_setup(conn)
    row = approved_order(conn, s, size=3)
    code, out, _ = cli("kill")
    assert code == 0 and out.strip() == "kill_switch=true"
    assert order_row(conn, row["id"])["status"] == "cancelled"
    assert assignment_row(conn, s.assignment["id"])["status"] == "halted"
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == 0
    code, out, _ = cli("ledger-check")
    assert code == 0 and "ledger ok" in out


def test_exchange_down_banner_after_15s(conn):
    """Host side of the banner: exchange_state reports `down` once the heartbeat is
    older than 15 s (or never seen); the HTML banner is the dashboard's test."""
    assert views.exchange_state(conn)["down"] is True, "never seen"
    conn.execute("UPDATE exchange_state SET heartbeat_at = now() - interval '5 seconds'")
    state = views.exchange_state(conn)
    assert state["down"] is False and 4 < state["heartbeat_age_s"] < 10
    conn.execute("UPDATE exchange_state SET heartbeat_at = now() - interval '16 seconds'")
    assert views.exchange_state(conn)["down"] is True


def test_reset_requires_exact_RESUME(pool, conn):
    with pool.connection() as c:
        kill.set_kill(c, "owner")
    for text in ("resume", "RESUME ", " RESUME", "", None, "Resume", "RESUME\n"):
        with pool.connection() as c:
            with pytest.raises(BadRequest):
                kill.reset_kill(c, "owner", text)
        assert flag(conn) is True, repr(text)
    with pool.connection() as c:
        kill.reset_kill(c, "owner", "RESUME")
    assert flag(conn) is False


def test_reset_keeps_assignments_halted(client, conn):
    paper = trade_setup(conn)
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME, worker=paper.worker)
    client.post("/api/kill")
    for a in (paper.assignment, live.assignment):
        assert assignment_row(conn, a["id"])["status"] == "halted"
    r = client.post(f"/api/assignments/{paper.assignment['id']}/activate")
    assert r.status_code == 409 and "kill" in r.json()["detail"]
    assert client.post("/api/assignments/activate-paper").status_code == 409
    client.post("/api/kill/reset", json={"confirm": "RESUME"})
    for a in (paper.assignment, live.assignment):
        assert assignment_row(conn, a["id"])["status"] == "halted", "a reset clears the flag only"
    r = client.post("/api/assignments/activate-paper")
    assert r.status_code == 200 and r.json() == {"activated": 1}
    assert assignment_row(conn, paper.assignment["id"])["status"] == "active"
    assert assignment_row(conn, live.assignment["id"])["status"] == "halted", "live stays halted (live_enabled is off)"
    r = client.post(f"/api/assignments/{live.assignment['id']}/activate")
    assert r.status_code == 409 and "live_enabled" in r.json()["detail"]
    enable_live(conn)
    assert client.post(f"/api/assignments/{live.assignment['id']}/activate").json()["status"] == "active"


def test_every_status_change_writes_order_event(pool, conn):
    s = trade_setup(conn)
    row = approved_order(conn, s, size=10)
    orders.set_status(conn, row["id"], "submitting", "executor", expected=("approved",))
    orders.set_status(conn, row["id"], "open", "executor", expected=("submitting",), exchange_order_id="x1")
    orders.record_fill(conn, row["id"], 0.52, 3, 5, "paper", "paper-sim")
    with pool.connection() as c:
        kill.set_kill(c, "owner")
    assert order_events(conn, row["id"]) == ["approved", "submitting", "open", "partial", "cancelled"]
    history = [r["status"] for r in conn.execute(
        "SELECT to_status AS status FROM order_events WHERE order_id = %s ORDER BY id", (row["id"],)).fetchall()]
    assert history[-1] == order_row(conn, row["id"])["status"]
    rejected = approve_order(conn, worker_row(conn, s.worker.id), s.body(size=1))
    assert order_events(conn, rejected["order_id"]) == ["rejected"]
    actors = {r["actor"] for r in conn.execute("SELECT actor FROM order_events").fetchall()}
    assert actors >= {s.worker.id, "executor", "paper-sim", "owner"}


class _ConstantModel(Model):
    """A model with a fixed home-win probability, for a proposal that must have an edge."""

    family = "constant"

    def __init__(self, p: float) -> None:
        super().__init__({})
        self.p = p

    def predict(self, game: dict, market_p: float | None, features: dict) -> float:
        return self.p


class _TradeAgent:
    """What TradeLoop needs of the agent."""

    conf = {"host_url": "http://host.test", "worker_token": "unused"}
    kill = False
    trade_jobs: dict = {}

    class options:
        http_timeout = 2.0
        trade_tick_s = None
        trade_max_games = None


def test_trade_worker_stops_proposing_under_kill(pool, conn, monkeypatch):
    """fleet/worker/trade.py against the real host state and approval: a tick before the
    kill proposes and gets an approval; the kill turns the state's kill flag on and halts
    the assignment, so the next ticks post nothing; after the reset the assignment is
    still halted (nothing proposed) until it is activated again."""
    from fleet.worker import trade as trade_module
    from host.api.serialize import jsonable
    from host.trading.assignments import activate_assignment, trade_state

    s = trade_setup(conn)
    set_setting(conn, "min_edge", 0.0)
    posts: list[str] = []

    def post_json(url: str, body: dict, token: str | None = None, timeout: float = 4.0) -> dict:
        posts.append(url.replace(_TradeAgent.conf["host_url"], ""))
        assert url.endswith("/orders/request"), url
        return approve_order(conn, worker_row(conn, s.worker.id), body)

    monkeypatch.setattr(trade_module.http, "post_json", post_json)
    loop = trade_module.TradeLoop(_TradeAgent())
    loop._models[str(s.model["id"])] = _ConstantModel(0.9)

    def state() -> dict:
        return jsonable(trade_state(conn, s.worker.id))

    posted = loop.tick(state())
    assert len(posted) == 1 and posted[0]["result"]["status"] == "approved" and posts == ["/api/v1/orders/request"]
    approved = posted[0]["result"]["order_id"]
    assert order_row(conn, approved)["status"] == "approved"

    with pool.connection() as c:
        kill.set_kill(c, "owner")
    killed = state()
    assert killed["kill"] is True and killed["assignments"][0]["status"] == "halted"
    assert killed["assignments"][0]["open_orders"] == [], "the kill cancelled the approved order"
    posts.clear()
    for _ in range(3):
        assert loop.tick(killed) == []
    assert posts == [] and loop.last_tick["kill"] is True and loop.last_tick["assignments"] == 1
    assert order_row(conn, approved)["status"] == "cancelled"

    with pool.connection() as c:
        kill.reset_kill(c, "owner", "RESUME")
    resumed = state()
    assert resumed["kill"] is False and resumed["assignments"][0]["status"] == "halted"
    assert loop.tick(resumed) == [] and posts == [], "halted after the reset: still nothing"

    activate_assignment(conn, s.assignment["id"], "owner")
    insert_snapshot(conn, s.market["id"])  # a fresh book gives the proposal a new client_request_id
    posted = loop.tick(state())
    assert len(posted) == 1 and posted[0]["result"]["status"] == "approved" and posted[0]["result"].get("duplicate") is None
    assert posts == ["/api/v1/orders/request"]


def test_role_switch_away_from_trade_cancels_first(client, conn):
    """Host side of the handshake: POST /api/v1/trade/release cancels the worker's open
    orders and hands its trade jobs back with reason `drain` before the ack heartbeat."""
    s = trade_setup(conn)
    row = approved_order(conn, s, size=4)
    orders.set_status(conn, row["id"], "open", "executor", expected=("approved",))
    client.post(f"/api/workers/{s.worker.id}/role", json={"role": "backtest"})
    r = client.post("/api/v1/trade/release", headers=s.worker.headers,
                    json={"jobs": [{"id": str(s.job["id"]), "lease_token": str(s.job["lease_token"])}]})
    assert r.status_code == 200 and r.json() == {"cancelled": 1, "pending": 0, "released": [str(s.job["id"])]}
    assert order_row(conn, row["id"])["status"] == "cancelled"
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == 0
    job = job_row(conn, s.job["id"])
    assert job["status"] == "queued" and job["lease_worker_id"] is None
    events = conn.execute("SELECT event, detail FROM job_events WHERE job_id = %s ORDER BY id", (s.job["id"],)).fetchall()
    assert events[-1]["event"] == "released" and events[-1]["detail"] == {"status": "queued", "reason": "drain"}
    r = client.post(f"/api/v1/workers/{s.worker.id}/heartbeat", json=heartbeat_body("backtest", acked_epoch=2), headers=s.worker.headers)
    assert r.json()["desired_role"] == "backtest" and r.json()["lost"] == []
    assert assignment_row(conn, s.assignment["id"])["status"] == "active", "another trade worker will pick the job up"


# ------------------------------------------------------------------ step 5: auto-kill

from host.trading.live import live_state  # noqa: E402
from tests.conftest import audit_rows  # noqa: E402


def test_auto_kill_sets_flag_with_auto_actor_and_audit(pool, conn):
    """kill.auto_kill is the kill transaction pressed by the exchange process: actor
    `auto:<reason>`, the normal kill sweep (live off, orders cancelled, assignments
    halted) plus one `auto_kill` audit row carrying the detail."""
    enable_live(conn)
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME)
    row = approved_order(conn, live, size=2)
    orders.set_status(conn, row["id"], "open", "executor", expected=("approved",), exchange_order_id="ex-9")
    with pool.connection() as c:
        assert kill.auto_kill(c, "auth_failures", {"failures": 3, "last_error": "401"}) is True
    assert flag(conn) is True and setting(conn, "live_enabled") is False
    assert assignment_row(conn, live.assignment["id"])["status"] == "halted"
    assert order_row(conn, row["id"])["status"] == "cancel_requested", "live orders go to the exchange for cancelling"
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]
    assert actions[-3:] == ["kill", "kill_cancel_all", "auto_kill"]
    assert audit(conn, "kill")[-1]["actor"] == "auto:auth_failures"
    auto = audit_rows(conn, "auto_kill")
    assert auto[-1]["actor"] == "auto:auth_failures" and auto[-1]["entity"] == "auth_failures"
    assert auto[-1]["before"] == {"kill_switch": False}
    assert auto[-1]["after"] == {"reason": "auth_failures", "failures": 3, "last_error": "401"}
    with pool.connection() as c:
        assert kill.auto_kill(c, "unknown_order", {"count": 1}) is False, "already killed"
    assert audit_rows(conn, "auto_kill")[-1]["before"] == {"kill_switch": True}
    assert live_state(conn)["auto_kill_reasons"] == ["auth_failures", "unknown_order"]


def test_auto_kill_reason_cleared_by_resume_and_live_stays_off(client, conn):
    enable_live(conn)
    kill.auto_kill(conn, "clock_skew", {"skew_ms": 50_000})
    state = client.get("/api/live").json()
    assert state["killed"] and state["auto_kill_reasons"] == ["clock_skew"] and state["live_enabled"] is False
    for body in ({"confirm": "resume"}, {}):
        assert client.post("/api/kill/reset", json=body).status_code == 400
        assert client.get("/api/live").json()["auto_kill_reasons"] == ["clock_skew"]
    assert client.post("/api/kill/reset", json={"confirm": "RESUME"}).status_code == 200
    state = client.get("/api/live").json()
    assert state["killed"] is False and state["auto_kill_reasons"] == [] and state["live_enabled"] is False
    assert client.get("/fragments/topbar").text.count(">PAPER<") == 1


def test_live_off_halts_live_and_cancels_through_exchange(pool, conn):
    """kill.live_off: not a kill. live_enabled false, live assignments halted with
    their orders cancel_requested, paper untouched, one live_off audit row."""
    enable_live(conn)
    paper = trade_setup(conn)
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME, worker=paper.worker)
    p = approved_order(conn, paper, size=1)
    lv = approved_order(conn, live, size=2)
    orders.set_status(conn, lv["id"], "open", "executor", expected=("approved",), exchange_order_id="ex-10")
    with pool.connection() as c:
        result = kill.live_off(c, "owner", "owner")
    assert result == {"live_enabled": False, "was_on": True, "reason": "owner", "assignments_halted": [str(live.assignment["id"])],
                      "orders_cancelled": 0, "orders_cancel_requested": 1}
    assert flag(conn) is False and setting(conn, "live_enabled") is False
    assert order_row(conn, lv["id"])["status"] == "cancel_requested" and order_row(conn, p["id"])["status"] == "approved"
    assert assignment_row(conn, paper.assignment["id"])["status"] == "active"
    assert audit_rows(conn, "live_off")[-1]["after"]["assignments_halted"] == [str(live.assignment["id"])]
    with pool.connection() as c:
        assert kill.live_off(c, "owner", "again")["was_on"] is False, "idempotent"
    gateway = SpyGateway()
    with pool.connection() as c:
        executor.Executor(gateway).tick(c)
    assert gateway.cancels == 1 and order_row(conn, lv["id"])["status"] == "cancelled"
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []
