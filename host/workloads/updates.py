"""Applying a synced manifest or a newly published image to machines.

A machine runs the manifest and image digest snapshotted on its assignment when it was
assigned (assign._switch). A sync or a publish only changes the workloads row; nothing
running changes until the owner applies the update to a machine, or rolls it out to every
machine that runs the workload. Applying goes through the same refusals as an assign
(pinned or live 409, native 409, placement 422) and, for polymarket, through the same drain
(the worker is set idle, its trades released) before the container is replaced.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.errors import Conflict, QueueError
from host.workloads import assign as assign_mod
from host.workloads.registry import get_workload, run_shape


def is_outdated(assignment: dict[str, Any], workload_row: dict[str, Any]) -> bool:
    """True when the machine's snapshot differs from the workload's synced manifest or
    published image (only for a machine that runs that workload)."""
    if assignment.get("workload") != workload_row["name"]:
        return False
    if assignment.get("run_manifest") is None:
        return False  # no snapshot yet: it already follows the workload row
    if workload_row["image_digest"] and assignment.get("run_image_digest") != workload_row["image_digest"]:
        return True
    return run_shape(assignment["run_manifest"]) != run_shape(workload_row["manifest"])


def outdated(conn: psycopg.Connection, workload: str) -> list[str]:
    """Machine ids that run `workload` on an older manifest or image than the synced one."""
    row = get_workload(conn, workload)
    rows = conn.execute(
        "SELECT * FROM workload_assignments WHERE workload = %s AND state <> 'draining' ORDER BY machine_id",
        (workload,),
    ).fetchall()
    return [a["machine_id"] for a in rows if is_outdated(a, row)]


def apply_update(conn: psycopg.Connection, machine_id: str, actor: str | None, ip: str | None) -> dict[str, Any]:
    """Move one machine to its workload's current manifest and image (a new epoch).

    Returns the assignment row ("up_to_date": True when there was nothing to apply). 409
    when nothing is assigned or a drain is running, plus every refusal of an assign."""
    machine = assign_mod._lock_machine(conn, machine_id)
    current = assign_mod._lock_assignment(conn, machine_id)
    if current["workload"] is None:
        raise Conflict("nothing is assigned to this machine")
    if current["state"] == "draining":
        raise Conflict("machine is draining; wait for the drain to finish")
    if not is_outdated(current, get_workload(conn, current["workload"])):
        return {**current, "up_to_date": True}
    machine = assign_mod.guard_move(conn, machine, current, current["workload"])
    return assign_mod._move(conn, machine, current, current["workload"], actor, ip, "workload_update")


def rollout(conn: psycopg.Connection, workload: str, actor: str | None, ip: str | None) -> dict[str, Any]:
    """apply_update on every outdated machine of `workload`, each in its own savepoint.

    Returns {"updated": [names], "draining": [names], "skipped": {name: reason}}: a pinned,
    live or refused machine is skipped with its reason and keeps running what it runs."""
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM machines").fetchall()}
    out: dict[str, Any] = {"updated": [], "draining": [], "skipped": {}}
    for machine_id in outdated(conn, workload):
        name = names.get(machine_id, machine_id)
        try:
            with conn.transaction():
                row = apply_update(conn, machine_id, actor, ip)
        except QueueError as exc:
            out["skipped"][name] = exc.message
            continue
        out["draining" if row["state"] == "draining" else "updated"].append(name)
    return out
