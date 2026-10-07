"""Per-workload data directories: <data>/<workload>/{scratch,state}.

scratch is wiped at every start and stop; state is never touched except for being
created when missing. As root the directories belong to the container uid; as the
unprivileged agent they are group-writable in the agent's group (the container joins
that group with --group-add), see fleetagent.secretfiles.

A container user can leave files the agent may not delete. wipe_dir() then falls back
to a throwaway container that runs `find <scratch> -mindepth 1 -delete` as root.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Callable

from fleetagent.docker import Docker, DockerError

log = logging.getLogger("fleetagent.workdirs")


def scratch_dir(data: str, workload: str) -> str:
    return os.path.join(data, workload, "scratch")


def state_dir(data: str, workload: str) -> str:
    return os.path.join(data, workload, "state")


def _empty(path: str) -> bool:
    try:
        return not os.listdir(path)
    except OSError:
        return True


def wipe_dir(path: str, docker: Docker | None = None, image: str | None = None) -> bool:
    """Empty `path` (keeping the directory itself). True when it is empty afterwards."""
    if not os.path.isdir(path):
        return True
    for name in os.listdir(path):
        full = os.path.join(path, name)
        try:
            if os.path.islink(full) or not os.path.isdir(full):
                os.unlink(full)
            else:
                shutil.rmtree(full)
        except OSError:
            pass
    if _empty(path):
        return True
    if docker is not None and image:
        try:
            docker.run_foreground(["--rm", "--network", "none", "--user", "0", "--entrypoint", "find",
                                   "-v", f"{path}:/wipe", image, "/wipe", "-mindepth", "1", "-delete"])
        except DockerError as exc:
            log.warning("could not wipe %s with a helper container: %s", path, exc)
    if not _empty(path):
        log.warning("%s is not empty after wiping", path)
        return False
    return True


def _make_dir(path: str, uid: int, root: bool, chown: Callable[[str, int, int], None]) -> None:
    created = not os.path.isdir(path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    if root:
        if created:
            os.chmod(path, 0o700)
            chown(path, uid, uid)
    elif created:
        os.chmod(path, 0o770)


def prepare_scratch(
    data: str, workload: str, uid: int, root: bool, chown: Callable[[str, int, int], None],
    docker: Docker | None = None, image: str | None = None,
) -> str:
    """A fresh empty scratch dir for the workload; returns its path."""
    path = scratch_dir(data, workload)
    wipe_dir(path, docker, image)
    _make_dir(path, uid, root, chown)
    return path


def prepare_state(data: str, workload: str, uid: int, root: bool, chown: Callable[[str, int, int], None]) -> str:
    """The persistent state dir (created when missing, never wiped); returns its path."""
    path = state_dir(data, workload)
    _make_dir(path, uid, root, chown)
    return path
