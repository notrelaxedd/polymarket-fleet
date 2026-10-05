"""Worker feed of the in-game training rows: GET /api/v1/data/pbp?seasons=A-B.

The body is gzip-compressed JSON lines, one `pbp_rows` row per line in the contract's
field order, served as Content-Type application/x-ndjson+gzip without a
Content-Encoding header, so urllib on the worker stores it as is (pbp.jsonl.gz) and
decompresses it lazily. The ETag is `<count>-<max season>-<stamp>` over the selected
seasons, the stamp being a content checksum of those rows: it holds across an
idempotent re-ingest and moves whenever a row is added or changed. A built body is kept
per app (the last two selections) so workers polling the same feed cost one checksum
query, not a rebuild; rows stream from a server-side cursor into the compressor.
"""
from __future__ import annotations

import gzip
import io
import threading

import psycopg
from fastapi import APIRouter, Depends, Request, Response
from psycopg.rows import tuple_row

from host import auth, pbp_rows
from host.api.data import etag_matches
from host.api.deps import DB, bearer

MEDIA_TYPE = "application/x-ndjson+gzip"
CACHE_ENTRIES = 2
FETCH_ROWS = 5000
FLOAT_DIGITS = 6
FLOAT_COLUMNS = ("home_win", "pregame_p_home", "vegas_wp")
STAMP_MASK = (1 << 64) - 1

router = APIRouter(prefix="/api/v1/data", tags=["data"])
_build_lock = threading.Lock()


def feed_etag(conn: psycopg.Connection, first: int, last: int) -> str:
    """`<count>-<max season>-<checksum>` of the rows of seasons first..last."""
    row = conn.execute(
        "SELECT count(*) AS n, max(season) AS s, coalesce(sum(hashtextextended(p::text, 0)), 0) AS h"
        " FROM pbp_rows p WHERE season BETWEEN %s AND %s",
        (first, last),
    ).fetchone()
    return f"{row['n']}-{row['s'] or 0}-{int(row['h']) & STAMP_MASK:016x}"


def _select(first: int, last: int) -> tuple[str, tuple[int, int]]:
    """The feed query: one compact JSON object per row (row_to_json keeps the column
    order), reals rounded to 6 digits, rows in game then play order."""
    columns = ", ".join(
        f"round({name}::numeric, {FLOAT_DIGITS})::float8 AS {name}" if name in FLOAT_COLUMNS else name
        for name in pbp_rows.COLUMNS
    )
    sql = (f"SELECT row_to_json(t)::text AS line FROM (SELECT {columns} FROM pbp_rows"
           " WHERE season BETWEEN %s AND %s ORDER BY game_id, length(play_id), play_id) t")
    return sql, (first, last)


def build_body(conn: psycopg.Connection, first: int, last: int) -> bytes:
    """The gzip JSON-lines body (deterministic bytes for the same rows)."""
    buffer = io.BytesIO()
    sql, params = _select(first, last)
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=6, mtime=0) as out:
        with conn.cursor(name="pbp_feed", row_factory=tuple_row) as cursor:
            cursor.execute(sql, params)
            while True:
                rows = cursor.fetchmany(FETCH_ROWS)
                if not rows:
                    break
                out.write(("\n".join(row[0] for row in rows) + "\n").encode("utf-8"))
    return buffer.getvalue()


def _cached_body(request: Request, conn: psycopg.Connection, key: tuple[int, int, str]) -> bytes:
    """The body for key from this app's cache, built (once, under a lock) when missing."""
    with _build_lock:
        cache: dict[tuple[int, int, str], bytes] = getattr(request.app.state, "pbp_feed_cache", {})
        body = cache.get(key)
        if body is None:
            body = build_body(conn, key[0], key[1])
            cache = {k: v for k, v in cache.items() if k[:2] != key[:2]}
            while len(cache) >= CACHE_ENTRIES:
                cache.pop(next(iter(cache)))
            cache[key] = body
            request.app.state.pbp_feed_cache = cache
    return body


@router.get("/pbp")
def get_pbp(
    request: Request, seasons: str | None = None, token: str = Depends(bearer), conn: psycopg.Connection = DB
) -> Response:
    """Every pbp row of the seasons (all when omitted); 304 when the client's ETag still matches."""
    auth.worker_for_token(conn, token)
    first, last = pbp_rows.season_range(seasons) if seasons else (pbp_rows.FIRST_SEASON, pbp_rows.LAST_SEASON)
    etag = feed_etag(conn, first, last)
    headers = {"ETag": f'"{etag}"', "Cache-Control": "no-cache"}
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    body = _cached_body(request, conn, (first, last, etag))
    return Response(body, media_type=MEDIA_TYPE, headers=headers)
