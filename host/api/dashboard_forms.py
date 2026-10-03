"""Dashboard form posts: thin wrappers over the JSON owner logic that redirect back.

Each handler reads an application/x-www-form-urlencoded body, calls the same
function the /api route calls, and redirects to the page it came from with a
flash message. Validation errors on the settings page re-render the form (400).
"""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from host import auth, kill, queue, web
from host.api.dashboard import page, settings_page
from host.api.deps import DB, get_config, require_owner
from host.api.owner import install_command
from host.config import Config
from host.errors import BadRequest, QueueError
from host.settings import set_settings
from host.settings_forms import parse_group

router = APIRouter(tags=["dashboard-forms"], dependencies=[Depends(require_owner)])


async def form_data(request: Request) -> dict[str, str]:
    """The posted form as plain strings (file fields are ignored)."""
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


FORM = Depends(form_data)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@router.post("/workers/{worker_id}/role")
def post_role(
    worker_id: str, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Set a worker's desired role (the role select)."""
    worker = queue.set_role(conn, worker_id, (form.get("role") or "").strip(), actor)
    return web.redirect("/", f"{worker['name']}: switching to {worker['desired_role']} (epoch {worker['role_epoch']})")


@router.post("/workers/{worker_id}/enabled")
def post_enabled(
    worker_id: str, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Enable or disable a worker."""
    worker = queue.set_enabled(conn, worker_id, _truthy(form.get("enabled")), actor)
    state = "enabled" if worker["enabled"] else "disabled"
    return web.redirect("/", f"{worker['name']} {state}")


def _job_params(form: dict[str, str]) -> dict[str, Any]:
    """Params for the send form; sleep takes seconds (default 60)."""
    if (form.get("kind") or "") == "sleep":
        text = (form.get("seconds") or "60").strip() or "60"
        try:
            seconds = int(text)
        except ValueError:
            raise BadRequest("seconds must be a whole number") from None
        if seconds < 1 or seconds > 86400:
            raise BadRequest("seconds must be between 1 and 86400")
        return {"seconds": seconds}
    return {}


@router.post("/jobs")
def post_job(form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """Send a job to any idle worker or a chosen one."""
    kind = (form.get("kind") or "").strip()
    target = (form.get("target") or queue.ANY_IDLE).strip() or queue.ANY_IDLE
    result = queue.create_job(conn, kind, _job_params(form), target, None, actor)
    job = result.job
    if result.waiting_for_idle_worker:
        note = "waiting for an idle worker"
    else:
        note = f"sent to {job['target_worker_id']}" if job["target_worker_id"] else "queued"
    return web.redirect("/jobs", f"{kind} job {str(job['id'])[:8]} created, {note}")


@router.post("/jobs/{job_id}/cancel")
def post_cancel(
    job_id: str, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Cancel a queued job or request cancellation of a leased one."""
    job = queue.cancel_job(conn, job_id, actor)
    back = web.safe_next(form.get("next"), "/jobs")
    return web.redirect(back, f"job {str(job['id'])[:8]} {job['status'].replace('_', ' ')}")


@router.post("/settings/{group}")
def post_settings(
    request: Request,
    group: str,
    form: dict[str, str] = FORM,
    actor: str = Depends(require_owner),
    config: Config = Depends(get_config),
    conn: psycopg.Connection = DB,
) -> Response:
    """Save one settings group; bad values re-render the page with the error (400)."""
    try:
        set_settings(conn, parse_group(group, form), actor)
    except BadRequest as exc:
        if group not in ("trading", "fleet", "tz"):
            raise
        conn.rollback()
        return settings_page(request, conn, config, errors={group: exc.message}, overrides=form, status=400)
    return web.redirect("/settings", f"{group} settings saved")


@router.post("/enroll-token")
def post_enroll_token(
    request: Request, config: Config = Depends(get_config), conn: psycopg.Connection = DB
) -> HTMLResponse:
    """Mint an enroll token and show it once with the install one-liners."""
    token, expires_at = auth.create_enroll_token(conn)
    url = config.public_url
    return page(
        request, conn, "enroll_token.html", token=token, expires_at=expires_at,
        arg_command=install_command(url, token),
        env_command=f"curl -fsSL {url}/install.sh | sudo FLEET_ENROLL_TOKEN={token} bash -s -- {url}",
    )


@router.post("/kill")
def post_kill(actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """The KILL button."""
    kill.set_kill(conn, actor)
    return web.redirect("/", "Trading killed. Reset in Settings.")


@router.post("/kill/reset")
def post_kill_reset(
    request: Request,
    form: dict[str, str] = FORM,
    actor: str = Depends(require_owner),
    config: Config = Depends(get_config),
    conn: psycopg.Connection = DB,
) -> Response:
    """The reset form on the settings page; the text must be exactly RESUME."""
    try:
        kill.reset_kill(conn, actor, form.get("confirm") or "")
    except QueueError as exc:
        conn.rollback()
        return settings_page(request, conn, config, errors={"kill": exc.message}, status=exc.status)
    return web.redirect("/settings", "Kill switch reset. Trading may resume.")
