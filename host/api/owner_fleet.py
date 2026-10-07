"""Owner routes for the 3D fleet page: reboot a worker and the event feed
(docs/FLEET_UI_CONTRACT.md, docs/PROTOCOL.md "Fleet UI additions")."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query

from host import fleet_events, reboot
from host.api.deps import DB, require_owner
from host.api.serialize import jsonable

router = APIRouter(prefix="/api", tags=["owner"], dependencies=[Depends(require_owner)])


@router.post("/workers/{worker_id}/reboot")
def post_reboot(
    worker_id: str,
    actor: str = Depends(require_owner),
    conn: psycopg.Connection = DB,
) -> dict[str, Any]:
    """Ask a worker to reboot: {"worker_id", "reboot_id", "requested_at"}. A pending
    request returns the same id; 404 unknown worker, 409 offline or cannot reboot."""
    return jsonable(reboot.request_reboot(conn, worker_id, actor))


@router.get("/fleet/events")
def get_fleet_events(
    since: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=fleet_events.DEFAULT_LIMIT, ge=1, le=fleet_events.MAX_LIMIT),
    conn: psycopg.Connection = DB,
) -> dict[str, Any]:
    """Newest first; with `since` every event at or after it, capped at `limit`."""
    return jsonable(fleet_events.fleet_events(conn, fleet_events.parse_since(since), limit))
