"""Container routes: run-token scope, the job lifecycle, the reaper, outbound from a container."""
from __future__ import annotations

import uuid

from host.workloads import queue
from tests.test_wl_api_support import (  # noqa: F401
    DIGEST_A, _registry_env, add_workload, auth, beat, enroll, manifest_data, no_secrets_key, run_token_for,
    secrets_key,
)


def setup_machine(client, conn, workload="hello", name="box1", **manifest_over):
    """A machine running `workload`; returns (machine_id, machine_token, epoch, run_token)."""
    if conn.execute("SELECT 1 FROM workloads WHERE name = %s", (workload,)).fetchone() is None:
        add_workload(conn, manifest_data(workload, secrets={"container": [], "host_only": []}, **manifest_over))
    mid, token = enroll(client, conn, name)
    epoch = client.post(f"/api/machines/{mid}/assign", json={"workload": workload}).json()["epoch"]
    run = run_token_for(client, mid, token, epoch)["run_token"]
    return mid, token, epoch, run


def make_job(client, workload="hello", kind="hello", **extra):
    r = client.post("/api/workload-jobs", json={"workload": workload, "kind": kind, "params": {"name": "x"}, **extra})
    assert r.status_code in (200, 201), r.text
    return r.json()


def claim(client, run, kinds=None):
    return client.post("/api/v1/wl/claim", json={"kinds": kinds} if kinds else {}, headers=auth(run))


def test_claim_complete_lifecycle(client, conn):
    mid, token, epoch, run = setup_machine(client, conn)
    assert claim(client, run).json() == {"job": None}
    job = make_job(client)
    got = claim(client, run).json()["job"]
    assert got["id"] == job["id"] and got["kind"] == "hello" and got["params"] == {"name": "x"}
    assert got["checkpoint"] is None and got["progress"] == 0 and got["lease_seconds"] == 30 and got["lease_token"]
    row = conn.execute("SELECT * FROM workload_jobs WHERE id = %s", (job["id"],)).fetchone()
    assert (row["status"], row["lease_machine_id"], row["lease_epoch"]) == ("leased", mid, epoch)
    assert claim(client, run).json() == {"job": None}, "nothing else queued"
    lt = got["lease_token"]
    r = client.post(f"/api/v1/wl/jobs/{job['id']}/heartbeat", headers=auth(run),
                    json={"lease_token": lt, "progress": 0.5, "checkpoint": {"step": 2}})
    assert r.status_code == 200 and r.json() == {"status": "leased", "cancel": False}
    row = conn.execute("SELECT * FROM workload_jobs WHERE id = %s", (job["id"],)).fetchone()
    assert (row["progress"], row["checkpoint"]) == (0.5, {"step": 2})
    r = client.post(f"/api/v1/wl/jobs/{job['id']}/complete", headers=auth(run), json={"lease_token": lt, "result": {"ok": 1}})
    assert r.status_code == 200 and r.json() == {"status": "succeeded"}
    again = client.post(f"/api/v1/wl/jobs/{job['id']}/complete", headers=auth(run), json={"lease_token": lt, "result": {"ok": 1}})
    assert again.status_code == 200, "complete is idempotent"
    done = client.get(f"/api/workload-jobs/{job['id']}").json()
    assert done["status"] == "succeeded" and done["result"] == {"ok": 1} and "lease_token" not in done
    assert [e["event"] for e in done["events"]] == ["created", "claimed", "succeeded"]


