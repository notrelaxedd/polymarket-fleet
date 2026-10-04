"""Assignments: one game, one model, one mode, one bankroll, one trade job.

Rules (docs/TRADING.md "Assignments, bankrolls, ledger"): the lineage must not be
retired; paper needs nothing else; live needs live_enabled, a live_eligible lineage
and exchange auth; at most max_paper_models_per_game paper assignments per game and
exactly one live (the partial unique indexes are the database guarantee). Creating
one funds a bankroll and inserts a `trade` job; halting cancels its open orders and
leaves the job leased; settlement (host/exchange/settle.py) completes the job.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from host.errors import BadRequest, Conflict, NotFound
from host.events import add_audit, add_job_event
from host.kill import approval_lock, cancel_active_orders
from host.money import MAX_CENTS
from host.settings import get_int_setting, get_setting
from host.trading import ledger
from host.trading.orders import ACTIVE_STATUSES
from host.trading.state import trade_state  # noqa: F401 - re-exported for the API

MODES = ("paper", "live")
ACTIVE_LIST = "', '".join(ACTIVE_STATUSES)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def get_assignment(conn: psycopg.Connection, assignment_id: Any, for_update: bool = False) -> dict[str, Any]:
    """One assignment row; 404 when missing or not a uuid."""
    try:
        aid = uuid.UUID(str(assignment_id))
    except (ValueError, TypeError):
        raise NotFound("assignment not found") from None
    sql = "SELECT * FROM assignments WHERE id = %s" + (" FOR UPDATE" if for_update else "")
    row = conn.execute(sql, (aid,)).fetchone()
    if row is None:
        raise NotFound("assignment not found")
    return dict(row)


def _model(conn: psycopg.Connection, model_id: Any) -> dict[str, Any]:
    try:
        mid = uuid.UUID(str(model_id))
    except (ValueError, TypeError):
        raise BadRequest("unknown model") from None
    row = conn.execute("SELECT * FROM models WHERE id = %s", (mid,)).fetchone()
    if row is None:
        raise BadRequest("unknown model")
    return dict(row)


def lineage_retired(conn: psycopg.Connection, lineage_id: Any) -> bool:
    row = conn.execute("SELECT 1 FROM models WHERE lineage_id = %s AND status = 'retired' LIMIT 1", (lineage_id,)).fetchone()
    return row is not None


def live_gate(conn: psycopg.Connection, model: dict[str, Any]) -> None:
    """The three live preconditions; 409 naming the first one that fails."""
    if get_setting(conn, "live_enabled", False) is not True:
        raise Conflict("live trading is disabled (live_enabled is false)")
    if model["status"] != "live_eligible":
        raise Conflict("the model's lineage is not live_eligible")
    state = conn.execute("SELECT auth_ok FROM exchange_state WHERE id").fetchone()
    if state is None or not state["auth_ok"]:
        raise Conflict("the exchange has not confirmed its credentials (auth_ok is false)")


def _insert_trade_job(conn: psycopg.Connection, assignment_id: Any, key: str | None) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO jobs (kind, role, params, idempotency_key, max_expiries)
        VALUES ('trade', 'trade', %s, %s, NULL) RETURNING *
        """,
        (Jsonb({"assignment_id": str(assignment_id)}), key),
    ).fetchone()
    add_job_event(conn, row["id"], "created", None, {"kind": "trade", "assignment_id": str(assignment_id)})
    conn.execute("UPDATE assignments SET job_id = %s, updated_at = now() WHERE id = %s", (row["id"], assignment_id))
    return dict(row)


