"""Worker cache of the in-game training rows (contract section 3).

refresh_pbp() downloads GET /api/v1/data/pbp?seasons=A-B into <state>/cache/pbp.jsonl.gz
with a conditional GET: the stored ETag (pbp.etag, JSON {"seasons", "etag"}) is sent as
If-None-Match only when the cached file holds the same season selection, and a 304
keeps the file. The body is the host's gzip-compressed JSON lines (Content-Type
application/x-ndjson+gzip, no Content-Encoding), streamed to a temporary file, checked
for the gzip magic and moved into place, so a half download never replaces a good
cache. A failed fetch falls back to the cached file when there is one (warning), else
raises PbpCacheError.

iter_rows() reads the file lazily (gzip + one JSON object per line), so a season range
is never held in memory; rows_factory() gives the search a callable it can re-iterate.
run_ingame_search_job() is the runner entry for the in-game search: it reads the cache
path from params["_context"]["pbp_path"].
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator

from fleet.common import http
from fleet.worker import config

log = logging.getLogger("fleet.pbp_cache")

PBP_PATH = "/api/v1/data/pbp"
PBP_FILE = "pbp.jsonl.gz"
PBP_ETAG_FILE = "pbp.etag"
DEFAULT_SEASONS = (2012, 2025)
GZIP_MAGIC = b"\x1f\x8b"
CHUNK = 1 << 16
DEFAULT_TIMEOUT = 300.0

# fetch(url, headers, dest_path) -> (status, etag): writes the body to dest_path on 200
Fetch = Callable[[str, dict[str, str], str], tuple[int, str | None]]


class PbpCacheError(Exception):
    """The rows are unavailable and there is no cached copy."""


def pbp_path(state_dir: str) -> str:
    return os.path.join(config.cache_dir(state_dir), PBP_FILE)


def pbp_etag_path(state_dir: str) -> str:
    return os.path.join(config.cache_dir(state_dir), PBP_ETAG_FILE)


def seasons_text(seasons: tuple[int, int] | list[int]) -> str:
    return f"{int(seasons[0])}-{int(seasons[1])}"


def http_fetch(timeout: float = DEFAULT_TIMEOUT) -> Fetch:
    """The default fetch: urllib without environment proxies, streaming the body."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def fetch(url: str, headers: dict[str, str], dest: str) -> tuple[int, str | None]:
        req = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with opener.open(req, timeout=timeout) as resp, open(dest, "wb") as out:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                return resp.status, resp.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, exc.headers.get("ETag")
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise http.HttpError(exc.code, detail, url) from None
        except (urllib.error.URLError, OSError) as exc:
            raise http.HttpConnectionError(f"GET {url}: {exc}") from None

    return fetch


def _read_etag(path: str, selection: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("seasons") != selection:
        return None
    etag = data.get("etag")
    return etag if isinstance(etag, str) and etag else None


def _write_etag(path: str, selection: str, etag: str | None) -> None:
    config.write_json_atomic(path, {"seasons": selection, "etag": etag})


def _is_gzip(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == GZIP_MAGIC
    except OSError:
        return False


def refresh_pbp(host_url: str, token: str, state_dir: str, seasons: tuple[int, int] | list[int] = DEFAULT_SEASONS,
                fetch: Fetch | None = None, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Refresh the cache with a conditional GET and return the cache file path."""
    fetch = fetch or http_fetch(timeout)
    path = pbp_path(state_dir)
    etag_path = pbp_etag_path(state_dir)
    selection = seasons_text(seasons)
    cached = os.path.isfile(path)
    etag = _read_etag(etag_path, selection) if cached else None
    headers = {"Accept": "application/x-ndjson+gzip", "User-Agent": "fleet-worker"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if etag:
        headers["If-None-Match"] = etag
    os.makedirs(config.cache_dir(state_dir), exist_ok=True)
    tmp = path + ".part"
    url = f"{host_url}{PBP_PATH}?seasons={selection}"
    try:
        status, new_etag = fetch(url, headers, tmp)
        if status == 304:
            log.info("pbp cache unchanged (%s)", etag)
            return path
        if status != 200 or not _is_gzip(tmp):
            raise http.HttpConnectionError(f"GET {url}: status {status} without a gzip body")
        os.replace(tmp, path)
        _write_etag(etag_path, selection, new_etag)
    except (http.HttpError, http.HttpConnectionError, OSError) as exc:
        _unlink(tmp)
        if cached:
            log.warning("pbp refresh failed (%s); using the cached %s", exc, path)
            return path
        raise PbpCacheError(f"pbp rows unavailable and no cached copy at {path}: {exc}") from None
    log.info("pbp cache refreshed (%s, etag %s)", selection, new_etag)
    return path


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def iter_rows(path: str, seasons: tuple[int | None, int | None] | list[int | None] | None = None) -> Iterator[dict[str, Any]]:
    """The rows of the cache file one at a time (blank lines skipped), optionally only
    the seasons first..last (inclusive, None = open)."""
    first, last = (seasons or (None, None))
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            season = row.get("season")
            if first is not None and (season is None or season < first):
                continue
            if last is not None and (season is None or season > last):
                continue
            yield row


def rows_factory(path: str, seasons: tuple[int | None, int | None] | list[int | None] | None = None
                 ) -> Callable[[], Iterator[dict[str, Any]]]:
    """A callable that starts a fresh lazy pass over the file on every call."""
    return lambda: iter_rows(path, seasons)


def run_ingame_search_job(params: dict[str, Any], checkpoint: dict[str, Any] | None,
                          emit: Callable[[dict[str, Any], float], None],
                          should_stop: Callable[[], bool]) -> dict[str, Any]:
    """The in-game search over the cached rows (params["_context"]["pbp_path"])."""
    from fleet.sim.ingame import search_from_params

    ctx = params.get("_context") if isinstance(params.get("_context"), dict) else {}
    path = ctx.get("pbp_path")
    if not path:
        raise ValueError("job context has no pbp_path")
    return search_from_params(params, rows_factory(str(path)), emit, should_stop, checkpoint)
