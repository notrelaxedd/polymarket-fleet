"""The `docker run` arguments of a workload container (design section 6, step 3).

Exactly the flags listed there, plus two that the unprivileged unit needs or the
Polymarket bootstrap reads: `--group-add <gid>` (the container user joins the agent's
group to read the secret files and scratch dir when the agent cannot chown) and the
environment variable FLEET_NICE. The run token never appears on the command line:
`-e FLEET_RUN_TOKEN` (no value) makes docker read it from the client's environment.
"""

from __future__ import annotations

import re
from typing import Any

DIGEST_RE = re.compile(r"@(sha256:[0-9a-f]{64})$")
TOKEN_ENV = "FLEET_RUN_TOKEN"
DEFAULT_UID = 10001


def container_name(workload: str, epoch: int) -> str:
    return f"fleet-{workload}-{epoch}"


def image_digest(ref: str) -> str | None:
    """The sha256 digest in an image reference (repo@sha256:...), None for a tag."""
    match = DIGEST_RE.search(ref or "")
    return match.group(1) if match else None


def run_uid(run: dict[str, Any]) -> int:
    try:
        return int(run.get("uid") or DEFAULT_UID)
    except (TypeError, ValueError):
        return DEFAULT_UID


def build_run_args(
    workload: str,
    epoch: int,
    run: dict[str, Any],
    *,
    scratch: str,
    state: str | None,
    secrets: str,
    group_add: int | None = None,
) -> list[str]:
    """Arguments for `docker run -d` (everything after it, ending with the image)."""
    uid = run_uid(run)
    args = [
        "--name", container_name(workload, epoch),
        "--label", f"fleet.workload={workload}",
        "--label", f"fleet.epoch={epoch}",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,size=256m",
        "--security-opt", "no-new-privileges",
        "--user", f"{uid}:{uid}",
    ]
    if group_add is not None:
        args += ["--group-add", str(group_add)]
    if run.get("memory_mb"):
        args += ["--memory", f"{int(run['memory_mb'])}m", "--memory-swap", "-1"]
    if run.get("cpus"):
        args += ["--cpus", str(run["cpus"])]
    args += ["--stop-timeout", str(int(run.get("stop_timeout_s") or 15))]
    args += ["--network", str(run.get("network") or "bridge")]
    if run.get("uts_host"):
        args += ["--uts", "host"]
    args += ["--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=3"]
    args += ["-v", f"{scratch}:/scratch"]
    if run.get("state_volume") and state:
        args += ["-v", f"{state}:/state"]
    args += ["-v", f"{secrets}:/run/fleet/secrets:ro"]
    env = dict(run.get("env") or {})
    env.pop(TOKEN_ENV, None)
    if run.get("nice") and "FLEET_NICE" not in env:
        env["FLEET_NICE"] = str(int(run["nice"]))
    for key in sorted(env):
        args += ["-e", f"{key}={env[key]}"]
    args += ["-e", TOKEN_ENV]
    args.append(str(run["image"]))
    return args
