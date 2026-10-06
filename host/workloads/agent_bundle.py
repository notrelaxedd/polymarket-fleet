"""The fleetagent tarball served from /dl/agent.tar.gz, built lazily.

Same recipe as host.bundle (sorted *.py, version = first 12 hex of sha256 over paths and
bytes, reproducible tar) with `fleetagent/` as the top-level directory. The package may
be absent (a host image built before it existed): nothing here is built at import or
app start, and `get_bundle()` returns None so /dl/agent* can answer 503.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
import threading
from pathlib import Path

from host.bundle import VERSION_FILE, Bundle, _tarinfo, compute_code_version, source_files
from host.workloads import config

TOP_DIR = "fleetagent"
_lock = threading.Lock()
_cache: dict[str, Bundle] = {}


def build_tarball(files: list[tuple[str, bytes]], version: str) -> bytes:
    """A gzip tarball with one top-level `fleetagent/` dir, *.py files and VERSION."""
    dirs: set[str] = set()
    for rel, _ in files:
        parent = Path(rel).parent.as_posix()
        while parent not in ("", "."):
            dirs.add(parent)
            parent = Path(parent).parent.as_posix()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        tar.addfile(_tarinfo(TOP_DIR, 0, is_dir=True))
        for d in sorted(dirs):
            tar.addfile(_tarinfo(f"{TOP_DIR}/{d}", 0, is_dir=True))
        version_bytes = (version + "\n").encode("utf-8")
        tar.addfile(_tarinfo(f"{TOP_DIR}/{VERSION_FILE}", len(version_bytes)), io.BytesIO(version_bytes))
        for rel, data in files:
            tar.addfile(_tarinfo(f"{TOP_DIR}/{rel}", len(data)), io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def build_agent_bundle(root: Path) -> Bundle | None:
    """Build the bundle from a fleetagent directory; None when it holds no python files."""
    files = source_files(root) if root.is_dir() else []
    if not files:
        return None
    version = compute_code_version(files)
    data = build_tarball(files, version)
    return Bundle(code_version=version, sha256=hashlib.sha256(data).hexdigest(), data=data)


def get_bundle() -> Bundle | None:
    """The cached bundle (built on first use), or None while the package is missing."""
    root = config.agent_dir()
    key = str(root)
    with _lock:
        if key not in _cache:
            built = build_agent_bundle(root)
            if built is None:
                return None
            _cache[key] = built
        return _cache[key]


def current_version() -> str | None:
    """The agent_version the host offers machines, None when there is no package."""
    bundle = get_bundle()
    return bundle.code_version if bundle else None


def reset_cache() -> None:
    """Forget the cached bundle (tests)."""
    with _lock:
        _cache.clear()
