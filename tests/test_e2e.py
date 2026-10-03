"""End to end: real host (uvicorn + Postgres + loop thread) driving the real worker agent.

One worker enrols against a live host, runs sleep jobs through the real subprocess
runner, is preempted by a role change, resumes from its checkpoint, auto-returns to
idle, gets a job cancelled, is handed the same lease again after a dropped heartbeat
response (re-offer), and finally survives a simulated crash with a lost register reply
(the retry with the previous token succeeds, held_jobs are re-adopted). Heartbeat 0.3 s,
host loop 0.5 s, every wait bounded.
"""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import httpx
import pytest
import uvicorn

from fleet.common import http as worker_http
from fleet.worker import config as worker_config
from fleet.worker.__main__ import enroll
from fleet.worker.agent import Agent, AgentOptions
from host import db
from host.api.app import create_app
from host.config import Config
from host.loop import LoopThread
from tests.conftest import heartbeat_body

HEARTBEAT = 0.3
LOOP = 0.5
POLL = 0.05
TIMEOUT = 10.0
ROLE_CHANGE_BOUND = HEARTBEAT + 1.0 + 1.5


def wait_for(predicate: Callable[[], Any], what: str, timeout: float = TIMEOUT) -> Any:
    """Poll every 50 ms until predicate() is truthy; fail with the last value on timeout."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(POLL)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}; last={last!r}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class LiveHost:
    """A running host and a client that talks to it over real TCP."""

    url: str
    code_version: str
    client: httpx.Client

    def get(self, path: str) -> Any:
        resp = self.client.get(path)
        assert resp.status_code == 200, f"GET {path}: {resp.status_code} {resp.text}"
        return resp.json()

    def post(self, path: str, body: dict[str, Any] | None = None, expect: int = 200) -> Any:
        resp = self.client.post(path, json=body)
        assert resp.status_code == expect, f"POST {path}: {resp.status_code} {resp.text}"
        return resp.json()

    def job(self, job_id: str) -> dict[str, Any]:
        return self.get(f"/api/jobs/{job_id}")

    def worker(self, worker_id: str) -> dict[str, Any]:
        return next(w for w in self.get("/api/fleet")["workers"] if w["id"] == worker_id)

    def send_job(self, seconds: int, target: str) -> dict[str, Any]:
        return self.post("/api/jobs", {"kind": "sleep", "params": {"seconds": seconds}, "target": target}, expect=201)

    def set_role(self, worker_id: str, role: str) -> dict[str, Any]:
        return self.post(f"/api/workers/{worker_id}/role", {"role": role})

    def events(self, job_id: str) -> list[str]:
        return [e["event"] for e in self.job(job_id)["events"]]


@pytest.fixture
def live_host(test_db_url: str, tmp_path) -> Iterator[LiveHost]:
    """uvicorn in a background thread on a free port plus the host loop at 0.5 s."""
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    cfg = Config.from_env(
        {"FLEET_DEV": "1", "FLEET_PUBLIC_URL": url, "DATABASE_URL": test_db_url,
         "FLEET_DEPLOY_DIR": str(tmp_path / "deploy")}
    )
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server_thread = threading.Thread(target=server.run, name="e2e-uvicorn", daemon=True)
    server_thread.start()
    client = httpx.Client(base_url=url, trust_env=False, timeout=5.0)
    loop_pool = db.make_pool(test_db_url, min_size=1, max_size=2)
    loop = LoopThread(loop_pool, LOOP)
    try:
        wait_for(lambda: server.started and _healthy(client), "host to come up")
        loop.start()
        yield LiveHost(url=url, code_version=app.state.bundle.code_version, client=client)
    finally:
        loop.stop()
        loop.join(5.0)
        loop_pool.close()
        server.should_exit = True
        server_thread.join(10.0)
        client.close()


def _healthy(client: httpx.Client) -> bool:
    try:
        return client.get("/healthz").status_code == 200
    except httpx.HTTPError:
        return False


class CrashingAgent(Agent):
    """An agent whose stop looks like kill -9: no drain and no final heartbeat."""

    def shutdown(self) -> None:
        return None


@dataclass
class AgentThread:
    """Runs one Agent.run_forever() in a thread."""

    agent: Agent
    thread: threading.Thread = field(init=False)
    result: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.thread = threading.Thread(target=lambda: self.result.append(self.agent.run_forever()), daemon=True)

    def start(self) -> "AgentThread":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.agent.stop.set()
        self.thread.join(15.0)


@pytest.fixture
def agents() -> Iterator[list[AgentThread]]:
    """Every agent started by the test is stopped afterwards, pass or fail."""
    started: list[AgentThread] = []
    yield started
    for runner in reversed(started):
        runner.stop()
        for rj in list(runner.agent.running.values()):
            rj.runner.kill()


@dataclass
class DropBox:
    """Drops the next heartbeat response that carries a claim, once armed.

    The request reaches the host and is committed there; only the answer is lost on
    the way back, exactly like a timeout after the host sent its reply.
    """

    armed: bool = False
    dropped: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def drop_box(monkeypatch) -> DropBox:
    """Wrap the worker's HTTP helper so one heartbeat answer can be thrown away."""
    box = DropBox()
    real_post = worker_http.post_json

    def post_json(url: str, body: Any, **kw: Any) -> Any:
        resp = real_post(url, body, **kw)
        if box.armed and url.endswith("/heartbeat") and isinstance(resp, dict) and resp.get("claimed"):
            box.armed = False
            box.dropped.append(resp)
            raise worker_http.HttpConnectionError("heartbeat response dropped by the test")
        return resp

    monkeypatch.setattr(worker_http, "post_json", post_json)
    return box


