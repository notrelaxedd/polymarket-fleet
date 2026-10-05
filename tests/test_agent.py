"""Agent tests against tests/fake_host.py with a 0.2 s heartbeat and a tmp state dir."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any, Iterator

import pytest

from fleet.common import http
from fleet.worker import config, context, launch, posts, update
from fleet.worker.__main__ import main as cli_main
from fleet.worker.agent import EXIT_CONF_MISSING, EXIT_UPDATED, Agent, AgentOptions, RunningJob
from tests.fake_host import TEST_JOBS_SPEC, FakeHost, build_worker_tarball

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(REPO, "deploy", "install_worker.sh")
GAMES_FIXTURE = os.path.join(REPO, "tests", "fixtures", "games_sample.csv")

HB = 0.2


@pytest.fixture
def host() -> Iterator[FakeHost]:
    fake = FakeHost(lease_seconds=30.0, heartbeat_seconds=HB).start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> str:
    directory = str(tmp_path / "state")
    os.makedirs(directory)
    monkeypatch.setenv("FLEET_STATE_DIR", directory)
    return directory


class AgentThread:
    """Runs Agent.run_forever() in a thread and records its return value."""

    def __init__(self, state_dir: str, **overrides) -> None:
        options = AgentOptions(heartbeat_seconds=HB, http_timeout=2.0, shutdown_flush_delay=0.1)
        for key, value in overrides.items():
            setattr(options, key, value)
        self.agent = Agent(state_dir=state_dir, options=options)
        self.result: list[int] = []
        self.thread = threading.Thread(target=lambda: self.result.append(self.agent.run_forever()), daemon=True)

    def start(self) -> "AgentThread":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.agent.stop.set()
        self.thread.join(15.0)


@pytest.fixture
def enrolled(host: FakeHost, state_dir: str) -> str:
    """Enroll through the CLI and return the worker id."""
    token = host.mint_enroll_token()
    assert cli_main(["enroll", f"--host={host.url}", f"--token={token}", "--name=box1"]) == 0
    return config.load_conf(state_dir)["worker_id"]


@pytest.fixture
def running(host: FakeHost, state_dir: str, enrolled: str) -> Iterator[AgentThread]:
    runner = AgentThread(state_dir).start()
    try:
        yield runner
    finally:
        runner.stop()


def _leased(host: FakeHost, job_id: str, min_elapsed: int = 0):
    def check():
        job = host.job(job_id)
        return job["status"] == "leased" and (job["checkpoint"] or {}).get("elapsed", 0) >= min_elapsed

    return check


# -------------------------------------------------------------------- tests


def test_enroll_writes_conf_0600(host: FakeHost, state_dir: str, enrolled: str) -> None:
    path = config.conf_path(state_dir)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    conf = config.load_conf(state_dir)
    assert conf["host_url"] == host.url
    assert conf["worker_id"] == enrolled
    assert host.worker(enrolled)["name"] == "box1"
    assert host.worker(enrolled)["token"] == conf["worker_token"]
    assert cli_main(["status"]) == 0


def test_register_rotates_token_and_rewrites_conf(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread) -> None:
    old = config.load_conf(state_dir)["worker_token"]
    host.wait_for(lambda: running.agent.heartbeat_count >= 1)
    new = config.load_conf(state_dir)["worker_token"]
    assert new != old
    assert host.worker(enrolled)["token"] == new


def test_register_adopts_held_job_and_resumes_from_checkpoint(host: FakeHost, state_dir: str, enrolled: str) -> None:
    host.set_desired_role(enrolled, "backtest")
    job_id = host.enqueue_job("sleep", {"seconds": 4})
    old_token = host.lease_to(enrolled, job_id, checkpoint={"elapsed": 3}, progress=0.75)
    started = time.monotonic()
    agent = AgentThread(state_dir).start()
    try:
        host.wait_for(lambda: host.job(job_id)["status"] == "succeeded", timeout=8.0)
    finally:
        agent.stop()
    assert time.monotonic() - started < 3.0, "job was not resumed from its checkpoint"
    job = host.job(job_id)
    assert job["result"] == {"slept": 4}
    assert job["checkpoint"]["elapsed"] >= 3
    assert old_token != job["done_token"], "held job must get a fresh lease token"
    assert "re-leased" in host.job_events(job_id)


def test_claims_and_completes_sleep_job(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    host.set_desired_role(enrolled, "backtest")
    host.wait_for(lambda: host.worker(enrolled)["reported_role"] == "backtest")
    job_id = host.enqueue_job("sleep", {"seconds": 1})
    host.wait_for(lambda: host.job(job_id)["status"] == "succeeded", timeout=8.0)
    job = host.job(job_id)
    assert job["result"] == {"slept": 1}
    assert job["progress"] == 1.0
    assert host.job_events(job_id) == ["claimed", "succeeded"]
    assert running.agent.running == {}
    host.wait_for(lambda: running.agent.pending_posts == [], timeout=4.0)  # the agent pops the post just after the host records it


def test_role_change_mid_job_drains_releases_and_acks_quickly(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    job_id = host.enqueue_job("sleep", {"seconds": 10}, target=enrolled)
    host.wait_for(_leased(host, job_id, min_elapsed=1), timeout=8.0)
    before = len(host.heartbeats)
    started = time.monotonic()
    host.set_desired_role(enrolled, "idle")
    epoch = host.worker(enrolled)["role_epoch"]

    def acked():
        for hb in host.heartbeats[before:]:
            req = hb["request"]
            if req["reported_role"] == "idle" and req["acked_epoch"] == epoch:
                return hb
        return None

    ack = host.wait_for(acked, timeout=8.0)
    elapsed = time.monotonic() - started
    assert elapsed < HB + 3.0 + 1.0, f"ack took {elapsed:.2f}s"
    job = host.job(job_id)
    assert job["status"] == "queued"
    assert job["checkpoint"]["elapsed"] >= 1
    assert job["lease_token"] is None
    assert "released" in host.job_events(job_id)
    released_in_ack = any(r["id"] == job_id for r in ack["request"]["released"])
    released_by_call = any(m == "POST" and p.endswith(f"/jobs/{job_id}/checkpoint") and s == 200 for m, p, s in host.requests)
    assert released_in_ack or released_by_call
    assert running.agent.running == {}
    assert running.agent.role == "idle"
    assert ack["request"]["jobs"] == []


def test_epoch_bump_same_role_drains_via_checkpoint_release(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    host.set_desired_role(enrolled, "backtest")
    job_id = host.enqueue_job("sleep", {"seconds": 10})
    host.wait_for(_leased(host, job_id, min_elapsed=1), timeout=8.0)
    first_token = host.job(job_id)["lease_token"]
    host.set_desired_role(enrolled, "backtest")
    epoch = host.worker(enrolled)["role_epoch"]
    host.wait_for(lambda: host.worker(enrolled)["acked_epoch"] == epoch, timeout=8.0)
    assert any(m == "POST" and p.endswith(f"/jobs/{job_id}/checkpoint") and s == 200 for m, p, s in host.requests)
    assert "released" in host.job_events(job_id)
    host.wait_for(lambda: host.job(job_id)["lease_token"] not in (None, first_token), timeout=8.0)
    assert host.job(job_id)["checkpoint"]["elapsed"] >= 1


def test_preempt_releases_job_with_checkpoint(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    host.set_desired_role(enrolled, "backtest")
    job_id = host.enqueue_job("sleep", {"seconds": 10})
    host.wait_for(_leased(host, job_id, min_elapsed=1), timeout=8.0)
    token = host.job(job_id)["lease_token"]
    host.request_preempt(job_id)
    host.wait_for(lambda: "released" in host.job_events(job_id), timeout=8.0)
    released = [e for e in host.events if e["job_id"] == job_id and e["event"] == "released"][0]
    assert released["detail"]["checkpoint"]["elapsed"] >= 1
    assert not any(m == "POST" and p.endswith(f"/jobs/{job_id}/checkpoint") for m, p, s in host.requests), "preempt releases ride the heartbeat"
    host.wait_for(lambda: host.job(job_id)["lease_token"] not in (None, token), timeout=8.0)
    assert host.job(job_id)["checkpoint"]["elapsed"] >= 1


def test_cancel_requested_job_is_released_and_cancelled(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    host.set_desired_role(enrolled, "backtest")
    job_id = host.enqueue_job("sleep", {"seconds": 10})
    host.wait_for(_leased(host, job_id), timeout=8.0)
    host.cancel_job(job_id)
    host.wait_for(lambda: host.job(job_id)["status"] == "cancelled", timeout=8.0)
    assert running.agent.running == {}


def test_lost_job_kills_runner(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    host.set_desired_role(enrolled, "backtest")
    job_id = host.enqueue_job("sleep", {"seconds": 30})
    host.wait_for(_leased(host, job_id), timeout=8.0)
    rj = host.wait_for(lambda: running.agent.running.get(job_id))
    host.expire_lease(job_id)
    host.wait_for(lambda: job_id not in running.agent.running, timeout=8.0)
    assert rj.runner.wait(5.0)
    assert rj.runner.kill_sent
    assert rj.runner.poll() == -signal.SIGKILL
    assert any(job_id in hb["response"]["lost"] for hb in host.heartbeats)


def test_conf_missing_exits_78(state_dir: str) -> None:
    assert Agent(state_dir=state_dir, options=AgentOptions(heartbeat_seconds=HB)).run_forever() == EXIT_CONF_MISSING
    env = dict(os.environ, FLEET_STATE_DIR=state_dir)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [sys.executable, "-m", "fleet.worker", "run"], env=env, cwd=repo_root, capture_output=True, timeout=30
    )
    assert proc.returncode == 78
    assert b"worker.conf" in proc.stderr


def test_self_update_downloads_verifies_swaps_symlink_and_exits_75(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread) -> None:
    host.wait_for(lambda: running.agent.heartbeat_count >= 1)
    version = "0123abcd4567"
    host.set_code_version(version)
    running.thread.join(15.0)
    assert running.result == [EXIT_UPDATED]
    app = config.app_dir(state_dir)
    assert os.readlink(os.path.join(app, "current")) == version
    with open(os.path.join(app, "current", "fleet", "VERSION"), encoding="utf-8") as fh:
        assert fh.read().strip() == version
    assert os.path.isfile(os.path.join(app, version, "fleet", "worker", "agent.py"))
    assert not any(name.endswith(".tmp") or ".staging" in name for name in os.listdir(app))
    assert any(p == "/dl/version" for _, p, _ in host.requests)
    assert any(p == "/dl/worker.tar.gz" for _, p, _ in host.requests)


def test_self_update_refuses_bad_sha256(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread) -> None:
    host.wait_for(lambda: running.agent.heartbeat_count >= 1)
    host.set_code_version("badbadbadbad", sha256_override="f" * 64)
    host.wait_for(lambda: running.agent.last_error and "sha256" in running.agent.last_error, timeout=8.0)
    assert running.thread.is_alive()
    assert not os.path.lexists(os.path.join(config.app_dir(state_dir), "current"))


def test_self_update_refuses_tarball_escaping_fleet_dir(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread) -> None:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"print('owned')\n"
        for name in ("fleet/__init__.py", "../evil.py"):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    host.wait_for(lambda: running.agent.heartbeat_count >= 1)
    host.set_code_version("evil00000000", tarball=buf.getvalue())
    host.wait_for(lambda: running.agent.last_error and "unsafe path" in running.agent.last_error, timeout=8.0)
    assert running.thread.is_alive()
    assert not os.path.exists(os.path.join(state_dir, "evil.py"))
    assert not os.path.lexists(os.path.join(config.app_dir(state_dir), "current"))


def test_heartbeat_payload_shape(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    hb = host.wait_for(lambda: host.heartbeats[-1] if host.heartbeats else None)
    req = hb["request"]
    for key in ("cpu_pct", "ram_used_mb", "ram_total_mb", "reported_role", "acked_epoch", "jobs", "released", "want_job", "code_version", "skew_ms"):
        assert key in req
    assert req["reported_role"] == "idle"
    assert req["want_job"] is False
    host.wait_for(lambda: len(host.heartbeats) >= 4)
    gaps = [b["t"] - a["t"] for a, b in zip(host.heartbeats, host.heartbeats[1:])]
    assert all(HB * 0.5 < g < HB * 2.5 for g in gaps), gaps


def test_host_outage_degrades_then_kills_runners_and_reregisters(state_dir: str) -> None:
    host = FakeHost(lease_seconds=1.0, heartbeat_seconds=HB).start()
    token = host.mint_enroll_token()
    assert cli_main(["enroll", f"--host={host.url}", f"--token={token}"]) == 0
    worker_id = config.load_conf(state_dir)["worker_id"]
    running = AgentThread(state_dir).start()
    try:
        host.set_desired_role(worker_id, "backtest")
        job_id = host.enqueue_job("sleep", {"seconds": 30})
        host.wait_for(_leased(host, job_id), timeout=8.0)
        rj = host.wait_for(lambda: running.agent.running.get(job_id))
        assert running.agent.lease_seconds == 1.0
        host.stop()
        host.wait_for(lambda: running.agent.degraded, timeout=8.0)
        host.wait_for(lambda: running.agent.state == "REGISTER", timeout=8.0)
        assert rj.runner.wait(5.0)
        assert rj.runner.poll() == -signal.SIGKILL
        assert running.agent.running == {}
        assert running.thread.is_alive()
    finally:
        running.stop()


# ------------------------------------------------- review fixes: pending posts


def test_pending_complete_keeps_lease_alive_and_is_retried(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    """A /complete the host answers 5xx stays queued, its job stays in heartbeat jobs[]
    (lease renewed, no re-offer) and the post is retried until acknowledged."""
    host.set_desired_role(enrolled, "backtest")
    host.wait_for(lambda: host.worker(enrolled)["reported_role"] == "backtest")
    host.fail_next("complete", 503, count=3)
    job_id = host.enqueue_job("sleep", {"seconds": 1})
    host.wait_for(lambda: running.agent.pending_posts, timeout=8.0)
    pending_since = time.monotonic()
    lease_token = running.agent.pending_posts[0].body["lease_token"]
    hb = host.wait_for(
        lambda: next((h for h in host.heartbeats if h["t"] > pending_since and any(j["id"] == job_id for j in h["request"]["jobs"])), None),
        timeout=8.0,
    )
    entry = [j for j in hb["request"]["jobs"] if j["id"] == job_id][0]
    assert entry["lease_token"] == lease_token
    assert entry["progress"] == 1.0
    assert "checkpoint" not in entry
    host.wait_for(lambda: host.job(job_id)["status"] == "succeeded", timeout=8.0)
    assert host.job(job_id)["result"] == {"slept": 1}
    assert host.job_events(job_id) == ["claimed", "succeeded"], "job must not be re-offered or re-run"
    host.wait_for(lambda: running.agent.pending_posts == [], timeout=4.0)  # the agent pops the post just after the host records it
    assert not os.path.exists(config.pending_posts_path(running.agent.state_dir))
    failed = [s for m, p, s in host.requests if p.endswith(f"/jobs/{job_id}/complete")]
    assert failed.count(503) == 3 and failed[-1] == 200


def test_pending_post_survives_reregister_and_is_rekeyed(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread) -> None:
    """Token rotated elsewhere while /complete is unsent: the agent re-registers with
    the previous token, the host hands the job back with a fresh lease token, and the
    post is re-sent with that token instead of the job being run again."""
    host.set_desired_role(enrolled, "backtest")
    host.wait_for(lambda: host.worker(enrolled)["reported_role"] == "backtest")
    host.fail_next("complete", 503, count=1000)
    job_id = host.enqueue_job("sleep", {"seconds": 1})
    host.wait_for(lambda: running.agent.pending_posts, timeout=8.0)
    old_token = running.agent.pending_posts[0].body["lease_token"]
    assert os.path.exists(config.pending_posts_path(state_dir)), "unsent post must be persisted"
    host.rotate_token(enrolled)
    host.wait_for(lambda: "re-leased" in host.job_events(job_id), timeout=8.0)
    host.wait_for(lambda: running.agent.pending_posts and running.agent.pending_posts[0].body["lease_token"] != old_token, timeout=8.0)
    assert running.agent.running == {}, "a job with an unsent result must not be restarted"
    host.failures.clear()
    host.wait_for(lambda: host.job(job_id)["status"] == "succeeded", timeout=8.0)
    assert host.job(job_id)["result"] == {"slept": 1}
    assert host.job_events(job_id) == ["claimed", "re-leased", "succeeded"]
    assert host.worker(enrolled)["prev_token"] is None, "first heartbeat with the new token clears the previous one"


def test_unsent_posts_are_resent_on_next_start(host: FakeHost, state_dir: str, enrolled: str) -> None:
    """A /complete persisted by a crashed run is delivered by the next run."""
    job_id = host.enqueue_job("sleep", {"seconds": 5})
    token = host.lease_to(enrolled, job_id)
    config.save_pending_posts(state_dir, [{"path": f"/api/v1/jobs/{job_id}/complete", "body": {"lease_token": token, "result": {"slept": 5}}, "job_id": job_id, "progress": 1.0}])
    agent = AgentThread(state_dir).start()
    try:
        host.wait_for(lambda: host.job(job_id)["status"] == "succeeded", timeout=8.0)
        assert agent.agent.running == {}, "the held job must be re-keyed, not run"
    finally:
        agent.stop()
    assert host.job(job_id)["result"] == {"slept": 5}
    assert not os.path.exists(config.pending_posts_path(state_dir))


def test_self_update_waits_for_pending_posts(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread) -> None:
    host.set_desired_role(enrolled, "backtest")
    host.wait_for(lambda: host.worker(enrolled)["reported_role"] == "backtest")
    host.fail_next("complete", 503, count=6)
    job_id = host.enqueue_job("sleep", {"seconds": 1})
    host.wait_for(lambda: running.agent.pending_posts, timeout=8.0)
    host.set_code_version("0123abcd4567")
    host.wait_for(lambda: host.job(job_id)["status"] == "succeeded", timeout=8.0)
    assert running.thread.is_alive() or running.result == [EXIT_UPDATED]
    running.thread.join(10.0)
    assert running.result == [EXIT_UPDATED]
    assert host.job(job_id)["status"] == "succeeded"
    host.wait_for(lambda: running.agent.pending_posts == [], timeout=4.0)  # the agent pops the post just after the host records it


# ---------------------------------------------------- review fixes: shutdown


def test_shutdown_final_heartbeat_sends_want_job_false_and_releases(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    host.set_desired_role(enrolled, "backtest")
    job_id = host.enqueue_job("sleep", {"seconds": 10})
    host.wait_for(_leased(host, job_id, min_elapsed=1), timeout=8.0)
    running.stop()
    assert running.thread.is_alive() is False
    final = host.heartbeats[-1]["request"]
    assert final["want_job"] is False
    assert [r["id"] for r in final["released"]] == [job_id]
    job = host.job(job_id)
    assert job["status"] == "queued"
    assert job["lease_worker_id"] is None
    assert job["checkpoint"]["elapsed"] >= 1
    assert host.job_events(job_id) == ["claimed", "released"], "the dying agent must not be re-offered the job"


def test_shutdown_completes_finished_runner_instead_of_releasing(host: FakeHost, state_dir: str, enrolled: str) -> None:
    """A runner that printed done just before SIGTERM is completed (with bounded retries), not released."""
    agent = Agent(state_dir=state_dir, options=AgentOptions(heartbeat_seconds=HB, http_timeout=2.0, shutdown_flush_delay=0.05))
    assert agent.boot() and agent.register_once()
    job_id = host.enqueue_job("sleep", {"seconds": 1})
    token = host.lease_to(enrolled, job_id)
    agent.handle_response({"claimed": [{"id": job_id, "kind": "sleep", "params": {"seconds": 1}, "checkpoint": None, "lease_token": token, "lease_seconds": 30}]})
    rj = agent.running[job_id]
    assert rj.runner.wait(10.0)
    host.fail_next("complete", 503, count=2)
    agent.shutdown()
    assert agent.pending_posts == []
    job = host.job(job_id)
    assert job["status"] == "succeeded"
    assert job["result"] == {"slept": 1}
    assert "released" not in host.job_events(job_id)
    assert [s for m, p, s in host.requests if p.endswith("/complete")] == [503, 503, 200]


# ----------------------------------------------- review fixes: lease deadline


def test_runner_killed_before_host_lease_can_expire(state_dir: str) -> None:
    """With heartbeats failing, runners die lease_seconds - heartbeat - http_timeout after
    the last acknowledged heartbeat was sent, which is before the host lease expires."""
    host = FakeHost(lease_seconds=3.0, heartbeat_seconds=HB).start()
    token = host.mint_enroll_token()
    assert cli_main(["enroll", f"--host={host.url}", f"--token={token}"]) == 0
    worker_id = config.load_conf(state_dir)["worker_id"]
    running = AgentThread(state_dir).start()
    try:
        host.set_desired_role(worker_id, "backtest")
        job_id = host.enqueue_job("sleep", {"seconds": 30})
        host.wait_for(_leased(host, job_id), timeout=8.0)
        rj = host.wait_for(lambda: running.agent.running.get(job_id))
        assert running.agent.lease_seconds == 3.0
        assert running.agent.lease_deadline() == pytest.approx(1.5)
        host.fail_next("heartbeat", 503, count=10_000)
        host.wait_for(lambda: rj.runner.kill_sent, timeout=8.0)
        killed_at = time.monotonic()
        last_ok = host.heartbeats[-1]["t"]
        assert rj.runner.wait(5.0)
        assert 1.3 <= killed_at - last_ok < 3.0, f"killed {killed_at - last_ok:.2f}s after the last acknowledged heartbeat"
        assert rj.runner.poll() == -signal.SIGKILL
        host.wait_for(lambda: "re-leased" in host.job_events(job_id), timeout=8.0)
    finally:
        running.stop()
        host.stop()


def test_lease_deadline_checked_in_service_loop_not_only_on_tick(state_dir: str, monkeypatch) -> None:
    """Heartbeats fail fast and the deadline falls between two 5 s ticks: the runner is
    killed by the 0.1 s service loop at the deadline, not at the next tick."""
    from fleet.worker import agent as agent_mod

    clock = {"t": 100.0}

    def sleep(seconds: float) -> None:
        clock["t"] += 0.5
        if clock["t"] > 140.0:
            agent.stop.set()

    agent = Agent(state_dir=state_dir, options=AgentOptions(heartbeat_seconds=5.0, http_timeout=4.0), clock=lambda: clock["t"], sleep=sleep)
    agent.conf = {"host_url": "http://127.0.0.1:1", "worker_id": "w_x", "worker_token": "t"}
    agent.lease_seconds = 30.0
    agent.last_ok_at = 100.0

    class FakeRunner:
        kill_sent = False
        killed_at = None
        outcome = None

        def snapshot(self):
            return None, 0.0, 0

        def kill(self):
            self.kill_sent = True
            self.killed_at = clock["t"]

        def wait(self, timeout=None):
            return True

        def reap_group(self, timeout=2.0):
            pass

    runner = FakeRunner()
    agent.running["j1"] = RunningJob(job={"id": "j1"}, lease_token="tok", runner=runner)  # type: ignore[arg-type]

    def failing_post(*args, **kwargs):
        raise agent_mod.http.HttpConnectionError("Network is unreachable")

    monkeypatch.setattr(agent_mod.http, "post_json", failing_post)
    assert agent.active_loop() is None, "the agent must go back to REGISTER"
    assert agent.lease_deadline() == 21.0
    assert runner.kill_sent
    assert runner.killed_at == pytest.approx(121.0), f"killed at t={runner.killed_at}; ticks are at 120 and 125"
    assert agent.running == {}


# ---------------------------------------------- review fixes: self-update rollback


def _run_installed_agent(state_dir: str) -> subprocess.CompletedProcess:
    app = config.app_dir(state_dir)
    env = dict(os.environ, FLEET_STATE_DIR=state_dir, PYTHONPATH=os.path.join(app, "current"))
    env.pop("PYTHONSAFEPATH", None)
    return subprocess.run([sys.executable, "-m", "fleet.worker", "run"], env=env, cwd=state_dir, capture_output=True, timeout=60)


def test_self_update_rolls_back_after_three_failed_starts(host: FakeHost, state_dir: str, enrolled: str) -> None:
    app = config.app_dir(state_dir)
    host.set_code_version("good01")
    update.install_version(app, "good01", host.tarball())
    update.swap_current(app, "good01")
    running = AgentThread(state_dir, code_version="good01").start()
    try:
        host.wait_for(lambda: running.agent.heartbeat_count >= 1)
        bad = build_worker_tarball("bad002", overrides={"worker/agent.py": b'raise RuntimeError("boom at import")\n'})
        host.set_code_version("bad002", tarball=bad)
        running.thread.join(15.0)
    finally:
        running.stop()
    assert running.result == [EXIT_UPDATED]
    assert os.readlink(os.path.join(app, "current")) == "bad002"
    assert launch.read_previous(app) == "good01"
    assert launch.read_pending(app) == {"version": "bad002", "starts": 0}

    for attempt in (1, 2):
        proc = _run_installed_agent(state_dir)
        assert proc.returncode == 1, proc.stderr.decode()
        assert b"boom at import" in proc.stderr
        assert launch.read_pending(app) == {"version": "bad002", "starts": attempt}
        assert os.readlink(os.path.join(app, "current")) == "bad002"
    proc = _run_installed_agent(state_dir)
    assert proc.returncode == 75, proc.stderr.decode()
    assert b"rolled back" in proc.stderr
    assert os.readlink(os.path.join(app, "current")) == "good01"
    assert launch.read_pending(app) is None
    assert launch.bad_versions(app) == {"bad002"}

    # The restored code refuses to update to the bad version again.
    again = AgentThread(state_dir, code_version="good01").start()
    try:
        host.wait_for(lambda: again.agent.last_error and "bad_versions" in again.agent.last_error, timeout=8.0)
        assert again.thread.is_alive()
        assert os.readlink(os.path.join(app, "current")) == "good01"
        assert launch.read_pending(app) is None, "a successful register clears pending.json"
    finally:
        again.stop()


def test_successful_register_clears_pending_version(host: FakeHost, state_dir: str, enrolled: str) -> None:
    app = config.app_dir(state_dir)
    launch.write_pending(app, "whatever", 2)  # before the agent starts: its register clears the file
    running = AgentThread(state_dir).start()
    try:
        host.wait_for(lambda: running.agent.heartbeat_count >= 1)
        assert launch.read_pending(app) is None
    finally:
        running.stop()


# ------------------------------------------------- review fixes: low findings


def test_release_now_5xx_is_carried_in_next_heartbeat(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    job_id = host.enqueue_job("sleep", {"seconds": 10}, target=enrolled)
    host.wait_for(_leased(host, job_id, min_elapsed=1), timeout=8.0)
    host.fail_next("checkpoint", 500, count=1)
    host.set_desired_role(enrolled, "idle")
    host.wait_for(lambda: host.job(job_id)["status"] == "queued", timeout=8.0)
    assert "released" in host.job_events(job_id)
    assert host.job(job_id)["checkpoint"]["elapsed"] >= 1
    assert any(any(r["id"] == job_id for r in hb["request"]["released"]) for hb in host.heartbeats), "release must ride a heartbeat after the 500"
    assert running.agent.pending_releases == []


def test_checkpoint_is_resent_when_heartbeat_fails(state_dir: str, monkeypatch) -> None:
    from fleet.worker import agent as agent_mod

    agent = Agent(state_dir=state_dir, options=AgentOptions(heartbeat_seconds=HB))
    agent.conf = {"host_url": "http://127.0.0.1:1", "worker_id": "w_x", "worker_token": "t"}

    class FakeRunner:
        def snapshot(self):
            return {"elapsed": 1}, 0.1, 1

    agent.running["j1"] = RunningJob(job={"id": "j1"}, lease_token="tok", runner=FakeRunner())  # type: ignore[arg-type]
    first = agent.build_heartbeat()
    assert first["jobs"][0]["checkpoint"] == {"elapsed": 1}
    monkeypatch.setattr(agent_mod.http, "post_json", lambda *a, **k: (_ for _ in ()).throw(agent_mod.http.HttpConnectionError("down")))
    with pytest.raises(agent_mod.http.HttpConnectionError):
        agent._post_heartbeat(first)
    second = agent.build_heartbeat()
    assert second["jobs"][0]["checkpoint"] == {"elapsed": 1}, "a checkpoint lost with a failed heartbeat must be resent"
    monkeypatch.setattr(agent_mod.http, "post_json", lambda *a, **k: {"desired_role": "idle"})
    agent._post_heartbeat(second)
    third = agent.build_heartbeat()
    assert "checkpoint" not in third["jobs"][0]


def test_job_failure_is_logged_to_stderr(state_dir: str, caplog) -> None:
    agent = Agent(state_dir=state_dir, options=AgentOptions(heartbeat_seconds=HB))
    with caplog.at_level(logging.WARNING, logger="fleet.agent"):
        agent._queue_post("fail", "j1", "tok", {"error": "Traceback (most recent call last):\n  File x\nZeroDivisionError: division by zero\n"}, 0.5)
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ZeroDivisionError: division by zero" in m and "j1" in m for m in messages)
    assert any("Traceback" in m for m in messages)
    assert agent.pending_posts[0].progress == 0.5


def test_enroll_token_from_environment(host: FakeHost, state_dir: str, monkeypatch, capsys) -> None:
    monkeypatch.setenv("FLEET_ENROLL_TOKEN", host.mint_enroll_token())
    assert cli_main(["enroll", f"--host={host.url}"]) == 0
    assert config.load_conf(state_dir)["worker_id"] in host.workers
    monkeypatch.delenv("FLEET_ENROLL_TOKEN")
    assert cli_main(["enroll", f"--host={host.url}"]) == 2
    assert "FLEET_ENROLL_TOKEN" in capsys.readouterr().err


# ------------------------------------------------------ review fixes: installer


def _installer_checker() -> str:
    with open(INSTALLER, encoding="utf-8") as fh:
        text = fh.read()
    match = re.search(r"# BEGIN tarball-check\n(.*?)# END tarball-check\n", text, re.S)
    assert match, "installer must carry the tarball check between the markers"
    return match.group(1)


def _run_checker(tarball: bytes, tmp_path) -> subprocess.CompletedProcess:
    script = tmp_path / "check.py"
    script.write_text(_installer_checker(), encoding="utf-8")
    path = tmp_path / "worker.tar.gz"
    path.write_bytes(tarball)
    return subprocess.run([sys.executable, str(script), str(path)], capture_output=True, timeout=30)


def _hostile_tarball(kind: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"print('hi')\n"
        info = tarfile.TarInfo("fleet/__init__.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        extra = tarfile.TarInfo("fleet/mem")
        if kind == "device":
            extra.type = tarfile.CHRTYPE
            extra.devmajor, extra.devminor = 1, 1
        elif kind == "symlink":
            extra.name = "fleet/etc"
            extra.type = tarfile.SYMTYPE
            extra.linkname = "/etc"
        elif kind == "setuid":
            extra.name = "fleet/worker/__init__.py"
            extra.size = len(data)
            extra.mode = 0o4777
        elif kind == "escape":
            extra.name = "fleet/../evil.py"
            extra.size = len(data)
        elif kind == "pyc":
            extra.name = "fleet/__pycache__/x.pyc"
            extra.size = len(data)
        tar.addfile(extra, io.BytesIO(data) if extra.size else None)
    return buf.getvalue()


@pytest.mark.parametrize("kind,message", [("device", "unsupported member type"), ("symlink", "unsupported member type"), ("escape", "unsafe path"), ("pyc", "compiled file")])
def test_installer_rejects_hostile_tarball_members(tmp_path, kind: str, message: str) -> None:
    proc = _run_checker(_hostile_tarball(kind), tmp_path)
    assert proc.returncode == 1
    assert message in proc.stderr.decode()


def test_installer_accepts_real_tarball_and_extracts_without_archive_permissions(tmp_path) -> None:
    assert _run_checker(build_worker_tarball("abc123"), tmp_path).returncode == 0
    assert subprocess.run(["bash", "-n", INSTALLER], capture_output=True).returncode == 0
    with open(INSTALLER, encoding="utf-8") as fh:
        text = fh.read()
    assert "tar --no-same-owner --no-same-permissions --no-overwrite-dir -xzf" in text
    assert "check_tarball \"$WORK/worker.tar.gz\"" in text
    assert "FLEET_ENROLL_TOKEN" in text and "--token-file" in text
    assert "export FLEET_ENROLL_TOKEN" in text and '"--token=$ENROLL_TOKEN"' not in text
    assert "switching it to $HOST_URL" in text
    # The setuid bit in the archive is dropped by --no-same-permissions (checked with real tar).
    path = tmp_path / "setuid.tar.gz"
    path.write_bytes(_hostile_tarball("setuid"))
    dest = tmp_path / "extract"
    dest.mkdir()
    subprocess.run(["tar", "--no-same-owner", "--no-same-permissions", "--no-overwrite-dir", "-xzf", str(path), "-C", str(dest)], check=True)
    mode = stat.S_IMODE(os.stat(dest / "fleet" / "worker" / "__init__.py").st_mode)
    assert not mode & stat.S_ISUID


# ------------------------------------------------- step 2: memory watchdog


def _alive(pid: int) -> bool:
    """True while the process exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state not in ("Z", "X")


