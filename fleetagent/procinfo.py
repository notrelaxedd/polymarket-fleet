"""CPU, RAM, CPU count and boot id from /proc. Every reader tolerates a missing file
and takes the proc root as an argument so tests can point it at a fake tree."""

from __future__ import annotations

import os
import platform

from fleetagent import config


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _cpu_counters(proc: str) -> tuple[int, int] | None:
    """(total_jiffies, idle_jiffies) from the aggregate cpu line of <proc>/stat."""
    text = _read_text(os.path.join(proc, "stat"))
    if text is None:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "cpu":
            try:
                values = [int(p) for p in parts[1:]]
            except ValueError:
                return None
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            return sum(values), idle
    return None


class CpuMeter:
    """CPU percentage from the delta between two stat samples (0.0 on the first call)."""

    def __init__(self, proc: str | None = None) -> None:
        self._proc = proc
        self._last: tuple[int, int] | None = None

    def sample(self) -> float:
        current = _cpu_counters(self._proc or config.proc_root())
        if current is None:
            return 0.0
        previous, self._last = self._last, current
        if previous is None:
            return 0.0
        total = current[0] - previous[0]
        idle = current[1] - previous[1]
        if total <= 0:
            return 0.0
        return round(max(0.0, min(100.0, 100.0 * (total - idle) / total)), 1)


def _meminfo_kb(proc: str) -> dict[str, int]:
    text = _read_text(os.path.join(proc, "meminfo")) or ""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            try:
                out[parts[0][:-1]] = int(parts[1])
            except ValueError:
                continue
    return out


def ram_total_mb(proc: str | None = None) -> int | None:
    info = _meminfo_kb(proc or config.proc_root())
    return info["MemTotal"] // 1024 if "MemTotal" in info else None


def ram_used_mb(proc: str | None = None) -> int | None:
    """MemTotal - MemAvailable in MiB, or None when unreadable."""
    info = _meminfo_kb(proc or config.proc_root())
    if "MemTotal" not in info:
        return None
    return max(0, info["MemTotal"] - info.get("MemAvailable", info.get("MemFree", 0))) // 1024


def cpu_count(proc: str | None = None) -> int | None:
    """Number of "processor" entries in <proc>/cpuinfo, else os.cpu_count()."""
    text = _read_text(os.path.join(proc or config.proc_root(), "cpuinfo"))
    if text is not None:
        count = sum(1 for line in text.splitlines() if line.split(":")[0].strip() == "processor")
        if count:
            return count
    return os.cpu_count()


def boot_id(proc: str | None = None) -> str | None:
    text = _read_text(os.path.join(proc or config.proc_root(), "sys", "kernel", "random", "boot_id"))
    return (text.strip() or None) if text is not None else None


def hostname() -> str:
    try:
        return os.uname().nodename
    except (AttributeError, OSError):
        return "unknown"


def arch() -> str:
    return platform.machine() or "unknown"
