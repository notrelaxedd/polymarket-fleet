"""Smoke tests for host.cli against the per-test database (main(argv) and one subprocess)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

import pytest

from host import queue
from host.cli import main
from tests.conftest import heartbeat_body, job_row, worker_row

REPO = Path(__file__).resolve().parent.parent
PUBLIC_URL = "http://127.0.0.1:8080"


@pytest.fixture
def cli_env(test_db_url: str, monkeypatch) -> dict[str, str]:
    """Point Config.from_env at the per-test database in dev mode."""
    env = {"DATABASE_URL": test_db_url, "FLEET_DEV": "1", "FLEET_PUBLIC_URL": PUBLIC_URL}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def run(cli_env, capsys) -> Callable[..., tuple[int, str, str]]:
    """Run main(argv) and return (exit code, stdout, stderr)."""

    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def test_migrate_is_idempotent(run) -> None:
    code, out, _ = run("migrate")
    assert code == 0 and "nothing (up to date)" in out


def test_enroll_token_prints_a_usable_token(run, conn) -> None:
    code, out, _ = run("enroll-token")
    assert code == 0
    lines = out.splitlines()
    token = lines[0].removeprefix("token: ")
    assert len(token) > 30 and lines[1].startswith("expires_at: ")
    assert lines[2] == f"curl -fsSL {PUBLIC_URL}/install.sh | sudo bash -s -- {PUBLIC_URL} {token}"
    reply = queue.register(conn, {"enroll_token": token, "hostname": "box9"})
    assert reply["worker_id"].startswith("w_") and worker_row(conn, reply["worker_id"])["name"] == "box9"


def test_workers_lists_the_fleet(run, make_worker) -> None:
    code, out, _ = run("workers")
    assert code == 0 and out.splitlines()[0].split() == [
        "id", "name", "online", "desired_role", "reported_role", "role_epoch", "acked_epoch",
        "auto_role", "enabled", "cpu_pct", "ram_used_mb", "code_version", "jobs",
    ]
    assert len(out.splitlines()) == 1, "no workers yet"
    w = make_worker("box1")
    make_worker("box2", online=False)
    code, out, _ = run("workers")
    rows = {line.split()[0]: line.split() for line in out.splitlines()[1:]}
    assert rows[w.id][1:5] == ["box1", "True", "idle", "idle"]
    assert len(rows) == 2 and "False" in rows[[k for k in rows if k != w.id][0]]


def test_send_job_targets_an_idle_worker(run, make_worker, conn) -> None:
    w = make_worker("box1")
    code, out, _ = run("send-job", "sleep", "--target", "any_idle", "--params", '{"seconds": 4}')
    assert code == 0
    assert out.startswith("created job ") and f"status=queued target={w.id}" in out and "waiting" not in out
    job_id = out.split()[2]
    assert job_row(conn, job_id)["params"] == {"seconds": 4}
    worker = worker_row(conn, w.id)
    assert worker["desired_role"] == "backtest" and worker["auto_role"] is True and worker["role_epoch"] == 2
    _, out, _ = run("jobs", "--status", "queued")
    assert job_id in out and "sleep" in out
    _, out, _ = run("jobs", "--status", "succeeded")
    assert job_id not in out
    _, out, _ = run("workers")
    row = next(line for line in out.splitlines() if line.startswith(w.id)).split()
    assert row[3:5] == ["backtest", "idle"] and row[7] == "True"
    assert row[-1] == "-", "a queued job is not a current job until it is leased"


def test_send_job_waits_without_idle_worker(run) -> None:
    code, out, _ = run("send-job", "sleep", "--target", "any_idle", "--idempotency-key", "k1")
    assert code == 0 and "target=None (waiting for an idle worker)" in out
    first = out.split()[2]
    code, out, _ = run("send-job", "sleep", "--target", "any_idle", "--idempotency-key", "k1")
    assert code == 0 and out.startswith(f"existing job {first} ")
    code, _, err = run("send-job", "mystery")
    assert code == 1 and "unknown job kind" in err
    code, _, err = run("send-job", "sleep", "--target", "w_nope")
    assert code == 1 and "worker not found" in err


def test_role_and_cancel(run, make_worker, conn) -> None:
    w = make_worker("box1")
    code, out, _ = run("role", w.id, "train")
    assert code == 0 and out.strip() == f"{w.id} desired_role=train role_epoch=2"
    assert worker_row(conn, w.id)["desired_role"] == "train"
    code, _, err = run("role", w.id, "chef")
    assert code == 1 and err.startswith("error: unknown role")
    code, _, err = run("role", "w_nope", "idle")
    assert code == 1 and "worker not found" in err
    _, out, _ = run("send-job", "sleep")
    job_id = out.split()[2]
    code, out, _ = run("cancel", job_id)
    assert code == 0 and out.strip() == f"job {job_id} status=cancelled"
    code, _, err = run("cancel", "garbage")
    assert code == 1 and "job not found" in err


def test_run_loop_dispatches(run, make_worker, conn) -> None:
    w = make_worker("box1")
    _, out, _ = run("send-job", "sleep")
    job_id = out.split()[2]
    assert job_row(conn, job_id)["target_worker_id"] is None
    code, out, _ = run("run-loop")
    assert code == 0 and json.loads(out) == {"reaped": 0, "dispatched": 1}
    assert job_row(conn, job_id)["target_worker_id"] == w.id
    code, out, _ = run("run-loop")
    assert code == 0 and json.loads(out) == {"reaped": 0, "dispatched": 0}


def test_module_entry_point(cli_env, make_worker) -> None:
    w = make_worker("box1")
    env = dict(os.environ, **cli_env)
    proc = subprocess.run(
        [sys.executable, "-m", "host.cli", "workers"], cwd=REPO, env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert w.id in proc.stdout
    proc = subprocess.run([sys.executable, "-m", "host.cli"], cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and "usage:" in proc.stderr


def _acking_agent(pool, conn, worker_id: str, stop: threading.Event) -> threading.Thread:
    """A thread that heartbeats every 0.1 s echoing whatever role the host desires."""

    def loop() -> None:
        while not stop.is_set():
            row = worker_row(conn, worker_id)
            with pool.connection() as c:
                queue.process_heartbeat(
                    c, worker_id, heartbeat_body(row["desired_role"], row["role_epoch"], want_job=False)
                )
            stop.wait(0.1)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def test_roletest_measures_the_role_ack(run, make_worker, pool, conn, monkeypatch) -> None:
    w = make_worker("box1")
    stop = threading.Event()
    agent = _acking_agent(pool, conn, w.id, stop)
    try:
        code, out, err = run("roletest", w.id, "--timeout", "10")
    finally:
        stop.set()
        agent.join(timeout=5)
    assert code == 0, (out, err)
    lines = out.splitlines()
    assert lines[0].startswith("sent sleep job ") and lines[1] == "set role train (epoch 3); waiting for the ack"
    match = re.fullmatch(r"role ack latency: (\d+\.\d\d) s", lines[2])
    assert match and 0 <= float(match.group(1)) < 5
    row = worker_row(conn, w.id)
    assert row["desired_role"] == "train" and row["acked_epoch"] == 3
    jobs = conn.execute("SELECT kind, params, status, target_worker_id FROM jobs").fetchall()
    assert jobs == [{"kind": "sleep", "params": {"seconds": 120}, "status": "cancelled", "target_worker_id": w.id}]
    actions = [a["action"] for a in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]
    assert actions == ["auto_role", "set_role"]
    # Over the limit: exit 1.
    monkeypatch.setattr("host.cli.ROLETEST_LIMIT_SECONDS", -1.0)
    stop = threading.Event()
    agent = _acking_agent(pool, conn, w.id, stop)
    try:
        code, out, err = run("roletest", w.id, "--timeout", "10")
    finally:
        stop.set()
        agent.join(timeout=5)
    assert code == 1 and "over the -1 s limit" in err


def test_roletest_fails_when_the_worker_never_acks(run, make_worker, conn) -> None:
    w = make_worker("box1")
    code, out, err = run("roletest", w.id, "--timeout", "0.5")
    assert code == 1 and "never acked" in err and "within 0.5 s" in err and "sent sleep job" in out
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "cancelled", "the sleep job is cleaned up on timeout"
    code, _, err = run("roletest", "w_nope")
    assert code == 1 and "worker not found" in err


def test_ingest_games_from_a_file_and_models_table(run, conn) -> None:
    from tests.conftest import FIXTURE_GAMES, backtest_metrics, insert_model

    code, out, _ = run("ingest-games", "--file", str(FIXTURE_GAMES))
    assert code == 0, out
    assert out.startswith("ingested 2761 rows from ") and "2761 inserted, 0 changed; last complete season 2025" in out
    assert conn.execute("SELECT count(*) AS n FROM games").fetchone()["n"] == 2761
    code, out, _ = run("ingest-games", "--file", str(FIXTURE_GAMES))
    assert code == 0 and "0 inserted, 0 changed" in out
    code, _, err = run("ingest-games", "--file", "/nonexistent/games.csv")
    assert code == 1 and "cannot read" in err
    code, out, _ = run("models")
    assert code == 0 and out.splitlines()[0].split() == [
        "rank", "id", "status", "family", "params", "roi", "bets", "log_loss", "market", "drawdown", "seasons", "rows",
    ]
    assert len(out.splitlines()) == 1
    top = insert_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 0}, metrics=backtest_metrics(n_bets=300, roi=0.05), status="paper_ok")
    insert_model(conn, params={"k": 21.0}, metrics=backtest_metrics(n_bets=10, roi=0.5))
    code, out, _ = run("models")
    lines = out.splitlines()
    assert code == 0 and len(lines) == 3
    assert lines[1].split()[:4] == ["1", str(top["id"])[:8], "paper_ok", "elo_blend"] and "K 20 · HFA 50 · MOV off" in lines[1]
    assert lines[2].split()[0] == "-" and "candidate" in lines[2]
