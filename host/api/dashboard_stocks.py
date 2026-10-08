"""The /stocks dashboard page and its forms (docs/ALPACA.md "Step 9", contract
tools/workflows/step9-contract.txt section 8).

Reads go through host.stocks.views (read-only); every form calls the same function its
/api/stocks twin calls (host.stocks.assignments, host.stocks.models.retire_model,
host.stocks.jobparams_stocks.create_stock_job), imported inside the handler, and
redirects back to /stocks with a flash. A rejected create or search form re-renders the
page with the error inline (400); a refused row action comes back as a flash, so the
owner never leaves the page on a phone (as /trading).
"""
from __future__ import annotations

from typing import Any, Callable

import psycopg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response

from host import kill, web
from host.api.dashboard import FORM, page
from host.api.deps import DB, require_owner
from host.errors import BadRequest, QueueError
from host.money import cents_to_dollars, dollars_to_cents
from host.settings import get_int_setting, get_setting
from host.stocks import views

router = APIRouter(tags=["stocks-dashboard"], dependencies=[Depends(require_owner)])

FAMILIES = ("momentum", "meanrev", "trend", "buyhold")
SEARCH_DEFAULTS = {"n": "200", "seed": "0"}
TRADABLE = ("paper_ok", "live_eligible")
INTRO = ("Stock models on Alpaca: what the broker says, which model trades which symbols and how it is doing. "
         "One market-on-close decision per session; actions sit in each row's menu.")


def live_context(conn: psycopg.Connection) -> dict[str, Any]:
    """Everything inside #stocks-live (refreshed every 5 s): the stats, the broker card,
    the assignment and model rows and the disclosures below them."""
    broker = views.broker(conn)
    held = views.positions(conn)
    rows = views.assignments(conn, held)
    orders = views.orders(conn)
    running = [a for a in rows if a["status"] != "closed"]
    all_models = views.models(conn)
    return {
        "broker": broker,
        "feed": views.feed(conn),
        "models": [m for m in all_models if m["status"] != "retired"],
        "retired": [m for m in all_models if m["status"] == "retired"],
        "assignments": rows,
        "positions": held,
        "orders": orders["rows"],
        "orders_session": orders["session_date"],
        "open_orders": sum(int(a["open_orders"]) for a in rows),
        "today_cents": sum(a["today_cents"] for a in running),
        "running": len(running),
        "killed": kill.is_killed(conn),
        "tone": views.tone,
    }


def _symbols_text(conn: psycopg.Connection) -> str:
    """The followed symbols that can trade now (tradable, with bars), for the create form."""
    ok = [f["symbol"] for f in views.feed(conn) if f["followed"] and f["tradable"] and f["bars_count"]]
    return ", ".join(ok)