def create_assignment(
    conn: psycopg.Connection,
    game_id: str,
    model_id: Any,
    mode: str,
    bankroll_cents: int,
    actor: str | None,
    max_bet_cents: int | None = None,
) -> dict[str, Any]:
    """Create an assignment, fund its bankroll and queue its trade job (one transaction).

    Returns the row plus "bankroll" and "job_id". 400 on bad input or a retired
    lineage, 409 when a limit or a live precondition refuses it.
    """
    if mode not in MODES:
        raise BadRequest(f"mode must be paper or live, not {mode!r}")
    if not _is_int(bankroll_cents) or bankroll_cents < 0 or bankroll_cents > MAX_CENTS:
        raise BadRequest("bankroll must be a whole number of cents between 0 and the fleet maximum")
    if max_bet_cents is not None and (not _is_int(max_bet_cents) or max_bet_cents < 0 or max_bet_cents > MAX_CENTS):
        raise BadRequest("max bet must be a whole number of cents")
    game = conn.execute("SELECT * FROM games WHERE game_id = %s FOR UPDATE", (str(game_id),)).fetchone()
    if game is None:
        raise BadRequest(f"unknown game {game_id!r}")
    if game["status"] == "final":
        raise Conflict(f"game {game_id} is final")
    model = _model(conn, model_id)
    if model["status"] == "retired" or lineage_retired(conn, model["lineage_id"]):
        raise BadRequest("the model's lineage is retired")
    if mode == "live":
        live_gate(conn, model)
    else:
        cap = get_int_setting(conn, "max_paper_models_per_game", 3)
        n = conn.execute(
            "SELECT count(*) AS n FROM assignments WHERE game_id = %s AND mode = 'paper' AND status IN ('active', 'halted')",
            (game["game_id"],),
        ).fetchone()["n"]
        if int(n) >= cap:
            raise Conflict(f"game {game_id} already has {n} paper assignments (max_paper_models_per_game is {cap})")
    try:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO assignments (game_id, model_id, lineage_id, mode, max_bet_cents, created_by)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
                """,
                (game["game_id"], model["id"], model["lineage_id"], mode, max_bet_cents, actor),
            ).fetchone()
    except UniqueViolation as exc:
        if "one_live" in str(exc):
            raise Conflict(f"game {game_id} already has a live assignment") from None
        raise Conflict(f"model {model['id']} already has a paper assignment on game {game_id}") from None
    job = _insert_trade_job(conn, row["id"], f"assignment:{row['id']}")
    bank = ledger.create_bankroll(conn, row["id"], mode, bankroll_cents)
    add_audit(
        conn, "assignment_created", str(row["id"]), actor, None,
        {"game_id": game["game_id"], "model_id": str(model["id"]), "mode": mode, "bankroll_cents": bankroll_cents,
         "max_bet_cents": max_bet_cents, "job_id": str(job["id"])},
    )
    out = get_assignment(conn, row["id"])
    out["bankroll"] = bank
    out["job_id"] = str(job["id"])
    return out


def _set_status(conn: psycopg.Connection, assignment_id: Any, status: str) -> dict[str, Any]:
    row = conn.execute(
        "UPDATE assignments SET status = %s, updated_at = now() WHERE id = %s RETURNING *", (status, assignment_id)
    ).fetchone()
    return dict(row)


def halt_assignment(conn: psycopg.Connection, assignment_id: Any, actor: str | None, reason: str) -> dict[str, Any]:
    """active -> halted, cancelling its open orders (paper at once, live cancel_requested).
    Already halted: idempotent. Settled or cancelled: 409.

    Runs under the mode's approval lock (the one approve_order and the kill take),
    so an approval in flight waits for the halt and then sees the halted row instead
    of committing a new order onto an assignment that was just halted."""
    approval_lock(conn, get_assignment(conn, assignment_id)["mode"])
    row = get_assignment(conn, assignment_id, for_update=True)
    if row["status"] == "halted":
        return row
    if row["status"] != "active":
        raise Conflict(f"assignment is {row['status']}")
    result = cancel_active_orders(conn, actor, reason, assignment_ids=[row["id"]])
    after = _set_status(conn, row["id"], "halted")
    add_audit(
        conn, "assignment_halted", str(row["id"]), actor, {"status": "active"},
        {"status": "halted", "reason": reason, "orders_cancelled": result["cancelled"],
         "orders_cancel_requested": len(result["requested"])},
    )
    return after


def _job_is_live(conn: psycopg.Connection, job_id: Any) -> bool:
    if job_id is None:
        return False
    row = conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()
    return row is not None and row["status"] in ("queued", "leased", "cancel_requested")


def _activate(conn: psycopg.Connection, row: dict[str, Any], actor: str | None) -> dict[str, Any]:
    """Shared body of activate_assignment and activate_all_paper (row is locked)."""
    if row["mode"] == "live":
        live_gate(conn, _model(conn, row["model_id"]))
    if not _job_is_live(conn, row["job_id"]):
        _insert_trade_job(conn, row["id"], None)
    after = _set_status(conn, row["id"], "active")
    add_audit(conn, "assignment_activated", str(row["id"]), actor, {"status": "halted"}, {"status": "active"})
    return after


def killed_locked(conn: psycopg.Connection) -> bool:
    """The kill flag read FOR SHARE: a kill that is committing (it holds the row FOR
    UPDATE) is waited for, so an activation can never slip in between the kill's
    flag write and its halts and re-activate what the kill just halted. Read before
    any assignment row is locked (the kill locks assignments last)."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'kill_switch' FOR SHARE").fetchone()
    return row is not None and row["value"] is True


