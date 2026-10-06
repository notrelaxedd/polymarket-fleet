"""Supervisor routes: register, token rotation, heartbeat (specs, logs, run block), /start."""
from __future__ import annotations

from host.auth import hash_token
from host.workloads import machines
from tests.test_wl_api_support import (  # noqa: F401
    DIGEST_A, DIGEST_B, GOOD_SPECS, _registry_env, add_workload, auth, beat, container, enroll, manifest_data,
    no_secrets_key, polymarket_data, run_token_for, secrets_key,
)


def assign(client, machine_id, workload):
    r = client.post(f"/api/machines/{machine_id}/assign", json={"workload": workload})
    assert r.status_code == 200, r.text
    return r.json()


def test_register_enrolls_and_creates_the_assignment_row(client, conn):
    mid, token = enroll(client, conn, "box1", boot_id="boot-1", ip="100.64.0.7")
    assert mid.startswith("m_") and len(mid) == 8
    row = conn.execute("SELECT * FROM machines WHERE id = %s", (mid,)).fetchone()
    assert row["name"] == "box1" and row["remote_ip"] == "100.64.0.7" and row["boot_id"] == "boot-1"
    assert row["token_hash"] == hash_token(token) and token not in str(dict(row))
    assert row["ram_total_mb"] == 3800 and row["disk_type_detected"] == "ssd" and row["docker_ok"] is True
    a = conn.execute("SELECT * FROM workload_assignments WHERE machine_id = %s", (mid,)).fetchone()
    assert (a["workload"], a["epoch"], a["state"]) == (None, 1, "stopped")
    audit = conn.execute("SELECT action FROM audit_log WHERE entity = %s", (mid,)).fetchall()
    assert [r["action"] for r in audit] == ["machine_enrolled"]


def test_register_reply_fields_and_enroll_token_is_single_use(client, conn):
    token = machines.create_enroll_token(conn)["token"]
    body = {"enroll_token": token, "name": "box1", "hostname": "box1", "specs": GOOD_SPECS}
    r = client.post("/api/v1/machines/register", json=body)
    assert r.status_code == 200
    reply = r.json()
    assert {"machine_id", "machine_token", "heartbeat_seconds", "server_time", "agent_version"} <= set(reply)
    assert reply["heartbeat_seconds"] == 5 and reply["agent_version"] is None, "fleetagent absent: no version"
    assert client.post("/api/v1/machines/register", json=body).status_code == 401
    assert client.post("/api/v1/machines/register", json={"enroll_token": "nope", "name": "x"}).status_code == 401
    assert client.post("/api/v1/machines/register", json={"name": "x"}).status_code == 401
    bad = machines.create_enroll_token(conn)["token"]
    assert client.post("/api/v1/machines/register", json={"enroll_token": bad, "name": "bad name!"}).status_code == 400


def test_expired_enroll_token_is_refused(client, conn):
    token = machines.create_enroll_token(conn, ttl_seconds=1)["token"]
    conn.execute("UPDATE machine_enroll_tokens SET expires_at = now() - interval '1 minute'")
    assert client.post("/api/v1/machines/register", json={"enroll_token": token, "name": "x"}).status_code == 401


def test_reregister_rotates_the_token_and_the_previous_one_retries_once(client, conn):
    mid, t1 = enroll(client, conn)
    r = client.post("/api/v1/machines/register", json={"machine_id": mid, "machine_token": t1, "boot_id": "b2"})
    assert r.status_code == 200
    t2 = r.json()["machine_token"]
    assert t2 != t1
    assert beat(client, mid, t1).status_code == 401, "the old token never works for a heartbeat"
    # the reply was lost: retry register with the previous token, which still works
    r = client.post("/api/v1/machines/register", json={"machine_id": mid, "machine_token": t1})
    assert r.status_code == 200
    t3 = r.json()["machine_token"]
    assert beat(client, mid, t3).status_code == 200
    row = conn.execute("SELECT prev_token_hash, boot_id FROM machines WHERE id = %s", (mid,)).fetchone()
    assert row["prev_token_hash"] is None, "the first heartbeat with the current token clears the previous"
    assert row["boot_id"] == "b2", "a register without boot_id keeps the old one"
    assert client.post("/api/v1/machines/register", json={"machine_id": mid, "machine_token": t1}).status_code == 401
    assert client.post("/api/v1/machines/register", json={"machine_id": mid, "machine_token": "x"}).status_code == 401


