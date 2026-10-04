"""The owner's model edits: the summary text and retiring a lineage (audited)."""
from __future__ import annotations

from typing import Any

import psycopg

from host.errors import BadRequest
from host.events import add_audit
from host.models import MAX_SUMMARY, _summary, get_model


def set_summary(conn: psycopg.Connection, model_id: Any, summary: Any, actor: str | None) -> dict[str, Any]:
    """Owner edit of the summary text (at most MAX_SUMMARY characters), audited."""
    if not isinstance(summary, str):
        raise BadRequest("summary must be a string")
    text = _summary(summary, MAX_SUMMARY)
    model = get_model(conn, model_id, for_update=True)
    row = conn.execute(
        "UPDATE models SET summary = %s, updated_at = now() WHERE id = %s RETURNING *", (text, model["id"])
    ).fetchone()
    add_audit(conn, "model_summary", str(model["id"]), actor, {"summary": model["summary"]}, {"summary": text})
    return row


def retire(conn: psycopg.Connection, model_id: Any, status: Any, actor: str | None) -> dict[str, Any]:
    """Owner status change: only `retired`, applied to the whole lineage, audited."""
    if status != "retired":
        raise BadRequest("the owner can only set status retired")
    from host.trading import assignments

    model = get_model(conn, model_id, for_update=True)
    conn.execute(
        "UPDATE models SET status = 'retired', updated_at = now() WHERE lineage_id = %s AND status <> 'retired'",
        (model["lineage_id"],),
    )
    # A retired lineage trades nothing more: every active assignment is halted, which
    # cancels its approved rows at once and asks the exchange to cancel the live ones.
    active = conn.execute(
        "SELECT id FROM assignments WHERE lineage_id = %s AND status = 'active' ORDER BY created_at", (model["lineage_id"],)
    ).fetchall()
    halted = [str(assignments.halt_assignment(conn, r["id"], actor, "lineage retired")["id"]) for r in active]
    add_audit(conn, "model_retired", str(model["id"]), actor, {"status": model["status"]},
              {"status": "retired", "assignments_halted": halted})
    return get_model(conn, model_id)
