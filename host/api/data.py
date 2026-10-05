"""Data routes: the worker games and prices feeds with ETag, and the owner's refresh."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query, Request, Response

from host import auth, games_feed, nflverse, prices_feed
from host.api.deps import DB, bearer, require_owner
from host.api.serialize import jsonable
from host.data_refresh import refresh_now
from host.settings import get_setting

worker_router = APIRouter(prefix="/api/v1/data", tags=["data"])
owner_router = APIRouter(prefix="/api/data", tags=["data"], dependencies=[Depends(require_owner)])


def etag_matches(header: str | None, etag: str) -> bool:
    """If-None-Match holds the ETag (quoted or bare, possibly a list, or *)."""
    if not header:
        return False
    candidates = [part.strip() for part in header.split(",")]
    for candidate in candidates:
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate.strip('"') == etag:
            return True
    return False


def _conditional(request: Request, etag: str, body: Any) -> Response:
    """304 when If-None-Match holds the ETag, else the body from `body()`."""
    quoted = f'"{etag}"'
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": quoted})
    return Response(body(), media_type="application/json", headers={"ETag": quoted, "Cache-Control": "no-cache"})


@worker_router.get("/games")
def get_games(
    request: Request, token: str = Depends(bearer), conn: psycopg.Connection = DB
) -> Response:
    """Every game in kickoff order with its signals, plus the team-game stats; 304 when
    the client's ETag still matches."""
    auth.worker_for_token(conn, token)
    return _conditional(request, games_feed.feed_etag(conn), lambda: games_feed.dumps_feed(conn))


@worker_router.get("/prices")
def get_prices(
    request: Request,
    since: str | None = Query(None),
    platform: str | None = Query(None),
    token: str = Depends(bearer),
    conn: psycopg.Connection = DB,
) -> Response:
    """Recorded bars and depth of confirmed markets before kickoff (docs/ROBUSTNESS.md
    B1); platform sim only when asked by name; 304 when the client's ETag matches."""
    auth.worker_for_token(conn, token)
    when, name = prices_feed.parse_since(since), prices_feed.check_platform(platform)
    etag = prices_feed.prices_etag(conn, when, name)
    return _conditional(request, etag, lambda: prices_feed.dumps_prices(conn, when, name))


@owner_router.post("/refresh")
def post_refresh(conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Fetch nflverse games.csv now (outside the request transaction) and upsert it."""
    url = get_setting(conn, "nflverse_url", nflverse.DEFAULT_URL)
    return jsonable(refresh_now(conn, str(url)))
