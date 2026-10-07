"""Workload manifests (`workloads/<name>/workload.toml`, docs/workloads-design.md section 2).

Pure parsing and validation; no database. Every problem is collected so the owner sees
the whole list at once, not one error per sync.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
IMAGE_RE = re.compile(r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*$")
PROTOCOLS = ("workload-v1", "fleet-worker")
MODES = ("jobs", "service")
NETWORKS = ("bridge", "host")
OUTBOUND_KINDS = ("email", "log")
MAX_SECRETS = 32
MANIFEST_FILE = "workload.toml"


class ManifestError(ValueError):
    """A manifest that cannot be used; `problems` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Resources:
    min_ram_mb: int
    min_disk_mb: int
    write_heavy: bool
    memory_max_mb: int | None = None
    memory_max_pct: int | None = None
    cpus: float | None = None


@dataclass(frozen=True)
class Runtime:
    mode: str
    job_kinds: tuple[str, ...] = ()
    network: str = "bridge"
    uts_host: bool = False
    uid: int = 10001
    state_volume: bool = False
    scratch_mb: int = 512
    stop_timeout_s: int = 15
    no_restart_exit_codes: tuple[int, ...] = (78,)
    nice: int = 0


@dataclass(frozen=True)
class Manifest:
    name: str
    description: str
    image: str
    protocol: str
    resources: Resources
    runtime: Runtime
    container_secrets: tuple[str, ...] = ()
    host_only_secrets: tuple[str, ...] = ()
    outbound_actions: tuple[str, ...] = ()
    can_trade: bool = False
    schema: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_approval(self) -> bool:
        """True when any outbound action is declared: every send waits for the owner."""
        return bool(self.outbound_actions)

    def to_json(self) -> dict[str, Any]:
        """JSON-able dict, stored in `workloads.manifest`; `from_json` reverses it."""
        data = asdict(self)
        data.pop("extra", None)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Manifest":
        res = data["resources"]
        rt = data["runtime"]
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            image=data["image"],
            protocol=data["protocol"],
            resources=Resources(**res),
            runtime=Runtime(
                **{
                    **rt,
                    "job_kinds": tuple(rt.get("job_kinds", ())),
                    "no_restart_exit_codes": tuple(rt.get("no_restart_exit_codes", (78,))),
                }
            ),
            container_secrets=tuple(data.get("container_secrets", ())),
            host_only_secrets=tuple(data.get("host_only_secrets", ())),
            outbound_actions=tuple(data.get("outbound_actions", ())),
            can_trade=bool(data.get("can_trade", False)),
            schema=int(data.get("schema", 1)),
        )


def _int(table: dict[str, Any], key: str, problems: list[str], lo: int, hi: int, default: int | None = None) -> int | None:
    value = table.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        problems.append(f"{key} must be an integer {lo}..{hi}")
        return default
    return value


def _bool(table: dict[str, Any], key: str, problems: list[str], default: bool = False) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        problems.append(f"{key} must be true or false")
        return default
    return value


def _names(table: dict[str, Any], key: str, pattern: re.Pattern[str], problems: list[str]) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{key} must be a list of strings")
        return ()
    bad = [v for v in value if not pattern.match(v)]
    if bad:
        problems.append(f"{key}: invalid names {bad}")
    if len(set(value)) != len(value):
        problems.append(f"{key}: duplicate names")
    return tuple(v for v in value if pattern.match(v))


def _resources(raw: Any, problems: list[str]) -> Resources:
    table = raw if isinstance(raw, dict) else {}
    if not isinstance(raw, dict):
        problems.append("[resources] table is required")
    min_ram = _int(table, "min_ram_mb", problems, 16, 1_048_576, 128) or 128
    min_disk = _int(table, "min_disk_mb", problems, 0, 10_485_760, 0) or 0
    write_heavy = _bool(table, "write_heavy", problems)
    mem_mb = _int(table, "memory_max_mb", problems, 16, 1_048_576)
    mem_pct = _int(table, "memory_max_pct", problems, 1, 100)
    if mem_mb is not None and mem_pct is not None:
        problems.append("set memory_max_mb or memory_max_pct, not both")
    cpus = table.get("cpus")
    if cpus is not None and (isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not 0 < cpus <= 256):
        problems.append("cpus must be a number above 0")
        cpus = None
    return Resources(min_ram, min_disk, write_heavy, mem_mb, mem_pct, float(cpus) if cpus is not None else None)


