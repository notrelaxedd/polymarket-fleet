"""Machine specs for register and heartbeat, and the native fleet-worker probe.

RAM and CPU come from /proc, the disk is the one that holds Docker's root dir (see
fleetagent.diskinfo). The proc and sys roots and the stat/statvfs calls are injectable
so tests run against fake trees.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Callable

from fleetagent import config, diskinfo, procinfo
from fleetagent.docker import Docker, DockerError

NATIVE_UNIT = "fleet-worker"
_INSTALLED_STATES = {
    "enabled", "enabled-runtime", "disabled", "static", "indirect", "generated", "alias",
    "linked", "linked-runtime", "masked", "masked-runtime", "transient",
}


def _default_stat_dev(path: str) -> int:
    return os.stat(path).st_dev


def native_polymarket(
    runner: Callable[..., Any] = subprocess.run, binary: str | None = None, unit: str = NATIVE_UNIT
) -> str | None:
    """"active", "inactive" or "absent" for the native fleet-worker systemd unit.

    `systemctl is-active` first ("activating" counts as active: a crash-looping worker
    still owns the machine), then `is-enabled` to tell an installed-but-stopped unit
    from a missing one. None when systemctl answers something unusable, so the caller
    can keep its last value instead of guessing; a missing systemctl binary is "absent".
    """
    exe = binary or config.systemctl_binary()
    try:
        active = runner([exe, "is-active", unit], capture_output=True, text=True, timeout=10.0)
    except FileNotFoundError:
        return "absent"
    except (subprocess.TimeoutExpired, OSError):
        return None
    word = (active.stdout or "").strip().splitlines()[0] if (active.stdout or "").strip() else ""
    if word in ("active", "activating", "reloading"):
        return "active"
    try:
        enabled = runner([exe, "is-enabled", unit], capture_output=True, text=True, timeout=10.0)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    state = (enabled.stdout or "").strip().splitlines()[0] if (enabled.stdout or "").strip() else ""
    if state in _INSTALLED_STATES:
        return "inactive"
    if state == "not-found" or (not state and word in ("inactive", "unknown")):
        return "absent"
    return None


class SpecsCollector:
    """Collects the `specs` block. One instance keeps the CPU meter and a disk cache."""

    def __init__(
        self,
        docker: Docker,
        proc: str | None = None,
        sys_root: str | None = None,
        stat_dev: Callable[[str], int] = _default_stat_dev,
        statvfs: Callable[[str], Any] = os.statvfs,
    ) -> None:
        self.docker = docker
        self._proc = proc
        self._sys = sys_root
        self._stat_dev = stat_dev
        self._statvfs = statvfs
        self._meter = procinfo.CpuMeter(proc)
        self._disk_cache: dict[tuple[str, int], diskinfo.DiskInfo] = {}
        self.docker_root: str | None = None
        self.docker_version: str | None = None
        self.docker_ok = False

    @property
    def proc(self) -> str:
        return self._proc or config.proc_root()

    @property
    def sys_root(self) -> str:
        return self._sys or config.sys_root()

    def _refresh_docker(self) -> None:
        try:
            info = self.docker.info()
        except DockerError:
            self.docker_ok = False
            return
        self.docker_ok = True
        self.docker_root = str(info["DockerRootDir"])
        self.docker_version = str(info.get("ServerVersion") or "") or None

    def disk(self, path: str) -> diskinfo.DiskInfo:
        """The disk holding `path` (cached per path and device number)."""
        target = diskinfo.nearest_existing(path)
        try:
            dev = self._stat_dev(target)
        except OSError:
            return diskinfo.UNKNOWN
        key = (self.sys_root, dev)
        if key not in self._disk_cache:
            self._disk_cache[key] = diskinfo.disk_info_for_device(os.major(dev), os.minor(dev), self.sys_root)
        return self._disk_cache[key]

    def free_mb(self, path: str) -> int | None:
        try:
            st = self._statvfs(diskinfo.nearest_existing(path))
        except OSError:
            return None
        return int(st.f_bavail * st.f_frsize) // (1024 * 1024)

    def collect(self) -> dict[str, Any]:
        """The specs block of register and heartbeat bodies."""
        self._refresh_docker()
        specs: dict[str, Any] = {
            "cpu_pct": self._meter.sample(),
            "ram_used_mb": procinfo.ram_used_mb(self.proc),
            "ram_total_mb": procinfo.ram_total_mb(self.proc),
            "cpu_count": procinfo.cpu_count(self.proc),
            "arch": procinfo.arch(),
            "disk_type": "unknown",
            "disk_size_mb": None,
            "disk_free_mb": None,
            "docker_root": self.docker_root,
            "docker_ok": self.docker_ok,
            "docker_version": self.docker_version,
        }
        if self.docker_root:
            disk = self.disk(self.docker_root)
            specs["disk_type"] = disk.type
            specs["disk_size_mb"] = disk.size_mb
            specs["disk_free_mb"] = self.free_mb(self.docker_root)
        return specs
