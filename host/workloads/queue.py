"""The workload_jobs queue: same lifecycle as the Polymarket `jobs` queue (docs/PROTOCOL.md),
fenced by (lease_token, lease_machine_id, lease_epoch) instead of (lease_token, worker)."""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host.errors import BadRequest, Conflict, NotFound
from host.events import add_audit
from host.leases import RELEASE_REASONS, as_uuid
from host.settings import get_int_setting
from host.workloads.registry import get_workload, manifest_of

ACTIVE = ("leased", "cancel_requested")
MAX_ERROR_CHARS = 16 * 1024


def add_event(conn: psycopg.Connection, job_id: Any, event: str, machine_id: str | None = None,
              detail: dict[str, Any] | None = None) -> None:
    """Append one workload_job_events row."""
    conn.execute(
        "INSERT INTO workload_job_events (job_id, machine_id, event, detail) VALUES (%s, %s, %s, %s)",
        (job_id, machine_id, event, Jsonb(detail) if detail is not None else None),
    )


def lease_seconds(conn: psycopg.Connection) -> int:
    """Lease length: the existing `lease_seconds` setting."""
    return get_int_setting(conn, "lease_seconds", 30)


def get_job(conn: psycopg.Connection, job_id: Any, for_update: bool = False) -> dict[str, Any]:
    """One workload_jobs row; 404 when missing or not a uuid."""
    jid = as_uuid(job_id)
    if jid is None:
        raise NotFound("job not found")
    row = conn.execute("SELECT * FROM workload_jobs WHERE id = %s" + (" FOR UPDATE" if for_update else ""), (jid,)).fetchone()
    if row is None:
        raise NotFound("job not found")
    return row


def create_job(
    conn: psycopg.Connection, *, workload: str, kind: str, params: dict[str, Any], target: str | None = None,
    idempotency_key: str | None = None, max_expiries: int | None = 3,
) -> dict[str, Any]:
    """Queue a job. 404 unknown workload or target machine, 400 kind not in its manifest.

    A reused idempotency key returns the existing row. The row carries `_created`
    (True for a new row; underscore keys are dropped by the JSON serializer).
    """
    row = get_workload(conn, workload)
    kinds = manifest_of(row).runtime.job_kinds
    if kind not in kinds:
        raise BadRequest(f"kind {kind!r} is not declared by workload {workload!r} (job_kinds: {list(kinds)})")
    if not isinstance(params, dict):
        raise BadRequest("params must be a JSON object")
    if target is not None and conn.execute("SELECT 1 FROM machines WHERE id = %s", (target,)).fetchone() is None:
        raise NotFound(f"unknown machine: {target!r}")
    job = conn.execute(
        """
        INSERT INTO workload_jobs (workload, kind, params, target_machine_id, idempotency_key, max_expiries)
        VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING *
        """,
        (workload, kind, Jsonb(params), target, idempotency_key, max_expiries),
    ).fetchone()
    if job is None:
        job = conn.execute("SELECT * FROM workload_jobs WHERE idempotency_key = %s", (idempotency_key,)).fetchone()
        job["_created"] = False
        return job
    add_event(conn, job["id"], "created", None, {"kind": kind, "target": target})
    job["_created"] = True
    return job


def _assignment(conn: psycopg.Connection, machine_id: str, workload: str, epoch: int) -> dict[str, Any]:
    """The machine's assignment when it is exactly (workload, epoch), else 409."""
    row = conn.execute("SELECT * FROM workload_assignments WHERE machine_id = %s", (machine_id,)).fetchone()
    if row is None or row["workload"] != workload or row["epoch"] != epoch:
        raise Conflict("assignment changed; epoch is not current")
    return row


def claim(
    conn: psycopg.Connection, *, machine_id: str, workload: str, epoch: int, kinds: list[str] | None
) -> dict[str, Any] | None:
    """Lease one queued job of `workload` (only kinds its manifest declares, narrowed by
    `kinds`), jobs targeted at this machine first, then oldest. None when nothing fits.
    The row carries `lease_seconds`."""
    assignment = _assignment(conn, machine_id, workload, epoch)
    if assignment["state"] == "draining":
        return None
    allowed = list(manifest_of(get_workload(conn, workload)).runtime.job_kinds)
    if kinds is not None:
        allowed = [k for k in allowed if k in kinds]
    if not allowed:
        return None
    lease = lease_seconds(conn)
    row = conn.execute(
        """
        WITH c AS (
          SELECT id FROM workload_jobs
           WHERE status = 'queued' AND workload = %(wl)s AND kind = ANY(%(kinds)s) AND run_after <= now()
             AND (target_machine_id IS NULL OR target_machine_id = %(mid)s)
           ORDER BY (target_machine_id IS NOT DISTINCT FROM %(mid)s) DESC, created_at
           LIMIT 1 FOR UPDATE SKIP LOCKED)
        UPDATE workload_jobs j SET status = 'leased', lease_machine_id = %(mid)s, lease_epoch = %(epoch)s,
               lease_token = gen_random_uuid(), lease_expires_at = now() + make_interval(secs => %(lease)s),
               started_at = COALESCE(started_at, now()), updated_at = now()
          FROM c WHERE j.id = c.id RETURNING j.*
        """,
        {"wl": workload, "kinds": allowed, "mid": machine_id, "epoch": epoch, "lease": lease},
    ).fetchone()
    if row is None:
        return None
    add_event(conn, row["id"], "claimed", machine_id, {"epoch": epoch})
    row["lease_seconds"] = lease
    return row


