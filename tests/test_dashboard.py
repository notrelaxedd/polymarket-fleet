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



def test_every_page_renders(client, make_worker):
    w = make_worker("box1")
    job = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 3}}).json()
    for path, needle in [
        ("/", 'id="fleet-grid"'),
        ("/jobs", 'action="/jobs"'),
        (f"/jobs/{job['id']}", "events"),
        ("/settings", 'action="/settings/trading"'),
        ("/kill/confirm", 'action="/kill"'),
        ("/trading", 'id="trading-live"'),
    ]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("text/html")
        html = r.text
        assert needle in html, path
        assert "<!doctype html>" in html.lower()
        assert 'href="/static/style.css"' in html and 'src="/static/app.js"' in html
        assert 'id="topbar-status"' in html and ">PAPER<" in html and "today $0.00" in html
        assert 'id="kill-form"' in html and 'id="updated"' in html
        assert ">Models<" in html and '<a href="/trading"' in html and 'href="/settings"' in html
        assert "step 4" not in html
    assert w.id in client.get("/").text


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
    html = client.get("/").text
    cards = re.findall(r'<article class="card worker[^"]*" data-worker="(\w+)">', html)
    assert cards == [online.id, stale.id, offline.id, switching.id], "one card per worker, sorted by name"
    assert html.count("<article") == 4
    assert f'<span class="dot online"' in html.split(stale.id)[0]
    assert re.search(rf'data-worker="{stale.id}">.*?<span class="dot stale"', html, re.S)
    assert re.search(rf'data-worker="{offline.id}">.*?<span class="dot offline"', html, re.S)
    assert re.search(rf'data-worker="{offline.id}">.*?<span class="chip">disabled</span>', html, re.S)
    assert f'action="/workers/{online.id}/role"' in html
    assert re.search(rf'id="role-{online.id}" name="role" data-autosubmit="1">', html)
    assert re.search(rf'<option value="backtest" selected>backtest</option>', html)
    assert re.search(rf'id="role-{switching.id}" name="role" data-autosubmit="1" disabled>', html)
    assert "switching to train (epoch 2)" in html
    # MEDIUM: status in text too, not only dot colour; the dot is announced
    assert re.search(rf'data-worker="{stale.id}">.*?<span class="dot stale" role="img" aria-label="stale"></span>.*?<span class="chip st-stale">stale</span>', html, re.S)
    assert re.search(rf'<article class="card worker is-disabled is-offline" data-worker="{offline.id}">.*?<span class="chip st-offline">offline</span>', html, re.S)
    assert 'class="chip st-online"' not in html
    assert re.search(r'<article class="card worker" data-worker="\w+">.*?<span class="dot online" role="img" aria-label="online"></span>', html, re.S)
    # MEDIUM: an offline worker's select stays enabled while it is "switching" so a wrong pick can be undone
    gone = make_worker("echo", online=False)
    client.post(f"/api/workers/{gone.id}/role", json={"role": "trade"})
    card = re.search(rf'data-worker="{gone.id}">.*?</article>', client.get("/").text, re.S).group(0)
    assert "switching to trade (epoch 2)" in card and " disabled" not in card
    assert f'id="role-{gone.id}" name="role" data-autosubmit="1">' in card
    assert 'aria-valuemin="0" aria-valuemax="100" aria-valuenow="40"' in html
    assert f'href="/jobs/{job["id"]}">sleep</a>' in html and 'style="width: 40%"' in html and ">40%<" in html
    assert "no job" in html
    assert 'CPU 2% &middot; RAM 0.5 / 4.0 GB &middot; 0 s ago &middot; <span class="nowrap">v test</span>' in html
    assert 'value="false">' in html and ">Disable<" in html and 'value="true">' in html and ">Enable<" in html
    assert "today $0.00" in html
    client.get("/")  # the empty state
    conn.execute("DELETE FROM jobs")
    conn.execute("DELETE FROM workers")
    assert "No workers yet. Mint an enroll token" in client.get("/").text


