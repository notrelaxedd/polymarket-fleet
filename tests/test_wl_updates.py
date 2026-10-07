"""Fixes from /verify: manifest snapshots and updates instead of the sync freeze, scrubbed job
results, the offline note on assign, and the dropped-log count in the heartbeat reply."""
from __future__ import annotations

import io
from contextlib import redirect_stdout

from host.workloads import queue, updates
from tests.conftest import flash_cookie
from tests.test_wl_api_support import (  # noqa: F401
    DIGEST_A, DIGEST_B, GOOD_SPECS, _registry_env, add_workload, auth, beat, container, enroll, manifest_data,
    polymarket_data, run_token_for, secrets_key,
)
from tests.wl_helpers import FakeMachine, live_trader

SECRET = "Bonjour-SECRET-xyz"


def assign(client, mid, workload):
    r = client.post(f"/api/machines/{mid}/assign", json={"workload": workload})
    assert r.status_code == 200, r.text
    return r.json()


def snapshot(conn, mid):
    return conn.execute("SELECT * FROM workload_assignments WHERE machine_id = %s", (mid,)).fetchone()


def publish(conn, name, digest):
    conn.execute("UPDATE workloads SET image_digest = %s WHERE name = %s", (digest, name))


def bump_runtime(conn, name, **runtime):
    conn.execute(
        "UPDATE workloads SET manifest = jsonb_set(manifest, '{runtime}', (manifest->'runtime') || %s::jsonb) WHERE name = %s",
        (__import__("json").dumps(runtime), name),
    )


# ------------------------------------------------------------------ snapshots


def test_assign_snapshots_the_manifest_and_digest(client, conn):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    assign(client, mid, "hello")
    a = snapshot(conn, mid)
    assert a["run_image_digest"] == DIGEST_A and a["run_manifest"]["name"] == "hello"
    assign(client, mid, None)
    a = snapshot(conn, mid)
    assert a["run_manifest"] is None and a["run_image_digest"] is None


def test_a_publish_or_sync_reaches_a_machine_only_when_applied(client, conn):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    epoch = assign(client, mid, "hello")["epoch"]
    before = beat(client, mid, token).json()["run"]
    publish(conn, "hello", DIGEST_B)
    bump_runtime(conn, "hello", stop_timeout_s=30)
    assert beat(client, mid, token).json()["run"] == before
    assert updates.outdated(conn, "hello") == [mid]
    machines = client.get("/api/machines").json()
    assert [m["update_available"] for m in machines if m["id"] == mid] == [True]
    row = client.post(f"/api/machines/{mid}/update").json()
    assert row["epoch"] == epoch + 1 and row["state"] == "pending"
    run = beat(client, mid, token).json()["run"]
    assert run["image"].endswith(DIGEST_B) and run["stop_timeout_s"] == 30
    assert updates.outdated(conn, "hello") == []
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE entity = %s ORDER BY id", (mid,))]
    assert actions[-1] == "workload_update"


def test_apply_update_refusals(client, conn, make_worker):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    assert client.post(f"/api/machines/{mid}/update").status_code == 409, "nothing assigned"
    assign(client, mid, "hello")
    assert client.post(f"/api/machines/{mid}/update").json()["up_to_date"] is True
    publish(conn, "hello", DIGEST_B)
    conn.execute("UPDATE machines SET pinned = true, pinned_reason = 'by hand' WHERE id = %s", (mid,))
    r = client.post(f"/api/machines/{mid}/update")
    assert r.status_code == 409 and "pinned" in r.json()["detail"]
    conn.execute("UPDATE machines SET pinned = false WHERE id = %s", (mid,))
    conn.execute("UPDATE workloads SET manifest = jsonb_set(manifest, '{resources,min_ram_mb}', '999999') WHERE name = 'hello'")
    r = client.post(f"/api/machines/{mid}/update")
    assert r.status_code == 422 and "ram_too_small" in r.json()["detail"]
    assert snapshot(conn, mid)["run_image_digest"] == DIGEST_A, "nothing changed on a refusal"


def test_updating_a_live_polymarket_machine_is_refused_and_a_paper_one_drains(client, conn):
    add_workload(conn, polymarket_data(), size_mb=300)
    mid, token = enroll(client, conn, "box1", specs={**GOOD_SPECS, "ram_total_mb": 8000})
    epoch = assign(client, mid, "polymarket")["epoch"]
    beat(client, mid, token, acked_epoch=epoch, container=container("polymarket", epoch))
    setup = live_trader(conn, FakeMachine(id=mid, token=token, name="box1"))
    bump_runtime(conn, "polymarket", nice=7)
    r = client.post(f"/api/machines/{mid}/update")
    assert r.status_code == 409 and "live trading" in r.json()["detail"]
    out = client.post("/api/workloads/polymarket/rollout").json()
    assert out["updated"] == [] and "live trading" in out["skipped"]["box1"]
    assert beat(client, mid, token).json()["run"]["nice"] == 5, "the live box keeps its snapshot"
    conn.execute("UPDATE assignments SET mode = 'paper' WHERE id = %s", (setup.assignment["id"],))
    row = client.post(f"/api/machines/{mid}/update").json()
    assert row["state"] == "draining" and row["draining_to"] == "polymarket" and row["epoch"] == epoch
    worker = conn.execute("SELECT desired_role FROM workers WHERE id = %s", (setup.worker.id,)).fetchone()
    assert worker["desired_role"] == "idle", "the trade release handshake runs first"
    assert beat(client, mid, token).json()["run"]["nice"] == 5, "the container stays up while draining"


