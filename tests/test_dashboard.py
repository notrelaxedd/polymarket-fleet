"""Dashboard pages, fragments and form posts (TestClient in FLEET_DEV mode)."""
from __future__ import annotations

import dataclasses
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from host import kill
from host.api.app import MAX_BODY_BYTES, create_app
from host.trading.positions import owner_tz
from tests.conftest import (
    GAME_ID, approve, approved_order, assignment_row, auth_state, backtest_metrics, enable_live,
    flash_cookie, heartbeat_body, ingest_fixture, insert_game, insert_market, insert_model, insert_paper_bet,
    insert_snapshot, insert_validated_model, insert_worker, job_row, lease_job, make_assignment, model_row, order_row,
    set_heartbeat_age, set_setting, stress_metrics, trade_setup, validation_metrics, worker_row,
)
from tests.pagecheck import Node, fleet_html, mode_pill, page, topbar


def _value(p: Node, name: str) -> str | None:
    """The value of the one input named name (a settings field, a form field)."""
    return p.input(name).attr("value")


def test_every_page_renders(client, make_worker):
    w = make_worker("box1")
    job = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 3}}).json()
    for path, needle in [
        ("/", '[data-page="home"] [data-card="attention"]'),
        ("/fleet", f'[data-page="fleet"] [data-row="worker"][data-id="{w.id}"]'),
        ("/jobs", '[data-form="backtest"]'),
        (f"/jobs/{job['id']}", '[data-list="events"]'),
        ("/settings", '[data-form="trading"]'),
        ("/kill/confirm", 'form[data-action="kill"]'),
        ("/trading", "#trading-live"),
    ]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("text/html")
        html = r.text
        p = page(html)
        assert p.has(needle), path
        assert p.page_name, path
        assert "<!doctype html>" in html.lower()
        assert "/static/style.css" in p.hrefs and p.has('script[src="/static/app.js"]')
        bar = topbar(p)
        assert p.has("#topbar-status") and mode_pill(p) == "PAPER" and "today" in bar.text and "$0.00" in bar.text
        assert p.has("#kill-form") and p.has("#updated")
        navs = {n.attr("data-nav"): n for n in p.select("[data-nav]")}
        assert {"fleet", "jobs", "models", "trading", "settings"} <= set(navs), path
        assert navs["models"].text == "Models" and navs["trading"].target == "/trading" and navs["settings"].target == "/settings"
        assert "step 4" not in html
    assert page(fleet_html(client)).has(f'[data-row="worker"][data-id="{w.id}"]')


def test_fleet_cards(client, conn, make_worker):
    online = make_worker("alpha", role="backtest")
    stale = make_worker("bravo")
    set_heartbeat_age(conn, stale.id, 30)
    offline = make_worker("charlie", online=False, enabled=False)
    switching = make_worker("delta")
    client.post(f"/api/workers/{switching.id}/role", json={"role": "train"})
    job = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 10}}).json()
    client.post(f"/api/v1/workers/{online.id}/heartbeat", json=heartbeat_body("backtest"), headers=online.headers)
    client.post(f"/api/v1/jobs/{job['id']}/checkpoint", headers=online.headers,
                json={"lease_token": str(job_row(conn, job["id"])["lease_token"]), "progress": 0.4, "checkpoint": {"elapsed": 4}})
    p = page(fleet_html(client))
    assert p.row_ids("worker") == [online.id, stale.id, offline.id, switching.id], "one card per worker, sorted by name"
    rows = {w.id: p.row("worker", w.id) for w in (online, stale, offline, switching)}
    # MEDIUM: status in text too, not only dot colour; the dot is announced
    assert rows[online.id].one(".dot").attr("aria-label") == "online" and rows[online.id].one(".dot").attr("role") == "img"
    assert rows[stale.id].one(".dot").attr("aria-label") == "stale" and rows[stale.id].chip("stale").text == "stale"
    assert rows[offline.id].one(".dot").attr("aria-label") == "offline" and rows[offline.id].chip("offline").text == "offline"
    assert rows[offline.id].chip("disabled").text == "disabled"
    assert not rows[online.id].has('[data-chip="online"]') and not p.has('[data-chip="online"]')
    assert rows[online.id].one('select[name="role"]').target == f"/workers/{online.id}/role"
    assert rows[online.id].one('select[name="role"]').attr("data-autosubmit") == "1" and not rows[online.id].one('select[name="role"]').disabled
    assert rows[online.id].one('option[selected]').attr("value") == "backtest"
    assert rows[switching.id].one('select[name="role"]').disabled and "switching to train (epoch 2)" in rows[switching.id].text
    # MEDIUM: an offline worker's select stays enabled while it is "switching" so a wrong pick can be undone
    gone = make_worker("echo", online=False)
    client.post(f"/api/workers/{gone.id}/role", json={"role": "trade"})
    card = page(fleet_html(client)).row("worker", gone.id)
    assert "switching to trade (epoch 2)" in card.text and not card.select("[disabled]")
    assert not card.one('select[name="role"]').disabled
    bar = rows[online.id].one('[role="progressbar"]')
    assert (bar.attr("aria-valuemin"), bar.attr("aria-valuemax"), bar.attr("aria-valuenow")) == ("0", "100", "40")
    assert "width: 40%" in (bar.first("[style]").attr("style") or "") and "40%" in rows[online.id].text
    assert rows[online.id].one("a.row-main").target == f"/jobs/{job['id']}" and rows[online.id].one(f'[data-job="{job["id"]}"]').text == "sleep 40%"
    assert not rows[stale.id].has("a.row-main"), "no job: nothing to open"
    assert "no job" in rows[stale.id].text
    assert "CPU 2% · RAM 0.5 / 4.0 GB · 0 s ago · v test" in rows[online.id].text
    assert rows[online.id].action("disable").text == "Disable" and rows[online.id].action("disable").one('input[name="enabled"]').attr("value") == "false"
    assert rows[offline.id].action("enable").text == "Enable" and rows[offline.id].action("enable").one('input[name="enabled"]').attr("value") == "true"
    assert "today $0.00" in rows[online.id].text
    # Step 7: one row per worker; Disable and the Details disclosure (the stats line) sit in the "..." menu.
    menu = rows[online.id].one("details.menu")
    details = menu.one('details[data-action="details"]')
    assert not menu.is_open and not details.is_open and details.attr("data-key") == f"fleet-worker-{online.id}"
    assert details.one("summary").text == "Details" and "CPU 2% · RAM 0.5 / 4.0 GB" in details.text
    assert menu.has('[data-action="disable"]') and rows[online.id].card(f"worker-{online.id}") is details
    assert rows[online.id].one(".jobrow").one('[role="progressbar"]') and not rows[stale.id].has(".jobrow")
    assert rows[online.id].one(".row-meta").text == "job: sleep 40%" and rows[stale.id].one(".row-meta").text == "stale · no job"
    assert rows[offline.id].one(".row-meta").text == "offline 1 h ago · disabled · no job", "the state in words, not only the dot"
    assert p.one("#fleet-grid").has(".stats") and p.stat("online").one(".stat-value").text == "2 / 4"
    assert p.stat("working").one(".stat-value").text == "1" and p.stat("switching").one(".stat-value").text == "1"
    assert p.nav("fleet").target == "/fleet" and p.nav("fleet").is_current and p.page_name == "fleet"
    conn.execute("DELETE FROM jobs")
    conn.execute("DELETE FROM workers")
    assert "No workers yet. Mint an enroll token" in page(fleet_html(client)).text


