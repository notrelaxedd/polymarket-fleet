"""Lease lifecycle: claim, re-offer, renew, release, checkpoint, complete, fail, reaper."""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host.errors import Conflict, NotFound
from host.events import add_job_event
from host.settings import get_int_setting

ACTIVE = ("leased", "cancel_requested")


def as_uuid(value: Any) -> uuid.UUID | None:
    """Parse a uuid-ish value; None when it is not a valid uuid."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def lease_seconds(conn: psycopg.Connection) -> int:
    """Current lease length from settings."""
    return get_int_setting(conn, "lease_seconds", 30)


def job_payload(row: dict[str, Any], lease: int) -> dict[str, Any]:
    """What a worker needs to run a job it was just handed."""
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "params": row["params"],
        "checkpoint": row["checkpoint"],
        "progress": row.get("progress"),
        "lease_token": str(row["lease_token"]),
        "lease_seconds": lease,
    }


def _fence(job: dict[str, Any], lease_token: Any, worker_id: str | None) -> uuid.UUID:
    """The parsed token when it matches the job's active lease held by `worker_id`, else 409."""
    tok = as_uuid(lease_token)
    if job["status"] not in ACTIVE or tok is None or job["lease_token"] != tok:
        raise Conflict("lease token mismatch or job not leased")
    if worker_id is not None and job["lease_worker_id"] != worker_id:
        raise Conflict("job is leased to another worker")
    return tok


def get_job(conn: psycopg.Connection, job_id: Any, for_update: bool = False) -> dict[str, Any]:
    """Fetch one job row; 404 when missing or the id is not a uuid."""
    jid = as_uuid(job_id)
    if jid is None:
        raise NotFound("job not found")
    sql = "SELECT * FROM jobs WHERE id = %s" + (" FOR UPDATE" if for_update else "")
    row = conn.execute(sql, (jid,)).fetchone()
    if row is None:
        raise NotFound("job not found")
    return row


def claim(conn: psycopg.Connection, worker_id: str, role: str, lease: int) -> dict[str, Any] | None:
    """Claim at most one queued job of `role` for the worker (targeted first)."""
    row = conn.execute(
        """
        WITH c AS (
          SELECT id FROM jobs
           WHERE status = 'queued' AND role = %(role)s AND run_after <= now()
             AND (target_worker_id IS NULL OR target_worker_id = %(wid)s)
           ORDER BY (target_worker_id IS NOT DISTINCT FROM %(wid)s) DESC, created_at
           LIMIT 1 FOR UPDATE SKIP LOCKED)
        UPDATE jobs j SET status = 'leased', lease_worker_id = %(wid)s,
               lease_token = gen_random_uuid(),
               lease_expires_at = now() + make_interval(secs => %(lease)s),
               started_at = COALESCE(started_at, now()),
               preempt_requested = false, updated_at = now()
          FROM c WHERE j.id = c.id RETURNING j.*
        """,
        {"role": role, "wid": worker_id, "lease": lease},
    ).fetchone()
    if row is not None:
        add_job_event(conn, row["id"], "claimed", worker_id)
    return row


def orphan_jobs(conn: psycopg.Connection, worker_id: str, reported_ids: list[str], lease: int) -> list[dict[str, Any]]:
    """Leases this worker holds but did not mention in its heartbeat: the claim reply
    was lost (or a stale heartbeat claimed on its behalf).

    `leased` orphans are renewed and returned so the worker is handed the same lease
    again instead of a second job. A `cancel_requested` orphan is cancelled outright:
    the worker is not running it, so there is nothing to stop.
    """
    known = [jid for jid in (as_uuid(v) for v in reported_ids) if jid is not None]
    cancelled = conn.execute(
        """
        UPDATE jobs SET status = 'cancelled', finished_at = now(),
               lease_worker_id = NULL, lease_token = NULL, lease_expires_at = NULL,
               preempt_requested = false, updated_at = now()
         WHERE lease_worker_id = %(wid)s AND status = 'cancel_requested'
           AND NOT (id = ANY(%(known)s::uuid[]))
         RETURNING id
        """,
        {"wid": worker_id, "known": known},
    ).fetchall()
    for row in cancelled:
        add_job_event(conn, row["id"], "cancelled", worker_id, {"reason": "orphaned lease"})
    rows = conn.execute(
        """
        UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %(lease)s), updated_at = now()
         WHERE lease_worker_id = %(wid)s AND status = 'leased'
           AND NOT (id = ANY(%(known)s::uuid[]))
         RETURNING *
        """,
        {"lease": lease, "wid": worker_id, "known": known},
    ).fetchall()
    for row in rows:
        add_job_event(conn, row["id"], "re-offered", worker_id)
    return rows


