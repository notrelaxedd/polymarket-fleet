"""Placement rules (docs/workloads-design.md section 4.1): pure functions, no database."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from host.workloads.manifest import Manifest

DISK_TYPES = ("ssd", "hdd", "flash", "unknown")


@dataclass(frozen=True)
class Refusal:
    """One reason a workload does not fit a machine."""

    code: str
    message: str


def effective_disk_type(machine: dict[str, Any]) -> str:
    """The owner's override if set, else the detected type, else "unknown"."""
    for key in ("disk_type_override", "disk_type_detected"):
        value = machine.get(key)
        if value in DISK_TYPES:
            return value
    return "unknown"


def check_placement(
    manifest: Manifest,
    machine: dict[str, Any],
    *,
    image_size_mb: int | None = None,
    image_published: bool = True,
    workload_enabled: bool = True,
) -> list[Refusal]:
    """Every refusal that applies, in the contract order (empty list = it fits)."""
    out: list[Refusal] = []
    res = manifest.resources
    if not workload_enabled:
        out.append(Refusal("workload_disabled", f"workload {manifest.name} is disabled"))
    if not image_published:
        out.append(Refusal("image_not_published", f"no image has been published for {manifest.name}"))
    if not machine.get("docker_ok"):
        out.append(Refusal("docker_missing", "Docker is not installed or not working on this machine"))
    ram = machine.get("ram_total_mb")
    if ram is None:
        out.append(Refusal("ram_too_small", f"needs {res.min_ram_mb} MB RAM, machine has not reported its RAM"))
    elif ram < res.min_ram_mb:
        out.append(Refusal("ram_too_small", f"needs {res.min_ram_mb} MB RAM, machine has {ram} MB"))
    need_disk = res.min_disk_mb + (image_size_mb or 0)
    free = machine.get("disk_free_mb")
    if free is None:
        out.append(Refusal("disk_too_small", f"needs {need_disk} MB free disk, machine has not reported its disk"))
    elif free < need_disk:
        out.append(Refusal("disk_too_small", f"needs {need_disk} MB free disk, machine has {free} MB"))
    disk = effective_disk_type(machine)
    if res.write_heavy and disk in ("flash", "unknown"):
        out.append(Refusal("write_heavy_on_flash", f"writes a lot and the disk is {disk}"))
    return out
