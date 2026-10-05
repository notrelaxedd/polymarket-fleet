"""Runner context for the batch job kinds (docs/PROTOCOL.md, step 3).

Before a backtest, model_search, train or validate runner starts, the agent refreshes the games
cache (<state>/cache/games.json, with its ETag in games.etag) from
GET /api/v1/data/games and fetches the model named by params.model_id from
GET /api/v1/models/{id}. The result is job["context"] =
{"games_path": <cache file>, "model": <model dict or None>}, which the runner child
merges into the job's params as params["_context"]. An object body is cached whole
(games, team_game_stats and the injury cutoff it states), so fleet.sim.data.load_games
attaches each game's team stats; a bare list (an older host) is cached as it is.

A snapshot backtest fetches the feed with ?decision_minutes=<its
decision_minutes_before_kickoff> into games.d<N>.json (games.d<N>.etag), so its injury
signals are cut at its own decision time whatever the setting says now and different
cutoffs never share a cache. A feed stating another cutoff, or none, fails the job;
the context then also carries "games_minutes", which the job checks again.

A games fetch that fails falls back to the cached file when there is one (with a
warning); without one, or when the model fetch fails, ContextError names the cause
and the agent fails the job.

A backtest with params.price_source "snapshots" (docs/ROBUSTNESS.md B1) also gets
"prices_path": the recorded prices of params.price_platform, refreshed the same way
(<state>/cache/prices-<platform>.json with its ETag, conditional
GET /api/v1/data/prices?since=...&platform=...). Platform sim without
params.allow_sim_prices is not fetched: the job itself refuses it.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlencode

from fleet.common import http
from fleet.sim.prices import DEFAULT_PLATFORM, SIM_PLATFORM, params_decision_minutes
from fleet.worker import config

log = logging.getLogger("fleet.context")

CONTEXT_KINDS = ("backtest", "model_search", "train", "validate")
GAMES_PATH = "/api/v1/data/games"
PRICES_PATH = "/api/v1/data/prices"
PRICES_SINCE = "2000-01-01"
STATED_MINUTES = "decision_minutes_before_kickoff"


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


def _cache_payload(body: Any, minutes: int | None) -> tuple[Any, int]:
    """(what to cache, row count) for a 200 body. An object keeps its team_game_stats
    and stated cutoff; with `minutes` it must state exactly that cutoff."""
    rows = _rows_from_body(body)
    if not isinstance(body, dict):
        return rows, len(rows)
    stated = body.get(STATED_MINUTES)
    if minutes is not None and stated != minutes:
        raise ContextError(f"GET {GAMES_PATH}: the feed cut injuries at {stated!r} minutes, the job needs {minutes}")
    stats = body.get("team_game_stats")
    payload: dict[str, Any] = {"games": rows, "count": len(rows),
                               "team_game_stats": stats if isinstance(stats, list) else []}
    if stated is not None:
        payload[STATED_MINUTES] = stated
    return payload, len(rows)


def write_games_cache(state_dir: str, rows: Any, etag: str | None, minutes: int | None = None) -> str:
    """Store the feed (a list of rows or the cached object) as <state>/cache/games.json
    (games.d<minutes>.json) and the etag next to it; returns the path."""
    path = config.games_cache_path(state_dir, minutes)
    os.makedirs(config.cache_dir(state_dir), exist_ok=True)
    config.write_json_atomic(path, rows)
    _write_etag(config.games_etag_path(state_dir, minutes), etag)
    return path


def refresh_games(host_url: str, token: str, state_dir: str, timeout: float, minutes: int | None = None) -> str:
    """Refresh the games cache with a conditional GET and return the cache file path;
    `minutes` asks for that injury cutoff (?decision_minutes=) in its own cache file.

    200: rewrite the file and etag. 304: keep the file. Any failure: keep the cached
    file when there is one (warning), else raise ContextError. A feed stating another
    cutoff than `minutes` always raises ContextError."""
    path = config.games_cache_path(state_dir, minutes)
    etag_path = config.games_etag_path(state_dir, minutes)
    cached = os.path.isfile(path)
    etag = _read_etag(etag_path) if cached else None
    url = host_url + GAMES_PATH
    if minutes is not None:
        url += "?" + urlencode({"decision_minutes": int(minutes)})
    try:
        resp = http.get_json_etag(url, token=token, etag=etag, timeout=timeout)
    except (http.HttpError, http.HttpConnectionError) as exc:
        if cached:
            log.warning("games refresh failed (%s); using the cached %s", exc, path)
            return path
        raise ContextError(f"games data unavailable and no cached copy at {path}: {exc}") from None
    if resp.status == 304:
        log.info("games cache unchanged (%s)", etag)
        return path
    payload, count = _cache_payload(resp.body, minutes)
    try:
        write_games_cache(state_dir, payload, resp.etag, minutes)
    except OSError as exc:
        if cached:
            log.warning("cannot rewrite the games cache (%s); using the existing %s", exc, path)
            return path
        raise ContextError(f"cannot write the games cache {path}: {exc}") from None
    log.info("games cache refreshed: %d rows, etag %s", count, resp.etag)
    return path


def refresh_prices(host_url: str, token: str, state_dir: str, platform: str, timeout: float) -> str:
    """Refresh one platform's prices cache with a conditional GET and return its path;
    same fallbacks as refresh_games."""
    path = config.prices_cache_path(state_dir, platform)
    etag_path = config.prices_etag_path(state_dir, platform)
    cached = os.path.isfile(path)
    etag = _read_etag(etag_path) if cached else None
    url = f"{host_url}{PRICES_PATH}?{urlencode({'since': PRICES_SINCE, 'platform': platform})}"
    try:
        resp = http.get_json_etag(url, token=token, etag=etag, timeout=timeout)
    except (http.HttpError, http.HttpConnectionError) as exc:
        if cached:
            log.warning("prices refresh failed (%s); using the cached %s", exc, path)
            return path
        raise ContextError(f"prices data unavailable and no cached copy at {path}: {exc}") from None
    if resp.status == 304:
        return path
    if not isinstance(resp.body, dict) or not isinstance(resp.body.get("markets"), list):
        raise ContextError(f"GET {PRICES_PATH}: unexpected body shape ({type(resp.body).__name__})")
    try:
        os.makedirs(config.cache_dir(state_dir), exist_ok=True)
        config.write_json_atomic(path, {"markets": resp.body["markets"], "count": len(resp.body["markets"])})
        _write_etag(etag_path, resp.etag)
    except OSError as exc:
        if cached:
            log.warning("cannot rewrite the prices cache (%s); using the existing %s", exc, path)
            return path
        raise ContextError(f"cannot write the prices cache {path}: {exc}") from None
    log.info("prices cache refreshed: %d markets on %s, etag %s", len(resp.body["markets"]), platform, resp.etag)
    return path


def snapshot_platform(job: dict[str, Any]) -> str | None:
    """The price platform a job needs fetched: a snapshot backtest's params.price_platform
    (sim only with params.allow_sim_prices); None for every other job."""
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    if job.get("kind") != "backtest" or params.get("price_source") != "snapshots":
        return None
    platform = str(params.get("price_platform") or DEFAULT_PLATFORM)
    if platform == SIM_PLATFORM and params.get("allow_sim_prices") is not True:
        return None
    return platform


def snapshot_minutes(job: dict[str, Any]) -> int | None:
    """The injury cutoff a snapshot backtest fetches the games feed with (the minutes its
    replay decides at); None for every other job."""
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    if job.get("kind") != "backtest" or params.get("price_source") != "snapshots":
        return None
    return params_decision_minutes(params)


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
    """{"games_path", "model"} for one job (plus "prices_path" and "games_minutes" for
    a snapshot backtest); ContextError when it cannot be built."""
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    minutes = snapshot_minutes(job)
    games_path = refresh_games(host_url, token, state_dir, data_timeout, minutes)
    model = None
    model_id = params.get("model_id")
    if model_id:
        model = fetch_model(host_url, token, str(model_id), timeout)
    out: dict[str, Any] = {"games_path": games_path, "model": model}
    if minutes is not None:
        out["games_minutes"] = minutes
    platform = snapshot_platform(job)
    if platform is not None:
        out["prices_path"] = refresh_prices(host_url, token, state_dir, platform, data_timeout)
    return out