def test_rollout_updates_what_it_can_and_reports_the_rest(client, conn):
    add_workload(conn, manifest_data("hello"))
    a, ta = enroll(client, conn, "box-a")
    b, tb = enroll(client, conn, "box-b")
    assign(client, a, "hello")
    assign(client, b, "hello")
    conn.execute("UPDATE machines SET pinned = true, pinned_reason = 'by hand' WHERE id = %s", (b,))
    publish(conn, "hello", DIGEST_B)
    out = client.post("/api/workloads/hello/rollout").json()
    assert out["updated"] == ["box-a"] and list(out["skipped"]) == ["box-b"] and "pinned" in out["skipped"]["box-b"]
    assert snapshot(conn, a)["run_image_digest"] == DIGEST_B and snapshot(conn, b)["run_image_digest"] == DIGEST_A
    assert client.post("/api/workloads/hello/rollout").json() == {"updated": [], "draining": [], "skipped": {"box-b": out["skipped"]["box-b"]}}


def test_dashboard_update_forms(client, conn):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn, "box1")
    beat(client, mid, token)
    assign(client, mid, "hello")
    publish(conn, "hello", DIGEST_B)
    page = client.get("/machines").text
    assert 'data-action="apply-update"' in page and "update available" in page
    assert 'data-form="rollout"' in client.get("/workloads/hello").text
    r = client.post(f"/machines/{mid}/update", follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "box1: update applied"
    assert "update available" not in client.get("/machines").text
    publish(conn, "hello", DIGEST_A)
    r = client.post("/workloads/hello/rollout", follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "updated box1"


def test_cli_machine_update_and_rollout(client, conn, test_db_url, monkeypatch):
    from host.cli import main

    monkeypatch.setenv("DATABASE_URL", test_db_url)
    add_workload(conn, manifest_data("hello"))
    mid, _ = enroll(client, conn, "box1")
    assign(client, mid, "hello")
    publish(conn, "hello", DIGEST_B)
    out = io.StringIO()
    with redirect_stdout(out):
        assert main(["workload-rollout", "hello"]) == 0
        assert main(["machine-update", "box1"]) == 0
    text = out.getvalue()
    assert "updated: box1" in text and "(up to date)" in text


# ------------------------------------------------------------------ scrubbed job results


def test_job_results_errors_and_checkpoints_are_scrubbed(client, conn, secrets_key):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn)
    assert client.put("/api/workloads/hello/secrets/HELLO_GREETING", json={"value": SECRET}).status_code == 200
    epoch = assign(client, mid, "hello")["epoch"]
    run = run_token_for(client, mid, token, epoch)["run_token"]
    ids = [queue.create_job(conn, workload="hello", kind="hello", params={})["id"] for _ in range(3)]
    jobs = [client.post("/api/v1/wl/claim", json={}, headers=auth(run)).json()["job"] for _ in ids]
    j1, j2, j3 = jobs
    hb = client.post(f"/api/v1/wl/jobs/{j1['id']}/heartbeat", headers=auth(run),
                     json={"lease_token": j1["lease_token"], "progress": 0.5, "checkpoint": {"note": f"saw {SECRET}"}})
    assert hb.status_code == 200
    done = client.post(f"/api/v1/wl/jobs/{j1['id']}/complete", headers=auth(run),
                       json={"lease_token": j1["lease_token"], "result": {"greeting": f"{SECRET}, x!", "quoted": f'"{SECRET}"'}})
    assert done.status_code == 200
    client.post(f"/api/v1/wl/jobs/{j2['id']}/fail", headers=auth(run), json={"lease_token": j2["lease_token"], "error": f"boom {SECRET}"})
    client.post(f"/api/v1/wl/jobs/{j3['id']}/release", headers=auth(run),
                json={"lease_token": j3["lease_token"], "checkpoint": {"k": SECRET}, "progress": 0.1, "reason": "shutdown"})
    rows = conn.execute("SELECT result::text AS r, error, checkpoint::text AS c FROM workload_jobs").fetchall()
    stored = " ".join(str(v) for row in rows for v in row.values())
    assert SECRET not in stored and "[redacted]" in stored
    assert SECRET not in client.get("/workloads/hello").text


# ------------------------------------------------------------------ offline note, dropped logs


def test_assigning_an_offline_machine_says_so(client, conn):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn, "box1")
    assert assign(client, mid, "hello")["note"] == "box1 has not checked in yet; the change takes effect when it does"
    beat(client, mid, token)
    assert "note" not in assign(client, mid, None)
    conn.execute("UPDATE machines SET last_heartbeat_at = now() - interval '1 hour' WHERE id = %s", (mid,))
    row = assign(client, mid, "hello")
    assert row["note"] == "box1 is offline; the change takes effect when it comes back"


def test_the_heartbeat_reports_dropped_log_lines(client, conn):
    mid, token = enroll(client, conn)
    logs = [{"ts": "2026-10-06T21:00:00Z", "stream": "stdout", "line": f"l{i}"} for i in range(203)]
    assert beat(client, mid, token, logs=logs).json()["logs_dropped"] == 3
    assert beat(client, mid, token, logs=logs[:5]).json()["logs_dropped"] == 0
