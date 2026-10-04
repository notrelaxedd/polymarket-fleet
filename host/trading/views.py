"""Read-only JSON views for the owner trading API and the /trading dashboard:
orders, fills, markets (and the owner's link-by-hand) and the exchange state."""
from __future__ import annotations

import uuid
from datetime import timezone
from typing import Any

import psycopg

from host.errors import BadRequest, NotFound
from host.events import add_audit

ORDER_STATUSES = (
    "rejected", "approved", "submitting", "open", "partial", "filled", "cancel_requested", "cancelled",
    "rejected_by_exchange", "expired",
)
ACTIVE = ("approved", "submitting", "open", "partial", "cancel_requested")


def _limit(limit: int, cap: int = 500) -> int:
    return max(1, min(int(limit), cap))


def parse_uuid(value: Any, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise NotFound(f"{what} not found") from None


def list_orders(
    conn: psycopg.Connection,
    status: str | None = None,
    limit: int = 50,
    assignment_id: Any = None,
    worker_id: str | None = None,
) -> list[dict[str, Any]]:
    """Newest orders first with the market, game, mode and worker name; `status`
    may be one status or "active" (approved .. cancel_requested)."""
    if status and status != "active" and status not in ORDER_STATUSES:
        raise BadRequest(f"unknown status: {status!r}")
    clauses, params = ["true"], []
    if status == "active":
        clauses.append("o.status = ANY(%s)")
        params.append(list(ACTIVE))
    elif status:
        clauses.append("o.status = %s")
        params.append(status)
    if assignment_id is not None:
        clauses.append("o.assignment_id = %s")
        params.append(parse_uuid(assignment_id, "assignment"))
    if worker_id is not None:
        clauses.append("o.worker_id = %s")
        params.append(worker_id)
    params.append(_limit(limit))
    rows = conn.execute(
        f"""
        SELECT o.*, m.title AS market_title, m.side, m.game_id, m.platform, w.name AS worker_name,
               a.model_id, a.max_bet_cents AS assignment_max_bet_cents, mo.family,
               (SELECT e.detail ->> 'reason' FROM order_events e WHERE e.order_id = o.id
                  AND e.to_status IN ('cancelled', 'cancel_requested', 'expired', 'rejected_by_exchange')
                 ORDER BY e.id DESC LIMIT 1) AS last_reason
          FROM orders o
          JOIN markets m ON m.id = o.market_id
          LEFT JOIN workers w ON w.id = o.worker_id
          LEFT JOIN assignments a ON a.id = o.assignment_id
          LEFT JOIN models mo ON mo.id = a.model_id
         WHERE {' AND '.join(clauses)}
         ORDER BY o.created_at DESC, o.id LIMIT %s
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def order_with_events(conn: psycopg.Connection, order_id: Any) -> dict[str, Any]:
    """One order plus its events and fills in chronological order."""
    oid = parse_uuid(order_id, "order")
    row = conn.execute("SELECT * FROM orders WHERE id = %s", (oid,)).fetchone()
    if row is None:
        raise NotFound("order not found")
    out = dict(row)
    out["events"] = [dict(e) for e in conn.execute(
        "SELECT * FROM order_events WHERE order_id = %s ORDER BY id", (oid,)
    ).fetchall()]
    out["fills"] = [dict(f) for f in conn.execute("SELECT * FROM fills WHERE order_id = %s ORDER BY id", (oid,)).fetchall()]
    return out


def list_fills(conn: psycopg.Connection, limit: int = 50, assignment_id: Any = None) -> list[dict[str, Any]]:
    """Newest fills first with their order, market and assignment."""
    clauses, params = ["true"], []
    if assignment_id is not None:
        clauses.append("o.assignment_id = %s")
        params.append(parse_uuid(assignment_id, "assignment"))
    params.append(_limit(limit))
    rows = conn.execute(
        f"""
        SELECT f.*, o.assignment_id, o.worker_id, o.market_id, o.price AS order_price, o.size AS order_size,
               m.title AS market_title, m.side, m.game_id
          FROM fills f
          JOIN orders o ON o.id = f.order_id
          JOIN markets m ON m.id = o.market_id
         WHERE {' AND '.join(clauses)}
         ORDER BY f.ts DESC, f.id DESC LIMIT %s
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def list_markets(conn: psycopg.Connection, unmatched: bool = False, game_id: str | None = None) -> list[dict[str, Any]]:
    """Markets with their latest snapshot age; `unmatched` keeps only those not
    confirmed to a game (the ones the owner links by hand)."""
    clauses, params = ["true"], []
    if unmatched:
        clauses.append("(NOT m.mapping_confirmed OR m.game_id IS NULL)")
    if game_id:
        clauses.append("m.game_id = %s")
        params.append(game_id)
    rows = conn.execute(
        f"""
        SELECT m.*, g.home_team, g.away_team, g.kickoff_at,
               EXTRACT(EPOCH FROM (now() - m.last_snapshot_at)) AS snapshot_age_s
          FROM markets m LEFT JOIN games g ON g.game_id = m.game_id
         WHERE {' AND '.join(clauses)}
         ORDER BY m.mapping_confirmed, g.kickoff_at NULLS LAST, m.title
        """,
        params,
    ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        row["snapshot_age_s"] = None if row["snapshot_age_s"] is None else float(row["snapshot_age_s"])
        out.append(row)
    return out


def link_market(conn: psycopg.Connection, market_id: Any, game_id: str, side: str, actor: str | None) -> dict[str, Any]:
    """Owner confirms a market's game and side by hand (mapping_confidence 1.0)."""
    if side not in ("home", "away"):
        raise BadRequest("side must be home or away")
    mid = parse_uuid(market_id, "market")
    game = conn.execute("SELECT game_id FROM games WHERE game_id = %s", (str(game_id),)).fetchone()
    if game is None:
        raise BadRequest(f"unknown game {game_id!r}")
    before = conn.execute("SELECT * FROM markets WHERE id = %s FOR UPDATE", (mid,)).fetchone()
    if before is None:
        raise NotFound("market not found")
    row = conn.execute(
        """
        UPDATE markets SET game_id = %s, side = %s, mapping_confirmed = true, mapping_confidence = 1.0,
               updated_at = now() WHERE id = %s RETURNING *
        """,
        (game["game_id"], side, mid),
    ).fetchone()
    add_audit(
        conn, "market_linked", str(mid), actor,
        {"game_id": before["game_id"], "side": before["side"], "mapping_confirmed": before["mapping_confirmed"]},
        {"game_id": game["game_id"], "side": side, "mapping_confirmed": True},
    )
    return dict(row)


def exchange_state(conn: psycopg.Connection) -> dict[str, Any]:
    """The exchange_state row plus `heartbeat_age_s` and `down` (older than 15 s or
    never seen). Uses host.exchange.state.read_state when that module exists."""
    try:
        from host.exchange.state import read_state
    except ImportError:
        read_state = None  # type: ignore[assignment]
    row = read_state(conn) if read_state is not None else conn.execute("SELECT * FROM exchange_state WHERE id").fetchone()
    out = dict(row or {})
    now = conn.execute("SELECT now() AS t").fetchone()["t"].astimezone(timezone.utc)
    beat = out.get("heartbeat_at")
    age = None if beat is None else max(0.0, (now - beat.astimezone(timezone.utc)).total_seconds())
    out["heartbeat_age_s"] = age
    out["down"] = age is None or age > 15
    return out
