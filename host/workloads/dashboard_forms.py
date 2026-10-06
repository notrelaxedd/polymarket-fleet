"""Form posts of the workloads pages (docs/workloads-design.md section 10): thin wrappers
over the same functions the owner JSON routes call (section 5.3). Each reads a form body,
calls the function, and redirects (303) with a flash; a refused change (409 pinned, 422
placement, 400 bad phrase, ...) rolls the transaction back and re-renders the page with
the error text and the contract's status, so nothing has changed.

The host modules (assign, pinning, secrets, outbound, queue, machines, registry) are
imported inside each handler so the pages render without them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from host import web
from host.api.dashboard import FORM, page
from host.api.deps import DB, get_config, remote_ip, require_owner
from host.config import Config
from host.errors import BadRequest, NotFound, QueueError
from host.events import add_audit
from host.workloads import views
from host.workloads.dashboard import (
    machines_response, outbound_response, unpin_response, workload_response, workloads_dir,
)

router = APIRouter(tags=["workloads-forms"], dependencies=[Depends(require_owner)])
Form = dict[str, str]


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _machine_name(conn: psycopg.Connection, machine_id: str) -> str:
    row = conn.execute("SELECT name FROM machines WHERE id = %s", (machine_id,)).fetchone()
    if row is None:
        raise NotFound(f"machine {machine_id} not found")
    return row["name"]


def _machines_error(request: Request, conn: psycopg.Connection, exc: QueueError) -> HTMLResponse:
    """A refused machine change: the transaction is rolled back, the grid shows why."""
    conn.rollback()
    return machines_response(request, conn, status=exc.status, error=exc.message)


@router.post("/machines/{machine_id}/assign")
def post_assign(request: Request, machine_id: str, form: Form = FORM, actor: str = Depends(require_owner),
                conn: psycopg.Connection = DB) -> Response:
    """The workload select: assign a workload to a machine, or "none"."""
    from host.workloads import assign as assign_mod

    value = (form.get("workload") or "").strip()
    target = None if value in ("", "none") else value
    try:
        row = assign_mod.assign(conn, machine_id, target, actor, remote_ip(request))
    except NotFound:
        raise
    except QueueError as exc:
        return _machines_error(request, conn, exc)
    name = _machine_name(conn, machine_id)
    if target is None:
        note = f"{name}: nothing assigned"
    elif isinstance(row, dict) and row.get("state") == "draining":
        note = f"{name}: draining the Polymarket worker, then {target}"
    else:
        note = f"{name}: assigned {target}"
    return web.redirect("/machines", note)


@router.post("/machines/{machine_id}/pin")
def post_pin(request: Request, machine_id: str, form: Form = FORM, actor: str = Depends(require_owner),
             conn: psycopg.Connection = DB) -> Response:
    """Pin a machine by hand: no assignment changes until it is unpinned."""
    from host.workloads import pinning

    reason = (form.get("reason") or "").strip() or "owner pin"
    try:
        pinning.pin(conn, machine_id, reason, actor, remote_ip(request))
    except NotFound:
        raise
    except QueueError as exc:
        return _machines_error(request, conn, exc)
    return web.redirect("/machines", f"{_machine_name(conn, machine_id)}: pinned")


@router.post("/machines/{machine_id}/unpin")
def post_unpin(request: Request, machine_id: str, form: Form = FORM, actor: str = Depends(require_owner),
               conn: psycopg.Connection = DB) -> Response:
    """The typed confirmation: `confirm` must be exactly "UNPIN <name>" (400 otherwise, 409 while live)."""
    from host.workloads import pinning

    typed = form.get("confirm") or ""
    try:
        pinning.unpin(conn, machine_id, typed, actor, remote_ip(request))
    except NotFound:
        raise
    except QueueError as exc:
        conn.rollback()
        return unpin_response(request, conn, machine_id, status=exc.status, error=exc.message, typed=typed)
    return web.redirect("/machines", f"{_machine_name(conn, machine_id)}: unpinned")


@router.post("/machines/{machine_id}/disk-type")
def post_disk_type(request: Request, machine_id: str, form: Form = FORM, actor: str = Depends(require_owner),
                   conn: psycopg.Connection = DB) -> Response:
    """Override the detected disk type (blank or "auto" goes back to the detected one)."""
    value = (form.get("disk_type") or "").strip().lower()
    override = None if value in ("", "auto") else value
    try:
        if override is not None and override not in views.DISK_TYPES:
            raise BadRequest(f"disk type must be one of {', '.join(views.DISK_TYPES)} or auto")
        old = conn.execute("SELECT disk_type_override AS v FROM machines WHERE id = %s FOR UPDATE", (machine_id,)).fetchone()
        if old is None:
            raise NotFound(f"machine {machine_id} not found")
        conn.execute("UPDATE machines SET disk_type_override = %s WHERE id = %s", (override, machine_id))
    except NotFound:
        raise
    except QueueError as exc:
        return _machines_error(request, conn, exc)
    add_audit(conn, "machine_disk_type", machine_id, actor, {"disk_type": old["v"]}, {"disk_type": override}, remote_ip(request))
    return web.redirect("/machines", f"{_machine_name(conn, machine_id)}: disk type {override or 'auto'}")


@router.post("/machines/{machine_id}/enabled")
def post_machine_enabled(request: Request, machine_id: str, form: Form = FORM, actor: str = Depends(require_owner),
                         conn: psycopg.Connection = DB) -> Response:
    """Enable or disable a machine."""
    enabled = _truthy(form.get("enabled"))
    old = conn.execute("SELECT name, enabled FROM machines WHERE id = %s FOR UPDATE", (machine_id,)).fetchone()
    if old is None:
        raise NotFound(f"machine {machine_id} not found")
    conn.execute("UPDATE machines SET enabled = %s WHERE id = %s", (enabled, machine_id))
    add_audit(conn, "machine_enabled", machine_id, actor, {"enabled": old["enabled"]}, {"enabled": enabled}, remote_ip(request))
    return web.redirect("/machines", f"{old['name']} {'enabled' if enabled else 'disabled'}")


@router.post("/machine-enroll-token")
def post_machine_enroll_token(request: Request, config: Config = Depends(get_config), conn: psycopg.Connection = DB) -> HTMLResponse:
    """Mint a machine enroll token and show it once with the install one-liner."""
    from host.workloads import machines as machines_mod

    created = machines_mod.create_enroll_token(conn)
    url = config.public_url.rstrip("/")
    return page(request, conn, "machine_enroll_token.html", token=created["token"], expires_at=created["expires_at"],
                command=f"curl -fsSL {url}/install-agent.sh | sudo bash -s -- {url} {created['token']}",
                approvals_pending=views.pending_approvals(conn))


@router.post("/workloads/sync")
def post_sync(actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """Read every workload.toml under FLEET_WORKLOADS_DIR into the workloads table."""
    from host.workloads import registry

    result = registry.sync_from_dir(conn, Path(workloads_dir()))
    errors: dict[str, Any] = result.get("errors") or {}
    note = f"synced {len(result.get('synced') or [])} workloads"
    if errors:
        note += "; errors: " + "; ".join(f"{n}: {(p[0] if isinstance(p, list) and p else p)}" for n, p in errors.items())
    return web.redirect("/workloads", note)


@router.post("/workloads/{name}/enabled")
def post_workload_enabled(request: Request, name: str, form: Form = FORM, actor: str = Depends(require_owner),
                          conn: psycopg.Connection = DB) -> Response:
    """Enable or disable a workload (a disabled one cannot be assigned)."""
    enabled = _truthy(form.get("enabled"))
    old = conn.execute("SELECT enabled FROM workloads WHERE name = %s FOR UPDATE", (name,)).fetchone()
    if old is None:
        raise NotFound(f"workload {name} not found")
    conn.execute("UPDATE workloads SET enabled = %s, updated_at = now() WHERE name = %s", (enabled, name))
    add_audit(conn, "workload_enabled", name, actor, {"enabled": old["enabled"]}, {"enabled": enabled}, remote_ip(request))
    return web.redirect("/workloads", f"{name} {'enabled' if enabled else 'disabled'}")


@router.post("/workloads/{name}/secrets")
def post_secret(request: Request, name: str, form: Form = FORM, actor: str = Depends(require_owner),
                conn: psycopg.Connection = DB) -> Response:
    """Set (`name`, `value`) or delete (`name`, `delete=1`) a secret. Write-only: the value
    is never shown again, and a rejected form is re-rendered without it."""
    from host.workloads import secrets

    secret = (form.get("name") or "").strip()
    ip = remote_ip(request)
    try:
        if _truthy(form.get("delete")):
            secrets.delete_secret(conn, name, secret, actor, ip)
            note = f"{secret} deleted"
        else:
            value = form.get("value") or ""
            if not value:
                raise BadRequest("a value is required")
            secrets.set_secret(conn, name, secret, value, actor, ip)
            note = f"{secret} saved"
    except NotFound:
        raise
    except QueueError as exc:
        conn.rollback()
        return workload_response(request, conn, name, status=exc.status, error=exc.message, open_section="secrets")
    return web.redirect(f"/workloads/{name}", note)


@router.post("/workloads/{name}/jobs")
def post_workload_job(request: Request, name: str, form: Form = FORM, actor: str = Depends(require_owner),
                      conn: psycopg.Connection = DB) -> Response:
    """Send a job to a jobs-mode workload: any machine, or a chosen one."""
    from host.workloads import queue as wl_queue

    kind = (form.get("kind") or "").strip()
    target = (form.get("target") or "").strip()
    target = None if target in ("", "any") else target
    try:
        text = (form.get("params") or "").strip() or "{}"
        try:
            params = json.loads(text)
        except ValueError as exc:
            raise BadRequest(f"params is not valid JSON: {exc}") from exc
        if not isinstance(params, dict):
            raise BadRequest("params must be a JSON object")
        job = wl_queue.create_job(conn, workload=name, kind=kind, params=params, target=target)
    except NotFound:
        raise
    except QueueError as exc:
        conn.rollback()
        return workload_response(request, conn, name, status=exc.status, error=exc.message, open_section="send",
                                 submitted={"kind": kind, "target": target or "", "params": form.get("params") or ""})
    return web.redirect(f"/workloads/{name}", f"{kind} job {str(job['id'])[:8]} queued")


@router.post("/workloads/{name}/jobs/{job_id}/cancel")
def post_workload_job_cancel(name: str, job_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """Cancel a queued job, or ask a running one to stop."""
    from host.workloads import queue as wl_queue

    job = wl_queue.cancel(conn, job_id, actor)
    return web.redirect(f"/workloads/{name}", f"job {str(job['id'])[:8]} {str(job['status']).replace('_', ' ')}")


@router.post("/outbound/{action_id}/approve")
def post_approve(request: Request, action_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """Approve a pending outbound action; the host sends it on its next pass."""
    from host.workloads import outbound

    try:
        outbound.approve(conn, action_id, actor, remote_ip(request))
    except NotFound:
        raise
    except QueueError as exc:
        conn.rollback()
        return outbound_response(request, conn, status=exc.status, error=exc.message)
    return web.redirect("/outbound", f"approved {action_id[:8]}")


@router.post("/outbound/{action_id}/reject")
def post_reject(request: Request, action_id: str, form: Form = FORM, actor: str = Depends(require_owner),
                conn: psycopg.Connection = DB) -> Response:
    """Reject a pending outbound action (the reason is optional)."""
    from host.workloads import outbound

    try:
        outbound.reject(conn, action_id, actor, remote_ip(request), (form.get("reason") or "").strip())
    except NotFound:
        raise
    except QueueError as exc:
        conn.rollback()
        return outbound_response(request, conn, status=exc.status, error=exc.message)
    return web.redirect("/outbound", f"rejected {action_id[:8]}")
