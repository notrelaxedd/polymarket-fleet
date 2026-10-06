"""Workloads CLI subcommands, the host.cli dispatch, and the loop wiring."""
from __future__ import annotations

import io
import json
from typing import Callable

import pytest

from host import loop as host_loop
from host.cli import main
from host.workloads import loop as wl_loop
from host.workloads import machines, outbound, queue
from tests.conftest import insert_job
from tests.test_wl_api_support import (  # noqa: F401
    DIGEST_A, GOOD_SPECS, _registry_env, add_workload, beat, enroll, manifest_data, no_secrets_key, secrets_key,
)

PUBLIC_URL = "http://127.0.0.1:8080"


@pytest.fixture
def cli_env(test_db_url: str, monkeypatch) -> None:
    for key, value in {"DATABASE_URL": test_db_url, "FLEET_DEV": "1", "FLEET_PUBLIC_URL": PUBLIC_URL}.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def run(cli_env, capsys) -> Callable[..., tuple[int, str, str]]:
    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def test_sync_image_and_machines(run, conn, client, tmp_path, monkeypatch):
    root = tmp_path / "wl"
    (root / "hello").mkdir(parents=True)
    (root / "hello" / "workload.toml").write_text(
        'schema = 1\nname = "hello"\nimage = "fleet/hello"\n[resources]\nmin_ram_mb = 128\n'
        '[runtime]\nmode = "jobs"\njob_kinds = ["hello"]\n')
    (root / "bad").mkdir()
    (root / "bad" / "workload.toml").write_text('schema = 1\nname = "bad"\nimage = "NO"\n')
    monkeypatch.setenv("FLEET_WORKLOADS_DIR", str(root))
    code, out, _ = run("workloads-sync")
    assert code == 0 and "synced: hello" in out and "error in bad:" in out
    code, out, err = run("workload-image", "hello", "sha256:short")
    assert code == 1 and "digest" in err
    code, out, _ = run("workload-image", "hello", DIGEST_A, "--size-mb", "42")
    assert code == 0 and DIGEST_A in out and "42 MB" in out
    row = conn.execute("SELECT * FROM workloads WHERE name = 'hello'").fetchone()
    assert (row["image_digest"], row["image_size_mb"]) == (DIGEST_A, 42)
    assert run("workload-image", "ghost", DIGEST_A)[0] == 1
    mid, _ = enroll(client, conn, "box1")
    code, out, _ = run("machines")
    assert code == 0 and mid in out and "box1" in out


def test_machine_enroll_token_registers(run, conn, client):
    code, out, _ = run("machine-enroll-token")
    assert code == 0
    lines = out.splitlines()
    token = lines[0].removeprefix("token: ")
    assert lines[2] == f"curl -fsSL {PUBLIC_URL}/install-agent.sh | sudo bash -s -- {PUBLIC_URL} {token}"
    r = client.post("/api/v1/machines/register", json={"enroll_token": token, "name": "cli-box"})
    assert r.status_code == 200


def test_assign_dispatch_by_machine_id_and_alias(run, conn, client):
    add_workload(conn, manifest_data("hello"))
    mid, _ = enroll(client, conn, "box1")
    code, out, _ = run("assign", mid, "hello")
    assert code == 0 and f"{mid} workload=hello epoch=2 state=pending" in out
    code, out, _ = run("machine-assign", "box1", "none")
    assert code == 0 and "workload=none epoch=3 state=stopped" in out
    code, _, err = run("assign", mid, "ghost")
    assert code == 1 and "unknown workload" in err
    code, _, err = run("machine-assign", "nobody", "hello")
    assert code == 1 and "unknown machine" in err
    # the Polymarket `assign GAME MODEL` still goes to the old command
    code, _, err = run("assign", "2025_01_KC_BAL", "00000000-0000-0000-0000-000000000000")
    assert "workload" not in err
    audit = conn.execute("SELECT actor FROM audit_log WHERE action = 'workload_assign'").fetchall()
    assert [a["actor"] for a in audit] == ["cli", "cli"]


def test_pin_and_unpin_with_prompt(run, conn, client, monkeypatch):
    mid, _ = enroll(client, conn, "box1")
    code, out, _ = run("pin", "box1", "--reason", "keep")
    assert code == 0 and "pinned: keep" in out
    monkeypatch.setattr("builtins.input", lambda prompt="": "nope")
    code, _, err = run("unpin", mid)
    assert code == 1 and "UNPIN box1" in err and conn.execute("SELECT pinned FROM machines").fetchone()["pinned"]
    monkeypatch.setattr("builtins.input", lambda prompt="": "UNPIN box1")
    code, out, _ = run("unpin", mid)
    assert code == 0 and "unpinned" in out and not conn.execute("SELECT pinned FROM machines").fetchone()["pinned"]
    assert run("unpin", mid, "--confirm", "UNPIN box1")[0] == 0
    assert run("pin", "ghost")[0] == 1


