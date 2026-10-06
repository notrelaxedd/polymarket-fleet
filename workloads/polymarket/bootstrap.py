"""Container entrypoint of the polymarket workload (docs/workloads-design.md section 7).

Standard library only, Python 3.11+. What it does, in order:
1. Lowers its priority to FLEET_NICE (default 5, systemd `Nice=5` on a native worker).
2. When /state/app/current does not hold a usable `fleet` package: downloads
   $FLEET_HOST_URL/dl/version and /dl/worker.tar.gz, checks the sha256 and every tar
   member with the installer's rules (deploy/install_worker.sh, fleet/worker/update.py),
   extracts to /state/app/.staging, renames it to /state/app/<version> and atomically
   points /state/app/current at it. An existing current is reused as is: from then on
   the agent's own self-update (exit 75) owns app/.
3. When /state/worker.conf is missing: enrolls with
   `python3 -m fleet.worker enroll --host $FLEET_HOST_URL --name <hostname>`, the token
   passed in the environment from /run/fleet/secrets/FLEET_ENROLL_TOKEN; no token file
   means exit 78 (the supervisor does not restart it, like RestartPreventExitStatus=78).
4. execve `python3 -m fleet.worker run` with PYTHONPATH=/state/app/current and
   FLEET_STATE_DIR=/state, so the agent's exit codes reach the supervisor directly.

Env: FLEET_HOST_URL (required for steps 2 and 3), FLEET_STATE_DIR (default /state),
FLEET_SECRETS_DIR (default /run/fleet/secrets), FLEET_NICE (default 5). Proxies from
the environment are ignored, as in fleet/common/http.py: the host is on the tailnet.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from typing import Any, NoReturn

EXIT_FAILED = 1
EXIT_CONFIG = 78
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TOKEN_NAME = "FLEET_ENROLL_TOKEN"
DROP_FROM_AGENT_ENV = (TOKEN_NAME, "FLEET_RUN_TOKEN")
DEFAULT_NICE = 5
HTTP_TIMEOUT = 120.0

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class BootstrapError(Exception):
    """A step failed; the message says which and why. Exit 1 (the supervisor retries)."""


def log(message: str) -> None:
    print(f"bootstrap: {message}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------- settings


def state_dir(env: dict[str, str]) -> str:
    return env.get("FLEET_STATE_DIR") or "/state"


def secrets_dir(env: dict[str, str]) -> str:
    return env.get("FLEET_SECRETS_DIR") or "/run/fleet/secrets"


def host_url(env: dict[str, str]) -> str:
    url = (env.get("FLEET_HOST_URL") or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise BootstrapError(f"FLEET_HOST_URL must start with http:// or https://, got {url!r}")
    return url


def apply_nice(env: dict[str, str]) -> int:
    """Raise the niceness to FLEET_NICE (an absolute value, like systemd Nice=)."""
    try:
        target = max(0, min(19, int(env.get("FLEET_NICE") or DEFAULT_NICE)))
    except ValueError:
        target = DEFAULT_NICE
    current = os.nice(0)
    return os.nice(target - current) if target > current else current


# ------------------------------------------------------------------ download


def fetch(url: str, timeout: float = HTTP_TIMEOUT) -> bytes:
    try:
        with _opener.open(url, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise BootstrapError(f"GET {url}: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise BootstrapError(f"GET {url}: {exc}") from None


def remote_version(base: str) -> tuple[str, str]:
    """(code_version, sha256) from /dl/version, both validated."""
    try:
        info: Any = json.loads(fetch(base + "/dl/version", timeout=10.0))
    except ValueError as exc:
        raise BootstrapError(f"/dl/version is not JSON: {exc}") from None
    if not isinstance(info, dict):
        raise BootstrapError(f"/dl/version answered {info!r}")
    version, sha = str(info.get("code_version") or ""), str(info.get("sha256") or "").lower()
    if not VERSION_RE.match(version):
        raise BootstrapError(f"odd code_version from host: {version!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise BootstrapError(f"odd sha256 from host: {sha!r}")
    return version, sha


def check_member(member: tarfile.TarInfo) -> None:
    """The rules of fleet/worker/update.py and the installer's check_tarball."""
    name = member.name
    parts = name.split("/")
    if name.startswith(("/", "\\")) or any(p in ("", ".", "..") for p in parts):
        raise BootstrapError(f"unsafe path in tarball: {name}")
    if parts[0] != "fleet":
        raise BootstrapError(f"unexpected top-level entry in tarball: {name}")
    if not (member.isfile() or member.isdir()):
        raise BootstrapError(f"unsupported member type in tarball: {name}")
    if "__pycache__" in parts or name.endswith(".pyc"):
        raise BootstrapError(f"compiled file in tarball: {name}")


