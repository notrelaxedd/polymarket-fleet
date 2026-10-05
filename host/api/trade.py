"""Worker trade routes: state, order request, order cancel and the release handshake."""
from __future__ import annotations

import time
from typing import Any, Literal

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from host import auth
from host.api.deps import DB, bearer
from host.api.limits import MAX_JOB_ENTRIES
from host.api.serialize import jsonable
from host.errors import Conflict, NotFound
from host import kill
from host.kill import cancel_active_orders
from host.leases import as_uuid, release
from host.trading import assignments, limits, orders

router = APIRouter(prefix="/api/v1", tags=["trade"])

RELEASE_WAIT_SECONDS = 3.0
RELEASE_POLL_SECONDS = 0.1


class OrderRequestBody(BaseModel):
    """POST /api/v1/orders/request. Limit or cost fields the worker adds are ignored.
    `order_side` is "buy" (default) or "sell" (docs/TRADING.md "Selling"); `side`, the
    team side the worker may send, is ignored as before."""

    model_config = ConfigDict(extra="ignore")
    client_request_id: str = Field(min_length=1, max_length=64)
    job_id: str = Field(max_length=64)
    lease_token: str = Field(max_length=64)
    assignment_id: str = Field(max_length=64)
    market_id: str = Field(max_length=64)
    snapshot_id: int | None = None
    price: float = Field(ge=0, le=1, allow_inf_nan=False)
    size: int = Field(ge=1, le=limits.MAX_SIZE)
    my_p: float | None = Field(default=None, allow_inf_nan=False)
    market_p: float | None = Field(default=None, allow_inf_nan=False)
    edge: float | None = Field(default=None, allow_inf_nan=False)
    rationale: str | None = Field(default=None, max_length=512)
    order_side: Literal["buy", "sell"] = "buy"


class ReleaseEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(max_length=64)
    lease_token: str = Field(max_length=64)


class ReleaseBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    jobs: list[ReleaseEntry] = Field(default_factory=list, max_length=MAX_JOB_ENTRIES)


@router.get("/trade/state")
def trade_state(token: str = Depends(bearer), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Everything a trade worker needs for one tick, per trade job it holds."""
    worker = auth.worker_for_token(conn, token)
    return jsonable(assignments.trade_state(conn, worker["id"]))


@router.post("/orders/request")
def request_order(body: OrderRequestBody, token: str = Depends(bearer), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Approve or reject one proposal (200 either way; see docs/TRADING.md "Approval")."""
    worker = auth.worker_for_token(conn, token)
    return jsonable(limits.approve_order(conn, worker, body.model_dump()))


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str, token: str = Depends(bearer), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Cancel the worker's own order (paper at once; live becomes cancel_requested)."""
    worker = auth.worker_for_token(conn, token)
    oid = as_uuid(order_id)
    if oid is None:
        raise NotFound("order not found")
    order = orders.get_order(conn, oid)
    if order["worker_id"] != worker["id"]:
        raise Conflict("order belongs to another worker")
    return {"status": orders.cancel_order(conn, oid, worker["id"], "worker cancel")}


def _owned_jobs(conn: psycopg.Connection, worker_id: str, entries: list[ReleaseEntry]) -> list[dict[str, Any]]:
    """The listed trade jobs this worker holds with matching tokens (others are ignored)."""
    out = []
    for entry in entries:
        jid, tok = as_uuid(entry.id), as_uuid(entry.lease_token)
        if jid is None or tok is None:
            continue
        job = conn.execute(
            """
            SELECT * FROM jobs WHERE id = %s AND lease_token = %s AND lease_worker_id = %s
               AND kind = 'trade' AND status IN ('leased', 'cancel_requested')
            """,
            (jid, tok, worker_id),
        ).fetchone()
        if job is not None:
            out.append(dict(job))
    return out


def _wait_for_cancels(conn: psycopg.Connection, ids: list[Any], deadline: float) -> int:
    """Poll (after the commit) until no listed order is still cancel_requested; the count left."""
    pending = len(ids)
    while ids and pending:
        pending = int(conn.execute(
            "SELECT count(*) AS n FROM orders WHERE id = ANY(%s::uuid[]) AND status = 'cancel_requested'",
            ([str(i) for i in ids],),
        ).fetchone()["n"])
        conn.commit()
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(RELEASE_POLL_SECONDS)
    return pending


@router.post("/trade/release")
def release_trade(body: ReleaseBody, token: str = Depends(bearer), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Before a role change away from trade: cancel the worker's open orders for those
    assignments (paper at once; live cancel_requested, waited for up to 3 s) and hand
    the trade jobs back to the queue with reason `drain`."""
    worker = auth.worker_for_token(conn, token)
    # Under both approval locks: an approval that read these jobs as leased cannot
    # commit after the release has handed them back to the queue.
    kill.approval_locks(conn)
    jobs = _owned_jobs(conn, worker["id"], body.jobs)
    assignment_ids = [j["params"].get("assignment_id") for j in jobs if isinstance(j["params"], dict)]
    assignment_ids = [a for a in assignment_ids if as_uuid(a) is not None]
    result = {"cancelled": 0, "requested": []}
    if assignment_ids:
        result = cancel_active_orders(conn, worker["id"], "drain", assignment_ids=assignment_ids, worker_id=worker["id"])
    released = []
    for job in jobs:
        if release(conn, job["id"], job["lease_token"], None, None, worker["id"], "drain") is not None:
            released.append(str(job["id"]))
    conn.commit()
    pending = _wait_for_cancels(conn, result["requested"], time.monotonic() + RELEASE_WAIT_SECONDS)
    return {"cancelled": result["cancelled"], "pending": pending, "released": released}
