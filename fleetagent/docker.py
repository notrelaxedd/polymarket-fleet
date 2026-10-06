"""Thin wrapper over the docker CLI (subprocess, injectable runner).

Docker(runner=subprocess.run, binary="docker"): every call goes through
`runner(cmd, capture_output=True, text=True, timeout=..., env=...)`, so a test passes a
fake runner and never needs a daemon. Failures raise DockerError (non-zero exit, timeout,
missing binary). Nothing here decides policy; supervisor.py and cleanup.py do.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$")
_UNITS = {
    "b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
}


class DockerError(Exception):
    """A docker command failed."""

    def __init__(self, message: str, cmd: list[str] | None = None, returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.cmd = cmd or []
        self.returncode = returncode
        self.stderr = stderr


def parse_size(text: str) -> int:
    """Bytes from docker's human sizes ("178MB", "24.58kB", "1.2GiB", "0B"); 0 when unparseable."""
    match = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$", text or "")
    if not match:
        return 0
    return int(float(match.group(1)) * _UNITS.get(match.group(2).lower() or "b", 1))


def normalize_ts(ts: str) -> str:
    """A docker log timestamp with a fixed 9-digit fraction and a Z suffix, so strings sort as times."""
    match = TS_RE.match(ts)
    if not match:
        return ts
    frac = (match.group(2) or ".")[1:].ljust(9, "0")[:9]
    return f"{match.group(1)}.{frac}Z"


@dataclass
class ContainerInfo:
    """What `docker inspect` says about one container."""

    id: str
    name: str = ""
    status: str = ""
    running: bool = False
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str = ""
    oom_killed: bool = False
    image_id: str = ""
    image: str = ""
    created: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    env: list[str] = field(default_factory=list)
    stop_timeout: int | None = None

    @property
    def workload(self) -> str | None:
        return self.labels.get("fleet.workload")

    @property
    def epoch(self) -> int | None:
        try:
            return int(self.labels.get("fleet.epoch", ""))
        except ValueError:
            return None

    def env_value(self, name: str) -> str | None:
        prefix = name + "="
        for item in self.env:
            if item.startswith(prefix):
                return item[len(prefix):]
        return None


@dataclass
class ImageInfo:
    """One row of `docker images --digests`."""

    id: str
    repository: str
    tag: str
    digest: str
    size_bytes: int

    @property
    def ref(self) -> str | None:
        """A reference rmi accepts (repo@digest for digest pulls, repo:tag), None for dangling images."""
        if self.repository in ("", "<none>"):
            return None
        if self.tag in ("", "<none>"):
            return f"{self.repository}@{self.digest}" if self.digest.startswith("sha256:") else None
        return f"{self.repository}:{self.tag}"


def _container_from_inspect(data: dict[str, Any]) -> ContainerInfo:
    state = data.get("State") or {}
    config = data.get("Config") or {}
    return ContainerInfo(
        id=str(data.get("Id", "")),
        name=str(data.get("Name", "")).lstrip("/"),
        status=str(state.get("Status", "")),
        running=bool(state.get("Running")),
        exit_code=state.get("ExitCode") if isinstance(state.get("ExitCode"), int) else None,
        started_at=state.get("StartedAt"),
        finished_at=state.get("FinishedAt"),
        error=str(state.get("Error") or ""),
        oom_killed=bool(state.get("OOMKilled")),
        image_id=str(data.get("Image", "")),
        image=str(config.get("Image", "")),
        created=str(data.get("Created", "")),
        labels=dict(config.get("Labels") or {}),
        env=list(config.get("Env") or []),
        stop_timeout=config.get("StopTimeout") if isinstance(config.get("StopTimeout"), int) else None,
    )


