"""Worker state directory, worker.conf, the status file and the reboot trigger.

status.json is rewritten after every heartbeat, so it lives in the runtime directory
(FLEET_RUN_DIR, /run/fleet under systemd: tmpfs, no flash wear). Without that variable
it falls back to /run/fleet when that exists next to the default state dir (so
`python3 -m fleet.worker status` run by hand as root finds it), else to the state dir
itself (tests, installs whose unit predates the runtime dir)."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

DEFAULT_STATE_DIR = "/var/lib/fleet"
DEFAULT_RUN_DIR = "/run/fleet"
REBOOT_TRIGGER_ENV = "FLEET_REBOOT_TRIGGER"
DONE_SUFFIX = ".done"
CONF_NAME = "worker.conf"
STATUS_NAME = "status.json"
PENDING_POSTS_NAME = "pending_posts.json"
CACHE_DIR_NAME = "cache"
GAMES_CACHE_NAME = "games.json"
GAMES_ETAG_NAME = "games.etag"
PRICES_CACHE_PREFIX = "prices-"


class ConfMissing(Exception):
    """worker.conf does not exist or is unusable."""


def state_dir() -> str:
    """State directory from FLEET_STATE_DIR, default /var/lib/fleet."""
    return os.environ.get("FLEET_STATE_DIR") or DEFAULT_STATE_DIR


def conf_path(directory: str) -> str:
    return os.path.join(directory, CONF_NAME)


def run_dir(directory: str) -> str:
    """Where per-heartbeat files go: FLEET_RUN_DIR, /run/fleet for the default state
    dir when it exists, else the state dir."""
    env = os.environ.get("FLEET_RUN_DIR")
    if env:
        return env
    if os.path.abspath(directory) == DEFAULT_STATE_DIR and os.path.isdir(DEFAULT_RUN_DIR):
        return DEFAULT_RUN_DIR
    return directory


def status_path(directory: str) -> str:
    return os.path.join(run_dir(directory), STATUS_NAME)


def remove_legacy_status(directory: str) -> None:
    """Delete a status.json an older version left in the state dir once status lives in
    the runtime dir, so nobody reads a stale copy (best effort)."""
    if os.path.abspath(run_dir(directory)) == os.path.abspath(directory):
        return
    try:
        os.unlink(os.path.join(directory, STATUS_NAME))
    except OSError:
        pass


def reboot_trigger() -> str | None:
    """The file whose creation makes the root fleet-reboot.path unit reboot the machine
    (set by the unit install_worker.sh writes); None when this install cannot reboot."""
    return os.environ.get(REBOOT_TRIGGER_ENV) or None


def trigger_writable(path: str) -> bool:
    """True when the agent can create the trigger file (its directory is writable)."""
    return os.access(os.path.dirname(path) or ".", os.W_OK)


def read_reboot_trigger(path: str) -> str | None:
    """The reboot request id written to a trigger file, or None."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def settle_reboot_trigger(path: str) -> tuple[str | None, bool]:
    """At agent start: (id of the last handled reboot request, whether a trigger was
    left over). The trigger lives on tmpfs, so one still present means its reboot never
    happened. It is moved to <path>.done: a trigger left in place would reboot the
    machine whenever fleet-reboot.path starts again (install.sh re-run, enable --now),
    and the .done copy keeps the id so the host's still-pending request is ignored
    across agent restarts until the host gives up on it."""
    left = read_reboot_trigger(path)
    leftover = os.path.lexists(path)
    if leftover:
        try:
            os.replace(path, path + DONE_SUFFIX)
        except OSError:
            pass
    return left or read_reboot_trigger(path + DONE_SUFFIX), leftover


def write_reboot_trigger(path: str, request_id: str) -> bool:
    """Create the trigger file (atomically, by rename, which fleet-reboot.path sees)
    holding the request id; False when it could not be written."""
    try:
        _write_text_atomic(path, request_id + "\n", 0o644)
    except OSError:
        return False
    return True


