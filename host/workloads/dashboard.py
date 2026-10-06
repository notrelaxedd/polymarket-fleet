"""Dashboard pages for workloads (docs/workloads-design.md section 10): /machines, the
machine logs, the typed unpin page, /workloads, /workloads/{name} and /outbound. The form
posts live in dashboard_forms; both routers are owner-only and `router` carries both.

The pages are server-rendered from host.workloads.views and need none of the other
workloads modules to render; the sub-nav on /fleet reads its pending count through the
`pending_approvals` template global registered here.
"""
from __future__ import annotations

import os
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from host import web
from host.api.dashboard import page
from host.api.deps import DB, require_owner
from host.errors import NotFound
from host.settings import get_settings
from host.workloads import views
from host.workloads.manifest import NAME_RE

router = APIRouter(tags=["workloads-dashboard"], dependencies=[Depends(require_owner)])


def _pending_for_request(request: Any) -> int | None:
    """The pending approvals count for a page that did not pass one (/fleet): one short
    pooled query; None (no count shown) when there is no pool, e.g. in a template test."""
    try:
        with request.app.state.pool.connection() as conn:
            return views.pending_approvals(conn)
    except Exception:  # noqa: BLE001 - a sub-nav count must never break the page
        return None


web.ENV.globals["pending_approvals"] = _pending_for_request


def workloads_dir() -> str:
    """Where workload folders live on the host (FLEET_WORKLOADS_DIR, default /app/workloads)."""
    return os.environ.get("FLEET_WORKLOADS_DIR") or "/app/workloads"


def _tz(conn: psycopg.Connection) -> Any:
    return web.zone(get_settings(conn).get("tz"))


def machines_response(request: Request, conn: psycopg.Connection, status: int = 200, error: str | None = None) -> HTMLResponse:
    """The machines grid; `error` is the refusal text of a rejected form (nothing changed)."""
    cards = views.machine_cards(conn)
    pending = views.pending_approvals(conn)
    return page(request, conn, "machines.html", status=status, machines=cards, stats=views.machine_stats(cards, pending),
                approvals_pending=pending, error=error, disk_types=views.DISK_TYPES)


def machine_or_404(conn: psycopg.Connection, machine_id: str) -> dict[str, Any]:
    card = views.machine_card(conn, machine_id)
    if card is None:
        raise NotFound(f"machine {machine_id} not found")
    return card


def unpin_response(request: Request, conn: psycopg.Connection, machine_id: str, status: int = 200,
                   error: str | None = None, typed: str = "") -> HTMLResponse:
    card = machine_or_404(conn, machine_id)
    return page(request, conn, "unpin_confirm.html", status=status, m=card, phrase=f"UNPIN {card['name']}", error=error,
                typed=typed, approvals_pending=views.pending_approvals(conn))


def workloads_response(request: Request, conn: psycopg.Connection, status: int = 200, error: str | None = None) -> HTMLResponse:
    rows = views.workload_list(conn)
    pending = views.pending_approvals(conn)
    stats = {"total": len(rows), "enabled": sum(1 for r in rows if r["enabled"]), "running": sum(r["running"] for r in rows),
             "queued": sum(r["queued"] for r in rows), "approvals": pending}
    return page(request, conn, "workloads.html", status=status, workloads=rows, stats=stats, approvals_pending=pending, error=error)


def workload_response(request: Request, conn: psycopg.Connection, name: str, status: int = 200, error: str | None = None,
                      open_section: str | None = None, submitted: dict[str, str] | None = None) -> HTMLResponse:
    """One workload; `open_section` ("send" or "secrets") opens the disclosure a rejected
    form belongs to, `submitted` keeps the send form's values (never a secret value)."""
    if not NAME_RE.match(name):
        raise NotFound(f"workload {name} not found")
    wl = views.workload_detail(conn, name)
    if wl is None:
        raise NotFound(f"workload {name} not found")
    machines = views.workload_machines(conn, name)
    jobs = views.workload_jobs(conn, name)
    pending = views.outbound_pending(conn, name)
    stats = {"machines": sum(1 for m in machines if m["state"] == "running"), "assigned": len(machines),
             "queued": sum(1 for j in jobs["running"] if j["status"] == "queued"),
             "running": sum(1 for j in jobs["running"] if j["status"] != "queued"), "approvals": len(pending)}
    targets = conn.execute("SELECT id, name FROM machines WHERE enabled ORDER BY name").fetchall()
    return page(
        request, conn, "workload.html", status=status, wl=wl, machines=machines, jobs=jobs, pending=pending, stats=stats,
        logs=views.workload_logs(conn, name, machines, _tz(conn)), secrets=views.secret_rows(conn, wl["manifest"]),
        secrets_ready=bool(os.environ.get("FLEET_SECRETS_KEY")), targets=targets, error=error, open_section=open_section,
        submitted=submitted or {}, approvals_pending=views.pending_approvals(conn),
    )


def outbound_response(request: Request, conn: psycopg.Connection, status: int = 200, error: str | None = None) -> HTMLResponse:
    counts = views.outbound_counts(conn)
    return page(request, conn, "outbound.html", status=status, pending=views.outbound_pending(conn), done=views.outbound_done(conn),
                counts=counts, approvals_pending=counts["pending"], error=error)


@router.get("/machines", response_class=HTMLResponse)
def machines_page(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """One card per machine: specs, state, the workload select and the "..." menu."""
    return machines_response(request, conn)


@router.get("/machines/{machine_id}/logs", response_class=HTMLResponse)
def machine_logs(request: Request, machine_id: str, conn: psycopg.Connection = DB) -> HTMLResponse:
    """The last 200 log lines of a machine, newest last."""
    card = machine_or_404(conn, machine_id)
    lines = views.log_lines(conn, machine_id, _tz(conn))
    return page(request, conn, "machine_logs.html", m=card, lines=lines, approvals_pending=views.pending_approvals(conn))


@router.get("/machines/{machine_id}/unpin", response_class=HTMLResponse)
def unpin_confirm(request: Request, machine_id: str, conn: psycopg.Connection = DB) -> HTMLResponse:
    """The typed confirmation page: the exact phrase unpins the machine."""
    return unpin_response(request, conn, machine_id)


@router.get("/workloads", response_class=HTMLResponse)
def workloads_page(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """One row per workload."""
    return workloads_response(request, conn)


@router.get("/workloads/{name}", response_class=HTMLResponse)
def workload_page(request: Request, name: str, conn: psycopg.Connection = DB) -> HTMLResponse:
    """Manifest, image, machines, jobs, logs, secrets and pending approvals of one workload."""
    return workload_response(request, conn, name)


@router.get("/outbound", response_class=HTMLResponse)
def outbound_page(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """Pending outbound actions to approve or reject, and the done list."""
    return outbound_response(request, conn)


# The form posts import the page builders above, so their router is included here, after
# they exist (the same arrangement as host.api.dashboard and its trading pages).
from host.workloads import dashboard_forms  # noqa: E402

router.include_router(dashboard_forms.router)
