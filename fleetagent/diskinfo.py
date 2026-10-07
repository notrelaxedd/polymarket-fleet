"""Which physical disk holds a path, and is it flash, hdd, ssd or unknown.

Design section 6: st_dev of Docker's root dir -> <sys>/dev/block/<major>:<minor> ->
the parent disk (a partition has a `partition` file, a device-mapper or md device lists
its parents under `slaves/`). Type is `flash` when removable=1, the name starts with
mmcblk, or the device path goes through a USB bus; else `hdd` when queue/rotational is 1,
`ssd` when 0, else `unknown`. Size is sysfs `size` (512-byte sectors).

The sysfs root is a parameter so tests can build fake trees.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

_USB_RE = re.compile(r"^usb\d+$")
_MAX_DEPTH = 8


@dataclass(frozen=True)
class DiskInfo:
    """The disk behind a filesystem: type (flash|hdd|ssd|unknown), size in MiB and the disk's name."""

    type: str
    size_mb: int | None
    name: str | None


UNKNOWN = DiskInfo("unknown", None, None)


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _physical_disks(path: str, depth: int = 0) -> list[str]:
    """Real sysfs directories of the physical disks behind a block device directory."""
    path = os.path.realpath(path)
    if depth > _MAX_DEPTH:
        return [path]
    if os.path.exists(os.path.join(path, "partition")):
        path = os.path.dirname(path)
    slaves = os.path.join(path, "slaves")
    try:
        names = sorted(os.listdir(slaves))
    except OSError:
        names = []
    if not names:
        return [path]
    out: list[str] = []
    for name in names:
        for disk in _physical_disks(os.path.join(slaves, name), depth + 1):
            if disk not in out:
                out.append(disk)
    return out or [path]


def classify_disk(disk_dir: str, sys_root: str) -> str:
    """flash, hdd, ssd or unknown for one physical disk directory."""
    name = os.path.basename(disk_dir)
    if _read(os.path.join(disk_dir, "removable")) == "1" or name.startswith("mmcblk"):
        return "flash"
    try:
        rel = os.path.relpath(disk_dir, os.path.realpath(sys_root))
    except ValueError:
        rel = disk_dir
    if any(_USB_RE.match(part) for part in rel.split(os.sep)):
        return "flash"
    rotational = _read(os.path.join(disk_dir, "queue", "rotational"))
    if rotational == "1":
        return "hdd"
    if rotational == "0":
        return "ssd"
    return "unknown"


def _combine(types: list[str]) -> str:
    """The most cautious type of several parents: flash, then unknown, then hdd, then ssd."""
    for wanted in ("flash", "unknown", "hdd"):
        if wanted in types:
            return wanted
    return "ssd" if types else "unknown"


def disk_info_for_device(major: int, minor: int, sys_root: str) -> DiskInfo:
    """Resolve the block device major:minor through <sys_root>/dev/block."""
    node = os.path.join(sys_root, "dev", "block", f"{major}:{minor}")
    if not os.path.exists(node):
        return UNKNOWN
    disks = _physical_disks(node)
    sectors = _read(os.path.join(disks[0], "size"))
    size_mb: int | None = None
    if sectors and sectors.isdigit():
        size_mb = int(sectors) * 512 // (1024 * 1024)
    kind = _combine([classify_disk(d, sys_root) for d in disks])
    return DiskInfo(kind, size_mb, os.path.basename(disks[0]))


def nearest_existing(path: str) -> str:
    """The path itself or its closest existing ancestor (Docker's root may not exist yet)."""
    current = path
    while current and not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current or "/"
