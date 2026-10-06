"""Assigning a workload to a machine, the "run" block a supervisor receives, and drains."""
from __future__ import annotations

from typing import Any

import psycopg

from host import scheduling
from host.config import Config
from host.errors import Conflict, NotFound
from host.events import add_audit
from host.settings import get_int_setting
from host.workloads import pinning, queue
from host.workloads.errors import Unplaceable
from host.workloads.placement import check_placement
from host.workloads.registry import get_workload, image_ref, manifest_of

POLYMARKET = "polymarket"


def _snapshot(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("workload", "epoch", "state", "draining_to")
    return {k: row.get(k) for k in keys}


def _lock_machine(conn: psycopg.Connection, machine_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM machines WHERE id = %s FOR UPDATE", (machine_id,)).fetchone()
    if row is None:
        raise NotFound(f"unknown machine: {machine_id!r}")
    return row


def _lock_assignment(conn: psycopg.Connection, machine_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM workload_assignments WHERE machine_id = %s FOR UPDATE", (machine_id,)).fetchone()
    if row is None:
        row = conn.execute(
            "INSERT INTO workload_assignments (machine_id) VALUES (%s) RETURNING *", (machine_id,)
        ).fetchone()
    return row


def _switch(conn: psycopg.Connection, machine_id: str, target: str | None, actor: str | None) -> dict[str, Any]:
    """Move to `target` now: new epoch, no run token, leased jobs of this machine released."""
    row = conn.execute(
        """
        UPDATE workload_assignments SET workload = %s, epoch = epoch + 1,
               state = CASE WHEN %s::text IS NULL THEN 'stopped' ELSE 'pending' END,
               draining_to = NULL, draining_to_set = false, run_token_hash = NULL,
               assigned_by = %s, assigned_at = now(), last_error = NULL, updated_at = now()
         WHERE machine_id = %s RETURNING *
        """,
        (target, target, actor, machine_id),
    ).fetchone()
    queue.release_machine_jobs(conn, machine_id)
    return row


def assign(
    conn: psycopg.Connection, machine_id: str, workload: str | None, actor: str | None, ip: str | None
) -> dict[str, Any]:
    """Assign `workload` (None = nothing) to a machine; returns the assignment row.

    Refusals in contract order: unknown machine 404, same workload no-op, pinned 409,
    native fleet-worker active 409, unknown workload 404, placement 422.
    """
    machine = _lock_machine(conn, machine_id)
    current = _lock_assignment(conn, machine_id)
    draining = current["state"] == "draining"
    if draining and workload == current["workload"]:
        raise Conflict(f"machine is draining {workload}; wait for the drain to finish")
    if workload == current["workload"] and not draining:
        return current
    if draining and workload == current["draining_to"]:
        return current
    from host.workloads import machines as wl_machines  # machines imports this module

    machine = {**machine, "polymarket_worker_id": wl_machines.link_polymarket_worker(conn, machine)}
    if machine["pinned"]:
        raise Conflict(f"pinned: {machine['pinned_reason'] or 'pinned'}; unpin first")
    if pinning.is_live_trading(conn, machine_id):
        # The loop pins it within one pass; never wait for that to protect a live trader.
        raise Conflict("pinned: live trading right now; it will show as pinned on the next refresh")
    if machine["native_polymarket"] == "active":
        raise Conflict("native fleet-worker is running on this machine; stop it first")
    if workload is not None:
        row = get_workload(conn, workload)
        refusals = check_placement(
            manifest_of(row), machine, image_size_mb=row["image_size_mb"],
            image_published=row["image_digest"] is not None, workload_enabled=row["enabled"],
        )
        if refusals:
            raise Unplaceable([(r.code, r.message) for r in refusals])
    if (current["workload"] == POLYMARKET and not draining and machine["polymarket_worker_id"] is None
            and current["state"] in ("running", "starting")):
        raise Conflict("cannot identify the polymarket worker on this machine (no unique boot_id match), so its "
                       "trades cannot be drained; set that worker idle on /fleet, wait for it to settle, then retry")
    before = _snapshot(current)
    # Any machine whose polymarket worker is known drains through the role handshake, even
    # when the last heartbeat did not report the container as running (a missed report must
    # not skip the trade release).
    leaving_live = (
        current["workload"] == POLYMARKET and machine["polymarket_worker_id"] is not None and not draining
    )
    if draining or leaving_live:
        if leaving_live:
            scheduling.set_role(conn, machine["polymarket_worker_id"], "idle", actor)
        after_row = conn.execute(
            """
            UPDATE workload_assignments SET state = 'draining', draining_to = %s, draining_to_set = true,
                   assigned_by = %s, updated_at = now() WHERE machine_id = %s RETURNING *
            """,
            (workload, actor, machine_id),
        ).fetchone()
    else:
        after_row = _switch(conn, machine_id, workload, actor)
    add_audit(conn, "workload_assign", machine_id, actor, before, _snapshot(after_row), ip)
    return after_row


def desired_run(
    conn: psycopg.Connection, machine: dict[str, Any], assignment: dict[str, Any], public_url: str | None = None
) -> dict[str, Any] | None:
    """The "run" block of the heartbeat reply (section 5.1), or None when nothing should run:
    nothing assigned, machine disabled, or the image is not published.

    While a machine drains away from polymarket the block keeps describing the running
    container (same workload, same epoch), so the supervisor leaves it up until the worker
    has finished the trade release handshake; finish_drains then moves the epoch."""
    name = assignment.get("workload")
    if name is None or not machine["enabled"]:
        return None
    row = get_workload(conn, name)
    ref = image_ref(row)
    if ref is None:
        return None
    m = manifest_of(row)
    res, rt = m.resources, m.runtime
    memory_mb: int | None = res.memory_max_mb
    if memory_mb is None and res.memory_max_pct is not None and machine.get("ram_total_mb"):
        memory_mb = int(res.memory_max_pct * machine["ram_total_mb"] / 100)
    host_url = public_url or Config.from_env().public_url
    return {
        "image": ref, "protocol": m.protocol, "mode": rt.mode, "network": rt.network, "uts_host": rt.uts_host,
        "uid": rt.uid, "memory_mb": memory_mb, "cpus": res.cpus, "nice": rt.nice,
        "stop_timeout_s": rt.stop_timeout_s, "state_volume": rt.state_volume, "scratch_mb": rt.scratch_mb,
        "no_restart_exit_codes": list(rt.no_restart_exit_codes),
        "env": {
            "FLEET_HOST_URL": host_url, "FLEET_WORKLOAD": name,
            "FLEET_MACHINE_ID": machine["id"], "FLEET_EPOCH": str(assignment["epoch"]),
            "FLEET_NICE": str(rt.nice),
        },
    }


def _drain_done(conn: psycopg.Connection, machine: dict[str, Any], online_after: int) -> bool:
    """True when the machine's polymarket worker has released its work: it acked idle (or
    has gone silent) AND holds no job AND nothing on the machine trades live. A silent
    worker alone never ends a drain: its leased trade jobs expire through the reaper and
    its open orders through the orphan rule first."""
    if machine["pinned"] or pinning.is_live_trading(conn, machine["id"]):
        return False
    for worker_id in pinning.machine_worker_ids(conn, machine["id"]):
        held = conn.execute(
            "SELECT 1 FROM jobs WHERE lease_worker_id = %s AND status IN ('leased', 'cancel_requested') LIMIT 1",
            (worker_id,),
        ).fetchone()
        if held is not None:
            return False
    worker_id = machine["polymarket_worker_id"]
    if worker_id is None:
        return True
    w = conn.execute(
        """
        SELECT reported_role, acked_epoch, role_epoch,
               (last_heartbeat_at IS NULL OR last_heartbeat_at < now() - make_interval(secs => %s)) AS silent
          FROM workers WHERE id = %s
        """,
        (online_after, worker_id),
    ).fetchone()
    if w is None or w["silent"]:
        return True
    return w["reported_role"] == "idle" and w["acked_epoch"] == w["role_epoch"]


def finish_drains(conn: psycopg.Connection) -> int:
    """Complete every drain whose worker has released its work; returns how many finished."""
    online_after = get_int_setting(conn, "online_after_seconds", 15)
    rows = conn.execute(
        """
        SELECT a.*, m.polymarket_worker_id FROM workload_assignments a JOIN machines m ON m.id = a.machine_id
         WHERE a.state = 'draining' ORDER BY a.updated_at FOR UPDATE OF a SKIP LOCKED
        """
    ).fetchall()
    done = 0
    for row in rows:
        machine = conn.execute("SELECT * FROM machines WHERE id = %s", (row["machine_id"],)).fetchone()
        if not _drain_done(conn, machine, online_after):
            continue
        target = row["draining_to"] if row["draining_to_set"] else None
        after = _switch(conn, row["machine_id"], target, "system")
        add_audit(conn, "workload_assign", row["machine_id"], "system", _snapshot(row), _snapshot(after))
        done += 1
    return done
