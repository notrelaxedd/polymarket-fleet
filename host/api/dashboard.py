"""Dashboard pages and refresh fragments (GET). Form posts live in dashboard_forms.

Everything here is read-only and reuses the queries behind the JSON API
(host.views, host.settings, host.pnl, host.kill); the templates do the layout.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from host import kill, pnl, views, web
from host.api.deps import DB, get_config, require_owner
from host.config import Config
from host.scheduling import online_after
from host.settings import ROLES, get_settings
from host.settings_forms import form_values

router = APIRouter(tags=["dashboard"], dependencies=[Depends(require_owner)])

STALE_AFTER_SECONDS = 60
JOB_KINDS = (
    ("sleep", "sleep", True),
    ("backtest", "backtest (step 3)", False),
    ("model_search", "model_search (step 3)", False),
    ("train", "train (step 3)", False),
)


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
    """Mode pill, P&L totals and the kill state."""
    settings = get_settings(conn) if settings is None else settings
    totals = pnl.pnl(conn)
    return {
        "live": settings.get("live_enabled") is True,
        "killed": settings.get("kill_switch") is True,
        "pnl_today": totals["today_cents"],
        "pnl_all": totals["all_time_cents"],
    }


def page(request: Request, conn: psycopg.Connection, template: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    """Render a full page: the topbar context and the owner's time zone (settings.tz,
    used by the ts filter) are added to every page."""
    settings = get_settings(conn)
    return web.render(request, template, status=status, topbar=topbar_context(conn, settings), tz=settings.get("tz"), **ctx)


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


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """Send form plus the newest 50 jobs."""
    names = views.worker_names(conn)
    return page(
        request, conn, "jobs.html", jobs=views.list_jobs(conn, None, 50), names=names, kinds=JOB_KINDS,
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str, conn: psycopg.Connection = DB) -> HTMLResponse:
    """One job with its events."""
    job = views.job_with_events(conn, job_id)
    return page(request, conn, "job.html", job=job, names=views.worker_names(conn))


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
        errors=errors or {}, killed=kill.is_killed(conn), audit=views.audit_rows(conn, 20),
        public_url=config.public_url,
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
