"""The kill switch: flag, audit, scope (trade only), reset confirmation, CLI and dashboard."""
from __future__ import annotations

import os
import re
import threading
from typing import Callable

import pytest

from host import kill
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
