"""Lease recovery: orphaned leases (re-offer), held jobs on register (re-lease), the reaper."""
from __future__ import annotations

from typing import Any

import psycopg

from host.events import add_job_event
from host.leases import as_uuid


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
                 ELSE 'failed after ' || (j.expiries + 1) || ' expiries (last: lease expired)' END,
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
