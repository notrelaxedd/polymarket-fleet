"""Secret files for one workload: <run dir>/secrets/<workload>/<NAME>.

The run dir is tmpfs (systemd RuntimeDirectory), so nothing is ever written to flash.
The directory is bind-mounted read-only at /run/fleet/secrets. As root the files are
mode 0400 owned by the container uid (design section 6). The unit runs as the
fleet-agent user, which cannot chown; then the files are mode 0440 in the agent's own
group and the container gets `--group-add <gid>` (see container_group()). Files are
deleted when the container stops.
"""

from __future__ import annotations

import os
import re
import shutil
from typing import Callable

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
WORKLOAD_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def secrets_dir(run_dir: str, workload: str) -> str:
    """The directory holding the secret files of one workload."""
    if not WORKLOAD_RE.match(workload):
        raise ValueError(f"bad workload name: {workload!r}")
    return os.path.join(run_dir, "secrets", workload)


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def container_group(root: bool | None = None) -> int | None:
    """The gid the container must join to read the files: None as root (files are owned by
    the container uid), else the agent's own group."""
    if (is_root() if root is None else root):
        return None
    return os.getegid()


def write_secrets(
    run_dir: str,
    workload: str,
    secrets: dict[str, str],
    uid: int,
    *,
    root: bool | None = None,
    chown: Callable[[str, int, int], None] = os.chown,
) -> str:
    """Replace the workload's secret files with `secrets`; returns the directory.

    `root` and `chown` are injectable: tests skip the chown (they are not root) and check
    the modes. Raises ValueError for a name that is not a plain identifier.
    """
    for name in secrets:
        if not NAME_RE.match(name):
            raise ValueError(f"bad secret name: {name!r}")
    as_root = is_root() if root is None else root
    directory = secrets_dir(run_dir, workload)
    remove_secrets(run_dir, workload)
    os.makedirs(os.path.dirname(directory), mode=0o755, exist_ok=True)
    os.mkdir(directory, 0o700)
    file_mode = 0o400 if as_root else 0o440
    for name, value in secrets.items():
        path = os.path.join(directory, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(str(value).encode("utf-8"))
        os.chmod(path, file_mode)
        if as_root:
            chown(path, uid, uid)
    if as_root:
        os.chmod(directory, 0o500)
        chown(directory, uid, uid)
    else:
        os.chmod(directory, 0o750)
    return directory


def remove_secrets(run_dir: str, workload: str) -> None:
    """Delete the workload's secret files (a no-op when there are none)."""
    directory = secrets_dir(run_dir, workload)
    if not os.path.isdir(directory):
        return
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    shutil.rmtree(directory, ignore_errors=True)


def read_values(run_dir: str, workload: str) -> dict[str, str]:
    """The values currently on disk (used to rebuild the redaction list after an agent restart)."""
    directory = secrets_dir(run_dir, workload)
    out: dict[str, str] = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in names:
        try:
            with open(os.path.join(directory, name), "rb") as fh:
                out[name] = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
    return out


def workloads_with_secrets(run_dir: str) -> list[str]:
    try:
        return sorted(os.listdir(os.path.join(run_dir, "secrets")))
    except OSError:
        return []
