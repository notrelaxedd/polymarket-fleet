"""Owner routes for workloads and machines (section 5.3). Every write is audited by the
function it calls (or here, for the enroll token)."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from host.api.deps import DB, get_config, remote_ip, require_owner
from host.api.serialize import jsonable
from host.config import Config
from host.events import add_audit
from host.settings import get_int_setting
from host.workloads import assign, config as wl_config, machines, pinning, registry, updates
from host.workloads.api_owner_ops import router as ops_router
from host.workloads.placement import check_placement, effective_disk_type

router = APIRouter(prefix="/api", tags=["workloads-owner"], dependencies=[Depends(require_owner)])


class EnabledBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool


class AssignBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    workload: str | None = None


class PinBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(default="", max_length=500)


class UnpinBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    confirm: str = Field(default="", max_length=200)


class DiskTypeBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    disk_type: str | None = None


def public_workload(conn: psycopg.Connection, row: dict[str, Any]) -> dict[str, Any]:
    """A workloads row plus how many machines run it and how many jobs wait."""
    running = conn.execute(
        "SELECT machine_id FROM workload_assignments WHERE workload = %s ORDER BY machine_id", (row["name"],)
    ).fetchall()
    queued = conn.execute(
        "SELECT count(*) AS n FROM workload_jobs WHERE workload = %s AND status = 'queued'", (row["name"],)
    ).fetchone()["n"]
    return {**row, "machines": [r["machine_id"] for r in running], "queued_jobs": queued,
            "image_published": row["image_digest"] is not None}


def machine_view(conn: psycopg.Connection, machine: dict[str, Any], assignment: dict[str, Any] | None,
                 workloads: list[dict[str, Any]], online_after: int) -> dict[str, Any]:
    """One machine for the owner: row, assignment, effective disk type, online flag, refusals."""
    age = None
    if machine["last_heartbeat_at"] is not None:
        age = conn.execute("SELECT extract(epoch FROM now() - %s) AS s", (machine["last_heartbeat_at"],)).fetchone()["s"]
    placement = {
        w["name"]: [{"code": r.code, "message": r.message} for r in check_placement(
            registry.manifest_of(w), machine, image_size_mb=w["image_size_mb"],
            image_published=w["image_digest"] is not None, workload_enabled=w["enabled"])]
        for w in workloads
    }
    a = {k: v for k, v in (assignment or {}).items() if k != "run_token_hash"}
    by_name = {w["name"]: w for w in workloads}
    current = by_name.get(a.get("workload")) if a else None
    return {**machines.public_machine(machine), "assignment": a, "effective_disk_type": effective_disk_type(machine),
            "online": age is not None and age <= online_after, "placement": placement,
            "update_available": bool(current and updates.is_outdated(a, current))}


@router.get("/workloads")
def list_workloads(conn: psycopg.Connection = DB) -> list[dict[str, Any]]:
    """Every workload with its image state, machines and queued jobs."""
    rows = conn.execute("SELECT * FROM workloads ORDER BY name").fetchall()
    return jsonable([public_workload(conn, r) for r in rows])


@router.post("/workloads/sync")
def post_sync(request: Request, actor: str = Depends(require_owner), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Re-read the manifests under FLEET_WORKLOADS_DIR."""
    result = registry.sync_from_dir(conn, wl_config.workloads_dir())
    add_audit(conn, "workloads_sync", None, actor, None, {"synced": result["synced"], "errors": sorted(result["errors"])},
              remote_ip(request))
    return result


@router.get("/workloads/{name}")
def get_workload(name: str, conn: psycopg.Connection = DB) -> dict[str, Any]:
    """One workload."""
    return jsonable(public_workload(conn, registry.get_workload(conn, name)))