def _status(state_dir: str) -> dict[str, Any]:
    return config.load_status(state_dir) or {}


def test_watchdog_trips_on_real_sleep_job_and_releases_with_reason_oom(host: FakeHost, state_dir: str, enrolled: str, caplog) -> None:
    """ram_total_mb=4 puts the limit at 3.2 MB; a python child uses more than that."""
    from fleet.common import sysinfo

    running = AgentThread(state_dir, ram_total_mb=4).start()
    try:
        with caplog.at_level(logging.WARNING, logger="fleet.watchdog"):
            host.set_desired_role(enrolled, "backtest")
            job_id = host.enqueue_job("sleep", {"seconds": 30})
            rj = host.wait_for(lambda: running.agent.running.get(job_id), timeout=8.0)
            pid = rj.runner.pid
            assert pid is not None
            host.wait_for(lambda: host.releases(job_id), timeout=15.0)
        release = host.releases(job_id)[0]
        assert release["reason"] == "oom"
        assert release["checkpoint"]["elapsed"] >= 1, "the unit in flight finishes before the stop; its checkpoint is released"
        assert rj.runner.poll() is not None, "runner child must have exited"
        assert not _alive(pid), "runner child left as a zombie"
        assert sysinfo.session_pids(pid) == [], "runner session must be empty"
        assert running.agent.running.get(job_id) is not rj, "a re-offered job gets a fresh runner, the tripped one is gone"
        host.wait_for(lambda: _status(state_dir).get("watchdog_trips", 0) >= 1, timeout=8.0)
        messages = [r.getMessage() for r in caplog.records if r.name == "fleet.watchdog"]
        assert any("memory watchdog" in m and job_id in m and "MB" in m for m in messages), messages
        assert any(re.search(r"over 3 MB \(80% of 4 MB\)", m) for m in messages), messages
        # The host re-offers the job and it trips again: after max_expiries oom releases it fails.
        host.wait_for(lambda: host.job(job_id)["status"] == "failed", timeout=25.0)
        job = host.job(job_id)
        assert job["expiries"] == 3
        assert [r["reason"] for r in host.releases(job_id)] == ["oom", "oom", "oom"]
        host.wait_for(lambda: _status(state_dir).get("watchdog_trips") == 3, timeout=8.0)
    finally:
        running.stop()
    assert running.agent.running == {}
    assert not _alive(pid)


