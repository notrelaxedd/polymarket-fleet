"""Owner routes: workloads, machines, assign refusals, pinning, drains, secrets, outbound, auth."""
from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from host.api.app import create_app
from host.workloads import assign as assign_mod, machines, outbound, pinning
from host.workloads.errors import SecretsUnavailable
from tests.conftest import insert_job, insert_model, ingest_fixture
from tests.test_wl_api_support import (  # noqa: F401
    DIGEST_A, GOOD_SPECS, _registry_env, add_workload, auth, beat, container, enroll, manifest_data,
    no_secrets_key, polymarket_data, run_token_for, secrets_key,
)

OWNER = {"Tailscale-User-Login": "owner@example.com"}


def post_assign(client, mid, workload):
    return client.post(f"/api/machines/{mid}/assign", json={"workload": workload})


def audit_actions(conn, like="%"):
    return [r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE %s ORDER BY id", (like,))]


# ------------------------------------------------------------------ workloads


def test_sync_reads_manifests_and_reports_errors(client, conn, tmp_path, monkeypatch):
    root = tmp_path / "wl"
    for name, body in {
        "hello": '''schema = 1
name = "hello"
image = "fleet/hello"
[resources]
min_ram_mb = 128
[runtime]
mode = "jobs"
job_kinds = ["hello"]
''',
        "broken": 'schema = 1\nname = "other"\nimage = "x y"\n',
        "_template": 'schema = 1\nname = "template"\n',
    }.items():
        (root / name).mkdir(parents=True)
        (root / name / "workload.toml").write_text(body)
    monkeypatch.setenv("FLEET_WORKLOADS_DIR", str(root))
    r = client.post("/api/workloads/sync")
    assert r.status_code == 200
    assert r.json()["synced"] == ["hello"] and "broken" in r.json()["errors"] and len(r.json()["errors"]["broken"]) >= 2
    listing = client.get("/api/workloads").json()
    assert [w["name"] for w in listing] == ["hello"] and listing[0]["image_published"] is False
    assert listing[0]["enabled"] is True and listing[0]["queued_jobs"] == 0 and listing[0]["machines"] == []
    from host.workloads import registry
    registry.set_image(conn, "hello", DIGEST_A, 12)
    assert client.post("/api/workloads/sync").status_code == 200
    one = client.get("/api/workloads/hello").json()
    assert one["image_digest"] == DIGEST_A and one["image_size_mb"] == 12, "a sync keeps the recorded digest"
    assert client.get("/api/workloads/nope").status_code == 404
    assert "workloads_sync" in audit_actions(conn)


def test_workload_enabled_toggle_is_audited_and_blocks_assign(client, conn):
    add_workload(conn, manifest_data("hello"))
    mid, _ = enroll(client, conn)
    r = client.post("/api/workloads/hello/enabled", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = post_assign(client, mid, "hello")
    assert r.status_code == 422 and "workload_disabled" in r.json()["detail"]
    client.post("/api/workloads/hello/enabled", json={"enabled": True})
    assert post_assign(client, mid, "hello").status_code == 200
    assert audit_actions(conn, "workload_enabled").count("workload_enabled") == 2
    assert client.post("/api/workloads/nope/enabled", json={"enabled": True}).status_code == 404


def test_set_image_validates(client, conn):
    from host.errors import BadRequest
    from host.workloads import registry

    add_workload(conn, manifest_data("hello"), digest=None)
    with pytest.raises(BadRequest):
        registry.set_image(conn, "hello", "sha256:short")
    assert registry.image_ref(registry.get_workload(conn, "hello")) is None
    registry.set_image(conn, "hello", DIGEST_A, 7)
    assert registry.image_ref(registry.get_workload(conn, "hello")) == f"reg.example.ts.net:5000/fleet/hello@{DIGEST_A}"


# ------------------------------------------------------------------ machines and assign


def test_machines_listing_has_assignment_flags_and_placement(client, conn):
    add_workload(conn, manifest_data("hello"))
    add_workload(conn, manifest_data("big", image="fleet/big", resources={"min_ram_mb": 8192, "write_heavy": True}))
    mid, token = enroll(client, conn, specs={**GOOD_SPECS, "disk_type": "flash"})
    beat(client, mid, token)
    [m] = client.get("/api/machines").json()
    assert m["id"] == mid and m["online"] is True and m["effective_disk_type"] == "flash"
    assert "token_hash" not in m and "prev_token_hash" not in m and "run_token_hash" not in m["assignment"]
    assert m["assignment"]["workload"] is None and m["placement"]["hello"] == []
    assert [r["code"] for r in m["placement"]["big"]] == ["ram_too_small", "write_heavy_on_flash"]
    conn.execute("UPDATE machines SET last_heartbeat_at = now() - interval '1 hour'")
    assert client.get("/api/machines").json()[0]["online"] is False
    client.post(f"/api/machines/{mid}/disk-type", json={"disk_type": "ssd"})
    assert client.get("/api/machines").json()[0]["effective_disk_type"] == "ssd"
    assert client.post(f"/api/machines/{mid}/disk-type", json={"disk_type": "tape"}).status_code == 400
    assert client.post(f"/api/machines/{mid}/disk-type", json={"disk_type": None}).status_code == 200
    assert client.get("/api/machines").json()[0]["effective_disk_type"] == "flash"
    assert client.post("/api/machines/m_nope00/disk-type", json={"disk_type": "ssd"}).status_code == 404


def test_assign_refusals_and_codes(client, conn):
    add_workload(conn, manifest_data("hello"))
    add_workload(conn, manifest_data("big", image="fleet/big", resources={"min_ram_mb": 8192, "min_disk_mb": 900_000,
                                                                          "write_heavy": True}), digest=None)
    mid, token = enroll(client, conn, specs={**GOOD_SPECS, "disk_type": "unknown", "docker_ok": False})
    assert post_assign(client, "m_nope00", "hello").status_code == 404
    assert post_assign(client, mid, "ghost").status_code == 404
    r = post_assign(client, mid, "big")
    assert r.status_code == 422
    for code in ("image_not_published", "docker_missing", "ram_too_small", "disk_too_small", "write_heavy_on_flash"):
        assert code in r.json()["detail"]
    assert "workload_disabled" not in r.json()["detail"]
    row = conn.execute("SELECT workload, epoch FROM workload_assignments WHERE machine_id = %s", (mid,)).fetchone()
    assert (row["workload"], row["epoch"]) == (None, 1), "a refused assign changes nothing"
    conn.execute("UPDATE machines SET docker_ok = true")
    assert post_assign(client, mid, "hello").status_code == 200
    # same workload again: a no-op, no epoch bump, no audit row
    before = len(audit_actions(conn, "workload_assign"))
    r = post_assign(client, mid, "hello")
    assert r.status_code == 200 and r.json()["epoch"] == 2
    assert len(audit_actions(conn, "workload_assign")) == before


def test_assign_refused_when_pinned_or_native_polymarket_is_active(client, conn):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    conn.execute("UPDATE machines SET pinned = true, pinned_reason = 'live trading' WHERE id = %s", (mid,))
    r = post_assign(client, mid, "hello")
    assert r.status_code == 409 and r.json()["detail"] == "pinned: live trading; unpin first"
    conn.execute("UPDATE machines SET pinned = false")
    beat(client, mid, token, native_polymarket="active")
    r = post_assign(client, mid, "hello")
    assert r.status_code == 409 and "native fleet-worker is running" in r.json()["detail"]
    assert post_assign(client, mid, None).status_code == 200, "assigning nothing to an unassigned machine is a no-op"
    beat(client, mid, token, native_polymarket="inactive")
    assert post_assign(client, mid, "hello").status_code == 200
    audit = conn.execute("SELECT * FROM audit_log WHERE action = 'workload_assign'").fetchone()
    assert audit["entity"] == mid and audit["before"]["workload"] is None and audit["after"]["workload"] == "hello"
    assert audit["actor"] == "dev"


def test_assign_switch_bumps_epoch_and_clears_the_run_token(client, conn, secrets_key):
    add_workload(conn, manifest_data("hello"))
    add_workload(conn, manifest_data("two", image="fleet/two", secrets={"container": [], "host_only": []}))
    mid, token = enroll(client, conn)
    epoch = post_assign(client, mid, "hello").json()["epoch"]
    run_token_for(client, mid, token, epoch)
    assert conn.execute("SELECT run_token_hash FROM workload_assignments").fetchone()["run_token_hash"] is not None
    r = post_assign(client, mid, "two").json()
    assert r["epoch"] == epoch + 1 and r["state"] == "pending"
    assert conn.execute("SELECT run_token_hash FROM workload_assignments").fetchone()["run_token_hash"] is None


# ------------------------------------------------------------------ pinning


def test_pin_unpin_typed_phrase(client, conn):
    mid, _ = enroll(client, conn, "box-one")
    r = client.post(f"/api/machines/{mid}/pin", json={"reason": "owner says so"})
    assert r.status_code == 200 and r.json()["pinned"] is True and r.json()["pinned_reason"] == "owner says so"
    assert client.post(f"/api/machines/{mid}/unpin", json={"confirm": "UNPIN box-two"}).status_code == 400
    assert client.post(f"/api/machines/{mid}/unpin", json={"confirm": "unpin box-one"}).status_code == 400
    assert client.post(f"/api/machines/{mid}/unpin", json={}).status_code == 400
    assert conn.execute("SELECT pinned FROM machines").fetchone()["pinned"] is True
    r = client.post(f"/api/machines/{mid}/unpin", json={"confirm": "UNPIN box-one"})
    assert r.status_code == 200 and r.json()["pinned"] is False and r.json()["pinned_reason"] is None
    audit = conn.execute("SELECT * FROM audit_log WHERE action = 'machine_unpin'").fetchone()
    assert audit["confirmation_text"] == "UNPIN box-one" and audit["entity"] == mid
    assert audit_actions(conn, "machine_pin") == ["machine_pin"]
    assert client.post("/api/machines/m_nope00/pin", json={"reason": "x"}).status_code == 404


def _live_trade_job(conn, worker_id):
    """A live assignment with a leased trade job held by `worker_id`."""
    ingest_fixture(conn)
    model = insert_model(conn)
    game = conn.execute("SELECT game_id FROM games LIMIT 1").fetchone()["game_id"]
    a = conn.execute(
        "INSERT INTO assignments (game_id, model_id, lineage_id, mode, status) VALUES (%s, %s, %s, 'live', 'active') RETURNING id",
        (game, model["id"], model["lineage_id"]),
    ).fetchone()
    from psycopg.types.json import Jsonb
    return insert_job(conn, "trade", params=Jsonb({"assignment_id": str(a["id"])}), status="leased",
                      lease_worker_id=worker_id, lease_token="00000000-0000-0000-0000-000000000001",
                      lease_expires_at=conn.execute("SELECT now() + interval '1 hour' AS t").fetchone()["t"]), a["id"]


def test_live_trading_auto_pins_and_blocks_unpin(client, conn, make_worker):
    w = make_worker("w1")
    conn.execute("UPDATE workers SET boot_id = 'boot-1' WHERE id = %s", (w.id,))
    mid, _ = enroll(client, conn, "box1", boot_id="boot-1")
    assert pinning.is_live_trading(conn, mid) is False and pinning.refresh_pins(conn) == []
    job, aid = _live_trade_job(conn, w.id)
    assert pinning.is_live_trading(conn, mid) is True
    assert pinning.refresh_pins(conn) == [mid]
    assert pinning.refresh_pins(conn) == [], "already pinned"
    m = conn.execute("SELECT * FROM machines WHERE id = %s", (mid,)).fetchone()
    assert m["pinned"] and m["pinned_reason"] == "live trading" and m["pinned_at"] is not None
    audit = conn.execute("SELECT * FROM audit_log WHERE action = 'machine_pin'").fetchone()
    assert audit["actor"] == "system"
    r = client.post(f"/api/machines/{mid}/unpin", json={"confirm": "UNPIN box1"})
    assert r.status_code == 409, "cannot unpin while trading live"
    conn.execute("UPDATE jobs SET status = 'succeeded' WHERE id = %s", (job["id"],))
    assert pinning.is_live_trading(conn, mid) is False
    assert conn.execute("SELECT pinned FROM machines").fetchone()["pinned"] is True, "pins are sticky"
    assert client.post(f"/api/machines/{mid}/unpin", json={"confirm": "UNPIN box1"}).status_code == 200


def test_paper_trading_does_not_pin(client, conn, make_worker):
    w = make_worker("w1")
    conn.execute("UPDATE workers SET boot_id = 'boot-1' WHERE id = %s", (w.id,))
    mid, _ = enroll(client, conn, "box1", boot_id="boot-1")
    job, aid = _live_trade_job(conn, w.id)
    conn.execute("UPDATE assignments SET mode = 'paper' WHERE id = %s", (aid,))
    assert pinning.is_live_trading(conn, mid) is False


# ------------------------------------------------------------------ draining polymarket


def test_leaving_polymarket_drains_the_linked_worker_first(client, conn, make_worker):
    add_workload(conn, polymarket_data(), size_mb=300)
    w = make_worker("w1", role="trade")
    conn.execute("UPDATE workers SET boot_id = 'boot-1' WHERE id = %s", (w.id,))
    mid, token = enroll(client, conn, "box1", boot_id="boot-1", specs={**GOOD_SPECS, "ram_total_mb": 8000})
    epoch = post_assign(client, mid, "polymarket").json()["epoch"]
    beat(client, mid, token, acked_epoch=epoch, container=container("polymarket", epoch))
    row = conn.execute("SELECT desired_role, role_epoch FROM workers WHERE id = %s", (w.id,)).fetchone()
    assert row["desired_role"] == "trade"
    r = post_assign(client, mid, None)
    assert r.status_code == 200 and r.json()["state"] == "draining" and r.json()["epoch"] == epoch
    assert (r.json()["draining_to"], r.json()["draining_to_set"]) == (None, True)
    worker = conn.execute("SELECT desired_role, role_epoch FROM workers WHERE id = %s", (w.id,)).fetchone()
    assert worker["desired_role"] == "idle" and worker["role_epoch"] == row["role_epoch"] + 1
    reply = beat(client, mid, token, container=container("polymarket", epoch)).json()
    assert (reply["workload"], reply["epoch"]) == ("polymarket", epoch), "the container stays up while draining"
    assert reply["run"] is not None and reply["run"]["env"]["FLEET_EPOCH"] == str(epoch)
    assert client.post(f"/api/machines/{mid}/enabled", json={"enabled": False}).status_code == 409
    assert assign_mod.finish_drains(conn) == 0, "worker has not acked idle yet"
    assert post_assign(client, mid, "polymarket").status_code == 409, "cannot cancel a drain"
    conn.execute("UPDATE workers SET reported_role = 'idle', acked_epoch = role_epoch, last_heartbeat_at = now() WHERE id = %s", (w.id,))
    assert assign_mod.finish_drains(conn) == 1
    a = conn.execute("SELECT * FROM workload_assignments WHERE machine_id = %s", (mid,)).fetchone()
    assert (a["workload"], a["epoch"], a["state"], a["draining_to"], a["draining_to_set"]) == (None, epoch + 1, "stopped", None, False)
    assert assign_mod.finish_drains(conn) == 0
    assert audit_actions(conn, "workload_assign").count("workload_assign") == 3


def test_drain_waits_for_the_workers_leased_jobs_and_ends_when_it_goes_silent(client, conn, make_worker):
    add_workload(conn, polymarket_data(), size_mb=300)
    add_workload(conn, manifest_data("hello"))
    w = make_worker("w1", role="idle")
    conn.execute("UPDATE workers SET boot_id = 'boot-2' WHERE id = %s", (w.id,))
    mid, token = enroll(client, conn, "box1", boot_id="boot-2", specs={**GOOD_SPECS, "ram_total_mb": 8000})
    epoch = post_assign(client, mid, "polymarket").json()["epoch"]
    beat(client, mid, token, acked_epoch=epoch, container=container("polymarket", epoch))
    post_assign(client, mid, "hello")
    job = insert_job(conn, "sleep", status="leased", lease_worker_id=w.id, lease_token="00000000-0000-0000-0000-000000000002",
                     lease_expires_at=conn.execute("SELECT now() + interval '1 hour' AS t").fetchone()["t"])
    conn.execute("UPDATE workers SET reported_role = 'idle', acked_epoch = role_epoch, last_heartbeat_at = now() WHERE id = %s", (w.id,))
    assert assign_mod.finish_drains(conn) == 0, "still holds a leased job"
    conn.execute("UPDATE workers SET last_heartbeat_at = now() - interval '1 hour' WHERE id = %s", (w.id,))
    assert assign_mod.finish_drains(conn) == 1, "a silent worker no longer blocks"
    a = conn.execute("SELECT workload, state FROM workload_assignments WHERE machine_id = %s", (mid,)).fetchone()
    assert (a["workload"], a["state"]) == ("hello", "pending")
    assert job["id"]


# ------------------------------------------------------------------ secrets


def test_secrets_are_write_only_and_never_audited(client, conn, secrets_key, caplog):
    add_workload(conn, manifest_data("hello"))
    assert client.get("/api/workloads/hello/secrets").json() == [
        {"name": "HELLO_GREETING", "scope": "container", "declared": True, "set": False, "updated_at": None},
        {"name": "SMTP_URL", "scope": "host_only", "declared": True, "set": False, "updated_at": None},
        {"name": "EMAIL_FROM", "scope": "host_only", "declared": True, "set": False, "updated_at": None},
    ]
    value = "TOP-SECRET-VALUE-123"
    r = client.put("/api/workloads/hello/secrets/HELLO_GREETING", json={"value": value})
    assert r.status_code == 200 and value not in r.text and r.json()["name"] == "HELLO_GREETING"
    listing = client.get("/api/workloads/hello/secrets")
    assert value not in listing.text and listing.json()[0]["set"] is True and listing.json()[0]["updated_at"]
    stored = conn.execute("SELECT * FROM workload_secrets").fetchone()
    assert value.encode() not in bytes(stored["ciphertext"]) and len(bytes(stored["nonce"])) == 24
    dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log").fetchall()], default=str)
    assert value not in dump and "secret_set" in dump
    for path in ("/api/workloads/hello", "/api/machines", "/api/outbound", "/api/workload-jobs"):
        assert value not in client.get(path).text
    assert client.put("/api/workloads/hello/secrets/NOT_DECLARED", json={"value": "x"}).status_code == 400
    assert client.put("/api/workloads/hello/secrets/HELLO_GREETING", json={"value": "x" * 17000}).status_code == 400
    assert client.put("/api/workloads/ghost/secrets/X", json={"value": "x"}).status_code == 404
    assert client.delete("/api/workloads/hello/secrets/HELLO_GREETING").status_code == 204
    assert client.delete("/api/workloads/hello/secrets/HELLO_GREETING").status_code == 404
    assert audit_actions(conn, "secret_%") == ["secret_set", "secret_delete"]
    assert value not in caplog.text


def test_secrets_unavailable_without_a_key(client, conn, no_secrets_key):
    add_workload(conn, manifest_data("hello"))
    r = client.put("/api/workloads/hello/secrets/HELLO_GREETING", json={"value": "x"})
    assert r.status_code == 409 and "FLEET_SECRETS_KEY" in r.json()["detail"]
    assert client.get("/api/workloads/hello/secrets").status_code == 200, "names are listable without a key"


def test_secrets_for_machine_are_epoch_fenced_and_container_scoped(client, conn, secrets_key):
    from host.workloads import secrets as wl_secrets

    add_workload(conn, manifest_data("hello"))
    for name, value in (("HELLO_GREETING", "hi"), ("SMTP_URL", "smtps://u:p@h:465")):
        client.put(f"/api/workloads/hello/secrets/{name}", json={"value": value})
    mid, _ = enroll(client, conn)
    epoch = post_assign(client, mid, "hello").json()["epoch"]
    assert wl_secrets.secrets_for_machine(conn, mid, epoch) == {"HELLO_GREETING": "hi"}
    assert wl_secrets.host_only_secrets(conn, "hello") == {"SMTP_URL": "smtps://u:p@h:465"}
    from host.errors import Conflict
    with pytest.raises(Conflict):
        wl_secrets.secrets_for_machine(conn, mid, epoch - 1)
    v1 = wl_secrets.secrets_version(conn, "hello")
    client.put("/api/workloads/hello/secrets/HELLO_GREETING", json={"value": "hello again"})
    assert wl_secrets.secrets_version(conn, "hello") != v1
    client.put("/api/workloads/hello/secrets/SMTP_URL", json={"value": "smtps://other"})
    v2 = wl_secrets.secrets_version(conn, "hello")
    client.put("/api/workloads/hello/secrets/EMAIL_FROM", json={"value": "a@b.c"})
    assert wl_secrets.secrets_version(conn, "hello") == v2, "host-only secrets do not change the container version"


def test_a_wrong_or_missing_key_cannot_decrypt(client, conn, monkeypatch, secrets_key):
    import base64
    import os

    from host.workloads import secrets as wl_secrets

    add_workload(conn, manifest_data("hello"))
    client.put("/api/workloads/hello/secrets/SMTP_URL", json={"value": "smtps://a"})
    monkeypatch.setenv("FLEET_SECRETS_KEY", base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(SecretsUnavailable):
        wl_secrets.host_only_secrets(conn, "hello")
    monkeypatch.setenv("FLEET_SECRETS_KEY", "not base64!")
    with pytest.raises(SecretsUnavailable):
        wl_secrets.secret_box()
    monkeypatch.delenv("FLEET_SECRETS_KEY")
    assert wl_secrets.secret_box() is None


# ------------------------------------------------------------------ outbound


class FakeSender:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[dict, dict]] = []
        self.fail = fail

    def send(self, action, host_secrets):
        self.sent.append((action, host_secrets))
        if self.fail:
            raise RuntimeError(f"smtp down for {host_secrets.get('SMTP_URL')}")
        return {"ok": True}


def queue_one(conn, kind="email", key="k1", **payload):
    return outbound.queue_action(conn, workload="hello", machine_id=None, job_id=None, kind=kind,
                                 payload=payload or {"to": "a@b.c", "subject": "s", "body": "b"}, dedupe_key=key)


def test_outbound_approve_reject_and_send(client, conn, secrets_key):
    add_workload(conn, manifest_data("hello"))
    client.put("/api/workloads/hello/secrets/SMTP_URL", json={"value": "smtps://u:pw@h:465"})
    a, b = queue_one(conn, key="a"), queue_one(conn, key="b")
    listing = client.get("/api/outbound?status=pending").json()
    assert {x["id"] for x in listing} == {str(a["id"]), str(b["id"])} and listing[0]["payload"]["to"] == "a@b.c"
    fake = FakeSender()
    assert outbound.send_approved(conn, {"email": fake}) == 0 and fake.sent == [], "nothing is sent unless approved"
    assert client.post(f"/api/outbound/{a['id']}/approve").json()["status"] == "approved"
    assert client.post(f"/api/outbound/{a['id']}/approve").status_code == 409
    assert client.post(f"/api/outbound/{b['id']}/reject", json={"reason": "no thanks"}).json()["status"] == "rejected"
    assert client.post(f"/api/outbound/{b['id']}/approve").status_code == 409, "a rejected action cannot be approved"
    assert client.post("/api/outbound/not-a-uuid/approve").status_code == 404
    assert outbound.send_approved(conn, {"email": fake}) == 1
    [(action, secrets)] = fake.sent
    assert action["id"] == a["id"] and secrets == {"SMTP_URL": "smtps://u:pw@h:465"}
    row = conn.execute("SELECT * FROM outbound_actions WHERE id = %s", (a["id"],)).fetchone()
    assert (row["status"], row["result"], row["decided_by"]) == ("sent", {"ok": True}, "dev") and row["sent_at"]
    assert outbound.send_approved(conn, {"email": fake}) == 0, "sent once only"
    done = client.get("/api/outbound?status=sent").json()
    assert [x["id"] for x in done] == [str(a["id"])]
    assert client.get("/api/outbound?status=bogus").status_code == 400
    assert audit_actions(conn, "outbound_%") == ["outbound_approve", "outbound_reject"]
    rej = conn.execute("SELECT * FROM outbound_actions WHERE id = %s", (b["id"],)).fetchone()
    assert rej["result"] == {"reason": "no thanks"}


def test_outbound_failure_is_recorded_and_redacted(client, conn, secrets_key):
    add_workload(conn, manifest_data("hello"))
    client.put("/api/workloads/hello/secrets/SMTP_URL", json={"value": "smtps://u:pw@h:465"})
    a = queue_one(conn)
    client.post(f"/api/outbound/{a['id']}/approve")
    assert outbound.send_approved(conn, {"email": FakeSender(fail=True)}) == 0
    row = conn.execute("SELECT * FROM outbound_actions").fetchone()
    assert row["status"] == "failed" and "smtp down" in row["error"] and "smtps://u:pw@h:465" not in row["error"]
    c = queue_one(conn, key="c")
    client.post(f"/api/outbound/{c['id']}/approve")
    assert outbound.send_approved(conn, {}) == 0
    assert "no sender" in conn.execute("SELECT error FROM outbound_actions WHERE id = %s", (c["id"],)).fetchone()["error"]


def test_outbound_dedupe_kinds_and_expiry(client, conn):
    add_workload(conn, manifest_data("hello", outbound={"actions": ["log"]}, secrets={"container": [], "host_only": []}))
    a = queue_one(conn, kind="log", key="same", x=1)
    again = queue_one(conn, kind="log", key="same", x=2)
    assert again["id"] == a["id"] and again["payload"] == {"x": 1}
    from host.errors import Forbidden
    with pytest.raises(Forbidden):
        queue_one(conn, kind="email", key="e")
    conn.execute("UPDATE outbound_actions SET created_at = now() - interval '8 days'")
    assert outbound.expire_old(conn) == 1
    assert conn.execute("SELECT status FROM outbound_actions").fetchone()["status"] == "expired"
    assert outbound.expire_old(conn) == 0


def test_log_sender_writes_a_job_event_and_email_sender_uses_smtp(client, conn):
    from host.workloads import queue
    from host.workloads.senders import EmailSender, LogSender

    add_workload(conn, manifest_data("hello"))
    job = queue.create_job(conn, workload="hello", kind="hello", params={})
    action = outbound.queue_action(conn, workload="hello", machine_id=None, job_id=job["id"], kind="log",
                                   payload={"m": 1}, dedupe_key="hello:1")
    assert LogSender(conn).send(action, {}) == {"logged": True}
    events = conn.execute("SELECT event FROM workload_job_events WHERE job_id = %s ORDER BY id", (job["id"],)).fetchall()
    assert [e["event"] for e in events] == ["created", "outbound_sent"]

    calls: list = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, pw):
            calls.append(("login", user, pw))

        def send_message(self, msg):
            calls.append(("send", msg["To"], msg["Subject"], msg["From"], msg.get_content().strip()))

    mail = {"payload": {"to": ["x@y.z"], "subject": "Hi", "body": "Body text"}}
    sender = EmailSender(smtp_ssl=FakeSMTP, smtp=FakeSMTP)
    out = sender.send(mail, {"SMTP_URL": "smtps://us%40er:p%3Aw@mail.example:465", "EMAIL_FROM": "fleet@example.com"})
    assert out == {"sent_to": ["x@y.z"]}
    assert calls == [("connect", "mail.example", 465), ("login", "us@er", "p:w"),
                     ("send", "x@y.z", "Hi", "fleet@example.com", "Body text")]
    with pytest.raises(ValueError):
        sender.send(mail, {})
    with pytest.raises(ValueError):
        sender.send({"payload": {"to": "x@y.z", "subject": "a\nBcc: evil@x.y", "body": "b"}},
                    {"SMTP_URL": "smtps://h", "EMAIL_FROM": "f@e.c"})
    with pytest.raises(ValueError):
        sender.send({"payload": {"to": "not an address", "subject": "a", "body": "b"}},
                    {"SMTP_URL": "smtps://h", "EMAIL_FROM": "f@e.c"})


# ------------------------------------------------------------------ jobs and logs


def test_workload_job_api_validation_and_idempotency(client, conn):
    add_workload(conn, manifest_data("hello"))
    r = client.post("/api/workload-jobs", json={"workload": "hello", "kind": "hello", "params": {"a": 1}, "idempotency_key": "k"})
    assert r.status_code == 201 and r.json()["status"] == "queued" and "lease_token" not in r.json()
    again = client.post("/api/workload-jobs", json={"workload": "hello", "kind": "hello", "idempotency_key": "k"})
    assert again.status_code == 200 and again.json()["id"] == r.json()["id"]
    assert client.post("/api/workload-jobs", json={"workload": "hello", "kind": "bad"}).status_code == 400
    assert client.post("/api/workload-jobs", json={"workload": "ghost", "kind": "hello"}).status_code == 404
    assert client.post("/api/workload-jobs", json={"workload": "hello", "kind": "hello", "target": "m_nope00"}).status_code == 404
    assert len(client.get("/api/workload-jobs?workload=hello&status=queued&limit=5").json()) == 1
    assert client.get("/api/workload-jobs?status=weird").status_code == 400
    assert client.get("/api/workload-jobs/garbage").status_code == 404
    assert audit_actions(conn, "workload_job_create") == ["workload_job_create"]


def test_machine_logs_route(client, conn):
    mid, token = enroll(client, conn)
    lines = [{"ts": f"2026-10-06T20:00:0{i}Z", "stream": "stdout", "line": f"line {i}"} for i in range(5)]
    beat(client, mid, token, logs=lines)
    got = client.get(f"/api/machines/{mid}/logs?limit=3").json()
    assert [x["line"] for x in got] == ["line 2", "line 3", "line 4"], "newest last"
    assert client.get("/api/machines/m_nope00/logs").status_code == 404


def test_enroll_token_route_and_cli_style_install_command(client, conn):
    r = client.post("/api/machine-enroll-token")
    assert r.status_code == 200
    token = r.json()["token"]
    assert r.json()["install_command"].endswith(token) and "/install-agent.sh" in r.json()["install_command"]
    assert audit_actions(conn, "machine_enroll_token") == ["machine_enroll_token"]
    reply = client.post("/api/v1/machines/register", json={"enroll_token": token, "name": "n1"})
    assert reply.status_code == 200


# ------------------------------------------------------------------ owner auth


def test_new_owner_routes_need_the_owner_login(config):
    strict = dataclasses.replace(config, dev=False, owner_login="owner@example.com")
    with TestClient(create_app(strict)) as c:
        for method, path in [("get", "/api/workloads"), ("get", "/api/machines"), ("get", "/api/outbound"),
                             ("get", "/api/workload-jobs"), ("post", "/api/machine-enroll-token"),
                             ("post", "/api/workloads/sync"), ("get", "/api/workloads/x/secrets")]:
            assert getattr(c, method)(path).status_code == 401, path
            assert getattr(c, method)(path, headers=OWNER).status_code in (200, 404), path
        r = c.post("/api/machine-enroll-token", headers={**OWNER, "Origin": "http://evil.example"})
        assert r.status_code == 403, "the Origin check applies"


def test_owner_routes_are_refused_from_a_machine_ip(config, conn):
    strict = dataclasses.replace(config, dev=False, owner_login="owner@example.com")
    with TestClient(create_app(strict)) as c:
        token = c.post("/api/enroll-token", headers=OWNER).json()
        mtoken = c.post("/api/machine-enroll-token", headers=OWNER).json()["token"]
        r = c.post("/api/v1/machines/register", json={"enroll_token": mtoken, "name": "box1"},
                   headers={"X-Forwarded-For": "9.9.9.9, 100.64.0.5"})
        mid = r.json()["machine_id"]
        assert conn.execute("SELECT remote_ip FROM machines WHERE id = %s", (mid,)).fetchone()["remote_ip"] == "100.64.0.5"
        denied = c.get("/api/machines", headers={**OWNER, "X-Forwarded-For": "100.64.0.5"})
        assert denied.status_code == 403 and mid in denied.json()["detail"] and "machine" in denied.json()["detail"]
        assert c.get("/api/fleet", headers={**OWNER, "X-Forwarded-For": "100.64.0.5"}).status_code == 403
        assert c.get("/api/machines", headers={**OWNER, "X-Forwarded-For": "100.64.0.6"}).status_code == 200
        assert token["token"]
    allowed = dataclasses.replace(strict, allow_worker_ips=True)
    with TestClient(create_app(allowed)) as c:
        assert c.get("/api/machines", headers={**OWNER, "X-Forwarded-For": "100.64.0.5"}).status_code == 200
