"""Register and heartbeat transactions (the worker side of the contract)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg

from host import auth
from host.errors import Unauthorized
from host.events import add_audit
from host.leases import claim, held_jobs, job_payload, lease_seconds, orphan_jobs, release, renew
from host.scheduling import auto_return_to_idle
from host.settings import BATCH_ROLES, get_int_setting, get_setting


def server_time(conn: psycopg.Connection) -> datetime:
    """Database clock, timezone aware."""
    return conn.execute("SELECT now() AS t").fetchone()["t"].astimezone(timezone.utc)


def kill_switch(conn: psycopg.Connection) -> bool:
    """The fleet-wide kill flag (only the JSON boolean true counts)."""
    return get_setting(conn, "kill_switch", False) is True


def _common_reply(conn: psycopg.Connection, worker: dict[str, Any]) -> dict[str, Any]:
    """Fields shared by register and heartbeat responses."""
    return {
        "desired_role": worker["desired_role"],
        "role_epoch": worker["role_epoch"],
        "kill": kill_switch(conn),
        "server_time": server_time(conn),
        "heartbeat_seconds": get_int_setting(conn, "heartbeat_seconds", 5),
    }


def _machine_fields(body: dict[str, Any], remote_ip: str | None) -> tuple[Any, ...]:
    return (
        body.get("hostname"),
        body.get("python_version"),
        body.get("code_version"),
        body.get("boot_id"),
        remote_ip,
    )


def _enroll(conn: psycopg.Connection, body: dict[str, Any], remote_ip: str | None) -> dict[str, Any]:
    """First registration via an enroll token: create the worker."""
    token_row = auth.lock_enroll_token(conn, str(body.get("enroll_token", "")))
    worker_id = auth.new_worker_id()
    while conn.execute("SELECT 1 FROM workers WHERE id = %s", (worker_id,)).fetchone():
        worker_id = auth.new_worker_id()
    plain = auth.mint_token()
    name = body.get("name") or body.get("hostname") or worker_id
    worker = conn.execute(
        """
        INSERT INTO workers (id, name, token_hash, hostname, python_version, code_version,
                             boot_id, remote_ip)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (worker_id, name, auth.hash_token(plain)) + _machine_fields(body, remote_ip),
    ).fetchone()
    auth.mark_enroll_token_used(conn, token_row["token_hash"], worker_id)
    add_audit(
        conn, "worker_enrolled", worker_id, worker_id, None,
        {"name": name, "hostname": body.get("hostname")}, ip=remote_ip,
    )
    worker["_plain_token"] = plain
    return worker


def _reregister(conn: psycopg.Connection, body: dict[str, Any], remote_ip: str | None) -> dict[str, Any]:
    """Re-registration: verify the current (or previous) token under the row lock, then rotate it."""
    worker_id = str(body.get("worker_id", ""))
    _, presented = auth.verify_register_token(conn, worker_id, str(body.get("worker_token", "")))
    plain = auth.rotate_worker_token(conn, worker_id, presented)
    worker = conn.execute(
        """
        UPDATE workers SET hostname = COALESCE(%s, hostname), python_version = %s,
               code_version = %s, boot_id = %s, remote_ip = %s
         WHERE id = %s RETURNING *
        """,
        _machine_fields(body, remote_ip) + (worker_id,),
    ).fetchone()
    worker["_plain_token"] = plain
    return worker


def register(conn: psycopg.Connection, body: dict[str, Any], remote_ip: str | None = None) -> dict[str, Any]:
    """POST /api/v1/workers/register, one transaction."""
    if body.get("enroll_token"):
        worker = _enroll(conn, body, remote_ip)
    elif body.get("worker_id") and body.get("worker_token"):
        worker = _reregister(conn, body, remote_ip)
    else:
        raise Unauthorized("enroll_token or worker_id + worker_token required")
    lease = lease_seconds(conn)
    held = held_jobs(conn, worker["id"], lease)
    reply = _common_reply(conn, worker)
    reply.update(
        {
            "worker_id": worker["id"],
            "worker_token": worker["_plain_token"],
            "held_jobs": [job_payload(row, lease) for row in held],
        }
    )
    return reply


