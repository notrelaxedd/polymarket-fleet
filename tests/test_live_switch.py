"""The typed live switch (docs/LIVE.md "Live switch"): the dated phrase, the three
preconditions, the immediate off, the settings API refusal, the kill, the audit
rows and the dashboard pill."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from host import kill
from host.errors import BadRequest, Conflict
from host.trading import live, orders
from host.trading.positions import owner_tz
from tests.conftest import (
    approve, approved_order, assignment_row, audit_rows, auth_state, bankroll_of, enable_live, order_row, post_loss,
    set_setting, trade_setup,
)

LIVE_GAME = "2026_05_BUF_MIA"


def ready(conn, **overrides) -> None:
    """The exchange has credentials, a fresh auth probe and no skew (live still off)."""
    cols = {"credentials_present": True, "auth_ok": True, "auth_checked_at": datetime.now(timezone.utc),
            "balance_cents": 50_000, "buying_power_cents": 50_000, "balance_checked_at": datetime.now(timezone.utc),
            "clock_skew_ms": 120}
    cols.update(overrides)
    auth_state(conn, **cols)


def live_flag(conn) -> object:
    return conn.execute("SELECT value FROM settings WHERE key = 'live_enabled'").fetchone()["value"]


def phrase(conn, days: int = 0) -> str:
    today = datetime.now(timezone.utc).astimezone(owner_tz(conn)).date() + timedelta(days=days)
    return f"ENABLE LIVE TRADING {today.isoformat()}"


def test_default_off_after_install(client, conn):
    assert live_flag(conn) is False
    state = client.get("/api/live").json()
    assert state["live_enabled"] is False and state["credentials_present"] is False and state["auth_ok"] is False
    assert state["live_enabled_at"] is None and state["live_enabled_by"] is None and state["auto_kill_reasons"] == []
    assert state["expected_phrase"] == phrase(conn) and state["killed"] is False
    assert len(state["problems"]) >= 2, "no credentials, no auth"
    assert client.get("/fragments/topbar").text.count(">PAPER<") == 1


def test_enable_requires_exact_dated_phrase(client, conn):
    ready(conn)
    good = phrase(conn)
    bad = [phrase(conn, -1), phrase(conn, 1), good.lower(), "ENABLE LIVE TRADING", good + " ", " " + good, good + "\n",
           good.replace("LIVE", "LIVE "), "enable live trading " + good[-10:], ""]
    for text in bad:
        r = client.post("/live", json={"confirm": text})
        assert r.status_code == 400, repr(text)
        assert good in r.json()["detail"], "the form shows the exact phrase to type"
        assert live_flag(conn) is False, repr(text)
    for body in ({}, {"confirm": None}, {"confirm": 5}, [good]):
        assert client.post("/live", json=body).status_code == 400, body
    assert client.post("/live", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert audit_rows(conn, "live_on") == []
    with pytest.raises(BadRequest):
        live.enable_live(conn, "owner", good.replace("-", "/"))
    r = client.post("/live", json={"confirm": good})
    assert r.status_code == 200, r.text
    assert r.json()["live_enabled"] is True and r.json()["live_enabled_by"] == "dev"
    assert live_flag(conn) is True
    state = conn.execute("SELECT live_enabled_at, live_enabled_by FROM exchange_state").fetchone()
    assert state["live_enabled_at"] is not None and state["live_enabled_by"] == "dev"
    # the form posts the same phrase as a form field
    client.post("/live/off")
    r = client.post("/live", data={"confirm": good})
    assert r.status_code == 200 and live_flag(conn) is True


def test_phrase_uses_the_owner_time_zone(conn):
    """At 03:00 UTC on the 5th it is still the 4th in New York: the phrase says the 4th."""
    set_setting(conn, "tz", "America/New_York")
    moment = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
    assert live.expected_phrase(conn, moment) == "ENABLE LIVE TRADING 2026-10-04"
    set_setting(conn, "tz", "Europe/Berlin")
    assert live.expected_phrase(conn, moment) == "ENABLE LIVE TRADING 2026-10-05"
    set_setting(conn, "tz", "UTC")
    assert live.expected_phrase(conn, moment) == "ENABLE LIVE TRADING 2026-10-05"


def test_enable_requires_fresh_auth(client, conn):
    good = phrase(conn)
    ready(conn, auth_checked_at=datetime.now(timezone.utc) - timedelta(minutes=11))
    r = client.post("/live", json={"confirm": good})
    assert r.status_code == 409 and "10 minutes" in r.json()["detail"] and live_flag(conn) is False
    ready(conn, auth_ok=False)
    assert client.post("/live", json={"confirm": good}).status_code == 409
    ready(conn, auth_checked_at=None, auth_ok=True)
    assert client.post("/live", json={"confirm": good}).status_code == 409
    ready(conn, clock_skew_ms=31_000)
    r = client.post("/live", json={"confirm": good})
    assert r.status_code == 409 and "skew" in r.json()["detail"] and live_flag(conn) is False
    set_setting(conn, "auto_kill", {"auth_failures": 3, "clock_skew_ms": 60_000})
    assert client.post("/live", json={"confirm": good}).status_code == 200, "the limit comes from settings"
    client.post("/live/off")
    ready(conn, auth_checked_at=datetime.now(timezone.utc) - timedelta(minutes=9))
    assert client.post("/live", json={"confirm": good}).status_code == 200 and live_flag(conn) is True
    assert audit_rows(conn, "live_on")[-1]["confirmation_text"] == good


def test_enable_refused_while_killed(client, conn):
    ready(conn)
    client.post("/api/kill")
    r = client.post("/live", json={"confirm": phrase(conn)})
    assert r.status_code == 409 and "kill" in r.json()["detail"] and live_flag(conn) is False
    assert client.get("/api/live").json()["killed"] is True
    client.post("/api/kill/reset", json={"confirm": "RESUME"})
    assert client.post("/live", json={"confirm": phrase(conn)}).status_code == 200 and live_flag(conn) is True


def test_enable_refused_without_credentials(client, conn):
    ready(conn, credentials_present=False)
    r = client.post("/live", json={"confirm": phrase(conn)})
    assert r.status_code == 409 and "credentials" in r.json()["detail"] and live_flag(conn) is False
    with pytest.raises(Conflict, match="credentials"):
        live.enable_live(conn, "owner", phrase(conn))
    ready(conn)
    assert client.post("/live", json={"confirm": phrase(conn)}).status_code == 200


def test_off_is_immediate_and_halts_live(client, conn):
    enable_live(conn)
    paper = trade_setup(conn)
    lv = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME, worker=paper.worker)
    p_open = approved_order(conn, paper, size=2)
    orders.set_status(conn, p_open["id"], "open", "executor", expected=("approved",))
    l_open = approved_order(conn, lv, size=3)
    orders.set_status(conn, l_open["id"], "open", "executor", expected=("approved",), exchange_order_id="ex-1")
    l_approved = approved_order(conn, lv, size=4)
    r = client.post("/live/off")
    assert r.status_code == 200 and r.json()["live_enabled"] is False and live_flag(conn) is False
    assert r.json()["live_off"]["assignments_halted"] == [str(lv.assignment["id"])]
    assert assignment_row(conn, lv.assignment["id"])["status"] == "halted"
    assert assignment_row(conn, paper.assignment["id"])["status"] == "active", "paper keeps going"
    assert order_row(conn, l_open["id"])["status"] == "cancel_requested", "cancelled through the exchange"
    assert order_row(conn, l_approved["id"])["status"] == "cancelled", "never submitted: cancelled at once"
    assert order_row(conn, p_open["id"])["status"] == "open"
    assert bankroll_of(conn, lv.assignment)["reserved_cents"] == l_open["cost_cents"]
    assert kill.is_killed(conn) is False, "off is not a kill"
    state = conn.execute("SELECT live_enabled_at, live_enabled_by FROM exchange_state").fetchone()
    assert state["live_enabled_at"] is None and state["live_enabled_by"] is None
    assert client.post("/live/off").status_code == 200, "idempotent"
    assert client.get("/api/live").json()["live_enabled"] is False


def test_settings_api_cannot_set_live_enabled(client, conn):
    ready(conn)
    for value in (True, False):
        r = client.post("/api/settings", json={"live_enabled": value})
        assert r.status_code == 400 and "use /live" in r.json()["detail"], value
    assert live_flag(conn) is False
    r = client.post("/api/settings", json={"live_enabled": True, "lease_seconds": 45})
    assert r.status_code == 400
    assert client.get("/api/settings").json()["lease_seconds"] == 30, "nothing of a refused update is written"
    assert client.post("/live", json={"confirm": phrase(conn)}).status_code == 200
    assert client.post("/api/settings", json={"live_enabled": False}).status_code == 400 and live_flag(conn) is True


def test_kill_turns_live_off(client, conn):
    enable_live(conn)
    lv = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME)
    row = approved_order(conn, lv, size=3)
    orders.set_status(conn, row["id"], "open", "executor", expected=("approved",), exchange_order_id="ex-2")
    assert client.get("/fragments/topbar").text.count(">LIVE<") == 1
    client.post("/api/kill")
    assert live_flag(conn) is False
    assert assignment_row(conn, lv.assignment["id"])["status"] == "halted"
    assert order_row(conn, row["id"])["status"] == "cancel_requested"
    state = client.get("/api/live").json()
    assert state["live_enabled"] is False and state["killed"] is True and state["live_enabled_at"] is None
    assert ">PAPER<" in client.get("/fragments/topbar").text
    client.post("/api/kill/reset", json={"confirm": "RESUME"})
    assert live_flag(conn) is False, "a reset never turns live back on"
    # the live daily-loss trip turns it off the same way (through kill.live_off)
    ready(conn)
    assert client.post("/live", json={"confirm": phrase(conn)}).status_code == 200
    client.post(f"/api/assignments/{lv.assignment['id']}/activate")
    assert assignment_row(conn, lv.assignment["id"])["status"] == "active"
    set_setting(conn, "max_daily_loss_cents", {"live": 1000, "paper": 1000})
    post_loss(conn, lv.assignment["bankroll"]["id"], 1000)
    assert approve(conn, lv, size=1)["reason"] == "daily_loss"
    assert live_flag(conn) is False and assignment_row(conn, lv.assignment["id"])["status"] == "halted"
    assert [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()][-2:] == ["live_off", "daily_loss_trip"]
    assert audit_rows(conn, "live_off")[-1]["after"]["reason"] == "daily_loss"


def test_audit_rows_live_on_off(client, conn):
    ready(conn)
    assert client.post("/live", json={"confirm": phrase(conn)}).status_code == 200
    on = audit_rows(conn, "live_on")
    assert len(on) == 1 and on[0]["actor"] == "dev" and on[0]["confirmation_text"] == phrase(conn)
    assert on[0]["before"] == {"live_enabled": False} and on[0]["after"]["live_enabled"] is True
    assert on[0]["after"]["balance_cents"] == 50_000 and on[0]["entity"] == "live_enabled"
    assert client.post("/live", json={"confirm": phrase(conn)}).status_code == 200, "idempotent"
    assert audit_rows(conn, "live_on")[-1]["before"] == {"live_enabled": True}
    r = client.post("/live/off", json={"reason": "owner button"})
    assert r.status_code == 200
    off = audit_rows(conn, "live_off")
    assert len(off) == 1 and off[0]["actor"] == "dev" and off[0]["after"]["reason"] == "owner button"
    assert off[0]["before"] == {"live_enabled": True} and off[0]["after"]["live_enabled"] is False
    assert off[0]["after"]["assignments_halted"] == [] and off[0]["after"]["orders_cancelled"] == 0
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]
    assert actions == ["live_on", "live_on", "live_off"]
    live.disable_live(conn, "cli", "drill")
    assert audit_rows(conn, "live_off")[-1]["after"]["reason"] == "drill" and audit_rows(conn, "live_off")[-1]["actor"] == "cli"


def test_dashboard_form_and_pill(client, conn):
    """GET /api/live carries everything the Settings group shows; the top bar pill
    reads PAPER until live is on and LIVE afterwards. The HTML form itself belongs
    to the dashboard template: asserted only when it is present."""
    ready(conn, last_auth_error=None)
    state = client.get("/api/live").json()
    for key in ("live_enabled", "live_enabled_at", "live_enabled_by", "credentials_present", "auth_ok", "auth_checked_at",
                "auth_age_s", "balance_cents", "buying_power_cents", "clock_skew_ms", "last_auth_error",
                "auto_kill_reasons", "expected_phrase"):
        assert key in state, key
    assert state["credentials_present"] is True and state["auth_ok"] is True and 0 <= state["auth_age_s"] < 5
    assert state["balance_cents"] == 50_000 and state["clock_skew_ms"] == 120 and state["problems"] == []
    topbar = client.get("/fragments/topbar").text
    assert ">PAPER<" in topbar and ">LIVE<" not in topbar
    assert client.post("/live", json={"confirm": state["expected_phrase"]}).status_code == 200
    topbar = client.get("/fragments/topbar").text
    assert ">LIVE<" in topbar and 'class="pill live"' in topbar
    page = client.get("/settings").text
    assert 'action="/settings/live/off"' in page and 'data-live-state="on"' in page, "the Disable button once on"
    client.post("/live/off", json={"reason": "test"})
    page = client.get("/settings").text
    assert 'action="/settings/live"' in page and state["expected_phrase"] in page, "the typed form shows the phrase"