def start_agent(agents: list[AgentThread], state_dir: str, code_version: str, cls: type[Agent] = Agent) -> AgentThread:
    options = AgentOptions(heartbeat_seconds=HEARTBEAT, http_timeout=2.0, code_version=code_version)
    runner = AgentThread(cls(state_dir=state_dir, options=options)).start()
    agents.append(runner)
    return runner


def settled(host: LiveHost, worker_id: str, role: str) -> Callable[[], bool]:
    """Predicate: the worker reports `role` and is not switching."""

    def check() -> bool:
        w = host.worker(worker_id)
        return w["reported_role"] == role and w["desired_role"] == role and not w["switching"]

    return check


def job_in(host: LiveHost, job_id: str, status: str, min_elapsed: int = 0) -> Callable[[], Any]:
    """Predicate: the job row once it has `status` and a checkpoint of at least min_elapsed."""

    def check() -> Any:
        job = host.job(job_id)
        if job["status"] == status and (job["checkpoint"] or {}).get("elapsed", 0) >= min_elapsed:
            return job
        return False

    return check


# ----------------------------------------------------------------- scenario phases


def phase_enroll(host: LiveHost, state_dir: str, agents: list[AgentThread]) -> tuple[str, AgentThread]:
    """Mint a token, enroll, start the (crashable) agent, see it online."""
    minted = host.post("/api/enroll-token")
    assert minted["install_command"].startswith(f"curl -fsSL {host.url}/install.sh")
    conf = enroll(host.url, minted["token"], "e2e-box", state_dir)
    worker_id = conf["worker_id"]
    assert host.worker(worker_id)["online"] is False, "no heartbeat yet"

    first = start_agent(agents, state_dir, host.code_version, cls=CrashingAgent)
    worker = wait_for(lambda: (w := host.worker(worker_id))["online"] and w, "worker online")
    assert worker["name"] == "e2e-box" and worker["reported_role"] == "idle"
    assert worker["cpu_pct"] is not None and worker["ram_used_mb"] > 0 and worker["ram_total_mb"] > 0
    assert worker["code_version"] == host.code_version and worker["switching"] is False
    assert worker_config.load_conf(state_dir)["worker_token"] != conf["worker_token"], "register rotated the token"
    return worker_id, first