def test_heartbeat_requires_the_machine_token_for_that_machine(client, conn):
    a, ta = enroll(client, conn, "box-a")
    b, tb = enroll(client, conn, "box-b")
    assert beat(client, a, ta).status_code == 200
    assert beat(client, a, tb).status_code == 401, "another machine's token"
    assert beat(client, "m_zzzzzz", ta).status_code == 401
    assert client.post(f"/api/v1/machines/{a}/heartbeat", json={}).status_code == 401, "no bearer"


def test_heartbeat_stores_specs_and_returns_the_empty_run_block(client, conn):
    mid, token = enroll(client, conn)
    specs = {**GOOD_SPECS, "ram_total_mb": 7800, "disk_type": "flash", "cpu_pct": 12.5, "docker_ok": False}
    r = beat(client, mid, token, specs=specs, native_polymarket="inactive", acked_epoch=1,
             cleanup={"images_removed": 1, "bytes_freed": 5, "low_disk": True})
    assert r.status_code == 200
    reply = r.json()
    assert reply["epoch"] == 1 and reply["workload"] is None and reply["run"] is None
    assert reply["secrets_version"] is None and reply["keep_images"] == [] and reply["heartbeat_seconds"] == 5
    m = conn.execute("SELECT * FROM machines WHERE id = %s", (mid,)).fetchone()
    assert (m["ram_total_mb"], m["disk_type_detected"], m["cpu_pct"], m["docker_ok"]) == (7800, "flash", 12.5, False)
    assert m["native_polymarket"] == "inactive" and m["low_disk"] is True and m["last_heartbeat_at"] is not None


def test_heartbeat_ignores_garbage_specs(client, conn):
    mid, token = enroll(client, conn)
    r = beat(client, mid, token, specs={"ram_total_mb": "lots", "disk_type": "tape", "cpu_count": -3,
                                        "docker_ok": "yes", "arch": 5}, native_polymarket="sideways")
    assert r.status_code == 200
    m = conn.execute("SELECT * FROM machines WHERE id = %s", (mid,)).fetchone()
    assert m["ram_total_mb"] == 3800 and m["disk_type_detected"] == "ssd" and m["native_polymarket"] == "absent"


def test_heartbeat_logs_are_stored_and_limited(client, conn):
    mid, token = enroll(client, conn)
    logs = [{"ts": "2026-10-06T20:00:00Z", "stream": "stdout", "line": "hello\x00 world"},
            {"ts": "bad", "stream": "weird", "line": "x" * 5000}]
    assert beat(client, mid, token, logs=logs).status_code == 200
    rows = conn.execute("SELECT * FROM machine_logs WHERE machine_id = %s ORDER BY id", (mid,)).fetchall()
    assert [r["line"][:11] for r in rows] == ["hello world", "x" * 11]
    assert len(rows[1]["line"]) == 2048 and rows[1]["stream"] == "stdout"
    assert rows[0]["ts"].year == 2026

    many = [{"ts": "2026-10-06T20:00:00Z", "stream": "stderr", "line": f"l{i}"} for i in range(260)]
    assert beat(client, mid, token, logs=many).status_code == 200
    n = conn.execute("SELECT count(*) AS n FROM machine_logs WHERE stream = 'stderr'").fetchone()["n"]
    assert n == 200, "at most 200 entries per heartbeat"

    conn.execute("DELETE FROM machine_logs")
    big = [{"ts": "2026-10-06T20:00:00Z", "stream": "agent", "line": "y" * 2000} for _ in range(60)]
    assert beat(client, mid, token, logs=big).status_code == 200
    total = conn.execute("SELECT sum(length(line)) AS s, count(*) AS n FROM machine_logs").fetchone()
    assert total["s"] <= 64 * 1024 and 30 <= total["n"] <= 33, "64 KiB total per heartbeat"


