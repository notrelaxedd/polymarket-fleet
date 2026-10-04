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
    """Worker cards: /api/fleet data plus age, dot class and per-worker P&L."""
    now = _now(conn)
    threshold = online_after(conn)
    by_worker = pnl.pnl(conn)["by_worker"]
    cards = []
    for w in views.fleet_workers(conn):
        age = _age_seconds(now, w["last_heartbeat_at"])
        cards.append({**w, "age": age, "dot": _dot(age, threshold), "pnl_cents": by_worker.get(w["id"], 0)})
    return {"workers": cards, "roles": ROLES}


def topbar_context(conn: psycopg.Connection, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mode pill, P&L per mode, the kill state and the two banners (docs/TRADING.md
    "Dashboard additions"): EXCHANGE DOWN and N assignments unattended."""
    settings = get_settings(conn) if settings is None else settings
    totals = pnl.pnl(conn)
    return {
        "live": settings.get("live_enabled") is True,
        "killed": settings.get("kill_switch") is True,
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
def fleet_page(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """The fleet grid."""
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
    request: Request, conn: psycopg.Connection, status: int = 200, **ctx: Any
) -> HTMLResponse:
    """The jobs page: the send forms (context from job_forms) plus the newest 50 jobs."""
    return page(request, conn, "jobs.html", status=status, jobs=views.list_jobs(conn, None, 50), **jobs_context(conn, **ctx))


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request, train_model: str | None = Query(default=None, max_length=64), conn: psycopg.Connection = DB
) -> HTMLResponse:
    """Send forms plus the newest 50 jobs; ?train_model=<id> prefills the train form."""
    return jobs_page_response(request, conn, train_model=train_model)


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
    return f"{len(checkpoint)} keys"


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str, conn: psycopg.Connection = DB) -> HTMLResponse:
    """One job with its events."""
    job = views.job_with_events(conn, job_id)
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    created = [m for m in (result.get("created_models") or []) if isinstance(m, dict) and m.get("id")]
    top = [t for t in (result.get("top") or []) if isinstance(t, dict)]
    per_season = [s for s in (result.get("per_season") or []) if isinstance(s, dict)]
    return page(
        request, conn, "job.html", job=job, names=views.worker_names(conn), result=result,
        created_models=created, top=top, per_season=per_season, checkpoint_digest=checkpoint_digest(job["kind"], job.get("checkpoint")),
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
    values.update(overrides or {})
    return page(
        request, conn, "settings.html", status=status, settings=settings, values=values,
        errors=errors or {}, killed=kill.is_killed(conn), audit=views.audit_rows(conn, 20), market_sources=MARKET_SOURCES,
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
from host.api import dashboard_trading  # noqa: E402

router.include_router(dashboard_trading.router)