def test_role_form_flips_role_and_redirects_with_flash(client, conn, make_worker):
    w = make_worker("box1")
    r = client.post(f"/workers/{w.id}/role", data={"role": "backtest"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/" and flash_cookie(r) == "box1: switching to backtest (epoch 2)"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    row = worker_row(conn, w.id)
    assert row["desired_role"] == "backtest" and row["role_epoch"] == 2 and row["auto_role"] is False
    page = client.get(r.headers["location"]).text
    assert '<div class="flash" role="status">box1: switching to backtest (epoch 2)</div>' in page
    assert '<div class="flash"' not in client.get("/").text, "a flash is shown once, not on every reload"
    r = client.post(f"/workers/{w.id}/role", data={"role": "chef"}, follow_redirects=False)
    assert r.status_code == 400 and "text/html" in r.headers["content-type"] and "unknown role" in r.text
    assert worker_row(conn, w.id)["role_epoch"] == 2
    assert client.post("/workers/w_nope/role", data={"role": "idle"}, follow_redirects=False).status_code == 404
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'set_role'").fetchone()["n"] == 1


def test_enabled_form(client, conn, make_worker):
    w = make_worker("box1")
    r = client.post(f"/workers/{w.id}/enabled", data={"enabled": "false"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and flash_cookie(r) == "box1 disabled"
    assert worker_row(conn, w.id)["enabled"] is False
    r = client.post(f"/workers/{w.id}/enabled", data={"enabled": "true"}, follow_redirects=False)
    assert r.headers["location"] == "/" and flash_cookie(r) == "box1 enabled"
    assert worker_row(conn, w.id)["enabled"] is True


def test_send_job_form_and_cancel(client, conn, make_worker):
    w = make_worker("box1")
    html = client.get("/jobs").text
    assert '<option value="any_idle">Any idle worker</option>' in html
    assert f'<option value="{w.id}">box1</option>' in html
    assert 'id="backtest"' in html and 'id="model_search"' in html and 'id="train"' in html and 'id="sleep"' in html
    assert 'name="seconds" value="60"' in html
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
    html = client.get("/jobs").text
    assert html.count("<tr>") == 4 and "waiting for an idle worker" in html
    assert html.count(f'action="/jobs/{waiting["id"]}/cancel"') == 1
    assert html.index(str(waiting["id"])) < html.index(str(first["id"])), "newest first"
    assert client.post("/jobs", data={"kind": "sleep", "seconds": "x"}, follow_redirects=False).status_code == 400
    assert client.post("/jobs", data={"kind": "mystery"}, follow_redirects=False).status_code == 400
    assert client.post("/jobs", data={"kind": "sleep", "target": "w_nope"}, follow_redirects=False).status_code == 404
    r = client.post(f"/jobs/{waiting['id']}/cancel", data={"next": "/jobs/" + str(waiting["id"])}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{waiting['id']}" and flash_cookie(r).startswith("job ")
    assert job_row(conn, waiting["id"])["status"] == "cancelled"
    r = client.post(f"/jobs/{first['id']}/cancel", data={"next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/jobs"
    detail = client.get(f"/jobs/{first['id']}").text
    assert ">cancelled<" in detail and "&#34;seconds&#34;: 7" in detail and ">created<" in detail
    assert client.get("/jobs/not-a-job").status_code == 404
    assert "<html" in client.get("/jobs/not-a-job").text


def test_settings_form_converts_rejects_and_audits(client, conn):
    html = client.get("/settings").text
    assert 'name="max_bet" value="25.00"' in html and 'name="max_daily_loss_paper" value="1000.00"' in html
    assert 'name="lease_seconds" value="30"' in html and 'name="tz" value="America/New_York"' in html
    assert 'name="max_expiries" value="3"' in html
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
    html = client.get("/settings").text
    assert 'name="max_bet" value="12.50"' in html and 'name="max_daily_loss_live" value="300.01"' in html
    for bad, message in [
        ({**good, "max_bet": "lots"}, "Max bet must be a dollar amount"),
        ({**good, "max_bet": "-1"}, "max_bet_cents must be between 0 and 100000000000"),
        ({**good, "min_edge": "2"}, "min_edge must be between 0 and 1"),
        ({**good, "trade_max_games": "1.5"}, "must be a whole number"),
    ]:
        r = client.post("/settings/trading", data=bad, follow_redirects=False)
        assert r.status_code == 400, bad
        assert f'<p class="error inline-error">{message}' in r.text or message in r.text
        assert 'name="max_bet" value="%s"' % bad["max_bet"] in r.text, "submitted values are kept"
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
    token = re.search(r'<code id="token" class="token">([^<]+)</code>', r.text).group(1)
    assert len(token) > 30
    assert f"curl -fsSL http://127.0.0.1:8080/install.sh | sudo bash -s -- http://127.0.0.1:8080 {token}" in r.text
    assert f"curl -fsSL http://127.0.0.1:8080/install.sh | sudo FLEET_ENROLL_TOKEN={token} bash -s -- http://127.0.0.1:8080" in r.text
    assert 'data-copy="token"' in r.text
    assert conn.execute("SELECT count(*) AS n FROM enroll_tokens").fetchone()["n"] == 1
    assert token not in client.get("/settings").text
    reg = client.post("/api/v1/workers/register", json={"enroll_token": token, "hostname": "box9"})
    assert reg.status_code == 200


def test_fragments_return_inner_html_only(client, make_worker):
    w = make_worker("box1")
    fleet = client.get("/fragments/fleet").text
    assert "<html" not in fleet and "<article" in fleet and w.id in fleet and 'id="fleet-grid"' not in fleet
    topbar = client.get("/fragments/topbar").text
    assert "<html" not in topbar and ">PAPER<" in topbar and 'data-kill="1"' in topbar
    assert 'id="topbar-status"' not in topbar


def test_names_and_params_are_escaped(client, conn, make_worker):
    w = make_worker("evil<script>alert(1)</script>")
    job = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 1, "note": "<script>x</script>"},
                                         "target": w.id}).json()
    for path in ("/", "/fragments/fleet", "/jobs", f"/jobs/{job['id']}", "/settings"):
        html = client.get(path).text
        assert "<script>" not in html.replace('<script src="/static/app.js" defer></script>', ""), path
    assert "evil&lt;script&gt;alert(1)&lt;/script&gt;" in client.get("/").text
    assert "&lt;script&gt;x&lt;/script&gt;" in client.get(f"/jobs/{job['id']}").text
    # The flash travels in a cookie set by the redirect: a crafted link cannot inject one.
    flash = client.get("/?flash=<img src=x onerror=alert(1)>").text
    assert "<img" not in flash and 'class="flash"' not in flash
    r = client.post(f"/workers/{w.id}/role", data={"role": "backtest"}, follow_redirects=False)
    page = client.get(r.headers["location"]).text
    assert '<div class="flash" role="status">evil&lt;script&gt;alert(1)&lt;/script&gt;: switching to backtest' in page


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
    assert re.search(r'<td class="nowrap lead">\d{4}-\d\d-\d\d \d\d:\d\d:\d\d JST</td>', client.get("/settings").text)
    r = client.post("/enroll-token")
    assert re.search(r"expires \d{4}-\d\d-\d\d \d\d:\d\d:\d\d JST\.", r.text)
    conn.execute("""UPDATE settings SET value = '"Mars/Olympus"' WHERE key = 'tz'""")
    assert created.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") in client.get("/jobs").text, "unknown zone: UTC"


def test_settings_inputs_open_the_number_keyboard(client):
    """MEDIUM: every numeric field carries an inputmode; the tz field does not."""
    html = client.get("/settings").text
    decimal = ("max_bet", "max_daily_loss_paper", "max_daily_loss_live", "default_bankroll", "liquidity_floor", "min_edge", "kelly_fraction",
               "taker_rate", "half_spread", "min_roi", "max_drawdown", "min_roi_ci_low", "max_market_p", "participation",
               "max_exposure_paper", "max_exposure_live", "paper_min_clv", "paper_min_pnl", "orders_per_s", "cancels_per_s",
               "market_data_per_s", "account_per_s")
    numeric = ("trade_max_games", "lease_seconds", "heartbeat_seconds", "online_after_seconds", "max_expiries",
               "min_bets", "seasons_first", "seasons_last", "validation_first", "validation_last", "nflverse_refresh_hours",
               "book_max_age_s", "gtd_seconds", "orphan_cancel_after_s", "trade_tick_s", "max_paper_models_per_game",
               "market_lookahead_days", "snapshot_active_s", "snapshot_idle_s", "snapshot_retention_days", "paper_min_games",
               "paper_min_bets", "paper_min_days")
    for name in decimal:
        assert re.search(rf'<input type="text" name="{name}" value="[^"]*" inputmode="decimal"', html), name
    for name in numeric:
        assert re.search(rf'<input type="text" name="{name}" value="[^"]*" inputmode="numeric"', html), name
    assert re.search(r'<input type="text" name="tz" value="[^"]*" autocomplete="off">', html)
    assert re.search(r'<input type="text" name="search_workers" value="auto" autocomplete="off">', html), "auto or a number: no number keyboard"
    assert re.search(r'<input type="text" name="nflverse_url" value="https://[^"]*" autocomplete="off">', html)
    assert re.search(r'<input type="text" name="scores_url" value="https://[^"]*" autocomplete="off">', html)
    assert html.count("inputmode=") == len(decimal) + len(numeric)


def test_fleet_timing_is_checked_across_fields(client):
    """MEDIUM: a lease shorter than two heartbeats (plus the HTTP timeout) or an
    online window shorter than a heartbeat is refused by the form and the API."""
    data = {"lease_seconds": "30", "heartbeat_seconds": "60", "online_after_seconds": "15", "max_expiries": "3"}
    r = client.post("/settings/fleet", data=data, follow_redirects=False)
    assert r.status_code == 400
    assert "lease_seconds must be at least 125 (2 x heartbeat_seconds + 5)" in r.text
    assert "online_after_seconds must be greater than heartbeat_seconds (60)" in r.text
    assert 'name="heartbeat_seconds" value="60"' in r.text, "submitted values are kept"
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
        assert "Max bet" in r.text and f'name="max_bet" value="{value}"' in r.text, value
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
        assert "404 Not found" in r.text and 'href="/"' in r.text, path
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


def test_models_page_renders_ranked_rows_and_attribution(client, conn):
    html = client.get("/models").text
    assert "No models yet. Send a model search" in html and "CC BY 4.0" in html and "nflverse" in html
    assert 'id="unranked"' not in html and 'id="ranked"' not in html and "<table" not in html
    only_unranked = insert_model(conn, params={"k": 19.0}, metrics=backtest_metrics(n_bets=20, roi=0.5))
    html = client.get("/models").text
    assert "No lineage is validated yet, so none is ranked." in html and "No models yet" not in html
    assert 'id="ranked"' not in html and 'id="unranked"' in html, "no empty ranked header above the unranked table"
    assert '<span class="chip chip-unvalidated" title="no validation-era metrics yet: send a validate job">not validated</span>' in html
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
    rows = re.findall(r'<tr class="model-row" data-model="([^"]+)">', html)
    assert rows == [str(best["id"]), str(second["id"]), str(none["id"]), str(few["id"])], "ranked first, then unranked newest first"
    ranked = html.split('id="unranked"')[0]
    assert str(few["id"]) not in ranked and 'id="unranked"' in html
    assert "#1" in ranked and "#2" in ranked
    assert '<span class="badge st-paper_ok">paper ok</span>' in ranked and '<span class="badge st-candidate">candidate</span>' in html
    assert "K 20 · HFA 50 · MOV on" in ranked and "K 30 · HFA 60 · MOV off" in ranked
    first = re.search(rf'<tr class="model-row" data-model="{best["id"]}">.*?</tr>', html, re.S).group(0)
    assert '<span class="k">validation ROI</span> +4.1% <span class="range">-1.2% to +9.4%</span>' in first, "the validation ROI with its 90% range"
    assert '<span class="k">beats market</span> <span class="chip chip-beats">yes</span> <span class="muted small">p 0.012</span>' in first
    assert '<span class="k">bets</span> 130 <span class="muted small">search 400</span>' in first and '<span class="k">search ROI</span> +5.0%' in first
    assert "0.651" in first and "vs 0.658" in first and '<span class="k">drawdown</span> 9.0%' in first and "chip-flag" not in first
    assert "Best lineage." in first and '<span class="chip">2 rows</span>' in first and "not validated" not in first
    row2 = re.search(rf'<tr class="model-row" data-model="{second["id"]}">.*?</tr>', html, re.S).group(0)
    assert '<span class="chip chip-flag chip-overfit"' in row2 and '>overfit</span>' in row2 and '>fragile</span>' in row2 and '>regime-dependent</span>' in row2
    assert '<span class="k">beats market</span> no <span class="muted small">p 0.310</span>' in row2
    unranked = html.split('id="unranked"')[1]
    assert unranked.count("not validated</span>") == 2 and "<th>validation ROI" in html and "<th>beats market</th>" in html
    assert '<span class="k">validation ROI</span> -' in unranked and '<span class="k">beats market</span> -' in unranked
    assert '<span class="k">bets</span> - <span class="muted small">search 20</span>' in unranked
    assert "not validated, or retired" in unranked
    assert f'href="/jobs?train_model={best["id"]}#train"' in ranked and ">Train</a>" in ranked
    assert f'<a class="btn small" href="/jobs?validate_model={best["id"]}#validate">Validate</a>' in ranked
    assert f'<a class="btn small" href="/trading?model={best["id"]}#assign">Assign</a>' in ranked
    assert f'action="/models/{best["id"]}/summary"' in ranked and 'maxlength="600"' in ranked
    assert "&lt;b&gt;bold&lt;/b&gt;" in html and "<b>bold</b>" not in html
    assert "No summary yet." in html
    assert 'class="attribution' in html and "creativecommons.org/licenses/by/4.0" in html
    assert '<a href="/models" class="active">Models</a>' in html and "step 3" not in html


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
    html = client.get(f"/models/{root['id']}").text
    assert "<h1>elo_blend" in html and "K 20 · HFA 50 · MOV on" in html and 'badge st-paper_ok' in html
    assert str(root["lineage_id"]) in html and '<span class="chip">root</span>' in html and "Root summary." in html
    assert "&#34;hfa&#34;: 50.0" in html, "params are shown as JSON"
    assert ">400<" in html and "+5.0%" in html and "2016-2018" in html
    assert "per season" in html and ">2016<" in html and ">2017<" in html and "-1.0%" in html and "-$7.00" in html
    assert "calibration" in html and "0.0-0.1" in html and "0.9-1.0" in html
    assert f'href="/models/{child["id"]}"' in html and "2024 week 10" in html and f'href="/jobs/{job["id"]}"' in html
    assert f'href="/jobs/{bt["id"]}"' in html and "ran against it" in html and "created this model" not in html
    assert f'action="/models/{root["id"]}/retire"' in html and 'data-confirm="Retire this whole lineage?' in html
    assert f'href="/jobs?train_model={root["id"]}#train"' in html and f'action="/models/{root["id"]}/summary"' in html
    assert '<form method="post" action="/jobs" class="inline validate-form">' in html and f'<input type="hidden" name="model_id" value="{root["id"]}">' in html
    assert '<input type="hidden" name="kind" value="validate">' in html and ">Validate</button>" in html
    assert '<h2>Robustness <span class="muted small">validation era 2022-2025, held out of the search</span></h2>' in html
    assert "<dt>validation shrunk ROI</dt><dd>+2.18%" in html and "<dt>search shrunk ROI</dt><dd>+4.00%" in html
    assert '<h2>backtest <span class="muted small">search era</span></h2>' in html
    child_html = client.get(f"/models/{child['id']}").text
    assert f'href="/models/{root["id"]}">{str(root["id"])[:8]}</a>' in child_html and "(this)" in child_html
    assert "No backtest metrics yet" not in child_html, "a child shows the lineage metrics"
    assert "created this model" in child_html and f'href="/jobs/{job["id"]}"' in child_html
    empty = insert_model(conn, params={"k": 40.0})
    assert "No backtest metrics yet" in client.get(f"/models/{empty['id']}").text
    assert client.get("/models/not-a-model").status_code == 404
    # The retire form retires the lineage and redirects back with a flash.
    r = client.post(f"/models/{child['id']}/retire", data={"next": f"/models/{child['id']}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/models/{child['id']}" and "retired" in flash_cookie(r)
    assert model_row(conn, root["id"])["status"] == "retired"
    assert "retire" not in client.get(f"/models/{root['id']}").text.split("<h2>summary</h2>")[0].split("</h1>")[1].lower().replace("retired", "")


def test_summary_edit_form(client, conn):
    root = insert_model(conn, summary="old")
    r = client.post(f"/models/{root['id']}/summary", data={"summary": "A new summary.", "next": "/models"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/models" and flash_cookie(r) == f"summary of {str(root['id'])[:8]} saved"
    assert model_row(conn, root["id"])["summary"] == "A new summary."
    assert "A new summary." in client.get("/models").text
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
    html = client.get("/jobs").text
    assert html.count('action="/jobs"') == 5 and html.count('<option value="any_idle">Any idle worker</option>') == 5
    assert 'name="seasons_first" value="2010"' in html and 'name="seasons_last" value="2021"' in html
    assert 'name="n" value="200"' in html and 'name="top_k" value="5"' in html and 'name="through_season" value="2025"' in html
    assert f'<option value="{model["id"]}">K 20 · HFA 50 · MOV on · untrained · {str(model["id"])[:8]}</option>' in html
    assert '<option value="elo_blend" selected>elo_blend</option>' in html
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
    html = client.get(f"/jobs?train_model={model['id']}").text
    assert f'<option value="{model["id"]} selected>' in html.replace('" selected>', ' selected>')
    r = client.post("/jobs", data={"kind": "train", "model_id": str(model["id"]), "through_season": "2024", "through_week": "10",
                                   "target": "any_idle"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("train job ")
    job = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert job["kind"] == "train" and job["params"] == {
        "model_id": str(model["id"]), "through": {"season": 2024, "week": 10}, "fee_model": {"taker_rate": 0.05, "half_spread": 0.01},
        "default_bankroll_cents": 10000, "max_bet_cents": 2500, "trade_max_games": 6, "backtest_seasons": [2010, 2021],
    }
    # Validate, prefilled from the models page link.
    html = client.get(f"/jobs?validate_model={model['id']}").text
    assert '<details class="card send" id="validate" open>' in html and 'name="validate_seed" value="1"' in html
    assert "held-out validation era (2022-2025)" in html
    r = client.post("/jobs", data={"kind": "validate", "model_id": str(model["id"]), "validate_seed": "4", "target": "any_idle"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("validate job ")
    job = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert job["kind"] == "validate" and job["role"] == "backtest" and job["params"]["model_id"] == str(model["id"]) and job["params"]["seed"] == 4
    assert job["params"]["validation_seasons"] == [2022, 2025] and job["params"]["workers"] == "auto"
    listing = client.get("/jobs").text
    assert "elo_blend n 50" in listing and listing.count('<tr>') == 6 and f"validate <span class=\"muted small\">{str(model['id'])[:8]}</span>" in listing


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
        assert f'<p class="error inline-error">{message}' in r.text, (data, message)
        assert 'id="fleet-grid"' not in r.text and 'action="/jobs"' in r.text, "the jobs page is re-rendered"
        if data["kind"] == "backtest":
            assert f'name="params" rows="2">{data["params"]}</textarea>' in r.text.replace("&#34;", '"'), "submitted values are kept"
            assert r.text.index('inline-error') < r.text.index('id="model_search"'), "the error sits in the posted form"
        if data["kind"] == "sleep":
            assert 'id="sleep" open>' in r.text
        if data["kind"] == "validate":
            assert 'id="validate" open>' in r.text
    assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0, "nothing was created"


def test_job_detail_renders_result_tables_and_model_links(client, conn, make_worker):
    w = make_worker("box1", role="backtest")
    model = insert_model(conn)
    metrics = backtest_metrics(n_bets=300, roi=0.04, per_season=[
        {"season": 2016, "n_games": 100, "n_bets": 30, "roi": 0.1, "pnl_cents": 3000, "log_loss": 0.66, "market_log_loss": 0.66, "max_drawdown": 0.03},
    ])
    bt = client.post("/api/jobs", json={"kind": "backtest", "params": {"model_id": str(model["id"])}, "target": w.id}).json()
    conn.execute("UPDATE jobs SET status = 'succeeded', result = %s, progress = 1 WHERE id = %s", (__import__("psycopg").types.json.Jsonb(metrics), bt["id"]))
    html = client.get(f"/jobs/{bt['id']}").text
    assert f'href="/models/{model["id"]}"' in html and ">300<" in html and "+4.0%" in html and ">2016<" in html and "+10.0%" in html
    assert "raw result" in html and 'class="table-wrap"' in html
    top = [{"index": 0, "params": {"k": 20.0, "hfa": 50.0, "mov_scale": 1}, "score": 0.03, "metrics": backtest_metrics(n_bets=200, roi=0.045)},
           {"index": 1, "params": {"k": 25.0, "hfa": 40.0, "mov_scale": 0}, "score": 0.01, "metrics": backtest_metrics(n_bets=100, roi=0.02)}]
    created = [{"id": str(model["id"]), "lineage_id": str(model["id"]), "created": False}, {"id": "00000000-0000-0000-0000-0000000000aa", "lineage_id": "x", "created": True}]
    ms = client.post("/api/jobs", json={"kind": "model_search", "params": {"family": "elo_blend", "n": 2}}).json()
    conn.execute("UPDATE jobs SET status = 'succeeded', result = %s WHERE id = %s", (__import__("psycopg").types.json.Jsonb({"evaluated": 2, "seasons": [2016, 2017], "top": top, "created_models": created}), ms["id"]))
    html = client.get(f"/jobs/{ms['id']}").text
    assert "K 20 · HFA 50 · MOV on" in html and "K 25 · HFA 40 · MOV off" in html and "+4.5%" in html
    assert f'href="/models/{model["id"]}">{str(model["id"])[:8]}</a>' in html and "0000000000aa" not in html.split("raw result")[0].replace('href="/models/00000000-0000-0000-0000-0000000000aa">00000000', "")
    assert f'<a class="btn small" href="/models/{model["id"]}">{str(model["id"])[:8]} (existing)</a>' in html
    assert "2 candidates evaluated over 2016-2017" in html


def test_phone_layout_rules(client, conn):
    """Tables stack on a phone (no horizontal scroll at 390 px) and tap targets stay 44 px."""
    insert_model(conn, metrics=backtest_metrics())
    css = client.get("/static/style.css").text
    assert "--tap: 44px" in css and "@media (max-width: 700px)" in css
    assert "table.models td.action .btn { flex: 1; min-height: var(--tap); }" in css
    assert ".send-grid { grid-template-columns: 1fr; }" in css
    for path in ("/models", "/jobs", "/trading"):
        html = client.get(path).text
        tables = re.findall(r"<table class=\"([^\"]+)\"", html)
        assert tables and all("stack" in t for t in tables), (path, tables)
        assert html.count("<table") == html.count('<div class="table-wrap">'), path
        assert 'width=device-width' in html
    assert "min-height: var(--tap)" in css.split("@media (max-width: 700px)")[1]


def test_metrics_render_as_pairs_and_stacked_tables(client, conn, make_worker):
    """MEDIUM: the whole-backtest metrics are labelled pairs (no sideways scroll on a
    phone) and the per-season and top-list tables stack like the leaderboard."""
    w = make_worker("box1", role="backtest")
    model = insert_model(conn, metrics=backtest_metrics(n_bets=0, roi=0.0, max_drawdown=0.004, per_season=[
        {"season": 2016, "n_games": 100, "n_bets": 0, "roi": 0.0, "pnl_cents": 0, "log_loss": 0.66, "market_log_loss": 0.66, "max_drawdown": None},
    ]))
    html = client.get(f"/models/{model['id']}").text
    assert '<dl class="kv metrics">' in html and '<dt>log-loss vs market</dt><dd>0.660 <span class="muted">vs 0.659</span></dd>' in html
    assert '<table class="metrics per-season stack">' in html and '<span class="k">drawdown</span> -' in html
    assert "<dt>ROI</dt><dd>-</dd>" in html and "<dt>hit rate</dt><dd>-</dd>" in html and "<dt>avg edge</dt><dd>-</dd>" in html, "no bets: no ROI, hit rate or edge"
    assert "<dt>max drawdown</dt><dd>0.4%" in html, "a 0.4% drawdown is not rounded to 0%"
    assert "+0.0%" not in html.split("<h2>lineage</h2>")[0]
    assert f'<a class="btn" href="/trading?model={model["id"]}#assign">Assign</a>' in html
    assert '<details class="edit">' in html and html.count("No summary yet.") == 1, "the summary editor is folded"
    assert '<dt>validation shrunk ROI</dt><dd><span class="muted">not validated</span>' in html and "<dt>search shrunk ROI</dt><dd>+0.00%" in html
    assert "Not validated yet: no held-out numbers" in html and ">Validate</button>" in html
    tables = re.findall(r"<table class=\"([^\"]+)\"", html)
    assert all("stack" in t for t in tables if "calibration" not in t), tables
    # The leaderboard row: "-" for ROI without bets, one-decimal drawdown.
    row = client.get("/models").text
    assert '<span class="k">search ROI</span> -' in row and '<span class="k">drawdown</span> 0.4%' in row
    assert '<span class="k">paper</span> -' in row and 'href="/trading?model=' in row
    # The search result page: a stacked top list with a shrunk ROI percentage.
    top = [{"index": 0, "params": {"k": 20.0, "hfa": 50.0, "mov_scale": 1}, "score": -0.0103, "metrics": backtest_metrics(n_bets=5, roi=-0.355, max_drawdown=0.5)}]
    ms = client.post("/api/jobs", json={"kind": "model_search", "params": {"family": "elo_blend", "n": 1}, "target": w.id}).json()
    conn.execute("UPDATE jobs SET status = 'succeeded', result = %s WHERE id = %s", (__import__("psycopg").types.json.Jsonb({"evaluated": 1, "seasons": [2016], "top": top, "created_models": []}), ms["id"]))
    html = client.get(f"/jobs/{ms['id']}").text
    assert '<table class="metrics top stack">' in html and "<th>shrunk ROI</th>" in html
    assert '<span class="k">shrunk ROI</span> -1.03%' in html and '<span class="k">ROI</span> -35.5%' in html and '<span class="k">drawdown</span> 50.0%' in html
    assert '<td class="lead"><span class="rank">#1</span> K 20 · HFA 50 · MOV on</td>' in html
    bt = client.post("/api/jobs", json={"kind": "backtest", "params": {"model_id": str(model["id"])}, "target": w.id}).json()
    conn.execute("UPDATE jobs SET status = 'succeeded', result = %s WHERE id = %s", (__import__("psycopg").types.json.Jsonb(backtest_metrics(n_bets=300, roi=0.04, per_season=[{"season": 2016, "n_games": 100, "n_bets": 30, "roi": 0.1, "pnl_cents": 3000, "log_loss": 0.66, "market_log_loss": 0.66, "max_drawdown": 0.03}])), bt["id"]))
    html = client.get(f"/jobs/{bt['id']}").text
    assert '<dl class="kv metrics">' in html and '<table class="metrics per-season stack">' in html and "<dt>hit rate</dt><dd>52.0%</dd>" in html
    css = client.get("/static/style.css").text
    assert "table.stack .k { display: inline; }" in css and "table.metrics.stack td { white-space: normal; }" in css
    assert ".kv { display: grid;" in css and ".chip {" in css and "white-space: nowrap; }" in css.split(".chip {")[1].split("\n")[0]
    assert ".btn.soon[disabled] { opacity: 1; color: var(--muted); border-style: dashed; }" in css


def test_send_cards_fold_so_the_job_list_is_near_the_top(client, conn):
    """MEDIUM: each send form is a details card; only the relevant one is open."""
    model = insert_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, trained_through=[2024, 18])
    html = client.get("/jobs").text
    assert '<details class="card send" id="backtest" open>' in html
    assert '<details class="card send" id="model_search">' in html and '<details class="card send" id="train">' in html
    assert html.count("<summary><h2>") == 4 and "<summary>Sleep test job</summary>" in html
    assert html.index('<table class="jobs stack">') > html.index('id="train"')
    assert f'K 20 · HFA 50 · MOV on · thru 2024 w18 · {str(model["id"])[:8]}</option>' in html, "the select label fits a phone"
    html = client.get(f"/jobs?train_model={model['id']}").text
    assert '<details class="card send" id="train" open>' in html and '<details class="card send" id="backtest">' in html
    r = client.post("/jobs", data={"kind": "model_search", "family": "elo_blend", "n": "0", "target": "any_idle"}, follow_redirects=False)
    assert r.status_code == 400 and '<details class="card send" id="model_search" open>' in r.text
    assert '<details class="card send" id="backtest">' in r.text
    insert_model(conn, family="elo_blend", params={"k": 21.0})
    assert "elo_blend · K 21" not in client.get("/jobs").text, "one family: no family prefix"


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


def test_trading_page_empty_state_and_fragment(client, conn):
    html = client.get("/trading").text
    assert '<a href="/trading" class="active">Trading</a>' in html
    assert 'id="assign"' in html and '<details class="card send assign" id="assign">' in html, "the create form is folded"
    assert "No assignments yet." in html and "No open orders." in html and "No orders yet." in html and "No fills yet." in html
    assert "Every market is mapped." in html and "No mapped markets yet." in html
    assert 'id="exchange"' in html and '<span class="chip chip-bad">DOWN</span>' in html and "never" in html
    assert 'id="ledger"' in html and '<span class="chip chip-ok">OK</span>' in html and "Replay of 0 bankrolls" in html
    assert "No upcoming game has a confirmed market yet" in html and "No model of a non-retired lineage" in html
    assert '<button type="submit" class="btn primary" disabled>Create assignment</button>' in html
    assert 'action="/assignments/activate-paper"' not in html and 'action="/cancel-all"' not in html
    assert 'id="trading-live"' in html and html.count("<section") == 8
    fragment = client.get("/fragments/trading").text
    assert "<html" not in fragment and 'id="trading-live"' not in fragment and 'id="assignments"' in fragment
    assert 'id="assign"' not in fragment, "the create form stays out of the refreshed region"
    assert fragment.count("<section") == 8


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
    live = html.split('id="trading-live"')[1]
    # assignments
    row = re.search(rf'<tr class="assignment-row" data-assignment="{setup.assignment["id"]}">.*?</tr>', live, re.S).group(0)
    assert "<strong>KC @ LV</strong>" in row and GAME_ID in row and f'href="/models/{setup.model["id"]}"' in row
    assert '<span class="chip mode-paper">paper</span>' in row and '<span class="badge st-active">active</span>' in row
    bank = conn.execute("SELECT * FROM bankrolls WHERE assignment_id = %s", (setup.assignment["id"],)).fetchone()
    assert f"avail ${bank['available_cents'] // 100}.{bank['available_cents'] % 100:02d}" in row
    assert "reserved $" in row and "open $2.08" in row and "realized -$0.12" in row
    assert '<span class="k">open orders</span> 1' in row and f'action="/assignments/{setup.assignment["id"]}/halt"' in row
    assert "Settle now" not in row and "Activate" not in row
    # open orders with a cancel button and the cancel-all form
    assert f'action="/orders/{opened["id"]}/cancel"' in live and 'action="/cancel-all"' in live
    assert "10 @ 0.52 (4 filled" in live and '<span class="badge st-partial">partial</span>' in live
    # recent orders: the rejection with its reason, the rationale, the worker name
    recent = live.split('id="orders"')[1].split('id="fills"')[0]
    assert str(rejected["order_id"]) in recent and '<span class="reason">max_bet: over max bet $' in recent and "&gt; $25.00</span>" in recent
    assert '<span class="badge st-rejected">rejected</span>' in recent and "my 0.58 vs ask 0.52, fee 0.012, edge 0.04" in recent
    assert "+4.0%" in recent and "my 0.58 vs 0.51" in recent and f"trader-" in recent and "200 @ 0.52" in recent
    # fills
    fills = live.split('id="fills"')[1].split('id="unmatched"')[0]
    assert "4 @ 0.52" in fills and "of 10 @ 0.52" in fills and "$0.12" in fills
    # unmatched market with the link form, escaped title
    unmatched = live.split('id="unmatched"')[1].split('id="markets"')[0]
    assert "Chiefs vs Raiders &lt;b&gt;x&lt;/b&gt;" in unmatched and f'action="/markets/{loose["id"]}/link"' in unmatched
    assert f'<option value="{GAME_ID}" selected>' in unmatched and '<option value="home" selected>home wins</option>' in unmatched
    assert "(50%)" in unmatched
    # mapped markets with snapshot ages
    markets = live.split('id="markets"')[1].split('id="exchange"')[0]
    assert markets.count("<tr data-market=") == 2 and "0.50 / 0.52" in markets and "0.46 / 0.48" in markets
    assert '<span class="stale-age">1 min ago</span>' in markets and "$2,000.00" in markets and "home wins" in markets and "away wins" in markets
    # exchange state and ledger
    exchange = live.split('id="exchange"')[1].split('id="ledger"')[0]
    assert '<span class="chip chip-ok">up</span>' in exchange and "3 s ago" in exchange and ">sim<" in exchange
    assert "boom &lt;i&gt;" in exchange and 'action="/exchange/probe"' in exchange
    assert "Replay of 1 bankroll " in live and '<span class="chip chip-ok">OK</span>' in live
    conn.execute("UPDATE bankrolls SET available_cents = available_cents + 1 WHERE id = %s", (bank["id"],))
    broken = client.get("/fragments/trading").text
    assert '<span class="chip chip-bad">problems</span>' in broken and "cached" in broken and "ledger sums to" in broken
    tables = re.findall(r'<table class="([^"]+)"', html)
    assert len(tables) == 6 and all("stack" in t for t in tables)


def test_create_assignment_form(client, conn):
    game = insert_game(conn)
    insert_market(conn, GAME_ID)
    insert_game(conn, "2026_05_DAL_PHI", home="PHI", away="DAL", kickoff_in_s=3 * 86400)
    insert_game(conn, "2025_01_OLD_GAME", kickoff_in_s=-86400)
    insert_market(conn, "2025_01_OLD_GAME")
    model = insert_model(conn, status="paper_ok", params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, trained_through=[2024, 18])
    retired = insert_model(conn, status="retired", params={"k": 21.0})
    html = client.get("/trading").text
    assert f'<option value="{GAME_ID}">KC @ LV · ' in html and "2026_05_DAL_PHI" not in html.split('id="trading-live"')[0], "only games with confirmed markets"
    assert "2025_01_OLD_GAME" not in html.split('id="trading-live"')[0], "kicked off games are not offered"
    assert f'<option value="{model["id"]}">elo_blend · K 20 · HFA 50 · MOV on · thru 2024 w18 · {str(model["id"])[:8]}</option>' in html
    assert str(retired["id"]) not in html
    assert 'name="bankroll" value="100.00"' in html and '<option value="paper" selected>paper</option>' in html and 'value="live"' not in html
    assert '<button type="submit" class="btn primary">Create assignment</button>' in html
    # ?model= opens the form with that model selected (the Assign button on the Models page)
    html = client.get(f"/trading?model={model['id']}").text
    assert '<details class="card send assign" id="assign" open>' in html and f'<option value="{model["id"]}" selected>' in html
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
    page = client.get("/trading").text
    assert f'data-assignment="{a["id"]}"' in page and '<details class="card send assign" id="assign">' in page
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
        assert '<p class="error inline-error">' in r.text and message in r.text, (data, message)
        assert '<details class="card send assign" id="assign" open>' in r.text and f'name="bankroll" value="{data["bankroll"]}"' in r.text
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
    html = client.get("/trading").text
    assert '<span class="badge st-halted">halted</span>' in html and f'action="/assignments/{aid}/activate"' in html
    assert f'action="/assignments/{aid}/halt"' not in html
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
    html = client.get("/trading").text
    assert f'action="/assignments/{aid}/settle"' in html and ">Settle now</button>" in html and "final 20-24" in html
    r = client.post(f"/assignments/{aid}/settle", data={}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == f"{GAME_ID} settled: 1 bets, P&L $4.68", flash_cookie(r)
    assert assignment_row(conn, aid)["status"] == "settled"
    bet = conn.execute("SELECT * FROM bets").fetchone()
    assert bet["result"] == "win" and bet["pnl_cents"] == 468 and bet["worker_id"] == setup.worker.id
    html = client.get("/trading").text
    assert '<span class="badge st-settled">settled</span>' in html and "paper today $4.68 &middot; all $4.68" in html
    assert '<span class="badge st-resolved">resolved YES</span>' in html
    fleet = client.get("/").text
    assert re.search(rf'data-worker="{setup.worker.id}">.*?today \$4\.68', fleet, re.S), "the worker's card shows its P&L"
    r = client.post(f"/assignments/{aid}/settle", data={}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("settle refused") or "settled" in flash_cookie(r)
    board = client.get("/models").text
    assert "1 g &middot; 1 bets &middot; $4.68" in board


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
    html = client.get("/trading").text
    assert "Every market is mapped." in html and "2026_05_DAL_PHI &middot; away wins" in html
    assert '<option value="2026_05_DAL_PHI">DAL @ PHI · ' in html, "a game without markets can be assigned now"
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
    html = client.get("/trading").text
    assert "Trading is killed: every assignment stays halted" in html and 'action="/assignments/activate-paper"' not in html
    assert html.count('<span class="badge st-halted">halted</span>') == 3 and "No open orders." in html
    assert ">Activate</button>" not in html, "no per-row activate under kill"
    assert 'data-banner="exchange-down"' in html, "no exchange heartbeat while killed"
    r = client.post("/assignments/activate-paper", data={}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "activate all paper refused: the kill switch is on; reset it first"
    client.post("/kill/reset", data={"confirm": "RESUME"}, follow_redirects=False)
    html = client.get("/trading").text
    assert ">Activate all paper (2)</button>" in html and f'action="/assignments/{done["id"]}/settle"' in html
    r = client.post("/assignments/activate-paper", data={}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "2 paper assignments activated"
    assert assignment_row(conn, setup.assignment["id"])["status"] == "active" and assignment_row(conn, second["id"])["status"] == "active"
    assert assignment_row(conn, done["id"])["status"] == "halted", "a final game's assignment waits for settlement"
    assert 'action="/assignments/activate-paper"' not in client.get("/trading").text
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'activate_all_paper'").fetchone()["n"] == 1


def test_topbar_banners(client, conn):
    assert 'data-banner' not in client.get("/fragments/topbar").text, "no heartbeat but nothing at stake: no banner"
    client.post("/kill", follow_redirects=False)
    topbar = client.get("/fragments/topbar").text
    assert '<a class="banner banner-down" href="/trading#exchange" data-banner="exchange-down" role="alert">EXCHANGE DOWN</a>' in topbar
    client.post("/kill/reset", data={"confirm": "RESUME"}, follow_redirects=False)
    assert 'data-banner' not in client.get("/fragments/topbar").text
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
    topbar = client.get("/fragments/topbar").text
    assert '<a class="banner banner-warn" href="/trading#assignments" data-banner="unattended" role="alert">1 assignment unattended</a>' in topbar
    second = make_assignment(conn, GAME_ID)
    conn.execute("UPDATE jobs SET created_at = now() - interval '2 minutes', updated_at = now() - interval '2 minutes' WHERE id = %s", (second["job_id"],))
    assert "2 assignments unattended" in client.get("/jobs").text
    client.post(f"/assignments/{second['id']}/halt", data={}, follow_redirects=False)
    assert "1 assignment unattended" in client.get("/fragments/topbar").text, "a halted assignment is not unattended"
    assert 'role="alert"' in client.get("/").text


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
    html = client.get("/").text
    assert "paper today $2.90 &middot; all $1.70" in html and "live today" not in html
    assert re.search(rf'data-worker="{helper.id}">.*?today \$3\.00', html, re.S) and re.search(rf'data-worker="{setup.worker.id}">.*?today -\$0\.10', html, re.S)
    card = re.search(rf'data-worker="{setup.worker.id}">.*?</article>', html, re.S).group(0)
    assert f'<div class="jobrow">\n    <a class="joblink" href="/jobs/{setup.job["id"]}">KC @ LV</a>' in card and '<span class="small muted">held</span>' in card
    assert 'role="progressbar"' not in card, "a held trade job has no progress to show"
    # a resolved market drops out of the open positions
    conn.execute("UPDATE markets SET status = 'resolved', resolved_yes = true WHERE id = %s", (setup.market["id"],))
    assert client.get("/api/pnl").json()["today_cents"] == 300
    set_setting(conn, "live_enabled", True)
    assert "live today $0.00 &middot; all $0.00" in client.get("/fragments/topbar").text


def test_leaderboard_paper_columns_and_ranking(client, conn):
    """A lineage with 5 paper games and 30 paper bets ranks on shrunk CLV ahead of the
    backtest-ranked ones; the others keep the step 3 order; paper columns show per row."""
    backtested = insert_validated_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, metrics=backtest_metrics(n_bets=400, roi=0.05), validation=validation_metrics(n_bets=400, roi=0.05), status="paper_ok")
    papered = insert_model(conn, params={"k": 30.0, "hfa": 60.0, "mov_scale": 0}, metrics=backtest_metrics(n_bets=10, roi=0.01), status="paper_ok")
    better = insert_model(conn, params={"k": 31.0, "hfa": 60.0, "mov_scale": 0}, metrics=backtest_metrics(n_bets=10, roi=0.01), status="live_eligible")
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
    # ties on shrunk CLV break on paper ROI
    _score(conn, almost, "2026_05_A_B", 10, 1000, 1000, 0.06)
    assert client.get("/api/models").json()["ranked"][0]["id"] == str(almost["id"])
    html = client.get("/models").text
    first = re.search(rf'<tr class="model-row" data-model="{almost["id"]}">.*?</tr>', html, re.S).group(0)
    assert '<span class="rank">#1</span><span class="chip chip-paper" title="ranked on paper CLV">paper</span>' in first
    assert '<span class="k">paper</span> 5 g &middot; 50 bets &middot; $14.00 &middot; ROI +28.0% &middot; CLV 0.052' in first
    last = re.search(rf'<tr class="model-row" data-model="{backtested["id"]}">.*?</tr>', html, re.S).group(0)
    assert '<span class="k">paper</span> -' in last and "chip-paper" not in last and "#4" in last
    assert "<th>paper</th>" in html and "5 paper games and 30 paper bets" in html
    detail = client.get(f"/models/{better['id']}").text
    assert "<dt>paper record</dt><dd>5 games &middot; 30 bets &middot; -$3.00 &middot; ROI -1.0% &middot; CLV 0.030" in detail
    assert "ranked on paper" in detail and "no paper games yet" in client.get(f"/models/{backtested['id']}").text


def test_settings_trade_group_round_trip(client, conn):
    html = client.get("/settings").text
    form = html.split('id="trade"')[1].split("</form>")[0]
    assert 'action="/settings/trade"' in html and "<h3>Order approval</h3>" in form and "<h3>Paper thresholds" in form
    assert '<option value="sim" selected>sim</option>' in form and '<option value="polymarket_clob">' in form
    assert 'name="participation" value="0.5"' in form and 'name="gtd_seconds" value="900"' in form
    assert 'name="trade_pregame_only" value="true" checked>' in form and 'name="paper_min_pnl" value="0.01"' in form
    assert 'name="max_exposure_paper" value="0.00"' in form and 'name="orders_per_s" value="5"' in form
    assert "&#34;gamma_url&#34;: &#34;https://gamma-api.polymarket.com&#34;" in form and "<textarea name=\"market_source_config\"" in form
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
    html = client.get("/settings").text
    assert '<option value="polymarket_clob" selected>' in html and 'name="trade_pregame_only" value="true">' in html
    assert 'name="paper_clv_ci" value="true">' in html and "CLV interval above zero" in html
    r = client.post("/settings/trade", data={**good, "paper_clv_ci": "true"}, follow_redirects=False)
    assert r.status_code == 303 and client.get("/api/settings").json()["thresholds_paper"]["clv_ci_excludes_zero"] is True
    assert 'name="paper_clv_ci" value="true" checked>' in client.get("/settings").text
    assert 'name="max_exposure_paper" value="1000.00"' in html and "&#34;gamma_url&#34;: &#34;https://g.example&#34;" in html
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
        assert 'name="participation" value="%s"' % bad["participation"] in r.text, "submitted values are kept"
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
    assert r.status_code == 200 and "Market probe" in r.text and '<pre id="payload">' in r.text and 'data-copy="payload"' in r.text
    assert ">sim<" in r.text or "sim" in r.text


def test_trading_style_rules():
    """The banner and chip colours use the text-safe tokens; the step 4 rules exist."""
    css = (Path(__file__).resolve().parent.parent / "host" / "static" / "style.css").read_text()
    assert ".banner-down { background: var(--red); color: var(--red-text); }" in css
    assert ".banner-warn { background: var(--amber-fill); color: #fff; }" in css
    assert ".chip.chip-bad { background: var(--red); color: var(--red-text); }" in css
    assert ".badge.st-rejected, .badge.st-rejected_by_exchange { background: var(--red); color: var(--red-text); }" in css
    phone = css.split("@media (max-width: 700px)")[-1]
    assert "table.assignments td.action { position: static; flex-basis: 100%; display: flex; gap: 0.5rem; }" in phone
    assert "label.check { min-height: var(--tap); }" in phone
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


def _live_section(html: str) -> str:
    return html.split('id="live"')[1].split('id="kill"')[0]


def test_settings_live_group_off_state(client, conn):
    """Live off: the state, the typed enable form with today's phrase as the hint, no
    Disable button, credentials no, auth not checked, blank balances, no auto-kill."""
    html = client.get("/settings").text
    live = _live_section(html)
    assert '<section class="card group live" id="live">' in html and 'data-live-state="off"' in live and ">OFF<" in live
    assert "Live trading is <strong>off</strong>" in live
    assert 'action="/settings/live"' in live and 'name="confirm" value="" placeholder="' + _phrase(conn) + '"' in live
    assert f'<code class="phrase">{_phrase(conn)}</code>' in live and "Enable live trading" in live
    assert 'action="/settings/live/off"' not in live and "Disable live" not in live
    assert "<dt>credentials</dt><dd>no " in live and '<span class="muted">not checked</span>' in live
    assert '<dt>balance</dt><dd><span class="muted">-</span>' in live and '<dt>buying power</dt><dd><span class="muted">-</span>' in live
    assert '<dt>clock skew</dt><dd><span class="muted">-</span>' in live and "<dt>last auth error</dt><dd><span class=\"muted\">none</span>" in live
    assert "none since the last reset" in live and "auto-kill-reason" not in live
    assert "the exchange process has no credentials loaded" in live, "the preconditions are listed before the owner types"
    assert 'name="live_enabled"' not in html, "the switch has no generic settings field"
    assert 'class="pill paper">PAPER</span>' in html and ">LIVE<" not in html


def test_settings_live_group_on_state(client, conn):
    """Live on: since when and by whom, the Disable button instead of the form, auth ok
    with its age, balance and buying power in dollars, the skew, the LIVE pill."""
    enable_live(conn, buying_power_cents=48_000)
    conn.execute("UPDATE exchange_state SET balance_cents = 50_000, clock_skew_ms = 120, live_enabled_by = 'owner@example.com',"
                 " live_enabled_at = now() - interval '26 minutes'")
    set_setting(conn, "tz", "America/New_York")
    html = client.get("/settings").text
    live = _live_section(html)
    assert '<section class="card group live is-live" id="live">' in html and 'data-live-state="on"' in live and ">ON<" in live
    since = re.search(r"Live trading is <strong>on</strong> since (\S+ \S+ \S+) by owner@example.com", live)
    assert since, live
    assert since.group(1).endswith(("EDT", "EST")), "the since time is shown in the owner's zone"
    assert 'action="/settings/live/off"' in live and "Disable live" in live and 'data-confirm="Disable live trading now?' in live
    assert 'action="/settings/live"' not in live and 'name="confirm"' not in live
    assert '<span class="chip chip-ok">ok</span>' in live and re.search(r"checked [0-9] s ago", live)
    assert "<dt>balance</dt><dd>$500.00</dd>" in live and "<dt>buying power</dt><dd>$480.00</dd>" in live
    assert "<dt>clock skew</dt><dd>120 ms</dd>" in live and "none since the last reset" in live
    assert 'class="pill live">LIVE</span>' in html and "live today $0.00" in html
    # a failed probe after live went on: the failure, its count and the last error show in red
    auth_state(conn, auth_ok=False, auth_failures=2, last_auth_error="401 unauthorized <b>")
    live = _live_section(client.get("/settings").text)
    assert '<span class="chip chip-bad">failed</span>' in live and "2 failures in a row" in live
    assert '<dt>last auth error</dt><dd><span class="error">401 unauthorized &lt;b&gt;</span>' in live


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
    html = client.get("/settings").text
    assert 'data-live-state="on"' in html and "by dev." in html and 'class="pill live">LIVE</span>' in html
    assert ">LIVE<" in client.get("/fragments/topbar").text and ">LIVE<" in client.get("/").text


def test_live_enable_wrong_phrase_shows_the_inline_error(client, conn):
    """A wrong phrase re-renders the page (400) with the error inside the live group and
    the typed text kept; a right phrase with a failed precondition is a 409 naming it."""
    _ready(conn)
    for bad in ("ENABLE LIVE TRADING", _phrase(conn, -1), _phrase(conn, 1), _phrase(conn).lower(), "RESUME", ""):
        r = client.post("/settings/live", data={"confirm": bad}, follow_redirects=False)
        assert r.status_code == 400, bad
        live = _live_section(r.text)
        assert '<p class="error inline-error">confirmation must be exactly &#34;' + _phrase(conn) + "&#34;</p>" in live, bad
        assert f'name="confirm" value="{bad}"' in live, "the typed text is kept"
        assert 'action="/settings/live"' in live and _live_flag(conn) is False
        assert "flash" not in r.cookies
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'live_on'").fetchone()["n"] == 0
    assert _live_section(client.get("/settings").text).count("inline-error") == 0, "a plain GET carries no error"
    auth_state(conn, credentials_present=False, auth_ok=False)
    r = client.post("/settings/live", data={"confirm": _phrase(conn)}, follow_redirects=False)
    assert r.status_code == 409 and _live_flag(conn) is False
    live = _live_section(r.text)
    assert "live trading cannot be enabled: the exchange process has no credentials loaded" in live
    client.post("/kill", follow_redirects=False)
    _ready(conn)
    r = client.post("/settings/live", data={"confirm": _phrase(conn)}, follow_redirects=False)
    assert r.status_code == 409 and "the kill switch is on; reset it first" in _live_section(r.text)
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
    assert 'data-live-state="on"' in client.get("/settings").text
    r = client.post("/settings/live/off", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#live"
    assert flash_cookie(r) == "Live trading disabled: 1 live assignment halted, 1 orders cancel requested on the exchange."
    assert _live_flag(conn) is False
    assert assignment_row(conn, setup.assignment["id"])["status"] == "halted"
    assert order_row(conn, opened["id"])["status"] == "cancel_requested"
    audit = conn.execute("SELECT actor FROM audit_log WHERE action = 'live_off'").fetchall()
    assert [a["actor"] for a in audit] == ["dev"]
    html = client.get("/settings").text
    assert 'data-live-state="off"' in html and 'class="pill paper">PAPER</span>' in html
    assert "Live trading is <strong>off</strong>" in html and 'action="/settings/live"' in html
    r = client.post("/settings/live/off", data={}, follow_redirects=False)
    assert r.status_code == 303 and "0 live assignments halted" in flash_cookie(r), "idempotent"


def test_topbar_pill_states(client, conn):
    """PAPER grey until live is on, LIVE green while it is, PAPER again after a kill."""
    for path in ("/", "/trading", "/settings", "/jobs", "/fragments/topbar"):
        html = client.get(path).text
        assert '<span class="pill paper">PAPER</span>' in html and ">LIVE<" not in html, path
    set_setting(conn, "live_enabled", True)
    for path in ("/", "/trading", "/settings", "/jobs", "/fragments/topbar"):
        html = client.get(path).text
        assert '<span class="pill live">LIVE</span>' in html and ">PAPER<" not in html, path
        assert "live today $0.00 &middot; all $0.00" in html, path
    assert ">ON<" in client.get("/settings").text
    client.post("/kill", follow_redirects=False)
    topbar = client.get("/fragments/topbar").text
    assert '<span class="pill paper">PAPER</span>' in topbar and 'data-killed="1"' in topbar, "a kill turns live off"
    css = client.get("/static/style.css").text
    assert ".pill.live { background: var(--green-fill); color: #fff; }" in css


def test_killed_bar_shows_the_auto_kill_reason(client, conn):
    """A kill pulled by the exchange process names its reason in the red bar and in the
    Settings kill section; a hand kill does not; a reset clears it."""
    client.post("/kill", follow_redirects=False)
    topbar = client.get("/fragments/topbar").text
    assert "TRADING KILLED. Reset in Settings." in topbar and "data-auto-kill" not in topbar and "automatically" not in topbar
    assert "Killed automatically" not in client.get("/settings").text
    client.post("/kill/reset", data={"confirm": "RESUME"}, follow_redirects=False)
    kill.auto_kill(conn, "auth_failures", {"failures": 3, "error": "401 <b>"})
    topbar = client.get("/fragments/topbar").text
    assert 'data-killed="1"' in topbar and 'data-auto-kill="auth_failures"' in topbar
    assert 'TRADING KILLED automatically: <span class="auto-reason">auth_failures</span>. Reset in Settings.' in topbar
    assert 'href="/settings#kill"' in topbar
    assert "TRADING KILLED automatically" in client.get("/").text and "TRADING KILLED automatically" in client.get("/trading").text
    html = client.get("/settings").text
    assert 'class="topbar killed"' in html
    assert "Killed automatically by the exchange process: <strong>auth_failures</strong> at " in html
    assert "401 &lt;b&gt;" in html and "&#34;failures&#34;: 3" in html
    assert '<span class="chip chip-bad auto-kill-reason">auth_failures</span>' in _live_section(html)
    # a later auto-kill is the one the bar names; the group lists both
    kill.auto_kill(conn, "clock_skew", {"skew_ms": 48_000})
    topbar = client.get("/fragments/topbar").text
    assert 'data-auto-kill="clock_skew"' in topbar and "auth_failures" not in topbar
    live = _live_section(client.get("/settings").text)
    assert live.index("auto-kill-reason\">auth_failures") < live.index("auto-kill-reason\">clock_skew")
    client.post("/kill/reset", data={"confirm": "RESUME"}, follow_redirects=False)
    topbar = client.get("/fragments/topbar").text
    assert "KILLED" not in topbar and "data-auto-kill" not in topbar
    html = client.get("/settings").text
    assert "Killed automatically" not in html and "none since the last reset" in html and "auto-kill-reason" not in html


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
    html = client.get("/trading").text
    live = html.split('id="trading-live"')[1]
    row = re.search(rf'<tr class="assignment-row is-live" data-assignment="{setup.assignment["id"]}">.*?</tr>', live, re.S).group(0)
    assert '<span class="chip mode-live">live</span>' in row
    assert f'<tr class="assignment-row" data-assignment="{paper["id"]}">' in live, "a paper row keeps its plain markup"
    open_orders = live.split('id="open-orders"')[1].split('id="orders"')[0]
    assert open_orders.count('class="is-live"') == 2 and open_orders.count('<span class="chip mode-live">live</span>') == 2
    assert f'<tr data-order="{smoke["id"]}" data-kind="smoke" class="is-live">' in open_orders
    assert open_orders.count('<span class="chip chip-smoke">smoke</span>') == 1 and "#pm-7f3a" in open_orders and "#pm-smoke-1" in open_orders
    assert f'action="/orders/{smoke["id"]}/cancel"' in open_orders, "a smoke order can be cancelled by hand"
    assert "1 @ 0.45" in open_orders and "$0.45" in open_orders
    recent = live.split('id="orders"')[1].split('id="fills"')[0]
    assert f'<tr data-order="{smoke["id"]}" data-kind="smoke" class="is-live">' in recent
    assert recent.count('<span class="chip chip-smoke">smoke</span>') == 1 and "smoke order" in recent
    exchange = live.split('id="exchange"')[1].split('id="ledger"')[0]
    assert '<span class="chip chip-ok">up</span>' in exchange and ">polymarket_us<" in exchange
    assert '<dd class="c-auth"><span class="chip chip-ok">ok</span> <span class="muted small">checked 0 s ago</span> &middot; credentials yes &middot; skew 140 ms</dd>' in exchange
    assert "$1,234.56 &middot; buying power $1,000.00" in exchange
    assert '<dd class="c-live-orders">2 open <span class="chip chip-smoke">1 smoke</span></dd>' in exchange
    assert "last auth error" not in exchange
    assert html.count("<section") == 8 and client.get("/fragments/trading").text.count("<section") == 8
    # auth failing: the chip turns, the error shows; a cancelled smoke order leaves the count
    auth_state(conn, auth_ok=False, auth_failures=2, last_auth_error="401 unauthorized <i>")
    conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (smoke["id"],))
    frag = client.get("/fragments/trading").text
    exchange = frag.split('id="exchange"')[1].split('id="ledger"')[0]
    assert '<span class="chip chip-bad">failed</span>' in exchange and "401 unauthorized &lt;i&gt;" in exchange
    assert '<dd class="c-live-orders">1 open</dd>' in exchange
    assert 'data-kind="smoke"' not in frag.split('id="open-orders"')[1].split('id="orders"')[0]
    assert "chip-smoke" in frag.split('id="orders"')[1].split('id="fills"')[0], "still flagged in the recent list"
    assert "No live orders" not in frag


def test_live_phone_layout_and_colour_rules(client, conn):
    """The live form stacks at phone width with 44 px buttons, the tinted live row keeps
    AA contrast in both schemes, the Settings tables stack."""
    from tests.test_style import _schemes, contrast

    css = client.get("/static/style.css").text
    assert ".chip.chip-smoke { background: var(--amber-fill); color: #fff; }" in css
    assert "tr.is-live { background: var(--live-bg); }" in css and css.count("--live-bg:") == 2
    assert ".live-form .btn, .live-off .btn { min-height: var(--tap); }" in css
    phone = css.split("@media (max-width: 700px)")[-1]
    assert ".live-form label { flex-basis: 100%; }" in phone and ".live-form .btn, .live-off .btn { flex: 1; width: 100%; }" in phone
    for name, tokens in zip(("light", "dark"), _schemes()):
        for fg in ("text", "muted", "accent", "red-fg"):
            assert contrast(tokens[fg], tokens["live-bg"]) >= 4.5, (name, fg)
    enable_live(conn)
    for state in ("on", "off"):
        html = client.get("/settings").text
        assert 'width=device-width' in html and re.findall(r'<table class="([^"]+)"', html) == ["audit stack"]
        if state == "on":
            assert '<form method="post" action="/settings/live/off" class="live-off"' in html
            set_setting(conn, "live_enabled", False)
        else:
            assert '<form method="post" action="/settings/live" class="live-form">' in html
            assert 'autocapitalize="characters" spellcheck="false"' in html, "a phone keyboard must not mangle the phrase"
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
    assert '<section class="robustness" id="robustness">' in html
    assert '<span class="chip chip-flag chip-overfit" title="the search era looked better' in html and '<span class="chip chip-flag chip-regime_dependent"' in html
    assert "<strong>overfit</strong>: the search era looked better than the held-out era" in html
    assert "<strong>regime-dependent</strong>: one game regime" in html
    assert 'Validation ROI <strong>+4.1%</strong> <span class="range">(90% range -1.2% to +9.4%)</span> over 130 bets, shrunk +2.32%.' in html
    assert "Hit rate 52.0% <span class=\"range\">(49.0% to 56.0%)</span>" in html and "average edge +3.4%" in html
    assert "CLV range <span class=\"range\">0.000 to 0.000</span>" in html
    assert '<p class="market-line beats">Beats the market on log-loss: mean gain +0.0021 per game, p = 0.012 (sign-flip test, 10 000 flips; beaten means p &lt; 0.05).</p>' in html
    assert '<span class="chip chip-beats">beats market</span>' in html
    assert "<dt>calibration slope</dt><dd>0.970" in html and "<dt>calibration intercept</dt><dd>-0.020" in html
    assert "<dt>reliability</dt><dd>0.0021</dd>" in html and "<dt>resolution</dt><dd>0.0146</dd>" in html and "<dt>uncertainty</dt><dd>0.2487</dd>" in html
    assert '<table class="metrics stress stack">' in html and "<strong>spread+0.01</strong>" in html and "<strong>spread+0.02</strong>" in html and "<strong>fee x1.5</strong>" in html
    assert '<span class="k">bets</span> 96' in html and '<span class="k">ROI</span> +2.5%' in html and '<span class="k">gain</span> 0.0020' in html
    assert "10 perturbations (every numeric parameter scaled by 0.9 to 1.1): shrunk ROI median +1.90%, 10th percentile +0.40%; log-loss gain median 0.0018, 10th percentile 0.0007." in html
    assert '<table class="metrics regimes stack">' in html
    for label in ("favourite", "underdog", "home", "away", "divisional", "non-divisional", "primetime", "day", "cold or windy", "other weather"):
        assert f"<strong>{label}</strong>" in html, label
    assert html.index("<strong>favourite</strong>") < html.index("<strong>underdog</strong>") < html.index("<strong>home</strong>")
    assert "<h3>validation per season</h3>" in html and ">2022<" in html and "Stress seed 9; bootstrap B = 1000, 10 000 sign flips." in html
    assert html.index('id="robustness"') < html.index("<h2>backtest"), "the validation era leads"
    tables = re.findall(r"<table class=\"([^\"]+)\"", html)
    assert all("stack" in t for t in tables if "calibration" not in t), tables
    assert chr(0x2014) not in html
    # No flags, market not beaten: the honest sentence and a "no flags" chip.
    plain = insert_model(conn, params={"k": 21.0}, validation=validation_metrics(market_p=0.4), stress=stress_metrics())
    html = client.get(f"/models/{plain['id']}").text
    assert '<span class="chip chip-ok">no flags</span>' in html and "Does not beat the market on log-loss" in html and "chip-beats" not in html
    # A validate job renders its result the same way, and the kind is listed with the model.
    w = make_worker("box1", role="backtest")
    job = client.post("/api/jobs", json={"kind": "validate", "params": {"model_id": str(model["id"])}, "target": w.id}).json()
    conn.execute("UPDATE jobs SET status = 'succeeded', result = %s, checkpoint = %s WHERE id = %s",
                 (__import__("psycopg").types.json.Jsonb({"validation_metrics": validation, "stress_metrics": stress}), __import__("psycopg").types.json.Jsonb({"stage": "regimes"}), job["id"]))
    html = client.get(f"/jobs/{job['id']}").text
    assert '<section class="robustness" id="robustness">' in html and "<strong>spread+0.02</strong>" in html and "stage regimes" in html
    assert f'href="/models/{model["id"]}"' in html and "raw result" in html


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
    html = client.get("/models").text
    assert re.search(r'<span class="range paper-ci">0\.0\d\d to 0\.0\d\d</span>', html), "the 90% CLV range next to the paper record"
    detail = client.get(f"/models/{model['id']}").text
    assert re.search(r'<span class="paper-ci">CLV 90% range 0\.0\d\d to 0\.0\d\d over 4 bets</span>', detail)


def test_settings_step6_groups_round_trip(client, conn):
    """The thresholds group with the gate fields and the seasons group with the
    validation era and the search pool; an overlapping era is an inline error."""
    html = client.get("/settings").text
    form = html.split('id="thresholds"')[1].split("</form>")[0]
    assert 'name="min_bets" value="50"' in form and 'name="min_roi_ci_low" value="0.0"' in form and 'name="max_market_p" value="0.1"' in form
    assert 'name="require_validation" value="true" checked>' in form and 'name="forbid_overfit" value="true" checked>' in form
    assert 'name="forbid_fragile" value="true" checked>' in form and 'name="forbid_regime_dependent" value="true">' in form
    assert "judged on the validation era" in form and "the bootstrap lower bound" in form and "0.05 = beats the market" in form
    seasons = html.split('id="seasons"')[1].split("</form>")[0]
    assert 'name="seasons_first" value="2010"' in seasons and 'name="seasons_last" value="2021"' in seasons
    assert 'name="validation_first" value="2022"' in seasons and 'name="validation_last" value=""' in seasons and 'name="search_workers" value="auto"' in seasons
    assert "blank = the season before the validation era" in seasons and "auto = cores minus one" in seasons
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
    assert 'name="require_validation" value="true">' in client.get("/settings").text
    # Seasons: the validation era must start after the search era; a blank search last season is allowed.
    r = client.post("/settings/seasons", data={"seasons_first": "2012", "seasons_last": "2022", "validation_first": "2022", "validation_last": "", "search_workers": "4"}, follow_redirects=False)
    assert r.status_code == 400 and "validation_seasons must start after the search era ends (2022)" in r.text
    assert 'name="search_workers" value="4"' in r.text, "submitted values are kept"
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