def test_secret_set_reads_stdin_and_never_prints_it(run, conn, secrets_key, monkeypatch):
    add_workload(conn, manifest_data("hello"))
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret value\n"))
    code, out, _ = run("secret-set", "hello", "HELLO_GREETING")
    assert code == 0 and "s3cret" not in out and "HELLO_GREETING set (container)" in out
    from host.workloads import secrets as wl_secrets
    row = conn.execute("SELECT * FROM workload_secrets").fetchone()
    assert wl_secrets._decrypt(wl_secrets.secret_box(), "hello", row) == "s3cret value"
    monkeypatch.setattr("sys.stdin", io.StringIO("x"))
    assert run("secret-set", "hello", "OTHER")[0] == 1


def test_outbound_cli_lists_approves_rejects(run, conn):
    add_workload(conn, manifest_data("hello"))
    a = outbound.queue_action(conn, workload="hello", machine_id=None, job_id=None, kind="email",
                              payload={"to": "a@b.c", "subject": "Hi"}, dedupe_key="a")
    b = outbound.queue_action(conn, workload="hello", machine_id=None, job_id=None, kind="log",
                              payload={"m": 1}, dedupe_key="b")
    code, out, _ = run("outbound")
    assert code == 0 and "email to a@b.c: Hi" in out and str(b["id"]) in out
    assert run("outbound", "--approve", str(a["id"]))[0] == 0
    assert run("outbound", "--reject", str(b["id"]), "--reason", "no")[0] == 0
    assert run("outbound", "--approve", str(a["id"]))[0] == 1
    statuses = {r["dedupe_key"]: r["status"] for r in conn.execute("SELECT * FROM outbound_actions")}
    assert statuses == {"a": "approved", "b": "rejected"}


def test_wl_send_job(run, conn, client):
    add_workload(conn, manifest_data("hello"))
    mid, _ = enroll(client, conn, "box1")
    code, out, _ = run("wl-send-job", "hello", "hello", "--params", '{"name": "z"}', "--target", "box1",
                       "--idempotency-key", "k")
    assert code == 0 and out.startswith("created job ") and f"target={mid}" in out
    code, out, _ = run("wl-send-job", "hello", "hello", "--idempotency-key", "k")
    assert out.startswith("existing job ")
    row = conn.execute("SELECT * FROM workload_jobs").fetchone()
    assert row["params"] == {"name": "z"} and row["target_machine_id"] == mid
    assert run("wl-send-job", "hello", "nope")[0] == 1
    assert run("wl-send-job", "hello", "hello", "--params", "{bad")[0] == 1


# ------------------------------------------------------------------ loop


