"""Fleet UI host side (docs/PROTOCOL.md "Fleet UI additions"): heartbeat machine health, the
reboot request flow, the extended /api/fleet and the /api/fleet/events feed."""
from __future__ import annotations

import dataclasses
from datetime import datetime

from fastapi.testclient import TestClient

from host.api.app import create_app
from tests.conftest import audit_rows, heartbeat_body, insert_job, worker_row

OWNER = {"Tailscale-User-Login": "owner@example.com"}
REGISTER = "/api/v1/workers/register"
HEALTH = {"temp_c": 61.5, "boot_media": "ssd", "wear_pct": 12.0, "disk_gb_written": 3.25}


def _reboot(client: TestClient, worker_id: str):
    return client.post(f"/api/workers/{worker_id}/reboot", headers=OWNER)


def _can_reboot(conn, worker_id: str, value: bool = True) -> None:
    conn.execute("UPDATE workers SET can_reboot = %s WHERE id = %s", (value, worker_id))


def _register(client: TestClient, worker_id: str, token: str, boot_id: str, **extra):
    body = {"worker_id": worker_id, "worker_token": token, "hostname": "box1", "python_version": "3.11",
            "code_version": "test", "boot_id": boot_id, **extra}
    r = client.post(REGISTER, json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ heartbeat and register fields


def test_heartbeat_stores_machine_health(client, conn, heartbeat, make_worker):
    w = make_worker("box1")
    r = heartbeat(w, **HEALTH)
    assert r.status_code == 200, r.text
    assert r.json()["reboot"] is None
    row = worker_row(conn, w.id)
    assert (row["temp_c"], row["boot_media"], row["wear_pct"], row["disk_gb_written"]) == (61.5, "ssd", 12.0, 3.25)
    r = heartbeat(w, temp_c=None, wear_pct=None, disk_gb_written=None)
    assert r.status_code == 200
    row = worker_row(conn, w.id)
    assert (row["temp_c"], row["wear_pct"], row["disk_gb_written"]) == (None, None, None), "null when unknown"
    assert row["boot_media"] == "ssd", "boot_media keeps its last value when absent"


def test_heartbeat_health_fields_are_validated(client, conn, make_worker):
    w = make_worker("box1")
    url = f"/api/v1/workers/{w.id}/heartbeat"
    bad = [
        {"temp_c": 150.5}, {"temp_c": -51}, {"temp_c": "hot"}, {"wear_pct": 100.1}, {"wear_pct": -1},
        {"disk_gb_written": -0.5}, {"boot_media": "floppy"}, {"boot_media": 3},
    ]
    for extra in bad:
        r = client.post(url, json=heartbeat_body(**extra), headers=w.headers)
        assert r.status_code == 400, (extra, r.text)
        assert next(iter(extra)) in r.json()["detail"]
    for raw in ('{"temp_c": NaN}', '{"wear_pct": Infinity}', '{"disk_gb_written": -Infinity}'):
        r = client.post(url, content=raw, headers={**w.headers, "Content-Type": "application/json"})
        assert r.status_code == 400, (raw, r.text)
    edges = {"temp_c": -50, "wear_pct": 100, "disk_gb_written": 0, "boot_media": "unknown"}
    assert client.post(url, json=heartbeat_body(**edges), headers=w.headers).status_code == 200
    assert worker_row(conn, w.id)["temp_c"] == -50


def test_register_fields_validated_and_stored(client, conn, make_worker):
    w = make_worker("box1")
    for extra in ({"boot_media": "tape"}, {"can_reboot": "perhaps"}):
        r = client.post(REGISTER, json={"worker_id": w.id, "worker_token": w.token, "boot_id": "b1", **extra})
        assert r.status_code == 400, (extra, r.text)
    body = _register(client, w.id, w.token, "b1", can_reboot=True, boot_media="flash")
    row = worker_row(conn, w.id)
    assert row["can_reboot"] is True and row["boot_media"] == "flash"
    _register(client, w.id, body["worker_token"], "b1")
    row = worker_row(conn, w.id)
    assert row["can_reboot"] is True and row["boot_media"] == "flash", "absent fields keep the stored values"


def test_register_with_new_boot_id_finishes_the_reboot(client, conn, make_worker):
    w = make_worker("box1")
    token = _register(client, w.id, w.token, "boot-a", can_reboot=True)["worker_token"]
    r = _reboot(client, w.id)
    assert r.status_code == 200, r.text
    rid = r.json()["reboot_id"]
    token = _register(client, w.id, token, "boot-a")["worker_token"]
    assert worker_row(conn, w.id)["reboot_id"] == rid, "same boot: the reboot has not happened yet"
    assert audit_rows(conn, "reboot_done") == []
    _register(client, w.id, token, "boot-b")
    row = worker_row(conn, w.id)
    assert row["reboot_id"] is None and row["reboot_requested_at"] is None and row["boot_id"] == "boot-b"
    done = audit_rows(conn, "reboot_done")
    assert len(done) == 1 and done[0]["entity"] == w.id
    assert done[0]["before"]["reboot_id"] == rid and done[0]["after"] == {"boot_id": "boot-b"}


# ------------------------------------------------------------------ the reboot request


def test_reboot_request_rules(client, conn, make_worker):
    assert _reboot(client, "w_nope00").status_code == 404
    off = make_worker("off", online=False)
    _can_reboot(conn, off.id)
    r = _reboot(client, off.id)
    assert r.status_code == 409 and r.json()["detail"] == "worker is offline"
    old = make_worker("old")
    r = _reboot(client, old.id)
    assert r.status_code == 409 and r.json()["detail"] == "this worker cannot reboot yet: re-run install.sh on it"
    assert audit_rows(conn, "reboot_requested") == []
    w = make_worker("box1")
    _can_reboot(conn, w.id)
    r = _reboot(client, w.id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"worker_id", "reboot_id", "requested_at"}
    assert body["worker_id"] == w.id and body["reboot_id"]
    datetime.fromisoformat(body["requested_at"].replace("Z", "+00:00"))
    again = _reboot(client, w.id).json()
    assert again == body, "idempotent while pending"
    conn.execute("UPDATE workers SET last_heartbeat_at = now() - interval '1 hour' WHERE id = %s", (w.id,))
    assert _reboot(client, w.id).json() == body, "a pending request answers even once the worker went down"
    rows = audit_rows(conn, "reboot_requested")
    assert len(rows) == 1 and rows[0]["entity"] == w.id and rows[0]["actor"] == "owner@example.com"
    assert rows[0]["after"] == {"reboot_id": body["reboot_id"]}


def test_heartbeat_carries_pending_reboot_and_claims_nothing(client, conn, heartbeat, make_worker):
    w = make_worker("box1", role="backtest")
    _can_reboot(conn, w.id)
    job = insert_job(conn, "backtest")
    rid = _reboot(client, w.id).json()["reboot_id"]
    r = heartbeat(w)
    assert r.status_code == 200, r.text
    assert r.json()["reboot"] == rid and r.json()["claimed"] == []
    assert conn.execute("SELECT status FROM jobs WHERE id = %s", (job["id"],)).fetchone()["status"] == "queued"
    # the request lapses after five minutes: no reboot in the reply, claims resume
    conn.execute("UPDATE workers SET reboot_requested_at = now() - interval '301 seconds' WHERE id = %s", (w.id,))
    r = heartbeat(w)
    assert r.json()["reboot"] is None
    assert [c["id"] for c in r.json()["claimed"]] == [str(job["id"])]
    fleet = {x["id"]: x for x in client.get("/api/fleet").json()["workers"]}
    assert fleet[w.id]["rebooting"] is False
    fresh = _reboot(client, w.id).json()
    assert fresh["reboot_id"] != rid, "an expired request is replaced, not returned"
    assert len(audit_rows(conn, "reboot_requested")) == 2


# ------------------------------------------------------------------ /api/fleet


def test_fleet_new_fields_and_roles(client, conn, heartbeat, make_worker):
    w = make_worker("box1")
    other = make_worker("box2", online=False)
    conn.execute("UPDATE workers SET ram_used_mb = NULL, ram_total_mb = NULL WHERE id = %s", (other.id,))
    heartbeat(w, **HEALTH)
    _can_reboot(conn, w.id)
    body = client.get("/api/fleet").json()
    assert body["online_after_seconds"] == 30
    assert body["roles"] == [
        {"id": "idle", "name": "Idle", "short": "Idle"},
        {"id": "backtest", "name": "Backtest", "short": "Backtest"},
        {"id": "model_search", "name": "Model Search", "short": "Model Search"},
        {"id": "train", "name": "Training", "short": "Training"},
        {"id": "trade", "name": "Trading", "short": "Trading"},
    ]
    assert body["settings"]["heartbeat_seconds"] == 3 and body["settings"]["online_after_seconds"] == 30
    workers = {x["name"]: x for x in body["workers"]}
    box1, box2 = workers["box1"], workers["box2"]
    assert box1["ram_pct"] == 12.5
    assert (box1["temp_c"], box1["boot_media"], box1["wear_pct"], box1["disk_gb_written"]) == (61.5, "ssd", 12.0, 3.25)
    assert isinstance(box1["seconds_since_heartbeat"], int) and 0 <= box1["seconds_since_heartbeat"] <= 5
    assert box1["can_reboot"] is True and box1["rebooting"] is False
    assert box2["ram_pct"] is None and box2["temp_c"] is None and box2["boot_media"] is None
    assert 3590 <= box2["seconds_since_heartbeat"] <= 3610
    assert box2["can_reboot"] is False and box2["rebooting"] is False
    conn.execute("UPDATE workers SET last_heartbeat_at = NULL WHERE id = %s", (other.id,))
    _reboot(client, w.id)
    workers = {x["name"]: x for x in client.get("/api/fleet").json()["workers"]}
    assert workers["box1"]["rebooting"] is True
    assert workers["box2"]["seconds_since_heartbeat"] is None
    online = conn.execute("SELECT value FROM settings WHERE key = 'online_after_seconds'").fetchone()["value"]
    assert online == 30, "migration 0010 moved the old default"


# ------------------------------------------------------------------ /api/fleet/events


def test_fleet_events_feed(client, conn, heartbeat, make_worker):
    w = make_worker("box3")
    _can_reboot(conn, w.id)
    assert client.post(f"/api/workers/{w.id}/role", json={"role": "model_search"}, headers=OWNER).status_code == 200
    client.post(f"/api/workers/{w.id}/role", json={"role": "backtest"}, headers=OWNER)
    insert_job(conn, "backtest")
    claim = heartbeat(w)
    assert len(claim.json()["claimed"]) == 1
    _reboot(client, w.id)
    client.post("/api/kill", headers=OWNER)
    r = client.get("/api/fleet/events")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"events", "server_time"}
    events = body["events"]
    for e in events:
        assert set(e) == {"key", "ts", "worker_id", "who", "tone", "text"}
        assert e["tone"] in ("ok", "hot", "off", "fg")
    keys = [e["key"] for e in events]
    assert len(set(keys)) == len(keys) and all(k[:2] in ("a:", "j:") for k in keys)
    stamps = [datetime.fromisoformat(e["ts"].replace("Z", "+00:00")) for e in events]
    assert stamps == sorted(stamps, reverse=True), "newest first"
    texts = [(e["who"], e["text"], e["tone"]) for e in events]
    assert texts[0] == ("fleet", "Kill switch on by owner@example.com", "hot")
    assert ("box3", "Reboot requested by owner@example.com", "off") in texts
    assert ("box3", "Took a Backtest job", "ok") in texts
    assert ("box3", "Moved to Model Search by owner@example.com", "ok") in texts
    assert ("box3", "Moved to Backtest by owner@example.com", "ok") in texts
    claimed = next(e for e in events if e["text"] == "Took a Backtest job")
    assert claimed["key"].startswith("j:") and claimed["worker_id"] == w.id
    moved = next(e for e in events if e["text"].startswith("Moved to Model Search"))
    assert moved["key"].startswith("a:") and moved["worker_id"] == w.id
    assert next(e for e in events if e["who"] == "fleet")["worker_id"] is None

    # limit caps the newest; since is inclusive and still capped
    two = client.get("/api/fleet/events", params={"limit": 2}).json()["events"]
    assert [e["key"] for e in two] == keys[:2]
    since = events[2]["ts"]
    newer = client.get("/api/fleet/events", params={"since": since}).json()["events"]
    assert [e["key"] for e in newer] == [e["key"] for e, ts in zip(events, stamps) if ts >= stamps[2]]
    assert events[2]["key"] in [e["key"] for e in newer], "since is inclusive"
    capped = client.get("/api/fleet/events", params={"since": since, "limit": 1}).json()["events"]
    assert [e["key"] for e in capped] == keys[:1]
    plus = since.replace("Z", "+00:00")
    assert [e["key"] for e in client.get("/api/fleet/events", params={"since": plus}).json()["events"]] == \
        [e["key"] for e in newer]
    unencoded = client.get(f"/api/fleet/events?since={plus}").json()["events"]
    assert [e["key"] for e in unencoded] == [e["key"] for e in newer], "an unencoded + (a space) still parses"
    assert client.get("/api/fleet/events", params={"since": "2999-01-01T00:00:00Z"}).json()["events"] == []

    for params in ({"limit": 0}, {"limit": 201}, {"limit": "many"}, {"since": "yesterday"}):
        r = client.get("/api/fleet/events", params=params)
        assert r.status_code == 400, (params, r.text)
    assert len(client.get("/api/fleet/events", params={"limit": 200}).json()["events"]) == len(events)


