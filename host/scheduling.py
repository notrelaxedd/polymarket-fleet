"""Owner-driven scheduling: job creation, targeting, role changes, dispatcher."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host.errors import BadRequest, Conflict, NotFound
from host.events import add_audit, add_job_event, worker_snapshot
from host.jobparams import prepare_params
from host.leases import get_job
from host.settings import BATCH_ROLES, ROLES, get_int_setting, get_setting, role_for_kind

ANY_IDLE = "any_idle"


@dataclass
class CreateResult:
    """Outcome of create_job."""

    job: dict[str, Any]
    created: bool
    waiting_for_idle_worker: bool


def get_worker(conn: psycopg.Connection, worker_id: str, for_update: bool = False) -> dict[str, Any]:
    """Fetch one worker row; 404 when missing."""
    sql = "SELECT * FROM workers WHERE id = %s" + (" FOR UPDATE" if for_update else "")
    row = conn.execute(sql, (worker_id,)).fetchone()
    if row is None:
        raise NotFound("worker not found")
    return row


def online_after(conn: psycopg.Connection) -> int:
    """Seconds after which a silent worker counts as offline."""
    return get_int_setting(conn, "online_after_seconds", 15)


IDLE_PICK_RETRIES = 4
IDLE_PICK_WAIT_S = 0.02


def idle_pick(conn: psycopg.Connection) -> dict[str, Any] | None:
    """Lock and return one online idle worker with nothing targeted at it.

    SKIP LOCKED keeps two pickers off the same worker, but a worker's own heartbeat
    holds its row lock for a few milliseconds every beat, so an idle worker can look
    taken for an instant; a few short retries make an `any_idle` job land on it at
    creation instead of waiting for the dispatcher's next pass.
    """
    for attempt in range(IDLE_PICK_RETRIES + 1):
        row = conn.execute(
            """
            SELECT * FROM workers w
             WHERE enabled AND desired_role = 'idle' AND reported_role = 'idle'
               AND acked_epoch = role_epoch
               AND last_heartbeat_at > now() - make_interval(secs => %s)
               AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.target_worker_id = w.id
                               AND j.status IN ('queued', 'leased', 'cancel_requested'))
             ORDER BY last_heartbeat_at DESC LIMIT 1 FOR UPDATE SKIP LOCKED
            """,
            (online_after(conn),),
        ).fetchone()
        if row is not None or attempt == IDLE_PICK_RETRIES:
            return row
        time.sleep(IDLE_PICK_WAIT_S)
    return None


def preempt_other_roles(conn: psycopg.Connection, worker_id: str, role: str) -> list[Any]:
    """Flag the worker's leased jobs of a different role for preemption."""
    rows = conn.execute(
        """
        UPDATE jobs SET preempt_requested = true, updated_at = now()
         WHERE lease_worker_id = %s AND status = 'leased' AND role <> %s
           AND NOT preempt_requested
         RETURNING id
        """,
        (worker_id, role),
    ).fetchall()
    for row in rows:
        add_job_event(conn, row["id"], "preempt_requested", worker_id, {"new_role": role})
    return [row["id"] for row in rows]


def flip_role(
    conn: psycopg.Connection,
    worker: dict[str, Any],
    role: str,
    auto_role: bool,
    actor: str | None,
    action: str,
) -> dict[str, Any]:
    """Set desired_role, bump the epoch, preempt other-role leases, audit."""
    after = conn.execute(
        """
        UPDATE workers SET desired_role = %s, role_epoch = role_epoch + 1, auto_role = %s
         WHERE id = %s RETURNING *
        """,
        (role, auto_role, worker["id"]),
    ).fetchone()
    preempt_other_roles(conn, worker["id"], role)
    add_audit(conn, action, worker["id"], actor, worker_snapshot(worker), worker_snapshot(after))
    return after


