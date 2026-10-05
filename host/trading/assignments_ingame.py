"""The in-game fields of an assignment (contract section 7, docs/INGAME.md).

An assignment carries its pre-game model (`model_id`) and optionally an in-game model
(`ingame_model_id`, an `ingame_wp` model whose lineage is not retired) plus the switch
`trade_ingame` (default: settings.trade_ingame). In-game trading is on when both are
set. In-game orders are paper-only in this step, so `trade_ingame` cannot be turned on
for a live assignment (the approval's "ingame_paper_only" stays the second guard).

The owner toggles them with `set_ingame` (POST /api/assignments/{id}/ingame, audited
as "assignment_ingame"). Turning in-game trading off cancels the assignment's open
in-game orders. The in-game model cannot change once the assignment has an open or
filled in-game order: settlement attributes those orders' bets rows to the
assignment's ingame_model_id, so it must be the model that placed them.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg

from host.errors import BadRequest, Conflict
from host.events import add_audit
from host.kill import approval_lock
from host.settings import get_setting
from host.trading import orders
from host.trading.orders import ACTIVE_STATUSES

INGAME_FAMILY = "ingame_wp"
PAPER_ONLY = "in-game trading is paper-only in this step"
FIELDS = ("ingame_model_id", "trade_ingame")


def default_trade_ingame(conn: psycopg.Connection) -> bool:
    """settings.trade_ingame (false unless the JSON boolean true)."""
    return get_setting(conn, "trade_ingame", False) is True


def _lineage_retired(conn: psycopg.Connection, lineage_id: Any) -> bool:
    row = conn.execute("SELECT 1 FROM models WHERE lineage_id = %s AND status = 'retired' LIMIT 1", (lineage_id,)).fetchone()
    return row is not None


def ingame_model(conn: psycopg.Connection, model_id: Any) -> dict[str, Any] | None:
    """The checked in-game model (None for None or ""): 400 unless it is an existing
    ingame_wp model of a lineage that is not retired."""
    if model_id is None or model_id == "":
        return None
    try:
        mid = uuid.UUID(str(model_id))
    except (ValueError, TypeError):
        raise BadRequest("unknown in-game model") from None
    row = conn.execute("SELECT * FROM models WHERE id = %s", (mid,)).fetchone()
    if row is None:
        raise BadRequest("unknown in-game model")
    if row["family"] != INGAME_FAMILY:
        raise BadRequest(f"the in-game model must be an {INGAME_FAMILY} model, not {row['family']}")
    if row["status"] == "retired" or _lineage_retired(conn, row["lineage_id"]):
        raise BadRequest("the in-game model's lineage is retired")
    return dict(row)


def check_pregame_model(model: dict[str, Any]) -> None:
    """An ingame_wp model has no pre-game rule: it goes in ingame_model_id."""
    if model.get("family") == INGAME_FAMILY:
        raise BadRequest(f"an {INGAME_FAMILY} model trades only in-game: set it as the in-game model of an assignment")


def resolve_new(conn: psycopg.Connection, mode: str, ingame_model_id: Any, trade_ingame: bool | None) -> tuple[Any, bool]:
    """(ingame model id or None, trade_ingame) for a new assignment. trade_ingame left
    out takes the settings default (false for a live assignment); an explicit true on
    a live assignment is refused."""
    if trade_ingame is not None and not isinstance(trade_ingame, bool):
        raise BadRequest("trade_ingame must be true or false")
    model = ingame_model(conn, ingame_model_id)
    if trade_ingame is None:
        trade_ingame = default_trade_ingame(conn) and mode != "live"
    if trade_ingame and mode == "live":
        raise Conflict(PAPER_ONLY)
    return (None if model is None else model["id"]), trade_ingame


def _has_ingame_orders(conn: psycopg.Connection, assignment_id: Any) -> bool:
    row = conn.execute(
        "SELECT 1 FROM orders WHERE assignment_id = %s AND ingame AND (filled_size > 0 OR status = ANY(%s)) LIMIT 1",
        (assignment_id, list(ACTIVE_STATUSES)),
    ).fetchone()
    return row is not None


def cancel_ingame_orders(conn: psycopg.Connection, assignment_id: Any, actor: str | None, reason: str) -> int:
    """Cancel the assignment's open in-game orders (paper at once); how many were touched."""
    rows = conn.execute(
        "SELECT id FROM orders WHERE assignment_id = %s AND ingame AND status = ANY(%s) ORDER BY created_at",
        (assignment_id, list(ACTIVE_STATUSES)),
    ).fetchall()
    for row in rows:
        orders.cancel_order(conn, row["id"], actor, reason)
    return len(rows)


def set_ingame(conn: psycopg.Connection, assignment_id: Any, actor: str | None, changes: dict[str, Any]) -> dict[str, Any]:
    """Set `ingame_model_id` and/or `trade_ingame` (only the keys present in `changes`)
    on an active or halted assignment; the updated row plus "orders_cancelled".

    400 on a bad model or value, 409 for a settled or cancelled assignment, for
    trade_ingame on a live assignment, and for a model change once in-game orders
    exist. Runs under the mode's approval lock, as a halt does."""
    from host.trading.assignments import get_assignment

    unknown = sorted(set(changes) - set(FIELDS))
    if unknown or not changes:
        raise BadRequest("give ingame_model_id and/or trade_ingame")
    approval_lock(conn, get_assignment(conn, assignment_id)["mode"])
    row = get_assignment(conn, assignment_id, for_update=True)
    if row["status"] not in ("active", "halted"):
        raise Conflict(f"assignment is {row['status']}")
    model_id = row["ingame_model_id"]
    if "ingame_model_id" in changes:
        model = ingame_model(conn, changes["ingame_model_id"])
        model_id = None if model is None else model["id"]
    trade = row["trade_ingame"]
    if "trade_ingame" in changes:
        if not isinstance(changes["trade_ingame"], bool):
            raise BadRequest("trade_ingame must be true or false")
        trade = changes["trade_ingame"]
    if trade and row["mode"] == "live":
        raise Conflict(PAPER_ONLY)
    if str(model_id) != str(row["ingame_model_id"]) and _has_ingame_orders(conn, row["id"]):
        raise Conflict("the assignment has in-game orders of its in-game model; turn trade_ingame off instead")
    was_on = bool(row["trade_ingame"] and row["ingame_model_id"] is not None)
    cancelled = 0
    if was_on and not (trade and model_id is not None):
        cancelled = cancel_ingame_orders(conn, row["id"], actor, "in-game trading turned off")
    after = conn.execute(
        "UPDATE assignments SET ingame_model_id = %s, trade_ingame = %s, updated_at = now() WHERE id = %s RETURNING *",
        (model_id, trade, row["id"]),
    ).fetchone()
    add_audit(
        conn, "assignment_ingame", str(row["id"]), actor,
        {"ingame_model_id": None if row["ingame_model_id"] is None else str(row["ingame_model_id"]),
         "trade_ingame": row["trade_ingame"]},
        {"ingame_model_id": None if model_id is None else str(model_id), "trade_ingame": trade,
         "orders_cancelled": cancelled},
    )
    out = dict(after)
    out["orders_cancelled"] = cancelled
    return out