def test_run_block_for_an_assigned_workload(client, conn):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    row = assign(client, mid, "hello")
    assert row["epoch"] == 2 and row["workload"] == "hello" and row["state"] == "pending"
    assert "run_token_hash" not in row
    reply = beat(client, mid, token, acked_epoch=1).json()
    assert reply["epoch"] == 2 and reply["workload"] == "hello"
    run = reply["run"]
    assert run["image"] == f"reg.example.ts.net:5000/fleet/hello@{DIGEST_A}"
    assert (run["protocol"], run["mode"], run["network"], run["uts_host"]) == ("workload-v1", "jobs", "bridge", False)
    assert (run["uid"], run["memory_mb"], run["cpus"], run["nice"], run["stop_timeout_s"]) == (10001, 256, 1.0, 0, 15)
    assert (run["state_volume"], run["scratch_mb"], run["no_restart_exit_codes"]) == (False, 512, [78])
    assert run["env"] == {"FLEET_HOST_URL": "http://127.0.0.1:8080", "FLEET_WORKLOAD": "hello",
                          "FLEET_MACHINE_ID": mid, "FLEET_EPOCH": "2", "FLEET_NICE": "0"}
    assert len(reply["secrets_version"]) == 16 and reply["keep_images"] == [DIGEST_A]
    a = conn.execute("SELECT acked_epoch FROM workload_assignments WHERE machine_id = %s", (mid,)).fetchone()
    assert a["acked_epoch"] == 1


def test_run_block_memory_percent_and_pending_states(client, conn):
    add_workload(conn, polymarket_data(), size_mb=300)
    mid, token = enroll(client, conn, specs={**GOOD_SPECS, "ram_total_mb": 4000})
    assign(client, mid, "polymarket")
    run = beat(client, mid, token).json()["run"]
    assert run["memory_mb"] == 3400 and run["network"] == "host" and run["uts_host"] is True
    assert run["state_volume"] is True and run["nice"] == 5 and run["mode"] == "service"


def test_run_block_is_null_when_unpublished_disabled_or_unassigned(client, conn):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    assign(client, mid, "hello")
    assert beat(client, mid, token).json()["run"] is not None
    conn.execute("UPDATE workloads SET image_digest = NULL WHERE name = 'hello'")
    assert beat(client, mid, token).json()["run"] is None, "image not published"
    conn.execute("UPDATE workloads SET image_digest = %s WHERE name = 'hello'", (DIGEST_A,))
    assert beat(client, mid, token).json()["run"] is not None
    assert client.post(f"/api/machines/{mid}/enabled", json={"enabled": False}).status_code == 200
    reply = beat(client, mid, token).json()
    assert reply["run"] is None and reply["workload"] == "hello", "machine disabled"
    client.post(f"/api/machines/{mid}/enabled", json={"enabled": True})
    conn.execute("UPDATE workload_assignments SET state = 'draining' WHERE machine_id = %s", (mid,))
    assert beat(client, mid, token).json()["run"] is not None, "a draining container keeps running"
    conn.execute("UPDATE workload_assignments SET state = 'pending' WHERE machine_id = %s", (mid,))
    assign(client, mid, None)
    reply = beat(client, mid, token).json()
    assert reply["run"] is None and reply["workload"] is None


