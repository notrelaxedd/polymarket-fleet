"""Owner routes: auth, CSRF, settings, fleet view, job CRUD."""
from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from host.api.app import create_app
from tests.conftest import heartbeat_body


def test_owner_header_required_unless_dev(config, make_worker):
    strict = dataclasses.replace(config, dev=False, owner_login="owner@example.com")
    with TestClient(create_app(strict)) as c:
        assert c.get("/api/fleet").status_code == 401
        assert c.get("/api/fleet", headers={"Tailscale-User-Login": "intruder@example.com"}).status_code == 401
        assert c.get("/api/fleet", headers={"Tailscale-User-Login": "owner@example.com"}).status_code == 200
        assert c.post("/api/enroll-token").status_code == 401
        assert c.get("/healthz").status_code == 200
        assert c.get("/dl/version").status_code == 200
    unset = dataclasses.replace(config, dev=False, owner_login="")
    with TestClient(create_app(unset)) as c:
        assert c.get("/api/fleet", headers={"Tailscale-User-Login": "x"}).status_code == 401
    with TestClient(create_app(config)) as c:
        assert c.get("/api/fleet").status_code == 200, "FLEET_DEV skips the header"


def test_origin_mismatch_is_403(client):
    assert client.post("/api/enroll-token", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/enroll-token", headers={"Origin": "http://127.0.0.1:8080"}).status_code == 200
    assert client.post("/api/enroll-token", headers={"Origin": "HTTP://127.0.0.1:8080/"}).status_code == 200
    assert client.post("/api/enroll-token").status_code == 200, "no Origin (curl) passes"
    assert client.get("/api/fleet", headers={"Origin": "http://evil.example"}).status_code == 200


def test_settings_get_and_post(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["lease_seconds"] == 30
    assert r.json()["max_daily_loss_cents"] == {"live": 30000, "paper": 100000}
    r = client.post("/api/settings", json={"lease_seconds": 45, "tz": "America/New_York"})
    assert r.status_code == 200 and r.json()["lease_seconds"] == 45
    assert client.get("/api/settings").json()["lease_seconds"] == 45
    r = client.post("/api/settings", json={"lease_seconds": 50, "bogus": 1})
    assert r.status_code == 400 and "bogus" in r.json()["detail"]
    assert client.get("/api/settings").json()["lease_seconds"] == 45, "rejected batch changed nothing"


def test_fleet_shape(client, make_worker):
    w = make_worker("box1", role="backtest")
    make_worker("box2", online=False)
    job = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 9}}).json()
    hb = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("backtest"), headers=w.headers).json()
    assert [c["id"] for c in hb["claimed"]] == [job["id"]]
    r = client.get("/api/fleet")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"workers", "settings", "server_time"}
    assert body["server_time"].endswith("Z")
    assert set(body["settings"]) >= {"lease_seconds", "heartbeat_seconds", "online_after_seconds", "kill_switch"}
    workers = {x["name"]: x for x in body["workers"]}
    expected_keys = {
        "id", "name", "online", "desired_role", "reported_role", "role_epoch", "acked_epoch",
        "switching", "auto_role", "enabled", "cpu_pct", "ram_used_mb", "ram_total_mb",
        "code_version", "python_version", "hostname", "last_heartbeat_at", "current_jobs",
    }
    assert set(workers["box1"]) == expected_keys
    assert "token_hash" not in workers["box1"]
    assert workers["box1"]["online"] is True and workers["box2"]["online"] is False
    assert workers["box1"]["switching"] is False
    assert workers["box1"]["cpu_pct"] == 1.5 and workers["box1"]["ram_used_mb"] == 512
    assert workers["box1"]["current_jobs"] == [{"id": job["id"], "kind": "sleep", "status": "leased", "progress": 0}]
    assert workers["box2"]["current_jobs"] == []


