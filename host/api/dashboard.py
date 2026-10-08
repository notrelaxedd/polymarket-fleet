"""Dashboard pages and refresh fragments (GET). Form posts live in dashboard_forms,
the models pages in dashboard_models and the trading page in dashboard_trading.

Everything here is read-only and reuses the queries behind the JSON API
(host.views, host.settings, host.pnl, host.kill); the templates do the layout.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from host import data_refresh, kill, nflverse, pnl, views, web
from host.api.deps import DB, get_config, require_owner
from host.api.job_forms import jobs_context
from host.api.robustness import robustness_context
from host.home import home_context
from host.jobs_view import duration_text, job_target, jobs_list_context, run_seconds
from host.leaderboard import short_params
from host.config import Config
from host.scheduling import online_after
from host.settings import ROLES, get_settings
from host.settings_forms import MARKET_SOURCES, form_values

router = APIRouter(tags=["dashboard"], dependencies=[Depends(require_owner)])

STALE_AFTER_SECONDS = 60


async def form_data(request: Request) -> dict[str, str]:
    """The posted form as plain strings (file fields are ignored)."""
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


FORM = Depends(form_data)


def _now(conn: psycopg.Connection) -> datetime:
    return conn.execute("SELECT now() AS t").fetchone()["t"]


def _age_seconds(now: datetime, then: datetime | None) -> int | None:
    if then is None:
        return None
    return max(0, int((now - then).total_seconds()))


def _dot(age: int | None, online_after_s: int) -> str:
    """Status dot class: online below online_after, stale below 60 s, offline otherwise."""
    if age is None:
        return "offline"
    if age < online_after_s:
        return "online"
    if age < max(STALE_AFTER_SECONDS, online_after_s):
        return "stale"
    return "offline"


def fleet_context(conn: psycopg.Connection) -> dict[str, Any]:
    """Worker cards: /api/fleet data plus age, dot class and per-worker P&L, and the
    counts the Fleet stats show (online, working, offline, switching)."""
    now = _now(conn)
    threshold = online_after(conn)
    by_worker = pnl.pnl(conn)["by_worker"]
    cards = []
    for w in views.fleet_workers(conn):
        age = _age_seconds(now, w["last_heartbeat_at"])
        cards.append({**w, "age": age, "dot": _dot(age, threshold), "pnl_cents": by_worker.get(w["id"], 0)})
    counts = {
        "online": sum(1 for c in cards if c["dot"] == "online"),
        "working": sum(1 for c in cards if c["current_jobs"]),
        "offline": sum(1 for c in cards if c["dot"] == "offline"),
        "switching": sum(1 for c in cards if c["switching"]),
    }
    return {"workers": cards, "roles": ROLES, "counts": counts}


def live_context(conn: psycopg.Connection) -> dict[str, Any]:
    """The Settings "Live trading" group (docs/LIVE.md "Dashboard additions"): the
    switch state, credentials, auth, balances, skew, the auto-kill reasons and today's
    phrase from host.trading.live, imported lazily so the dashboard still renders on a
    host without the step 5 module."""
    try:
        from host.trading.live import live_state
    except ImportError:
        state = views.live_state_fallback(conn)
    else:
        state = live_state(conn)
    state["remedies"] = {reason: views.auto_kill_remedy(reason) for reason in state.get("auto_kill_reasons") or []}
    return state


def topbar_context(conn: psycopg.Connection, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mode pill, P&L per mode, the kill state (with the auto-kill reason when the
    exchange pulled the switch) and the two banners (docs/TRADING.md "Dashboard
    additions"): EXCHANGE DOWN and N assignments unattended."""
    settings = get_settings(conn) if settings is None else settings
    totals = pnl.pnl(conn)
    killed = settings.get("kill_switch") is True
    live = settings.get("live_enabled") is True
    return {
        "live": live,
        "live_activity": live or views.live_activity(conn),
        "killed": killed,
        "auto_kill": views.latest_auto_kill(conn) if killed else None,
        "pnl_today": totals["today_cents"],
        "pnl_all": totals["all_time_cents"],
        "pnl": totals["by_mode"],
        "exchange_down": views.exchange_down(conn),
        "unattended": views.unattended_assignments(conn),
    }


