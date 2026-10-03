"""CPU, RAM and boot id from /proc. Every reader tolerates a missing file."""

from __future__ import annotations

import os

PROC_STAT = "/proc/stat"
PROC_MEMINFO = "/proc/meminfo"
PROC_BOOT_ID = "/proc/sys/kernel/random/boot_id"


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _read_cpu_counters(path: str = PROC_STAT) -> tuple[int, int] | None:
    """Return (total_jiffies, idle_jiffies) from the aggregate cpu line."""
    text = _read_text(path)
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
    """CPU percentage from the delta between two /proc/stat samples."""

    def __init__(self, path: str = PROC_STAT) -> None:
        self._path = path
        self._last: tuple[int, int] | None = None

    def sample(self) -> float:
        """Return busy percent since the previous call (0.0 on the first call)."""
        current = _read_cpu_counters(self._path)
        if current is None:
            return 0.0
        previous = self._last
        self._last = current
        if previous is None:
            return 0.0
        total = current[0] - previous[0]
        idle = current[1] - previous[1]
        if total <= 0:
            return 0.0
        busy = 100.0 * (total - idle) / total
        return round(max(0.0, min(100.0, busy)), 1)


_default_meter = CpuMeter()


def cpu_pct() -> float:
    """Module-level CPU meter (first call returns 0.0)."""
    return _default_meter.sample()


def _meminfo_kb(path: str = PROC_MEMINFO) -> dict[str, int]:
    text = _read_text(path)
    if text is None:
        return {}
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            try:
                out[parts[0][:-1]] = int(parts[1])
            except ValueError:
                continue
    return out


def ram_total_mb(path: str = PROC_MEMINFO) -> int | None:
    """Total RAM in MiB, or None when /proc/meminfo is unreadable."""
    info = _meminfo_kb(path)
    if "MemTotal" not in info:
        return None
    return info["MemTotal"] // 1024


def ram_used_mb(path: str = PROC_MEMINFO) -> int | None:
    """Used RAM in MiB (MemTotal - MemAvailable), or None when unreadable."""
    info = _meminfo_kb(path)
    if "MemTotal" not in info:
        return None
    available = info.get("MemAvailable", info.get("MemFree", 0))
    return max(0, info["MemTotal"] - available) // 1024


def boot_id(path: str = PROC_BOOT_ID) -> str | None:
    """Kernel boot id, or None when unreadable."""
    text = _read_text(path)
    if text is None:
        return None
    return text.strip() or None


def hostname() -> str:
    """Short host name."""
    try:
        return os.uname().nodename
    except (AttributeError, OSError):
        return "unknown"
