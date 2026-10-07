"""The two ways the parity run starts the same worker agent.

NativeWorker mirrors deploy/install_worker.sh plus deploy/fleet-worker.service as far
as a sandbox without systemd allows: main's fleet/ tarball installed under
<state>/app/<version> with app/current, `python3 -m fleet.worker enroll` with the token
in the environment, then `/usr/bin/python3 -m fleet.worker run` as uid 10001 with Nice=5
and only the unit's environment (PYTHONPATH, FLEET_STATE_DIR, PATH).

ContainerWorker runs the polymarket image with the docker flags the supervisor uses
(the exact flags fleetagent.runargs.build_run_args gives the supervisor, from the real manifest): --network host --uts host, --init, --read-only, --tmpfs
/tmp, no-new-privileges, --user 10001:10001, the memory cap from memory_max_pct, the
/state and /scratch bind mounts and the read-only secrets directory.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any, Protocol

from fleet.worker import update as worker_update
from host.bundle import build_bundle

UID = 10001
UNIT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
NICE = 5
STOP_TIMEOUT = 15
MEMORY_MAX_PCT = 85


class Worker(Protocol):
    label: str
    state: Path

    def prepare(self, host_url: str, host_code_version: str, token: str) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> int | None: ...
    def alive(self) -> bool: ...
    def describe(self) -> dict[str, Any]: ...


def chown_tree(path: Path, uid: int = UID) -> None:
    for root, dirs, files in os.walk(path):
        os.chown(root, uid, uid)
        for name in dirs + files:
            os.lchown(os.path.join(root, name), uid, uid)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def ram_total_mb() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    raise RuntimeError("no MemTotal in /proc/meminfo")


class NativeWorker:
    """The systemd fleet-worker, minus systemd (see the module docstring)."""

    label = "native"

    def __init__(self, root: Path, main_fleet: Path, python: str = "/usr/bin/python3") -> None:
        self.root, self.main_fleet, self.python = root, main_fleet, python
        self.state = root / "state"
        self.log_path = root / "worker.log"
        self.proc: subprocess.Popen[bytes] | None = None
        self.version = ""

    def env(self) -> dict[str, str]:
        return {"PATH": UNIT_PATH, "PYTHONPATH": str(self.state / "app" / "current"), "FLEET_STATE_DIR": str(self.state)}

    def prepare(self, host_url: str, host_code_version: str, token: str) -> None:
        bundle = build_bundle(self.main_fleet)
        if bundle.code_version != host_code_version:
            raise RuntimeError(f"main's fleet/ is {bundle.code_version} but the host serves {host_code_version}: "
                               "the native agent would self-update; the run would not compare main's code")
        app = self.state / "app"
        app.mkdir(parents=True)
        worker_update.install_version(str(app), bundle.code_version, bundle.data)
        worker_update.swap_current(str(app), bundle.code_version)
        self.version = bundle.code_version
        chown_tree(self.state)
        self.state.chmod(0o750)
        enroll = subprocess.run([self.python, "-m", "fleet.worker", "enroll", f"--host={host_url}"],
                                env=dict(self.env(), FLEET_ENROLL_TOKEN=token), user=UID, group=UID, extra_groups=[],
                                capture_output=True, text=True, check=False)
        if enroll.returncode != 0:
            raise RuntimeError(f"native enroll failed ({enroll.returncode}): {enroll.stderr.strip()}")

    def start(self) -> None:
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen([self.python, "-m", "fleet.worker", "run"], env=self.env(), cwd="/",
                                     user=UID, group=UID, extra_groups=[], stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, preexec_fn=lambda: os.nice(NICE))
        log.close()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> int | None:
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        return self.proc.returncode

    def describe(self) -> dict[str, Any]:
        probe = subprocess.run([self.python, "-c", "import platform, sys; print(platform.python_version(), sys.executable)"],
                               capture_output=True, text=True, check=False)
        return {"mode": self.label, "python": probe.stdout.strip(), "code_version": self.version, "log": str(self.log_path)}


class ContainerWorker:
    """The polymarket workload container, started the way fleetagent will start it."""

    label = "container"

    def __init__(self, root: Path, image: str, name: str, docker: str = "docker") -> None:
        self.root, self.image, self.name, self.docker = root, image, name, docker
        self.state = root / "state"
        self.scratch = root / "scratch"
        self.secrets = root / "secrets"
        self.log_path = root / "worker.log"
        self.host_url = ""
        self.exit_code: int | None = None

    def prepare(self, host_url: str, host_code_version: str, token: str) -> None:
        self.host_url = host_url
        for path in (self.state, self.scratch, self.secrets):
            path.mkdir(parents=True)
            os.chown(path, UID, UID)
        self.state.chmod(0o750)
        secret = self.secrets / "FLEET_ENROLL_TOKEN"
        secret.write_text(token, encoding="utf-8")
        os.chown(secret, UID, UID)
        secret.chmod(0o400)

    def run_block(self) -> dict[str, Any]:
        """The heartbeat "run" block the host computes for this machine (host.workloads.assign
        desired_run), built from the real polymarket manifest."""
        from host.workloads.manifest import load_manifest

        m = load_manifest(Path(__file__).resolve().parents[3] / "workloads" / "polymarket")
        res, rt = m.resources, m.runtime
        return {
            "image": self.image, "network": rt.network, "uts_host": rt.uts_host, "uid": rt.uid,
            "memory_mb": int((res.memory_max_pct or MEMORY_MAX_PCT) * ram_total_mb() / 100), "cpus": res.cpus,
            "nice": rt.nice, "stop_timeout_s": rt.stop_timeout_s, "state_volume": rt.state_volume,
            "no_restart_exit_codes": list(rt.no_restart_exit_codes),
            "env": {"FLEET_HOST_URL": self.host_url, "FLEET_WORKLOAD": "polymarket", "FLEET_MACHINE_ID": "m_parity",
                    "FLEET_EPOCH": "1", "FLEET_NICE": str(rt.nice)},
        }

    def run_args(self) -> list[str]:
        """Exactly the supervisor's docker run flags (fleetagent.runargs.build_run_args)."""
        from fleetagent.runargs import build_run_args

        args = build_run_args("polymarket", 1, self.run_block(), scratch=str(self.scratch), state=str(self.state),
                              secrets=str(self.secrets))
        # The supervisor names its containers fleet-<workload>-<epoch>; keep the harness's own name.
        i = args.index("--name")
        args[i + 1] = self.name
        return [self.docker, "run", "-d", *args]

    def start(self) -> None: ...
    def stop(self) -> int | None: ...
    def alive(self) -> bool: ...
    def describe(self) -> dict[str, Any]: ...