def extract(data: bytes, dest: str) -> None:
    """Validate every member first, then write plain files (0644) and dirs (0755) only:
    no owners, modes or links from the archive (tar --no-same-owner --no-same-permissions)."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
        members = tar.getmembers()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BootstrapError(f"bad tarball: {exc}") from None
    with tar:
        for member in members:
            check_member(member)
        if not any(m.name == "fleet/__init__.py" and m.isfile() for m in members):
            raise BootstrapError("tarball has no fleet/__init__.py")
        os.makedirs(dest, mode=0o755)
        for member in members:
            target = os.path.join(dest, member.name)
            if member.isdir():
                os.makedirs(target, mode=0o755, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                raise BootstrapError(f"unreadable member in tarball: {member.name}")
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o644)


def app_ready(app: str) -> bool:
    """True when app/current resolves to a directory holding fleet/__init__.py."""
    return os.path.isfile(os.path.join(app, "current", "fleet", "__init__.py"))


def point_current(app: str, version: str) -> None:
    tmp = os.path.join(app, "current.tmp")
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(version, tmp)
    os.replace(tmp, os.path.join(app, "current"))


def install_app(base: str, app: str) -> str:
    """Download, verify and install the host's worker code; returns the version."""
    version, expected = remote_version(base)
    data = fetch(base + "/dl/worker.tar.gz")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise BootstrapError(f"sha256 mismatch: expected {expected}, got {actual}")
    os.makedirs(app, mode=0o755, exist_ok=True)
    staging = os.path.join(app, ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        extract(data, staging)
        target = os.path.join(app, version)
        if os.path.lexists(target):
            shutil.rmtree(target)
        os.rename(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    point_current(app, version)
    try:
        os.unlink(os.path.join(app, "pending.json"))  # an installed version is not a self-update
    except FileNotFoundError:
        pass
    log(f"installed worker {version} ({len(data)} bytes); app/current -> {version}")
    return version


# -------------------------------------------------------------------- enroll


def read_token(directory: str) -> str | None:
    try:
        with open(os.path.join(directory, TOKEN_NAME), encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        return None
    return token or None


def agent_env(env: dict[str, str], state: str) -> dict[str, str]:
    out = {k: v for k, v in env.items() if k not in DROP_FROM_AGENT_ENV}
    out["PYTHONPATH"] = os.path.join(state, "app", "current")
    out["FLEET_STATE_DIR"] = state
    return out


def enroll(env: dict[str, str], state: str, base: str, token: str) -> None:
    """python3 -m fleet.worker enroll; the token travels in the environment only."""
    cmd = [sys.executable, "-m", "fleet.worker", "enroll", "--host", base, "--name", socket.gethostname()]
    child_env = dict(agent_env(env, state), **{TOKEN_NAME: token})
    result = subprocess.run(cmd, env=child_env, check=False)
    if result.returncode != 0:
        raise BootstrapError(f"enroll exited {result.returncode}")


def exec_agent(env: dict[str, str], state: str) -> NoReturn:
    argv = [sys.executable, "-m", "fleet.worker", "run"]
    os.execve(sys.executable, argv, agent_env(env, state))
    raise AssertionError("execve returned")  # pragma: no cover (only a patched execve returns)


# ---------------------------------------------------------------------- main


def main(env: dict[str, str] | None = None) -> int:
    env = dict(os.environ if env is None else env)
    state = state_dir(env)
    app = os.path.join(state, "app")
    try:
        apply_nice(env)
        if not app_ready(app):
            install_app(host_url(env), app)
        if not os.path.isfile(os.path.join(state, "worker.conf")):
            token = read_token(secrets_dir(env))
            if token is None:
                log(f"worker.conf is missing and there is no {TOKEN_NAME} secret; not starting (exit 78)")
                return EXIT_CONFIG
            enroll(env, state, host_url(env), token)
    except BootstrapError as exc:
        log(str(exc))
        return EXIT_FAILED
    exec_agent(env, state)


if __name__ == "__main__":
    sys.exit(main())