def test_watchdog_does_not_trip_with_realistic_ram_total(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread) -> None:
    assert running.agent.watchdog.limit_mb() is not None and running.agent.watchdog.limit_mb() > 100
    host.set_desired_role(enrolled, "backtest")
    job_id = host.enqueue_job("sleep", {"seconds": 2})
    host.wait_for(lambda: host.job(job_id)["status"] == "succeeded", timeout=10.0)
    assert host.releases(job_id) == []
    assert running.agent.watchdog.trips == 0
    host.wait_for(lambda: "watchdog_trips" in _status(state_dir), timeout=5.0)
    assert _status(state_dir)["watchdog_trips"] == 0


def test_watchdog_patched_measurement_of_zero_never_trips(host: FakeHost, state_dir: str, enrolled: str, monkeypatch) -> None:
    from fleet.common import sysinfo

    calls: list[int] = []

    def zero(sid: int, proc_root: str = sysinfo.PROC_ROOT) -> int:
        calls.append(sid)
        return 0

    monkeypatch.setattr(sysinfo, "session_rss_kb", zero)
    running = AgentThread(state_dir, ram_total_mb=4).start()
    try:
        host.set_desired_role(enrolled, "backtest")
        job_id = host.enqueue_job("sleep", {"seconds": 2})
        host.wait_for(lambda: host.job(job_id)["status"] == "succeeded", timeout=10.0)
    finally:
        running.stop()
    assert calls, "the watchdog must have measured the runner"
    assert host.releases(job_id) == []
    assert running.agent.watchdog.trips == 0
    assert host.job(job_id)["result"] == {"slept": 2}