def chown_tree(path: Path, uid: int = UID) -> None:
    for root, dirs, files in os.walk(path):
        os.chown(root, uid, uid)
        for name in dirs + files:
            os.lchown(os.path.join(root, name), uid, uid)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def ram_total_mb() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    raise RuntimeError("no MemTotal in /proc/meminfo")


class NativeWorker:
    """The systemd fleet-worker, minus systemd (see the module docstring)."""

    label = "native"

    def __init__(self, root: Path, main_fleet: Path, python: str = "/usr/bin/python3") -> None:
        self.root, self.main_fleet, self.python = root, main_fleet, python
        self.state = root / "state"
        self.log_path = root / "worker.log"
        self.proc: subprocess.Popen[bytes] | None = None
        self.version = ""

    def env(self) -> dict[str, str]:
        return {"PATH": UNIT_PATH, "PYTHONPATH": str(self.state / "app" / "current"), "FLEET_STATE_DIR": str(self.state)}

    def prepare(self, host_url: str, host_code_version: str, token: str) -> None:
        bundle = build_bundle(self.main_fleet)
        if bundle.code_version != host_code_version:
            raise RuntimeError(f"main's fleet/ is {bundle.code_version} but the host serves {host_code_version}: "
                               "the native agent would self-update; the run would not compare main's code")
        app = self.state / "app"
        app.mkdir(parents=True)
        worker_update.install_version(str(app), bundle.code_version, bundle.data)
        worker_update.swap_current(str(app), bundle.code_version)
        self.version = bundle.code_version
        chown_tree(self.state)
        self.state.chmod(0o750)
        enroll = subprocess.run([self.python, "-m", "fleet.worker", "enroll", f"--host={host_url}"],
                                env=dict(self.env(), FLEET_ENROLL_TOKEN=token), user=UID, group=UID, extra_groups=[],
                                capture_output=True, text=True, check=False)
        if enroll.returncode != 0:
            raise RuntimeError(f"native enroll failed ({enroll.returncode}): {enroll.stderr.strip()}")

    def start(self) -> None:
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen([self.python, "-m", "fleet.worker", "run"], env=self.env(), cwd="/",
                                     user=UID, group=UID, extra_groups=[], stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, preexec_fn=lambda: os.nice(NICE))
        log.close()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> int | None:
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        return self.proc.returncode

    def describe(self) -> dict[str, Any]:
        probe = subprocess.run([self.python, "-c", "import platform, sys; print(platform.python_version(), sys.executable)"],
                               capture_output=True, text=True, check=False)
        return {"mode": self.label, "python": probe.stdout.strip(), "code_version": self.version, "log": str(self.log_path)}


