"""Container routes (section 5.2), run-token bearer: claim, renew, release, complete, fail, outbound."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator

from host.api.deps import DB, bearer
from host.api.limits import small_payload
from host.api.serialize import jsonable
from host.errors import NotFound
from host.workloads import outbound, queue, tokens

router = APIRouter(prefix="/api/v1/wl", tags=["workload-run"])
MAX_ERROR_CHARS = 16 * 1024


def run_scope(token: str = Depends(bearer), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """The (machine_id, workload, epoch) a run token is scoped to; 401 for any other token."""
    return tokens.run_scope(conn, token)


class ClaimBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kinds: list[str] | None = Field(default=None, max_length=32)


class BeatBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    lease_token: str = Field(max_length=64)
    progress: float | None = Field(default=None, allow_inf_nan=False)
    checkpoint: dict[str, Any] | None = None

    @field_validator("checkpoint")
    @classmethod
    def _small(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return small_payload(value, "checkpoint")


class ReleaseBody(BeatBody):
    reason: str | None = Field(default=None, max_length=32)


class CompleteBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    lease_token: str = Field(max_length=64)
    result: dict[str, Any] = Field(default_factory=dict)

    @field_validator("result")
    @classmethod
    def _small(cls, value: dict[str, Any]) -> dict[str, Any]:
        return small_payload(value, "result")


class FailBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    lease_token: str = Field(max_length=64)
    error: str = Field(default="", max_length=MAX_ERROR_CHARS)


class OutboundBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str = Field(max_length=32)
    payload: dict[str, Any]
    dedupe_key: str = Field(min_length=1, max_length=200)
    job_id: str | None = Field(default=None, max_length=64)


def _own_job(conn: psycopg.Connection, scope: dict[str, Any], job_id: str) -> None:
    """404 unless the job belongs to the token's workload (never confirm other workloads' ids)."""
    job = queue.get_job(conn, job_id)
    if job["workload"] != scope["workload"]:
        raise NotFound("job not found")


@router.post("/claim")
def claim(body: ClaimBody, scope: dict[str, Any] = Depends(run_scope), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Lease one queued job of this workload (only kinds its manifest declares)."""
    row = queue.claim(conn, machine_id=scope["machine_id"], workload=scope["workload"], epoch=scope["epoch"], kinds=body.kinds)
    if row is None:
        return {"job": None}
    return {"job": jsonable({
        "id": row["id"], "kind": row["kind"], "params": row["params"], "checkpoint": row["checkpoint"],
        "progress": row["progress"], "lease_token": row["lease_token"], "lease_seconds": row["lease_seconds"],
    })}


@router.post("/jobs/{job_id}/heartbeat")
def job_heartbeat(job_id: str, body: BeatBody, scope: dict[str, Any] = Depends(run_scope),
                  conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Renew the lease and store progress; `cancel` true means stop and release."""
    _own_job(conn, scope, job_id)
    return queue.renew(conn, job_id=job_id, lease_token=body.lease_token, machine_id=scope["machine_id"],
                       epoch=scope["epoch"], progress=body.progress, checkpoint=body.checkpoint)


@router.post("/jobs/{job_id}/release")
def job_release(job_id: str, body: ReleaseBody, scope: dict[str, Any] = Depends(run_scope),
                conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Hand the job back (queued again, or cancelled when cancel was requested)."""
    _own_job(conn, scope, job_id)
    row = queue.release(conn, job_id=job_id, lease_token=body.lease_token, machine_id=scope["machine_id"],
                        epoch=scope["epoch"], checkpoint=body.checkpoint, progress=body.progress, reason=body.reason)
    return {"status": row["status"]}


@router.post("/jobs/{job_id}/complete")
def job_complete(job_id: str, body: CompleteBody, scope: dict[str, Any] = Depends(run_scope),
                 conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Succeed the job (idempotent)."""
    _own_job(conn, scope, job_id)
    row = queue.complete(conn, job_id=job_id, lease_token=body.lease_token, machine_id=scope["machine_id"],
                         epoch=scope["epoch"], result=body.result)
    return {"status": row["status"]}


@router.post("/jobs/{job_id}/fail")
def job_fail(job_id: str, body: FailBody, scope: dict[str, Any] = Depends(run_scope),
             conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Fail the job for good."""
    _own_job(conn, scope, job_id)
    row = queue.fail(conn, job_id=job_id, lease_token=body.lease_token, machine_id=scope["machine_id"],
                     epoch=scope["epoch"], error=body.error)
    return {"status": row["status"]}


@router.post("/outbound")
def post_outbound(body: OutboundBody, scope: dict[str, Any] = Depends(run_scope),
                  conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Queue an outbound action; it waits for the owner's approval."""
    row = outbound.queue_action(conn, workload=scope["workload"], machine_id=scope["machine_id"], job_id=body.job_id,
                                kind=body.kind, payload=body.payload, dedupe_key=body.dedupe_key)
    return {"id": str(row["id"]), "status": row["status"]}


@router.get("/outbound/{action_id}")
def get_outbound(action_id: str, scope: dict[str, Any] = Depends(run_scope), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Status of one of this workload's actions (404 for any other workload's)."""
    row = outbound.get_action(conn, action_id)
    if row["workload"] != scope["workload"]:
        raise NotFound("outbound action not found")
    return jsonable({"id": row["id"], "status": row["status"], "error": row["error"], "result": row["result"]})