def test_workloads_loop_step_runs_reaper_pins_outbound_and_pruning(pool, conn, client, make_worker):
    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn, "box1")
    epoch = client.post(f"/api/machines/{mid}/assign", json={"workload": "hello"}).json()["epoch"]
    job = queue.create_job(conn, workload="hello", kind="hello", params={})
    row = queue.claim(conn, machine_id=mid, workload="hello", epoch=epoch, kinds=None)
    conn.execute("UPDATE workload_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (job["id"],))
    old = outbound.queue_action(conn, workload="hello", machine_id=None, job_id=None, kind="log", payload={}, dedupe_key="o")
    conn.execute("UPDATE outbound_actions SET created_at = now() - interval '9 days'")
    approved = outbound.queue_action(conn, workload="hello", machine_id=None, job_id=None, kind="log", payload={}, dedupe_key="p")
    outbound.approve(conn, approved["id"], "t", None)
    conn.execute("INSERT INTO machine_logs (machine_id, ts, stream, line) VALUES (%s, now() - interval '4 days', 'stdout', 'old')", (mid,))
    conn.execute("INSERT INTO machine_logs (machine_id, ts, stream, line) VALUES (%s, now(), 'stdout', 'new')", (mid,))
    wl_loop.run_once(pool)
    wl_loop.send_once(pool)
    assert conn.execute("SELECT status FROM workload_jobs").fetchone()["status"] == "queued"
    statuses = {r["dedupe_key"]: r["status"] for r in conn.execute("SELECT * FROM outbound_actions")}
    assert statuses == {"o": "expired", "p": "sent"}, "the default log sender sends approved log actions"
    assert [r["line"] for r in conn.execute("SELECT line FROM machine_logs")] == ["new"]
    assert row["id"] == job["id"] and old["id"]


def test_log_pruning_keeps_the_newest_5000_lines_per_machine(conn, client, monkeypatch):
    mid, _ = enroll(client, conn, "box1")
    monkeypatch.setattr(wl_loop, "LOG_LINES_PER_MACHINE", 5)
    conn.execute("INSERT INTO machine_logs (machine_id, ts, stream, line)"
                 " SELECT %s, now(), 'stdout', 'l' || g FROM generate_series(1, 9) g", (mid,))
    assert wl_loop.prune_logs(conn) == 4
    assert [r["line"] for r in conn.execute("SELECT line FROM machine_logs ORDER BY id")] == ["l5", "l6", "l7", "l8", "l9"]


def test_a_failing_workloads_step_does_not_stop_the_others(pool, conn, monkeypatch, caplog):
    add_workload(conn, manifest_data("hello"))
    outbound.queue_action(conn, workload="hello", machine_id=None, job_id=None, kind="log", payload={}, dedupe_key="o")
    conn.execute("UPDATE outbound_actions SET created_at = now() - interval '9 days'")

    def boom(_conn):
        raise RuntimeError("reaper exploded")

    monkeypatch.setattr(queue, "reap", boom)
    wl_loop.run_once(pool)
    assert conn.execute("SELECT status FROM outbound_actions").fetchone()["status"] == "expired"
    assert "reap" in caplog.text and "reaper exploded" in caplog.text


def test_the_polymarket_loop_is_untouched_and_the_workloads_run_in_their_own_threads(pool, conn, monkeypatch):
    """host/loop.py is byte-identical to main: a hanging SMTP server or a slow workloads query
    can never delay the reaper, the dispatcher or the orphan cancel."""
    import inspect
    import subprocess

    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    main_loop = subprocess.run(["git", "show", "main:host/loop.py"], cwd=root, capture_output=True, text=True)
    if main_loop.returncode == 0:
        assert (root / "host" / "loop.py").read_text() == main_loop.stdout
    assert "workloads" not in inspect.getsource(host_loop)
    calls = []
    monkeypatch.setattr(wl_loop, "run_once", lambda p: calls.append(("loop", p)))
    monkeypatch.setattr(wl_loop, "send_once", lambda p: calls.append(("send", p)))
    threads = wl_loop.start_threads(pool, 0.01)
    try:
        for _ in range(200):
            if {"loop", "send"} <= {c[0] for c in calls}:
                break
            import time

            time.sleep(0.01)
    finally:
        for thread in threads:
            thread.stop()
        for thread in threads:
            thread.join(2)
    assert {"loop", "send"} <= {c[0] for c in calls}
    assert [t.name for t in threads] == ["workloads-loop", "outbound-sender"]


def test_the_sender_sends_a_bounded_batch_per_pass(pool, conn):
    add_workload(conn, manifest_data("hello"))
    for i in range(7):
        a = outbound.queue_action(conn, workload="hello", machine_id=None, job_id=None, kind="log", payload={}, dedupe_key=f"k{i}")
        outbound.approve(conn, a["id"], "t", None)
    assert wl_loop.send_once(pool) == wl_loop.SEND_BATCH
    assert wl_loop.send_once(pool) == 7 - wl_loop.SEND_BATCH


def test_a_sync_never_changes_how_an_assigned_workload_runs(conn, client, tmp_path):
    """Review H2: a changed image repository (which clears the digest) or runtime must not
    reach machines that run the workload, pinned live traders included."""
    from host.workloads import registry

    add_workload(conn, manifest_data("hello"))
    mid, token = enroll(client, conn, "box1")
    assert client.post(f"/api/machines/{mid}/assign", json={"workload": "hello"}).status_code == 200
    folder = tmp_path / "hello"
    folder.mkdir()
    data = manifest_data("hello")
    lines = ['name = "hello"', 'image = "fleet/other"', 'protocol = "workload-v1"',
             "[resources]", f"min_ram_mb = {data['resources']['min_ram_mb']}", "[runtime]", 'mode = "jobs"',
             'job_kinds = ["hello"]']
    (folder / "workload.toml").write_text("\n".join(lines) + "\n")
    result = registry.sync_from_dir(conn, tmp_path)
    assert result["synced"] == [] and "assigned to 1 machine" in result["errors"]["hello"][0]
    row = conn.execute("SELECT image_repo, image_digest FROM workloads WHERE name = 'hello'").fetchone()
    assert row["image_repo"] == data["image"] and row["image_digest"] is not None
    assert beat(client, mid, token).json()["run"] is not None
    client.post(f"/api/machines/{mid}/assign", json={"workload": None})
    assert registry.sync_from_dir(conn, tmp_path)["synced"] == ["hello"]


def test_a_broken_workloads_package_never_takes_the_polymarket_cli_down(monkeypatch, capsys):
    """Review L2: kill, cancel-all and every other existing command keep working even if a
    workloads module cannot be imported."""
    import builtins

    real_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "host.workloads" or name.startswith("host.workloads."):
            raise ImportError("simulated broken workloads module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "workloads commands unavailable" in capsys.readouterr().err