def test_role_and_enabled_routes(client, make_worker):
    w = make_worker("box1")
    r = client.post(f"/api/workers/{w.id}/role", json={"role": "train"})
    assert r.status_code == 200, r.text
    assert r.json()["desired_role"] == "train" and r.json()["role_epoch"] == 2
    assert "token_hash" not in r.json()
    assert client.post(f"/api/workers/{w.id}/role", json={"role": "chef"}).status_code == 400
    assert client.post("/api/workers/w_nope/role", json={"role": "idle"}).status_code == 404
    r = client.post(f"/api/workers/{w.id}/enabled", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    fleet = client.get("/api/fleet").json()["workers"][0]
    assert fleet["switching"] is True and fleet["enabled"] is False


def test_jobs_crud(client, make_worker):
    w = make_worker("box1")
    r = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 3}, "target": "any_idle"})
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["target_worker_id"] == w.id and job["role"] == "backtest" and job["status"] == "queued"
    assert "waiting_for_idle_worker" not in job
    r = client.post("/api/jobs", json={"kind": "sleep", "target": "any_idle"})
    assert r.status_code == 201 and r.json()["waiting_for_idle_worker"] is True
    r = client.post("/api/jobs", json={"kind": "sleep", "idempotency_key": "same"})
    assert r.status_code == 201
    first = r.json()["id"]
    r = client.post("/api/jobs", json={"kind": "sleep", "idempotency_key": "same"})
    assert r.status_code == 200 and r.json()["id"] == first
    assert client.post("/api/jobs", json={"kind": "mystery"}).status_code == 400
    assert client.post("/api/jobs", json={"kind": "sleep", "target": "w_nope"}).status_code == 404
    assert client.post("/api/jobs", json={}).status_code == 400
    listing = client.get("/api/jobs").json()
    assert [j["id"] for j in listing][-1] == job["id"], "newest first"
    assert len(client.get("/api/jobs", params={"status": "queued", "limit": 2}).json()) == 2
    assert client.get("/api/jobs", params={"status": "weird"}).status_code == 400
    r = client.post(f"/api/jobs/{first}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert client.post(f"/api/jobs/{first}/cancel").status_code == 200, "cancel is idempotent"
    detail = client.get(f"/api/jobs/{first}").json()
    assert detail["status"] == "cancelled"
    assert [e["event"] for e in detail["events"]] == ["created", "cancelled"]
    assert client.get("/api/jobs/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/api/jobs/garbage").status_code == 404


OWNER = {"Tailscale-User-Login": "owner@example.com"}
MACHINE = {"hostname": "box1", "python_version": "3.13.1", "code_version": "abc", "boot_id": "b1"}


def _enroll_from(c: TestClient, peer: str) -> str:
    """Enroll a worker whose recorded remote_ip is `peer` (last X-Forwarded-For hop)."""
    token = c.post("/api/enroll-token", headers=OWNER).json()["token"]
    r = c.post("/api/v1/workers/register", json={"enroll_token": token, **MACHINE},
               headers={"X-Forwarded-For": f"9.9.9.9, {peer}"})
    assert r.status_code == 200, r.text
    return r.json()["worker_id"]


def test_owner_routes_refused_from_a_worker_machine(config, conn):
    """MEDIUM: a request from a registered worker's tailnet IP is not the owner, even with
    the owner login header, unless FLEET_OWNER_ALLOW_WORKER_IPS is set."""
    strict = dataclasses.replace(config, dev=False, owner_login="owner@example.com")
    with TestClient(create_app(strict)) as c:
        wid = _enroll_from(c, "100.64.0.7")
        assert conn.execute("SELECT remote_ip FROM workers WHERE id = %s", (wid,)).fetchone()["remote_ip"] == "100.64.0.7"
        r = c.get("/api/fleet", headers={**OWNER, "X-Forwarded-For": "100.64.0.7"})
        assert r.status_code == 403 and wid in r.json()["detail"]
        assert c.post("/api/enroll-token", headers={**OWNER, "X-Forwarded-For": "100.64.0.7"}).status_code == 403
        assert c.get("/api/fleet", headers={**OWNER, "X-Forwarded-For": "100.64.0.9"}).status_code == 200
        assert c.get("/api/fleet", headers={**OWNER, "X-Forwarded-For": "100.64.0.7, 100.64.0.9"}).status_code == 200, \
            "only the last hop counts"
        assert c.get("/api/fleet", headers={"X-Forwarded-For": "100.64.0.9"}).status_code == 401, "login still required"
    allowed = dataclasses.replace(strict, allow_worker_ips=True)
    with TestClient(create_app(allowed)) as c:
        assert c.get("/api/fleet", headers={**OWNER, "X-Forwarded-For": "100.64.0.7"}).status_code == 200
    direct = dataclasses.replace(strict, trust_proxy=False)
    with TestClient(create_app(direct)) as c:
        assert c.get("/api/fleet", headers={**OWNER, "X-Forwarded-For": "100.64.0.7"}).status_code == 200, \
            "X-Forwarded-For is ignored when the proxy is not trusted"
        other = _enroll_from(c, "100.64.0.8")
        assert conn.execute("SELECT remote_ip FROM workers WHERE id = %s", (other,)).fetchone()["remote_ip"] == "testclient"
        assert c.get("/api/fleet", headers=OWNER).status_code == 403, "the socket peer is now a worker"


def test_non_ascii_owner_header_is_401_not_500(config):
    strict = dataclasses.replace(config, dev=False, owner_login="owner@example.com")
    with TestClient(create_app(strict)) as c:
        r = c.get("/api/fleet", headers={b"tailscale-user-login": "\xe9".encode("latin-1")})
        assert r.status_code == 401
        r = c.get("/api/fleet", headers={b"tailscale-user-login": "owner@example.com".encode()})
        assert r.status_code == 200


def test_settings_are_validated_and_audited(client, conn, make_worker):
    """MEDIUM: wrong types and ranges are 400 without coercion; each change is audited."""
    before = client.get("/api/settings").json()
    bad = [
        {"kill_switch": "false"}, {"kill_switch": 1}, {"live_enabled": "false"}, {"lease_seconds": -5},
        {"lease_seconds": 5}, {"lease_seconds": 30.5}, {"heartbeat_seconds": 0}, {"max_bet_cents": "lots"},
        {"max_bet_cents": -1}, {"min_edge": 2}, {"kelly_fraction": "0.5"}, {"tz": "../etc/passwd"},
        {"max_daily_loss_cents": {"live": 1}}, {"max_daily_loss_cents": {"live": "1", "paper": 2}},
        {"max_expiries": 0}, {"kill_switch": "no", "lease_seconds": 60},
        # MEDIUM: fleet timing is checked across fields over the merged settings
        {"heartbeat_seconds": 60}, {"heartbeat_seconds": 13}, {"online_after_seconds": 5},
        {"lease_seconds": 20, "heartbeat_seconds": 10, "online_after_seconds": 30},
        {"max_bet_cents": 10**25}, {"max_daily_loss_cents": {"live": 10**25, "paper": 0}},
    ]
    for body in bad:
        r = client.post("/api/settings", json=body)
        assert r.status_code == 400, (body, r.text)
        assert next(iter(body)) in r.json()["detail"]
    assert client.get("/api/settings").json() == before, "nothing changed"
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'settings_changed'").fetchone()["n"] == 0
    # kill_switch never moves through the generic settings write: only the kill
    # transaction (cancel-all, halts, audit) and the RESUME reset may change it
    for body in ({"kill_switch": True}, {"kill_switch": False}, {"kill_switch": True, "lease_seconds": 45}):
        r = client.post("/api/settings", json=body)
        assert r.status_code == 400 and "use /api/kill" in r.json()["detail"], (body, r.text)
    assert client.get("/api/settings").json() == before
    r = client.post("/api/settings", json={"lease_seconds": 45, "tz": "America/New_York", "max_expiries": None})
    assert r.status_code == 200, r.text
    assert r.json()["kill_switch"] is False and r.json()["max_expiries"] is None
    rows = conn.execute(
        "SELECT entity, actor, before, after FROM audit_log WHERE action = 'settings_changed' ORDER BY id"
    ).fetchall()
    assert [(x["entity"], x["before"], x["after"]) for x in rows] == [
        ("lease_seconds", {"lease_seconds": 30}, {"lease_seconds": 45}),
        ("max_expiries", {"max_expiries": 3}, {"max_expiries": None}),
    ]
    assert all(x["actor"] for x in rows)
    w = make_worker("box1", role="backtest")
    assert client.post("/api/kill").status_code == 200
    hb = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("backtest"), headers=w.headers).json()
    assert hb["kill"] is True
    assert client.post("/api/settings", json={"live_enabled": True}).status_code == 400, "live cannot be switched on under kill"
    assert client.post("/api/kill/reset", json={"confirm": "RESUME"}).status_code == 200
    client.post("/api/jobs", json={"kind": "sleep"})
    hb = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("backtest"), headers=w.headers).json()
    assert hb["kill"] is False and hb["claimed"][0]["lease_seconds"] == 45