class Docker:
    """The docker CLI behind injectable `runner` and `binary`."""

    def __init__(self, runner: Callable[..., Any] = subprocess.run, binary: str = "docker") -> None:
        self._runner = runner
        self.binary = binary

    def _exec(self, args: list[str], timeout: float = 60.0, env: dict[str, str] | None = None, check: bool = True) -> Any:
        cmd = [self.binary, *args]
        kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout}
        if env is not None:
            kwargs["env"] = env
        try:
            proc = self._runner(cmd, **kwargs)
        except FileNotFoundError:
            raise DockerError(f"docker binary not found: {self.binary}", cmd) from None
        except subprocess.TimeoutExpired:
            raise DockerError(f"docker {args[0]} timed out after {timeout:.0f}s", cmd) from None
        except OSError as exc:
            raise DockerError(f"docker {args[0]} failed to run: {exc}", cmd) from None
        if check and proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise DockerError(f"docker {' '.join(args[:2])} exited {proc.returncode}: {err[:300]}", cmd, proc.returncode, err)
        return proc

    def _json_lines(self, args: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for line in (self._exec(args).stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    # ------------------------------------------------------------------ daemon

    def info(self) -> dict[str, Any]:
        """`docker info` as a dict (DockerRootDir, ServerVersion, ...); DockerError when the daemon is unreachable."""
        proc = self._exec(["info", "--format", "{{json .}}"], timeout=20.0)
        try:
            data = json.loads(proc.stdout or "")
        except ValueError:
            raise DockerError("docker info returned no JSON", [self.binary, "info"]) from None
        if not isinstance(data, dict) or not data.get("DockerRootDir"):
            raise DockerError("docker info has no DockerRootDir", [self.binary, "info"])
        return data

    # ------------------------------------------------------------------ images

    def image_present(self, ref: str) -> bool:
        return self._exec(["image", "inspect", "--format", "{{.Id}}", ref], check=False).returncode == 0

    def pull(self, ref: str) -> None:
        self._exec(["pull", "--quiet", ref], timeout=3600.0)

    def images(self, reference: str | None = None, label: str | None = None) -> list[ImageInfo]:
        args = ["images", "--digests", "--no-trunc", "--format", "{{json .}}"]
        if reference:
            args += ["--filter", f"reference={reference}"]
        if label:
            args += ["--filter", f"label={label}"]
        return [
            ImageInfo(
                id=str(r.get("ID", "")), repository=str(r.get("Repository", "")), tag=str(r.get("Tag", "")),
                digest=str(r.get("Digest", "")), size_bytes=parse_size(str(r.get("Size", ""))),
            )
            for r in self._json_lines(args)
        ]

    def rmi(self, ref: str) -> None:
        self._exec(["rmi", ref])

    def image_prune(self) -> int:
        """`docker image prune -f`; bytes reclaimed."""
        out = self._exec(["image", "prune", "-f"], timeout=300.0).stdout or ""
        match = re.search(r"reclaimed space:\s*(\S+)", out)
        return parse_size(match.group(1)) if match else 0

    def builder_prune(self) -> int:
        """`docker builder prune -af`; bytes reclaimed."""
        out = self._exec(["builder", "prune", "-af"], timeout=300.0).stdout or ""
        match = re.search(r"Total:\s*(\S+)", out)
        return parse_size(match.group(1)) if match else 0

    # -------------------------------------------------------------- containers

    def run(self, args: list[str], env: dict[str, str] | None = None) -> str:
        """`docker run -d <args>`; the new container id. `env` is the client environment
        (a `-e NAME` flag without a value reads the value from it, keeping secrets off argv)."""
        out = self._exec(["run", "-d", *args], timeout=180.0, env=env).stdout or ""
        cid = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if not cid:
            raise DockerError("docker run printed no container id")
        return cid

    def run_foreground(self, args: list[str]) -> str:
        """`docker run <args>` in the foreground (helper containers); its stdout."""
        return self._exec(["run", *args], timeout=300.0).stdout or ""

    def stop(self, container: str, timeout: int) -> None:
        self._exec(["stop", "-t", str(int(timeout)), container], timeout=float(timeout) + 60.0)

    def rm(self, container: str) -> None:
        self._exec(["rm", "-f", container])

    def inspect_many(self, ids: list[str]) -> list[ContainerInfo]:
        if not ids:
            return []
        proc = self._exec(["inspect", "--type", "container", *ids], check=False)
        try:
            rows = json.loads(proc.stdout or "[]")
        except ValueError:
            rows = []
        return [_container_from_inspect(r) for r in rows if isinstance(r, dict)]

    def inspect(self, container: str) -> ContainerInfo | None:
        found = self.inspect_many([container])
        return found[0] if found else None

    def ps(self, labels: list[str], all_states: bool = True) -> list[ContainerInfo]:
        """Containers carrying every given label ("fleet.workload" or "k=v"), as inspected infos."""
        args = ["ps", "-q", "--no-trunc"] + (["-a"] if all_states else [])
        for label in labels:
            args += ["--filter", f"label={label}"]
        ids = [line.strip() for line in (self._exec(args).stdout or "").splitlines() if line.strip()]
        return self.inspect_many(ids)

    def container_prune(self, label: str) -> None:
        self._exec(["container", "prune", "-f", "--filter", f"label={label}"], timeout=300.0)

    def stats(self, container: str) -> dict[str, float | int] | None:
        """{"cpu_pct", "mem_mb"} from one `docker stats --no-stream` sample, None when unavailable."""
        proc = self._exec(["stats", "--no-stream", "--format", "{{json .}}", container], timeout=30.0, check=False)
        if proc.returncode != 0:
            return None
        for line in (proc.stdout or "").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            cpu = str(row.get("CPUPerc", "")).strip().rstrip("%")
            used = str(row.get("MemUsage", "")).split("/")[0]
            try:
                return {"cpu_pct": round(float(cpu), 1), "mem_mb": parse_size(used) // (1024 * 1024)}
            except ValueError:
                return None
        return None

    def logs(self, container: str, since: str | None = None, tail: int | None = None) -> list[tuple[str, str, str]]:
        """(timestamp, stream, text) of the container's log lines at or after `since` (at most the
        last `tail` lines), oldest first. Timestamps are normalized to 9 fraction digits so they
        compare as strings."""
        args = ["logs", "--timestamps"]
        if since:
            args += ["--since", since]
        if tail is not None:
            args += ["--tail", str(int(tail))]
        proc = self._exec([*args, container], timeout=30.0, check=False)
        if proc.returncode != 0:
            raise DockerError(f"docker logs exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}", returncode=proc.returncode)
        rows: list[tuple[str, str, str]] = []
        for stream, text in (("stdout", proc.stdout or ""), ("stderr", proc.stderr or "")):
            for raw in text.splitlines():
                ts, _, rest = raw.partition(" ")
                if TS_RE.match(ts):
                    rows.append((normalize_ts(ts), stream, rest))
        rows.sort(key=lambda r: r[0])
        return rows
