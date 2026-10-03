"""Runner context for the batch job kinds (docs/PROTOCOL.md, step 3).

Before a backtest, model_search or train runner starts, the agent refreshes the games
cache (<state>/cache/games.json, with its ETag in games.etag) from
GET /api/v1/data/games and fetches the model named by params.model_id from
GET /api/v1/models/{id}. The result is job["context"] =
{"games_path": <cache file>, "model": <model dict or None>}, which the runner child
merges into the job's params as params["_context"].

A games fetch that fails falls back to the cached file when there is one (with a
warning); without one, or when the model fetch fails, ContextError names the cause
and the agent fails the job.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fleet.common import http
from fleet.worker import config

log = logging.getLogger("fleet.context")

CONTEXT_KINDS = ("backtest", "model_search", "train")
GAMES_PATH = "/api/v1/data/games"


class ContextError(Exception):
    """The context for a job cannot be built; the message is the job's error."""


def needs_context(kind: Any) -> bool:
    return isinstance(kind, str) and kind in CONTEXT_KINDS


def _read_etag(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        return None
    return value or None


def _write_etag(path: str, etag: str | None) -> None:
    if not etag:
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(etag + "\n")
    os.replace(tmp, path)


def _rows_from_body(body: Any) -> list[Any]:
    """Accept a bare list or an object carrying the rows under games/rows/items."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("games", "rows", "items"):
            if isinstance(body.get(key), list):
                return body[key]
    raise ContextError(f"GET {GAMES_PATH}: unexpected body shape ({type(body).__name__})")


def write_games_cache(state_dir: str, rows: list[Any], etag: str | None) -> str:
    """Store rows as <state>/cache/games.json and the etag next to it; returns the path."""
    path = config.games_cache_path(state_dir)
    os.makedirs(config.cache_dir(state_dir), exist_ok=True)
    config.write_json_atomic(path, rows)
    _write_etag(config.games_etag_path(state_dir), etag)
    return path


def refresh_games(host_url: str, token: str, state_dir: str, timeout: float) -> str:
    """Refresh the games cache with a conditional GET and return the cache file path.

    200: rewrite the file and etag. 304: keep the file. Any failure: keep the cached
    file when there is one (warning), else raise ContextError."""
    path = config.games_cache_path(state_dir)
    etag_path = config.games_etag_path(state_dir)
    cached = os.path.isfile(path)
    etag = _read_etag(etag_path) if cached else None
    try:
        resp = http.get_json_etag(host_url + GAMES_PATH, token=token, etag=etag, timeout=timeout)
    except (http.HttpError, http.HttpConnectionError) as exc:
        if cached:
            log.warning("games refresh failed (%s); using the cached %s", exc, path)
            return path
        raise ContextError(f"games data unavailable and no cached copy at {path}: {exc}") from None
    if resp.status == 304:
        log.info("games cache unchanged (%s)", etag)
        return path
    rows = _rows_from_body(resp.body)
    try:
        write_games_cache(state_dir, rows, resp.etag)
    except OSError as exc:
        if cached:
            log.warning("cannot rewrite the games cache (%s); using the existing %s", exc, path)
            return path
        raise ContextError(f"cannot write the games cache {path}: {exc}") from None
    log.info("games cache refreshed: %d rows, etag %s", len(rows), resp.etag)
    return path


def fetch_model(host_url: str, token: str, model_id: str, timeout: float) -> dict[str, Any]:
    """GET /api/v1/models/{id}; ContextError on any failure or a non-object answer."""
    url = f"{host_url}/api/v1/models/{model_id}"
    try:
        body = http.get_json(url, token=token, timeout=timeout)
    except (http.HttpError, http.HttpConnectionError) as exc:
        raise ContextError(f"model {model_id} unavailable: {exc}") from None
    if not isinstance(body, dict) or not body.get("id"):
        raise ContextError(f"model {model_id}: unexpected answer from the host")
    return body


def build_context(
    host_url: str,
    token: str,
    state_dir: str,
    job: dict[str, Any],
    timeout: float,
    data_timeout: float,
) -> dict[str, Any]:
    """{"games_path", "model"} for one job; ContextError when it cannot be built."""
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    games_path = refresh_games(host_url, token, state_dir, data_timeout)
    model = None
    model_id = params.get("model_id")
    if model_id:
        model = fetch_model(host_url, token, str(model_id), timeout)
    return {"games_path": games_path, "model": model}