def test_container_block_drives_the_assignment_state(client, conn):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    epoch = assign(client, mid, "hello")["epoch"]

    def state():
        return conn.execute("SELECT * FROM workload_assignments WHERE machine_id = %s", (mid,)).fetchone()

    beat(client, mid, token, acked_epoch=epoch, container=container("hello", epoch, "running", restarts=2,
                                                                      error="oops"))
    a = state()
    assert (a["state"], a["container_id"], a["image_digest_running"], a["restarts"]) == ("running", "c0ffee", DIGEST_A, 2)
    assert (a["cpu_pct"], a["mem_mb"], a["last_error"], a["acked_epoch"]) == (1.5, 40, "oops", epoch)
    assert a["started_at"].year == 2026
    beat(client, mid, token, acked_epoch=epoch, container=container("hello", epoch, "failed", exit_code=78))
    assert (state()["state"], state()["last_exit_code"]) == ("failed", 78)
    beat(client, mid, token, acked_epoch=epoch, container=container("hello", epoch, "exited", exit_code=1))
    assert state()["state"] == "starting", "exited: the supervisor restarts it"
    beat(client, mid, token, acked_epoch=epoch, container=container("other", epoch, "running"))
    assert state()["state"] == "pending" and state()["container_id"] is None, "a container of another workload"
    beat(client, mid, token, acked_epoch=epoch, container=container("hello", epoch - 1, "running"))
    assert state()["state"] == "pending", "an older epoch does not count"
    beat(client, mid, token, acked_epoch=epoch, container=None)
    assert state()["state"] == "pending"
    beat(client, mid, token, acked_epoch=epoch + 50)
    assert state()["acked_epoch"] == epoch, "an ack never passes the current epoch"
    beat(client, mid, token, acked_epoch=0)
    assert state()["acked_epoch"] == epoch, "an ack never moves backwards"


def test_keep_images_lists_the_current_and_the_running_digest(client, conn):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    epoch = assign(client, mid, "hello")["epoch"]
    beat(client, mid, token, acked_epoch=epoch, container=container("hello", epoch))
    conn.execute("UPDATE workloads SET image_digest = %s WHERE name = 'hello'", (DIGEST_B,))
    assert beat(client, mid, token).json()["keep_images"] == [DIGEST_B, DIGEST_A]


def test_start_returns_a_run_token_and_secrets_no_store(client, conn, secrets_key):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    assert client.put("/api/workloads/hello/secrets/HELLO_GREETING", json={"value": "Howdy"}).status_code == 200
    epoch = assign(client, mid, "hello")["epoch"]
    r = client.post(f"/api/v1/machines/{mid}/start", json={"epoch": epoch}, headers=auth(token))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["secrets"] == {"HELLO_GREETING": "Howdy"} and len(body["run_token"]) > 30
    a = conn.execute("SELECT * FROM workload_assignments WHERE machine_id = %s", (mid,)).fetchone()
    assert a["run_token_hash"] == hash_token(body["run_token"]) and a["state"] == "starting"
    second = run_token_for(client, mid, token, epoch)
    assert second["run_token"] != body["run_token"], "each start mints a new token"
    claim = client.post("/api/v1/wl/claim", json={}, headers=auth(body["run_token"]))
    assert claim.status_code == 401, "the old run token stops working"
    assert client.post("/api/v1/wl/claim", json={}, headers=auth(second["run_token"])).status_code == 200


def test_start_is_fenced_by_epoch_and_assignment(client, conn, secrets_key):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    post = lambda epoch, tok=token: client.post(f"/api/v1/machines/{mid}/start", json={"epoch": epoch}, headers=auth(tok))
    assert post(1).status_code == 409, "nothing assigned"
    epoch = assign(client, mid, "hello")["epoch"]
    assert post(epoch - 1).status_code == 409 and post(epoch + 1).status_code == 409
    assert post(epoch, "bad-token").status_code == 401
    secret_run = run_token_for(client, mid, token, epoch)["run_token"]
    new_epoch = assign(client, mid, None)["epoch"]
    assert new_epoch == epoch + 1 and post(epoch).status_code == 409, "stale epoch after reassignment"
    assert client.post("/api/v1/wl/claim", json={}, headers=auth(secret_run)).status_code == 401, "token cleared on change"


