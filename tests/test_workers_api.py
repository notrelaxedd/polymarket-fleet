"""Worker-facing HTTP routes: enrollment, registration, heartbeat auth, downloads."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
import threading
import time

import psycopg

from host import auth
from host.api.app import MAX_BODY_BYTES
from host.api.limits import MAX_PAYLOAD_BYTES
from tests.conftest import heartbeat_body

REGISTER = "/api/v1/workers/register"
MACHINE = {"hostname": "box1", "python_version": "3.13.1", "code_version": "abc", "boot_id": "b1"}


def test_enroll_token_is_single_use(client, conn):
    r = client.post("/api/enroll-token")
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert "install.sh" in r.json()["install_command"]
    assert r.json()["expires_at"].endswith("Z")
    r = client.post(REGISTER, json={"enroll_token": token, "name": "box1", **MACHINE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["worker_id"].startswith("w_") and len(body["worker_id"]) == 8
    assert body["desired_role"] == "idle" and body["held_jobs"] == []
    assert body["kill"] is False and body["heartbeat_seconds"] == 3
    assert body["code_version"] == client.app.state.bundle.code_version
    row = conn.execute("SELECT * FROM workers WHERE id = %s", (body["worker_id"],)).fetchone()
    assert row["name"] == "box1" and row["hostname"] == "box1" and row["remote_ip"]
    assert row["token_hash"] == auth.hash_token(body["worker_token"])
    used = conn.execute("SELECT used_by_worker_id FROM enroll_tokens").fetchone()
    assert used["used_by_worker_id"] == body["worker_id"]
    r = client.post(REGISTER, json={"enroll_token": token, **MACHINE})
    assert r.status_code == 401
    assert "enroll token" in r.json()["detail"]


def test_enroll_token_expiry_and_garbage(client, conn):
    expired = auth.mint_token()
    conn.execute(
        "INSERT INTO enroll_tokens (token_hash, expires_at) VALUES (%s, now() - interval '1 minute')",
        (auth.hash_token(expired),),
    )
    assert client.post(REGISTER, json={"enroll_token": expired, **MACHINE}).status_code == 401
    assert client.post(REGISTER, json={"enroll_token": "nope", **MACHINE}).status_code == 401
    assert client.post(REGISTER, json={"hostname": "x"}).status_code == 401


def test_register_rotates_token(client, make_worker):
    w = make_worker("box1")
    r = client.post(REGISTER, json={"worker_id": w.id, "worker_token": w.token, **MACHINE})
    assert r.status_code == 200, r.text
    new_token = r.json()["worker_token"]
    assert new_token != w.token and r.json()["worker_id"] == w.id
    old_headers = {"Authorization": f"Bearer {w.token}"}
    r = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body(), headers=old_headers)
    assert r.status_code == 401, "heartbeats never accept the previous token"
    new_headers = {"Authorization": f"Bearer {new_token}"}
    r = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body(), headers=new_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["desired_role"] == "idle" and body["claimed"] == [] and body["lost"] == []
    assert body["server_time"].endswith("Z")
    r = client.post(REGISTER, json={"worker_id": w.id, "worker_token": w.token, **MACHINE})
    assert r.status_code == 401, "once the new token has heartbeated, a zombie with the old one is locked out"
    r = client.post(REGISTER, json={"worker_id": w.id, "worker_token": new_token, **MACHINE})
    assert r.status_code == 200


def test_lost_register_reply_can_be_retried(client, conn, make_worker):
    """HIGH: a register whose reply never reached the agent must be retryable with the
    token the agent still has, without bricking the worker."""
    w = make_worker("box1")
    first = client.post(REGISTER, json={"worker_id": w.id, "worker_token": w.token, **MACHINE})
    assert first.status_code == 200
    unseen = first.json()["worker_token"]
    row = conn.execute("SELECT prev_token_hash FROM workers WHERE id = %s", (w.id,)).fetchone()
    assert row["prev_token_hash"] == auth.hash_token(w.token)
    retry = client.post(REGISTER, json={"worker_id": w.id, "worker_token": w.token, **MACHINE})
    assert retry.status_code == 200, retry.text
    current = retry.json()["worker_token"]
    assert current not in (w.token, unseen)
    url = f"/api/v1/workers/{w.id}/heartbeat"
    assert client.post(url, json=heartbeat_body(), headers={"Authorization": f"Bearer {unseen}"}).status_code == 401
    assert client.post(url, json=heartbeat_body(), headers={"Authorization": f"Bearer {w.token}"}).status_code == 401
    assert client.post(url, json=heartbeat_body(), headers={"Authorization": f"Bearer {current}"}).status_code == 200
    row = conn.execute("SELECT prev_token_hash, token_hash FROM workers WHERE id = %s", (w.id,)).fetchone()
    assert row["prev_token_hash"] is None and row["token_hash"] == auth.hash_token(current)
    assert client.post(REGISTER, json={"worker_id": w.id, "worker_token": w.token, **MACHINE}).status_code == 401
    assert client.post(REGISTER, json={"worker_id": w.id, "worker_token": unseen, **MACHINE}).status_code == 401
    fleet = client.get("/api/fleet").json()["workers"][0]
    assert "prev_token_hash" not in fleet and "token_hash" not in fleet
    r = client.post(f"/api/workers/{w.id}/role", json={"role": "idle"})
    assert "prev_token_hash" not in r.json() and "token_hash" not in r.json()


def _drive_asgi(app, method: str, path: str, body: dict, headers: dict[str, str], sent_event: threading.Event) -> dict:
    """Run one request through the raw ASGI app; set `sent_event` the moment the last
    response byte has been handed to the server (before any after-response teardown)."""
    raw = json.dumps(body).encode()
    hdrs = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    hdrs += [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": hdrs, "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8080),
    }
    messages: list[dict] = []
    delivered = {"body": False}

    async def receive() -> dict:
        if not delivered["body"]:
            delivered["body"] = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            sent_event.set()

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return {"status": status, "json": json.loads(payload or b"null")}


def test_write_is_committed_before_the_response_is_sent(client, conn, make_worker, monkeypatch):
    """MEDIUM: the token rotation must be visible to a second connection as soon as the
    register response has been sent, even when the commit itself is slow."""
    real_commit = psycopg.Connection.commit

    def slow_commit(self):
        time.sleep(0.4)
        real_commit(self)

    monkeypatch.setattr(psycopg.Connection, "commit", slow_commit)
    w = make_worker("box1")
    sent = threading.Event()
    result: dict = {}
    body = {"worker_id": w.id, "worker_token": w.token, **MACHINE}
    thread = threading.Thread(
        target=lambda: result.update(_drive_asgi(client.app, "POST", REGISTER, body, {}, sent))
    )
    thread.start()
    assert sent.wait(timeout=15), "no response"
    seen_hash = conn.execute("SELECT token_hash FROM workers WHERE id = %s", (w.id,)).fetchone()["token_hash"]
    thread.join(timeout=15)
    assert result["status"] == 200, result
    assert seen_hash == auth.hash_token(result["json"]["worker_token"]), "response sent before commit"
    assert seen_hash != auth.hash_token(w.token)


def test_heartbeat_rejects_wrong_and_foreign_tokens(client, make_worker):
    a = make_worker("a")
    b = make_worker("b")
    url = f"/api/v1/workers/{a.id}/heartbeat"
    assert client.post(url, json=heartbeat_body()).status_code == 401
    assert client.post(url, json=heartbeat_body(), headers={"Authorization": "Bearer bogus"}).status_code == 401
    assert client.post(url, json=heartbeat_body(), headers=b.headers).status_code == 401
    assert client.post(url, json=heartbeat_body(), headers=a.headers).status_code == 200
    assert client.post("/api/v1/workers/w_nope/heartbeat", json=heartbeat_body(), headers=a.headers).status_code == 401


def test_heartbeat_validation_is_400(client, make_worker):
    w = make_worker("a")
    r = client.post(f"/api/v1/workers/{w.id}/heartbeat", json={"jobs": "nope"}, headers=w.headers)
    assert r.status_code == 400
    assert "jobs" in r.json()["detail"]


def test_job_routes_require_a_worker_token(client, make_worker):
    w = make_worker("a", role="backtest")
    r = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 1}})
    job_id = r.json()["id"]
    hb = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("backtest"), headers=w.headers).json()
    token = hb["claimed"][0]["lease_token"]
    body = {"lease_token": token, "checkpoint": {"elapsed": 1}, "progress": 0.5}
    assert client.post(f"/api/v1/jobs/{job_id}/checkpoint", json=body).status_code == 401
    r = client.post(f"/api/v1/jobs/{job_id}/checkpoint", json=body, headers=w.headers)
    assert r.status_code == 200 and r.json() == {"status": "leased"}
    r = client.post(f"/api/v1/jobs/{job_id}/checkpoint", json={**body, "release": True}, headers=w.headers)
    assert r.json() == {"status": "queued"}
    assert client.post("/api/v1/jobs/not-a-uuid/complete", json={"lease_token": token}, headers=w.headers).status_code == 404


def test_dl_version_and_tarball(client):
    version = client.get("/dl/version").json()
    code_version = version["code_version"]
    assert len(code_version) == 12 and int(code_version, 16) >= 0
    r = client.get("/dl/worker.tar.gz")
    assert r.status_code == 200
    assert hashlib.sha256(r.content).hexdigest() == version["sha256"]
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        names = tar.getnames()
        assert {n.split("/")[0] for n in names} == {"fleet"}
        files = [n for n in names if tar.getmember(n).isfile()]
        assert all(n.endswith(".py") or n == "fleet/VERSION" for n in files)
        assert "fleet/__init__.py" in files
        assert not any("__pycache__" in n for n in names)
        assert tar.extractfile("fleet/VERSION").read().decode().strip() == code_version
    assert client.get("/dl/worker.tar.gz").content == r.content, "built once, served from memory"


def test_install_sh_missing_then_substituted(client, config):
    r = client.get("/install.sh")
    assert r.status_code == 503 and "install_worker.sh" in r.text
    config.deploy_dir.mkdir(parents=True)
    (config.deploy_dir / "install_worker.sh").write_text('#!/bin/bash\nHOST="__FLEET_HOST_URL__"\n')
    r = client.get("/install.sh")
    assert r.status_code == 200
    assert 'HOST="http://127.0.0.1:8080"' in r.text
    assert "__FLEET_HOST_URL__" not in r.text


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True, "db": True}


def test_reregister_event_rows_are_deduped(client, conn, make_worker):
    """LOW: a register retry loop writes one re-leased row per run, not one per call."""
    w = make_worker("box1", role="backtest")
    job = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 1}}).json()
    hb = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("backtest"), headers=w.headers).json()
    assert [c["id"] for c in hb["claimed"]] == [job["id"]]
    token = w.token
    for _ in range(5):
        r = client.post(REGISTER, json={"worker_id": w.id, "worker_token": token, **MACHINE})
        assert r.status_code == 200 and [h["id"] for h in r.json()["held_jobs"]] == [job["id"]]
        token = r.json()["worker_token"]
    events = [e["event"] for e in conn.execute(
        "SELECT event FROM job_events WHERE job_id = %s ORDER BY id", (job["id"],)).fetchall()]
    assert events == ["created", "claimed", "re-leased"]


def test_register_records_proxy_peer_and_validates_names(client, conn):
    """LOW: only the last X-Forwarded-For hop (added by tailscale serve) is trusted and
    hostname/name are short and plain."""
    token = client.post("/api/enroll-token").json()["token"]
    r = client.post(REGISTER, json={"enroll_token": token, "hostname": "<script>x</script>", **{k: v for k, v in MACHINE.items() if k != "hostname"}})
    assert r.status_code == 400 and "hostname" in r.json()["detail"]
    r = client.post(REGISTER, json={"enroll_token": token, "name": "a" * 5000, **MACHINE})
    assert r.status_code == 400 and "name" in r.json()["detail"]
    r = client.post(REGISTER, json={"enroll_token": token, **MACHINE}, headers={"X-Forwarded-For": "6.6.6.6, 100.64.0.7"})
    assert r.status_code == 200, r.text
    row = conn.execute("SELECT remote_ip FROM workers WHERE id = %s", (r.json()["worker_id"],)).fetchone()
    assert row["remote_ip"] == "100.64.0.7"
    audit = conn.execute("SELECT ip FROM audit_log WHERE action = 'worker_enrolled'").fetchone()
    assert audit["ip"] == "100.64.0.7"


def test_worker_payload_limits(client, make_worker):
    """LOW: oversized checkpoints, non-finite floats, long job lists and huge bodies are refused."""
    w = make_worker("box1", role="backtest")
    url = f"/api/v1/workers/{w.id}/heartbeat"
    big = {"blob": "x" * (MAX_PAYLOAD_BYTES + 1)}
    entry = {"id": "00000000-0000-0000-0000-000000000000", "lease_token": "00000000-0000-0000-0000-000000000000"}
    r = client.post(url, json=heartbeat_body("backtest", jobs=[{**entry, "checkpoint": big}]), headers=w.headers)
    assert r.status_code == 400 and "checkpoint" in r.json()["detail"]
    json_headers = {**w.headers, "Content-Type": "application/json"}
    nan_job = json.dumps(heartbeat_body("backtest", jobs=[{**entry, "progress": "NAN"}])).replace('"NAN"', "NaN")
    r = client.post(url, content=nan_job.encode(), headers=json_headers)
    assert r.status_code == 400 and "progress" in r.json()["detail"]
    inf_cpu = json.dumps(heartbeat_body("backtest", cpu_pct="INF")).replace('"INF"', "Infinity")
    r = client.post(url, content=inf_cpu.encode(), headers=json_headers)
    assert r.status_code == 400 and "cpu_pct" in r.json()["detail"]
    r = client.post(url, json=heartbeat_body("backtest", jobs=[entry] * 65), headers=w.headers)
    assert r.status_code == 400 and "jobs" in r.json()["detail"]
    r = client.post(url, json=heartbeat_body("backtest", jobs=[entry] * 64), headers=w.headers)
    assert r.status_code == 200 and len(r.json()["lost"]) == 64
    r = client.post(url, content=json.dumps(heartbeat_body("backtest", pad="x" * (MAX_BODY_BYTES + 1))).encode(),
                    headers=json_headers)
    assert r.status_code == 413
    job = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 1}}).json()
    hb = client.post(url, json=heartbeat_body("backtest"), headers=w.headers).json()
    lease = hb["claimed"][0]["lease_token"]
    r = client.post(f"/api/v1/jobs/{job['id']}/checkpoint", json={"lease_token": lease, "checkpoint": big}, headers=w.headers)
    assert r.status_code == 400 and "checkpoint" in r.json()["detail"]
    r = client.post(f"/api/v1/jobs/{job['id']}/complete", json={"lease_token": lease, "result": big}, headers=w.headers)
    assert r.status_code == 400 and "result" in r.json()["detail"]


def test_job_routes_check_the_calling_worker(client, make_worker):
    """LOW: a valid token of another worker plus a leaked lease token is 409."""
    a = make_worker("a", role="backtest")
    b = make_worker("b", role="backtest")
    job = client.post("/api/jobs", json={"kind": "sleep", "params": {"seconds": 1}}).json()
    hb = client.post(f"/api/v1/workers/{b.id}/heartbeat", json=heartbeat_body("backtest"), headers=b.headers).json()
    lease = hb["claimed"][0]["lease_token"]
    body = {"lease_token": lease, "checkpoint": {"elapsed": 1}, "progress": 0.5}
    assert client.post(f"/api/v1/jobs/{job['id']}/checkpoint", json=body, headers=a.headers).status_code == 409
    assert client.post(f"/api/v1/jobs/{job['id']}/complete", json={"lease_token": lease}, headers=a.headers).status_code == 409
    assert client.post(f"/api/v1/jobs/{job['id']}/fail", json={"lease_token": lease, "error": "x"}, headers=a.headers).status_code == 409
    hb = client.post(f"/api/v1/workers/{a.id}/heartbeat", headers=a.headers,
                     json=heartbeat_body("backtest", want_job=False, released=[{"id": job["id"], "lease_token": lease}])).json()
    assert hb["claimed"] == []
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "leased", "another worker cannot release it"
    r = client.post(f"/api/v1/jobs/{job['id']}/complete", json={"lease_token": lease, "result": {"ok": 1}}, headers=b.headers)
    assert r.status_code == 200 and r.json()["status"] == "succeeded"