def _runtime(raw: Any, protocol: str, problems: list[str]) -> Runtime:
    table = raw if isinstance(raw, dict) else {}
    if not isinstance(raw, dict):
        problems.append("[runtime] table is required")
    mode = table.get("mode", "jobs")
    if mode not in MODES:
        problems.append(f"runtime.mode must be one of {MODES}")
        mode = "jobs"
    kinds = _names(table, "job_kinds", KIND_RE, problems)
    if mode == "jobs" and not kinds:
        problems.append("runtime.job_kinds must list at least one kind when mode is jobs")
    network = table.get("network", "bridge")
    if network not in NETWORKS:
        problems.append(f"runtime.network must be one of {NETWORKS}")
        network = "bridge"
    uts_host = _bool(table, "uts_host", problems)
    if protocol != "fleet-worker" and (network == "host" or uts_host):
        problems.append("network = host and uts_host are allowed only with protocol fleet-worker")
    codes = table.get("no_restart_exit_codes", [78])
    if not isinstance(codes, list) or not all(isinstance(c, int) and not isinstance(c, bool) and 0 < c < 256 for c in codes):
        problems.append("runtime.no_restart_exit_codes must be a list of exit codes 1..255")
        codes = [78]
    return Runtime(
        mode=mode,
        job_kinds=kinds,
        network=network,
        uts_host=uts_host,
        uid=_int(table, "uid", problems, 1, 2_147_483_647, 10001) or 10001,
        state_volume=_bool(table, "state_volume", problems),
        scratch_mb=_int(table, "scratch_mb", problems, 0, 1_048_576, 512) or 0,
        stop_timeout_s=_int(table, "stop_timeout_s", problems, 1, 600, 15) or 15,
        no_restart_exit_codes=tuple(codes),
        nice=_int(table, "nice", problems, 0, 19, 0) or 0,
    )


def parse_manifest(data: dict[str, Any], folder_name: str | None = None) -> Manifest:
    """Validate a decoded workload.toml; raise ManifestError listing every problem."""
    problems: list[str] = []
    if data.get("schema", 1) != 1:
        problems.append("schema must be 1")
    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        problems.append("name must match ^[a-z][a-z0-9-]{1,31}$")
        name = str(name)
    if folder_name is not None and name != folder_name:
        problems.append(f"name {name!r} must equal the folder name {folder_name!r}")
    description = data.get("description", "")
    if not isinstance(description, str) or len(description) > 300:
        problems.append("description must be a string of at most 300 characters")
        description = ""
    image = data.get("image")
    if not isinstance(image, str) or not IMAGE_RE.match(image) or len(image) > 200:
        problems.append("image must be a repository name such as fleet/hello (no tag, no digest)")
        image = str(image)
    protocol = data.get("protocol", "workload-v1")
    if protocol not in PROTOCOLS:
        problems.append(f"protocol must be one of {PROTOCOLS}")
        protocol = "workload-v1"
    resources = _resources(data.get("resources"), problems)
    runtime = _runtime(data.get("runtime"), protocol, problems)
    secrets = data.get("secrets", {}) if isinstance(data.get("secrets", {}), dict) else {}
    container = _names(secrets, "container", SECRET_RE, problems)
    host_only = _names(secrets, "host_only", SECRET_RE, problems)
    if set(container) & set(host_only):
        problems.append(f"secrets in both container and host_only: {sorted(set(container) & set(host_only))}")
    if len(container) + len(host_only) > MAX_SECRETS:
        problems.append(f"at most {MAX_SECRETS} secrets per workload")
    outbound = data.get("outbound", {}) if isinstance(data.get("outbound", {}), dict) else {}
    actions = outbound.get("actions", [])
    if not isinstance(actions, list) or any(a not in OUTBOUND_KINDS for a in actions):
        problems.append(f"outbound.actions must be a subset of {OUTBOUND_KINDS}")
        actions = []
    if host_only and not actions:
        problems.append("host_only secrets are only for outbound senders; declare outbound.actions")
    trading = data.get("trading", {}) if isinstance(data.get("trading", {}), dict) else {}
    can_trade = _bool(trading, "can_trade", problems)
    if can_trade and protocol != "fleet-worker":
        problems.append("trading.can_trade is only for protocol fleet-worker")
    if problems:
        raise ManifestError(problems)
    return Manifest(
        name=name,
        description=description,
        image=image,
        protocol=protocol,
        resources=resources,
        runtime=runtime,
        container_secrets=container,
        host_only_secrets=host_only,
        outbound_actions=tuple(actions),
        can_trade=can_trade,
    )


def load_manifest(path: Path) -> Manifest:
    """Read and validate `<folder>/workload.toml` (path may be the folder or the file)."""
    file = path / MANIFEST_FILE if path.is_dir() else path
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError([f"{file}: {exc}"]) from exc
    return parse_manifest(data, file.parent.name)


def discover(root: Path) -> list[Path]:
    """Workload folders under root (those with a manifest, skipping names starting with `_`)."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")) and (p / MANIFEST_FILE).is_file())