def test_fleet_events_job_texts_and_auto_kill(client, conn, make_worker):
    from host import kill
    from host.leases import fail
    from tests.conftest import lease_job

    w = make_worker("box1", role="backtest")
    job = lease_job(conn, w)
    fail(conn, job["id"], job["lease_token"], "boom", w.id)
    kill.auto_kill(conn, "clock_skew", {"skew_ms": 40000})
    events = client.get("/api/fleet/events").json()["events"]
    texts = [(e["who"], e["text"], e["tone"]) for e in events]
    assert ("box1", "A Backtest job failed", "hot") in texts
    assert texts[0] == ("fleet", "Kill switch on automatically: Clock Skew", "hot")
    assert not any(t[1].startswith("Kill switch on by") for t in texts), "the auto kill is one line"


def test_new_routes_require_the_owner(config, make_worker, conn):
    strict = dataclasses.replace(config, dev=False, owner_login="owner@example.com")
    w = make_worker("box1")
    _can_reboot(conn, w.id)
    with TestClient(create_app(strict)) as c:
        assert c.post(f"/api/workers/{w.id}/reboot").status_code == 401
        assert c.get("/api/fleet/events").status_code == 401
        assert c.post(f"/api/workers/{w.id}/reboot", headers={"Tailscale-User-Login": "x@example.com"}).status_code == 401
        assert c.post(f"/api/workers/{w.id}/reboot", headers={**OWNER, "Origin": "http://evil.example"}).status_code == 403
        assert c.get("/api/fleet/events", headers=OWNER).status_code == 200
        r = c.post(f"/api/workers/{w.id}/reboot", headers=OWNER)
        assert r.status_code == 200, r.text
    assert audit_rows(conn, "reboot_requested")[0]["actor"] == "owner@example.com"
