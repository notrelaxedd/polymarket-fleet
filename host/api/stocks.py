"""Stock routes (docs/ALPACA.md "Step 9", contract tools/workflows/step9-contract.txt):
worker routes under /api/v1 (bars, trade state, order requests, release) and owner JSON
routes under /api/stocks (owner login, worker-IP refusal and the Origin check, like the
owner trading routes)."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from host import auth
from host.api.data import _conditional
from host.api.deps import DB, bearer, require_owner
from host.api.serialize import jsonable
from host.money import MAX_CENTS
from host.settings import get_int_setting
from host.stocks import approve, assignments, models, views_api
from host.stocks.jobparams_stocks import create_stock_job

worker_router = APIRouter(prefix="/api/v1", tags=["stocks-worker"])
owner_router = APIRouter(prefix="/api/stocks", tags=["stocks-owner"], dependencies=[Depends(require_owner)])


class OrderEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    client_request_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=10)
    side: str = Field(max_length=4)
    qty: int = Field(ge=1, le=approve.MAX_QTY)
    ref_price_cents: int = Field(ge=1, le=approve.MAX_QTY * 100)
    rationale: str | None = Field(default=None, max_length=512)


class OrdersBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    job_id: str = Field(max_length=64)
    lease_token: str | None = Field(default=None, max_length=64)
    assignment_id: int | str
    session_date: str = Field(max_length=10)
    orders: list[OrderEntry] = Field(default_factory=list, max_length=approve.MAX_ORDERS)


class ReleaseBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    job_ids: list[str] = Field(default_factory=list, max_length=views_api.MAX_RELEASE)


class AssignmentBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model_id: int
    mode: str = "paper"
    bankroll_cents: int | None = Field(default=None, ge=1, le=MAX_CENTS)
    symbols: list[str] = Field(min_length=1, max_length=assignments.MAX_SYMBOLS)


class HaltBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(default="owner halt", max_length=200)


class JobBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    target: str | None = Field(default=None, max_length=64)


# ------------------------------------------------------------------ worker routes


@worker_router.get("/data/stock_bars")
def get_stock_bars(request: Request, token: str = Depends(bearer), conn: psycopg.Connection = DB) -> Response:
    """Every instrument's daily bars, New York dates, oldest first; 304 when the
    client's ETag still matches."""
    auth.worker_for_token(conn, token)
    return _conditional(request, views_api.bars_etag(conn), lambda: views_api.dumps_bars(conn))


@worker_router.get("/stock_trade/state")
def get_trade_state(
    job_id: str = Query(max_length=64), lease_token: str | None = Query(default=None, max_length=64),
    token: str = Depends(bearer), conn: psycopg.Connection = DB,
) -> dict[str, Any]:
    """One held stock_trade job's tick: kill, assignment, model, positions, open orders,
    broker, decision and settings (409 when the job is not leased by this worker, or a
    lease_token is sent that is not the job's)."""
    worker = auth.worker_for_token(conn, token)
    return jsonable(views_api.trade_state(conn, worker, job_id, lease_token))


@worker_router.post("/stock_orders/request")
def post_orders(body: OrdersBody, token: str = Depends(bearer), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Approve or reject one session's batch (200 either way, a reason code per order)."""
    worker = auth.worker_for_token(conn, token)
    return jsonable(approve.request_orders(conn, worker, body.model_dump()))


@worker_router.post("/stock_trade/release")
def post_release(body: ReleaseBody, token: str = Depends(bearer), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Before a role change away from trade: approved orders cancelled, jobs handed back."""
    worker = auth.worker_for_token(conn, token)
    return views_api.release_jobs(conn, worker, body.job_ids)


# ------------------------------------------------------------------ owner routes


@owner_router.get("/summary")
def get_summary(conn: psycopg.Connection = DB) -> dict[str, Any]:
    return jsonable(views_api.summary(conn))


@owner_router.get("/models")
def get_models(status: str | None = Query(default=None, max_length=16), conn: psycopg.Connection = DB) -> list[dict[str, Any]]:
    return jsonable(models.list_models(conn, status))


@owner_router.post("/models/{model_id}/retire")
def post_retire(model_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Retire (final); its active assignments are halted."""
    return jsonable(models.retire_model(conn, model_id, actor))


@owner_router.get("/assignments")
def get_assignments(status: str | None = Query(default=None, max_length=16), conn: psycopg.Connection = DB) -> list[dict[str, Any]]:
    return jsonable(assignments.list_assignments(conn, status))


@owner_router.post("/assignments", status_code=201)
def post_assignment(body: AssignmentBody, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Create a stock assignment (its own cash, a stock_trade job); bankroll defaults to
    stock_default_bankroll_cents."""
    bankroll = body.bankroll_cents
    if bankroll is None:
        bankroll = get_int_setting(conn, "stock_default_bankroll_cents", 1_000_000)
    return jsonable(assignments.create_assignment(conn, body.model_id, body.mode, bankroll, body.symbols, actor))


@owner_router.post("/assignments/{assignment_id}/halt")
def post_halt(
    assignment_id: str, body: HaltBody | None = None, actor: str = Depends(require_owner), conn: psycopg.Connection = DB,
) -> dict[str, Any]:
    reason = body.reason if body is not None else "owner halt"
    return jsonable(assignments.halt_assignment(conn, assignment_id, reason, actor))


@owner_router.post("/assignments/{assignment_id}/resume")
def post_resume(assignment_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    return jsonable(assignments.resume_assignment(conn, assignment_id, actor))


@owner_router.post("/assignments/{assignment_id}/close")
def post_close(assignment_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    return jsonable(assignments.close_assignment(conn, assignment_id, actor))


@owner_router.post("/assignments/{assignment_id}/liquidate")
def post_liquidate(assignment_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Sell all of a halted assignment (market on close), so it can be closed."""
    from host.stocks.liquidate import liquidate_assignment

    return jsonable(liquidate_assignment(conn, assignment_id, actor))


@owner_router.post("/jobs", status_code=201)
def post_job(body: JobBody, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Create a stock_search, stock_backtest or stock_validate job with host-filled params."""
    return jsonable(create_stock_job(conn, body.kind, body.params, actor, body.target))
