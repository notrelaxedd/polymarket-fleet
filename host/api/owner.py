"""Owner routes: fleet view, roles, jobs, enroll tokens, settings, health."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict

from host import auth, queue, views
from host.api.deps import DB, get_config, get_pool, require_owner
from host.api.serialize import jsonable, public_worker
from host.config import Config
from host.errors import BadRequest
from host.settings import get_settings, set_settings

router = APIRouter(prefix="/api", tags=["owner"], dependencies=[Depends(require_owner)])
health_router = APIRouter(tags=["health"])


class RoleBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: str


class EnabledBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool


class JobBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str
    params: dict[str, Any] = {}
    target: str | None = None
    idempotency_key: str | None = None


def install_command(public_url: str, token: str) -> str:
    """The one-liner printed next to a fresh enroll token."""
    return f"curl -fsSL {public_url}/install.sh | sudo bash -s -- {public_url} {token}"


@router.get("/fleet")
def get_fleet(conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Workers, public settings and server time."""
    return jsonable(views.fleet(conn))


@router.post("/workers/{worker_id}/role")
def post_role(
    worker_id: str,
    body: RoleBody,
    actor: str = Depends(require_owner),
    conn: psycopg.Connection = DB,
) -> dict[str, Any]:
    """Change a worker's desired role."""
    return public_worker(queue.set_role(conn, worker_id, body.role, actor))


@router.post("/workers/{worker_id}/enabled")
def post_enabled(
    worker_id: str,
    body: EnabledBody,
    actor: str = Depends(require_owner),
    conn: psycopg.Connection = DB,
) -> dict[str, Any]:
    """Enable or disable a worker."""
    return public_worker(queue.set_enabled(conn, worker_id, body.enabled, actor))


@router.post("/jobs", status_code=201)
def post_job(
    body: JobBody,
    response: Response,
    actor: str = Depends(require_owner),
    conn: psycopg.Connection = DB,
) -> dict[str, Any]:
    """Create a job (201), or return the existing one for a reused idempotency key (200)."""
    result = queue.create_job(conn, body.kind, body.params, body.target, body.idempotency_key, actor)
    if not result.created:
        response.status_code = 200
    job = jsonable(dict(result.job))
    if result.waiting_for_idle_worker:
        job["waiting_for_idle_worker"] = True
    return job


@router.get("/jobs")
def get_jobs(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    conn: psycopg.Connection = DB,
) -> list[dict[str, Any]]:
    """Newest jobs first."""
    if status and status not in views.JOB_STATUSES:
        raise BadRequest(f"unknown status: {status!r}")
    return jsonable(views.list_jobs(conn, status, limit))


@router.get("/jobs/{job_id}")
def get_job(job_id: str, conn: psycopg.Connection = DB) -> dict[str, Any]:
    """One job with its last 50 events."""
    return jsonable(views.job_with_events(conn, job_id))


@router.post("/jobs/{job_id}/cancel")
def post_cancel(
    job_id: str,
    actor: str = Depends(require_owner),
    conn: psycopg.Connection = DB,
) -> dict[str, Any]:
    """Cancel a queued job or request cancellation of a leased one."""
    return jsonable(dict(queue.cancel_job(conn, job_id, actor)))


@router.post("/enroll-token")
def post_enroll_token(
    config: Config = Depends(get_config),
    conn: psycopg.Connection = DB,
) -> dict[str, Any]:
    """Mint a single-use enroll token valid for one hour."""
    token, expires_at = auth.create_enroll_token(conn)
    return jsonable(
        {"token": token, "expires_at": expires_at, "install_command": install_command(config.public_url, token)}
    )


@router.get("/settings")
def get_settings_route(conn: psycopg.Connection = DB) -> dict[str, Any]:
    """All settings."""
    return get_settings(conn)


@router.post("/settings")
def post_settings(
    body: dict[str, Any], actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> dict[str, Any]:
    """Update settings; unknown keys, wrong types and out-of-range values are 400."""
    return set_settings(conn, body, actor)


@health_router.get("/healthz")
def healthz(response: Response, pool=Depends(get_pool)) -> dict[str, Any]:
    """Liveness plus a database round trip."""
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return {"ok": True, "db": True}
    except Exception:  # noqa: BLE001 - any failure means unhealthy
        response.status_code = 503
        return {"ok": False, "db": False}