def set_role(conn: psycopg.Connection, worker_id: str, role: str, actor: str | None = None) -> dict[str, Any]:
    """Owner role change: always bumps the epoch and clears auto_role."""
    if role not in ROLES:
        raise BadRequest(f"unknown role: {role!r}")
    worker = get_worker(conn, worker_id, for_update=True)
    return flip_role(conn, worker, role, False, actor, "set_role")


def set_enabled(conn: psycopg.Connection, worker_id: str, enabled: bool, actor: str | None = None) -> dict[str, Any]:
    """Enable or disable claiming for a worker."""
    worker = get_worker(conn, worker_id, for_update=True)
    after = conn.execute(
        "UPDATE workers SET enabled = %s WHERE id = %s RETURNING *", (bool(enabled), worker_id)
    ).fetchone()
    add_audit(conn, "set_enabled", worker_id, actor, worker_snapshot(worker), worker_snapshot(after))
    return after


def target_job_at(
    conn: psycopg.Connection, job: dict[str, Any], worker: dict[str, Any], actor: str | None, auto: bool
) -> dict[str, Any]:
    """Point a job at a locked worker and flip the worker's role if needed.

    `auto` records that the system picked the worker (any_idle, dispatcher): such a
    target is dropped again when the job is released or its lease expires.
    """
    job = conn.execute(
        "UPDATE jobs SET target_worker_id = %s, target_auto = %s, updated_at = now() WHERE id = %s RETURNING *",
        (worker["id"], auto, job["id"]),
    ).fetchone()
    add_job_event(conn, job["id"], "targeted", worker["id"], {"auto": auto})
    if worker["desired_role"] != job["role"]:
        flip_role(conn, worker, job["role"], True, actor, "auto_role")
    return job