def test_pnl_and_audit_routes(client, make_worker):
    """Step 2: /api/pnl is zeros keyed by worker (and by mode from step 4); /api/audit is newest first."""
    a = make_worker("a")
    b = make_worker("b")
    r = client.get("/api/pnl")
    assert r.status_code == 200
    zero = {"today_cents": 0, "all_time_cents": 0}
    assert r.json() == {**zero, "by_worker": {a.id: 0, b.id: 0}, "by_mode": {"paper": zero, "live": zero}}
    client.post(f"/api/workers/{a.id}/role", json={"role": "train"})
    client.post("/api/settings", json={"lease_seconds": 40})
    r = client.get("/api/audit")
    assert r.status_code == 200
    rows = r.json()
    assert [x["action"] for x in rows] == ["settings_changed", "set_role"]
    assert rows[0]["entity"] == "lease_seconds" and rows[0]["ts"].endswith("Z")
    assert set(rows[1]) >= {"id", "ts", "actor", "action", "entity", "before", "after", "confirmation_text"}
    assert len(client.get("/api/audit", params={"limit": 1}).json()) == 1
    assert client.get("/api/audit", params={"limit": 0}).status_code == 400


def test_step6_settings_defaults_and_validators(client, conn):
    """The migration seeds the robustness keys; the eras, the search pool and the new
    gate fields are checked with no coercion, and the validation era must start after
    the search era."""
    s = client.get("/api/settings").json()
    assert s["validation_seasons"] == [2022, None] and s["search_workers"] == "auto" and s["backtest_seasons"] == [2010, 2021]
    assert s["thresholds_backtest"] == {"min_bets": 50, "min_roi": 0.02, "max_drawdown": 0.3, "require_validation": True,
                                        "min_roi_ci_low": 0.0, "max_market_p": 0.1, "forbid_flags": ["overfit", "fragile"]}
    assert s["thresholds_paper"] == {"min_games": 10, "min_bets": 40, "min_days": 21, "min_clv": 0.0, "min_pnl_cents": 1, "clv_ci_excludes_zero": True}
    base = s["thresholds_backtest"]
    bad = [
        {"validation_seasons": [2022]}, {"validation_seasons": [2025, 2022]}, {"validation_seasons": ["2022", None]},
        {"validation_seasons": [1990, None]}, {"validation_seasons": [2021, None]}, {"validation_seasons": [2010, 2020]},
        {"backtest_seasons": [2010, 2022]}, {"backtest_seasons": [2010, 2021], "validation_seasons": [2021, None]},
        {"search_workers": 0}, {"search_workers": 65}, {"search_workers": "many"}, {"search_workers": 2.5}, {"search_workers": True},
        {"thresholds_backtest": {**base, "min_roi_ci_low": 2}}, {"thresholds_backtest": {**base, "max_market_p": 1.5}},
        {"thresholds_backtest": {**base, "max_market_p": -0.1}}, {"thresholds_backtest": {**base, "require_validation": "yes"}},
        {"thresholds_backtest": {**base, "forbid_flags": ["nope"]}}, {"thresholds_backtest": {**base, "forbid_flags": "overfit"}},
        {"thresholds_backtest": {**base, "forbid_flags": ["overfit", "overfit"]}}, {"thresholds_backtest": {**base, "extra": 1}},
        {"thresholds_backtest": {"min_bets": 50}},
        {"thresholds_paper": {"min_games": 10, "min_bets": 40, "min_days": 21, "min_clv": 0.0, "min_pnl_cents": 1, "clv_ci_excludes_zero": 1}},
    ]
    for body in bad:
        r = client.post("/api/settings", json=body)
        assert r.status_code == 400, (body, r.text)
        assert next(iter(body)) in r.json()["detail"] or "validation_seasons" in r.json()["detail"], (body, r.text)
    assert client.get("/api/settings").json() == s, "nothing changed"
    r = client.post("/api/settings", json={"backtest_seasons": [2010, 2022]})
    assert "validation_seasons must start after the search era ends (2022)" in r.json()["detail"]
    good = {
        "backtest_seasons": [2012, None], "validation_seasons": [2023, 2025], "search_workers": 3,
        "thresholds_backtest": {**base, "min_roi_ci_low": -0.01, "max_market_p": 0.05, "forbid_flags": ["overfit", "fragile", "regime_dependent"], "require_validation": False},
        "thresholds_paper": {"min_games": 10, "min_bets": 40, "min_days": 21, "min_clv": 0.0, "min_pnl_cents": 1, "clv_ci_excludes_zero": False},
    }
    r = client.post("/api/settings", json=good)
    assert r.status_code == 200, r.text
    assert client.get("/api/settings").json()["search_workers"] == 3
    assert client.post("/api/settings", json={"backtest_seasons": [2012, 2022], "validation_seasons": [2023, None]}).status_code == 200
    assert client.post("/api/settings", json={"search_workers": "auto"}).status_code == 200
    assert client.post("/api/settings", json={"thresholds_backtest": {"min_bets": 50, "min_roi": 0.02, "max_drawdown": 0.3}}).status_code == 200, "legacy shape"
    assert client.post("/api/settings", json={"thresholds_paper": {"min_games": 10, "min_bets": 40, "min_days": 21, "min_clv": 0.0, "min_pnl_cents": 1}}).status_code == 200
    audited = [a["entity"] for a in conn.execute("SELECT entity FROM audit_log WHERE action = 'settings_changed' ORDER BY id").fetchall()]
    assert audited[:5] == ["backtest_seasons", "validation_seasons", "search_workers", "thresholds_backtest", "thresholds_paper"]