def phase_preempt_and_resume(host: LiveHost, worker_id: str) -> None:
    """any_idle job -> auto role flip -> role train mid-job -> requeue -> resume from checkpoint.

    The worker is moved to another batch role rather than idle: an idle worker is a
    dispatch target, so with a single worker the dispatcher would hand the requeued
    (now untargeted) job straight back and the preempted state could not be observed.
    """
    job = host.send_job(6, "any_idle")
    job_id = job["id"]
    assert job["target_worker_id"] == worker_id and "waiting_for_idle_worker" not in job
    flipped = host.worker(worker_id)
    assert flipped["desired_role"] == "backtest" and flipped["auto_role"] is True

    wait_for(settled(host, worker_id, "backtest"), "agent to ack backtest")
    wait_for(lambda: (j := host.job(job_id))["status"] == "leased" and j["progress"] > 0, "progress above 0")
    assert host.worker(worker_id)["current_jobs"][0]["id"] == job_id
    wait_for(job_in(host, job_id, "leased", min_elapsed=2), "two checkpoints")

    t_role = time.monotonic()
    changed = host.set_role(worker_id, "train")
    assert changed["desired_role"] == "train" and changed["auto_role"] is False
    wait_for(settled(host, worker_id, "train"), "worker to report train")
    # Contract worst case at this cadence: one poll + one 1 s sleep unit (SIGTERM is
    # honoured between units) + two round trips, with headroom for a loaded CI box.
    role_wall = time.monotonic() - t_role
    assert role_wall < ROLE_CHANGE_BOUND, f"train reported after {role_wall:.2f} s (bound {ROLE_CHANGE_BOUND} s)"
    requeued = wait_for(lambda: (j := host.job(job_id))["status"] == "queued" and j, "job back in queue")
    released_elapsed = requeued["checkpoint"]["elapsed"]
    assert released_elapsed >= 1 and requeued["lease_worker_id"] is None and requeued["lease_token"] is None
    assert requeued["target_worker_id"] is None, "a system-chosen target is cleared on release"
    assert requeued["expiries"] == 0
    assert host.worker(worker_id)["desired_role"] == "train", "a worker in another role is not re-dispatched to"
    assert {"preempt_requested", "released"} <= set(host.events(job_id))

    t_resume = time.monotonic()
    host.set_role(worker_id, "backtest")
    wait_for(job_in(host, job_id, "leased"), "job re-leased")
    seen: list[int] = []
    while True:
        current = host.job(job_id)
        seen.append(current["checkpoint"]["elapsed"])
        if current["status"] == "succeeded":
            break
        assert current["status"] == "leased", current["status"]
        assert time.monotonic() - t_resume < TIMEOUT, "resume did not finish"
        time.sleep(POLL)
    resume_wall = time.monotonic() - t_resume
    assert min(seen) >= released_elapsed, f"checkpoint went backwards: {seen}"
    assert resume_wall < 6.0, f"took {resume_wall:.1f}s after resume: restarted from zero"
    assert current["result"] == {"slept": 6} and current["progress"] == 1 and current["finished_at"]
    assert host.events(job_id).count("claimed") == 2 and host.events(job_id)[-1] == "succeeded"


def phase_auto_return(host: LiveHost, worker_id: str) -> None:
    """A manual role set cleared auto_role; a fresh any_idle job must flip and flip back."""
    host.set_role(worker_id, "idle")
    wait_for(settled(host, worker_id, "idle"), "worker idle again")
    job = host.send_job(2, "any_idle")
    assert job["target_worker_id"] == worker_id and host.worker(worker_id)["auto_role"] is True
    done = wait_for(lambda: (j := host.job(job["id"]))["status"] == "succeeded" and j, "short job done")
    assert done["result"] == {"slept": 2}
    worker = wait_for(lambda: (w := host.worker(worker_id))["desired_role"] == "idle" and w, "auto-return to idle")
    assert worker["auto_role"] is False
    wait_for(settled(host, worker_id, "idle"), "agent to ack idle")


def phase_cancel(host: LiveHost, worker_id: str) -> None:
    """Cancel a running job: cancel_requested, released by the agent, cancelled by the host."""
    job = host.send_job(6, worker_id)
    assert job["target_worker_id"] == worker_id
    wait_for(lambda: (j := host.job(job["id"]))["status"] == "leased" and j["progress"] > 0, "cancel target running")
    assert host.post(f"/api/jobs/{job['id']}/cancel")["status"] == "cancel_requested"
    cancelled = wait_for(lambda: (j := host.job(job["id"]))["status"] == "cancelled" and j, "job cancelled")
    assert cancelled["finished_at"] and cancelled["lease_worker_id"] is None
    assert host.events(job["id"])[-2:] == ["cancel_requested", "released"]
    assert host.post(f"/api/jobs/{job['id']}/cancel")["status"] == "cancelled", "cancel is idempotent"
    wait_for(settled(host, worker_id, "idle"), "worker idle after cancel")
    assert host.worker(worker_id)["current_jobs"] == []


