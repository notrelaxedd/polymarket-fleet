"""In-game forms of the /trading page (docs/DASHBOARD.md "Trading page", contract
section 12): the per-assignment in-game toggle, the New assignment form's in-game
fields and the "Probe game state" button.

Mounted from host/api/dashboard.py beside dashboard_trading. The toggle calls
host.trading.assignments_ingame.set_ingame, as POST /api/assignments/{id}/ingame does;
a refusal comes back as a flash so the owner stays on the page.
"""
from __future__ import annotations

import re
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from host import web
from host.api.dashboard import FORM, page
from host.api.deps import DB, require_owner
from host.errors import BadRequest, Conflict, NotFound
from host.settings import get_setting
from host.trading import assignments_ingame

router = APIRouter(tags=["dashboard-ingame"], dependencies=[Depends(require_owner)])

EVENT_ID = re.compile(r"^[0-9]{1,20}$")
MARKER = "ingame_form"


def _checked(form: dict[str, str], name: str) -> bool:
    return (form.get(name) or "").strip().lower() in ("on", "true", "1", "yes")


def create_args(form: dict[str, str]) -> dict[str, Any]:
    """The in-game keyword arguments of create_assignment from the New assignment form:
    ingame_model_id when one is picked, trade_ingame when the form carried the box (an
    unticked box posts nothing, so the hidden marker tells "off" from "not on the form")."""
    out: dict[str, Any] = {}
    model = (form.get("ingame_model_id") or "").strip()
    if model:
        out["ingame_model_id"] = model
    if MARKER in form:
        out["trade_ingame"] = _checked(form, "trade_ingame")
    return out


def form_context(conn: psycopg.Connection, submitted: dict[str, str] | None, model: str | None = None) -> dict[str, Any]:
    """The New assignment form's in-game values (the box defaults to settings.trade_ingame);
    its select lists `ingame_models`, which the live context already carries. A model's
    Assign button (?model=) on an ingame_wp model preselects it in the in-game select."""
    values = {"ingame_model_id": model or "", "trade_ingame": get_setting(conn, "trade_ingame", False) is True}
    if submitted is not None and MARKER in submitted:
        values = {"ingame_model_id": (submitted.get("ingame_model_id") or "").strip(),
                  "trade_ingame": _checked(submitted, "trade_ingame")}
    return {"ingame_values": values}


@router.post("/assignments/{assignment_id}/ingame")
def post_ingame(
    assignment_id: str, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Set or clear an assignment's in-game model and switch (audited by the call)."""
    model = (form.get("ingame_model_id") or "").strip() or None
    trade = _checked(form, "trade_ingame")
    try:
        row = assignments_ingame.set_ingame(conn, assignment_id, actor, {"ingame_model_id": model, "trade_ingame": trade})
    except (BadRequest, Conflict, NotFound) as exc:
        conn.rollback()
        return web.redirect("/trading#assignments", f"in-game change refused: {exc.message}")
    on = bool(row.get("trade_ingame") and row.get("ingame_model_id"))
    chosen = f"model {str(row['ingame_model_id'])[:8]}" if row.get("ingame_model_id") else "no in-game model"
    note = f"assignment {str(row['id'])[:8]}: in-game trading {'on' if on else 'off'}, {chosen}"
    if row.get("orders_cancelled"):
        note += f", {row['orders_cancelled']} open in-game orders cancelled"
    return web.redirect("/trading#assignments", note)


@router.post("/exchange/probe-gamestate")
def post_probe_gamestate(request: Request, form: dict[str, str] = FORM, conn: psycopg.Connection = DB) -> Response:
    """One game-state request for an ESPN event id, shown with what the parser extracted."""
    event = (form.get("event") or "").strip()
    if not EVENT_ID.match(event):
        return web.redirect("/trading#exchange", "game-state probe refused: the event is the ESPN event id (digits only)")
    try:
        from host.exchange.probe import probe_gamestate
    except ImportError:
        return web.redirect("/trading#exchange", "game-state probe failed: exchange module not available")
    result = probe_gamestate(conn, event, _checked(form, "yahoo"))
    return page(request, conn, "probe.html", probe=result, gamestate=True)
