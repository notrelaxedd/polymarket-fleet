"""The /trading page: assignments, orders, fills, markets, the exchange state and
the forms that act on them (docs/DASHBOARD.md "Trading page").

Reads go through host.trading.assignments / views and host.views; every form calls
the same function its /api twin calls and redirects back to /trading with a flash.
A rejected create form re-renders the page with the error inline (400); a refused
action (409) comes back as a flash rather than an error page, so the owner never
leaves the page on a phone.
"""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response

from host import kill, views, web
from host.api.dashboard import FORM, _age_seconds, _now, page
from host.api.deps import DB, require_owner
from host.errors import BadRequest, Conflict
from host.leaderboard import short_params
from host.money import cents_to_dollars, dollars_to_cents
from host.settings import get_int_setting, get_setting, get_settings
from host.trading import assignments, ledger, orders
from host.trading import views as trading_views

router = APIRouter(tags=["dashboard-trading"], dependencies=[Depends(require_owner)])

RECENT_ORDERS = 50
RECENT_FILLS = 50


def _game_label(game: dict[str, Any], tz: Any) -> str:
    kickoff = web.format_ts(game.get("kickoff_at"), web.zone(tz)) if game.get("kickoff_at") else "kickoff TBD"
    return f"{game['away_team']} @ {game['home_team']} · {kickoff} · {game['game_id']}"


UNTRAINED = "untrained: mirrors the market, never trades"


def _model_label(model: dict[str, Any]) -> str:
    through = model.get("trained_through")
    point = f"thru {through[0]} w{through[1]}" if isinstance(through, list) and len(through) == 2 else UNTRAINED
    return f"{model['family']} · {short_params(model['family'], model['params'])} · {point} · {str(model['id'])[:8]}"


REASON_TEXT = {
    "duplicate": "repeated request", "killed": "kill switch on", "lease": "job not leased by this worker",
    "assignment": "assignment not active", "market": "market not tradable", "kickoff": "game kicked off",
    "mode": "live not allowed", "stale_book": "cited book too old", "liquidity": "book below the liquidity floor",
    "price_band": "price outside the band", "bankroll": "cost over available", "daily_loss": "daily loss limit",
    "exposure": "exposure limit", "buying_power": "buying power",
}


def reason_text(order: dict[str, Any], settings: dict[str, Any]) -> str | None:
    """A rejection reason in words with the number it broke, after the code."""
    code = order.get("reject_reason")
    if not code:
        return None
    if code == "max_bet":
        limit = int(settings.get("max_bet_cents") or 0)
        own = order.get("assignment_max_bet_cents")
        if own is not None:
            limit = min(limit, int(own))
        return f"over max bet {web.format_cents(int(order['cost_cents']))} > {web.format_cents(limit)}"
    if code == "participation":
        share = float(settings.get("participation") or 0.0)
        return f"over {int(round(share * 100))}% of book depth"
    return REASON_TEXT.get(code, code)