class ContainerWorker:
    """The polymarket workload container, started the way fleetagent will start it."""

    label = "container"

    def __init__(self, root: Path, image: str, name: str, docker: str = "docker") -> None:
        self.root, self.image, self.name, self.docker = root, image, name, docker
        self.state = root / "state"
        self.scratch = root / "scratch"
        self.secrets = root / "secrets"
        self.log_path = root / "worker.log"
        self.host_url = ""
        self.exit_code: int | None = None

    def prepare(self, host_url: str, host_code_version: str, token: str) -> None:
        self.host_url = host_url
        for path in (self.state, self.scratch, self.secrets):
            path.mkdir(parents=True)
            os.chown(path, UID, UID)
        self.state.chmod(0o750)
        secret = self.secrets / "FLEET_ENROLL_TOKEN"
        secret.write_text(token, encoding="utf-8")
        os.chown(secret, UID, UID)
        secret.chmod(0o400)

    def run_args(self) -> list[str]:
        memory_mb = MEMORY_MAX_PCT * ram_total_mb() // 100
        return [
            self.docker, "run", "-d", "--name", self.name,
            "--label", "fleet.workload=polymarket", "--label", "fleet.epoch=1",
            "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=256m", "--security-opt", "no-new-privileges",
            "--user", f"{UID}:{UID}", "--memory", f"{memory_mb}m", "--memory-swap", "-1",
            "--stop-timeout", str(STOP_TIMEOUT), "--network", "host", "--uts", "host",
            "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
            "-v", f"{self.scratch}:/scratch", "-v", f"{self.state}:/state", "-v", f"{self.secrets}:/run/fleet/secrets:ro",
            "-e", f"FLEET_HOST_URL={self.host_url}", "-e", "FLEET_WORKLOAD=polymarket", "-e", "FLEET_MACHINE_ID=m_parity",
            "-e", "FLEET_EPOCH=1", "-e", "FLEET_RUN_TOKEN=parity-run-token",
            self.image,
        ]

    def start(self) -> None:
        subprocess.run([self.docker, "rm", "-f", self.name], capture_output=True, check=False)
        env = {**os.environ, "FLEET_RUN_TOKEN": "parity-run-token"}  # the token stays off argv, as in the agent
        subprocess.run(self.run_args(), capture_output=True, text=True, check=True, env=env)

    def _inspect(self, fmt: str) -> str:
        out = subprocess.run([self.docker, "inspect", "-f", fmt, self.name], capture_output=True, text=True, check=False)
        return out.stdout.strip() if out.returncode == 0 else ""

    def alive(self) -> bool:
        return self._inspect("{{.State.Running}}") == "true"

    def stop(self) -> int | None:
        if not self._inspect("{{.Id}}"):
            return None
        subprocess.run([self.docker, "stop", "-t", str(STOP_TIMEOUT), self.name], capture_output=True, check=False)
        code = self._inspect("{{.State.ExitCode}}")
        self.exit_code = int(code) if code.lstrip("-").isdigit() else None
        logs = subprocess.run([self.docker, "logs", self.name], capture_output=True, check=False)
        self.log_path.write_bytes(logs.stdout + logs.stderr)
        subprocess.run([self.docker, "rm", "-f", self.name], capture_output=True, check=False)
        return self.exit_code

    def describe(self) -> dict[str, Any]:
        probe = subprocess.run([self.docker, "run", "--rm", "--network", "none", "--entrypoint", "python3", self.image, "-c",
                                "import platform, sys; print(platform.python_version(), sys.executable)"],
                               capture_output=True, text=True, check=False)
        current = self.state / "app" / "current"
        version = os.readlink(current) if current.is_symlink() else ""
        return {"mode": self.label, "python": probe.stdout.strip(), "code_version": version, "image": self.image,
                "log": str(self.log_path)}


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
