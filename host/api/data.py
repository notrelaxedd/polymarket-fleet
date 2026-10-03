"""Games data routes: the worker feed with ETag and the owner's refresh."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request, Response

from host import auth, nflverse
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


@worker_router.get("/games")
def get_games(
    request: Request, token: str = Depends(bearer), conn: psycopg.Connection = DB
) -> Response:
    """Every game in kickoff order; 304 when the client's ETag still matches."""
    auth.worker_for_token(conn, token)
    etag = nflverse.games_etag(conn)
    quoted = f'"{etag}"'
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": quoted})
    body = nflverse.dumps_games(nflverse.worker_games(conn))
    return Response(body, media_type="application/json", headers={"ETag": quoted, "Cache-Control": "no-cache"})


@owner_router.post("/refresh")
def post_refresh(conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Fetch nflverse games.csv now (outside the request transaction) and upsert it."""
    url = get_setting(conn, "nflverse_url", nflverse.DEFAULT_URL)
    return jsonable(refresh_now(conn, str(url)))