def page(request: Request, conn: psycopg.Connection, template: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    """Render a full page: the topbar context and the owner's time zone (settings.tz,
    used by the ts filter) are added to every page."""
    settings = get_settings(conn)
    return web.render(
        request, template, status=status, topbar=topbar_context(conn, settings), tz=settings.get("tz"),
        short_params=short_params, **ctx,
    )


@router.get("/", response_class=HTMLResponse)
def home_page(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """Home: the headline stats, what needs attention and the last settled bets."""
    return page(request, conn, "home.html", **home_context(conn))


@router.get("/fragments/home", response_class=HTMLResponse)
def home_fragment(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """Inner HTML of #home-live (the stats, Needs attention, Recent) for the 5 s refresh."""
    return web.render(request, "_home.html", tz=get_settings(conn).get("tz"), **home_context(conn))


@router.get("/fleet/list", response_class=HTMLResponse)
def fleet_page(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """The fleet as cards, one per worker (the phone-friendly view; /fleet is the 3D
    page from dashboard_fleet3d)."""
    return page(request, conn, "fleet.html", **fleet_context(conn))


@router.get("/fragments/fleet", response_class=HTMLResponse)
def fleet_fragment(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """Inner HTML of #fleet-grid for the 5 s refresh."""
    return web.render(request, "_fleet.html", **fleet_context(conn))


@router.get("/fragments/topbar", response_class=HTMLResponse)
def topbar_fragment(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """Inner HTML of #topbar-status for the 10 s refresh."""
    return web.render(request, "_topbar.html", topbar=topbar_context(conn))


def jobs_page_response(
    request: Request, conn: psycopg.Connection, status: int = 200, tab: str | None = None, **ctx: Any
) -> HTMLResponse:
    """The jobs page: the send forms (context from job_forms) plus one tab of the job
    list (host.jobs_view: Running, or the last 50 Done)."""
    return page(request, conn, "jobs.html", status=status, **jobs_list_context(conn, tab), **jobs_context(conn, **ctx))


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request, train_model: str | None = Query(default=None, max_length=64),
    validate_model: str | None = Query(default=None, max_length=64),
    tab: str | None = Query(default=None, max_length=16), conn: psycopg.Connection = DB,
) -> HTMLResponse:
    """Send forms plus a tab of jobs (?tab=done for the finished ones); ?train_model=<id>
    prefills the train form, ?validate_model=<id> the validate form."""
    return jobs_page_response(request, conn, tab=tab, train_model=train_model, validate_model=validate_model)


def checkpoint_digest(kind: Any, checkpoint: Any) -> str:
    """One line on where a job's checkpoint stands; the full JSON stays behind a details."""
    if not isinstance(checkpoint, dict):
        return "none"
    nxt = checkpoint.get("next")
    if kind == "sleep":
        return f"elapsed {checkpoint.get('elapsed', 0)} s"
    if kind == "model_search" and isinstance(nxt, list) and len(nxt) == 2:
        return f"candidate {nxt[0]}, season index {nxt[1]}; {checkpoint.get('evaluated', 0)} evaluated, {len(checkpoint.get('top') or [])} in the top list"
    if kind == "backtest":
        return f"{len(checkpoint.get('per_season') or [])} seasons done"
    if kind == "train":
        return f"{nxt} of {len(checkpoint.get('seasons') or [])} seasons replayed"
    if kind == "validate" and checkpoint.get("stage"):
        return f"stage {checkpoint['stage']}"
    return f"{len(checkpoint)} keys"


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str, conn: psycopg.Connection = DB) -> HTMLResponse:
    """One job with its events."""
    job = views.job_with_events(conn, job_id)
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    created = [m for m in (result.get("created_models") or []) if isinstance(m, dict) and m.get("id")]
    top = [t for t in (result.get("top") or []) if isinstance(t, dict)]
    per_season = [s for s in (result.get("per_season") or []) if isinstance(s, dict)]
    validation = result.get("validation_metrics") if isinstance(result.get("validation_metrics"), dict) else None
    return page(
        request, conn, "job.html", job=job, names=views.worker_names(conn), result=result,
        target=job_target(job), duration=duration_text(run_seconds(job, _now(conn))),
        created_models=created, top=top, per_season=per_season, checkpoint_digest=checkpoint_digest(job["kind"], job.get("checkpoint")),
        robustness=robustness_context(validation, result.get("stress_metrics")),
        model_links={m.get("id") for m in created}, family=job["params"].get("family") if isinstance(job.get("params"), dict) else None,
    )


def settings_page(
    request: Request,
    conn: psycopg.Connection,
    config: Config,
    errors: dict[str, str] | None = None,
    overrides: dict[str, str] | None = None,
    status: int = 200,
) -> HTMLResponse:
    """The settings page; `errors` and `overrides` re-render a rejected group form."""
    settings = get_settings(conn)
    values = form_values(settings)
    values.setdefault("live_confirm", "")
    values.update(overrides or {})
    killed = kill.is_killed(conn)
    return page(
        request, conn, "settings.html", status=status, settings=settings, values=values,
        errors=errors or {}, killed=killed, auto_kill=views.latest_auto_kill(conn) if killed else None,
        live=live_context(conn), audit=views.audit_rows(conn, 20), market_sources=MARKET_SOURCES,
        public_url=config.public_url, games_count=nflverse.games_count(conn),
        last_complete_season=nflverse.last_complete_season(conn), refresh=data_refresh.STATUS.snapshot(),
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_get(
    request: Request, config: Config = Depends(get_config), conn: psycopg.Connection = DB
) -> HTMLResponse:
    """Settings groups, enroll, kill switch and the audit log."""
    return settings_page(request, conn, config)


@router.get("/kill/confirm", response_class=HTMLResponse)
def kill_confirm(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """The no-JavaScript confirmation page with a real POST button."""
    return page(request, conn, "kill_confirm.html", killed=kill.is_killed(conn))


# The trading router imports `page` and `FORM` from this module, so it is included
# here, after they exist, rather than registered in host/api/app.py.
from host.api import dashboard_fleet3d, dashboard_ingame, dashboard_trading  # noqa: E402

router.include_router(dashboard_trading.router)
router.include_router(dashboard_ingame.router)
# The 3D page owns /fleet, /fleet/ and /fleet/assets/...; /fleet/list above is a fixed
# path that none of those patterns match, so the card view is never shadowed.
router.include_router(dashboard_fleet3d.router)
