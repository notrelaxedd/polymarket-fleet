"""Register and heartbeat transactions (the worker side of the contract)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg

from host import auth
from host.errors import Unauthorized
from host.events import add_audit
from host.leases import claim_many, job_payload, lease_seconds, release, renew
from host.reboot import PENDING_SQL, finish_reboot
from host.recovery import held_jobs, orphan_jobs
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
        "heartbeat_seconds": get_int_setting(conn, "heartbeat_seconds", 3),
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
                             boot_id, remote_ip, can_reboot, boot_media)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, false), %s) RETURNING *
        """,
        (worker_id, name, auth.hash_token(plain)) + _machine_fields(body, remote_ip)
        + (body.get("can_reboot"), body.get("boot_media")),
    ).fetchone()
    auth.mark_enroll_token_used(conn, token_row["token_hash"], worker_id)
    add_audit(
        conn, "worker_enrolled", worker_id, worker_id, None,
        {"name": name, "hostname": body.get("hostname")}, ip=remote_ip,
    )
    worker["_plain_token"] = plain
    return worker


def _reregister(conn: psycopg.Connection, body: dict[str, Any], remote_ip: str | None) -> dict[str, Any]:
    """Re-registration: verify the current (or previous) token under the row lock, then rotate it.

    A pending (or expired) reboot request is finished when the boot_id changed.
    `can_reboot` and `boot_media` are stored when the agent sends them.
    """
    worker_id = str(body.get("worker_id", ""))
    before, presented = auth.verify_register_token(conn, worker_id, str(body.get("worker_token", "")))
    plain = auth.rotate_worker_token(conn, worker_id, presented)
    finish_reboot(conn, before, body.get("boot_id"), remote_ip)
    worker = conn.execute(
        """
        UPDATE workers SET hostname = COALESCE(%s, hostname), python_version = %s,
               code_version = %s, boot_id = %s, remote_ip = %s,
               can_reboot = COALESCE(%s, can_reboot), boot_media = COALESCE(%s, boot_media)
         WHERE id = %s RETURNING *
        """,
        _machine_fields(body, remote_ip) + (body.get("can_reboot"), body.get("boot_media"), worker_id),
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
    The machine health fields are stored as reported (null when the agent does not
    know them); boot_media keeps its last value when absent. The returned row carries
    `reboot_pending` (host.reboot.PENDING_SQL).
    """
    return conn.execute(
        f"""
        UPDATE workers SET last_heartbeat_at = now(), cpu_pct = %(cpu)s, ram_used_mb = %(ram_used)s,
               ram_total_mb = %(ram_total)s, reported_role = COALESCE(%(role)s, reported_role),
               acked_epoch = GREATEST(acked_epoch, COALESCE(%(ack)s, acked_epoch)),
               code_version = COALESCE(%(code)s, code_version), skew_ms = %(skew)s,
               temp_c = %(temp)s, boot_media = COALESCE(%(media)s, boot_media),
               wear_pct = %(wear)s, disk_gb_written = %(written)s,
               prev_token_hash = NULL
         WHERE id = %(wid)s AND (%(hash)s::text IS NULL OR token_hash = %(hash)s)
         RETURNING *, {PENDING_SQL} AS reboot_pending
        """,
        {
            "temp": body.get("temp_c"),
            "media": body.get("boot_media"),
            "wear": body.get("wear_pct"),
            "written": body.get("disk_gb_written"),
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


def _preempt_ids(conn: psycopg.Connection, worker_id: str) -> tuple[list[str], list[str]]:
    """Step 4: jobs this worker must stop and hand back, as (preempt, cancel).

    `preempt` holds every id (preempt_requested or cancel_requested); `cancel` the
    subset whose job is cancel_requested, so the agent can name the release reason.
    """
    rows = conn.execute(
        """
        SELECT id, status FROM jobs
         WHERE lease_worker_id = %s AND status IN ('leased', 'cancel_requested')
           AND (preempt_requested OR status = 'cancel_requested')
         ORDER BY created_at
        """,
        (worker_id,),
    ).fetchall()
    preempt = [str(row["id"]) for row in rows]
    cancel = [str(row["id"]) for row in rows if row["status"] == "cancel_requested"]
    return preempt, cancel


MAX_TRADE_SLOTS = 100


def _claim_slots(worker: dict[str, Any], body: dict[str, Any], kill: bool) -> int:
    """Step 6 preconditions: how many jobs this heartbeat may claim.

    Batch roles (backtest, model_search, train) claim one job when `want_job` is set
    and keep claiming under kill: a stopped backtest hides research for no safety
    gain. A trade worker asks for its free slots with `want_jobs` (int) and gets
    nothing under kill.
    """
    role = worker["desired_role"]
    settled = (
        bool(worker["enabled"])
        and worker["reported_role"] == worker["desired_role"]
        and worker["acked_epoch"] == worker["role_epoch"]
    )
    if not settled:
        return 0
    if role in BATCH_ROLES:
        return 1 if body.get("want_job") else 0
    if role == "trade" and not kill:
        wanted = body.get("want_jobs")
        if isinstance(wanted, bool) or not isinstance(wanted, int):
            return 0
        return max(0, min(wanted, MAX_TRADE_SLOTS))
    return 0


def _trade_slots_left(conn: psycopg.Connection, worker_id: str) -> int:
    """The host-side cap on trade claims: `trade_max_games` minus the trade jobs this
    worker already holds, so a worker's `want_jobs` can never exceed the setting."""
    cap = get_int_setting(conn, "trade_max_games", 6)
    held = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE lease_worker_id = %s AND kind = 'trade' AND status IN ('leased', 'cancel_requested')",
        (worker_id,),
    ).fetchone()["n"]
    return max(0, cap - int(held))


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
    While an owner reboot request is pending the reply names it in `reboot` and
    nothing new is claimed (host/reboot.py).
    """
    worker = _update_worker(conn, worker_id, body, token_hash)
    if worker is None:
        raise Unauthorized("invalid worker token")
    reboot = worker["reboot_id"] if worker["reboot_pending"] else None
    lease = lease_seconds(conn)
    lost: list[str] = []
    for entry in body.get("jobs") or []:
        ok = renew(conn, worker_id, entry.get("id"), entry.get("lease_token"), lease,
                   entry.get("progress"), entry.get("checkpoint"))
        if not ok:
            lost.append(str(entry.get("id")))
    for entry in body.get("released") or []:
        release(conn, entry.get("id"), entry.get("lease_token"), entry.get("progress"),
                entry.get("checkpoint"), worker_id, entry.get("reason"))
    preempt, cancel = _preempt_ids(conn, worker_id)
    worker = auto_return_to_idle(conn, worker_id) or worker
    kill = kill_switch(conn)
    claimed: list[dict[str, Any]] = []
    slots = 0 if reboot else _claim_slots(worker, body, kill)  # a worker about to reboot takes nothing new
    if slots > 0 and worker["desired_role"] == "trade":
        slots = min(slots, _trade_slots_left(conn, worker_id))
    if slots > 0:
        # A lease this worker holds but did not report (its claim reply was lost) is
        # handed back first; nothing new is claimed while one exists.
        orphans = orphan_jobs(conn, worker_id, _reported_ids(body), lease)
        if orphans:
            claimed = [job_payload(row, lease) for row in orphans]
        else:
            rows = claim_many(conn, worker_id, worker["desired_role"], lease, slots)
            claimed = [job_payload(row, lease) for row in rows]
    reply = _common_reply(conn, worker)
    reply.update(
        {"kill": kill, "preempt": preempt, "cancel": cancel, "lost": lost, "claimed": claimed, "reboot": reboot}
    )
    return reply
