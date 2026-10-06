"""Owner routes for secrets, outbound approvals, workload jobs and machine logs (section 5.3).

Included by api_owner.router, which supplies the require_owner dependency.
"""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from host.api.deps import DB, remote_ip, require_owner
from host.api.serialize import jsonable
from host.errors import BadRequest
from host.events import add_audit
from host.workloads import machines, outbound, queue, secrets as wl_secrets
from host.workloads.machine_specs import MAX_LINE_CHARS

router = APIRouter()
JOB_STATUSES = ("queued", "leased", "cancel_requested", "succeeded", "failed", "cancelled")


class SecretBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    value: str = Field(max_length=wl_secrets.MAX_VALUE_BYTES)


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(default="", max_length=500)


class JobBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    workload: str
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    target: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)


def _public(job: dict[str, Any]) -> dict[str, Any]:
    """A job row without its lease token."""
    return {k: v for k, v in job.items() if k != "lease_token"}


@router.get("/workloads/{name}/secrets")
def get_secrets(name: str, conn: psycopg.Connection = DB) -> list[dict[str, Any]]:
    """Secret names, scope and whether each is set. Never a value."""
    return jsonable(wl_secrets.list_secret_names(conn, name))


@router.put("/workloads/{name}/secrets/{secret}")
def put_secret(name: str, secret: str, body: SecretBody, request: Request, actor: str = Depends(require_owner),
               conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Write one secret (write-only)."""
    return jsonable(wl_secrets.set_secret(conn, name, secret, body.value, actor, remote_ip(request)))


@router.delete("/workloads/{name}/secrets/{secret}", status_code=204)
def delete_secret(name: str, secret: str, request: Request, actor: str = Depends(require_owner),
                  conn: psycopg.Connection = DB) -> Response:
    """Delete one secret."""
    wl_secrets.delete_secret(conn, name, secret, actor, remote_ip(request))
    return Response(status_code=204)


@router.get("/outbound")
def list_outbound(status: str | None = Query(default=None), limit: int = Query(default=200, ge=1, le=500),
                  conn: psycopg.Connection = DB) -> list[dict[str, Any]]:
    """Outbound actions, newest first; ?status=pending for the approval queue."""
    if status is not None and status not in outbound.STATUSES:
        raise BadRequest(f"unknown status: {status!r}")
    rows = conn.execute(
        "SELECT * FROM outbound_actions WHERE (%(s)s::text IS NULL OR status = %(s)s) ORDER BY created_at DESC LIMIT %(n)s",
        {"s": status, "n": limit},
    ).fetchall()
    return jsonable(rows)


@router.post("/outbound/{action_id}/approve")
def post_approve(action_id: str, request: Request, actor: str = Depends(require_owner),
                 conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Approve a pending action; the host sends it on its next loop pass."""
    return jsonable(outbound.approve(conn, action_id, actor, remote_ip(request)))


@router.post("/outbound/{action_id}/reject")
def post_reject(action_id: str, body: RejectBody, request: Request, actor: str = Depends(require_owner),
                conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Reject a pending action."""
    return jsonable(outbound.reject(conn, action_id, actor, remote_ip(request), body.reason))


@router.post("/workload-jobs", status_code=201)
def post_job(body: JobBody, request: Request, response: Response, actor: str = Depends(require_owner),
             conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Create a workload job (201), or return the existing one for a reused idempotency key (200)."""
    job = queue.create_job(conn, workload=body.workload, kind=body.kind, params=body.params, target=body.target,
                           idempotency_key=body.idempotency_key)
    if not job["_created"]:
        response.status_code = 200
    else:
        add_audit(conn, "workload_job_create", str(job["id"]), actor, None,
                  {"workload": body.workload, "kind": body.kind, "target": body.target}, remote_ip(request))
    return jsonable(_public(job))


@router.get("/workload-jobs")
def list_jobs(workload: str | None = Query(default=None), status: str | None = Query(default=None),
              limit: int = Query(default=50, ge=1, le=500), conn: psycopg.Connection = DB) -> list[dict[str, Any]]:
    """Newest workload jobs first."""
    if status is not None and status not in JOB_STATUSES:
        raise BadRequest(f"unknown status: {status!r}")
    rows = conn.execute(
        """
        SELECT * FROM workload_jobs WHERE (%(w)s::text IS NULL OR workload = %(w)s)
           AND (%(s)s::text IS NULL OR status = %(s)s) ORDER BY created_at DESC LIMIT %(n)s
        """,
        {"w": workload, "s": status, "n": limit},
    ).fetchall()
    return jsonable([_public(r) for r in rows])


@router.get("/workload-jobs/{job_id}")
def get_job(job_id: str, conn: psycopg.Connection = DB) -> dict[str, Any]:
    """One job with its last 50 events (the lease token is not shown)."""
    job = queue.get_job(conn, job_id)
    events = conn.execute(
        "SELECT * FROM (SELECT * FROM workload_job_events WHERE job_id = %s ORDER BY id DESC LIMIT 50) e ORDER BY id",
        (job["id"],),
    ).fetchall()
    return jsonable({**_public(job), "events": events})


@router.post("/workload-jobs/{job_id}/cancel")
def post_cancel(job_id: str, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Cancel a queued job, or ask the machine running it to stop."""
    return jsonable(_public(queue.cancel(conn, job_id, actor)))


@router.get("/machines/{machine_id}/logs")
def get_logs(machine_id: str, limit: int = Query(default=200, ge=1, le=1000), conn: psycopg.Connection = DB) -> list[dict[str, Any]]:
    """The newest `limit` log lines of a machine, oldest first."""
    machines.get_machine(conn, machine_id)
    rows = conn.execute(
        "SELECT * FROM (SELECT ts, stream, workload, left(line, %s) AS line, id FROM machine_logs"
        " WHERE machine_id = %s ORDER BY id DESC LIMIT %s) l ORDER BY id",
        (MAX_LINE_CHARS, machine_id, limit),
    ).fetchall()
    return jsonable([{k: v for k, v in r.items() if k != "id"} for r in rows])