def test_fail_release_and_fencing(client, conn):
    mid, token, epoch, run = setup_machine(client, conn)
    a, b = make_job(client), make_job(client)
    ja = claim(client, run).json()["job"]
    bad = client.post(f"/api/v1/wl/jobs/{ja['id']}/complete", headers=auth(run),
                      json={"lease_token": str(uuid.uuid4()), "result": {}})
    assert bad.status_code == 409, "wrong lease token"
    r = client.post(f"/api/v1/wl/jobs/{ja['id']}/release", headers=auth(run),
                    json={"lease_token": ja["lease_token"], "checkpoint": {"k": 1}, "progress": 0.3, "reason": "shutdown"})
    assert r.json() == {"status": "queued"}
    row = conn.execute("SELECT * FROM workload_jobs WHERE id = %s", (ja["id"],)).fetchone()
    assert (row["checkpoint"], row["lease_token"], row["lease_machine_id"], row["expiries"]) == ({"k": 1}, None, None, 0)
    assert client.post(f"/api/v1/wl/jobs/{ja['id']}/fail", headers=auth(run),
                       json={"lease_token": ja["lease_token"], "error": "x"}).status_code == 409, "old lease"
    jb = claim(client, run).json()["job"]
    assert jb["id"] == a["id"], "oldest first, the released job is back at its place"
    r = client.post(f"/api/v1/wl/jobs/{jb['id']}/fail", headers=auth(run), json={"lease_token": jb["lease_token"], "error": "boom"})
    assert r.json() == {"status": "failed"}
    assert conn.execute("SELECT error FROM workload_jobs WHERE id = %s", (jb["id"],)).fetchone()["error"] == "boom"
    assert client.post(f"/api/v1/wl/jobs/{jb['id']}/fail", headers=auth(run),
                       json={"lease_token": jb["lease_token"], "error": "boom"}).status_code == 200, "fail is idempotent"
    big = client.post(f"/api/v1/wl/jobs/{jb['id']}/fail", headers=auth(run),
                      json={"lease_token": jb["lease_token"], "error": "e" * 20000})
    assert big.status_code == 400, "errors above 16 KiB are refused"
    assert b["id"] != a["id"]