def test_role_form_flips_role_and_redirects_with_flash(client, conn, make_worker):
    w = make_worker("box1")
    r = client.post(f"/workers/{w.id}/role", data={"role": "backtest"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/fleet" and flash_cookie(r) == "box1: switching to backtest (epoch 2)"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    row = worker_row(conn, w.id)
    assert row["desired_role"] == "backtest" and row["role_epoch"] == 2 and row["auto_role"] is False
    flash = page(client.get(r.headers["location"]).text).one("[data-flash]")
    assert flash.text == "box1: switching to backtest (epoch 2)" and flash.attr("role") == "status"
    assert page(client.get("/").text).flash is None, "a flash is shown once, not on every reload"
    r = client.post(f"/workers/{w.id}/role", data={"role": "chef"}, follow_redirects=False)
    assert r.status_code == 400 and "text/html" in r.headers["content-type"] and "unknown role" in r.text
    assert worker_row(conn, w.id)["role_epoch"] == 2
    assert client.post("/workers/w_nope/role", data={"role": "idle"}, follow_redirects=False).status_code == 404
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'set_role'").fetchone()["n"] == 1


def test_enabled_form(client, conn, make_worker):
    w = make_worker("box1")
    r = client.post(f"/workers/{w.id}/enabled", data={"enabled": "false"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/fleet" and flash_cookie(r) == "box1 disabled"
    assert worker_row(conn, w.id)["enabled"] is False
    r = client.post(f"/workers/{w.id}/enabled", data={"enabled": "true"}, follow_redirects=False)
    assert r.headers["location"] == "/fleet" and flash_cookie(r) == "box1 enabled"
    assert worker_row(conn, w.id)["enabled"] is True


def test_send_job_form_and_cancel(client, conn, make_worker):
    w = make_worker("box1")
    p = page(client.get("/jobs").text)
    targets = p.form("sleep").one('select[name="target"]')
    assert targets.one('option[value="any_idle"]').text == "Any idle worker" and targets.one(f'option[value="{w.id}"]').text == "box1"
    assert all(p.has(f'[data-form="{kind}"]') for kind in ("backtest", "model_search", "train", "validate", "sleep"))
    assert p.form("sleep").one('input[name="seconds"]').attr("value") == "60"
    r = client.post("/jobs", data={"kind": "sleep", "seconds": "7", "target": "any_idle"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs" and flash_cookie(r).startswith("sleep job ")
    assert f"sent to {w.id}" in flash_cookie(r)
    first = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert first["params"] == {"seconds": 7} and first["target_worker_id"] == w.id and first["target_auto"] is True
    assert worker_row(conn, w.id)["desired_role"] == "backtest"
    r = client.post("/jobs", data={"kind": "sleep", "seconds": "", "target": w.id}, follow_redirects=False)
    assert r.status_code == 303 and f"sent to {w.id}" in flash_cookie(r)
    chosen = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert chosen["params"] == {"seconds": 60} and chosen["target_auto"] is False
    r = client.post("/jobs", data={"kind": "sleep", "target": "any_idle"}, follow_redirects=False)
    assert "waiting for an idle worker" in flash_cookie(r)
    waiting = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    p = page(client.get("/jobs").text)
    assert len(p.rows("job")) == 3 and "waiting for an idle worker" in p.row("job", waiting["id"]).text
    assert p.row("job", waiting["id"]).action("cancel").target == f"/jobs/{waiting['id']}/cancel"
    assert p.row_ids("job").index(str(waiting["id"])) < p.row_ids("job").index(str(first["id"])), "newest first"
    assert client.post("/jobs", data={"kind": "sleep", "seconds": "x"}, follow_redirects=False).status_code == 400
    assert client.post("/jobs", data={"kind": "mystery"}, follow_redirects=False).status_code == 400
    assert client.post("/jobs", data={"kind": "sleep", "target": "w_nope"}, follow_redirects=False).status_code == 404
    r = client.post(f"/jobs/{waiting['id']}/cancel", data={"next": "/jobs/" + str(waiting["id"])}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{waiting['id']}" and flash_cookie(r).startswith("job ")
    assert job_row(conn, waiting["id"])["status"] == "cancelled"
    r = client.post(f"/jobs/{first['id']}/cancel", data={"next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/jobs"
    detail = page(client.get(f"/jobs/{first['id']}").text)
    assert detail.chip("cancelled").text == "cancelled" and '"seconds": 7' in detail.text and detail.prop("created")
    assert client.get("/jobs/not-a-job").status_code == 404
    assert "<html" in client.get("/jobs/not-a-job").text


def test_settings_form_converts_rejects_and_audits(client, conn):
    p = page(client.get("/settings").text)
    assert _value(p, "max_bet") == "25.00" and _value(p, "max_daily_loss_paper") == "1000.00"
    assert _value(p, "lease_seconds") == "30" and _value(p, "tz") == "America/New_York"
    assert _value(p, "max_expiries") == "3"
    good = {
        "max_bet": "12.50", "max_daily_loss_paper": "$1,500", "max_daily_loss_live": "300.005",
        "default_bankroll": "100", "liquidity_floor": "500.00", "min_edge": "0.05", "kelly_fraction": "0.25",
        "trade_max_games": "4",
    }
    r = client.post("/settings/trading", data=good, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings" and flash_cookie(r) == "trading settings saved"
    s = client.get("/api/settings").json()
    assert s["max_bet_cents"] == 1250 and s["max_daily_loss_cents"] == {"live": 30001, "paper": 150000}
    assert s["default_bankroll_cents"] == 10000 and s["liquidity_floor_cents"] == 50000
    assert s["min_edge"] == 0.05 and s["trade_max_games"] == 4 and s["kelly_fraction"] == 0.25
    audited = conn.execute(
        "SELECT entity, before, after FROM audit_log WHERE action = 'settings_changed' ORDER BY id"
    ).fetchall()
    assert [a["entity"] for a in audited] == ["max_bet_cents", "max_daily_loss_cents", "min_edge", "trade_max_games"]
    assert audited[0]["before"] == {"max_bet_cents": 2500} and audited[0]["after"] == {"max_bet_cents": 1250}
    p = page(client.get("/settings").text)
    assert _value(p, "max_bet") == "12.50" and _value(p, "max_daily_loss_live") == "300.01"
    for bad, message in [
        ({**good, "max_bet": "lots"}, "Max bet must be a dollar amount"),
        ({**good, "max_bet": "-1"}, "max_bet_cents must be between 0 and 100000000000"),
        ({**good, "min_edge": "2"}, "min_edge must be between 0 and 1"),
        ({**good, "trade_max_games": "1.5"}, "must be a whole number"),
    ]:
        r = client.post("/settings/trading", data=bad, follow_redirects=False)
        assert r.status_code == 400, bad
        p = page(r.text)
        assert any(message in e for e in p.card("trading").texts(".error")), (bad, message)
        assert _value(p, "max_bet") == bad["max_bet"], "submitted values are kept"
    assert client.get("/api/settings").json()["max_bet_cents"] == 1250
    r = client.post("/settings/fleet", data={"lease_seconds": "45", "heartbeat_seconds": "5",
                                              "online_after_seconds": "20", "max_expiries": ""}, follow_redirects=False)
    assert r.status_code == 303
    s = client.get("/api/settings").json()
    assert s["lease_seconds"] == 45 and s["online_after_seconds"] == 20 and s["max_expiries"] is None
    r = client.post("/settings/fleet", data={"lease_seconds": "5", "heartbeat_seconds": "5",
                                              "online_after_seconds": "20", "max_expiries": ""}, follow_redirects=False)
    assert r.status_code == 400 and "lease_seconds must be between 10 and 3600" in r.text
    r = client.post("/settings/tz", data={"tz": "Europe/Berlin"}, follow_redirects=False)
    assert r.status_code == 303 and client.get("/api/settings").json()["tz"] == "Europe/Berlin"
    r = client.post("/settings/tz", data={"tz": "../etc"}, follow_redirects=False)
    assert r.status_code == 400 and "IANA" in r.text
    assert client.post("/settings/nope", data={}, follow_redirects=False).status_code == 400
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'settings_changed'").fetchone()["n"] == 8


def test_enroll_token_form_shows_token_once(client, conn):
    r = client.post("/enroll-token", follow_redirects=False)
    assert r.status_code == 200
    token = page(r.text).one("#token").text
    assert len(token) > 30
    assert f"curl -fsSL http://127.0.0.1:8080/install.sh | sudo bash -s -- http://127.0.0.1:8080 {token}" in r.text
    assert f"curl -fsSL http://127.0.0.1:8080/install.sh | sudo FLEET_ENROLL_TOKEN={token} bash -s -- http://127.0.0.1:8080" in r.text
    assert page(r.text).has('[data-copy="token"]')
    assert conn.execute("SELECT count(*) AS n FROM enroll_tokens").fetchone()["n"] == 1
    assert token not in client.get("/settings").text
    reg = client.post("/api/v1/workers/register", json={"enroll_token": token, "hostname": "box9"})
    assert reg.status_code == 200


def test_fragments_return_inner_html_only(client, make_worker):
    w = make_worker("box1")
    fleet = page(client.get("/fragments/fleet").text)
    assert not fleet.has("html") and fleet.has(f'[data-row="worker"][data-id="{w.id}"]') and not fleet.has("#fleet-grid")
    bar = page(client.get("/fragments/topbar").text)
    assert not bar.has("html") and mode_pill(bar) == "PAPER" and bar.has('[data-kill="1"]')
    assert not bar.has("#topbar-status")


def test_names_and_params_are_escaped(client, conn, make_worker):
    w = make_worker("evil<script>alert(1)</script>")
    job = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 1, "note": "<script>x</script>"},
                                         "target": w.id}).json()
    for path in ("/", "/fragments/fleet", "/jobs", f"/jobs/{job['id']}", "/settings"):
        html = client.get(path).text
        assert "<script>" not in html and all(s.has_attr("src") for s in page(html).select("script")), path
    assert "evil&lt;script&gt;alert(1)&lt;/script&gt;" in fleet_html(client)
    assert "&lt;script&gt;x&lt;/script&gt;" in client.get(f"/jobs/{job['id']}").text
    # The flash travels in a cookie set by the redirect: a crafted link cannot inject one.
    crafted = client.get("/?flash=<img src=x onerror=alert(1)>").text
    assert "<img" not in crafted and page(crafted).flash is None
    r = client.post(f"/workers/{w.id}/role", data={"role": "backtest"}, follow_redirects=False)
    shown = client.get(r.headers["location"]).text
    assert page(shown).flash.startswith("evil<script>alert(1)</script>: switching to backtest")
    assert "evil&lt;script&gt;alert(1)&lt;/script&gt;: switching to backtest" in shown, "escaped in the source"


def test_auth_and_csrf_render_html(config, client):
    strict = dataclasses.replace(config, dev=False, owner_login="owner@example.com")
    with TestClient(create_app(strict)) as c:
        r = c.get("/")
        assert r.status_code == 401 and r.headers["content-type"].startswith("text/html")
        assert "401" in r.text and "owner login required" in r.text and "tailscale" in r.text.lower()
        r = c.get("/", headers={"Tailscale-User-Login": "intruder@example.com"})
        assert r.status_code == 401
        assert c.get("/jobs").status_code == 401 and c.get("/fragments/fleet").status_code == 401
        assert c.post("/kill", headers={"Tailscale-User-Login": "intruder@example.com"}).status_code == 401
        assert c.get("/", headers={"Tailscale-User-Login": "owner@example.com"}).status_code == 200
        r = c.get("/static/style.css")
        assert r.status_code == 200 and "text/css" in r.headers["content-type"] and "prefers-color-scheme" in r.text
        assert c.get("/static/app.js").status_code == 200
        assert c.get("/api/fleet").status_code == 401 and c.get("/api/fleet").headers["content-type"].startswith("application/json")
    r = client.post("/kill", headers={"Origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code == 403 and r.headers["content-type"].startswith("text/html") and "origin not allowed" in r.text
    assert client.get("/api/settings").json()["kill_switch"] is False
    r = client.post("/kill", headers={"Origin": "http://127.0.0.1:8080"}, follow_redirects=False)
    assert r.status_code == 303


def test_timestamps_follow_the_owner_time_zone(client, conn):
    """MEDIUM: Jobs, job detail, the audit log and the enroll page show settings.tz."""
    job = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 3}}).json()
    created = job_row(conn, job["id"])["created_at"]
    html = client.get("/jobs").text
    assert created.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z") in html, "the default tz"
    client.post("/api/settings", json={"tz": "Asia/Tokyo"})
    expected = created.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S JST")
    assert expected in client.get("/jobs").text and expected in client.get(f"/jobs/{job['id']}").text
    audit = page(client.get("/settings").text).rows("audit")
    assert audit and all(re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d JST", row.text) for row in audit)
    r = client.post("/enroll-token")
    assert re.search(r"expires \d{4}-\d\d-\d\d \d\d:\d\d:\d\d JST\.", r.text)
    conn.execute("""UPDATE settings SET value = '"Mars/Olympus"' WHERE key = 'tz'""")
    assert created.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") in client.get("/jobs").text, "unknown zone: UTC"


def test_settings_inputs_open_the_number_keyboard(client):
    """MEDIUM: every numeric field carries an inputmode; the tz field does not."""
    p = page(client.get("/settings").text)
    decimal = ("max_bet", "max_daily_loss_paper", "max_daily_loss_live", "default_bankroll", "liquidity_floor", "min_edge", "kelly_fraction",
               "taker_rate", "half_spread", "min_roi", "max_drawdown", "min_roi_ci_low", "max_market_p", "participation",
               "max_exposure_paper", "max_exposure_live", "paper_min_clv", "paper_min_pnl", "orders_per_s", "cancels_per_s",
               "market_data_per_s", "account_per_s")
    numeric = ("trade_max_games", "lease_seconds", "heartbeat_seconds", "online_after_seconds", "max_expiries",
               "min_bets", "seasons_first", "seasons_last", "validation_first", "validation_last", "nflverse_refresh_hours",
               "book_max_age_s", "gtd_seconds", "orphan_cancel_after_s", "trade_tick_s", "max_paper_models_per_game",
               "market_lookahead_days", "snapshot_active_s", "snapshot_idle_s", "snapshot_retention_days", "paper_min_games",
               "paper_min_bets", "paper_min_days", "decision_minutes_before_kickoff", "signals_refresh_hours")
    def field_input(name):
        box = p.field(name).one(f'input[name="{name}"]')
        assert box.attr("type") == "text", name
        return box

    for name in decimal:
        assert field_input(name).attr("inputmode") == "decimal", name
    for name in numeric:
        assert field_input(name).attr("inputmode") == "numeric", name
    assert field_input("tz").attr("inputmode") is None and field_input("tz").attr("autocomplete") == "off"
    assert field_input("search_workers").attr("value") == "auto" and field_input("search_workers").attr("inputmode") is None, "auto or a number: no number keyboard"
    for name in ("nflverse_url", "scores_url"):
        assert field_input(name).attr("value").startswith("https://") and field_input(name).attr("inputmode") is None, name
    assert p.count("[inputmode]") == len(decimal) + len(numeric)


def test_fleet_timing_is_checked_across_fields(client):
    """MEDIUM: a lease shorter than two heartbeats (plus the HTTP timeout) or an
    online window shorter than a heartbeat is refused by the form and the API."""
    data = {"lease_seconds": "30", "heartbeat_seconds": "60", "online_after_seconds": "15", "max_expiries": "3"}
    r = client.post("/settings/fleet", data=data, follow_redirects=False)
    assert r.status_code == 400
    assert "lease_seconds must be at least 125 (2 x heartbeat_seconds + 5)" in r.text
    assert "online_after_seconds must be greater than heartbeat_seconds (60)" in r.text
    assert _value(page(r.text), "heartbeat_seconds") == "60", "submitted values are kept"
    s = client.get("/api/settings").json()
    assert (s["lease_seconds"], s["heartbeat_seconds"], s["online_after_seconds"]) == (30, 5, 15), "nothing stored"
    r = client.post("/settings/fleet", data={**data, "lease_seconds": "125", "online_after_seconds": "61"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.post("/api/settings", json={"lease_seconds": 100}).status_code == 400, "judged against stored values"
    assert client.post("/api/settings", json={"lease_seconds": 125}).status_code == 200


def test_money_fields_reject_decimal_commas_and_huge_amounts(client):
    """LOW: '1,5' must not become $15.00, and a huge amount is an inline 400, not a 500."""
    good = {
        "max_bet": "12.50", "max_daily_loss_paper": "$1,500.25", "max_daily_loss_live": "300", "default_bankroll": "100",
        "liquidity_floor": "500.00", "min_edge": "0.05", "kelly_fraction": "0.25", "trade_max_games": "4",
    }
    r = client.post("/settings/trading", data=good, follow_redirects=False)
    assert r.status_code == 303 and client.get("/api/settings").json()["max_daily_loss_cents"]["paper"] == 150025
    r = client.post("/settings/trading", data={**good, "max_bet": "1,5"}, follow_redirects=False)
    assert r.status_code == 400 and "Max bet: use a dot for cents, such as 1.50" in r.text
    for value in ("1e30", "1e999999999", "1e25", "1000000000.01", "12,34,567"):
        r = client.post("/settings/trading", data={**good, "max_bet": value}, follow_redirects=False)
        assert r.status_code == 400 and r.headers["content-type"].startswith("text/html"), value
        assert "Max bet" in page(r.text).card("trading").text and _value(page(r.text), "max_bet") == value, value
    assert client.get("/api/settings").json()["max_bet_cents"] == 1250
    assert client.post("/api/settings", json={"max_bet_cents": 10**25}).status_code == 400


def test_dashboard_responses_refuse_framing_and_caching(client):
    """MEDIUM: anti-framing headers on every non-API response (the owner is
    authenticated by the network, so a framed form would pass the Origin check).
    LOW: pages, the enroll token page in particular, are never cached."""
    for path in ("/", "/jobs", "/settings", "/kill/confirm", "/fragments/fleet", "/trading", "/fragments/trading", "/static/style.css", "/nope"):
        r = client.get(path)
        assert r.headers["x-frame-options"] == "DENY", path
        assert r.headers["content-security-policy"] == "frame-ancestors 'none'", path
        assert r.headers["referrer-policy"] == "same-origin", path
    for path in ("/", "/jobs", "/settings", "/nope"):
        assert client.get(path).headers["cache-control"] == "no-store", path
    r = client.post("/enroll-token")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
    r = client.post("/kill", follow_redirects=False)
    assert r.status_code == 303 and r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
    assert "x-frame-options" not in client.get("/api/fleet").headers


def test_unknown_paths_render_html_on_the_dashboard_and_json_under_api(client):
    """LOW: Starlette's own 404 and 405 go through the HTML error page outside /api."""
    for path in ("/nope", "/workers", "/jobs/x/y", "/static/", "/static/missing.css", "/static/../web.py"):
        r = client.get(path)
        assert r.status_code == 404 and r.headers["content-type"].startswith("text/html"), path
        assert "404 Not found" in r.text and "/" in page(r.text).hrefs, path
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    r = client.post("/settings", data={})
    assert r.status_code == 405 and r.headers["content-type"].startswith("text/html")


def test_chunked_body_over_the_limit_is_refused(client):
    """LOW: a body without Content-Length is counted as it arrives; the 413 is HTML
    on dashboard paths and JSON under /api."""
    client.post("/kill")
    form = {"Content-Type": "application/x-www-form-urlencoded"}

    def chunks():
        for _ in range(2000):
            yield b"confirm=" + b"x" * 1000 + b"&"

    r = client.post("/kill/reset", content=chunks(), headers=form)
    assert r.status_code == 413 and r.headers["content-type"].startswith("text/html") and "413 Too large" in r.text
    r = client.post("/kill/reset", content=b"confirm=" + b"x" * (MAX_BODY_BYTES + 1), headers=form)
    assert r.status_code == 413 and r.headers["content-type"].startswith("text/html")
    r = client.post("/api/kill/reset", content=b"x" * (MAX_BODY_BYTES + 1), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == {"detail": "request body too large"}
    assert client.get("/api/settings").json()["kill_switch"] is True


# ------------------------------------------------------------------ step 3: models and job forms


def _has_card(p, name: str) -> bool:
    """A section (data-card) or a disclosure (data-key ending in -name) is on the page."""
    return p.has(f'[data-card="{name}"]') or p.has(f'details[data-key$="-{name}"]')


def test_models_page_renders_ranked_rows_and_attribution(client, conn):
    p = page(client.get("/models").text)
    assert "No models yet. Send a model search" in p.text and "CC BY 4.0" in p.text and "nflverse" in p.text
    assert not _has_card(p, "unranked") and not _has_card(p, "ranked") and not p.rows("model")
    only_unranked = insert_model(conn, params={"k": 19.0}, metrics=backtest_metrics(n_bets=20, roi=0.5))
    p = page(client.get("/models").text)
    assert "No lineage is validated yet, so none is ranked." in p.text and "No models yet" not in p.text
    assert not _has_card(p, "ranked") and _has_card(p, "unranked"), "no empty ranked header above the unranked list"
    chip = p.row("model", only_unranked["id"]).chip("unvalidated")
    assert chip.text == "not validated" and chip.closest("[title]").attr("title") == "no validation-era metrics yet: send a validate job"
    conn.execute("DELETE FROM models WHERE id = %s", (only_unranked["id"],))
    best = insert_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, metrics=backtest_metrics(n_bets=400, roi=0.05, log_loss=0.65, market_log_loss=0.659, max_drawdown=0.14, seasons=[2016, 2017, 2018, 2019]),
                        validation=validation_metrics(n_bets=130, roi=0.041, ci_roi=(-0.012, 0.094), market_p=0.012, log_loss=0.651, market_log_loss=0.658, max_drawdown=0.09),
                        stress=stress_metrics(), status="paper_ok", summary="Best lineage.")
    insert_model(conn, parent=best, trained_through=[2024, 10])
    second = insert_model(conn, params={"k": 30.0, "hfa": 60.0, "mov_scale": 0}, metrics=backtest_metrics(n_bets=120, roi=0.03),
                          validation=validation_metrics(n_bets=80, roi=0.02, market_p=0.31, flags=["overfit"]), stress=stress_metrics(flags=["fragile", "regime_dependent"]))
    few = insert_model(conn, params={"k": 31.0}, metrics=backtest_metrics(n_bets=20, roi=0.5))
    none = insert_model(conn, params={"k": 32.0}, summary="<b>bold</b>")
    html = client.get("/models").text
    p = page(html)
    assert p.row_ids("model") == [str(best["id"]), str(second["id"]), str(none["id"]), str(few["id"])], "ranked first, then unranked newest first"
    ranked, unranked = p.card("ranked"), p.card("unranked")
    assert ranked.row_ids("model") == [str(best["id"]), str(second["id"])] and unranked.row_ids("model") == [str(none["id"]), str(few["id"])]
    assert "#1" in ranked.row("model", best["id"]).text and "#2" in ranked.row("model", second["id"]).text
    assert ranked.row("model", best["id"]).chip("paper_ok").text == "paper ok" and p.row("model", few["id"]).chip("candidate").text == "candidate"
    assert "K 20 · HFA 50 · MOV on" in ranked.text and "K 30 · HFA 60 · MOV off" in ranked.text
    first = ranked.row("model", best["id"])
    assert first.one(".row-value").text == "ROI +4.1%", "ranked on the validation era: its ROI is the headline"
    assert first.one(".row-meta").text.startswith("paper ok 130 held-out bets · range -1.2% to +9.4% · p = 0.012")
    assert first.chip("beats").text == "beats market"
    assert not {"overfit", "fragile", "regime_dependent"} & set(first.chips())
    assert "Best lineage." not in first.text and first.chip("members").text == "2 rows" and "unvalidated" not in first.chips()
    row2 = ranked.row("model", second["id"])
    assert {"overfit", "fragile", "regime_dependent"} <= set(row2.chips())
    assert row2.chip("overfit").text == "overfit" and row2.chip("fragile").text == "fragile" and row2.chip("regime_dependent").text == "regime-dependent"
    assert "p = 0.310" in row2.text and "beats" not in row2.chips()
    assert unranked.count('[data-chip="unvalidated"]') == 2
    few_row = unranked.row("model", few["id"])
    assert few_row.one(".row-value").text == "ROI +50.0%" and few_row.one(".row-meta").text == "candidate not validated · search era 20 bets"
    assert "not validated, or retired" in unranked.text and not unranked.is_open, "the unranked list starts folded"
    assert first.action("train").target == f"/jobs?train_model={best['id']}#train" and first.action("train").text == "Train"
    assert first.action("validate").target == f"/jobs?validate_model={best['id']}#validate" and first.action("validate").text == "Validate"
    assert first.action("assign").target == f"/trading?model={best['id']}#assign" and first.action("assign").text == "Assign"
    retire = first.action("retire")
    assert retire.target == f"/models/{best['id']}/retire" and retire.one('input[name="next"]').attr("value") == "/models"
    assert retire.attr("data-confirm").startswith("Retire this whole lineage?")
    assert not p.has('[data-form="summary"]') and "Best lineage." not in p.text, "the summary lives on the detail page"
    detail_html = client.get(f"/models/{none['id']}").text
    assert "&lt;b&gt;bold&lt;/b&gt;" in detail_html and "<b>bold</b>" not in detail_html and "<b>bold</b>" in page(detail_html).text
    assert "No summary yet." in page(client.get(f"/models/{few['id']}").text).text
    assert p.has(".attribution") and "https://creativecommons.org/licenses/by/4.0/" in p.hrefs
    assert p.nav("models").is_current and not p.nav("trading").is_current and "step 3" not in html


def test_model_detail_page(client, conn, make_worker):
    root = insert_validated_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, metrics=backtest_metrics(n_bets=400, roi=0.05, per_season=[
        {"season": 2016, "n_games": 200, "n_bets": 50, "roi": 0.02, "pnl_cents": 1200, "log_loss": 0.66, "market_log_loss": 0.655, "max_drawdown": 0.05},
        {"season": 2017, "n_games": 210, "n_bets": 60, "roi": -0.01, "pnl_cents": -700, "log_loss": 0.67, "market_log_loss": 0.665, "max_drawdown": 0.08},
    ]), status="paper_ok", summary="Root summary.")
    w = make_worker("box1", role="train")
    job = lease_job(conn, w, "train", {"model_id": str(root["id"]), "through": {"season": 2024, "week": 10}})
    child = insert_model(conn, parent=root, trained_through=[2024, 10])
    conn.execute("UPDATE models SET created_by_job_id = %s WHERE id = %s", (job["id"], child["id"]))
    r = client.post("/api/jobs", json={"kind": "backtest", "params": {"model_id": str(root["id"])}})
    assert r.status_code == 201, r.text
    bt = r.json()
    p = page(client.get(f"/models/{root['id']}").text)
    assert p.page_name == "model" and p.one("h1").text.startswith("elo_blend") and "K 20 · HFA 50 · MOV on" in p.one("h1").text
    assert p.card("model").one(".verdict").chip("paper_ok").text == "paper ok"
    assert str(root["lineage_id"]) in p.text and p.card("model").has('[data-chip="root"]') and "Root summary." in p.text
    assert '"hfa": 50.0' in p.text, "params are shown as JSON"
    backtest = p.card("backtest")
    assert backtest.prop("bets") == "400" and backtest.prop("ROI").startswith("+5.0%") and backtest.prop("seasons") == "2016-2018"
    assert backtest.row("season", 2016) and "-1.0%" in backtest.row("season", 2017).text and "-$7.00" in backtest.row("season", 2017).text
    assert "0.0-0.1" in backtest.listing("calibration").text and "0.9-1.0" in backtest.listing("calibration").text
    lineage = p.card("lineage")
    assert f"/models/{child['id']}" in lineage.hrefs and "2024 week 10" in lineage.row("lineage", child["id"]).text
    assert f"/jobs/{job['id']}" in lineage.hrefs
    jobs = p.card("jobs")
    assert f"/jobs/{bt['id']}" in jobs.hrefs and "ran against it" in jobs.row("job", bt["id"]).text and "created this model" not in jobs.text
    retire = p.action("retire")
    assert retire.target == f"/models/{root['id']}/retire" and retire.attr("data-confirm").startswith("Retire this whole lineage?")
    assert p.action("train").target == f"/jobs?train_model={root['id']}#train" and p.form("summary").target == f"/models/{root['id']}/summary"
    validate = p.action("validate")
    assert validate.target == "/jobs" and validate.attr("method") == "post" and validate.text == "Validate"
    assert validate.one('input[name="model_id"]').attr("value") == str(root["id"]) and validate.one('input[name="kind"]').attr("value") == "validate"
    assert "validation era 2022-2025, held out of the search" in p.card("robustness").one(".disclosure-summary").text
    assert p.prop("validation shrunk ROI").startswith("+2.18%") and p.prop("search shrunk ROI").startswith("+4.00%")
    assert backtest.one(".disclosure-summary").text.startswith("search era 2016-2018")
    child_p = page(client.get(f"/models/{child['id']}").text)
    parent = child_p.card("lineage").row("lineage", root["id"])
    assert parent.first(f'a[href="/models/{root["id"]}"]').one(".row-title").text.startswith(str(root["id"])[:8]) and "(this)" in child_p.card("lineage").text
    assert "No backtest metrics yet" not in child_p.text, "a child shows the lineage metrics"
    assert "created this model" in child_p.card("jobs").text and f"/jobs/{job['id']}" in child_p.card("jobs").hrefs
    empty = insert_model(conn, params={"k": 40.0})
    assert "No backtest metrics yet" in client.get(f"/models/{empty['id']}").text
    assert client.get("/models/not-a-model").status_code == 404
    # The retire form retires the lineage and redirects back with a flash.
    r = client.post(f"/models/{child['id']}/retire", data={"next": f"/models/{child['id']}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/models/{child['id']}" and "retired" in flash_cookie(r)
    assert model_row(conn, root["id"])["status"] == "retired"
    retired = page(client.get(f"/models/{root['id']}").text)
    assert not retired.has('[data-action="retire"]') and retired.card("model").one(".verdict").has('[data-chip="retired"]')


def test_summary_edit_form(client, conn):
    root = insert_model(conn, summary="old")
    r = client.post(f"/models/{root['id']}/summary", data={"summary": "A new summary.", "next": "/models"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/models" and flash_cookie(r) == f"summary of {str(root['id'])[:8]} saved"
    assert model_row(conn, root["id"])["summary"] == "A new summary."
    assert "A new summary." in client.get(f"/models/{root['id']}").text, "the summary shows on the detail page"
    r = client.post(f"/models/{root['id']}/summary", data={"summary": "x" * 601, "next": f"/models/{root['id']}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/models/{root['id']}" and "not saved" in flash_cookie(r) and "600" in flash_cookie(r)
    assert model_row(conn, root["id"])["summary"] == "A new summary."
    r = client.post(f"/models/{root['id']}/summary", data={"summary": "x", "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/models"
    assert client.post("/models/not-a-model/summary", data={"summary": "x"}, follow_redirects=False).status_code == 404
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'model_summary'").fetchone()["n"] == 2


def test_three_job_forms_post_valid_jobs(client, conn, make_worker):
    w = make_worker("box1")
    ingest_fixture(conn)
    model = insert_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1})
    p = page(client.get("/jobs").text)
    forms = [p.form(kind) for kind in ("backtest", "model_search", "train", "validate", "sleep")]
    assert all(f.target == "/jobs" for f in forms) and all(f.one('option[value="any_idle"]').text == "Any idle worker" for f in forms)
    assert _value(p.form("backtest"), "seasons_first") == "2010" and _value(p.form("backtest"), "seasons_last") == "2021"
    assert _value(p.form("model_search"), "n") == "200" and _value(p.form("model_search"), "top_k") == "5"
    assert _value(p.form("train"), "through_season") == "2025"
    assert p.form("train").one(f'option[value="{model["id"]}"]').text == f"K 20 · HFA 50 · MOV on · untrained · {str(model['id'])[:8]}"
    family = p.form("model_search").one('select[name="family"] option[selected]')
    assert family.attr("value") == "elo_blend" and family.text == "elo_blend"
    # Backtest by family + params.
    r = client.post("/jobs", data={"kind": "backtest", "model_id": "", "family": "elo_blend", "params": '{"k": 22}',
                                   "seasons_first": "2018", "seasons_last": "", "target": "any_idle"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs" and flash_cookie(r).startswith("backtest job ")
    job = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert job["kind"] == "backtest" and job["params"]["family"] == "elo_blend" and job["params"]["params"] == {"k": 22}
    assert job["params"]["seasons"] == [2018, 2025] and job["params"]["backtest_seasons"] == [2010, 2021]
    assert job["target_worker_id"] == w.id and worker_row(conn, w.id)["desired_role"] == "backtest"
    # Backtest by model.
    r = client.post("/jobs", data={"kind": "backtest", "model_id": str(model["id"]), "family": "elo_blend", "params": "{}",
                                   "seasons_first": "", "seasons_last": "", "target": w.id}, follow_redirects=False)
    assert r.status_code == 303
    job = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert job["params"]["model_id"] == str(model["id"]) and "family" not in job["params"] and "seasons" not in job["params"]
    # Model search.
    r = client.post("/jobs", data={"kind": "model_search", "family": "elo_blend", "n": "50", "seed": "7", "seasons_first": "2016",
                                   "seasons_last": "2019", "top_k": "3", "target": "any_idle"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("model_search job ")
    job = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert job["kind"] == "model_search" and job["role"] == "model_search"
    assert job["params"]["n"] == 50 and job["params"]["seed"] == 7 and job["params"]["top_k"] == 3 and job["params"]["seasons"] == [2016, 2019]
    # Train, prefilled from the models page link.
    train = page(client.get(f"/jobs?train_model={model['id']}").text).form("train")
    assert train.one('select[name="model_id"] option[selected]').attr("value") == str(model["id"])
    r = client.post("/jobs", data={"kind": "train", "model_id": str(model["id"]), "through_season": "2024", "through_week": "10",
                                   "target": "any_idle"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("train job ")
    job = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert job["kind"] == "train" and job["params"] == {
        "model_id": str(model["id"]), "through": {"season": 2024, "week": 10}, "fee_model": {"taker_rate": 0.05, "half_spread": 0.01},
        "default_bankroll_cents": 10000, "max_bet_cents": 2500, "trade_max_games": 6, "backtest_seasons": [2010, 2021],
    }
    # Validate, prefilled from the models page link.
    p = page(client.get(f"/jobs?validate_model={model['id']}").text)
    assert p.form("validate").closest("details").is_open and _value(p.form("validate"), "validate_seed") == "1"
    assert "held-out validation era (2022-2025)" in p.form("validate").text
    r = client.post("/jobs", data={"kind": "validate", "model_id": str(model["id"]), "validate_seed": "4", "target": "any_idle"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("validate job ")
    job = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert job["kind"] == "validate" and job["role"] == "backtest" and job["params"]["model_id"] == str(model["id"]) and job["params"]["seed"] == 4
    assert job["params"]["validation_seasons"] == [2022, 2025] and job["params"]["workers"] == "auto"
    listing = page(client.get("/jobs").text)
    assert len(listing.rows("job")) == 5 and any("elo_blend n 50" in row.text for row in listing.rows("job"))
    assert f"validate {str(model['id'])[:8]}" in listing.row("job", job["id"]).text


def test_invalid_job_params_rerender_with_an_inline_error(client, conn):
    base = {"kind": "backtest", "model_id": "", "family": "elo_blend", "params": "{}", "seasons_first": "", "seasons_last": "", "target": "any_idle"}
    for data, message in [
        ({**base, "params": "not json"}, "Params must be a JSON object"),
        ({**base, "params": "[1]"}, "Params must be a JSON object"),
        ({**base, "params": '{"k": "x"}'}, "params.k must be a number"),
        ({**base, "seasons_first": "abc"}, "First season must be a whole number"),
        ({**base, "seasons_first": "2020", "seasons_last": "2010"}, "seasons last must not be before first"),
        ({**base, "seasons_last": "2020"}, "First season is required"),
        ({**base, "model_id": "00000000-0000-0000-0000-000000000000"}, "unknown model"),
        ({"kind": "model_search", "family": "elo_blend", "n": "0", "target": "any_idle"}, "n must be between 1 and 5000"),
        ({"kind": "model_search", "family": "elo_blend", "n": "1.5", "target": "any_idle"}, "Candidates must be a whole number"),
        ({"kind": "train", "model_id": "", "through_season": "2024", "through_week": "", "target": "any_idle"}, "Through season and week are required"),
        ({"kind": "train", "model_id": "garbage", "through_season": "2024", "through_week": "3", "target": "any_idle"}, "model_id must be a uuid"),
        ({"kind": "validate", "model_id": "garbage", "validate_seed": "1", "target": "any_idle"}, "model_id must be a uuid"),
        ({"kind": "validate", "model_id": "00000000-0000-0000-0000-000000000000", "validate_seed": "x", "target": "any_idle"}, "Seed must be a whole number"),
        ({"kind": "sleep", "seconds": "0", "target": "any_idle"}, "seconds must be between 1 and 86400"),
    ]:
        r = client.post("/jobs", data=data, follow_redirects=False)
        assert r.status_code == 400 and r.headers["content-type"].startswith("text/html"), data
        p = page(r.text)
        posted = p.form(data["kind"])
        assert any(e.startswith(message) for e in posted.texts(".inline-error")), (data, message)
        assert p.page_name == "jobs" and not p.has('[data-list="workers"]'), "the jobs page is re-rendered"
        assert p.count(".inline-error") == 1, "the error sits in the posted form only"
        assert posted.closest("details") is None or posted.closest("details").is_open, "the posted form is open"
        if data["kind"] == "backtest":
            assert posted.one('textarea[name="params"]').text == data["params"], "submitted values are kept"
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0, "nothing was created"


def test_job_detail_renders_result_tables_and_model_links(client, conn, make_worker):
    w = make_worker("box1", role="backtest")
    model = insert_model(conn)
    metrics = backtest_metrics(n_bets=300, roi=0.04, per_season=[
        {"season": 2016, "n_games": 100, "n_bets": 30, "roi": 0.1, "pnl_cents": 3000, "log_loss": 0.66, "market_log_loss": 0.66, "max_drawdown": 0.03},
    ])
    bt = client.post("/api/jobs", json={"kind": "backtest", "params": {"model_id": str(model["id"])}, "target": w.id}).json()
    conn.execute("UPDATE jobs SET status = 'succeeded', result = %s, progress = 1 WHERE id = %s", (__import__("psycopg").types.json.Jsonb(metrics), bt["id"]))
    p = page(client.get(f"/jobs/{bt['id']}").text)
    assert f"/models/{model['id']}" in p.hrefs and p.prop("bets") == "300" and p.prop("ROI") == "+4.0%"
    assert "+10.0%" in p.row("season", 2016).text and '"n_bets": 300' in p.card("raw").text and not p.card("raw").is_open
    assert [p.card(name).one(".disclosure-title").text for name in ("params", "checkpoints", "log", "raw")] == [
        "Parameters", "Checkpoints", "Log", "Raw"], "the job page groups its detail into four disclosures"
    assert p.stat("progress").chip("succeeded").text == "succeeded" and p.prop("Progress") == "100%"
    top = [{"index": 0, "params": {"k": 20.0, "hfa": 50.0, "mov_scale": 1}, "score": 0.03, "metrics": backtest_metrics(n_bets=200, roi=0.045)},
           {"index": 1, "params": {"k": 25.0, "hfa": 40.0, "mov_scale": 0}, "score": 0.01, "metrics": backtest_metrics(n_bets=100, roi=0.02)}]
    created = [{"id": str(model["id"]), "lineage_id": str(model["id"]), "created": False}, {"id": "00000000-0000-0000-0000-0000000000aa", "lineage_id": "x", "created": True}]
    ms = client.post("/api/jobs", json={"kind": "model_search", "params": {"family": "elo_blend", "n": 2}}).json()
    conn.execute("UPDATE jobs SET status = 'succeeded', result = %s WHERE id = %s", (__import__("psycopg").types.json.Jsonb({"evaluated": 2, "seasons": [2016, 2017], "top": top, "created_models": created}), ms["id"]))
    p = page(client.get(f"/jobs/{ms['id']}").text)
    first, second = p.row("candidate", 1), p.row("candidate", 2)
    assert "K 20 · HFA 50 · MOV on" in first.text and "K 25 · HFA 40 · MOV off" in second.text and "+4.5%" in first.text
    assert first.one(f'a[href="/models/{model["id"]}"]').text == str(model["id"])[:8]
    assert second.one('a[href="/models/00000000-0000-0000-0000-0000000000aa"]').text == "00000000", "the created id, shortened"
    created = p.listing("created-models").select(f'a[href="/models/{model["id"]}"]')
    assert [a.text for a in created] == [f"{str(model['id'])[:8]} (existing)"]
    assert "2 candidates evaluated over 2016-2017" in p.text


def test_phone_layout_rules(client, conn):
    """Tables stack on a phone (no horizontal scroll at 390 px) and tap targets stay 44 px."""
    insert_model(conn, metrics=backtest_metrics())
    css = client.get("/static/style.css").text
    assert "--tap: 44px" in css and "@media (max-width: 700px)" in css
    for path in ("/models", "/jobs", "/trading"):
        p = page(client.get(path).text)
        for table in p.select("table"):
            assert table.has_class("stack") and table.closest(".table-wrap") is not None, (path, table)
        assert "width=device-width" in p.one('meta[name="viewport"]').attr("content")
    assert "min-height: var(--tap)" in css.split("@media (max-width: 700px)")[1]


def test_metrics_render_as_pairs_and_stacked_tables(client, conn, make_worker):
    """MEDIUM: the whole-backtest metrics are labelled pairs (no sideways scroll on a
    phone) and the per-season and top-list tables stack like the leaderboard."""
    w = make_worker("box1", role="backtest")
    model = insert_model(conn, metrics=backtest_metrics(n_bets=0, roi=0.0, max_drawdown=0.004, per_season=[
        {"season": 2016, "n_games": 100, "n_bets": 0, "roi": 0.0, "pnl_cents": 0, "log_loss": 0.66, "market_log_loss": 0.66, "max_drawdown": None},
    ]))
    html = client.get(f"/models/{model['id']}").text
    p = page(html)
    backtest = p.card("backtest")
    assert backtest.has('[data-list="metrics"]') and backtest.prop("log-loss vs market").startswith("0.660 vs 0.659")
    assert backtest.has('[data-list="per-season"]') and "drawdown -" in backtest.row("season", 2016).text
    assert [backtest.prop(k).split(" ")[0] for k in ("ROI", "hit rate", "avg edge")] == ["-", "-", "-"], "no bets: no ROI, hit rate or edge"
    assert backtest.prop("max drawdown").startswith("0.4%"), "a 0.4% drawdown is not rounded to 0%"
    assert all("+0.0%" not in p.card(name).text for name in ("model", "robustness", "snapshot", "backtest"))
    assert p.action("assign").target == f"/trading?model={model['id']}#assign" and p.action("assign").text == "Assign"
    editor = p.form("summary").closest("details")
    assert editor is not None and not editor.is_open and p.text.count("No summary yet.") == 1, "the summary editor is folded"
    assert p.prop("validation shrunk ROI").startswith("not validated") and p.prop("search shrunk ROI").startswith("+0.00%")
    assert "Not validated yet: no held-out numbers" in p.card("robustness").text and p.action("validate").text == "Validate"
    assert all(t.has_class("stack") for t in p.select("table") if t.attr("data-list") != "calibration")
    # The leaderboard row: "-" for ROI without bets, one-decimal drawdown.
    row = page(client.get("/models").text).row("model", model["id"])
    assert row.one(".row-value").text == "ROI -" and "search era 0 bets" in row.one(".row-meta").text
    assert row.action("assign").target.startswith("/trading?model=")
    # The search result page: a stacked top list with a shrunk ROI percentage.
    top = [{"index": 0, "params": {"k": 20.0, "hfa": 50.0, "mov_scale": 1}, "score": -0.0103, "metrics": backtest_metrics(n_bets=5, roi=-0.355, max_drawdown=0.5)}]
    ms = client.post("/api/jobs", json={"kind": "model_search", "params": {"family": "elo_blend", "n": 1}, "target": w.id}).json()
    conn.execute("UPDATE jobs SET status = 'succeeded', result = %s WHERE id = %s", (__import__("psycopg").types.json.Jsonb({"evaluated": 1, "seasons": [2016], "top": top, "created_models": []}), ms["id"]))
    p = page(client.get(f"/jobs/{ms['id']}").text)
    assert p.listing("candidates").has_class("stack") and "shrunk ROI" in p.listing("candidates").text
    top_row = p.row("candidate", 1)
    assert "shrunk ROI -1.03%" in top_row.text and "ROI -35.5%" in top_row.text and "drawdown 50.0%" in top_row.text
    assert top_row.text.startswith("#1 K 20 · HFA 50 · MOV on")
    bt = client.post("/api/jobs", json={"kind": "backtest", "params": {"model_id": str(model["id"])}, "target": w.id}).json()
    conn.execute("UPDATE jobs SET status = 'succeeded', result = %s WHERE id = %s", (__import__("psycopg").types.json.Jsonb(backtest_metrics(n_bets=300, roi=0.04, per_season=[{"season": 2016, "n_games": 100, "n_bets": 30, "roi": 0.1, "pnl_cents": 3000, "log_loss": 0.66, "market_log_loss": 0.66, "max_drawdown": 0.03}])), bt["id"]))
    p = page(client.get(f"/jobs/{bt['id']}").text)
    assert p.has('[data-list="metrics"]') and p.has('[data-list="per-season"]') and p.prop("hit rate") == "52.0%"


def test_send_cards_fold_so_the_job_list_is_near_the_top(client, conn):
    """Step 7: every send form sits in one "New job" disclosure, closed by default, with
    a kind select (JavaScript shows only the chosen form; without it every form shows
    under its heading). A prefill link or a rejected post opens it on that kind."""
    model = insert_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, trained_through=[2024, 18])
    kinds = ("backtest", "model_search", "train", "validate", "sleep")

    def new_job(html):
        p = page(html)
        box = p.card("new")
        picked = box.one('select[data-switch="job-kind"] option[selected]').attr("value")
        return p, box, picked

    html = client.get("/jobs").text
    p, box, picked = new_job(html)
    assert not box.is_open and picked == "backtest" and box.one(".disclosure-title").text == "New job"
    assert all(p.form(k).closest("details") is box for k in kinds), "every form is inside the one disclosure"
    assert [box.one(f'[data-card="send-{k}"]').one("h2").text for k in kinds] == ["Backtest", "Model search", "Train", "Validate", "Sleep (test)"]
    assert [box.one(f'[data-card="send-{k}"]').attr("data-when") for k in kinds] == list(kinds)
    assert all(box.one(f'[data-card="send-{k}"]').attr("data-switch-for") == "job-kind" for k in kinds)
    assert not box.select("[hidden]"), "with JavaScript off every form shows"
    assert box.one('select[data-switch="job-kind"]').closest("label").has_class("js-only"), "the kind select needs JavaScript"
    assert p.one(".tabs").start > box.start and p.text.count("Nothing running or queued.") == 1, "the job list follows the folded forms"
    assert p.form("train").one(f'option[value="{model["id"]}"]').text.endswith(f"K 20 · HFA 50 · MOV on · thru 2024 w18 · {str(model['id'])[:8]}"), "the select label fits a phone"
    _, box, picked = new_job(client.get(f"/jobs?train_model={model['id']}").text)
    assert box.is_open and picked == "train"
    _, box, picked = new_job(client.get(f"/jobs?validate_model={model['id']}").text)
    assert box.is_open and picked == "validate"
    r = client.post("/jobs", data={"kind": "model_search", "family": "elo_blend", "n": "0", "target": "any_idle"}, follow_redirects=False)
    _, box, picked = new_job(r.text)
    assert r.status_code == 400 and box.is_open and picked == "model_search"
    insert_model(conn, family="elo_blend", params={"k": 21.0})
    assert "elo_blend · K 21" not in client.get("/jobs").text, "one family: no family prefix"


def test_jobs_tabs_split_running_from_done(client, conn, make_worker):
    """Running (queued, running, held) and Done (finished, failed, cancelled) are two
    link tabs with aria-current; each job is one row with its target and state chip."""
    w = make_worker("box1", role="backtest")
    model = insert_model(conn)
    running = lease_job(conn, w, "validate", {"model_id": str(model["id"])})
    conn.execute("UPDATE jobs SET progress = 0.42 WHERE id = %s", (running["id"],))
    queued = client.post("/api/jobs", json={"kind": "model_search", "params": {"family": "elo_blend", "n": 50}}).json()
    failed = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 5}}).json()
    conn.execute("UPDATE jobs SET status = 'failed', error = 'boom', finished_at = now() WHERE id = %s", (failed["id"],))
    p = page(client.get("/jobs").text)
    tabs = {a.attr("data-tab"): a for a in p.select(".tabs a")}
    assert tabs["running"].is_current and not tabs["done"].is_current and tabs["done"].target == "/jobs?tab=done"
    assert tabs["running"].one(".count").text == "2" and tabs["done"].one(".count").text == "1"
    assert set(p.row_ids("job")) == {str(running["id"]), str(queued["id"])}
    row = p.row("job", running["id"])
    assert row.one("a.row-main").target == f"/jobs/{running['id']}" and row.one(".row-title").text == f"validate {str(model['id'])[:8]}"
    assert row.chip("leased").text == "leased" and row.one('[role="progressbar"]').attr("aria-valuenow") == "42"
    assert "box1" in row.one(".row-meta").text and row.action("cancel").target == f"/jobs/{running['id']}/cancel"
    assert "model_search elo_blend n 50" in p.row("job", queued["id"]).text
    assert p.stat("running").one(".stat-value").text == "1" and p.stat("queued").one(".stat-value").text == "1"
    done = page(client.get("/jobs?tab=done").text)
    assert done.one('.tabs a[data-tab="done"]').is_current and done.row_ids("job") == [str(failed["id"])]
    assert done.row("job", failed["id"]).chip("failed").text == "failed" and not done.row("job", failed["id"]).has('[data-action="cancel"]')
    assert "finished" in done.row("job", failed["id"]).one(".row-meta").text
    assert done.stat("done").one(".stat-note").text == "1 failed"
    assert page(client.get("/jobs?tab=bogus").text).one('.tabs a[data-tab="running"]').is_current


# ------------------------------------------------------------------ step 4: trading


def _open_order(conn, setup, **kw):
    """An approved order moved to open, as the paper executor would."""
    from host.trading import orders

    row = approved_order(conn, setup, **kw)
    orders.set_status(conn, row["id"], "open", "test", expected=("approved",), submitted_at=datetime.now(timezone.utc))
    return order_row(conn, row["id"])


def _fill(conn, order, price=0.52, size=10, fee=12, age_s=0):
    """Record a fill on an open order (fills row, ledger fill, status partial/filled)."""
    from host.trading import orders

    orders.record_fill(conn, order["id"], price, size, fee, order["mode"], "test", snapshot_id=order["snapshot_id"])
    if age_s:
        conn.execute("UPDATE fills SET ts = now() - make_interval(secs => %s) WHERE order_id = %s", (age_s, order["id"]))
    return conn.execute("SELECT * FROM fills WHERE order_id = %s ORDER BY id DESC LIMIT 1", (order["id"],)).fetchone()


def _bet(conn, order, pnl_cents, settled_age_s=0, worker_id=None, clv=0.01):
    """A settled bet for an order, `settled_age_s` ago (seeded straight in SQL)."""
    a = assignment_row(conn, order["assignment_id"])
    conn.execute(
        """
        INSERT INTO bets (order_id, assignment_id, model_id, lineage_id, game_id, worker_id, mode, date, event, platform,
                          contract, side, entry_price, fee_cents, cost_cents, stake_cents, closing_price, clv, result, pnl_cents,
                          settled_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, current_date, 'KC @ LV', 'sim', 'LV wins', 'home', 0.52, 12, 520, 532, 0.53, %s,
                %s, %s, now() - make_interval(secs => %s))
        """,
        (order["id"], a["id"], a["model_id"], a["lineage_id"], a["game_id"], worker_id or order["worker_id"], order["mode"],
         clv, "win" if pnl_cents >= 0 else "loss", pnl_cents, settled_age_s),
    )


def _score(conn, model, game_id, n_bets, pnl_cents, stake_cents, avg_clv):
    conn.execute(
        """
        INSERT INTO model_scores (model_id, game_id, mode, lineage_id, n_bets, stake_cents, pnl_cents, avg_clv)
        VALUES (%s, %s, 'paper', %s, %s, %s, %s, %s)
        """,
        (model["id"], game_id, model["lineage_id"], n_bets, stake_cents, pnl_cents, avg_clv),
    )


TRADING_CARDS = ("assignments", "positions", "open-orders", "orders", "fills", "unmatched", "markets", "exchange", "ledger")


def test_trading_page_empty_state_and_fragment(client, conn):
    p = page(client.get("/trading").text)
    assert p.page_name == "trading" and p.nav("trading").is_current
    assign = p.form("assign").closest("details")
    assert assign is not None and not assign.is_open, "the create form is folded"
    assert "No assignments yet." in p.card("assignments").text and "No open orders." in p.card("open-orders").text
    assert "No orders yet." in p.card("orders").text and "No fills yet." in p.card("fills").text
    assert "Every market is mapped." in p.card("unmatched").text and "No mapped markets yet." in p.card("markets").text
    assert p.card("exchange").chip("exchange-down").text == "DOWN" and "never" in p.card("exchange").text
    assert p.card("ledger").chip("ledger-ok").text == "OK" and "Replay of 0 bankrolls" in p.card("ledger").text
    assert "No upcoming game has a confirmed market yet" in p.form("assign").text and "No model of a non-retired lineage" in p.form("assign").text
    create = p.form("assign").one('button[type="submit"]')
    assert create.text == "Create assignment" and create.disabled
    assert not p.has('[data-action="activate-all-paper"]') and not p.has('[data-action="cancel-all"]')
    assert p.has("#trading-live") and set(TRADING_CARDS) <= set(p.cards())
    fragment = page(client.get("/fragments/trading").text)
    assert not fragment.has("html") and not fragment.has("#trading-live") and fragment.card("assignments")
    assert not fragment.has('[data-form="assign"]'), "the create form stays out of the refreshed region"
    assert set(TRADING_CARDS) <= set(fragment.cards())


def test_trading_page_shows_assignments_orders_fills_markets_and_exchange(client, conn):
    setup = trade_setup(conn)
    other = insert_market(conn, GAME_ID, side="away")
    insert_snapshot(conn, other["id"], bid=0.46, ask=0.48, age_s=90)
    loose = insert_market(conn, GAME_ID, side="home", confirmed=False)
    conn.execute("UPDATE markets SET title = 'Chiefs vs Raiders <b>x</b>' WHERE id = %s", (loose["id"],))
    opened = _open_order(conn, setup)
    _fill(conn, opened, size=4)
    rejected = approve(conn, setup, size=200)
    assert rejected["reason"] == "max_bet"
    conn.execute("UPDATE exchange_state SET heartbeat_at = now() - interval '3 seconds', market_source = 'sim', last_error = 'boom <i>'")
    html = client.get("/trading").text
    live = page(html).one("#trading-live")
    # assignments
    row = live.card("assignments").row("assignment", setup.assignment["id"])
    assert "KC @ LV" in row.text and GAME_ID in row.text and f"/models/{setup.model['id']}" in row.hrefs
    assert row.chip("paper").text == "paper" and row.chip("active").text == "active"
    bank = conn.execute("SELECT * FROM bankrolls WHERE assignment_id = %s", (setup.assignment["id"],)).fetchone()
    assert f"avail ${bank['available_cents'] // 100}.{bank['available_cents'] % 100:02d}" in row.text
    assert "reserved $" in row.text and "open $2.08" in row.text and "realized -$0.12" in row.text
    assert "open orders 1" in row.text and row.action("halt").target == f"/assignments/{setup.assignment['id']}/halt"
    assert row.actions() == ["halt"], "no Settle now, no Activate"
    # open orders with a cancel button and the cancel-all form
    open_orders = live.card("open-orders")
    assert open_orders.row("order", opened["id"]).action("cancel").target == f"/orders/{opened['id']}/cancel"
    assert open_orders.action("cancel-all").target == "/cancel-all"
    assert "10 @ 0.52 (4 filled" in open_orders.row("order", opened["id"]).text
    assert open_orders.row("order", opened["id"]).chip("partial").text == "partial"
    # recent orders: the rejection with its reason, the rationale, the worker name
    recent = live.card("orders")
    refused = recent.row("order", rejected["order_id"])
    reason = refused.one(".reason").text
    assert reason.startswith("max_bet: over max bet $") and reason.endswith("> $25.00")
    assert refused.chip("rejected").text == "rejected" and "my 0.58 vs ask 0.52, fee 0.012, edge 0.04" in refused.text
    assert "200 @ 0.52" in refused.text and "trader-" in recent.text
    assert "+4.0%" in recent.text and "my 0.58 vs 0.51" in recent.text
    # fills
    fills = live.card("fills")
    assert len(fills.rows("fill")) == 1 and "4 @ 0.52" in fills.text and "of 10 @ 0.52" in fills.text and "$0.12" in fills.text
    # unmatched market with the link form, escaped title
    unmatched = live.card("unmatched").row("market", loose["id"])
    assert "Chiefs vs Raiders <b>x</b>" in unmatched.text and "Chiefs vs Raiders &lt;b&gt;x&lt;/b&gt;" in html
    link = unmatched.form("link")
    assert link.target == f"/markets/{loose['id']}/link"
    assert link.one('select[name="game_id"] option[selected]').attr("value") == GAME_ID
    assert link.one('select[name="side"] option[selected]').text == "home wins" and "(50%)" in unmatched.text
    # mapped markets with snapshot ages
    markets = live.card("markets")
    assert len(markets.rows("market")) == 2 and "0.50 / 0.52" in markets.text and "0.46 / 0.48" in markets.text
    assert markets.row("market", other["id"]).one(".stale-age").text == "1 min ago"
    assert "$2,000.00" in markets.text and "home wins" in markets.text and "away wins" in markets.text
    # exchange state and ledger
    exchange = live.card("exchange")
    assert exchange.chip("exchange-up").text == "up" and "3 s ago" in exchange.text and exchange.prop("source") == "sim"
    assert "boom <i>" in exchange.prop("last error") and exchange.action("probe").target == "/exchange/probe"
    assert "Replay of 1 bankroll " in live.card("ledger").text and live.card("ledger").chip("ledger-ok").text == "OK"
    conn.execute("UPDATE bankrolls SET available_cents = available_cents + 1 WHERE id = %s", (bank["id"],))
    broken = page(client.get("/fragments/trading").text).card("ledger")
    assert broken.chip("ledger-problems").text == "problems" and "cached" in broken.text and "ledger sums to" in broken.text
    tables = page(html).select("table")
    assert all(t.has_class("stack") for t in tables)


def test_create_assignment_form(client, conn):
    game = insert_game(conn)
    insert_market(conn, GAME_ID)
    insert_game(conn, "2026_05_DAL_PHI", home="PHI", away="DAL", kickoff_in_s=3 * 86400)
    insert_game(conn, "2025_01_OLD_GAME", kickoff_in_s=-86400)
    insert_market(conn, "2025_01_OLD_GAME")
    model = insert_model(conn, status="paper_ok", params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, trained_through=[2024, 18])
    retired = insert_model(conn, status="retired", params={"k": 21.0})
    html = client.get("/trading").text
    form = page(html).form("assign")
    games = [o.attr("value") for o in form.select('select[name="game_id"] option')]
    assert games == [GAME_ID] and form.one(f'option[value="{GAME_ID}"]').text.startswith("KC @ LV · "), "only upcoming games with confirmed markets"
    assert form.one(f'option[value="{model["id"]}"]').text == f"elo_blend · K 20 · HFA 50 · MOV on · thru 2024 w18 · {str(model['id'])[:8]}"
    assert str(retired["id"]) not in html
    assert _value(form, "bankroll") == "100.00" and [o.attr("value") for o in form.select('select[name="mode"] option')] == ["paper"]
    assert form.one('select[name="mode"] option[selected]').text == "paper"
    create = form.one('button[type="submit"]')
    assert create.text == "Create assignment" and not create.disabled
    # ?model= opens the form with that model selected (the Assign button on the Models page)
    form = page(client.get(f"/trading?model={model['id']}").text).form("assign")
    assert form.closest("details").is_open and form.one('select[name="model_id"] option[selected]').attr("value") == str(model["id"])
    r = client.post("/assignments", data={"game_id": GAME_ID, "model_id": str(model["id"]), "mode": "paper", "bankroll": "250", "max_bet": "5"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/trading", r.text
    assert flash_cookie(r).endswith(f"created: paper on {GAME_ID}, bankroll $250.00")
    a = conn.execute("SELECT * FROM assignments").fetchone()
    assert a["model_id"] == model["id"] and a["max_bet_cents"] == 500 and a["status"] == "active"
    bank = conn.execute("SELECT * FROM bankrolls WHERE assignment_id = %s", (a["id"],)).fetchone()
    assert bank["initial_cents"] == 25000 and bank["available_cents"] == 25000
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (a["job_id"],)).fetchone()
    assert job["kind"] == "trade" and job["status"] == "queued" and job["params"] == {"assignment_id": str(a["id"])}
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'assignment_created'").fetchone()["n"] == 1
    p = page(client.get("/trading").text)
    assert p.row("assignment", a["id"]) and not p.form("assign").closest("details").is_open
    # a refusal re-renders the page with the error inline, the submitted values kept, nothing stored
    for data, status, message in [
        ({"game_id": GAME_ID, "model_id": str(model["id"]), "mode": "paper", "bankroll": "lots"}, 400, "Bankroll must be a dollar amount"),
        ({"game_id": GAME_ID, "model_id": str(model["id"]), "mode": "paper", "bankroll": "100"}, 409, "already has a paper assignment"),
        ({"game_id": "nope", "model_id": str(model["id"]), "mode": "paper", "bankroll": "100"}, 400, "unknown game"),
        ({"game_id": GAME_ID, "model_id": str(retired["id"]), "mode": "paper", "bankroll": "100"}, 400, "retired"),
        ({"game_id": GAME_ID, "model_id": str(model["id"]), "mode": "live", "bankroll": "100"}, 409, "live trading is disabled"),
        ({"game_id": GAME_ID, "model_id": str(model["id"]), "mode": "paper", "bankroll": "100", "max_bet": "1,5"}, 400, "Max bet: use a dot"),
    ]:
        r = client.post("/assignments", data=data, follow_redirects=False)
        assert r.status_code == status and r.headers["content-type"].startswith("text/html"), (data, r.status_code)
        form = page(r.text).form("assign")
        assert any(message in e for e in form.texts(".inline-error")), (data, message)
        assert form.closest("details").is_open and _value(form, "bankroll") == data["bankroll"]
    assert conn.execute("SELECT count(*) AS n FROM assignments").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM bankrolls").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 1
    assert game["game_id"] == GAME_ID


def test_halt_activate_and_settle_forms(client, conn):
    setup = trade_setup(conn)
    aid = setup.assignment["id"]
    opened = _open_order(conn, setup)
    _fill(conn, opened, size=10)
    r = client.post(f"/assignments/{aid}/halt", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/trading" and flash_cookie(r) == f"assignment {str(aid)[:8]} halted"
    assert assignment_row(conn, aid)["status"] == "halted" and order_row(conn, opened["id"])["status"] == "filled"
    row = page(client.get("/trading").text).row("assignment", aid)
    assert row.chip("halted").text == "halted" and row.action("activate").target == f"/assignments/{aid}/activate"
    assert not row.has('[data-action="halt"]')
    # settle is refused until the game is final (flash, not an error page)
    r = client.post(f"/assignments/{aid}/settle", data={}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "settle refused: the game is not final yet"
    r = client.post(f"/assignments/{aid}/activate", data={}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == f"assignment {str(aid)[:8]} activated"
    assert assignment_row(conn, aid)["status"] == "active"
    assert client.post(f"/assignments/{aid}/activate", data={}, follow_redirects=False).status_code == 303, "idempotent"
    assert client.post("/assignments/00000000-0000-0000-0000-000000000000/halt", data={}, follow_redirects=False).status_code == 404
    # the game goes final: the row offers Settle now; settling writes the bet and the score
    conn.execute("UPDATE games SET status = 'final', home_score = 24, away_score = 20 WHERE game_id = %s", (GAME_ID,))
    row = page(client.get("/trading").text).row("assignment", aid)
    assert row.action("settle").target == f"/assignments/{aid}/settle" and row.action("settle").text == "Settle now" and "final 20-24" in row.text
    r = client.post(f"/assignments/{aid}/settle", data={}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == f"{GAME_ID} settled: 1 bets, P&L $4.68", flash_cookie(r)
    assert assignment_row(conn, aid)["status"] == "settled"
    bet = conn.execute("SELECT * FROM bets").fetchone()
    assert bet["result"] == "win" and bet["pnl_cents"] == 468 and bet["worker_id"] == setup.worker.id
    p = page(client.get("/trading").text)
    assert p.row("assignment", aid).chip("settled").text == "settled" and "paper today $4.68 · all $4.68" in topbar(p).text
    assert p.row("market", setup.market["id"]).chip("resolved").text == "resolved YES"
    fleet = page(fleet_html(client))
    assert "today $4.68" in fleet.row("worker", setup.worker.id).text, "the worker's card shows its P&L"
    r = client.post(f"/assignments/{aid}/settle", data={}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("settle refused") or "settled" in flash_cookie(r)
    board = page(client.get("/models").text)
    assert "1 g · 1 bets · $4.68" in board.row("model", setup.model["id"]).text


def test_order_cancel_and_cancel_all_forms(client, conn):
    setup = trade_setup(conn)
    first = _open_order(conn, setup)
    second = approved_order(conn, setup, price=0.53)
    r = client.post(f"/orders/{first['id']}/cancel", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/trading" and flash_cookie(r) == f"order {str(first['id'])[:8]} cancelled"
    assert order_row(conn, first["id"])["status"] == "cancelled"
    bank = conn.execute("SELECT * FROM bankrolls WHERE assignment_id = %s", (setup.assignment["id"],)).fetchone()
    assert bank["reserved_cents"] == second["cost_cents"], "the first order's reservation was released"
    r = client.post(f"/orders/{first['id']}/cancel", data={}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).endswith("cancelled"), "a terminal order answers its status"
    assert client.post("/orders/not-an-order/cancel", data={}, follow_redirects=False).status_code == 404
    assert client.post(f"/orders/{uuid.uuid4()}/cancel", data={}, follow_redirects=False).status_code == 404
    r = client.post("/cancel-all", data={"mode": "live"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "0 orders cancelled, 0 cancel requested"
    assert order_row(conn, second["id"])["status"] == "approved"
    r = client.post("/cancel-all", data={"mode": ""}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "1 orders cancelled, 0 cancel requested"
    assert order_row(conn, second["id"])["status"] == "cancelled" and bankroll_reserved(conn, setup) == 0
    r = client.post("/cancel-all", data={"mode": "margin"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("cancel all refused")
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'cancel_all'").fetchone()["n"] == 2
    assert client.get("/api/settings").json()["kill_switch"] is False, "cancel-all is not a kill"


def bankroll_reserved(conn, setup):
    return conn.execute("SELECT reserved_cents FROM bankrolls WHERE assignment_id = %s", (setup.assignment["id"],)).fetchone()["reserved_cents"]


def test_link_market_form(client, conn):
    insert_game(conn)
    insert_game(conn, "2026_05_DAL_PHI", home="PHI", away="DAL", kickoff_in_s=3 * 86400)
    loose = insert_market(conn, GAME_ID, confirmed=False)
    r = client.post(f"/markets/{loose['id']}/link", data={"game_id": "2026_05_DAL_PHI", "side": "away"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/trading#markets" and flash_cookie(r) == "market linked to 2026_05_DAL_PHI (away)"
    m = conn.execute("SELECT * FROM markets WHERE id = %s", (loose["id"],)).fetchone()
    assert m["mapping_confirmed"] is True and m["game_id"] == "2026_05_DAL_PHI" and m["side"] == "away" and m["mapping_confidence"] == 1.0
    p = page(client.get("/trading").text)
    assert "Every market is mapped." in p.card("unmatched").text and "2026_05_DAL_PHI · away wins" in p.row("market", loose["id"]).text
    assert p.form("assign").one('option[value="2026_05_DAL_PHI"]').text.startswith("DAL @ PHI · "), "a game without markets can be assigned now"
    r = client.post(f"/markets/{loose['id']}/link", data={"game_id": "nope", "side": "home"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/trading#unmatched" and flash_cookie(r).startswith("market not linked: unknown game")
    r = client.post(f"/markets/{loose['id']}/link", data={"game_id": GAME_ID, "side": "sideways"}, follow_redirects=False)
    assert flash_cookie(r) == "market not linked: side must be home or away"
    assert client.post(f"/markets/{uuid.uuid4()}/link", data={"game_id": GAME_ID, "side": "home"}, follow_redirects=False).status_code == 404
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'market_linked'").fetchone()["n"] == 1


def test_activate_all_paper_after_a_kill_reset(client, conn):
    setup = trade_setup(conn)
    _open_order(conn, setup)
    second = make_assignment(conn, GAME_ID)
    done = make_assignment(conn, insert_game(conn, "2026_05_DAL_PHI", home="PHI", away="DAL")["game_id"])
    conn.execute("UPDATE games SET status = 'final', home_score = 1, away_score = 0 WHERE game_id = '2026_05_DAL_PHI'")
    r = client.post("/kill", follow_redirects=False)
    assert r.status_code == 303
    p = page(client.get("/trading").text)
    assert "Trading is killed: every assignment stays halted" in p.card("assignments").text and not p.has('[data-action="activate-all-paper"]')
    assert [row.first('[data-chip="halted"]') is not None for row in p.rows("assignment")] == [True] * 3 and "No open orders." in p.text
    assert not p.has('[data-action="activate"]'), "no per-row activate under kill"
    assert p.has('[data-banner="exchange-down"]'), "no exchange heartbeat while killed"
    r = client.post("/assignments/activate-paper", data={}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "activate all paper refused: the kill switch is on; reset it first"
    client.post("/kill/reset", data={"confirm": "RESUME"}, follow_redirects=False)
    p = page(client.get("/trading").text)
    assert p.action("activate-all-paper").text == "Activate all paper (2)" and p.row("assignment", done["id"]).action("settle").target == f"/assignments/{done['id']}/settle"
    r = client.post("/assignments/activate-paper", data={}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "2 paper assignments activated"
    assert assignment_row(conn, setup.assignment["id"])["status"] == "active" and assignment_row(conn, second["id"])["status"] == "active"
    assert assignment_row(conn, done["id"])["status"] == "halted", "a final game's assignment waits for settlement"
    assert not page(client.get("/trading").text).has('[data-action="activate-all-paper"]')
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'activate_all_paper'").fetchone()["n"] == 1


def test_topbar_banners(client, conn):
    def banners():
        return page(client.get("/fragments/topbar").text).select("[data-banner]")

    assert not banners(), "no heartbeat but nothing at stake: no banner"
    client.post("/kill", follow_redirects=False)
    [down] = banners()
    assert (down.attr("data-banner"), down.target, down.attr("role"), down.text) == ("exchange-down", "/trading#exchange", "alert", "EXCHANGE DOWN")
    client.post("/kill/reset", data={"confirm": "RESUME"}, follow_redirects=False)
    assert not banners()
    setup = trade_setup(conn)
    _open_order(conn, setup)
    assert "EXCHANGE DOWN" in client.get("/").text, "an open order with no heartbeat"
    conn.execute("UPDATE exchange_state SET heartbeat_at = now() - interval '14 seconds'")
    assert "EXCHANGE DOWN" not in client.get("/fragments/topbar").text
    conn.execute("UPDATE exchange_state SET heartbeat_at = now() - interval '16 seconds'")
    assert "EXCHANGE DOWN" in client.get("/fragments/topbar").text
    # unattended: an active assignment whose trade job sits queued for over a minute
    assert "unattended" not in client.get("/fragments/topbar").text, "the job is leased"
    conn.execute("UPDATE jobs SET status = 'queued', lease_worker_id = NULL, lease_token = NULL, updated_at = now() - interval '30 seconds',"
                 " created_at = now() - interval '2 minutes' WHERE id = %s", (setup.job["id"],))
    assert "unattended" not in client.get("/fragments/topbar").text, "queued again 30 s ago"
    conn.execute("UPDATE jobs SET updated_at = now() - interval '61 seconds' WHERE id = %s", (setup.job["id"],))
    [warn] = [b for b in banners() if b.attr("data-banner") == "unattended"]
    assert (warn.attr("data-banner"), warn.target, warn.attr("role"), warn.text) == ("unattended", "/trading#assignments", "alert", "1 assignment unattended")
    second = make_assignment(conn, GAME_ID)
    conn.execute("UPDATE jobs SET created_at = now() - interval '2 minutes', updated_at = now() - interval '2 minutes' WHERE id = %s", (second["job_id"],))
    assert "2 assignments unattended" in client.get("/jobs").text
    client.post(f"/assignments/{second['id']}/halt", data={}, follow_redirects=False)
    assert "1 assignment unattended" in client.get("/fragments/topbar").text, "a halted assignment is not unattended"
    assert page(client.get("/").text).has('[data-banner][role="alert"]')


def test_pnl_maths(client, conn, make_worker):
    """Today = bets settled today (owner tz) + mark-to-mid change of open positions since
    the later of the day start and the fill; all-time = every bet + the whole unrealized."""
    from host import pnl

    start, end = pnl.owner_day(conn, datetime(2026, 10, 3, 3, 0, tzinfo=timezone.utc))
    assert (start.isoformat(), end.isoformat()) == ("2026-10-02T00:00:00-04:00", "2026-10-03T00:00:00-04:00"), "the owner's day in America/New_York"
    idle = make_worker("idle-box")
    setup = trade_setup(conn)
    helper = insert_worker(conn, "helper", role="trade")
    assert client.get("/api/pnl").json() == {
        "today_cents": 0, "all_time_cents": 0, "by_worker": {idle.id: 0, setup.worker.id: 0, helper.id: 0},
        "by_mode": {"paper": {"today_cents": 0, "all_time_cents": 0}, "live": {"today_cents": 0, "all_time_cents": 0}},
    }
    # an old fill: 10 @ 0.52 two days ago; the mid was 0.50 before today and is 0.51 now
    old = _open_order(conn, setup)
    _fill(conn, old, price=0.52, size=10, age_s=2 * 86400)
    insert_snapshot(conn, setup.market["id"], bid=0.49, ask=0.51, age_s=25 * 3600)
    insert_snapshot(conn, setup.market["id"], bid=0.50, ask=0.52)
    totals = pnl.pnl(conn)
    assert totals["today_cents"] == 10 and totals["all_time_cents"] == -10, "today: 510 - 500; all time: 510 - 520"
    assert totals["by_worker"][setup.worker.id] == 10 and totals["by_mode"]["paper"] == {"today_cents": 10, "all_time_cents": -10}
    # a fill made today counts from its own price
    fresh = _open_order(conn, setup, price=0.53)
    _fill(conn, fresh, price=0.53, size=10)
    totals = pnl.pnl(conn)
    assert totals["today_cents"] == 10 - 20 and totals["all_time_cents"] == -10 - 20
    # settled bets: one today (+300, the helper worker), one two days ago (-100)
    a = _open_order(conn, setup, price=0.54)
    b = _open_order(conn, setup, price=0.55)
    _bet(conn, a, 300, worker_id=helper.id)
    _bet(conn, b, -100, settled_age_s=2 * 86400)
    totals = client.get("/api/pnl").json()
    assert totals["today_cents"] == 290 and totals["all_time_cents"] == 170
    assert totals["by_worker"] == {idle.id: 0, setup.worker.id: -10, helper.id: 300}
    assert totals["by_mode"] == {"paper": {"today_cents": 290, "all_time_cents": 170}, "live": {"today_cents": 0, "all_time_cents": 0}}
    bar = topbar(page(client.get("/").text))
    assert re.search(r"\bpaper\b[^$]*\+?\$2\.90", bar.text) and not re.search(r"\blive\b", bar.text), "the current mode's P&L only"
    fleet = page(fleet_html(client))
    assert re.search(r"\+?\$3\.00", fleet.row("worker", helper.id).text) and "-$0.10" in fleet.row("worker", setup.worker.id).text
    card = fleet.row("worker", setup.worker.id)
    held = card.one(f'[data-job="{setup.job["id"]}"]')
    assert held.text == "KC @ LV held" and card.one("a.row-main").target == f"/jobs/{setup.job['id']}"
    assert not card.has('[role="progressbar"]'), "a held trade job has no progress to show"
    # a resolved market drops out of the open positions
    conn.execute("UPDATE markets SET status = 'resolved', resolved_yes = true WHERE id = %s", (setup.market["id"],))
    assert client.get("/api/pnl").json()["today_cents"] == 300
    set_setting(conn, "live_enabled", True)
    assert "live today $0.00 · all $0.00" in page(client.get("/fragments/topbar").text).text


def test_leaderboard_paper_columns_and_ranking(client, conn):
    """A validated lineage with 5 paper games and 30 paper bets ranks on shrunk CLV ahead
    of the validation-ranked ones; an unvalidated one never ranks, whatever its paper
    record (review 6A); paper columns show per row."""
    backtested = insert_validated_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, metrics=backtest_metrics(n_bets=400, roi=0.05), validation=validation_metrics(n_bets=400, roi=0.05), status="paper_ok")
    papered = insert_model(conn, params={"k": 30.0, "hfa": 60.0, "mov_scale": 0}, metrics=backtest_metrics(n_bets=10, roi=0.01), status="paper_ok",
                           validation=validation_metrics(n_bets=60, roi=0.01))
    better = insert_model(conn, params={"k": 31.0, "hfa": 60.0, "mov_scale": 0}, metrics=backtest_metrics(n_bets=10, roi=0.01), status="live_eligible",
                          validation=validation_metrics(n_bets=60, roi=0.01))
    almost = insert_model(conn, params={"k": 32.0}, metrics=backtest_metrics(n_bets=10, roi=0.01))
    retired = insert_model(conn, params={"k": 33.0}, status="retired")
    for i in range(5):
        insert_game(conn, f"2026_0{i + 1}_A_B", kickoff_in_s=-(i + 1) * 86400)
        _score(conn, papered, f"2026_0{i + 1}_A_B", 6, 120, 6000, 0.02)
        _score(conn, better, f"2026_0{i + 1}_A_B", 6, -60, 6000, 0.03)
        _score(conn, retired, f"2026_0{i + 1}_A_B", 6, 900, 6000, 0.09)
    for i in range(4):
        _score(conn, almost, f"2026_0{i + 1}_A_B", 10, 100, 1000, 0.05)
    board = client.get("/api/models").json()
    ranked = board["ranked"]
    assert [m["id"] for m in ranked] == [str(better["id"]), str(papered["id"]), str(backtested["id"])]
    assert [m["rank"] for m in ranked] == [1, 2, 3] and [m["rank_mode"] for m in ranked] == ["paper", "paper", "validation"]
    assert ranked[0]["paper"] == {"games": 5, "bets": 30, "pnl_cents": -300, "stake_cents": 30000, "roi": -0.01, "avg_clv": pytest.approx(0.03)}
    assert ranked[0]["paper_score"] == pytest.approx(0.03 * 30 / 55) and ranked[1]["paper_score"] == pytest.approx(0.02 * 30 / 55)
    assert ranked[2]["paper"]["games"] == 0 and ranked[2]["paper"]["roi"] is None and ranked[2]["score"] == 0.05 * 400 / 500
    assert ranked[0]["live"] == {"games": 0, "bets": 0, "pnl_cents": 0, "stake_cents": 0, "roi": None, "avg_clv": None}
    unranked = {m["id"]: m for m in board["unranked"]}
    assert set(unranked) == {str(almost["id"]), str(retired["id"])}
    assert unranked[str(almost["id"])]["paper"]["games"] == 4 and unranked[str(almost["id"])]["rank_mode"] == "validation", "4 games: not yet"
    assert unranked[str(almost["id"])]["unranked_reason"] == "not validated"
    assert unranked[str(retired["id"])]["paper"]["games"] == 5, "retired lineages are never ranked, however good"
    # a fifth paper game does not rank a lineage the validation era has not judged
    _score(conn, almost, "2026_05_A_B", 10, 1000, 1000, 0.06)
    still = {m["id"]: m for m in client.get("/api/models").json()["unranked"]}[str(almost["id"])]
    assert still["rank_mode"] == "paper" and still["unranked_reason"] == "not validated"
    conn.execute("UPDATE models SET validation_metrics = %s WHERE lineage_id = %s", (__import__("psycopg").types.json.Jsonb(validation_metrics()), almost["lineage_id"]))
    assert client.get("/api/models").json()["ranked"][0]["id"] == str(almost["id"]), "validated, it ranks first on paper CLV"
    p = page(client.get("/models").text)
    first = p.row("model", almost["id"])
    assert first.text.startswith("#1") and first.chip("rank-paper").text == "paper" and first.chip("rank-paper").closest("[title]").attr("title") == "ranked on paper CLV"
    assert first.one(".row-value").text == "CLV +5.2%" and "5 games · 50 bets · +$14.00" in first.one(".row-meta").text
    last = p.row("model", backtested["id"])
    assert last.one(".row-value").text == "ROI +5.0%" and "rank-paper" not in last.chips() and last.text.startswith("#4")
    assert "5 paper games and 30 paper bets" in p.one("[data-sort-line]").text
    detail = page(client.get(f"/models/{better['id']}").text)
    assert detail.prop("paper record").startswith("5 games · 30 bets · -$3.00 · ROI -1.0% · CLV 0.030")
    assert "ranked on paper" in detail.prop("paper record")
    assert "no paper games yet" in page(client.get(f"/models/{backtested['id']}").text).prop("paper record")


def test_settings_trade_group_round_trip(client, conn):
    form = page(client.get("/settings").text).form("trade")
    assert form.target == "/settings/trade" and "Order approval" in form.texts("h3") and any(h.startswith("Paper thresholds") for h in form.texts("h3"))
    source = form.field("market_source")
    assert source.one("option[selected]").attr("value") == "sim" and source.has('option[value="polymarket_clob"]')
    assert _value(form, "participation") == "0.5" and _value(form, "gtd_seconds") == "900"
    assert form.input("trade_pregame_only").has_attr("checked") and _value(form, "paper_min_pnl") == "0.01"
    assert _value(form, "max_exposure_paper") == "0.00" and _value(form, "orders_per_s") == "5"
    assert '"gamma_url": "https://gamma-api.polymarket.com"' in form.field("market_source_config").one("textarea").text
    good = {
        "participation": "0.4", "book_max_age_s": "45", "gtd_seconds": "600", "orphan_cancel_after_s": "40", "trade_tick_s": "4",
        "max_paper_models_per_game": "2", "max_exposure_paper": "1,000", "max_exposure_live": "0",
        "market_source": "polymarket_clob", "market_lookahead_days": "9", "snapshot_active_s": "3", "snapshot_idle_s": "20",
        "snapshot_retention_days": "7", "scores_url": "https://example.com/scores",
        "market_source_config": '{"polymarket_clob": {"gamma_url": "https://g.example", "clob_url": "https://c.example", "tag_slug": "nfl"}}',
        "paper_min_games": "8", "paper_min_bets": "20", "paper_min_days": "14", "paper_min_clv": "0.005", "paper_min_pnl": "2.50",
        "orders_per_s": "4", "cancels_per_s": "8", "market_data_per_s": "9.5", "account_per_s": "1",
    }
    r = client.post("/settings/trade", data=good, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings" and flash_cookie(r) == "trade settings saved"
    s = client.get("/api/settings").json()
    assert s["participation"] == 0.4 and s["book_max_age_s"] == 45 and s["gtd_seconds"] == 600 and s["trade_tick_s"] == 4
    assert s["trade_pregame_only"] is False, "an unticked checkbox sends nothing"
    assert s["market_source"] == "polymarket_clob" and s["market_source_config"]["polymarket_clob"]["gamma_url"] == "https://g.example"
    assert s["thresholds_paper"] == {"min_games": 8, "min_bets": 20, "min_days": 14, "min_clv": 0.005, "min_pnl_cents": 250, "clv_ci_excludes_zero": False}, "the unticked CLV box stores false"
    assert s["max_exposure_cents"] == {"paper": 100000, "live": 0} and s["scores_url"] == "https://example.com/scores"
    assert s["rate_limits"] == {"orders_per_s": 4, "cancels_per_s": 8, "market_data_per_s": 9.5, "account_per_s": 1}
    p = page(client.get("/settings").text)
    assert p.field("market_source").one("option[selected]").attr("value") == "polymarket_clob"
    assert not p.input("trade_pregame_only").has_attr("checked") and p.input("trade_pregame_only").attr("value") == "true"
    assert not p.input("paper_clv_ci").has_attr("checked") and "CLV interval above zero" in p.field("paper_clv_ci").text
    r = client.post("/settings/trade", data={**good, "paper_clv_ci": "true"}, follow_redirects=False)
    assert r.status_code == 303 and client.get("/api/settings").json()["thresholds_paper"]["clv_ci_excludes_zero"] is True
    assert page(client.get("/settings").text).input("paper_clv_ci").has_attr("checked")
    assert _value(p, "max_exposure_paper") == "1000.00" and '"gamma_url": "https://g.example"' in p.field("market_source_config").text
    for bad, message in [
        ({**good, "participation": "2"}, "participation must be between 0 and 1"),
        ({**good, "market_source": "kalshi"}, "market_source must be one of sim, polymarket_us, polymarket_clob"),
        ({**good, "market_source_config": "[1, 2]"}, "Market source config must be a JSON object"),
        ({**good, "market_source_config": "{not json"}, "Market source config must be a JSON object"),
        ({**good, "paper_min_games": "x"}, "Paper min games must be a whole number"),
        ({**good, "paper_min_pnl": "1,5"}, "Paper min P&amp;L: use a dot for cents"),
        ({**good, "scores_url": "ftp://x"}, "scores_url must be an http(s) URL"),
        ({**good, "snapshot_active_s": "0"}, "snapshot_active_s must be between 1 and 300"),
    ]:
        r = client.post("/settings/trade", data=bad, follow_redirects=False)
        assert r.status_code == 400 and message in r.text, (bad, message)
        assert _value(page(r.text), "participation") == bad["participation"], "submitted values are kept"
    assert client.get("/api/settings").json()["participation"] == 0.4
    audited = [a["entity"] for a in conn.execute("SELECT entity FROM audit_log WHERE action = 'settings_changed' ORDER BY id").fetchall()]
    assert "market_source" in audited and "thresholds_paper" in audited and "trade_pregame_only" in audited and len(audited) == 18
    # saving the group again with a paper record present recomputes paper eligibility without error
    model = insert_model(conn, status="paper_ok", metrics=backtest_metrics())
    insert_game(conn, "2026_01_A_B", kickoff_in_s=-86400)
    _score(conn, model, "2026_01_A_B", 5, 100, 1000, 0.02)
    r = client.post("/settings/trade", data={**good, "paper_min_games": "1", "paper_min_bets": "1", "paper_min_days": "0"}, follow_redirects=False)
    assert r.status_code == 303


def test_probe_page_and_exchange_card(client, conn):
    """The probe button renders the raw payload page (the sim source needs no network)."""
    insert_game(conn)
    r = client.post("/exchange/probe", data={}, follow_redirects=False)
    p = page(r.text)
    assert r.status_code == 200 and p.page_name == "probe" and "Market probe" in p.one("h1").text
    assert p.has("#payload") and p.has('[data-copy="payload"]')
    assert "sim" in p.one("h1").text, "the probed source is named"


def test_trading_style_rules():
    """The banner and chip colours use the text-safe tokens (tests/test_style.py checks
    the component contract); the trading region refreshes."""
    from tests.test_style import declarations

    assert "var(--red)" in declarations(".banner-down") and "var(--amber-fill)" in declarations(".banner-warn")
    assert "var(--red)" in declarations(".chip-bad")
    js = (Path(__file__).resolve().parent.parent / "host" / "static" / "app.js").read_text()
    assert 'refresh("trading-live", "/fragments/trading")' in js and chr(0x2014) not in js


# ------------------------------------------------------------------ step 5: the live switch on the dashboard


def _phrase(conn, days: int = 0) -> str:
    today = datetime.now(timezone.utc).astimezone(owner_tz(conn)).date() + timedelta(days=days)
    return f"ENABLE LIVE TRADING {today.isoformat()}"


def _ready(conn, **overrides) -> None:
    """The exchange process has credentials, a fresh successful auth probe and a small skew."""
    now = datetime.now(timezone.utc)
    cols = {"credentials_present": True, "auth_ok": True, "auth_checked_at": now, "balance_cents": 50_000,
            "buying_power_cents": 48_000, "balance_checked_at": now, "clock_skew_ms": 120, "auth_failures": 0,
            "last_auth_error": None}
    cols.update(overrides)
    auth_state(conn, **cols)


def _smoke_order(conn, market, status: str = "open"):
    """A smoke order row as the CLI creates it: kind smoke, mode live, no assignment, no worker."""
    return conn.execute(
        """
        INSERT INTO orders (client_request_id, kind, market_id, mode, price, size, cost_cents, fee_cents_est, status,
                            exchange_order_id, submitted_at, rationale)
        VALUES (%s, 'smoke', %s, 'live', 0.45, 1, 45, 0, %s, 'pm-smoke-1', now(), 'smoke order') RETURNING *
        """,
        ("smoke-" + uuid.uuid4().hex[:24], market["id"], status),
    ).fetchone()


def _live_flag(conn) -> object:
    return conn.execute("SELECT value FROM settings WHERE key = 'live_enabled'").fetchone()["value"]


def _live_section(html: str) -> Node:
    return page(html).card("live")


def test_settings_live_group_off_state(client, conn):
    """Live off: the state, the typed enable form with today's phrase as the hint, no
    Disable button, credentials no, auth not checked, blank balances, no auto-kill."""
    html = client.get("/settings").text
    live = _live_section(html)
    state = live.one("[data-live-state]")
    assert not live.has_class("is-live") and state.attr("data-live-state") == "off" and state.text == "OFF"
    assert "Live trading is off" in live.text
    enable = live.form("live-on")
    assert enable.target == "/settings/live" and _value(enable, "confirm") == "" and enable.input("confirm").attr("placeholder") == _phrase(conn)
    assert live.one(".phrase").text == _phrase(conn) and "Enable live trading" in enable.text
    assert not live.has('[data-action="live-off"]') and "Disable live" not in live.text
    assert live.prop("credentials").startswith("no ") and live.prop("auth") == "not checked"
    assert (live.prop("balance"), live.prop("buying power"), live.prop("clock skew")) == ("-", "-", "-")
    assert live.prop("last auth error") == "none"
    assert "none since the last reset" in live.text and not live.has('[data-chip="auto-kill"]')
    assert "the exchange process has no credentials loaded" in live.text, "the preconditions are listed before the owner types"
    assert not page(html).has('[name="live_enabled"]'), "the switch has no generic settings field"
    assert mode_pill(page(html)) == "PAPER" and "LIVE" not in topbar(page(html)).text


def test_settings_live_group_on_state(client, conn):
    """Live on: since when and by whom, the Disable button instead of the form, auth ok
    with its age, balance and buying power in dollars, the skew, the LIVE pill."""
    enable_live(conn, buying_power_cents=48_000)
    conn.execute("UPDATE exchange_state SET balance_cents = 50_000, clock_skew_ms = 120, live_enabled_by = 'owner@example.com',"
                 " live_enabled_at = now() - interval '26 minutes'")
    set_setting(conn, "tz", "America/New_York")
    html = client.get("/settings").text
    live = _live_section(html)
    state = live.one("[data-live-state]")
    assert live.has_class("is-live") and state.attr("data-live-state") == "on" and state.text == "ON"
    since = re.search(r"Live trading is on since (\S+ \S+ \S+) by owner@example.com", live.text)
    assert since, live.text
    assert since.group(1).endswith(("EDT", "EST")), "the since time is shown in the owner's zone"
    off = live.action("live-off")
    assert off.target == "/settings/live/off" and "Disable live" in off.text and off.attr("data-confirm").startswith("Disable live trading now?")
    assert not live.has('[data-form="live-on"]') and not live.has('[name="confirm"]')
    assert live.chip("auth-ok").text == "ok" and re.search(r"checked [0-9] s ago", live.prop("auth"))
    assert live.prop("balance") == "$500.00" and live.prop("buying power") == "$480.00"
    assert live.prop("clock skew") == "120 ms" and "none since the last reset" in live.text
    assert mode_pill(page(html)) == "LIVE" and "live today $0.00" in topbar(page(html)).text
    # a failed probe after live went on: the failure, its count and the last error show in red
    auth_state(conn, auth_ok=False, auth_failures=2, last_auth_error="401 unauthorized <b>")
    live = _live_section(client.get("/settings").text)
    assert live.chip("auth-failed").text == "failed" and "2 failures in a row" in live.prop("auth")
    assert live.prop("last auth error") == "401 unauthorized <b>" and live.texts(".error")[-1] == "401 unauthorized <b>"


def test_live_enable_form_posts_the_phrase(client, conn):
    """The typed form enables live through host.trading.live.enable_live: redirect with a
    flash, the flag on, the live_on audit row with the confirmation text, the page on."""
    _ready(conn)
    r = client.post("/settings/live", data={"confirm": _phrase(conn)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#live"
    assert flash_cookie(r) == "Live trading enabled by dev. Exchange balance $500.00.", "the dev owner is the actor"
    assert _live_flag(conn) is True
    audit = conn.execute("SELECT actor, confirmation_text FROM audit_log WHERE action = 'live_on'").fetchall()
    assert [dict(a) for a in audit] == [{"actor": "dev", "confirmation_text": _phrase(conn)}]
    p = page(client.get("/settings").text)
    assert p.one("[data-live-state]").attr("data-live-state") == "on" and "by dev." in p.card("live").text and mode_pill(p) == "LIVE"
    assert mode_pill(page(client.get("/fragments/topbar").text)) == "LIVE" and mode_pill(page(client.get("/").text)) == "LIVE"


def test_live_enable_wrong_phrase_shows_the_inline_error(client, conn):
    """A wrong phrase re-renders the page (400) with the error inside the live group and
    the typed text kept; a right phrase with a failed precondition is a 409 naming it."""
    _ready(conn)
    for bad in ("ENABLE LIVE TRADING", _phrase(conn, -1), _phrase(conn, 1), _phrase(conn).lower(), "RESUME", ""):
        r = client.post("/settings/live", data={"confirm": bad}, follow_redirects=False)
        assert r.status_code == 400, bad
        live = _live_section(r.text)
        assert live.texts(".inline-error") == [f'confirmation must be exactly "{_phrase(conn)}"'], bad
        assert _value(live, "confirm") == bad, "the typed text is kept"
        assert live.form("live-on").target == "/settings/live" and _live_flag(conn) is False
        assert "flash" not in r.cookies
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'live_on'").fetchone()["n"] == 0
    assert _live_section(client.get("/settings").text).count(".inline-error") == 0, "a plain GET carries no error"
    auth_state(conn, credentials_present=False, auth_ok=False)
    r = client.post("/settings/live", data={"confirm": _phrase(conn)}, follow_redirects=False)
    assert r.status_code == 409 and _live_flag(conn) is False
    live = _live_section(r.text)
    assert "live trading cannot be enabled: the exchange process has no credentials loaded" in live.text
    client.post("/kill", follow_redirects=False)
    _ready(conn)
    r = client.post("/settings/live", data={"confirm": _phrase(conn)}, follow_redirects=False)
    assert r.status_code == 409 and "the kill switch is on; reset it first" in _live_section(r.text).text
    # a generic settings group never carries the switch
    r = client.post("/settings/trading", data={"max_bet": "25", "max_daily_loss_paper": "100", "max_daily_loss_live": "100",
                                                "default_bankroll": "100", "liquidity_floor": "100", "min_edge": "0.02",
                                                "kelly_fraction": "0.25", "trade_max_games": "4", "live_enabled": "true"},
                    follow_redirects=False)
    assert r.status_code == 303 and _live_flag(conn) is False


def test_live_disable_form_halts_live_assignments(client, conn):
    """The Disable button is immediate: live off, the live assignment halted, its open
    live order cancel_requested for the exchange, a live_off audit row, a flash."""
    setup = trade_setup(conn, mode="live", model_status="live_eligible")
    opened = _open_order(conn, setup)
    conn.execute("UPDATE orders SET exchange_order_id = 'pm-7f3a' WHERE id = %s", (opened["id"],))
    assert page(client.get("/settings").text).one("[data-live-state]").attr("data-live-state") == "on"
    r = client.post("/settings/live/off", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#live"
    assert flash_cookie(r) == "Live trading disabled: 1 live assignment halted, 1 orders cancel requested on the exchange."
    assert _live_flag(conn) is False
    assert assignment_row(conn, setup.assignment["id"])["status"] == "halted"
    assert order_row(conn, opened["id"])["status"] == "cancel_requested"
    audit = conn.execute("SELECT actor FROM audit_log WHERE action = 'live_off'").fetchall()
    assert [a["actor"] for a in audit] == ["dev"]
    p = page(client.get("/settings").text)
    assert p.one("[data-live-state]").attr("data-live-state") == "off" and mode_pill(p) == "PAPER"
    assert "Live trading is off" in p.card("live").text and p.form("live-on").target == "/settings/live"
    r = client.post("/settings/live/off", data={}, follow_redirects=False)
    assert r.status_code == 303 and "0 live assignments halted" in flash_cookie(r), "idempotent"


def test_topbar_pill_states(client, conn):
    """PAPER grey until live is on, LIVE green while it is, PAPER again after a kill."""
    from tests.test_style import declarations

    for path in ("/", "/trading", "/settings", "/jobs", "/fragments/topbar"):
        bar = topbar(page(client.get(path).text))
        assert mode_pill(bar) == "PAPER" and bar.one(".pill").has_class("paper") and "LIVE" not in bar.text, path
    set_setting(conn, "live_enabled", True)
    for path in ("/", "/trading", "/settings", "/jobs", "/fragments/topbar"):
        bar = topbar(page(client.get(path).text))
        assert mode_pill(bar) == "LIVE" and bar.one(".pill").has_class("live") and "PAPER" not in bar.text, path
        assert re.search(r"\blive\b[^$]*\$0\.00", bar.text), (path, bar.text)
    assert page(client.get("/settings").text).one("[data-live-state]").text == "ON"
    client.post("/kill", follow_redirects=False)
    bar = page(client.get("/fragments/topbar").text)
    assert mode_pill(bar) == "PAPER" and bar.has('[data-killed="1"]'), "a kill turns live off"
    assert "var(--green-fill)" in declarations(".pill.live")


def test_killed_bar_shows_the_auto_kill_reason(client, conn):
    """A kill pulled by the exchange process names its reason in the red bar and in the
    Settings kill section; a hand kill does not; a reset clears it."""
    def bar() -> Node:
        return page(client.get("/fragments/topbar").text)

    client.post("/kill", follow_redirects=False)
    hand = bar()
    assert "TRADING KILLED. Reset in Settings." in hand.text and not hand.has("[data-auto-kill]") and "automatically" not in hand.text
    assert "Killed automatically" not in page(client.get("/settings").text).card("kill").text
    client.post("/kill/reset", data={"confirm": "RESUME"}, follow_redirects=False)
    kill.auto_kill(conn, "auth_failures", {"failures": 3, "error": "401 <b>"})
    auto = bar()
    reason = auto.one("[data-auto-kill]")
    assert auto.has('[data-killed="1"]') and reason.attr("data-auto-kill") == "auth_failures"
    assert reason.text == "TRADING KILLED automatically: auth_failures. Reset in Settings." and reason.target == "/settings#kill"
    for path in ("/", "/trading"):
        assert "TRADING KILLED automatically" in topbar(page(client.get(path).text)).text, path
    p = page(client.get("/settings").text)
    assert p.one("#topbar").has_class("killed")
    section = p.card("kill")
    assert "Killed automatically by the exchange process: auth_failures at " in section.text
    assert "401 <b>" in section.text and '"failures": 3' in section.text and not section.has("b"), "the detail is escaped"
    assert p.card("live").texts('[data-chip="auto-kill"]') == ["auth_failures"]
    # a later auto-kill is the one the bar names; the group lists both
    kill.auto_kill(conn, "clock_skew", {"skew_ms": 48_000})
    later = bar()
    assert later.one("[data-auto-kill]").attr("data-auto-kill") == "clock_skew" and "auth_failures" not in later.text
    assert _live_section(client.get("/settings").text).texts('[data-chip="auto-kill"]') == ["auth_failures", "clock_skew"]
    client.post("/kill/reset", data={"confirm": "RESUME"}, follow_redirects=False)
    cleared = bar()
    assert "KILLED" not in cleared.text and not cleared.has("[data-auto-kill]")
    p = page(client.get("/settings").text)
    assert "Killed automatically" not in p.text and "none since the last reset" in p.card("live").text and not p.has('[data-chip="auto-kill"]')


def test_trading_exchange_box_live_rows_and_smoke_flag(client, conn):
    """The exchange box carries auth, balance, buying power and the open live orders;
    live assignments and orders get the tinted row; smoke orders are flagged."""
    setup = trade_setup(conn, mode="live", model_status="live_eligible")
    conn.execute("UPDATE exchange_state SET heartbeat_at = now() - interval '2 seconds', market_source = 'polymarket_us',"
                 " balance_cents = 123456, buying_power_cents = 100000, clock_skew_ms = 140, last_error = NULL")
    opened = _open_order(conn, setup)
    conn.execute("UPDATE orders SET exchange_order_id = 'pm-7f3a' WHERE id = %s", (opened["id"],))
    smoke = _smoke_order(conn, setup.market)
    paper = make_assignment(conn, GAME_ID)
    live = page(client.get("/trading").text).one("#trading-live")
    row = live.row("assignment", setup.assignment["id"])
    assert row.has_class("is-live") and row.chip("live").text == "live"
    assert not live.row("assignment", paper["id"]).has_class("is-live"), "a paper row stays plain"
    open_orders = live.card("open-orders")
    rows = open_orders.rows("order")
    assert len(rows) == 2 and all(r.has_class("is-live") for r in rows) and [r.chip("live").text for r in rows] == ["live", "live"]
    smoke_row = open_orders.row("order", smoke["id"])
    assert smoke_row.attr("data-kind") == "smoke" and smoke_row.has_class("is-live")
    assert open_orders.count('[data-chip="smoke"]') == 1 and "#pm-7f3a" in open_orders.text and "#pm-smoke-1" in open_orders.text
    assert smoke_row.action("cancel").target == f"/orders/{smoke['id']}/cancel", "a smoke order can be cancelled by hand"
    assert "1 @ 0.45" in smoke_row.text and "$0.45" in smoke_row.text
    recent = live.card("orders")
    assert recent.row("order", smoke["id"]).attr("data-kind") == "smoke" and recent.row("order", smoke["id"]).has_class("is-live")
    assert recent.count('[data-chip="smoke"]') == 1 and "smoke order" in recent.text
    exchange = live.card("exchange")
    assert exchange.chip("exchange-up").text == "up" and exchange.prop("source") == "polymarket_us"
    auth = exchange.prop("auth")
    assert exchange.chip("auth-ok").text == "ok" and "checked 0 s ago" in auth and "credentials yes" in auth and "skew 140 ms" in auth
    assert "$1,234.56" in exchange.text and "buying power $1,000.00" in exchange.text
    assert exchange.prop("live orders").startswith("2 open") and exchange.chip("smoke").text == "1 smoke"
    assert "last auth error" not in exchange.text
    sections = {"assignments", "positions", "open-orders", "orders", "fills", "unmatched", "markets", "exchange", "ledger"}
    fragment = page(client.get("/fragments/trading").text)
    assert sections <= set(live.cards()) and fragment.cards() == live.cards(), "the fragment carries every trading card"
    # auth failing: the chip turns, the error shows; a cancelled smoke order leaves the count
    auth_state(conn, auth_ok=False, auth_failures=2, last_auth_error="401 unauthorized <i>")
    conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (smoke["id"],))
    frag = page(client.get("/fragments/trading").text)
    exchange = frag.card("exchange")
    assert exchange.chip("auth-failed").text == "failed" and "401 unauthorized <i>" in exchange.text and not exchange.has("i")
    assert exchange.prop("live orders") == "1 open" and not exchange.has('[data-chip="smoke"]')
    assert "smoke" not in [r.attr("data-kind") for r in frag.card("open-orders").rows("order")]
    assert frag.card("orders").has('[data-chip="smoke"]'), "still flagged in the recent list"
    assert "No live orders" not in frag.text


def test_live_phone_layout_and_colour_rules(client, conn):
    """The live form stacks at phone width with 44 px buttons, the tinted live row keeps
    AA contrast in both schemes, the Settings tables stack."""
    from tests.test_style import _schemes, contrast, declarations

    assert "var(--amber-fill)" in declarations(".chip-smoke")
    assert "var(--live-bg)" in declarations(".is-live")
    light, dark = _schemes()
    assert light["live-bg"] != dark["live-bg"], "the live tint has a dark-scheme value"
    for sel in (".live-form .btn", ".live-off .btn"):
        assert "var(--tap)" in declarations(sel) + declarations(".btn"), sel
        assert "100%" in declarations(sel, media="max-width"), sel
    assert "100%" in declarations(".live-form label", media="max-width")
    for name, tokens in zip(("light", "dark"), (light, dark)):
        for fg in ("text", "muted", "accent", "red-fg"):
            assert contrast(tokens[fg], tokens["live-bg"]) >= 4.5, (name, fg)
    enable_live(conn)
    for state in ("on", "off"):
        p = page(client.get("/settings").text)
        assert "width=device-width" in p.one('meta[name="viewport"]').attr("content")
        assert all(t.has_class("stack") for t in p.select("table")), "the Settings tables stack"
        live = p.card("live")
        if state == "on":
            off = live.action("live-off")
            assert off.tag == "form" and off.attr("method") == "post" and off.target == "/settings/live/off" and off.has_class("live-off")
            set_setting(conn, "live_enabled", False)
        else:
            form = live.form("live-on")
            assert form.tag == "form" and form.attr("method") == "post" and form.target == "/settings/live" and form.has_class("live-form")
            box = form.input("confirm")
            assert box.attr("autocapitalize") == "characters" and box.attr("spellcheck") == "false", "a phone keyboard must not mangle the phrase"
    for path in ("/settings", "/trading"):
        assert chr(0x2014) not in client.get(path).text


# ------------------------------------------------------------------ step 6: robustness on the dashboard


def test_model_page_robustness_section_renders_every_element(client, conn, make_worker):
    """The Robustness section: flags with their meanings, the CI line, the market test
    sentence, calibration slope and intercept, the price stress table, the
    neighbourhood summary and the regime table; the validate job page shows the same."""
    validation = validation_metrics(n_bets=130, roi=0.041, ci_roi=(-0.012, 0.094), market_p=0.012, mean_ll_gain=0.0021, flags=["overfit"],
                                    per_season=[{"season": 2022, "n_games": 270, "n_bets": 30, "roi": 0.05, "pnl_cents": 1800, "log_loss": 0.65, "market_log_loss": 0.655, "max_drawdown": 0.04}])
    stress = stress_metrics(flags=["regime_dependent"], seed=9)
    model = insert_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, metrics=backtest_metrics(), validation=validation, stress=stress)
    html = client.get(f"/models/{model['id']}").text
    p = page(html)
    rob = p.card("robustness")
    overfit = rob.chip("overfit")
    assert overfit.text == "overfit" and overfit.has_class("chip-overfit") and overfit.closest("[title]").attr("title").startswith("the search era looked better")
    assert rob.chip("regime_dependent").has_class("chip-regime_dependent") and rob.chip("regime_dependent").text == "regime-dependent"
    assert "overfit: the search era looked better than the held-out era" in rob.text
    assert "regime-dependent: in one regime pair" in rob.text
    assert "Validation ROI +4.1% (90% range -1.2% to +9.4%) over 130 bets, shrunk +2.32%." in rob.text
    assert "(90% range -1.2% to +9.4%)" in rob.texts(".range")
    assert "Hit rate 52.0% (49.0% to 56.0%)" in rob.text and "average edge +3.4%" in rob.text
    assert "CLV range 0.000 to 0.000" in rob.text
    market = rob.one(".market-line")
    assert market.has_class("beats") and market.text == "Beats the market on log-loss: mean gain +0.0021 per game, p = 0.012 (sign-flip test, 10 000 flips; beaten means p < 0.05)."
    assert rob.chip("beats").text == "beats market"
    assert rob.prop("calibration slope").startswith("0.970") and rob.prop("calibration intercept").startswith("-0.020")
    assert (rob.prop("reliability"), rob.prop("resolution"), rob.prop("uncertainty")) == ("0.0021", "0.0146", "0.2487")
    assert rob.listing("stress").has_class("stack") and rob.row_ids("stress") == ["base", "spread+0.01", "spread+0.02", "fee x1.5"]
    first = rob.row("stress", "spread+0.01")
    assert "bets 96" in first.text and "ROI +2.5%" in first.text and "gain 0.0020" in first.text
    assert "10 perturbations (every numeric parameter scaled by 0.9 to 1.1): shrunk ROI median +1.90%, 10th percentile +0.40%; log-loss gain median 0.0018, 10th percentile 0.0007." in rob.text
    assert rob.listing("regimes").has_class("stack")
    regimes = rob.row_ids("regime")
    for label in ("favourite", "underdog", "home", "away", "divisional", "non-divisional", "primetime", "day", "cold or windy", "other weather"):
        assert label in regimes, label
    assert regimes.index("favourite") < regimes.index("underdog") < regimes.index("home")
    assert "validation per season" in p.texts("h3") and p.has('[data-row="season"][data-id="2022"]')
    assert "Stress seed 9; bootstrap B = 1000, 10 000 sign flips." in rob.text
    assert rob.start < p.card("backtest").start, "the validation era leads"
    assert [t.attr("class") for t in p.select("table") if not t.has_class("stack") and not t.has_class("calibration")] == []
    assert chr(0x2014) not in html
    # No flags, market not beaten: the honest sentence and a "no flags" chip.
    plain = insert_model(conn, params={"k": 21.0}, validation=validation_metrics(market_p=0.4), stress=stress_metrics())
    rob = page(client.get(f"/models/{plain['id']}").text).card("robustness")
    assert rob.chip("no-flags").text == "no flags" and "Does not beat the market on log-loss" in rob.text and not rob.has('[data-chip="beats"]')
    # A validate job renders its result the same way, and the kind is listed with the model.
    w = make_worker("box1", role="backtest")
    job = client.post("/api/jobs", json={"kind": "validate", "params": {"model_id": str(model["id"])}, "target": w.id}).json()
    conn.execute("UPDATE jobs SET status = 'succeeded', result = %s, checkpoint = %s WHERE id = %s",
                 (__import__("psycopg").types.json.Jsonb({"validation_metrics": validation, "stress_metrics": stress}), __import__("psycopg").types.json.Jsonb({"stage": "regimes"}), job["id"]))
    jp = page(client.get(f"/jobs/{job['id']}").text)
    assert "spread+0.02" in jp.card("robustness").row_ids("stress") and "stage regimes" in jp.text
    assert f"/models/{model['id']}" in jp.hrefs and "raw result" in jp.text


def test_leaderboard_shows_the_paper_clv_interval(client, conn):
    model = insert_validated_model(conn, status="paper_ok", params={"k": 22.0})
    for i, clv in enumerate((0.03, 0.02, 0.04, 0.03)):
        insert_game(conn, f"2026_0{i + 1}_C_D", kickoff_in_s=-(i + 1) * 86400)
        insert_paper_bet(conn, model, f"2026_0{i + 1}_C_D", clv, pnl_cents=50)
        _score(conn, model, f"2026_0{i + 1}_C_D", 1, 50, 1000, clv)
    from host import eligibility

    eligibility.recompute_paper(conn, model["lineage_id"])
    row = client.get("/api/models").json()["ranked"][0]
    assert row["paper_ci"]["n_bets"] == 4 and row["paper_ci"]["ci"][0] > 0 and row["paper_ci"]["avg_clv"] == pytest.approx(0.03)
    board = page(client.get("/models").text)
    assert re.search(r"range \+\d\.\d% to \+\d\.\d%", board.row("model", row["id"]).one(".row-meta").text), "the 90% CLV range next to the paper record"
    detail = page(client.get(f"/models/{model['id']}").text)
    assert re.search(r"CLV 90% range 0\.0\d\d to 0\.0\d\d over 4 bets", detail.text)


def test_settings_step6_groups_round_trip(client, conn):
    """The thresholds group with the gate fields and the seasons group with the
    validation era and the search pool; an overlapping era is an inline error."""
    p = page(client.get("/settings").text)
    form = p.form("thresholds")
    assert (_value(form, "min_bets"), _value(form, "min_roi_ci_low"), _value(form, "max_market_p")) == ("50", "0.0", "0.1")
    checked = {name: form.input(name).has_attr("checked") for name in ("require_validation", "forbid_overfit", "forbid_fragile", "forbid_regime_dependent")}
    assert checked == {"require_validation": True, "forbid_overfit": True, "forbid_fragile": True, "forbid_regime_dependent": False}
    assert all(form.input(name).attr("value") == "true" for name in checked)
    assert "judged on the validation era" in p.card("thresholds").text
    assert "the bootstrap lower bound" in form.field("min_roi_ci_low").text and "0.05 = beats the market" in form.field("max_market_p").text
    seasons = p.form("seasons")
    assert (_value(seasons, "seasons_first"), _value(seasons, "seasons_last")) == ("2010", "2021")
    assert (_value(seasons, "validation_first"), _value(seasons, "validation_last"), _value(seasons, "search_workers")) == ("2022", "", "auto")
    assert "blank = the season before the validation era" in seasons.field("seasons_last").text and "auto = cores minus one" in seasons.field("search_workers").text
    model = insert_validated_model(conn, validation=validation_metrics(n_bets=60, roi=0.03, ci_roi=(-0.01, 0.07), market_p=0.08), stress=stress_metrics(flags=["regime_dependent"]))
    assert model_row(conn, model["id"])["status"] == "candidate"
    good = {"min_bets": "60", "min_roi": "0.02", "max_drawdown": "0.3", "min_roi_ci_low": "-0.01", "max_market_p": "0.08", "require_validation": "true", "forbid_overfit": "true", "forbid_fragile": "true"}
    r = client.post("/settings/thresholds", data=good, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "thresholds settings saved"
    s = client.get("/api/settings").json()["thresholds_backtest"]
    assert s == {"min_bets": 60, "min_roi": 0.02, "max_drawdown": 0.3, "require_validation": True, "min_roi_ci_low": -0.01, "max_market_p": 0.08, "forbid_flags": ["overfit", "fragile"]}
    assert model_row(conn, model["id"])["status"] == "paper_ok", "saving recomputes every lineage on the new rules"
    r = client.post("/settings/thresholds", data={**good, "forbid_regime_dependent": "true"}, follow_redirects=False)
    assert r.status_code == 303 and model_row(conn, model["id"])["status"] == "candidate"
    for bad, message in [
        ({**good, "min_roi_ci_low": "2"}, "min_roi_ci_low must be between -1 and 1"),
        ({**good, "max_market_p": "x"}, "Max market p must be a number"),
        ({**good, "min_bets": "1.5"}, "Min bets must be a whole number"),
    ]:
        r = client.post("/settings/thresholds", data=bad, follow_redirects=False)
        assert r.status_code == 400 and message in r.text, (bad, message)
    r = client.post("/settings/thresholds", data={k: v for k, v in good.items() if k not in ("require_validation", "forbid_overfit", "forbid_fragile")}, follow_redirects=False)
    assert r.status_code == 303
    s = client.get("/api/settings").json()["thresholds_backtest"]
    assert s["require_validation"] is False and s["forbid_flags"] == [], "unticked boxes store false and an empty list"
    unticked = page(client.get("/settings").text).input("require_validation")
    assert unticked.attr("value") == "true" and not unticked.has_attr("checked")
    # Seasons: the validation era must start after the search era; a blank search last season is allowed.
    r = client.post("/settings/seasons", data={"seasons_first": "2012", "seasons_last": "2022", "validation_first": "2022", "validation_last": "", "search_workers": "4"}, follow_redirects=False)
    assert r.status_code == 400 and "validation_seasons must start after the search era ends (2022)" in r.text
    assert _value(page(r.text), "search_workers") == "4", "submitted values are kept"
    r = client.post("/settings/seasons", data={"seasons_first": "2012", "seasons_last": "", "validation_first": "2023", "validation_last": "2025", "search_workers": "4"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "seasons settings saved"
    s = client.get("/api/settings").json()
    assert s["backtest_seasons"] == [2012, None] and s["validation_seasons"] == [2023, 2025] and s["search_workers"] == 4
    r = client.post("/settings/seasons", data={"seasons_first": "2012", "seasons_last": "", "validation_first": "2023", "validation_last": "", "search_workers": "lots"}, follow_redirects=False)
    assert r.status_code == 400 and "Search workers must be auto or a whole number" in r.text
    r = client.post("/settings/seasons", data={"seasons_first": "2012", "seasons_last": "", "validation_first": "2023", "validation_last": "", "search_workers": "0"}, follow_redirects=False)
    assert r.status_code == 400 and 'search_workers must be "auto" or an integer between 1 and 64' in r.text.replace("&#34;", '"')
    r = client.post("/settings/seasons", data={"seasons_first": "2012", "seasons_last": "", "validation_first": "2023", "validation_last": "", "search_workers": ""}, follow_redirects=False)
    assert r.status_code == 303 and client.get("/api/settings").json()["search_workers"] == "auto", "blank means auto"
    ingest_fixture(conn)
    assert client.get("/jobs").text.count("held-out validation era (2023-2025)") == 1
