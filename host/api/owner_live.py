"""Owner routes for the live switch (docs/LIVE.md "Live switch", docs/PROTOCOL.md
"Step 5 additions"): POST /live (the typed enable), POST /live/off, GET /api/live
and POST /api/exchange/probe-account.

fleet-host never loads the exchange credentials, so the account probe cannot make
the balance call itself: it answers with what the exchange process last recorded
(auth, balance, skew, the last error) and names the CLI command that returns the
raw payload from inside the exchange container.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from host.api.deps import DB, require_owner
from host.api.serialize import jsonable
from host.errors import BadRequest, QueueError
from host.trading import live

router = APIRouter(tags=["live"], dependencies=[Depends(require_owner)])

PROBE_COMMAND = "docker compose run --rm exchange python -m host.exchange.cli probe-account"


async def _confirm_text(request: Request) -> str:
    """The confirmation text from a JSON body {"confirm": ...} or a form field."""
    content_type = request.headers.get("content-type", "")
    if "form" in content_type:
        form = await request.form()
        value = form.get("confirm")
    else:
        raw = await request.body()
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise BadRequest("the body must be JSON: {\"confirm\": \"ENABLE LIVE TRADING YYYY-MM-DD\"}") from None
        value = body.get("confirm") if isinstance(body, dict) else None
    if not isinstance(value, str):
        raise BadRequest('confirm must be the string "ENABLE LIVE TRADING YYYY-MM-DD" (today, owner time zone)')
    return value


def _is_form(request: Request) -> bool:
    return "form" in request.headers.get("content-type", "")


@router.post("/live")
async def post_live(request: Request, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Any:
    """Enable live trading with today's exact phrase; 400 on the phrase, 409 on a
    precondition. Outside /api the app renders errors as HTML pages: a JSON caller
    gets JSON errors from here, a form post keeps the dashboard error page."""
    try:
        confirm = await _confirm_text(request)
        return jsonable(live.enable_live(conn, actor, confirm))
    except QueueError as exc:
        if _is_form(request):
            raise
        return JSONResponse({"detail": exc.message}, status_code=exc.status)


@router.post("/live/off")
async def post_live_off(request: Request, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Any:
    """Live off at once: live assignments halted, live orders cancelled on the exchange.
    Optional body {"reason": "..."} (JSON or form)."""
    reason = "owner"
    if _is_form(request):
        value = (await request.form()).get("reason")
    else:
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = {}
        value = body.get("reason") if isinstance(body, dict) else None
    if isinstance(value, str) and value.strip():
        reason = value.strip()[:200]
    try:
        return jsonable(live.disable_live(conn, actor, reason))
    except QueueError as exc:
        if _is_form(request):
            raise
        return JSONResponse({"detail": exc.message}, status_code=exc.status)


@router.get("/api/live")
def get_live(conn: psycopg.Connection = DB) -> dict[str, Any]:
    """The live switch state, credentials and auth status, balances, skew and the
    auto-kill reasons since the last reset."""
    return jsonable(live.live_state(conn))


@router.post("/api/exchange/probe-account")
def post_probe_account(conn: psycopg.Connection = DB) -> dict[str, Any]:
    """What the exchange process recorded about the account; the raw payload itself
    comes from the exchange container's CLI (fleet-host holds no key)."""
    state = live.live_state(conn)
    return jsonable({
        "key_present": state["credentials_present"],
        "status": "ok" if state["auth_ok"] else "failed" if state["last_auth_error"] else None,
        "payload": None,
        "auth_ok": state["auth_ok"], "auth_checked_at": state["auth_checked_at"], "auth_age_s": state["auth_age_s"],
        "balance_cents": state["balance_cents"], "buying_power_cents": state["buying_power_cents"],
        "clock_skew_ms": state["clock_skew_ms"], "last_auth_error": state["last_auth_error"],
        "error": None if state["credentials_present"] else "the exchange process reports no credentials",
        "raw_payload_command": PROBE_COMMAND,
    })
