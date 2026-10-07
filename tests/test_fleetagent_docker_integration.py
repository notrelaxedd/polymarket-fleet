"""Real Docker, one container: gated by FLEET_AGENT_DOCKER_TESTS=1.

Runs the supervisor against the fake host and the local python:3.13-slim image (no pull,
no network). The test image is that base plus a small script (committed as a tag under
agent-builder.local/fleet/), the container is named fleet-agent-builder-itest-<epoch> and
labelled fleet.workload=agent-builder-itest; the supervisor is scoped to exactly those
labels and repositories and never runs the daemon-wide prunes, so other users of the same
Docker daemon are untouched. Everything created is removed at the end.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest

from fleetagent import config
from fleetagent.supervisor import Options, Supervisor
from tests.fake_machine_host import FakeMachineHost, make_run

pytestmark = pytest.mark.skipif(
    os.environ.get("FLEET_AGENT_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="set FLEET_AGENT_DOCKER_TESTS=1 (needs a Docker daemon with python:3.13-slim) to run",
)

WORKLOAD = "agent-builder-itest"
REPO = "agent-builder.local/fleet"
BASE = "python:3.13-slim"
SCRIPT = r'''
import os, signal, sys, time
def term(signum, frame):
    print("itest: got SIGTERM, leaving", flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, term)
secret = open("/run/fleet/secrets/HELLO_GREETING").read()
print("itest: hi", flush=True)
print("itest: greeting=" + secret, flush=True)
print("itest: token=" + os.environ["FLEET_RUN_TOKEN"], flush=True)
print("itest: workload=" + os.environ["FLEET_WORKLOAD"], flush=True)
print("itest: uid=%d" % os.getuid(), flush=True)
open("/scratch/hello.txt", "w").write("scratch works")
try:
    open("/should-not-write", "w").write("x")
    print("itest: root fs is writable", flush=True)
except OSError:
    print("itest: root fs is read-only", flush=True)
print("itest: error line", file=sys.stderr, flush=True)
while True:
    time.sleep(0.5)
'''


def docker(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if check and proc.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


def _make_image(tag: str, script: str, tmp_path, marker: str) -> str:
    """BASE plus /opt/itest.py as CMD, committed as `tag`; returns the image id."""
    name = "agent-builder-mk-" + marker
    path = tmp_path / f"{marker}.py"
    path.write_text(script)
    docker("create", "--name", name, BASE)
    try:
        docker("cp", str(path), f"{name}:/opt/itest.py")
        docker("commit", "--change", 'CMD ["python3", "-u", "/opt/itest.py"]', "--change", f"LABEL agent-builder={marker}", name, tag)
    finally:
        docker("rm", "-f", name, check=False)
    return docker("image", "inspect", "--format", "{{.Id}}", tag)


def _wait(predicate, timeout: float = 30.0, step: float = 0.5, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(step)


def _containers() -> list[str]:
    return docker("ps", "-aq", "--filter", f"label=fleet.workload={WORKLOAD}").split()


def _cleanup_all() -> None:
    for cid in _containers():
        docker("rm", "-f", cid, check=False)
    for tag in docker("images", "--format", "{{.Repository}}:{{.Tag}}", REPO + "/*").split():
        docker("rmi", tag, check=False)
    for name in docker("ps", "-aq", "--filter", "name=agent-builder-mk-").split():
        docker("rm", "-f", name, check=False)


@pytest.fixture
def stage(tmp_path, monkeypatch):
    """Fake host, scoped supervisor options and two images (the one to run and an old one)."""
    _cleanup_all()
    for sub in ("state", "run", "data"):
        (tmp_path / sub).mkdir()
    monkeypatch.setenv("FLEET_AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FLEET_AGENT_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("FLEET_AGENT_DATA_DIR", str(tmp_path / "data"))
    host = FakeMachineHost(heartbeat_seconds=1.0).start()
    config.save_conf(str(tmp_path / "state"), {"host_url": host.url, "machine_id": "m_it", "machine_token": "t0", "name": "itest"})
    host.machines["m_it"] = {"id": "m_it", "name": "itest", "token": "t0", "prev": None, "specs": None}
    image_id = _make_image(f"{REPO}/itest:1", SCRIPT, tmp_path, "current")
    old_id = _make_image(f"{REPO}/itest-old:1", "print('old')\n", tmp_path, "old")
    assert image_id != old_id
    options = Options(
        heartbeat_seconds=1.0, background_pull=False, self_update=False, prune_images=False, prune_builder=False,
        labels=(f"fleet.workload={WORKLOAD}",), repo_filter=lambda repo: repo.startswith(REPO + "/"), stats_interval=0.0,
    )
    try:
        yield {"host": host, "tmp": tmp_path, "image_id": image_id, "old_id": old_id, "options": options}
    finally:
        host.stop()
        _cleanup_all()


def _supervisor(stage) -> Supervisor:
    sup = Supervisor(state_dir=str(stage["tmp"] / "state"), options=stage["options"])
    assert sup.boot() and sup.register_once()
    return sup


def _tick_until(sup: Supervisor, predicate, timeout: float = 30.0, what: str = "condition"):
    def step():
        sup.tick()
        return predicate()

    return _wait(step, timeout, 0.5, what)


def test_real_container_lifecycle(stage) -> None:
    host: FakeMachineHost = stage["host"]
    tmp = stage["tmp"]
    run = make_run(f"{REPO}/itest:1", env={"FLEET_HOST_URL": host.url, "FLEET_WORKLOAD": WORKLOAD, "FLEET_MACHINE_ID": "m_it", "FLEET_EPOCH": "1"})
    host.assign(WORKLOAD, run, epoch=1, keep=[stage["image_id"]])
    host.set_secrets({"HELLO_GREETING": "itest-s3cret-value"})
    sup = _supervisor(stage)

    # --- start: exact flags on a real container
    sup.tick()
    (cid,) = _containers()
    info = json.loads(docker("inspect", cid))[0]
    full_id = info["Id"]
    host_cfg, cfg = info["HostConfig"], info["Config"]
    assert info["Name"] == "/fleet-agent-builder-itest-1"
    assert cfg["Labels"]["fleet.workload"] == WORKLOAD and cfg["Labels"]["fleet.epoch"] == "1"
    assert host_cfg["ReadonlyRootfs"] is True and cfg["User"] == "10001:10001"
    assert "no-new-privileges" in " ".join(host_cfg["SecurityOpt"] or []) and host_cfg["NetworkMode"] == "bridge"
    assert host_cfg["Memory"] == 256 * 1024 * 1024 and host_cfg["LogConfig"]["Type"] == "local"
    assert host_cfg["LogConfig"]["Config"] == {"max-file": "3", "max-size": "10m"}
    assert cfg["StopTimeout"] == 15 and "/tmp" in host_cfg["Tmpfs"]
    token = host.run_tokens[-1]
    assert f"FLEET_RUN_TOKEN={token}" in cfg["Env"]
    mounts = {m["Destination"]: m for m in info["Mounts"]}
    assert mounts["/run/fleet/secrets"]["RW"] is False and mounts["/scratch"]["RW"] is True

    # --- logs ship, redacted, both streams
    _tick_until(sup, lambda: any("itest: error line" in l["line"] for l in host.shipped_logs()), what="logs to ship")
    lines = [l["line"] for l in host.shipped_logs()]
    assert "itest: hi" in lines and "itest: greeting=[redacted]" in lines and "itest: token=[redacted]" in lines
    assert f"itest: workload={WORKLOAD}" in lines and "itest: uid=10001" in lines and "itest: root fs is read-only" in lines
    assert all("itest-s3cret-value" not in l and token not in l for l in lines)
    assert {l["stream"] for l in host.shipped_logs()} >= {"stdout", "stderr", "agent"}
    assert (tmp / "data" / WORKLOAD / "scratch" / "hello.txt").read_text() == "scratch works"
    secret_file = tmp / "run" / "secrets" / WORKLOAD / "HELLO_GREETING"
    assert secret_file.read_text() == "itest-s3cret-value" and oct(secret_file.stat().st_mode & 0o777) == "0o400"
    assert secret_file.stat().st_uid == 10001

    # --- ack and status
    _tick_until(sup, lambda: host.acked_epoch == 1 and (host.last_container or {}).get("mem_mb") is not None, what="ack and stats")
    block = host.last_container
    assert block["state"] == "running" and block["container_id"] == full_id and block["restarts"] == 0

    # --- cleanup after the start removed the old image, kept the running one
    images = docker("images", "--format", "{{.Repository}}:{{.Tag}}", REPO + "/*").split()
    assert f"{REPO}/itest:1" in images and f"{REPO}/itest-old:1" not in images

    # --- a restarted agent adopts the running container
    adopter = _supervisor(stage)
    assert adopter.reconciler.managed is not None and adopter.reconciler.managed.container_id == full_id
    adopter.tick()
    adopter.tick()
    assert _containers() == [cid] and len(host.starts) == 1

    # --- the crash rule: killed (exit 137) is restarted about 3 s later with a fresh run token
    docker("kill", cid)
    t0 = time.monotonic()
    _tick_until(adopter, lambda: len(_containers()) == 1 and _containers() != [cid], timeout=30.0, what="the restart")
    assert time.monotonic() - t0 >= 3.0
    assert adopter.reconciler.managed.restarts == 1 and len(host.starts) == 2 and host.run_tokens[0] != host.run_tokens[1]

    # --- stop path: unassign stops the container, removes it and wipes secrets and scratch
    (second,) = _containers()
    host.unassign()
    _tick_until(adopter, lambda: _containers() == [], what="the stop")
    assert not (tmp / "run" / "secrets" / WORKLOAD).exists()
    assert list((tmp / "data" / WORKLOAD / "scratch").iterdir()) == []
    _tick_until(adopter, lambda: any("got SIGTERM" in l["line"] for l in host.shipped_logs()), what="the last log lines")
    _tick_until(adopter, lambda: host.acked_epoch == 2 and host.last_container is None, what="the ack of the empty epoch")
    assert docker("ps", "-aq", "--filter", f"id={second}") == ""