@router.post("/workloads/{name}/enabled")
def post_workload_enabled(name: str, body: EnabledBody, request: Request, actor: str = Depends(require_owner),
                          conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Enable or disable a workload."""
    return jsonable(public_workload(conn, registry.set_enabled(conn, name, body.enabled, actor, remote_ip(request))))


@router.get("/machines")
def list_machines(conn: psycopg.Connection = DB) -> list[dict[str, Any]]:
    """Every machine with its assignment and, per workload, the placement refusals."""
    online_after = get_int_setting(conn, "online_after_seconds", 15)
    workloads = conn.execute("SELECT * FROM workloads ORDER BY name").fetchall()
    assignments = {a["machine_id"]: a for a in conn.execute("SELECT * FROM workload_assignments").fetchall()}
    rows = conn.execute("SELECT * FROM machines ORDER BY registered_at, id").fetchall()
    return jsonable([machine_view(conn, m, assignments.get(m["id"]), workloads, online_after) for m in rows])


@router.post("/machines/{machine_id}/assign")
def post_assign(machine_id: str, body: AssignBody, request: Request, actor: str = Depends(require_owner),
                conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Assign a workload (or null) to a machine: 404, 409 (pinned, native) or 422 (placement codes in detail)."""
    row = assign.assign(conn, machine_id, body.workload, actor, remote_ip(request))
    return jsonable({k: v for k, v in row.items() if k != "run_token_hash"})


@router.post("/machines/{machine_id}/update")
def post_update(machine_id: str, request: Request, actor: str = Depends(require_owner),
                conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Apply the workload's synced manifest and published image to this machine."""
    row = updates.apply_update(conn, machine_id, actor, remote_ip(request))
    return jsonable({k: v for k, v in row.items() if k != "run_token_hash"})


@router.post("/workloads/{name}/rollout")
def post_rollout(name: str, request: Request, actor: str = Depends(require_owner),
                 conn: psycopg.Connection = DB) -> dict[str, Any]:
    """apply_update on every machine of the workload that runs an older manifest or image."""
    registry.get_workload(conn, name)
    return updates.rollout(conn, name, actor, remote_ip(request))


@router.post("/machines/{machine_id}/pin")
def post_pin(machine_id: str, body: PinBody, request: Request, actor: str = Depends(require_owner),
             conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Pin a machine."""
    return jsonable(machines.public_machine(pinning.pin(conn, machine_id, body.reason, actor, remote_ip(request))))


@router.post("/machines/{machine_id}/unpin")
def post_unpin(machine_id: str, body: UnpinBody, request: Request, actor: str = Depends(require_owner),
               conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Unpin with the typed phrase `UNPIN <name>`."""
    return jsonable(machines.public_machine(pinning.unpin(conn, machine_id, body.confirm, actor, remote_ip(request))))


@router.post("/machines/{machine_id}/disk-type")
def post_disk_type(machine_id: str, body: DiskTypeBody, request: Request, actor: str = Depends(require_owner),
                   conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Override the detected disk type (null clears the override)."""
    return jsonable(machines.public_machine(machines.set_disk_type(conn, machine_id, body.disk_type, actor, remote_ip(request))))


@router.post("/machines/{machine_id}/enabled")
def post_machine_enabled(machine_id: str, body: EnabledBody, request: Request, actor: str = Depends(require_owner),
                         conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Enable or disable a machine."""
    return jsonable(machines.public_machine(machines.set_enabled(conn, machine_id, body.enabled, actor, remote_ip(request))))


@router.post("/machine-enroll-token")
def post_enroll_token(request: Request, actor: str = Depends(require_owner), cfg: Config = Depends(get_config),
                      conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Mint a single-use machine enroll token valid for one hour."""
    result = machines.create_enroll_token(conn)
    add_audit(conn, "machine_enroll_token", None, actor, None, {"expires_at": result["expires_at"].isoformat()},
              remote_ip(request))
    cmd = f"curl -fsSL {cfg.public_url}/install-agent.sh | sudo bash -s -- {cfg.public_url} {result['token']}"
    return jsonable({**result, "install_command": cmd})


router.include_router(ops_router)
