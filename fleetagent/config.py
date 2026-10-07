"""Directories, agent.conf and the status file of the machine agent.

Defaults (design section 6) and the environment overrides tests use:
  FLEET_AGENT_STATE_DIR  /var/lib/fleet-agent       agent.conf, status.json, app/
  FLEET_AGENT_DATA_DIR   /var/lib/fleet-workloads   <workload>/{state,scratch}
  FLEET_AGENT_RUN_DIR    /run/fleet-agent           secrets/<workload>/<NAME> (tmpfs)
  FLEET_AGENT_DOCKER     docker                     the docker binary
  FLEET_AGENT_SYSTEMCTL  systemctl                  the systemctl binary
  FLEET_AGENT_PROC_ROOT  /proc and FLEET_AGENT_SYS_ROOT /sys  (fake trees for `specs`)
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

DEFAULT_STATE_DIR = "/var/lib/fleet-agent"
DEFAULT_DATA_DIR = "/var/lib/fleet-workloads"
DEFAULT_RUN_DIR = "/run/fleet-agent"
CONF_NAME = "agent.conf"
STATUS_NAME = "status.json"


class ConfMissing(Exception):
    """agent.conf does not exist or is unusable."""


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def state_dir() -> str:
    return _env("FLEET_AGENT_STATE_DIR", DEFAULT_STATE_DIR)


def data_dir() -> str:
    return _env("FLEET_AGENT_DATA_DIR", DEFAULT_DATA_DIR)


def run_dir() -> str:
    return _env("FLEET_AGENT_RUN_DIR", DEFAULT_RUN_DIR)


def docker_binary() -> str:
    return _env("FLEET_AGENT_DOCKER", "docker")


def systemctl_binary() -> str:
    return _env("FLEET_AGENT_SYSTEMCTL", "systemctl")


def proc_root() -> str:
    return _env("FLEET_AGENT_PROC_ROOT", "/proc")


def sys_root() -> str:
    return _env("FLEET_AGENT_SYS_ROOT", "/sys")


def conf_path(directory: str) -> str:
    return os.path.join(directory, CONF_NAME)


def status_path(directory: str) -> str:
    return os.path.join(directory, STATUS_NAME)


def app_dir(directory: str) -> str:
    return os.path.join(directory, "app")


def write_json_atomic(path: str, data: Any, mode: int = 0o644) -> None:
    """Write JSON atomically (temp file, chmod, rename) with the given file mode."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_conf(directory: str) -> dict[str, Any]:
    """Load agent.conf; raise ConfMissing when absent or incomplete."""
    path = conf_path(directory)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfMissing(f"{path}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfMissing(f"{path}: not a JSON object")
    for key in ("host_url", "machine_id", "machine_token"):
        if not data.get(key):
            raise ConfMissing(f"{path}: missing {key}")
    data["host_url"] = str(data["host_url"]).rstrip("/")
    return data


def save_conf(directory: str, conf: dict[str, Any]) -> None:
    """Write agent.conf with mode 0600."""
    write_json_atomic(conf_path(directory), conf, 0o600)


def save_status(directory: str, status: dict[str, Any]) -> None:
    """Write status.json (best effort; errors are swallowed)."""
    try:
        write_json_atomic(status_path(directory), status, 0o644)
    except OSError:
        pass


def load_status(directory: str) -> dict[str, Any] | None:
    try:
        with open(status_path(directory), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def redacted(conf: dict[str, Any]) -> dict[str, Any]:
    """Copy of the conf with the token hidden."""
    out = dict(conf)
    token = str(out.get("machine_token", ""))
    out["machine_token"] = (token[:4] + "..." + token[-2:]) if len(token) > 8 else "***"
    return out
