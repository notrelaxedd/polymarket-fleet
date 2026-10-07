"""Cleaning of what a supervisor reports: specs, container block, log batches."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from host.workloads.placement import DISK_TYPES

MAX_LOG_ENTRIES = 200
MAX_LOG_BYTES = 64 * 1024
MAX_LINE_CHARS = 2048
LOG_STREAMS = ("stdout", "stderr", "agent")
NATIVE_STATES = ("active", "inactive", "absent")
CONTAINER_STATES = ("starting", "running", "exited", "failed", "stopped")

_INT_SPECS = ("cpu_count", "ram_total_mb", "ram_used_mb", "disk_size_mb", "disk_free_mb")
_STR_SPECS = ("arch", "docker_root", "docker_version")


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value) if 0 <= value < 2**53 else None


def _text(value: Any, limit: int = 200) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.replace("\x00", "")[:limit]


def spec_columns(specs: Any) -> dict[str, Any]:
    """The `machines` columns a specs dict may set (invalid or missing values are dropped)."""
    if not isinstance(specs, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _INT_SPECS:
        value = _int(specs.get(key))
        if value is not None:
            out[key] = value
    for key in _STR_SPECS:
        value = _text(specs.get(key))
        if value is not None:
            out[key] = value
    cpu = specs.get("cpu_pct")
    if isinstance(cpu, (int, float)) and not isinstance(cpu, bool) and math.isfinite(cpu):
        out["cpu_pct"] = float(cpu)
    if isinstance(specs.get("docker_ok"), bool):
        out["docker_ok"] = specs["docker_ok"]
    if specs.get("disk_type") in DISK_TYPES:
        out["disk_type_detected"] = specs["disk_type"]
    return out


def parse_ts(value: Any) -> datetime | None:
    """An ISO-8601 timestamp (Z allowed) as an aware datetime, else None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def clip_logs(logs: Any) -> tuple[list[dict[str, Any]], int]:
    """At most 200 entries and 64 KiB of text; lines cut at 2048 chars; NUL bytes removed.

    Returns (kept, dropped): entries beyond a limit are dropped and counted, so the
    heartbeat reply can say so (the supervisor sends at most 200 lines a beat itself).
    Each kept entry is {"ts": datetime | None, "stream", "line"}.
    """
    out: list[dict[str, Any]] = []
    total = 0
    entries = logs if isinstance(logs, list) else []
    for entry in entries:
        if len(out) >= MAX_LOG_ENTRIES:
            break
        if not isinstance(entry, dict) or not isinstance(entry.get("line"), str):
            continue
        line = entry["line"].replace("\x00", "")[:MAX_LINE_CHARS]
        size = len(line.encode("utf-8"))
        if total + size > MAX_LOG_BYTES:
            break
        total += size
        stream = entry.get("stream") if entry.get("stream") in LOG_STREAMS else "stdout"
        out.append({"ts": parse_ts(entry.get("ts")), "stream": stream, "line": line})
    valid = sum(1 for e in entries if isinstance(e, dict) and isinstance(e.get("line"), str))
    return out, valid - len(out)


def container_block(container: Any) -> dict[str, Any] | None:
    """The cleaned container block of a heartbeat, or None when absent or malformed."""
    if not isinstance(container, dict):
        return None
    state = container.get("state")
    epoch = _int(container.get("epoch"))
    return {
        "workload": _text(container.get("workload"), 64),
        "epoch": epoch,
        "state": state if state in CONTAINER_STATES else None,
        "container_id": _text(container.get("container_id"), 128),
        "image_digest": _text(container.get("image_digest"), 80),
        "exit_code": container.get("exit_code") if isinstance(container.get("exit_code"), int)
        and not isinstance(container.get("exit_code"), bool) else None,
        "restarts": _int(container.get("restarts")) or 0,
        "cpu_pct": float(container["cpu_pct"]) if isinstance(container.get("cpu_pct"), (int, float))
        and not isinstance(container.get("cpu_pct"), bool) and math.isfinite(container["cpu_pct"]) else None,
        "mem_mb": _int(container.get("mem_mb")),
        "started_at": parse_ts(container.get("started_at")),
        "error": _text(container.get("error"), 2000),
    }