def _update_worker(
    conn: psycopg.Connection, worker_id: str, body: dict[str, Any], token_hash: str | None
) -> dict[str, Any] | None:
    """Step 1: record the heartbeat and lock the worker row.

    The token is checked inside the locking UPDATE so a heartbeat that raced a
    register cannot act after the rotation committed. The first heartbeat with the
    current token clears prev_token_hash (a zombie copy is locked out from then on).
    acked_epoch never moves backwards, so a stale heartbeat cannot undo an ack.
    """
    return conn.execute(
        """
        UPDATE workers SET last_heartbeat_at = now(), cpu_pct = %(cpu)s, ram_used_mb = %(ram_used)s,
               ram_total_mb = %(ram_total)s, reported_role = COALESCE(%(role)s, reported_role),
               acked_epoch = GREATEST(acked_epoch, COALESCE(%(ack)s, acked_epoch)),
               code_version = COALESCE(%(code)s, code_version), skew_ms = %(skew)s,
               prev_token_hash = NULL
         WHERE id = %(wid)s AND (%(hash)s::text IS NULL OR token_hash = %(hash)s)
         RETURNING *
        """,
        {
            "cpu": body.get("cpu_pct"),
            "ram_used": body.get("ram_used_mb"),
            "ram_total": body.get("ram_total_mb"),
            "role": body.get("reported_role"),
            "ack": body.get("acked_epoch"),
            "code": body.get("code_version"),
            "skew": body.get("skew_ms"),
            "wid": worker_id,
            "hash": token_hash,
        },
    ).fetchone()


def _preempt_ids(conn: psycopg.Connection, worker_id: str) -> list[str]:
    """Step 4: jobs this worker must stop and hand back."""
    rows = conn.execute(
        """
        SELECT id FROM jobs
         WHERE lease_worker_id = %s AND status IN ('leased', 'cancel_requested')
           AND (preempt_requested OR status = 'cancel_requested')
         ORDER BY created_at
        """,
        (worker_id,),
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _may_claim(worker: dict[str, Any], body: dict[str, Any], kill: bool) -> bool:
    """Step 6 preconditions."""
    return (
        not kill
        and bool(worker["enabled"])
        and bool(body.get("want_job"))
        and worker["reported_role"] == worker["desired_role"]
        and worker["acked_epoch"] == worker["role_epoch"]
        and worker["desired_role"] in BATCH_ROLES
    )


def _reported_ids(body: dict[str, Any]) -> list[str]:
    """Every job id the heartbeat mentions (running or released)."""
    ids: list[str] = []
    for key in ("jobs", "released"):
        for entry in body.get(key) or []:
            if entry.get("id"):
                ids.append(str(entry["id"]))
    return ids


def process_heartbeat(
    conn: psycopg.Connection, worker_id: str, body: dict[str, Any], token_hash: str | None = None
) -> dict[str, Any]:
    """POST /api/v1/workers/{id}/heartbeat, one transaction, contract order.

    `token_hash` is the sha256 of the bearer token; when given it is verified under
    the worker row lock (the API always passes it, direct callers may skip it).
    """
    worker = _update_worker(conn, worker_id, body, token_hash)
    if worker is None:
        raise Unauthorized("invalid worker token")
    lease = lease_seconds(conn)
    lost: list[str] = []
    for entry in body.get("jobs") or []:
        ok = renew(conn, worker_id, entry.get("id"), entry.get("lease_token"), lease,
                   entry.get("progress"), entry.get("checkpoint"))
        if not ok:
            lost.append(str(entry.get("id")))
    for entry in body.get("released") or []:
        release(conn, entry.get("id"), entry.get("lease_token"), entry.get("progress"),
                entry.get("checkpoint"), worker_id)
    preempt = _preempt_ids(conn, worker_id)
    worker = auto_return_to_idle(conn, worker_id) or worker
    kill = kill_switch(conn)
    claimed: list[dict[str, Any]] = []
    if _may_claim(worker, body, kill):
        # A lease this worker holds but did not report (its claim reply was lost) is
        # handed back first; nothing new is claimed while one exists.
        orphans = orphan_jobs(conn, worker_id, _reported_ids(body), lease)
        if orphans:
            claimed = [job_payload(row, lease) for row in orphans]
        else:
            row = claim(conn, worker_id, worker["desired_role"], lease)
            if row is not None:
                claimed.append(job_payload(row, lease))
    reply = _common_reply(conn, worker)
    reply.update({"kill": kill, "preempt": preempt, "lost": lost, "claimed": claimed})
    return reply