def test_start_refuses_without_a_published_image_or_when_disabled(client, conn, secrets_key):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    epoch = assign(client, mid, "hello")["epoch"]
    conn.execute("UPDATE workloads SET image_digest = NULL")
    assert client.post(f"/api/v1/machines/{mid}/start", json={"epoch": epoch}, headers=auth(token)).status_code == 409
    conn.execute("UPDATE workloads SET image_digest = %s", (DIGEST_A,))
    client.post(f"/api/machines/{mid}/enabled", json={"enabled": False})
    assert client.post(f"/api/v1/machines/{mid}/start", json={"epoch": epoch}, headers=auth(token)).status_code == 409


def test_start_without_a_secrets_key_is_409_when_secrets_are_declared(client, conn, no_secrets_key):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    epoch = assign(client, mid, "hello")["epoch"]
    r = client.post(f"/api/v1/machines/{mid}/start", json={"epoch": epoch}, headers=auth(token))
    assert r.status_code == 409 and "FLEET_SECRETS_KEY" in r.json()["detail"]
    add_workload(conn, manifest_data("plain", secrets={"container": [], "host_only": []}, outbound={"actions": []}))
    epoch = assign(client, mid, "plain")["epoch"]
    assert client.post(f"/api/v1/machines/{mid}/start", json={"epoch": epoch}, headers=auth(token)).json()["secrets"] == {}


def test_machine_links_to_the_worker_with_the_same_boot_id(client, conn, make_worker):
    w = make_worker("w1")
    conn.execute("UPDATE workers SET boot_id = 'boot-9' WHERE id = %s", (w.id,))
    mid, _ = enroll(client, conn, "box1", boot_id="boot-9")
    assert conn.execute("SELECT polymarket_worker_id FROM machines WHERE id = %s", (mid,)).fetchone()[
        "polymarket_worker_id"] == w.id
    other = make_worker("w2")
    conn.execute("UPDATE workers SET boot_id = 'boot-9' WHERE id = %s", (other.id,))
    mid2, _ = enroll(client, conn, "box2", boot_id="boot-9")
    assert conn.execute("SELECT polymarket_worker_id FROM machines WHERE id = %s", (mid2,)).fetchone()[
        "polymarket_worker_id"] is None, "two workers share the boot id: no unique match"


def test_agent_downloads_are_503_while_fleetagent_is_absent(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_AGENT_DIR", str(tmp_path / "missing"))
    assert client.get("/dl/agent/version").status_code == 503
    assert client.get("/dl/agent.tar.gz").status_code == 503
    assert client.get("/install-agent.sh").status_code == 503


def test_agent_downloads_and_version_in_register(client, conn, config, tmp_path, monkeypatch):
    import hashlib
    import io
    import tarfile

    from host.workloads import agent_bundle

    pkg = tmp_path / "fleetagent"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("X = 1\n")
    (pkg / "sub" / "mod.py").write_text("Y = 2\n")
    monkeypatch.setenv("FLEET_AGENT_DIR", str(pkg))
    agent_bundle.reset_cache()
    config.deploy_dir.mkdir()
    (config.deploy_dir / "install_agent.sh").write_text("echo __FLEET_HOST_URL__\n")
    try:
        v = client.get("/dl/agent/version").json()
        data = client.get("/dl/agent.tar.gz")
        assert data.status_code == 200 and hashlib.sha256(data.content).hexdigest() == v["sha256"]
        with tarfile.open(fileobj=io.BytesIO(data.content)) as tar:
            names = tar.getnames()
        assert "fleetagent/__init__.py" in names and "fleetagent/sub/mod.py" in names and "fleetagent/VERSION" in names
        assert client.get("/install-agent.sh").text == "echo http://127.0.0.1:8080\n"
        mid, token = enroll(client, conn)
        assert beat(client, mid, token).json()["agent_version"] == v["agent_version"]
    finally:
        agent_bundle.reset_cache()