def phase_reoffer(host: LiveHost, worker_id: str, agent: AgentThread, drop_box: DropBox) -> None:
    """The heartbeat answer that claims a job is lost; the next heartbeat gets the same lease back."""
    drop_box.armed = True
    job = host.send_job(3, worker_id)
    job_id = job["id"]
    wait_for(lambda: drop_box.dropped, "claim reply dropped")
    assert str(drop_box.dropped[0]["claimed"][0]["id"]) == job_id
    offered_token = drop_box.dropped[0]["claimed"][0]["lease_token"]

    wait_for(lambda: job_id in agent.agent.running, "job re-offered and started")
    assert agent.agent.running[job_id].lease_token == offered_token, "re-offer keeps the lease token"
    events = host.events(job_id)
    assert events.count("claimed") == 1 and events.count("re-offered") == 1, events
    assert host.job(job_id)["lease_token"] == offered_token
    done = wait_for(lambda: (j := host.job(job_id))["status"] == "succeeded" and j, "re-offered job done")
    assert done["result"] == {"slept": 3}
    events = host.events(job_id)
    assert events.count("claimed") == 1 and events.count("re-offered") == 1 and events[-1] == "succeeded", events
    assert agent.agent.misses == 0 and agent.agent.degraded is False
    wait_for(settled(host, worker_id, "idle"), "worker idle after the re-offer")


def phase_crash(host: LiveHost, state_dir: str, worker_id: str, first: AgentThread, agents: list[AgentThread]) -> None:
    """Kill the agent mid-job, lose a register reply, then a new instance re-registers
    with the token it still has (the previous one) and adopts the lease."""
    job = host.send_job(6, worker_id)
    job_id = job["id"]
    leased = wait_for(job_in(host, job_id, "leased", min_elapsed=2), "job running before crash")
    old_lease_token = leased["lease_token"]
    old_token = worker_config.load_conf(state_dir)["worker_token"]

    first.stop()
    assert first.result == [0]
    for rj in list(first.agent.running.values()):
        rj.runner.kill()
        rj.runner.wait(5.0)
    assert host.job(job_id)["status"] == "leased", "lease outlives the crash"
    elapsed_at_crash = host.job(job_id)["checkpoint"]["elapsed"]

    # A register whose reply never reached the agent: the host rotated, worker.conf did not.
    lost = host.post("/api/v1/workers/register", {"worker_id": worker_id, "worker_token": old_token, "hostname": "e2e-box"})
    unseen_token = lost["worker_token"]
    assert unseen_token != old_token and [j["id"] for j in lost["held_jobs"]] == [job_id]
    assert worker_config.load_conf(state_dir)["worker_token"] == old_token

    t_restart = time.monotonic()
    second = start_agent(agents, state_dir, host.code_version)
    wait_for(lambda: second.agent.heartbeat_count >= 1, "new instance heartbeating")
    assert second.agent.last_error is None, "register with the previous token must succeed first time"
    assert job_id in second.agent.running, "held job adopted on register"
    new_token = worker_config.load_conf(state_dir)["worker_token"]
    assert new_token not in (old_token, unseen_token)

    for stale_token in (old_token, unseen_token):
        stale = {"Authorization": f"Bearer {stale_token}"}
        resp = host.client.post(f"/api/v1/workers/{worker_id}/heartbeat", json=heartbeat_body(), headers=stale)
        assert resp.status_code == 401, "only the current token heartbeats"
        resp = host.client.post("/api/v1/workers/register", json={"worker_id": worker_id, "worker_token": stale_token})
        assert resp.status_code == 401, "prev_token_hash is cleared by the first heartbeat"
    adopted = host.job(job_id)
    assert adopted["lease_token"] != old_lease_token and "re-leased" in host.events(job_id)

    done = wait_for(lambda: (j := host.job(job_id))["status"] == "succeeded" and j, "adopted job done")
    assert time.monotonic() - t_restart < 6.0, "adopted job restarted from zero"
    assert done["result"] == {"slept": 6} and done["progress"] == 1
    assert done["checkpoint"]["elapsed"] >= elapsed_at_crash, "resumed job must not rewind its checkpoint"
    wait_for(settled(host, worker_id, "idle"), "worker idle at the end")
    second.stop()
    assert second.result == [0]


def test_fleet_end_to_end(live_host: LiveHost, tmp_path, monkeypatch, agents: list[AgentThread], drop_box: DropBox) -> None:
    state_dir = str(tmp_path / "state")
    monkeypatch.setenv("FLEET_STATE_DIR", state_dir)
    started = time.monotonic()

    worker_id, first = phase_enroll(live_host, state_dir, agents)
    phase_preempt_and_resume(live_host, worker_id)
    phase_auto_return(live_host, worker_id)
    phase_cancel(live_host, worker_id)
    phase_reoffer(live_host, worker_id, first, drop_box)
    phase_crash(live_host, state_dir, worker_id, first, agents)

    assert time.monotonic() - started < 60.0
    statuses = {j["status"] for j in live_host.get("/api/jobs")}
    assert statuses == {"succeeded", "cancelled"}
