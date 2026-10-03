"""Dashboard pages, fragments and form posts (TestClient in FLEET_DEV mode)."""
from __future__ import annotations

import dataclasses
import re
from datetime import timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from host.api.app import create_app
from tests.conftest import (
    FIXTURE_GAMES, backtest_metrics, flash_cookie, heartbeat_body, ingest_fixture, insert_model, job_row, lease_job,
    model_row, set_heartbeat_age, worker_row,
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
        assert ">Models<" in html and ">Trading<" in html and 'href="/settings"' in html
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
               "taker_rate", "half_spread", "min_roi", "max_drawdown")
    numeric = ("trade_max_games", "lease_seconds", "heartbeat_seconds", "online_after_seconds", "max_expiries",
               "min_bets", "seasons_first", "seasons_last", "nflverse_refresh_hours")
    for name in decimal:
        assert re.search(rf'<input type="text" name="{name}" value="[^"]*" inputmode="decimal"', html), name
    for name in numeric:
        assert re.search(rf'<input type="text" name="{name}" value="[^"]*" inputmode="numeric"', html), name
    assert re.search(r'<input type="text" name="tz" value="[^"]*" autocomplete="off">', html)
    assert re.search(r'<input type="text" name="nflverse_url" value="https://[^"]*" autocomplete="off">', html)
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
    for path in ("/", "/jobs", "/settings", "/kill/confirm", "/fragments/fleet", "/static/style.css", "/nope"):
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
    r = client.post("/kill/reset", content=b"confirm=" + b"x" * 300_000, headers=form)
    assert r.status_code == 413 and r.headers["content-type"].startswith("text/html")
    r = client.post("/api/kill/reset", content=b"x" * 300_000, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == {"detail": "request body too large"}
    assert client.get("/api/settings").json()["kill_switch"] is True


# ------------------------------------------------------------------ step 3: models and job forms


def test_models_page_renders_ranked_rows_and_attribution(client, conn):
    html = client.get("/models").text
    assert "No models yet. Send a model search" in html and "CC BY 4.0" in html and "nflverse" in html
    assert 'id="unranked"' not in html and 'id="ranked"' not in html and "<table" not in html
    only_unranked = insert_model(conn, params={"k": 19.0}, metrics=backtest_metrics(n_bets=20, roi=0.5))
    html = client.get("/models").text
    assert "No lineage has 50 backtest bets yet, so none is ranked." in html and "No models yet" not in html
    assert 'id="ranked"' not in html and 'id="unranked"' in html, "no empty ranked header above the unranked table"
    conn.execute("DELETE FROM models WHERE id = %s", (only_unranked["id"],))
    best = insert_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, metrics=backtest_metrics(n_bets=400, roi=0.05, log_loss=0.65, market_log_loss=0.659, max_drawdown=0.14, seasons=[2016, 2017, 2018, 2019]), status="paper_ok", summary="Best lineage.")
    insert_model(conn, parent=best, trained_through=[2024, 10])
    second = insert_model(conn, params={"k": 30.0, "hfa": 60.0, "mov_scale": 0}, metrics=backtest_metrics(n_bets=120, roi=0.03))
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
    assert "+5.0%" in ranked and ">400<" not in ranked and "400" in ranked and "0.650" in ranked and "vs 0.659" in ranked
    assert "14.0%" in ranked and "2016-2019" in ranked and "Best lineage." in ranked and '<span class="chip">2 rows</span>' in ranked
    assert f'href="/jobs?train_model={best["id"]}#train"' in ranked and ">Train</a>" in ranked
    assert 'disabled title="comes with step 4">Assign <small>step 4</small></button>' in ranked
    assert f'action="/models/{best["id"]}/summary"' in ranked and 'maxlength="600"' in ranked
    assert "&lt;b&gt;bold&lt;/b&gt;" in html and "<b>bold</b>" not in html
    assert "No summary yet." in html
    assert 'class="attribution' in html and "creativecommons.org/licenses/by/4.0" in html
    assert '<a href="/models" class="active">Models</a>' in html and "step 3" not in html


def test_model_detail_page(client, conn, make_worker):
    root = insert_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, metrics=backtest_metrics(n_bets=400, roi=0.05, per_season=[
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
    assert html.count('action="/jobs"') == 4 and html.count('<option value="any_idle">Any idle worker</option>') == 4
    assert 'name="seasons_first" value="2010"' in html and 'name="seasons_last" value=""' in html
    assert 'name="n" value="200"' in html and 'name="top_k" value="5"' in html and 'name="through_season" value="2025"' in html
    assert f'<option value="{model["id"]}">K 20 · HFA 50 · MOV on · untrained · {str(model["id"])[:8]}</option>' in html
    assert '<option value="elo_blend" selected>elo_blend</option>' in html
    # Backtest by family + params.
    r = client.post("/jobs", data={"kind": "backtest", "model_id": "", "family": "elo_blend", "params": '{"k": 22}',
                                   "seasons_first": "2018", "seasons_last": "", "target": "any_idle"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs" and flash_cookie(r).startswith("backtest job ")
    job = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert job["kind"] == "backtest" and job["params"]["family"] == "elo_blend" and job["params"]["params"] == {"k": 22}
    assert job["params"]["seasons"] == [2018, 2025] and job["params"]["backtest_seasons"] == [2010, 2025]
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
        "default_bankroll_cents": 10000, "max_bet_cents": 2500, "trade_max_games": 6, "backtest_seasons": [2010, 2025],
    }
    listing = client.get("/jobs").text
    assert "elo_blend n 50" in listing and listing.count('<tr>') == 5


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
    for path in ("/models", "/jobs"):
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
    assert 'class="btn soon" disabled title="comes with step 4">Assign' in html
    assert '<details class="edit">' in html and html.count("No summary yet.") == 1, "the summary editor is folded"
    assert "<dt>shrunk ROI</dt><dd>+0.00%" in html
    tables = re.findall(r"<table class=\"([^\"]+)\"", html)
    assert all("stack" in t for t in tables if "calibration" not in t), tables
    # The leaderboard row: "-" for ROI without bets, one-decimal drawdown.
    row = client.get("/models").text
    assert '<span class="k">ROI</span> -' in row and '<span class="k">drawdown</span> 0.4%' in row
    assert 'class="btn small soon" disabled' in row
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
    assert html.count("<summary><h2>") == 3 and "<summary>Sleep test job</summary>" in html
    assert html.index('<table class="jobs stack">') > html.index('id="train"')
    assert f'K 20 · HFA 50 · MOV on · thru 2024 w18 · {str(model["id"])[:8]}</option>' in html, "the select label fits a phone"
    html = client.get(f"/jobs?train_model={model['id']}").text
    assert '<details class="card send" id="train" open>' in html and '<details class="card send" id="backtest">' in html
    r = client.post("/jobs", data={"kind": "model_search", "family": "elo_blend", "n": "0", "target": "any_idle"}, follow_redirects=False)
    assert r.status_code == 400 and '<details class="card send" id="model_search" open>' in r.text
    assert '<details class="card send" id="backtest">' in r.text
    insert_model(conn, family="elo_blend", params={"k": 21.0})
    assert "elo_blend · K 21" not in client.get("/jobs").text, "one family: no family prefix"