def _fenced(conn: psycopg.Connection, job_id: Any, lease_token: Any, machine_id: str, epoch: int) -> dict[str, Any]:
    """The locked job when the token, machine and epoch match its active lease, else 409."""
    job = get_job(conn, job_id, for_update=True)
    tok = as_uuid(lease_token)
    if (job["status"] not in ACTIVE or tok is None or job["lease_token"] != tok
            or job["lease_machine_id"] != machine_id or job["lease_epoch"] != epoch):
        raise Conflict("lease token mismatch or job not leased to this machine and epoch")
    return job


def renew(conn: psycopg.Connection, *, job_id: Any, lease_token: str, machine_id: str, epoch: int,
          progress: float, checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    """Extend the lease and store progress; {"status", "cancel"} (cancel = stop and release)."""
    job = _fenced(conn, job_id, lease_token, machine_id, epoch)
    row = conn.execute(
        """
        UPDATE workload_jobs SET lease_expires_at = now() + make_interval(secs => %s),
               progress = COALESCE(%s, progress), checkpoint = COALESCE(%s, checkpoint), updated_at = now()
         WHERE id = %s RETURNING status
        """,
        (lease_seconds(conn), progress, Jsonb(checkpoint) if checkpoint is not None else None, job["id"]),
    ).fetchone()
    return {"status": row["status"], "cancel": row["status"] == "cancel_requested"}


def release(conn: psycopg.Connection, *, job_id: Any, lease_token: str, machine_id: str, epoch: int,
            checkpoint: dict[str, Any] | None, progress: float | None, reason: str | None) -> dict[str, Any]:
    """Hand a leased job back: queued again, or cancelled when cancel was requested.

    Reason `oom` counts like a lease expiry (the job fails at max_expiries); other
    reasons leave `expiries` alone.
    """
    job = _fenced(conn, job_id, lease_token, machine_id, epoch)
    reason = reason if reason in RELEASE_REASONS else None
    bump = 1 if reason == "oom" else 0
    expiries = job["expiries"] + bump
    exhausted = bump == 1 and job["max_expiries"] is not None and expiries >= job["max_expiries"]
    cancelled = job["status"] == "cancel_requested"
    status = "cancelled" if cancelled else "failed" if exhausted else "queued"
    row = conn.execute(
        """
        UPDATE workload_jobs SET status = %s, expiries = %s,
               error = CASE WHEN %s THEN 'failed after ' || %s::text || ' expiries (last: out of memory)' ELSE error END,
               finished_at = CASE WHEN %s THEN now() ELSE finished_at END,
               progress = COALESCE(%s, progress), checkpoint = COALESCE(%s, checkpoint),
               lease_machine_id = NULL, lease_epoch = NULL, lease_token = NULL, lease_expires_at = NULL,
               updated_at = now()
         WHERE id = %s RETURNING *
        """,
        (status, expiries, exhausted and not cancelled, expiries, status != "queued", progress,
         Jsonb(checkpoint) if checkpoint is not None else None, job["id"]),
    ).fetchone()
    detail: dict[str, Any] = {"status": status}
    if reason:
        detail["reason"] = reason
    add_event(conn, job["id"], "released", machine_id, detail)
    return row


def complete(conn: psycopg.Connection, *, job_id: Any, lease_token: str, machine_id: str, epoch: int,
             result: dict[str, Any]) -> dict[str, Any]:
    """Succeed the job. Idempotent for a repeat from the same lease holder."""
    job = get_job(conn, job_id, for_update=True)
    tok = as_uuid(lease_token)
    if tok is None or job["lease_token"] != tok or job["lease_machine_id"] != machine_id or job["lease_epoch"] != epoch:
        raise Conflict("lease token mismatch or job not leased to this machine and epoch")
    if job["status"] == "succeeded":
        return job
    if job["status"] not in ACTIVE:
        raise Conflict(f"job is {job['status']}")
    row = conn.execute(
        """
        UPDATE workload_jobs SET status = 'succeeded', result = %s, progress = 1, finished_at = now(),
               lease_expires_at = NULL, updated_at = now() WHERE id = %s RETURNING *
        """,
        (Jsonb(result), job["id"]),
    ).fetchone()
    add_event(conn, job["id"], "succeeded", machine_id)
    return row


def fail(conn: psycopg.Connection, *, job_id: Any, lease_token: str, machine_id: str, epoch: int,
         error: str) -> dict[str, Any]:
    """Terminal failure, never retried. Idempotent for a repeat from the same lease holder."""
    job = get_job(conn, job_id, for_update=True)
    tok = as_uuid(lease_token)
    if tok is None or job["lease_token"] != tok or job["lease_machine_id"] != machine_id or job["lease_epoch"] != epoch:
        raise Conflict("lease token mismatch or job not leased to this machine and epoch")
    if job["status"] == "failed":
        return job
    if job["status"] not in ACTIVE:
        raise Conflict(f"job is {job['status']}")
    text = (error or "")[:MAX_ERROR_CHARS].replace("\x00", "")
    row = conn.execute(
        """
        UPDATE workload_jobs SET status = 'failed', error = %s, finished_at = now(),
               lease_expires_at = NULL, updated_at = now() WHERE id = %s RETURNING *
        """,
        (text, job["id"]),
    ).fetchone()
    add_event(conn, job["id"], "failed", machine_id, {"error": text[:500]})
    return row


def cancel(conn: psycopg.Connection, job_id: Any, actor: str | None) -> dict[str, Any]:
    """queued -> cancelled; leased -> cancel_requested; terminal -> 409. Audited."""
    job = get_job(conn, job_id, for_update=True)
    status = job["status"]
    if status in ("cancelled", "cancel_requested"):
        return job
    if status == "queued":
        row = conn.execute(
            "UPDATE workload_jobs SET status = 'cancelled', finished_at = now(), updated_at = now() WHERE id = %s RETURNING *",
            (job["id"],),
        ).fetchone()
        add_event(conn, job["id"], "cancelled", None, {"by": actor})
    elif status == "leased":
        row = conn.execute(
            "UPDATE workload_jobs SET status = 'cancel_requested', updated_at = now() WHERE id = %s RETURNING *",
            (job["id"],),
        ).fetchone()
        add_event(conn, job["id"], "cancel_requested", job["lease_machine_id"], {"by": actor})
    else:
        raise Conflict(f"job is {status}")
    add_audit(conn, "workload_job_cancel", str(job["id"]), actor, {"status": status}, {"status": row["status"]})
    return row


def reap(conn: psycopg.Connection) -> int:
    """Expire overdue leases: requeue, cancel or fail (at max_expiries); returns the count."""
    rows = conn.execute(
        """
        WITH e AS (
          SELECT id, lease_machine_id AS old_machine FROM workload_jobs
           WHERE status IN ('leased', 'cancel_requested') AND lease_expires_at < now()
           FOR UPDATE SKIP LOCKED)
        UPDATE workload_jobs j SET
               expiries = j.expiries + 1,
               status = CASE WHEN j.status = 'cancel_requested' THEN 'cancelled'
                             WHEN j.max_expiries IS NULL OR j.expiries + 1 < j.max_expiries THEN 'queued'
                             ELSE 'failed' END,
               error = CASE WHEN j.status <> 'cancel_requested' AND j.max_expiries IS NOT NULL
                                 AND j.expiries + 1 >= j.max_expiries
                            THEN 'failed after ' || (j.expiries + 1) || ' expiries (last: lease expired)'
                            ELSE j.error END,
               finished_at = CASE WHEN j.status = 'cancel_requested'
                                       OR (j.max_expiries IS NOT NULL AND j.expiries + 1 >= j.max_expiries)
                                  THEN now() ELSE j.finished_at END,
               lease_machine_id = NULL, lease_epoch = NULL, lease_token = NULL, lease_expires_at = NULL,
               updated_at = now()
          FROM e WHERE j.id = e.id
          RETURNING j.id, j.status, j.expiries, e.old_machine
        """
    ).fetchall()
    for row in rows:
        add_event(conn, row["id"], "lease_expired", row["old_machine"],
                  {"status": row["status"], "expiries": row["expiries"]})
    return len(rows)


def release_machine_jobs(conn: psycopg.Connection, machine_id: str) -> int:
    """Put every job leased to this machine back (reassignment): queued, or cancelled
    when cancel was requested. Expiries are not counted."""
    rows = conn.execute(
        """
        UPDATE workload_jobs SET
               status = CASE WHEN status = 'cancel_requested' THEN 'cancelled' ELSE 'queued' END,
               finished_at = CASE WHEN status = 'cancel_requested' THEN now() ELSE finished_at END,
               lease_machine_id = NULL, lease_epoch = NULL, lease_token = NULL, lease_expires_at = NULL,
               updated_at = now()
         WHERE lease_machine_id = %s AND status IN ('leased', 'cancel_requested')
         RETURNING id, status
        """,
        (machine_id,),
    ).fetchall()
    for row in rows:
        add_event(conn, row["id"], "released", machine_id, {"status": row["status"], "reason": "reassigned"})
    return len(rows)

