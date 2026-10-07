"""Dashboard form posts: thin wrappers over the JSON owner logic that redirect back.

Each handler reads an application/x-www-form-urlencoded body, calls the same
function the /api route calls, and redirects to the page it came from with a
flash message. Validation errors on the settings page re-render the form (400).
"""
from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from host import auth, kill, queue, web
from host.api.dashboard import FORM, form_data, jobs_page_response, page, settings_page
from host.api.deps import DB, get_config, require_owner
from host.api.job_forms import parse_job_form
from host.api.owner import install_command
from host.config import Config
from host.eligibility import recompute_all, recompute_paper
from host.errors import BadRequest, Conflict, QueueError
from host.settings import set_settings
from host.settings_forms import GROUPS, parse_group

router = APIRouter(tags=["dashboard-forms"], dependencies=[Depends(require_owner)])

__all__ = ["FORM", "form_data", "router"]


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@router.post("/workers/{worker_id}/role")
def post_role(
    worker_id: str, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Set a worker's desired role (the role select)."""
    worker = queue.set_role(conn, worker_id, (form.get("role") or "").strip(), actor)
    return web.redirect("/fleet/list", f"{worker['name']}: switching to {worker['desired_role']} (epoch {worker['role_epoch']})")


@router.post("/workers/{worker_id}/enabled")
def post_enabled(
    worker_id: str, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Enable or disable a worker."""
    worker = queue.set_enabled(conn, worker_id, _truthy(form.get("enabled")), actor)
    state = "enabled" if worker["enabled"] else "disabled"
    return web.redirect("/fleet/list", f"{worker['name']} {state}")


@router.post("/jobs")
def post_job(
    request: Request, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Send a job to any idle worker or a chosen one; bad params re-render the page (400)."""
    target = (form.get("target") or queue.ANY_IDLE).strip() or queue.ANY_IDLE
    try:
        kind, params = parse_job_form(form)
        result = queue.create_job(conn, kind, params, target, None, actor)
    except BadRequest as exc:
        conn.rollback()
        return jobs_page_response(request, conn, status=400, error=exc.message, submitted=form)
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


# The live routes are declared before /settings/{group} so "live" is never read as a
# settings group: the switch moves only through the typed phrase (docs/LIVE.md).
@router.post("/settings/live")
def post_live_enable(
    request: Request,
    form: dict[str, str] = FORM,
    actor: str = Depends(require_owner),
    config: Config = Depends(get_config),
    conn: psycopg.Connection = DB,
) -> Response:
    """The typed enable form of the "Live trading" group: `confirm` must be today's
    exact phrase. A wrong phrase (400) or a failed precondition (409) re-renders the
    page with the error inline in the group and the typed text kept."""
    confirm = form.get("confirm") or ""
    try:
        from host.trading.live import enable_live
    except ImportError:
        return web.redirect("/settings#live", "live switch unavailable: host.trading.live is not installed")
    try:
        state = enable_live(conn, actor, confirm)
    except (BadRequest, Conflict) as exc:
        conn.rollback()
        return settings_page(request, conn, config, errors={"live": exc.message}, overrides={"live_confirm": confirm}, status=exc.status)
    balance = web.format_cents(state.get("balance_cents")) if state.get("balance_cents") is not None else "unknown"
    return web.redirect("/settings#live", f"Live trading enabled by {actor}. Exchange balance {balance}.")


@router.post("/settings/live/off")
def post_live_disable(actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> Response:
    """The "Disable live" button: immediate, live assignments halted and live orders
    cancelled through the exchange (host.trading.live.disable_live)."""
    try:
        from host.trading.live import disable_live
    except ImportError:
        return web.redirect("/settings#live", "live switch unavailable: host.trading.live is not installed")
    result = disable_live(conn, actor, "owner").get("live_off") or {}
    halted = result.get("assignments_halted", 0)
    halted = halted if isinstance(halted, int) else len(halted)
    return web.redirect(
        "/settings#live",
        f"Live trading disabled: {halted} live assignment{'' if halted == 1 else 's'} halted, "
        f"{result.get('orders_cancel_requested', 0)} orders cancel requested on the exchange.",
    )


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
        updates = parse_group(group, form)
        set_settings(conn, updates, actor)
    except BadRequest as exc:
        if group not in GROUPS:
            raise
        conn.rollback()
        return settings_page(request, conn, config, errors={group: exc.message}, overrides=form, status=400)
    if "thresholds_backtest" in updates:
        recompute_all(conn)
    if "thresholds_paper" in updates:
        for row in conn.execute("SELECT DISTINCT lineage_id FROM model_scores WHERE mode = 'paper'").fetchall():
            recompute_paper(conn, row["lineage_id"], actor)
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