def stocks_context(
    conn: psycopg.Connection, model: str | None = None, error: str | None = None, submitted: dict[str, str] | None = None,
    search_error: str | None = None, search: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The whole page: the live region plus the two forms above it (New assignment,
    New search) with their defaults or what was just typed."""
    ctx = live_context(conn)
    env = ctx["broker"].get("environment")
    values = {"model_id": model or "", "mode": env or "paper", "symbols": _symbols_text(conn),
              "bankroll": cents_to_dollars(get_int_setting(conn, "stock_default_bankroll_cents", 1_000_000))}
    values.update({k: v for k, v in (submitted or {}).items() if k in values})
    search_values = {**SEARCH_DEFAULTS, **{f"family_{f}": "true" for f in FAMILIES}}
    if search is not None:
        search_values = {k: (search.get(k) or "") for k in search_values}
    return {
        **ctx,
        "assignable": [m for m in ctx["models"] if m["status"] in TRADABLE],
        "values": values,
        "error": error,
        "assign_open": bool(model or error),
        "live_mode": get_setting(conn, "live_enabled") is True,
        "families": FAMILIES,
        "search": search_values,
        "search_error": search_error,
        "intro": INTRO,
    }


def stocks_page_response(request: Request, conn: psycopg.Connection, status: int = 200, **ctx: Any) -> HTMLResponse:
    return page(request, conn, "stocks.html", status=status, **stocks_context(conn, **ctx))


@router.get("/stocks", response_class=HTMLResponse)
def stocks_page(
    request: Request, model: str | None = Query(default=None, max_length=24), conn: psycopg.Connection = DB,
) -> HTMLResponse:
    """The stocks page; ?model=<id> opens New assignment with that model selected."""
    return stocks_page_response(request, conn, model=model)


@router.get("/fragments/stocks", response_class=HTMLResponse)
def stocks_fragment(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """Inner HTML of #stocks-live for the 5 s refresh."""
    return web.render(request, "_stocks.html", tz=get_setting(conn, "tz"), **live_context(conn))


def _model_id(text: str | None, label: str = "Model") -> int:
    value = (text or "").strip()
    if not value.isdigit() or len(value) > 18 or int(value) <= 0:
        raise BadRequest(f"{label}: choose a stock model")
    return int(value)


def _symbols(text: str | None) -> list[str]:
    return [s.upper() for s in (text or "").replace(",", " ").split() if s]


@router.post("/stocks/assignments")
def post_assignment(
    request: Request, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Create a stock assignment; a refusal re-renders the page with the error (400/409)."""
    from host.stocks.assignments import create_assignment

    try:
        bankroll = dollars_to_cents(form.get("bankroll") or "", "Bankroll")
        mode = (form.get("mode") or "paper").strip() or "paper"
        row = create_assignment(conn, _model_id(form.get("model_id")), mode, bankroll, _symbols(form.get("symbols")), actor)
    except QueueError as exc:
        conn.rollback()
        return stocks_page_response(request, conn, status=exc.status, error=exc.message, submitted=form)
    return web.redirect("/stocks", f"stock assignment {row['id']} created: {row['mode']} on {len(row['symbols'])} "
                                   f"symbols, bankroll {web.format_cents(bankroll)}")


def _action(conn: psycopg.Connection, label: str, call: Callable[[], str], anchor: str = "") -> Response:
    """Run a row action; a refusal (400, 404, 409) becomes a flash, not an error page."""
    try:
        note = call()
    except QueueError as exc:
        conn.rollback()
        return web.redirect("/stocks" + anchor, f"{label} refused: {exc.message}")
    return web.redirect("/stocks" + anchor, note)


@router.post("/stocks/assignments/{assignment_id}/halt")
def post_halt(
    assignment_id: str, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Halt: approved orders cancelled, orders at Alpaca cancel_requested."""
    from host.stocks.assignments import halt_assignment

    reason = (form.get("reason") or "").strip()[:200] or "owner halt"
    return _action(conn, "halt", lambda: f"stock assignment {halt_assignment(conn, assignment_id, reason, actor)['id']} halted",
                   "#assignments")


@router.post("/stocks/assignments/{assignment_id}/resume")
def post_resume(assignment_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """halted -> active under the same gates as create."""
    from host.stocks.assignments import resume_assignment

    return _action(conn, "resume", lambda: f"stock assignment {resume_assignment(conn, assignment_id, actor)['id']} resumed",
                   "#assignments")


@router.post("/stocks/assignments/{assignment_id}/close")
def post_close(assignment_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """Close: only with no positions and no open orders."""
    from host.stocks.assignments import close_assignment

    return _action(conn, "close", lambda: f"stock assignment {close_assignment(conn, assignment_id, actor)['id']} closed",
                   "#assignments")


@router.post("/stocks/models/{model_id}/retire")
def post_retire(model_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """Retire a stock model (final); its active assignments are halted."""
    from host.stocks.models import retire_model

    return _action(conn, "retire", lambda: f"stock model {retire_model(conn, model_id, actor)['id']} retired", "#models")


def _search_params(form: dict[str, str]) -> dict[str, Any]:
    """{n, seed, families} of the New search form; 400 on anything that is not a whole number."""
    out: dict[str, Any] = {}
    for key, label in (("n", "Candidates"), ("seed", "Seed")):
        try:
            out[key] = int((form.get(key) or "").strip())
        except ValueError:
            raise BadRequest(f"{label} must be a whole number") from None
    families = [f for f in FAMILIES if (form.get(f"family_{f}") or "").strip().lower() in {"1", "true", "on", "yes"}]
    if not families:
        raise BadRequest("tick at least one family")
    out["families"] = families
    return out


def _queued(job: dict[str, Any]) -> str:
    return f"{job['kind']} job {str(job['id'])[:8]} queued"


@router.post("/stocks/jobs")
def post_job(
    request: Request, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Queue a stock_search (the New search form; a refusal re-renders it, 400) or a
    stock_backtest / stock_validate of one model (a model row's menu; a refusal is a flash)."""
    from host.stocks.jobparams_stocks import create_stock_job

    kind = (form.get("kind") or "").strip()
    if kind == "stock_search":
        try:
            job = create_stock_job(conn, kind, _search_params(form), actor)
        except QueueError as exc:
            conn.rollback()
            return stocks_page_response(request, conn, status=exc.status, search_error=exc.message, search=form)
        return web.redirect("/stocks", _queued(job))
    if kind not in ("stock_backtest", "stock_validate"):
        return web.redirect("/stocks", f"job refused: unknown kind {kind[:40]!r}")
    word = kind.removeprefix("stock_")
    return _action(conn, word, lambda: _queued(create_stock_job(
        conn, kind, {"model_id": _model_id(form.get("model_id"))}, actor)), "#models")
