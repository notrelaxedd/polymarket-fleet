"""Owner reboot requests: the request transaction, the pending check the heartbeat
and /api/fleet share, and the register-side "the reboot happened" bookkeeping."""
from __future__ import annotations

import secrets
from typing import Any

import psycopg

from host.errors import Conflict
from host.events import add_audit
from host.scheduling import get_worker, online_after

REBOOT_WINDOW_SECONDS = 300

# SQL over a workers row (aliased or not): a reboot request is pending while its id is
# set and it is younger than REBOOT_WINDOW_SECONDS by the database clock.
PENDING_SQL = (
    f"(reboot_id IS NOT NULL AND reboot_requested_at > now() - make_interval(secs => {REBOOT_WINDOW_SECONDS}))"
)

OFFLINE = "worker is offline"
CANNOT_REBOOT = "this worker cannot reboot yet: re-run install.sh on it"


def new_reboot_id() -> str:
    """A fresh request id, short and filename safe (the agent writes it to a file)."""
    return "rb_" + secrets.token_hex(8)


def request_reboot(conn: psycopg.Connection, worker_id: str, actor: str | None) -> dict[str, Any]:
    """POST /api/workers/{id}/reboot, one transaction.

    A pending request is returned as is (idempotent). Otherwise the worker must be
    online and its install must be able to reboot (409 each), and a new request id is
    stored and audited as `reboot_requested`.
    """
    get_worker(conn, worker_id, for_update=True)
    row = conn.execute(
        f"""
        SELECT reboot_id, reboot_requested_at, can_reboot, {PENDING_SQL} AS pending,
               (last_heartbeat_at > now() - make_interval(secs => %s)) AS online
          FROM workers WHERE id = %s
        """,
        (online_after(conn), worker_id),
    ).fetchone()
    if row["pending"]:
        return {"worker_id": worker_id, "reboot_id": row["reboot_id"], "requested_at": row["reboot_requested_at"]}
    if not row["online"]:
        raise Conflict(OFFLINE)
    if not row["can_reboot"]:
        raise Conflict(CANNOT_REBOOT)
    after = conn.execute(
        """
        UPDATE workers SET reboot_id = %s, reboot_requested_at = now()
         WHERE id = %s RETURNING reboot_id, reboot_requested_at
        """,
        (new_reboot_id(), worker_id),
    ).fetchone()
    add_audit(
        conn, "reboot_requested", worker_id, actor,
        {"reboot_id": row["reboot_id"]}, {"reboot_id": after["reboot_id"]},
    )
    return {"worker_id": worker_id, "reboot_id": after["reboot_id"], "requested_at": after["reboot_requested_at"]}


def finish_reboot(
    conn: psycopg.Connection, worker: dict[str, Any], boot_id: str | None, remote_ip: str | None
) -> None:
    """Register: a reboot request is done once the worker comes back with another
    boot_id. Clears the request (pending or expired) and audits `reboot_done`.
    `worker` is the row as it was before register stored the new boot_id."""
    if not worker.get("reboot_id") or not boot_id or boot_id == worker.get("boot_id"):
        return
    conn.execute(
        "UPDATE workers SET reboot_id = NULL, reboot_requested_at = NULL WHERE id = %s", (worker["id"],)
    )
    add_audit(
        conn, "reboot_done", worker["id"], worker["id"],
        {"reboot_id": worker["reboot_id"], "boot_id": worker.get("boot_id")},
        {"boot_id": boot_id}, ip=remote_ip,
    )