def renew(
    conn: psycopg.Connection,
    worker_id: str,
    job_id: Any,
    lease_token: Any,
    lease: int,
    progress: float | None = None,
    checkpoint: dict[str, Any] | None = None,
) -> bool:
    """Extend one lease; False when the (id, token, worker, status) do not match."""
    jid, tok = as_uuid(job_id), as_uuid(lease_token)
    if jid is None or tok is None:
        return False
    row = conn.execute(
        """
        UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %(lease)s),
               progress = COALESCE(%(progress)s, progress),
               checkpoint = COALESCE(%(checkpoint)s, checkpoint),
               updated_at = now()
         WHERE id = %(id)s AND lease_token = %(tok)s AND lease_worker_id = %(wid)s
           AND status IN ('leased', 'cancel_requested')
         RETURNING id
        """,
        {
            "lease": lease,
            "progress": progress,
            "checkpoint": Jsonb(checkpoint) if checkpoint is not None else None,
            "id": jid,
            "tok": tok,
            "wid": worker_id,
        },
    ).fetchone()
    return row is not None


def release(
    conn: psycopg.Connection,
    job_id: Any,
    lease_token: Any,
    progress: float | None = None,
    checkpoint: dict[str, Any] | None = None,
    worker_id: str | None = None,
) -> str | None:
    """Hand a leased job back: queued, or cancelled if cancel was requested.

    Returns the new status, or None when the token (or, when given, the worker)
    did not match. `expiries` is left untouched. A system-chosen target
    (`target_auto`) is cleared so the dispatcher can place the job elsewhere.
    """
    jid, tok = as_uuid(job_id), as_uuid(lease_token)
    if jid is None or tok is None:
        return None
    row = conn.execute(
        """
        UPDATE jobs SET
               status = CASE WHEN status = 'cancel_requested' THEN 'cancelled' ELSE 'queued' END,
               finished_at = CASE WHEN status = 'cancel_requested' THEN now() ELSE finished_at END,
               progress = COALESCE(%(progress)s, progress),
               checkpoint = COALESCE(%(checkpoint)s, checkpoint),
               target_worker_id = CASE WHEN target_auto THEN NULL ELSE target_worker_id END,
               target_auto = false,
               lease_worker_id = NULL, lease_token = NULL, lease_expires_at = NULL,
               preempt_requested = false, updated_at = now()
         WHERE id = %(id)s AND lease_token = %(tok)s
           AND (%(wid)s::text IS NULL OR lease_worker_id = %(wid)s)
           AND status IN ('leased', 'cancel_requested')
         RETURNING id, status, lease_worker_id
        """,
        {
            "progress": progress,
            "checkpoint": Jsonb(checkpoint) if checkpoint is not None else None,
            "id": jid,
            "tok": tok,
            "wid": worker_id,
        },
    ).fetchone()
    if row is None:
        return None
    add_job_event(conn, row["id"], "released", worker_id, {"status": row["status"]})
    return row["status"]


def checkpoint(
    conn: psycopg.Connection,
    job_id: Any,
    lease_token: Any,
    checkpoint_data: dict[str, Any] | None,
    progress: float | None,
    do_release: bool = False,
    worker_id: str | None = None,
) -> str:
    """POST /checkpoint: store progress under the fence; optionally release.

    `worker_id` (the caller's identity) must own the lease when given.
    """
    job = get_job(conn, job_id, for_update=True)
    tok = _fence(job, lease_token, worker_id)
    if do_release:
        status = release(conn, job_id, tok, progress, checkpoint_data, job["lease_worker_id"])
        return status or job["status"]
    conn.execute(
        """
        UPDATE jobs SET progress = COALESCE(%s, progress),
               checkpoint = COALESCE(%s, checkpoint), updated_at = now()
         WHERE id = %s
        """,
        (progress, Jsonb(checkpoint_data) if checkpoint_data is not None else None, job["id"]),
    )
    return job["status"]