def _insert_job(
    conn: psycopg.Connection,
    kind: str,
    role: str,
    params: dict[str, Any],
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Insert an untargeted queued job."""
    max_expiries = get_setting(conn, "max_expiries", 3)
    row = conn.execute(
        """
        INSERT INTO jobs (kind, role, params, idempotency_key, max_expiries)
        VALUES (%s, %s, %s, %s, %s) RETURNING *
        """,
        (kind, role, Jsonb(params), idempotency_key, max_expiries),
    ).fetchone()
    add_job_event(conn, row["id"], "created", None, {"kind": kind})
    return row


def _existing_by_key(conn: psycopg.Connection, key: str) -> dict[str, Any] | None:
    """Serialise on the key and return the job already created for it, if any."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
    return conn.execute("SELECT * FROM jobs WHERE idempotency_key = %s", (key,)).fetchone()


def create_job(
    conn: psycopg.Connection,
    kind: str,
    params: dict[str, Any] | None = None,
    target: str | None = None,
    idempotency_key: str | None = None,
    actor: str | None = None,
) -> CreateResult:
    """POST /api/jobs in one transaction (caller owns the transaction)."""
    role = role_for_kind(kind)
    params = params if params is not None else {}
    if not isinstance(params, dict):
        raise BadRequest("params must be a JSON object")
    params = prepare_params(conn, kind, params)
    if idempotency_key:
        existing = _existing_by_key(conn, idempotency_key)
        if existing is not None:
            return CreateResult(existing, False, False)
    if target and target != ANY_IDLE:
        worker = get_worker(conn, target, for_update=True)
        job = _insert_job(conn, kind, role, params, idempotency_key)
        job = target_job_at(conn, job, worker, actor, auto=False)
        return CreateResult(job, True, False)
    job = _insert_job(conn, kind, role, params, idempotency_key)
    if target == ANY_IDLE:
        worker = idle_pick(conn)
        if worker is not None:
            return CreateResult(target_job_at(conn, job, worker, actor, auto=True), True, False)
        return CreateResult(job, True, True)
    return CreateResult(job, True, False)


def _halt_for_cancelled_trade_job(conn: psycopg.Connection, job: dict[str, Any], actor: str | None) -> None:
    """A trade job cancelled from the Jobs page or `POST /api/jobs/{id}/cancel` goes
    through the assignment halt first: its open orders are cancelled with the ledger
    release and the assignment shows `halted` on /trading (Activate makes a new
    trade job). Runs before the job row is locked, because the halt takes the
    approval lock and the approval locks the job row after it."""
    from host.trading import assignments

    params = job["params"] if isinstance(job["params"], dict) else {}
    aid = params.get("assignment_id")
    if job["kind"] != "trade" or aid is None:
        return
    try:
        assignments.halt_assignment(conn, aid, actor, "job cancelled")
    except (NotFound, Conflict):
        return


def cancel_job(conn: psycopg.Connection, job_id: Any, actor: str | None = None) -> dict[str, Any]:
    """queued -> cancelled; leased -> cancel_requested; terminal -> 409. A trade
    job's assignment is halted first (its open orders cancelled)."""
    job = get_job(conn, job_id)
    if job["status"] in ("queued", "leased"):
        _halt_for_cancelled_trade_job(conn, job, actor)
    job = get_job(conn, job_id, for_update=True)
    status = job["status"]
    if status in ("cancelled", "cancel_requested"):
        return job
    if status == "queued":
        row = conn.execute(
            """
            UPDATE jobs SET status = 'cancelled', finished_at = now(), updated_at = now()
             WHERE id = %s RETURNING *
            """,
            (job["id"],),
        ).fetchone()
        add_job_event(conn, job["id"], "cancelled", None, {"by": actor})
        return row
    if status == "leased":
        row = conn.execute(
            "UPDATE jobs SET status = 'cancel_requested', updated_at = now() WHERE id = %s RETURNING *",
            (job["id"],),
        ).fetchone()
        add_job_event(conn, job["id"], "cancel_requested", job["lease_worker_id"], {"by": actor})
        return row
    raise Conflict(f"job is {status}")


def auto_return_to_idle(conn: psycopg.Connection, worker_id: str) -> dict[str, Any] | None:
    """Flip an auto_role worker back to idle once nothing is left for it.

    Nothing left means: no lease held, no queued job targeted at it, and no
    untargeted queued job of its current role it could claim right away.
    Returns the updated worker row, or None when the conditions did not hold.
    The caller must already hold the worker row lock.
    """
    before = conn.execute("SELECT * FROM workers WHERE id = %s", (worker_id,)).fetchone()
    after = conn.execute(
        """
        UPDATE workers w SET desired_role = 'idle', role_epoch = role_epoch + 1, auto_role = false
         WHERE id = %s AND auto_role AND desired_role <> 'idle'
           AND reported_role = desired_role AND acked_epoch = role_epoch
           AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.lease_worker_id = w.id
                           AND j.status IN ('leased', 'cancel_requested'))
           AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.target_worker_id = w.id
                           AND j.status = 'queued')
           AND NOT EXISTS (SELECT 1 FROM jobs q WHERE q.status = 'queued'
                           AND q.target_worker_id IS NULL AND q.role = w.desired_role
                           AND q.run_after <= now())
         RETURNING *
        """,
        (worker_id,),
    ).fetchone()
    if after is not None and before is not None:
        add_audit(conn, "auto_idle", worker_id, None, worker_snapshot(before), worker_snapshot(after))
    return after


def dispatch(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Target untargeted queued batch jobs, oldest first, at idle workers."""
    jobs = conn.execute(
        """
        SELECT * FROM jobs
         WHERE status = 'queued' AND target_worker_id IS NULL AND role = ANY(%s)
         ORDER BY created_at FOR UPDATE SKIP LOCKED
        """,
        (list(BATCH_ROLES),),
    ).fetchall()
    assigned: list[dict[str, Any]] = []
    for job in jobs:
        worker = idle_pick(conn)
        if worker is None:
            break
        assigned.append(target_job_at(conn, job, worker, "dispatcher", auto=True))
    return assigned
