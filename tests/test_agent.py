"""Agent tests against tests/fake_host.py with a 0.2 s heartbeat and a tmp state dir."""

from __future__ import annotations

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

from fleet.worker import config, launch, update
from fleet.worker.__main__ import main as cli_main
from fleet.worker.agent import EXIT_CONF_MISSING, EXIT_UPDATED, Agent, AgentOptions, RunningJob
from tests.fake_host import FakeHost, build_worker_tarball

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(REPO, "deploy", "install_worker.sh")

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
    assert running.agent.pending_posts == []


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
    proc = subprocess.run([sys.executable, "-m", "fleet.worker", "run"], env=env, capture_output=True, timeout=30)
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
    assert running.agent.pending_posts == []
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
    assert running.agent.pending_posts == []


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


def test_successful_register_clears_pending_version(host: FakeHost, state_dir: str, enrolled: str, running: AgentThread) -> None:
    app = config.app_dir(state_dir)
    launch.write_pending(app, "whatever", 2)
    host.wait_for(lambda: running.agent.heartbeat_count >= 1)
    assert launch.read_pending(app) is None


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
