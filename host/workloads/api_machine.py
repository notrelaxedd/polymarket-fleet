"""Supervisor routes (docs/workloads-design.md section 5.1), machine bearer token."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from host.api.deps import DB, bearer, remote_ip
from host.api.serialize import jsonable
from host.workloads import machines, start as start_mod
from host.workloads.machine_specs import MAX_LOG_ENTRIES

router = APIRouter(prefix="/api/v1/machines", tags=["machines"])

NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
SHORT_RE = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"


class RegisterBody(BaseModel):
    """Either enroll_token or machine_id + machine_token."""

    model_config = ConfigDict(extra="ignore")
    enroll_token: str | None = Field(default=None, max_length=256)
    machine_id: str | None = Field(default=None, max_length=64)
    machine_token: str | None = Field(default=None, max_length=256)
    name: str | None = Field(default=None, pattern=NAME_RE)
    hostname: str | None = Field(default=None, pattern=NAME_RE)
    boot_id: str | None = Field(default=None, pattern=SHORT_RE)
    agent_version: str | None = Field(default=None, pattern=SHORT_RE)
    native_polymarket: str | None = Field(default=None, max_length=16)
    specs: dict[str, Any] = Field(default_factory=dict)


class HeartbeatBody(BaseModel):
    """Heartbeat payload; the host cleans every field again (host.workloads.machine_specs)."""

    model_config = ConfigDict(extra="ignore")
    specs: dict[str, Any] = Field(default_factory=dict)
    native_polymarket: str | None = Field(default=None, max_length=16)
    acked_epoch: int | None = None
    container: dict[str, Any] | None = None
    logs: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_LOG_ENTRIES * 5)
    cleanup: dict[str, Any] = Field(default_factory=dict)
    agent_version: str | None = Field(default=None, max_length=64)


class StartBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    epoch: int


@router.post("/register")
def register(body: RegisterBody, request: Request, conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Enroll with an enroll token, or re-register to rotate the machine token."""
    return jsonable(machines.register(conn, body.model_dump(), remote_ip(request)))


@router.post("/{machine_id}/heartbeat")
def heartbeat(
    machine_id: str, body: HeartbeatBody, request: Request,
    token: str = Depends(bearer), conn: psycopg.Connection = DB,
) -> dict[str, Any]:
    """Specs, container status and logs in; epoch, run block and secrets_version out."""
    machine = machines.verify_machine(conn, machine_id, token)
    return jsonable(machines.heartbeat(conn, machine, body.model_dump(), remote_ip(request)))


@router.post("/{machine_id}/start")
def start(
    machine_id: str, body: StartBody, response: Response,
    token: str = Depends(bearer), conn: psycopg.Connection = DB,
) -> dict[str, Any]:
    """A new run token and the container secrets for the current epoch (never cached, never logged)."""
    machine = machines.verify_machine(conn, machine_id, token)
    result = start_mod.start(conn, machine, body.epoch)
    response.headers["Cache-Control"] = "no-store"
    return result