def activate_assignment(conn: psycopg.Connection, assignment_id: Any, actor: str | None) -> dict[str, Any]:
    """halted -> active (refused under kill, for a final game, or when a live
    precondition fails). Already active: idempotent. Settled or cancelled: 409."""
    killed = killed_locked(conn)
    row = get_assignment(conn, assignment_id, for_update=True)
    if row["status"] == "active":
        return row
    if row["status"] != "halted":
        raise Conflict(f"assignment is {row['status']}")
    if killed:
        raise Conflict("the kill switch is on; reset it first")
    game = conn.execute("SELECT status FROM games WHERE game_id = %s", (row["game_id"],)).fetchone()
    if game is not None and game["status"] == "final":
        raise Conflict("the game is final; settle the assignment instead")
    return _activate(conn, row, actor)


def activate_all_paper(conn: psycopg.Connection, actor: str | None) -> int:
    """Re-activate every halted paper assignment whose game is not final (after a kill
    reset). Refused under kill. Returns how many were activated."""
    if killed_locked(conn):
        raise Conflict("the kill switch is on; reset it first")
    rows = conn.execute(
        """
        SELECT a.* FROM assignments a JOIN games g ON g.game_id = a.game_id
         WHERE a.mode = 'paper' AND a.status = 'halted' AND g.status <> 'final'
         ORDER BY a.created_at FOR UPDATE OF a
        """
    ).fetchall()
    for row in rows:
        _activate(conn, dict(row), actor)
    add_audit(conn, "activate_all_paper", "assignments", actor, None, {"activated": len(rows)})
    return len(rows)


def list_assignments(
    conn: psycopg.Connection, status: str | None = None, assignment_id: Any = None
) -> list[dict[str, Any]]:
    """Assignments joined with bankroll, game, model summary and the open order count
    (one status, or one assignment by id)."""
    rows = conn.execute(
        f"""
        SELECT a.*, b.id AS bankroll_id, b.initial_cents, b.available_cents, b.reserved_cents,
               b.open_cost_cents, b.realized_pnl_cents,
               g.season, g.week, g.home_team, g.away_team, g.kickoff_at, g.status AS game_status,
               g.home_score, g.away_score,
               m.family, m.params, m.status AS model_status, m.summary,
               j.status AS job_status, j.lease_worker_id,
               (SELECT count(*) FROM orders o WHERE o.assignment_id = a.id AND o.status IN ('{ACTIVE_LIST}')) AS open_orders
          FROM assignments a
          LEFT JOIN bankrolls b ON b.assignment_id = a.id
          JOIN games g ON g.game_id = a.game_id
          JOIN models m ON m.id = a.model_id
          LEFT JOIN jobs j ON j.id = a.job_id
         WHERE (%(status)s::text IS NULL OR a.status = %(status)s)
           AND (%(id)s::uuid IS NULL OR a.id = %(id)s)
         ORDER BY g.kickoff_at NULLS LAST, a.created_at
        """,
        {"status": status, "id": None if assignment_id is None else str(assignment_id)},
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"], "game_id": r["game_id"], "model_id": r["model_id"], "lineage_id": r["lineage_id"],
                "mode": r["mode"], "status": r["status"], "job_id": r["job_id"], "max_bet_cents": r["max_bet_cents"],
                "created_by": r["created_by"], "created_at": r["created_at"], "settled_at": r["settled_at"],
                "bankroll": {
                    "id": r["bankroll_id"], "initial_cents": r["initial_cents"], "available_cents": r["available_cents"],
                    "reserved_cents": r["reserved_cents"], "open_cost_cents": r["open_cost_cents"],
                    "realized_pnl_cents": r["realized_pnl_cents"],
                },
                "game": {
                    "game_id": r["game_id"], "season": r["season"], "week": r["week"], "home_team": r["home_team"],
                    "away_team": r["away_team"], "kickoff_at": r["kickoff_at"], "status": r["game_status"],
                    "home_score": r["home_score"], "away_score": r["away_score"],
                },
                "model": {"id": r["model_id"], "family": r["family"], "params": r["params"], "status": r["model_status"],
                          "summary": r["summary"]},
                "job": {"id": r["job_id"], "status": r["job_status"], "lease_worker_id": r["lease_worker_id"]},
                "open_orders": int(r["open_orders"]),
            }
        )
    return out