def app_dir(directory: str) -> str:
    return os.path.join(directory, "app")


def pending_posts_path(directory: str) -> str:
    return os.path.join(directory, PENDING_POSTS_NAME)


def cache_dir(directory: str) -> str:
    return os.path.join(directory, CACHE_DIR_NAME)


def _games_name(name: str, minutes: int | None) -> str:
    if minutes is None:
        return name
    stem, ext = os.path.splitext(name)
    return f"{stem}.d{int(minutes)}{ext}"


def games_cache_path(directory: str, minutes: int | None = None) -> str:
    """<state>/cache/games.json: the games feed handed to runners as games_path. A feed
    fetched with an explicit injury cutoff (a snapshot backtest) is games.d<N>.json, so
    different cutoffs never share a cache."""
    return os.path.join(cache_dir(directory), _games_name(GAMES_CACHE_NAME, minutes))


def games_etag_path(directory: str, minutes: int | None = None) -> str:
    """<state>/cache/games.etag (games.d<N>.etag): the ETag of that cached feed."""
    return os.path.join(cache_dir(directory), _games_name(GAMES_ETAG_NAME, minutes))


def _platform_slug(platform: str) -> str:
    slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in str(platform))
    return slug or "default"


def prices_cache_path(directory: str, platform: str) -> str:
    """<state>/cache/prices-<platform>.json: the recorded prices of one platform
    (GET /api/v1/data/prices), handed to snapshot backtests as prices_path."""
    return os.path.join(cache_dir(directory), f"{PRICES_CACHE_PREFIX}{_platform_slug(platform)}.json")


def prices_etag_path(directory: str, platform: str) -> str:
    """<state>/cache/prices-<platform>.etag: the ETag of that cached prices file."""
    return os.path.join(cache_dir(directory), f"{PRICES_CACHE_PREFIX}{_platform_slug(platform)}.etag")


def _write_private_json(path: str, data: Any, mode: int) -> None:
    """Write JSON atomically with the given file mode."""
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


def _write_text_atomic(path: str, text: str, mode: int) -> None:
    """Write text atomically with the given file mode."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_conf(directory: str) -> dict[str, Any]:
    """Load worker.conf; raise ConfMissing when absent or incomplete."""
    path = conf_path(directory)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfMissing(f"{path}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfMissing(f"{path}: not a JSON object")
    for key in ("host_url", "worker_id", "worker_token"):
        if not data.get(key):
            raise ConfMissing(f"{path}: missing {key}")
    data["host_url"] = str(data["host_url"]).rstrip("/")
    return data


def save_conf(directory: str, conf: dict[str, Any]) -> None:
    """Write worker.conf with mode 0600."""
    _write_private_json(conf_path(directory), conf, 0o600)


def save_status(directory: str, status: dict[str, Any]) -> None:
    """Write status.json (best effort; errors are swallowed)."""
    try:
        _write_private_json(status_path(directory), status, 0o644)
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
    token = str(out.get("worker_token", ""))
    out["worker_token"] = (token[:4] + "..." + token[-2:]) if len(token) > 8 else "***"
    return out


def save_pending_posts(directory: str, posts: list[dict[str, Any]]) -> None:
    """Persist unsent /complete and /fail calls (best effort) so a crash does not lose them."""
    path = pending_posts_path(directory)
    try:
        if not posts:
            if os.path.exists(path):
                os.unlink(path)
            return
        _write_private_json(path, posts, 0o600)
    except OSError:
        pass


def write_json_atomic(path: str, data: Any, mode: int = 0o644) -> None:
    """Public atomic JSON writer (the games cache uses it)."""
    _write_private_json(path, data, mode)


def load_pending_posts(directory: str) -> list[dict[str, Any]]:
    """Unsent posts saved by an earlier run, or an empty list."""
    try:
        with open(pending_posts_path(directory), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, dict) and p.get("path") and isinstance(p.get("body"), dict)]