def complete(
    conn: psycopg.Connection, job_id: Any, lease_token: Any, result: Any, worker_id: str | None = None
) -> dict[str, Any]:
    """POST /complete: succeed the job. Idempotent for a repeat with the same token."""
    job = get_job(conn, job_id, for_update=True)
    tok = as_uuid(lease_token)
    if tok is None or job["lease_token"] != tok:
        raise Conflict("lease token mismatch")
    if job["status"] == "succeeded":
        return job
    if job["status"] not in ACTIVE:
        raise Conflict(f"job is {job['status']}")
    _fence(job, tok, worker_id)
    row = conn.execute(
        """
        UPDATE jobs SET status = 'succeeded', result = %s, progress = 1, finished_at = now(),
               lease_worker_id = NULL, lease_expires_at = NULL,
               preempt_requested = false, updated_at = now()
         WHERE id = %s RETURNING *
        """,
        (Jsonb(result) if result is not None else None, job["id"]),
    ).fetchone()
    add_job_event(conn, job["id"], "succeeded", job["lease_worker_id"])
    return row


def fail(
    conn: psycopg.Connection, job_id: Any, lease_token: Any, error: str, worker_id: str | None = None
) -> dict[str, Any]:
    """POST /fail: terminal failure, never retried."""
    job = get_job(conn, job_id, for_update=True)
    tok = as_uuid(lease_token)
    if tok is None or job["lease_token"] != tok:
        raise Conflict("lease token mismatch")
    if job["status"] == "failed":
        return job
    if job["status"] not in ACTIVE:
        raise Conflict(f"job is {job['status']}")
    _fence(job, tok, worker_id)
    row = conn.execute(
        """
        UPDATE jobs SET status = 'failed', error = %s, finished_at = now(),
               lease_worker_id = NULL, lease_expires_at = NULL,
               preempt_requested = false, updated_at = now()
         WHERE id = %s RETURNING *
        """,
        (error, job["id"]),
    ).fetchone()
    add_job_event(conn, job["id"], "failed", job["lease_worker_id"], {"error": error})
    return row


def _last_event(conn: psycopg.Connection, job_id: Any) -> dict[str, Any] | None:
    """The newest job_events row for a job."""
    return conn.execute(
        "SELECT event, worker_id FROM job_events WHERE job_id = %s ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()


def held_jobs(conn: psycopg.Connection, worker_id: str, lease: int) -> list[dict[str, Any]]:
    """Re-lease the worker's live leases with fresh tokens (register).

    A `re-leased` event is written once per run of registers: when the job's newest
    event is already `re-leased` by this worker (a register retry loop) no row is added,
    so a looping agent cannot grow job_events without bound.
    """
    rows = conn.execute(
        """
        UPDATE jobs SET lease_token = gen_random_uuid(),
               lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
         WHERE lease_worker_id = %s AND status IN ('leased', 'cancel_requested')
           AND lease_expires_at > now()
         RETURNING *
        """,
        (lease, worker_id),
    ).fetchall()
    for row in rows:
        last = _last_event(conn, row["id"])
        if last is None or last["event"] != "re-leased" or last["worker_id"] != worker_id:
            add_job_event(conn, row["id"], "re-leased", worker_id)
    return rows


def reap(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Expire overdue leases: requeue, cancel or fail per the contract."""
    rows = conn.execute(
        """
        WITH e AS (
          SELECT id, lease_worker_id AS old_worker FROM jobs
           WHERE status IN ('leased', 'cancel_requested') AND lease_expires_at < now()
           FOR UPDATE SKIP LOCKED)
        UPDATE jobs j SET
               expiries = j.expiries + 1,
               status = CASE
                 WHEN j.status = 'cancel_requested' THEN 'cancelled'
                 WHEN j.max_expiries IS NULL OR j.expiries + 1 < j.max_expiries THEN 'queued'
                 ELSE 'failed' END,
               error = CASE
                 WHEN j.status = 'cancel_requested' THEN j.error
                 WHEN j.max_expiries IS NULL OR j.expiries + 1 < j.max_expiries THEN j.error
                 ELSE 'lease expired ' || (j.expiries + 1) || ' times' END,
               finished_at = CASE
                 WHEN j.status = 'cancel_requested' THEN now()
                 WHEN j.max_expiries IS NULL OR j.expiries + 1 < j.max_expiries THEN j.finished_at
                 ELSE now() END,
               target_worker_id = CASE WHEN j.target_auto THEN NULL ELSE j.target_worker_id END,
               target_auto = false,
               lease_worker_id = NULL, lease_token = NULL, lease_expires_at = NULL,
               preempt_requested = false, updated_at = now()
          FROM e WHERE j.id = e.id
          RETURNING j.id, j.status, j.expiries, e.old_worker
        """
    ).fetchall()
    for row in rows:
        add_job_event(
            conn, row["id"], "lease_expired", row["old_worker"],
            {"status": row["status"], "expiries": row["expiries"]},
        )
    return rows