def _annotate_orders(rows: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    for o in rows:
        o["reason_text"] = reason_text(o, settings)
    return rows


def exchange_context(conn: psycopg.Connection) -> dict[str, Any]:
    """The exchange box: the state row with `down`, plus (step 5) the age of the last
    auth probe and of the balance figure for the auth and buying-power lines."""
    exchange = trading_views.exchange_state(conn)
    now = _now(conn)
    exchange["auth_age_s"] = _age_seconds(now, exchange.get("auth_checked_at"))
    exchange["balance_age_s"] = _age_seconds(now, exchange.get("balance_checked_at"))
    return exchange


def live_context(conn: psycopg.Connection) -> dict[str, Any]:
    """Everything inside #trading-live (refreshed every 5 s)."""
    rows = assignments.list_assignments(conn)
    for a in rows:
        a["short_params"] = short_params(a["model"]["family"], a["model"]["params"])
        a["settle_ready"] = a["game"]["status"] == "final" and a["status"] in ("active", "halted")
    markets = trading_views.list_markets(conn)
    killed = kill.is_killed(conn)
    settings = get_settings(conn)
    live_orders = views.live_order_counts(conn)
    return {
        "live_orders": live_orders["live"],
        "smoke_orders": live_orders["smoke"],
        "assignments": rows,
        "killed": killed,
        "halted_paper": 0 if killed else views.halted_paper_count(conn),
        "open_orders": trading_views.list_orders(conn, "active", 200),
        "orders": _annotate_orders(trading_views.list_orders(conn, None, RECENT_ORDERS), settings),
        "fills": trading_views.list_fills(conn, RECENT_FILLS),
        "unmatched": [m for m in markets if not m["mapping_confirmed"] or m["game_id"] is None],
        "markets": [m for m in markets if m["mapping_confirmed"] and m["game_id"] is not None],
        "link_games": [{**g, "label": _game_label(g, get_setting(conn, "tz"))} for g in views.upcoming_games(conn, with_markets=False)],
        "exchange": exchange_context(conn),
        "ledger_problems": ledger.replay_problems(conn),
        "bankrolls": conn.execute("SELECT count(*) AS n FROM bankrolls").fetchone()["n"],
        "names": views.worker_names(conn),
    }


def trading_context(
    conn: psycopg.Connection, model: str | None = None, error: str | None = None, submitted: dict[str, str] | None = None
) -> dict[str, Any]:
    """The whole page: the live region plus the create form's selects and defaults."""
    tz = get_setting(conn, "tz")
    values = {
        "game_id": "", "model_id": model or "", "mode": "paper",
        "bankroll": cents_to_dollars(get_int_setting(conn, "default_bankroll_cents", 10_000)), "max_bet": "",
    }
    values.update({k: v for k, v in (submitted or {}).items() if k in values})
    return {
        **live_context(conn),
        "games": [{**g, "label": _game_label(g, tz)} for g in views.upcoming_games(conn)],
        "models": [{"id": str(m["id"]), "label": _model_label(m)} for m in views.assignable_models(conn)],
        "values": values,
        "error": error,
        "assign_open": bool(model or error),
        "live_mode": get_setting(conn, "live_enabled") is True,
    }


def trading_page_response(request: Request, conn: psycopg.Connection, status: int = 200, **ctx: Any) -> HTMLResponse:
    return page(request, conn, "trading.html", status=status, **trading_context(conn, **ctx))


@router.get("/trading", response_class=HTMLResponse)
def trading_page(
    request: Request, model: str | None = Query(default=None, max_length=64), conn: psycopg.Connection = DB
) -> HTMLResponse:
    """The trading page; ?model=<id> opens the create form with that model selected."""
    return trading_page_response(request, conn, model=model)


@router.get("/fragments/trading", response_class=HTMLResponse)
def trading_fragment(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """Inner HTML of #trading-live for the 5 s refresh."""
    settings_tz = get_setting(conn, "tz")
    return web.render(request, "_trading.html", tz=settings_tz, **live_context(conn))


def _optional_dollars(form: dict[str, str], name: str, label: str) -> int | None:
    text = (form.get(name) or "").strip()
    return dollars_to_cents(text, label) if text else None


@router.post("/assignments")
def post_assignment(
    request: Request, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Create an assignment from the form; a refusal re-renders the page with the error (400)."""
    try:
        bankroll = dollars_to_cents(form.get("bankroll") or "", "Bankroll")
        row = assignments.create_assignment(
            conn, (form.get("game_id") or "").strip(), (form.get("model_id") or "").strip(),
            (form.get("mode") or "paper").strip() or "paper", bankroll, actor, _optional_dollars(form, "max_bet", "Max bet"),
        )
    except (BadRequest, Conflict) as exc:
        conn.rollback()
        return trading_page_response(request, conn, status=exc.status, error=exc.message, submitted=form)
    return web.redirect("/trading", f"assignment {str(row['id'])[:8]} created: {row['mode']} on {row['game_id']}, bankroll {web.format_cents(bankroll)}")


def _action(conn: psycopg.Connection, label: str, call: Any) -> Response:
    """Run an assignment or order action; a 409 becomes a flash, not an error page."""
    try:
        note = call()
    except Conflict as exc:
        conn.rollback()
        return web.redirect("/trading", f"{label} refused: {exc.message}")
    return web.redirect("/trading", note)


@router.post("/assignments/activate-paper")
def post_activate_paper(actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """Every halted paper assignment back to active (after a kill reset)."""
    return _action(conn, "activate all paper", lambda: f"{assignments.activate_all_paper(conn, actor)} paper assignments activated")


@router.post("/assignments/{assignment_id}/halt")
def post_halt(
    assignment_id: str, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Halt: open orders cancelled, the trade job stays leased."""
    reason = (form.get("reason") or "").strip()[:200] or "owner halt"

    def halt() -> str:
        row = assignments.halt_assignment(conn, assignment_id, actor, reason)
        return f"assignment {str(row['id'])[:8]} halted"

    return _action(conn, "halt", halt)


@router.post("/assignments/{assignment_id}/activate")
def post_activate(assignment_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """halted -> active (refused under kill or for a final game)."""
    return _action(conn, "activate", lambda: f"assignment {str(assignments.activate_assignment(conn, assignment_id, actor)['id'])[:8]} activated")


@router.post("/assignments/{assignment_id}/settle")
def post_settle(assignment_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """Settle now: only once the game is final."""

    def settle() -> str:
        row = assignments.get_assignment(conn, assignment_id)
        game = conn.execute("SELECT status FROM games WHERE game_id = %s", (row["game_id"],)).fetchone()
        if game is None or game["status"] != "final":
            raise Conflict("the game is not final yet")
        try:
            from host.exchange.settle import settle_game
        except ImportError:
            raise Conflict("exchange module not available") from None
        summary = settle_game(conn, row["game_id"], actor)
        return f"{row['game_id']} settled: {summary.get('bets', 0)} bets, P&L {web.format_cents(summary.get('pnl_cents', 0))}"

    return _action(conn, "settle", settle)


@router.post("/orders/{order_id}/cancel")
def post_cancel_order(order_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """Owner cancel of one order (paper at once; live cancel_requested)."""
    oid = trading_views.parse_uuid(order_id, "order")
    orders.get_order(conn, oid)
    status = orders.cancel_order(conn, oid, actor, "owner cancel")
    return web.redirect("/trading", f"order {str(oid)[:8]} {status.replace('_', ' ')}")


@router.post("/cancel-all")
def post_cancel_all(form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """Cancel every active order (or one mode's) without a kill."""
    mode = (form.get("mode") or "").strip() or None
    try:
        result = kill.cancel_all(conn, actor, mode)
    except BadRequest as exc:
        conn.rollback()
        return web.redirect("/trading", f"cancel all refused: {exc.message}")
    return web.redirect("/trading", f"{result['cancelled']} orders cancelled, {result['requested']} cancel requested")


@router.post("/markets/{market_id}/link")
def post_link(
    market_id: str, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Confirm an unmatched market's game and side by hand."""
    try:
        row = trading_views.link_market(conn, market_id, (form.get("game_id") or "").strip(), (form.get("side") or "").strip(), actor)
    except BadRequest as exc:
        conn.rollback()
        return web.redirect("/trading#unmatched", f"market not linked: {exc.message}")
    return web.redirect("/trading#markets", f"market linked to {row['game_id']} ({row['side']})")


@router.post("/exchange/probe")
def post_probe(request: Request, conn: psycopg.Connection = DB) -> Response:
    """Fetch the raw markets payload of the configured source and show it for pasting back."""
    try:
        from host.exchange.probe import probe_markets
    except ImportError:
        return web.redirect("/trading#exchange", "probe failed: exchange module not available")
    result = probe_markets(conn)
    return page(request, conn, "probe.html", probe=result)
