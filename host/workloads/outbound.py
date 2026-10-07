"""Outbound actions: a workload queues them, the owner approves, the host sends.

Nothing is ever sent unless a row is `approved`. Polymarket orders are not involved.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host.errors import BadRequest, Conflict, Forbidden, NotFound
from host.events import add_audit
from host.leases import as_uuid
from host.workloads.errors import SecretsUnavailable, TooMany
from host.workloads.registry import get_workload, manifest_of
from host.workloads.secrets import host_only_secrets
from host.workloads.senders import EmailSender, LogSender, Sender, default_senders  # noqa: F401

log = logging.getLogger(__name__)
MAX_PENDING = 500
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_DEDUPE_CHARS = 200
STATUSES = ("pending", "approved", "rejected", "sending", "sent", "failed", "expired")


def get_action(conn: psycopg.Connection, action_id: Any, for_update: bool = False) -> dict[str, Any]:
    """One outbound_actions row; 404 when missing or not a uuid."""
    aid = as_uuid(action_id)
    row = None
    if aid is not None:
        row = conn.execute(
            "SELECT * FROM outbound_actions WHERE id = %s" + (" FOR UPDATE" if for_update else ""), (aid,)
        ).fetchone()
    if row is None:
        raise NotFound("outbound action not found")
    return row


def queue_action(
    conn: psycopg.Connection, *, workload: str, machine_id: str | None, job_id: Any, kind: str,
    payload: dict[str, Any], dedupe_key: str,
) -> dict[str, Any]:
    """Queue an action for approval; idempotent on (workload, dedupe_key).

    403 for a kind the manifest does not declare, 400 for a payload above 64 KiB or a bad
    dedupe key or job, 429 when the workload already has MAX_PENDING pending.
    """
    manifest = manifest_of(get_workload(conn, workload))
    if kind not in manifest.outbound_actions:
        raise Forbidden(f"workload {workload!r} does not declare the outbound kind {kind!r}")
    if not isinstance(payload, dict):
        raise BadRequest("payload must be a JSON object")
    if len(json.dumps(payload)) > MAX_PAYLOAD_BYTES:
        raise BadRequest(f"payload larger than {MAX_PAYLOAD_BYTES} bytes")
    if not isinstance(dedupe_key, str) or not 1 <= len(dedupe_key) <= MAX_DEDUPE_CHARS:
        raise BadRequest(f"dedupe_key must be 1..{MAX_DEDUPE_CHARS} characters")
    existing = conn.execute(
        "SELECT * FROM outbound_actions WHERE workload = %s AND dedupe_key = %s", (workload, dedupe_key)
    ).fetchone()
    if existing is not None:
        return existing
    jid = None
    if job_id is not None:
        jid = as_uuid(job_id)
        if jid is None or conn.execute(
            "SELECT 1 FROM workload_jobs WHERE id = %s AND workload = %s", (jid, workload)
        ).fetchone() is None:
            raise BadRequest("job_id is not a job of this workload")
    pending = conn.execute(
        "SELECT count(*) AS n FROM outbound_actions WHERE workload = %s AND status = 'pending'", (workload,)
    ).fetchone()["n"]
    if pending >= MAX_PENDING:
        raise TooMany(f"workload {workload!r} already has {MAX_PENDING} actions waiting for approval")
    row = conn.execute(
        """
        INSERT INTO outbound_actions (workload, machine_id, job_id, kind, payload, dedupe_key)
        VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (workload, dedupe_key) DO NOTHING RETURNING *
        """,
        (workload, machine_id, jid, kind, Jsonb(payload), dedupe_key),
    ).fetchone()
    if row is None:  # lost a race with the same key
        row = conn.execute(
            "SELECT * FROM outbound_actions WHERE workload = %s AND dedupe_key = %s", (workload, dedupe_key)
        ).fetchone()
    return row


def _decide(conn: psycopg.Connection, action_id: Any, status: str, actor: str | None, ip: str | None,
            audit: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
    action = get_action(conn, action_id, for_update=True)
    if action["status"] != "pending":
        raise Conflict(f"action is {action['status']}, not pending")
    row = conn.execute(
        "UPDATE outbound_actions SET status = %s, decided_by = %s, decided_at = now(), result = %s WHERE id = %s RETURNING *",
        (status, actor, Jsonb(result) if result is not None else None, action["id"]),
    ).fetchone()
    add_audit(conn, audit, str(action["id"]), actor, {"status": "pending"},
              {"status": status, "workload": action["workload"], "kind": action["kind"], **(result or {})}, ip)
    return row


def approve(conn: psycopg.Connection, action_id: Any, actor: str | None, ip: str | None) -> dict[str, Any]:
    """pending -> approved (the host sends it on its next pass); 409 otherwise."""
    return _decide(conn, action_id, "approved", actor, ip, "outbound_approve")


def reject(conn: psycopg.Connection, action_id: Any, actor: str | None, ip: str | None, reason: str = "") -> dict[str, Any]:
    """pending -> rejected; 409 otherwise."""
    return _decide(conn, action_id, "rejected", actor, ip, "outbound_reject", {"reason": (reason or "")[:500]})


def expire_old(conn: psycopg.Connection, days: int = 7) -> int:
    """Pending actions older than `days` become expired; returns the count."""
    rows = conn.execute(
        "UPDATE outbound_actions SET status = 'expired' WHERE status = 'pending'"
        " AND created_at < now() - make_interval(days => %s) RETURNING id",
        (days,),
    ).fetchall()
    return len(rows)


def _redact(text: str, secrets: dict[str, str]) -> str:
    for value in secrets.values():
        if value:
            text = text.replace(value, "[redacted]")
    return text[:500]


def send_approved(conn: psycopg.Connection, senders: dict[str, Sender], limit: int | None = None) -> int:
    """Send approved actions (oldest decision first, at most `limit`): approved -> sending ->
    sent or failed. Returns the number sent.

    The `sending` mark is committed before the sender runs, so a crash mid-send leaves a
    `sending` row (visible to the owner) rather than a second send on the next pass.
    """
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM outbound_actions WHERE status = 'approved' ORDER BY decided_at, created_at LIMIT %s",
        (limit,),
    ).fetchall()]
    sent = 0
    for action_id in ids:
        action = conn.execute(
            "UPDATE outbound_actions SET status = 'sending' WHERE id = %s AND status = 'approved' RETURNING *",
            (action_id,),
        ).fetchone()
        if action is None:
            continue
        conn.commit()
        secrets: dict[str, str] = {}
        try:
            sender = senders.get(action["kind"])
            if sender is None:
                raise ValueError(f"no sender for kind {action['kind']!r}")
            try:
                secrets = host_only_secrets(conn, action["workload"])
            except SecretsUnavailable:
                secrets = {}
            result = sender.send(action, secrets)
            conn.execute(
                "UPDATE outbound_actions SET status = 'sent', sent_at = now(), result = %s, error = NULL WHERE id = %s",
                (Jsonb(result or {}), action["id"]),
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001 - a failing sender must not stop the rest
            message = _redact(f"{type(exc).__name__}: {exc}", secrets)
            log.warning("outbound action %s failed: %s", action["id"], message)
            conn.rollback()
            conn.execute("UPDATE outbound_actions SET status = 'failed', error = %s WHERE id = %s", (message, action["id"]))
        conn.commit()
    return sent