def test_watchdog_checks_each_runner_at_most_once_per_second(state_dir: str, monkeypatch) -> None:
    from fleet.common import sysinfo
    from fleet.worker.watchdog import MemoryWatchdog

    clock = {"t": 50.0}
    measured: list[float] = []
    monkeypatch.setattr(sysinfo, "session_rss_kb", lambda sid, proc_root=None: measured.append(clock["t"]) or 0)

    class FakeRunner:
        pid = 4242

    wd = MemoryWatchdog(fraction=0.8, ram_total_mb=4, clock=lambda: clock["t"])
    running = {"j1": RunningJob(job={"id": "j1"}, lease_token="t", runner=FakeRunner())}  # type: ignore[arg-type]
    for _ in range(12):
        assert wd.over_limit(running) == []
        clock["t"] += 0.25
    assert measured == [51.0, 52.0], "first check one interval after the runner was seen, then every second"
    monkeypatch.setattr(sysinfo, "session_rss_kb", lambda sid, proc_root=None: 4 * 1024)
    clock["t"] = 53.5
    assert wd.over_limit(running) == ["j1"]
    assert wd.trips == 1
    running.clear()
    assert wd.over_limit(running) == []


# ------------------------------------------------- step 2: release reasons


def test_drain_release_carries_reason_drain(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    job_id = host.enqueue_job("sleep", {"seconds": 10}, target=enrolled)
    host.wait_for(_leased(host, job_id, min_elapsed=1), timeout=8.0)
    host.set_desired_role(enrolled, "idle")
    host.wait_for(lambda: host.releases(job_id), timeout=8.0)
    assert host.releases(job_id) == [{"checkpoint": {"elapsed": pytest.approx(1, abs=1)}, "reason": "drain"}]
    assert host.releases(job_id)[0]["checkpoint"]["elapsed"] >= 1


def test_preempt_release_carries_reason_preempt(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    host.set_desired_role(enrolled, "backtest")
    job_id = host.enqueue_job("sleep", {"seconds": 10})
    host.wait_for(_leased(host, job_id, min_elapsed=1), timeout=8.0)
    host.request_preempt(job_id)
    host.wait_for(lambda: host.releases(job_id), timeout=8.0)
    assert host.releases(job_id)[0]["reason"] == "preempt"
    hb = next(h for h in host.heartbeats if any(r["id"] == job_id for r in h["request"]["released"]))
    assert [r["reason"] for r in hb["request"]["released"]] == ["preempt"]
    assert host.job(job_id)["expiries"] == 0


def test_cancel_release_carries_reason_cancel(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    host.set_desired_role(enrolled, "backtest")
    job_id = host.enqueue_job("sleep", {"seconds": 10})
    host.wait_for(_leased(host, job_id), timeout=8.0)
    host.cancel_job(job_id)
    host.wait_for(lambda: host.job(job_id)["status"] == "cancelled", timeout=8.0)
    assert host.releases(job_id)[0]["reason"] == "cancel"


def test_shutdown_release_carries_reason_shutdown(host: FakeHost, enrolled: str, running: AgentThread) -> None:
    host.set_desired_role(enrolled, "backtest")
    job_id = host.enqueue_job("sleep", {"seconds": 10})
    host.wait_for(_leased(host, job_id, min_elapsed=1), timeout=8.0)
    running.stop()
    final = host.heartbeats[-1]["request"]
    assert [(r["id"], r["reason"]) for r in final["released"]] == [(job_id, "shutdown")]
    assert host.releases(job_id)[0]["reason"] == "shutdown"


def test_release_now_body_and_heartbeat_entries_carry_reason(state_dir: str, monkeypatch) -> None:
    from fleet.worker import agent as agent_mod

    agent = Agent(state_dir=state_dir, options=AgentOptions(heartbeat_seconds=HB))
    agent.conf = {"host_url": "http://127.0.0.1:1", "worker_id": "w_x", "worker_token": "t"}

    class FakeRunner:
        def snapshot(self):
            return {"elapsed": 2}, 0.2, 2

    rj = RunningJob(job={"id": "j1"}, lease_token="tok", runner=FakeRunner())  # type: ignore[arg-type]
    entry = agent._release_entry(rj, "oom")
    assert entry == {"id": "j1", "lease_token": "tok", "progress": 0.2, "checkpoint": {"elapsed": 2}, "reason": "oom"}
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(agent_mod.http, "post_json", lambda url, body, **kw: sent.append(body) or {"status": "queued"})
    assert agent._release_now(entry) is True
    assert sent == [{"lease_token": "tok", "checkpoint": {"elapsed": 2}, "progress": 0.2, "release": True, "reason": "oom"}]
    agent.pending_releases.append(entry)
    assert agent.build_heartbeat()["released"][0]["reason"] == "oom"


# ------------------------------------------------- step 2: kill flag scope


def test_kill_does_not_stop_batch_claims_and_is_recorded_in_status(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread) -> None:
    host.set_kill(True)
    host.set_desired_role(enrolled, "backtest")
    host.wait_for(lambda: running.agent.kill is True, timeout=8.0)
    job_id = host.enqueue_job("sleep", {"seconds": 1})
    host.wait_for(lambda: host.job(job_id)["status"] == "succeeded", timeout=10.0)
    assert host.job(job_id)["result"] == {"slept": 1}
    assert host.job_events(job_id) == ["claimed", "succeeded"]
    claim_hb = next(h for h in host.heartbeats if any(j["id"] == job_id for j in h["response"]["claimed"]))
    assert claim_hb["request"]["want_job"] is True
    assert claim_hb["response"]["kill"] is True
    host.wait_for(lambda: _status(state_dir).get("kill") is True, timeout=5.0)
    assert running.agent.wants_job() is True
    assert running.agent.on_kill() is None, "on_kill is a no-op for batch roles until step 4"
    host.set_kill(False)
    host.wait_for(lambda: _status(state_dir).get("kill") is False, timeout=5.0)
    assert running.agent.kill is False


def test_kill_transition_calls_on_kill_once(state_dir: str, monkeypatch) -> None:
    agent = Agent(state_dir=state_dir, options=AgentOptions(heartbeat_seconds=HB))
    calls: list[str] = []
    monkeypatch.setattr(agent, "on_kill", lambda: calls.append("kill"))
    agent._apply_common_fields({"kill": True})
    agent._apply_common_fields({"kill": True})
    assert calls == ["kill"] and agent.kill is True
    agent._apply_common_fields({"kill": False})
    agent._apply_common_fields({"kill": True})
    assert calls == ["kill", "kill"]


# ------------------------------------------------- step 3: games cache and context


def _games_rows(limit: int = 40) -> list[dict[str, str]]:
    with open(GAMES_FIXTURE, newline="", encoding="utf-8") as fh:
        return [row for _, row in zip(range(limit), csv.DictReader(fh))]


@pytest.fixture
def test_jobs(monkeypatch) -> None:
    """Runner children use the echo jobs from tests/fake_host.py for the batch kinds."""
    monkeypatch.setenv("FLEET_TEST_JOBS", TEST_JOBS_SPEC)


def _run_batch_job(host: FakeHost, enrolled: str, kind: str, params: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    """Send a job to the worker (flipping its role) and wait for it to finish."""
    job_id = host.enqueue_job(kind, params, target=enrolled)
    host.wait_for(lambda: host.job(job_id)["status"] in ("succeeded", "failed"), timeout=timeout)
    return host.job(job_id)


def _request_index(host: FakeHost, method: str, suffix: str, status: int | None = None) -> int:
    for i, (m, p, s) in enumerate(host.requests):
        if m == method and p.endswith(suffix) and (status is None or s == status):
            return i
    raise AssertionError(f"no {method} ...{suffix} in {host.requests}")


def test_games_cache_is_fetched_before_the_backtest_runner(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread, test_jobs) -> None:
    rows = _games_rows(40)
    etag = host.set_games(rows)
    job = _run_batch_job(host, enrolled, "backtest", {"family": "elo_blend", "params": {"k": 20}})
    assert job["status"] == "succeeded", job["error"]
    result = job["result"]
    assert result["games_rows"] == 40, "the runner must see the fetched rows in games_path"
    assert result["context_keys"] == ["games_path", "model"]
    assert result["model"] is None
    assert result["params"] == {"family": "elo_blend", "params": {"k": 20}}, "params are untouched apart from _context"
    cache = config.games_cache_path(state_dir)
    assert cache == os.path.join(state_dir, "cache", "games.json")
    with open(cache, encoding="utf-8") as fh:
        assert json.load(fh) == rows
    with open(config.games_etag_path(state_dir), encoding="utf-8") as fh:
        assert fh.read().strip() == etag
    assert _request_index(host, "GET", "/api/v1/data/games", 200) < _request_index(host, "POST", f"/jobs/{job['id']}/complete", 200)
    assert "created_models" not in result


def test_games_cache_304_keeps_the_file_and_a_change_refreshes_it(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread, test_jobs) -> None:
    rows = _games_rows(30)
    host.set_games(rows)
    first = _run_batch_job(host, enrolled, "model_search", {"family": "elo_blend", "n": 1, "seed": 1})
    assert first["status"] == "succeeded", first["error"]
    cache = config.games_cache_path(state_dir)
    stat_before = os.stat(cache)
    second = _run_batch_job(host, enrolled, "train", {"through": {"season": 2024, "week": 1}})
    assert second["status"] == "succeeded", second["error"]
    assert second["result"]["games_rows"] == 30
    assert [s for m, p, s in host.requests if p == "/api/v1/data/games"] == [200, 304]
    assert os.stat(cache).st_mtime_ns == stat_before.st_mtime_ns and os.stat(cache).st_ino == stat_before.st_ino, "304 must not rewrite the cache"
    new_rows = _games_rows(55)
    new_etag = host.set_games(new_rows)
    third = _run_batch_job(host, enrolled, "backtest", {"family": "elo_blend", "params": {}})
    assert third["status"] == "succeeded", third["error"]
    assert third["result"]["games_rows"] == 55
    assert [s for m, p, s in host.requests if p == "/api/v1/data/games"] == [200, 304, 200]
    with open(config.games_etag_path(state_dir), encoding="utf-8") as fh:
        assert fh.read().strip() == new_etag


@pytest.mark.parametrize("status", [0, 503])
def test_games_fetch_failure_uses_the_cached_file_with_a_warning(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread, test_jobs, caplog, status: int) -> None:
    rows = _games_rows(25)
    context.write_games_cache(state_dir, rows, "stale-etag")
    host.fail_next("data/games", status=status, count=5)
    with caplog.at_level(logging.WARNING, logger="fleet.context"):
        job = _run_batch_job(host, enrolled, "backtest", {"family": "elo_blend", "params": {}})
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["games_rows"] == 25
    assert any("games refresh failed" in r.getMessage() and "cached" in r.getMessage() for r in caplog.records), caplog.records
    assert (("GET", "/api/v1/data/games", status) in host.requests)
    with open(config.games_cache_path(state_dir), encoding="utf-8") as fh:
        assert json.load(fh) == rows


def test_games_fetch_failure_without_a_cache_fails_the_job_before_any_runner(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread, test_jobs, tmp_path) -> None:
    host.fail_next("data/games", status=503, count=50)
    marker = tmp_path / "ran"
    job = _run_batch_job(host, enrolled, "backtest", {"family": "elo_blend", "params": {}, "marker": str(marker)})
    assert job["status"] == "failed"
    assert "games data unavailable" in job["error"] and "503" in job["error"], job["error"]
    assert not marker.exists(), "no runner must start without games data"
    assert host.job_events(job["id"]) == ["claimed", "failed"]
    assert running.agent.running == {}
    assert not os.path.exists(config.games_cache_path(state_dir))


def test_model_is_fetched_into_the_runner_context(host: FakeHost, enrolled: str, running: AgentThread, test_jobs) -> None:
    host.set_games(_games_rows(10))
    model_id = host.add_model({"family": "elo_blend", "params": {"k": 24.0, "hfa": 55.0}, "artifact": {"ratings": {"KC": 1600.0}, "through": [2024, 5]}})
    job = _run_batch_job(host, enrolled, "train", {"model_id": model_id, "through": {"season": 2024, "week": 8}})
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["model"] == host.models()[0]
    assert job["result"]["model"]["id"] == model_id and job["result"]["model"]["artifact"]["ratings"] == {"KC": 1600.0}
    assert job["result"]["context_keys"] == ["games_path", "model"]
    assert _request_index(host, "GET", f"/api/v1/models/{model_id}", 200) < _request_index(host, "POST", f"/jobs/{job['id']}/complete")


def test_unknown_model_fails_the_job(host: FakeHost, enrolled: str, running: AgentThread, test_jobs, tmp_path) -> None:
    host.set_games(_games_rows(10))
    marker = tmp_path / "ran"
    job = _run_batch_job(host, enrolled, "backtest", {"model_id": "00000000-0000-0000-0000-000000000000", "marker": str(marker)})
    assert job["status"] == "failed"
    assert "model 00000000-0000-0000-0000-000000000000 unavailable" in job["error"] and "404" in job["error"]
    assert not marker.exists()


def test_get_json_etag_round_trip(host: FakeHost, enrolled: str) -> None:
    etag = host.set_games([{"game_id": "x"}])
    token = host.worker(enrolled)["token"]
    first = http.get_json_etag(host.url + "/api/v1/data/games", token=token)
    assert (first.status, first.body, first.etag) == (200, [{"game_id": "x"}], etag)
    second = http.get_json_etag(host.url + "/api/v1/data/games", token=token, etag=etag)
    assert (second.status, second.body, second.etag) == (304, None, etag)
    assert http.get_json(host.url + "/api/v1/data/games", token=token) == [{"game_id": "x"}], "the plain API still works"
    with pytest.raises(http.HttpError) as exc:
        http.get_json_etag(host.url + "/api/v1/data/games", token="wrong")
    assert exc.value.status == 401


# ------------------------------------------------- step 3: create_models posts


def _candidate(k: float, **extra: Any) -> dict[str, Any]:
    entry = {"family": "elo_blend", "params": {"k": k, "hfa": 55.0}, "artifact": None, "backtest_metrics": {"n_bets": 10, "roi": 0.03}, "summary": f"k {k}", "trained_through": None}
    entry.update(extra)
    return entry


def test_create_models_are_posted_in_order_before_complete(host: FakeHost, enrolled: str, running: AgentThread, test_jobs) -> None:
    host.set_games(_games_rows(10))
    search_result = {"evaluated": 2, "seasons": [2010, 2024], "top": [], "create_models": [_candidate(20.0), _candidate(30.0)]}
    job = _run_batch_job(host, enrolled, "model_search", {"family": "elo_blend", "n": 2, "seed": 1, "result": search_result})
    assert job["status"] == "succeeded", job["error"]
    calls = host.model_posts()
    assert [c["path"] for c in calls] == ["/api/v1/models", "/api/v1/models"]
    assert [c["body"]["params"]["k"] for c in calls] == [20.0, 30.0], "posted in result order"
    for call in calls:
        assert call["body"]["job_id"] == job["id"]
        assert set(call["body"]) == {"job_id", "family", "params", "artifact", "backtest_metrics", "summary", "parent_model_id", "trained_through",
                                     "validation_metrics", "stress_metrics"}
        assert call["body"]["parent_model_id"] is None
    result = job["result"]
    assert "create_models" not in result
    assert result["created_models"] == [
        {"id": calls[0]["response"]["id"], "lineage_id": calls[0]["response"]["id"], "created": True},
        {"id": calls[1]["response"]["id"], "lineage_id": calls[1]["response"]["id"], "created": True},
    ]
    assert result["evaluated"] == 2 and result["seasons"] == [2010, 2024]
    model_posts = [i for i, (m, p, s) in enumerate(host.requests) if m == "POST" and p == "/api/v1/models"]
    assert len(model_posts) == 2 and max(model_posts) < _request_index(host, "POST", f"/jobs/{job['id']}/complete", 200)
    assert len(host.models()) == 2
    host.wait_for(lambda: running.agent.pending_posts == [], timeout=4.0)  # the agent pops the post just after the host records it
    assert not os.path.exists(config.pending_posts_path(running.agent.state_dir))


def test_create_models_resume_after_a_crash_between_posts_without_duplicates(host: FakeHost, state_dir: str, enrolled: str, test_jobs) -> None:
    """The host parks the second model post, the agent is stopped while it is unsent
    (a crash: shutdown cannot flush it), and the next start resumes the sequence from
    the persisted index: the first model is not posted again."""
    host.set_games(_games_rows(10))
    host.hold_posts("models", skip=1)
    first = AgentThread(state_dir, http_timeout=0.5).start()
    try:
        search_result = {"evaluated": 2, "create_models": [_candidate(20.0), _candidate(30.0)]}
        job_id = host.enqueue_job("model_search", {"family": "elo_blend", "n": 2, "seed": 1, "result": search_result}, target=enrolled)
        host.wait_for(lambda: len(host.model_posts()) == 1, timeout=15.0)
        host.wait_for(lambda: (config.load_pending_posts(state_dir) or [{}])[0].get("index") == 1, timeout=10.0)
    finally:
        first.stop()
    saved = config.load_pending_posts(state_dir)
    assert len(saved) == 1 and saved[0]["index"] == 1 and len(saved[0]["steps"]) == 2
    assert saved[0]["body"]["result"]["created_models"] == [{"id": host.models()[0]["id"], "lineage_id": host.models()[0]["id"], "created": True}]
    assert len(host.model_posts()) == 1 and len(host.models()) == 1
    assert host.job(job_id)["status"] == "leased", "the lease is kept alive while the sequence is unsent"
    host.release_holds()

    second = AgentThread(state_dir).start()
    try:
        host.wait_for(lambda: host.job(job_id)["status"] == "succeeded", timeout=15.0)
        assert second.agent.running == {}, "the job must be resumed as posts, not re-run"
    finally:
        second.stop()
    calls = host.model_posts()
    assert [c["body"]["params"]["k"] for c in calls] == [20.0, 30.0]
    assert [c["response"]["created"] for c in calls] == [True, True]
    assert len(host.models()) == 2
    assert host.job(job_id)["result"]["created_models"] == [
        {"id": calls[0]["response"]["id"], "lineage_id": calls[0]["response"]["id"], "created": True},
        {"id": calls[1]["response"]["id"], "lineage_id": calls[1]["response"]["id"], "created": True},
    ]
    assert host.job_events(job_id) == ["claimed", "re-leased", "succeeded"]
    assert [s for m, p, s in host.requests if m == "POST" and p == "/api/v1/models" and s == 200] == [200, 200]
    assert not os.path.exists(config.pending_posts_path(state_dir))


def test_existing_model_is_returned_with_created_false(host: FakeHost, enrolled: str, running: AgentThread, test_jobs) -> None:
    host.set_games(_games_rows(10))
    existing = host.add_model({"family": "elo_blend", "params": {"k": 20.0, "hfa": 55.0}})
    job = _run_batch_job(host, enrolled, "model_search", {"family": "elo_blend", "result": {"create_models": [_candidate(20.0), _candidate(21.0)]}})
    assert job["status"] == "succeeded", job["error"]
    created = job["result"]["created_models"]
    assert created[0] == {"id": existing, "lineage_id": existing, "created": False}
    assert created[1]["created"] is True and created[1]["id"] != existing
    assert len(host.models()) == 2


def test_backtest_with_model_id_posts_metrics_before_complete(host: FakeHost, enrolled: str, running: AgentThread, test_jobs) -> None:
    host.set_games(_games_rows(10))
    model_id = host.add_model({"family": "elo_blend", "params": {"k": 24.0}})
    metrics = {"n_games": 100, "n_bets": 12, "roi": 0.05, "max_drawdown": 0.1, "log_loss": 0.66, "per_season": {"2020": {"n_bets": 12}}}
    job = _run_batch_job(host, enrolled, "backtest", {"model_id": model_id, "result": metrics})
    assert job["status"] == "succeeded", job["error"]
    calls = host.model_posts()
    assert len(calls) == 1 and calls[0]["path"] == f"/api/v1/models/{model_id}/backtest"
    assert calls[0]["body"]["job_id"] == job["id"]
    posted = calls[0]["body"]["backtest_metrics"]
    assert {k: posted[k] for k in metrics} == metrics
    assert host.models()[0]["backtest_metrics"]["roi"] == 0.05
    assert _request_index(host, "POST", f"/models/{model_id}/backtest", 200) < _request_index(host, "POST", f"/jobs/{job['id']}/complete", 200)
    assert "created_models" not in job["result"]
    assert job["result"]["model"]["id"] == model_id


def test_backtest_by_family_posts_nothing(host: FakeHost, enrolled: str, running: AgentThread, test_jobs) -> None:
    host.set_games(_games_rows(10))
    job = _run_batch_job(host, enrolled, "backtest", {"family": "elo_blend", "params": {"k": 24.0}, "result": {"n_bets": 3, "roi": 0.0}})
    assert job["status"] == "succeeded", job["error"]
    assert host.model_posts() == []
    assert job["result"]["n_bets"] == 3


def test_model_post_refused_by_the_host_fails_the_job(host: FakeHost, enrolled: str, running: AgentThread, test_jobs) -> None:
    host.set_games(_games_rows(10))
    bad = {"params": {"k": 1.0}, "artifact": None}  # no family -> 400 from the host
    job = _run_batch_job(host, enrolled, "model_search", {"family": "elo_blend", "result": {"create_models": [bad, _candidate(2.0)]}})
    assert job["status"] == "failed"
    assert "/api/v1/models refused (400" in job["error"], job["error"]
    assert host.model_posts() == [], "the sequence stops at the refused step"
    host.wait_for(lambda: running.agent.pending_posts == [], timeout=4.0)  # the agent pops the post just after the host records it


def test_model_post_5xx_is_retried_without_duplicating_models(host: FakeHost, enrolled: str, running: AgentThread, test_jobs) -> None:
    host.set_games(_games_rows(10))
    host.fail_next("models", status=503, count=2)
    job = _run_batch_job(host, enrolled, "model_search", {"family": "elo_blend", "result": {"create_models": [_candidate(5.0), _candidate(6.0)]}})
    assert job["status"] == "succeeded", job["error"]
    statuses = [s for m, p, s in host.requests if m == "POST" and p == "/api/v1/models"]
    assert statuses == [503, 503, 200, 200]
    assert len(host.models()) == 2
    assert [c["created"] for c in job["result"]["created_models"]] == [True, True]


def test_pending_post_sequence_round_trips_through_json() -> None:
    job = {"id": "j9", "kind": "backtest", "params": {"model_id": "m1"}}
    post = posts.complete_post(job, "tok", {"n_bets": 1, "create_models": [_candidate(1.0)]})
    assert [s["kind"] for s in post.steps] == ["model", "backtest"]
    assert post.steps[1]["path"] == "/api/v1/models/m1/backtest"
    assert post.steps[1]["body"]["backtest_metrics"] == {"n_bets": 1}
    assert post.body == {"lease_token": "tok", "result": {"n_bets": 1, "created_models": []}}
    data = json.loads(json.dumps(post.to_dict()))
    back = posts.PendingPost.from_dict(data)
    assert back == post
    data["index"] = 7
    assert posts.PendingPost.from_dict(data).index == 2, "a stale index is clamped to the step count"
    plain = posts.complete_post({"id": "j1", "kind": "sleep", "params": {"seconds": 1}}, "tok", {"slept": 1})
    assert plain.steps == [] and plain.to_dict() == {"path": "/api/v1/jobs/j1/complete", "body": {"lease_token": "tok", "result": {"slept": 1}}, "job_id": "j1", "progress": 1.0, "attempts": 0}


def test_validate_with_model_id_posts_validation_before_complete(host: FakeHost, enrolled: str, running: AgentThread, test_jobs) -> None:
    """Step 6: a validate job's result goes to POST /api/v1/models/{id}/validation
    (job_id, validation_metrics, stress_metrics) before /complete; the host stores both."""
    host.set_games(_games_rows(10))
    model_id = host.add_model({"family": "elo_blend", "params": {"k": 24.0}, "backtest_metrics": {"n_bets": 300, "roi": 0.05}})
    validation = {"n_games": 1100, "n_bets": 80, "roi": 0.03, "era": "validation", "ci": {"roi": [-0.01, 0.07]}, "market_p": 0.2, "flags": []}
    stress = {"prices": [{"name": "spread+0.02", "n_bets": 60, "roi": 0.01, "log_loss": 0.6, "mean_ll_gain": 0.0}], "neighbourhood": {"n": 10}, "regimes": {}, "flags": ["fragile"], "seed": 1}
    job = _run_batch_job(host, enrolled, "validate", {"model_id": model_id, "seed": 1, "result": {"validation_metrics": validation, "stress_metrics": stress}})
    assert job["status"] == "succeeded", job["error"]
    calls = host.model_posts()
    assert len(calls) == 1 and calls[0]["path"] == f"/api/v1/models/{model_id}/validation"
    assert set(calls[0]["body"]) == {"job_id", "validation_metrics", "stress_metrics"}
    assert calls[0]["body"]["job_id"] == job["id"]
    assert calls[0]["body"]["validation_metrics"] == validation and calls[0]["body"]["stress_metrics"] == stress
    stored = host.models()[0]
    assert stored["validation_metrics"] == validation and stored["stress_metrics"] == stress
    assert stored["backtest_metrics"] == {"n_bets": 300, "roi": 0.05}, "the search-era metrics are untouched"
    assert _request_index(host, "POST", f"/models/{model_id}/validation", 200) < _request_index(host, "POST", f"/jobs/{job['id']}/complete", 200)
    assert job["result"]["model"]["id"] == model_id, "the validate runner gets the model in its context"
    assert job["result"]["games_rows"] == 10 and "created_models" not in job["result"]
    assert job["result"]["validation_metrics"] == validation


def test_validate_refused_by_the_host_fails_the_job(host: FakeHost, enrolled: str, running: AgentThread, test_jobs) -> None:
    host.set_games(_games_rows(10))
    model_id = host.add_model({"family": "elo_blend", "params": {"k": 24.0}})
    job = _run_batch_job(host, enrolled, "validate", {"model_id": model_id, "result": {"validation_metrics": {"n_bets": 1}, "stress_metrics": None}})
    assert job["status"] == "failed" and f"/api/v1/models/{model_id}/validation refused (400" in job["error"], job["error"]
    assert host.models()[0]["validation_metrics"] is None


def test_search_create_models_carry_validation_fields_through_unchanged(host: FakeHost, enrolled: str, running: AgentThread, test_jobs) -> None:
    host.set_games(_games_rows(10))
    validation = {"n_bets": 40, "roi": 0.02, "era": "validation", "flags": ["overfit"]}
    stress = {"prices": [], "neighbourhood": {"n": 10}, "regimes": {}, "flags": [], "seed": 7}
    entries = [_candidate(20.0, validation_metrics=validation, stress_metrics=stress), _candidate(30.0, validation_metrics=None, stress_metrics=None)]
    job = _run_batch_job(host, enrolled, "model_search", {"family": "elo_blend", "n": 2, "seed": 7, "result": {"create_models": entries, "validation_note": "x"}})
    assert job["status"] == "succeeded", job["error"]
    calls = host.model_posts()
    assert [c["body"]["validation_metrics"] for c in calls] == [validation, None]
    assert [c["body"]["stress_metrics"] for c in calls] == [stress, None]
    assert [m["validation_metrics"] for m in host.models()] == [validation, None]
    assert job["result"]["validation_note"] == "x" and len(job["result"]["created_models"]) == 2


def test_validate_pending_post_sequence_round_trips_through_json() -> None:
    job = {"id": "v9", "kind": "validate", "params": {"model_id": "m1", "seed": 2}}
    result = {"validation_metrics": {"n_bets": 1, "era": "validation"}, "stress_metrics": {"flags": [], "seed": 2}}
    post = posts.complete_post(job, "tok", result)
    assert [s["kind"] for s in post.steps] == ["validation"]
    assert post.steps[0]["path"] == "/api/v1/models/m1/validation"
    assert post.steps[0]["body"] == {"job_id": "v9", "validation_metrics": result["validation_metrics"], "stress_metrics": result["stress_metrics"]}
    assert post.body == {"lease_token": "tok", "result": result}
    assert posts.PendingPost.from_dict(json.loads(json.dumps(post.to_dict()))) == post
    assert posts.complete_post({"id": "v1", "kind": "validate", "params": {}}, "tok", result).steps == [], "no model_id, nothing to post"
    assert posts.complete_post({"id": "b1", "kind": "backtest", "params": {"model_id": "m1"}}, "tok", result).steps[0]["kind"] == "backtest"


# ------------------------------------------------- step 4: trade role


TICK = 0.2


@pytest.fixture
def trader(host: FakeHost, state_dir: str, enrolled: str) -> Iterator[AgentThread]:
    """A running agent that ticks the trade loop every 0.2 s."""
    runner = AgentThread(state_dir, trade_tick_s=TICK).start()
    try:
        yield runner
    finally:
        runner.stop()


def _held_trade_jobs(host: FakeHost, worker_id: str) -> list[str]:
    with host.lock:
        return sorted(j["id"] for j in host.jobs.values() if j["kind"] == "trade" and j["lease_worker_id"] == worker_id and j["status"] == "leased")


def _ack_heartbeat(host: FakeHost, worker_id: str, role: str, epoch: int, since: int = 0):
    def find():
        for hb in host.heartbeats[since:]:
            req = hb["request"]
            if req["reported_role"] == role and req["acked_epoch"] == epoch:
                return hb
        return None

    return find


def test_trade_worker_claims_up_to_the_slots_ticks_and_proposes(host: FakeHost, state_dir: str, enrolled: str, trader: AgentThread) -> None:
    host.set_trade_settings(trade_max_games=2, trade_tick_s=TICK)
    first, second, third = (host.add_assignment() for _ in range(3))
    host.set_desired_role(enrolled, "trade")
    host.wait_for(lambda: len(_held_trade_jobs(host, enrolled)) == 2, timeout=8.0)
    assert trader.agent.running == {}, "trade jobs start no runner"
    assert sorted(trader.agent.trade_jobs) == _held_trade_jobs(host, enrolled)
    held = {host.job(j)["params"]["assignment_id"] for j in _held_trade_jobs(host, enrolled)}
    queued = ({first, second, third} - held).pop()
    assert host.job(host.assignment(queued)["job_id"])["status"] == "queued", "the third assignment waits for a free slot"
    claim_hb = next(h for h in host.heartbeats if h["response"]["claimed"])
    assert claim_hb["request"]["want_jobs"] == 2 and claim_hb["request"]["want_job"] is False
    host.wait_for(lambda: host.heartbeats[-1]["request"]["want_jobs"] == 0 and host.heartbeats[-1]["request"]["reported_role"] == "trade", timeout=8.0)
    assert sorted(j["id"] for j in host.heartbeats[-1]["request"]["jobs"]) == sorted(trader.agent.trade_jobs), "trade jobs are renewed like any lease"
    # One proposal per held assignment: my 0.65 vs home ask 0.55 has the edge, the away side does not.
    host.wait_for(lambda: len(host.orders(status="open")) == 2, timeout=8.0)
    orders = host.orders(status="open")
    assert {o["assignment_id"] for o in orders} == held
    fee = 0.05 * 0.55 * 0.45
    cost = 0.55 + fee
    edge = 0.65 - cost
    stake = int(0.25 * 10000 * edge / (1 - cost))
    size = int(stake / (cost * 100))
    assert size == 8
    for o in orders:
        assert o["market_id"] == host.market_ids(o["assignment_id"])["home"]
        assert o["price"] == 0.55 and o["size"] == size and o["cost_cents"] == int(round(size * cost * 100))
        assert o["rationale"] == f"my 0.65 vs ask 0.55, fee {fee:.3f}, edge {edge:.3f}"
        assert o["edge"] == pytest.approx(edge, abs=1e-6)
    for aid in held:
        bank = host.assignment(aid)["bankroll"]
        assert bank["reserved_cents"] == orders[0]["cost_cents"] and bank["available_cents"] == 10000 - orders[0]["cost_cents"]
    # The open order keeps its market from being proposed again; ticks continue.
    ticks = trader.agent.trade.ticks
    host.wait_for(lambda: trader.agent.trade.ticks >= ticks + 3, timeout=8.0)
    assert len(host.trade_calls("/orders/request")) == 2, "one open order per market"
    host.wait_for(lambda: (_status(state_dir).get("trade") or {}).get("last_tick"), timeout=5.0)
    status = _status(state_dir)["trade"]
    assert sorted(status["jobs"]) == sorted(trader.agent.trade_jobs) and status["max_games"] == 2
    assert {a["id"] for a in status["assignments"]} == held
    assert all(a["open_orders"] == 1 and a["status"] == "active" for a in status["assignments"])
    assert status["last_tick"]["assignments"] == 2 and status["last_tick"]["kill"] is False


def test_trade_worker_cancels_stale_orders(host: FakeHost, enrolled: str, trader: AgentThread) -> None:
    host.set_trade_settings(trade_tick_s=TICK)
    aid = host.add_assignment()
    host.set_desired_role(enrolled, "trade")
    host.wait_for(lambda: host.orders(aid, status="open"), timeout=8.0)
    order = host.orders(aid, status="open")[0]
    host.set_ask(host.market_ids(aid)["home"], 0.70)  # my 0.65 against a cost above 0.71: negative edge
    host.wait_for(lambda: host.orders(aid, status="cancelled"), timeout=8.0)
    assert host.orders(aid, status="cancelled")[0]["id"] == order["id"]
    assert any(c["path"].endswith(f"/orders/{order['id']}/cancel") for c in host.trade_calls())
    bank = host.assignment(aid)["bankroll"]
    assert bank["reserved_cents"] == 0 and bank["available_cents"] == 10000, "the fake released the reservation"
    ticks = trader.agent.trade.ticks
    host.wait_for(lambda: trader.agent.trade.ticks >= ticks + 2, timeout=8.0)
    assert len(host.orders(aid)) == 1, "no new proposal at a price without edge"
    assert trader.agent.trade.last_tick["cancelled"] in (0, 1)


def test_trade_worker_stops_proposing_under_kill_but_keeps_ticking(host: FakeHost, state_dir: str, enrolled: str, trader: AgentThread) -> None:
    host.set_trade_settings(trade_tick_s=TICK)
    aid = host.add_assignment()
    host.set_desired_role(enrolled, "trade")
    host.wait_for(lambda: host.orders(aid, status="open"), timeout=8.0)
    host.set_kill(True)
    host.halt_assignment(aid)  # what the real kill does: orders cancelled, assignment halted
    host.wait_for(lambda: trader.agent.kill is True, timeout=8.0)
    killed_at = time.monotonic()
    asked_before = len(host.trade_calls("/orders/request"))
    ticks = trader.agent.trade.ticks
    host.wait_for(lambda: trader.agent.trade.ticks >= ticks + 3, timeout=8.0)
    assert trader.agent.trade.last_tick["kill"] is True and trader.agent.trade.last_tick["proposed"] == 0
    assert [c for c in host.trade_calls("/orders/request") if c["t"] > killed_at] == [], "no request once the kill flag arrived"
    assert len(host.trade_calls("/orders/request")) == asked_before
    assert sorted(trader.agent.trade_jobs) == _held_trade_jobs(host, enrolled), "the trade job stays held under kill"
    host.wait_for(lambda: _status(state_dir).get("kill") is True and (_status(state_dir).get("trade") or {}).get("last_tick", {}).get("kill") is True, timeout=5.0)
    assert _status(state_dir)["trade"]["assignments"][0]["status"] == "halted"
    # Reset the kill (assignments stay halted): still nothing. Reactivate: proposals resume.
    host.set_kill(False)
    host.wait_for(lambda: trader.agent.kill is False, timeout=8.0)
    ticks = trader.agent.trade.ticks
    host.wait_for(lambda: trader.agent.trade.ticks >= ticks + 2, timeout=8.0)
    assert len(host.trade_calls("/orders/request")) == asked_before, "a halted assignment gets no proposal"
    with host.lock:
        host.trade.assignments[aid]["status"] = "active"
    host.set_ask(host.market_ids(aid)["home"], 0.55)  # a new snapshot: the old client_request_id would be a duplicate
    host.wait_for(lambda: host.orders(aid, status="open"), timeout=8.0)
    assert trader.agent.trade.last_tick["kill"] is False


def test_role_change_away_from_trade_calls_release_before_the_ack(host: FakeHost, enrolled: str, trader: AgentThread) -> None:
    host.set_trade_settings(trade_tick_s=TICK)
    a1, a2 = host.add_assignment(), host.add_assignment()
    host.set_desired_role(enrolled, "trade")
    host.wait_for(lambda: len(host.orders(status="open")) == 2, timeout=8.0)
    jobs = _held_trade_jobs(host, enrolled)
    assert len(jobs) == 2
    before = len(host.heartbeats)
    host.set_desired_role(enrolled, "idle")
    epoch = host.worker(enrolled)["role_epoch"]
    ack = host.wait_for(_ack_heartbeat(host, enrolled, "idle", epoch, before), timeout=8.0)
    releases = host.trade_calls("/trade/release")
    assert len(releases) == 1
    assert sorted(j["id"] for j in releases[0]["body"]["jobs"]) == jobs
    assert releases[0]["t"] < ack["t"], "the release handshake runs before the ack heartbeat"
    assert releases[0]["response"] == {"cancelled": 2, "pending": 0, "released": pytest.approx(releases[0]["response"]["released"])}
    assert sorted(releases[0]["response"]["released"]) == jobs
    assert ack["request"]["jobs"] == [] and ack["request"]["released"] == [], "the ack carries no trade jobs"
    assert trader.agent.trade_jobs == {} and trader.agent.role == "idle"
    for job_id in jobs:
        job = host.job(job_id)
        assert job["status"] == "queued" and job["lease_worker_id"] is None
        assert host.releases(job_id) == [{"checkpoint": None, "reason": "drain"}]
    assert host.orders(status="open") == [] and len(host.orders(status="cancelled")) == 2
    for aid in (a1, a2):
        assert host.assignment(aid)["bankroll"] == {"available_cents": 10000, "reserved_cents": 0, "open_cost_cents": 0, "realized_pnl_cents": 0}
    assert all(hb["request"]["want_jobs"] == 0 for hb in host.heartbeats[len(host.heartbeats) - 1:])


def test_role_change_release_failure_falls_back_to_the_heartbeat_release(host: FakeHost, enrolled: str, trader: AgentThread) -> None:
    host.set_trade_settings(trade_tick_s=TICK)
    host.add_assignment()
    host.set_desired_role(enrolled, "trade")
    host.wait_for(lambda: host.orders(status="open"), timeout=8.0)
    job_id = _held_trade_jobs(host, enrolled)[0]
    host.fail_next("trade/release", 503, count=2)
    before = len(host.heartbeats)
    host.set_desired_role(enrolled, "idle")
    epoch = host.worker(enrolled)["role_epoch"]
    ack = host.wait_for(_ack_heartbeat(host, enrolled, "idle", epoch, before), timeout=8.0)
    assert [s for m, p, s in host.requests if p.endswith("/trade/release")] == [503, 503], "one retry, then proceed"
    assert [(r["id"], r["reason"]) for r in ack["request"]["released"]] == [(job_id, "drain")]
    assert ack["request"]["jobs"] == []
    assert host.job(job_id)["status"] == "queued" and trader.agent.trade_jobs == {}


def test_kickoff_in_the_past_means_no_proposals(host: FakeHost, enrolled: str, trader: AgentThread) -> None:
    host.set_trade_settings(trade_tick_s=TICK)
    aid = host.add_assignment()
    host.set_kickoff_past(aid)
    host.set_desired_role(enrolled, "trade")
    host.wait_for(lambda: _held_trade_jobs(host, enrolled), timeout=8.0)
    host.wait_for(lambda: trader.agent.trade.ticks >= 3, timeout=8.0)
    assert host.trade_calls("/orders/request") == []
    assert host.orders(aid) == []
    assert trader.agent.trade.last_tick["assignments"] == 1 and trader.agent.trade.last_tick["proposed"] == 0
    # Not a pregame rule when the setting is off.
    host.set_trade_settings(trade_pregame_only=False)
    host.wait_for(lambda: host.orders(aid, status="open"), timeout=8.0)


def test_preempted_trade_job_is_released_through_the_heartbeat(host: FakeHost, enrolled: str, trader: AgentThread) -> None:
    host.set_trade_settings(trade_tick_s=TICK, trade_max_games=1)
    aid = host.add_assignment()
    host.set_desired_role(enrolled, "trade")
    job_id = host.wait_for(lambda: (_held_trade_jobs(host, enrolled) or [None])[0], timeout=8.0)
    token = host.job(job_id)["lease_token"]
    host.request_preempt(job_id)
    host.wait_for(lambda: host.releases(job_id), timeout=8.0)
    assert host.releases(job_id)[0]["reason"] == "preempt"
    hb = next(h for h in host.heartbeats if any(r["id"] == job_id for r in h["request"]["released"]))
    assert [(r["id"], r["lease_token"], r["reason"]) for r in hb["request"]["released"]] == [(job_id, token, "preempt")]
    assert not any(j["id"] == job_id for j in hb["request"]["jobs"])
    # The job went back to the queue; the same worker (with a free slot again) claims it anew.
    host.wait_for(lambda: host.job(job_id)["status"] == "leased" and host.job(job_id)["lease_token"] != token, timeout=8.0)
    assert host.job_events(job_id) == ["claimed", "released", "claimed"]
    host.cancel_job(job_id)
    host.wait_for(lambda: host.job(job_id)["status"] == "cancelled", timeout=8.0)
    assert host.releases(job_id)[-1]["reason"] == "cancel"
    assert trader.agent.trade_jobs == {} and trader.agent.running == {}
    assert host.assignment(aid)["status"] == "active"


def test_lost_trade_job_is_dropped_and_reclaimed(host: FakeHost, enrolled: str, trader: AgentThread) -> None:
    host.set_trade_settings(trade_tick_s=TICK, trade_max_games=1)
    host.add_assignment()
    host.set_desired_role(enrolled, "trade")
    job_id = host.wait_for(lambda: (_held_trade_jobs(host, enrolled) or [None])[0], timeout=8.0)
    token = host.job(job_id)["lease_token"]
    host.expire_lease(job_id)
    host.wait_for(lambda: any(job_id in hb["response"]["lost"] for hb in host.heartbeats), timeout=8.0)
    host.wait_for(lambda: host.job(job_id)["status"] == "leased" and host.job(job_id)["lease_token"] != token, timeout=8.0)
    # The host grants the new lease in a heartbeat response the agent applies just after;
    # wait for the agent to have taken it rather than racing the response handling.
    host.wait_for(lambda: job_id in dict(trader.agent.trade_jobs), timeout=8.0)
    assert list(trader.agent.trade_jobs) == [job_id] and trader.agent.trade_jobs[job_id]["lease_token"] == host.job(job_id)["lease_token"]


def test_shutdown_releases_trade_jobs_through_the_handshake(host: FakeHost, enrolled: str, trader: AgentThread) -> None:
    host.set_trade_settings(trade_tick_s=TICK)
    aid = host.add_assignment()
    host.set_desired_role(enrolled, "trade")
    host.wait_for(lambda: host.orders(aid, status="open"), timeout=8.0)
    job_id = _held_trade_jobs(host, enrolled)[0]
    trader.stop()
    release = host.trade_calls("/trade/release")[-1]
    assert release["response"]["released"] == [job_id]
    assert host.job(job_id)["status"] == "queued" and host.orders(aid, status="open") == []
    assert host.releases(job_id) == [{"checkpoint": None, "reason": "drain"}]
    assert not any(hb["t"] > release["t"] and any(j["id"] == job_id for j in hb["request"]["jobs"]) for hb in host.heartbeats)
    assert trader.agent.trade_jobs == {}


def test_want_jobs_is_zero_outside_the_trade_role(state_dir: str) -> None:
    agent = Agent(state_dir=state_dir, options=AgentOptions(heartbeat_seconds=HB))
    assert agent.want_jobs() == 0
    agent.role = "trade"
    assert agent.want_jobs() == 6, "trade_max_games defaults to 6 before any state payload"
    agent.trade.settings["trade_max_games"] = 3
    agent.trade_jobs = {"a": {"id": "a", "lease_token": "t", "params": {}}}
    assert agent.want_jobs() == 2
    agent.stopping = True
    assert agent.want_jobs() == 0
    agent.stopping = False
    agent.options.trade_max_games = 1
    assert agent.want_jobs() == 0
    hb = agent.build_heartbeat()
    assert hb["want_jobs"] == 0 and hb["want_job"] is False
    assert hb["jobs"] == [{"id": "a", "lease_token": "t", "progress": 0.0}]