def test_cancel_flow(client, conn):
    mid, token, epoch, run = setup_machine(client, conn)
    queued = make_job(client)
    r = client.post(f"/api/workload-jobs/{queued['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    leased_job = make_job(client)
    got = claim(client, run).json()["job"]
    assert got["id"] == leased_job["id"]
    r = client.post(f"/api/workload-jobs/{got['id']}/cancel")
    assert r.json()["status"] == "cancel_requested"
    hb = client.post(f"/api/v1/wl/jobs/{got['id']}/heartbeat", headers=auth(run), json={"lease_token": got["lease_token"], "progress": 0.1})
    assert hb.json() == {"status": "cancel_requested", "cancel": True}
    rel = client.post(f"/api/v1/wl/jobs/{got['id']}/release", headers=auth(run),
                      json={"lease_token": got["lease_token"], "reason": "cancel"})
    assert rel.json() == {"status": "cancelled"}
    done = make_job(client)
    g = claim(client, run).json()["job"]
    client.post(f"/api/v1/wl/jobs/{g['id']}/complete", headers=auth(run), json={"lease_token": g["lease_token"], "result": {}})
    assert client.post(f"/api/workload-jobs/{done['id']}/cancel").status_code == 409, "terminal jobs cannot be cancelled"
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'workload_job%%'").fetchall()]
    assert actions.count("workload_job_cancel") == 2 and actions.count("workload_job_create") == 3


def test_reaper_requeues_then_fails_at_max_expiries(client, conn):
    mid, token, epoch, run = setup_machine(client, conn)
    job = make_job(client)
    for expected in ("queued", "queued", "failed"):
        got = claim(client, run).json()["job"]
        assert got["id"] == job["id"]
        conn.execute("UPDATE workload_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (job["id"],))
        assert queue.reap(conn) == 1
        row = conn.execute("SELECT * FROM workload_jobs WHERE id = %s", (job["id"],)).fetchone()
        assert row["status"] == expected and row["lease_token"] is None
    assert row["expiries"] == 3 and "3 expiries" in row["error"] and row["finished_at"] is not None
    assert queue.reap(conn) == 0
    # a late complete from the dead lease is refused
    r = client.post(f"/api/v1/wl/jobs/{job['id']}/complete", headers=auth(run), json={"lease_token": got["lease_token"], "result": {}})
    assert r.status_code == 409


def test_reaper_cancels_a_cancel_requested_job(client, conn):
    mid, token, epoch, run = setup_machine(client, conn)
    job = make_job(client)
    claim(client, run)
    client.post(f"/api/workload-jobs/{job['id']}/cancel")
    conn.execute("UPDATE workload_jobs SET lease_expires_at = now() - interval '1 second'")
    assert queue.reap(conn) == 1
    assert conn.execute("SELECT status FROM workload_jobs").fetchone()["status"] == "cancelled"


def test_reassignment_releases_the_machines_leases_and_fences_the_old_epoch(client, conn):
    mid, token, epoch, run = setup_machine(client, conn)
    job = make_job(client)
    got = claim(client, run).json()["job"]
    new_epoch = client.post(f"/api/machines/{mid}/assign", json={"workload": None}).json()["epoch"]
    row = conn.execute("SELECT * FROM workload_jobs WHERE id = %s", (job["id"],)).fetchone()
    assert row["status"] == "queued" and row["lease_machine_id"] is None and row["expiries"] == 0
    assert new_epoch == epoch + 1
    r = client.post(f"/api/v1/wl/jobs/{job['id']}/complete", headers=auth(run), json={"lease_token": got["lease_token"], "result": {}})
    assert r.status_code == 401, "the run token of the old epoch is gone"
    # a stale lease from a lost token holder with the right token but another epoch is fenced in queue too
    conn.execute("UPDATE workload_jobs SET status = 'leased', lease_machine_id = %s, lease_epoch = %s,"
                 " lease_token = %s, lease_expires_at = now() + interval '1 minute'", (mid, epoch, got["lease_token"]))
    import pytest
    from host.errors import Conflict

    with pytest.raises(Conflict):
        queue.complete(conn, job_id=job["id"], lease_token=got["lease_token"], machine_id=mid, epoch=new_epoch, result={})


def test_oom_release_counts_as_an_expiry(client, conn):
    mid, token, epoch, run = setup_machine(client, conn)
    job = make_job(client)
    for _ in range(3):
        got = claim(client, run).json()["job"]
        r = client.post(f"/api/v1/wl/jobs/{got['id']}/release", headers=auth(run),
                        json={"lease_token": got["lease_token"], "reason": "oom"})
    assert r.json() == {"status": "failed"}
    assert "out of memory" in conn.execute("SELECT error FROM workload_jobs WHERE id = %s", (job["id"],)).fetchone()["error"]


def test_targeted_jobs_go_to_their_machine_first_and_only_declared_kinds_are_claimed(client, conn):
    mid, token, epoch, run = setup_machine(client, conn)
    other, other_token = enroll(client, conn, "box2")
    other_epoch = client.post(f"/api/machines/{other}/assign", json={"workload": "hello"}).json()["epoch"]
    other_run = run_token_for(client, other, other_token, other_epoch)["run_token"]
    untargeted = make_job(client)
    targeted = make_job(client, target=other)
    assert claim(client, run).json()["job"]["id"] == untargeted["id"], "never someone else's targeted job"
    assert claim(client, run).json() == {"job": None}
    assert claim(client, other_run).json()["job"]["id"] == targeted["id"]
    make_job(client)
    assert claim(client, run, kinds=["nope"]).json() == {"job": None}, "kinds narrow, never widen"
    assert claim(client, run, kinds=["hello", "nope"]).json()["job"] is not None


def test_a_run_token_only_reaches_its_own_workloads_jobs(client, conn):
    mid, token, epoch, run = setup_machine(client, conn, "hello")
    add_workload(conn, manifest_data("other", image="fleet/other", runtime={"job_kinds": ["other"]},
                                     secrets={"container": [], "host_only": []}))
    m2, t2 = enroll(client, conn, "box2")
    e2 = client.post(f"/api/machines/{m2}/assign", json={"workload": "other"}).json()["epoch"]
    run2 = run_token_for(client, m2, t2, e2)["run_token"]
    foreign = make_job(client, workload="other", kind="other")
    mine = make_job(client)
    assert claim(client, run).json()["job"]["id"] == mine["id"]
    assert claim(client, run).json() == {"job": None}, "hello's token never claims other's kind"
    held = claim(client, run2).json()["job"]
    assert held["id"] == foreign["id"]
    r = client.post(f"/api/v1/wl/jobs/{foreign['id']}/complete", headers=auth(run),
                    json={"lease_token": held["lease_token"], "result": {}})
    assert r.status_code == 404, "another workload's job is not even visible"
    assert client.post("/api/workload-jobs", json={"workload": "hello", "kind": "other", "params": {}}).status_code == 400


def test_run_tokens_are_refused_everywhere_except_wl(client, conn, make_worker):
    mid, token, epoch, run = setup_machine(client, conn)
    worker = make_worker("w")
    for method, path in [
        ("post", f"/api/v1/workers/{worker.id}/heartbeat"),
        ("post", f"/api/v1/machines/{mid}/heartbeat"),
        ("post", f"/api/v1/machines/{mid}/start"),
        ("get", "/api/v1/data/games"),
        ("post", "/api/v1/trade/order"),
        ("post", "/api/v1/jobs/claim"),
    ]:
        r = getattr(client, method)(path, json={"epoch": epoch}, headers=auth(run)) if method == "post" else client.get(path, headers=auth(run))
        assert r.status_code in (401, 404, 405), (path, r.status_code)
        assert r.status_code != 200, path
    assert client.post(f"/api/v1/workers/{worker.id}/heartbeat", json={}, headers=auth(run)).status_code == 401
    assert client.post(f"/api/v1/machines/{mid}/heartbeat", json={}, headers=auth(run)).status_code == 401


def test_machine_and_worker_tokens_are_refused_on_wl(client, conn, make_worker):
    mid, token, epoch, run = setup_machine(client, conn)
    worker = make_worker("w")
    for tok in (token, worker.token, "garbage"):
        assert client.post("/api/v1/wl/claim", json={}, headers=auth(tok)).status_code == 401
        assert client.post("/api/v1/wl/outbound", json={"kind": "log", "payload": {}, "dedupe_key": "k"},
                           headers=auth(tok)).status_code == 401
    assert client.post("/api/v1/wl/claim", json={}).status_code == 401, "no bearer at all"


def test_run_token_dies_when_the_machine_is_disabled(client, conn):
    mid, token, epoch, run = setup_machine(client, conn)
    assert claim(client, run).status_code == 200
    client.post(f"/api/machines/{mid}/enabled", json={"enabled": False})
    assert claim(client, run).status_code == 401
    client.post(f"/api/machines/{mid}/enabled", json={"enabled": True})
    assert claim(client, run).status_code == 200


def test_outbound_from_a_container_queues_and_reads_back_only_its_own(client, conn):
    mid, token, epoch, run = setup_machine(client, conn, "hello", outbound={"actions": ["email", "log"]})
    job = make_job(client)
    got = claim(client, run).json()["job"]
    body = {"kind": "log", "payload": {"msg": "hi"}, "dedupe_key": f"hello:{got['id']}", "job_id": got["id"]}
    r = client.post("/api/v1/wl/outbound", json=body, headers=auth(run))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    aid = r.json()["id"]
    assert client.post("/api/v1/wl/outbound", json=body, headers=auth(run)).json()["id"] == aid, "idempotent"
    assert conn.execute("SELECT count(*) AS n FROM outbound_actions").fetchone()["n"] == 1
    st = client.get(f"/api/v1/wl/outbound/{aid}", headers=auth(run)).json()
    assert st == {"id": aid, "status": "pending", "error": None, "result": None}
    r = client.post("/api/v1/wl/outbound", json={**body, "kind": "sms", "dedupe_key": "z"}, headers=auth(run))
    assert r.status_code == 403, "kind not declared by the manifest"
    r = client.post("/api/v1/wl/outbound", json={**body, "dedupe_key": "big", "payload": {"x": "y" * 70000}}, headers=auth(run))
    assert r.status_code == 400
    r = client.post("/api/v1/wl/outbound", json={**body, "dedupe_key": "nojob", "job_id": str(uuid.uuid4())}, headers=auth(run))
    assert r.status_code == 400
    add_workload(conn, manifest_data("other", image="fleet/other", secrets={"container": [], "host_only": []}))
    m2, t2 = enroll(client, conn, "box2")
    e2 = client.post(f"/api/machines/{m2}/assign", json={"workload": "other"}).json()["epoch"]
    run2 = run_token_for(client, m2, t2, e2)["run_token"]
    assert client.get(f"/api/v1/wl/outbound/{aid}", headers=auth(run2)).status_code == 404
    assert job["id"] == got["id"]


def test_outbound_pending_cap_is_429(client, conn, monkeypatch):
    from host.workloads import outbound

    mid, token, epoch, run = setup_machine(client, conn, "hello", outbound={"actions": ["log"]})
    monkeypatch.setattr(outbound, "MAX_PENDING", 2)
    for i in range(2):
        assert client.post("/api/v1/wl/outbound", json={"kind": "log", "payload": {}, "dedupe_key": f"k{i}"},
                           headers=auth(run)).status_code == 200
    r = client.post("/api/v1/wl/outbound", json={"kind": "log", "payload": {}, "dedupe_key": "k9"}, headers=auth(run))
    assert r.status_code == 429
    again = client.post("/api/v1/wl/outbound", json={"kind": "log", "payload": {}, "dedupe_key": "k0"}, headers=auth(run))
    assert again.status_code == 200, "an existing dedupe key still answers at the cap"
