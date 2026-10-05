"""Owner trading routes: assignments, orders, fills, markets, exchange state, cancel-all."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from host import kill
from host.api.deps import DB, require_owner
from host.api.serialize import jsonable
from host.errors import Conflict
from host.money import MAX_CENTS
from host.settings import get_int_setting
from host.trading import assignments, assignments_ingame, views
from host.trading.positions import positions

router = APIRouter(prefix="/api", tags=["trading"], dependencies=[Depends(require_owner)])

EXCHANGE_MISSING = "exchange module not available"


class AssignmentBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    game_id: str = Field(max_length=64)
    model_id: str = Field(max_length=64)
    mode: str = "paper"
    bankroll_cents: int | None = Field(default=None, ge=0, le=MAX_CENTS)
    max_bet_cents: int | None = Field(default=None, ge=0, le=MAX_CENTS)
    ingame_model_id: str | None = Field(default=None, max_length=64)
    trade_ingame: bool | None = None


class IngameBody(BaseModel):
    """Only the fields sent are changed (an explicit null ingame_model_id clears it)."""
    model_config = ConfigDict(extra="ignore")
    ingame_model_id: str | None = Field(default=None, max_length=64)
    trade_ingame: bool | None = None


class HaltBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(default="owner halt", max_length=200)


class LinkBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    game_id: str = Field(max_length=64)
    side: str = Field(max_length=8)


class CancelAllBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mode: str | None = None


@router.get("/assignments")
def get_assignments(status: str | None = Query(default=None), conn: psycopg.Connection = DB) -> list[dict[str, Any]]:
    """Assignments with bankroll, game, model summary and open order count."""
    return jsonable(assignments.list_assignments(conn, status))


@router.post("/assignments", status_code=201)
def post_assignment(body: AssignmentBody, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Create an assignment (funds the bankroll, queues the trade job)."""
    bankroll = body.bankroll_cents
    if bankroll is None:
        bankroll = get_int_setting(conn, "default_bankroll_cents", 10_000)
    row = assignments.create_assignment(
        conn, body.game_id, body.model_id, body.mode, bankroll, actor, body.max_bet_cents,
        ingame_model_id=body.ingame_model_id, trade_ingame=body.trade_ingame,
    )
    return jsonable(row)


@router.post("/assignments/activate-paper")
def post_activate_paper(actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """After a kill reset: every halted paper assignment back to active."""
    return {"activated": assignments.activate_all_paper(conn, actor)}


@router.get("/assignments/{assignment_id}")
def get_assignment(assignment_id: str, conn: psycopg.Connection = DB) -> dict[str, Any]:
    """One assignment (the list row shape) with its orders, fills and positions."""
    row = assignments.get_assignment(conn, assignment_id)
    row = assignments.list_assignments(conn, assignment_id=row["id"])[0]
    row["orders"] = views.list_orders(conn, limit=200, assignment_id=row["id"])
    row["fills"] = views.list_fills(conn, limit=200, assignment_id=row["id"])
    row["positions"] = positions(conn, row["id"])
    return jsonable(row)


@router.post("/assignments/{assignment_id}/halt")
def post_halt(
    assignment_id: str, body: HaltBody | None = None, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> dict[str, Any]:
    """Halt: cancels the assignment's open orders, leaves the job leased."""
    reason = body.reason if body is not None else "owner halt"
    return jsonable(assignments.halt_assignment(conn, assignment_id, actor, reason))


@router.post("/assignments/{assignment_id}/activate")
def post_activate(assignment_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """halted -> active (refused under kill)."""
    return jsonable(assignments.activate_assignment(conn, assignment_id, actor))


@router.post("/assignments/{assignment_id}/ingame")
def post_ingame(
    assignment_id: str, body: IngameBody, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> dict[str, Any]:
    """Set the in-game model and/or the trade_ingame switch (audited); turning in-game
    trading off cancels the assignment's open in-game orders."""
    changes = {key: getattr(body, key) for key in assignments_ingame.FIELDS if key in body.model_fields_set}
    if changes.get("trade_ingame", False) is None:
        changes.pop("trade_ingame")
    return jsonable(assignments_ingame.set_ingame(conn, assignment_id, actor, changes))


@router.post("/assignments/{assignment_id}/settle")
def post_settle(assignment_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Settle now: only once the game is final (409 otherwise); runs the exchange's settle_game."""
    row = assignments.get_assignment(conn, assignment_id)
    game = conn.execute("SELECT status FROM games WHERE game_id = %s", (row["game_id"],)).fetchone()
    if game is None or game["status"] != "final":
        raise Conflict("the game is not final yet")
    try:
        from host.exchange.settle import settle_game
    except ImportError:
        raise HTTPException(status_code=503, detail=EXCHANGE_MISSING) from None
    return jsonable(settle_game(conn, row["game_id"], actor))


@router.get("/orders")
def get_orders(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    assignment_id: str | None = Query(default=None),
    conn: psycopg.Connection = DB,
) -> list[dict[str, Any]]:
    """Newest orders first (status may be one status or "active")."""
    return jsonable(views.list_orders(conn, status, limit, assignment_id))


@router.get("/orders/{order_id}")
def get_order(order_id: str, conn: psycopg.Connection = DB) -> dict[str, Any]:
    """One order with its events and fills."""
    return jsonable(views.order_with_events(conn, order_id))


@router.post("/orders/{order_id}/cancel")
def post_cancel_order(order_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Owner cancel of one order (paper at once; live cancel_requested)."""
    from host.trading import orders

    oid = views.parse_uuid(order_id, "order")
    orders.get_order(conn, oid)
    return {"status": orders.cancel_order(conn, oid, actor, "owner cancel")}


@router.get("/fills")
def get_fills(limit: int = Query(default=50, ge=1, le=500), conn: psycopg.Connection = DB) -> list[dict[str, Any]]:
    """Newest fills first."""
    return jsonable(views.list_fills(conn, limit))


@router.get("/markets")
def get_markets(
    unmatched: int = Query(default=0), game_id: str | None = Query(default=None), conn: psycopg.Connection = DB
) -> list[dict[str, Any]]:
    """Markets; `?unmatched=1` lists those the owner still has to link to a game."""
    return jsonable(views.list_markets(conn, bool(unmatched), game_id))


@router.post("/markets/{market_id}/link")
def post_link(
    market_id: str, body: LinkBody, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> dict[str, Any]:
    """Confirm a market's game and side by hand."""
    return jsonable(views.link_market(conn, market_id, body.game_id, body.side, actor))


@router.get("/exchange")
def get_exchange(conn: psycopg.Connection = DB) -> dict[str, Any]:
    """The exchange process state (heartbeat age, source, auth, last error)."""
    return jsonable(views.exchange_state(conn))


@router.post("/exchange/probe")
def post_probe(conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Raw (truncated) markets payload of the configured source, for pasting back."""
    try:
        from host.exchange.probe import probe_markets
    except ImportError:
        raise HTTPException(status_code=503, detail=EXCHANGE_MISSING) from None
    return jsonable(probe_markets(conn))


@router.post("/cancel-all")
def post_cancel_all(
    body: CancelAllBody | None = None, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> dict[str, Any]:
    """Cancel every active order (or one mode's) without raising the kill switch."""
    return kill.cancel_all(conn, actor, body.mode if body is not None else None)
