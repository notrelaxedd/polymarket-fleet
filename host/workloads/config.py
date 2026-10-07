"""Workload settings read from the environment at call time (so tests can monkeypatch them).

FLEET_REGISTRY       host:port of the image registry machines pull from (default localhost:5000).
FLEET_WORKLOADS_DIR  folder holding workloads/<name>/workload.toml (default <repo>/workloads,
                     which is /app/workloads in the image).
FLEET_SECRETS_KEY    base64 of 32 random bytes; lives in secrets.env, never in .env.
FLEET_AGENT_DIR      override for the fleetagent package directory (default <repo>/fleetagent).
"""
from __future__ import annotations

import os
from pathlib import Path

from host.config import REPO_ROOT

DEFAULT_REGISTRY = "localhost:5000"


def registry() -> str:
    """The registry host:port, without scheme or trailing slash."""
    value = os.environ.get("FLEET_REGISTRY", "").strip().rstrip("/")
    for scheme in ("https://", "http://"):
        value = value.removeprefix(scheme)
    return value or DEFAULT_REGISTRY


def workloads_dir() -> Path:
    """Folder scanned by `workloads-sync` and POST /api/workloads/sync."""
    raw = os.environ.get("FLEET_WORKLOADS_DIR", "").strip()
    if not raw:
        return REPO_ROOT / "workloads"
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def secrets_key() -> str:
    """The raw FLEET_SECRETS_KEY value ("" when not configured)."""
    return os.environ.get("FLEET_SECRETS_KEY", "").strip()


def agent_dir() -> Path:
    """Directory of the fleetagent package (may not exist)."""
    raw = os.environ.get("FLEET_AGENT_DIR", "").strip()
    return Path(raw) if raw else REPO_ROOT / "fleetagent"
