"""Shared harness for the supervisor tests: fake clock, fake systemctl and a ready-built Supervisor."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from fleetagent import config, specs
from fleetagent.supervisor import Options, Supervisor
from tests.fake_machine_host import FakeMachineHost
from tests.test_fleetagent_fakes import FakeDocker, FakeSys


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSystemctl:
    """Runner for `systemctl is-active|is-enabled fleet-worker`; state is active, inactive or absent."""

    def __init__(self, state: str = "absent") -> None:
        self.state = state
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(cmd[1:])
        verb = cmd[1]
        if verb == "is-active":
            if self.state == "active":
                return subprocess.CompletedProcess(cmd, 0, "active\n", "")
            return subprocess.CompletedProcess(cmd, 3, "inactive\n", "")
        if self.state == "absent":
            return subprocess.CompletedProcess(cmd, 1, "", "Failed to get unit file state for fleet-worker.service: No such file or directory\n")
        return subprocess.CompletedProcess(cmd, 0, "enabled\n", "")


@dataclass
class Rig:
    """One machine under test: supervisor, fake docker, fake host, fake clock, fake systemctl."""

    sup: Supervisor
    docker: FakeDocker
    host: FakeMachineHost
    clock: FakeClock
    systemctl: FakeSystemctl
    free_mb: list[int]
    state_dir: str
    run_dir: str
    data_dir: str
    make_sup: Callable[[], Supervisor]
    chowns: list[tuple[str, int, int]]

    def tick(self, seconds: float = 5.0) -> None:
        """Advance the clock and send one heartbeat (with its reconcile)."""
        self.clock.advance(seconds)
        self.sup.tick()

    def boot(self) -> "Rig":
        assert self.sup.boot()
        assert self.sup.register_once()
        return self

    def fleet_containers(self) -> list[dict[str, Any]]:
        return [c for c in self.docker.containers.values() if "fleet.workload" in c["labels"]]


def _fake_proc(root: str) -> None:
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "meminfo"), "w", encoding="utf-8") as fh:
        fh.write("MemTotal:        3891200 kB\nMemFree:          500000 kB\nMemAvailable:    2891200 kB\n")
    with open(os.path.join(root, "cpuinfo"), "w", encoding="utf-8") as fh:
        fh.write("processor\t: 0\nmodel name\t: x\n\nprocessor\t: 1\nmodel name\t: x\n")


def build_rig(tmp_path, monkeypatch, host: FakeMachineHost, *, native: str = "absent", enroll: bool = True, root: bool = False, **opts: Any) -> Rig:
    """A Supervisor wired to a FakeDocker, a fake sysfs (one 100 GB SATA SSD) and `host`."""
    state_dir, run_dir, data_dir = (str(tmp_path / n) for n in ("state", "run", "data"))
    for d in (state_dir, run_dir, data_dir):
        os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("FLEET_AGENT_STATE_DIR", state_dir)
    monkeypatch.setenv("FLEET_AGENT_RUN_DIR", run_dir)
    monkeypatch.setenv("FLEET_AGENT_DATA_DIR", data_dir)
    sysroot = tmp_path / "sys"
    fsys = FakeSys(str(sysroot))
    disk = fsys.disk("sda", "pci0000:00/ata1/host0/target0:0:0/0:0:0:0", 200_000_000, rotational=0)
    part = fsys.partition(disk, "sda1", 199_000_000)
    major, minor = fsys.link(8, 1, part)
    _fake_proc(str(tmp_path / "proc"))
    free_mb = [50_000]
    docker = FakeDocker()
    clock = FakeClock()
    systemctl = FakeSystemctl(native)
    chowns: list[tuple[str, int, int]] = []
    options = Options(heartbeat_seconds=5.0, background_pull=False, self_update=False, stats_interval=0.0, poll_interval=0.0)
    for key, value in opts.items():
        setattr(options, key, value)
    if enroll:
        config.save_conf(state_dir, {"host_url": host.url, "machine_id": "m_pre", "machine_token": "t0", "name": "box1"})
        host.machines["m_pre"] = {"id": "m_pre", "name": "box1", "token": "t0", "prev": None, "specs": None}

    def make_sup() -> Supervisor:
        collector = specs.SpecsCollector(
            docker.docker(), proc=str(tmp_path / "proc"), sys_root=str(sysroot),
            stat_dev=lambda path: os.makedev(major, minor),
            statvfs=lambda path: SimpleNamespace(f_bavail=free_mb[0], f_frsize=1024 * 1024),
        )
        return Supervisor(
            state_dir=state_dir, options=options, docker=docker.docker(), clock=clock, sleep=lambda s: None,
            systemctl_runner=systemctl, root=root, chown=lambda path, uid, gid: chowns.append((path, uid, gid)),
            collector=collector,
        )

    return Rig(make_sup(), docker, host, clock, systemctl, free_mb, state_dir, run_dir, data_dir, make_sup, chowns)


@pytest.fixture
def host():
    fake = FakeMachineHost(heartbeat_seconds=5.0).start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def rig(tmp_path, monkeypatch, host):
    return build_rig(tmp_path, monkeypatch, host).boot()
