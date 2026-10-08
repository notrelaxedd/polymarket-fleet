"""Stock assignments (contract section 5): one model, one mode, a set of symbols, a
bankroll of its own and one stock_trade job.

Create (and resume) gates, each refusal naming why: the model is paper_ok or
live_eligible (live: live_eligible); the mode equals the broker's environment, the
exchange process has keys and checked the broker within 4 * stock_broker_poll_s; live
needs settings.live_enabled; the symbols are a non-empty subset of the tradable
instruments with bars; at most stock_max_assignments active or halted; the bankroll
fits in the broker's free cash (one Alpaca account is shared). Every change runs under
the mode's approval lock (the one the approval, the kill and live off take) and writes
an audit row.

Free cash (`_room`) is the broker's cash_cents minus what the other active or halted
assignments of the mode still claim, their cash + reserved: a halted assignment keeps
its money, and an assignment that grew claims its gains (shares bought are already out
of the broker's cash). Resume repeats this gate with the assignment's own cash +
reserved, so a halt, a new assignment and a resume can never claim more than the
account holds (Alpaca would fill the excess on margin).
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from host.errors import BadRequest, Conflict, NotFound
from host.events import add_audit, add_job_event
from host.kill import approval_lock
from host.money import MAX_CENTS
from host.settings import get_int_setting, get_setting
from host.stocks import market, orders
from host.stocks.models import get_model, model_id_of, require_tradable_status

MODES = ("paper", "live")
MAX_SYMBOLS = 200


def get_assignment(conn: psycopg.Connection, assignment_id: Any, for_update: bool = False) -> dict[str, Any]:
    """One stock_assignments row; 404 when missing."""
    aid = model_id_of(assignment_id)
    if aid is None:
        raise NotFound("stock assignment not found")
    sql = "SELECT * FROM stock_assignments WHERE id = %s" + (" FOR UPDATE" if for_update else "")
    row = conn.execute(sql, (aid,)).fetchone()
    if row is None:
        raise NotFound("stock assignment not found")
    return dict(row)


def check_symbols(conn: psycopg.Connection, symbols: Any) -> list[str]:
    """A non-empty list of distinct tradable symbols with bars; 400 naming the others."""
    if not isinstance(symbols, list) or not symbols or len(symbols) > MAX_SYMBOLS:
        raise BadRequest(f"symbols must be a list of 1 to {MAX_SYMBOLS} ticker symbols")
    if any(not isinstance(s, str) for s in symbols) or len(set(symbols)) != len(symbols):
        raise BadRequest("symbols must be distinct ticker symbols")
    ok = market.tradable_symbols(conn, symbols)
    bad = [s for s in symbols if s not in ok]
    if bad:
        raise BadRequest(f"not tradable or without daily bars: {', '.join(bad)}")
    return list(symbols)


def _gates(conn: psycopg.Connection, model: dict[str, Any], mode: str) -> dict[str, Any]:
    """The model, broker and live gates shared by create and resume; the broker row."""
    require_tradable_status(model, mode)
    broker = market.broker_state(conn)
    problem = market.broker_problem(conn, broker, mode, market.server_now(conn))
    if problem:
        raise BadRequest(f"cannot trade {mode}: {problem}")
    if mode == "live" and get_setting(conn, "live_enabled", False) is not True:
        raise BadRequest("live trading is off (turn it on with the typed live switch first)")
    return broker


def _room(conn: psycopg.Connection, broker: dict[str, Any], mode: str, exclude_id: int | None = None) -> int:
    """The broker's cash minus the claim (cash + reserved) of the other active or halted
    assignments of `mode`."""
    claimed = conn.execute(
        "SELECT COALESCE(SUM(cash_cents + reserved_cents), 0) AS s FROM stock_assignments"
        " WHERE mode = %s AND status IN ('active', 'halted') AND (%s::bigint IS NULL OR id <> %s)",
        (mode, exclude_id, exclude_id),
    ).fetchone()["s"]
    return int(broker.get("cash_cents") or 0) - int(claimed)


def _insert_job(conn: psycopg.Connection, assignment_id: int, key: str | None) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO jobs (kind, role, params, idempotency_key, max_expiries)
        VALUES ('stock_trade', 'trade', %s, %s, NULL) RETURNING *
        """,
        (Jsonb({"assignment_id": assignment_id}), key),
    ).fetchone()
    add_job_event(conn, row["id"], "created", None, {"kind": "stock_trade", "assignment_id": assignment_id})
    conn.execute("UPDATE stock_assignments SET job_id = %s, updated_at = now() WHERE id = %s", (row["id"], assignment_id))
    return dict(row)


def create_assignment(
    conn: psycopg.Connection, model_id: Any, mode: str, bankroll_cents: int, symbols: list[str], actor: str,
) -> dict[str, Any]:
    """Create a stock assignment with cash_cents = bankroll_cents and queue its
    stock_trade job; the row plus "job_id". 400/409 naming the gate that refused."""
    if mode not in MODES:
        raise BadRequest(f"mode must be paper or live, not {mode!r}")
    if isinstance(bankroll_cents, bool) or not isinstance(bankroll_cents, int) or not 0 < bankroll_cents <= MAX_CENTS:
        raise BadRequest("bankroll must be a positive whole number of cents")
    approval_lock(conn, mode)  # serialised with approvals, the kill, live off and other creates
    model = get_model(conn, model_id)
    broker = _gates(conn, model, mode)
    symbols = check_symbols(conn, symbols)
    cap = get_int_setting(conn, "stock_max_assignments", 3)
    n = conn.execute("SELECT count(*) AS n FROM stock_assignments WHERE status IN ('active', 'halted')").fetchone()["n"]
    if int(n) >= cap:
        raise Conflict(f"there are already {n} stock assignments (stock_max_assignments is {cap})")
    room = _room(conn, broker, mode)
    if bankroll_cents > room:
        raise Conflict(f"bankroll {bankroll_cents} cents is more than the broker cash left for {mode} ({max(room, 0)} cents)")
    try:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO stock_assignments (model_id, mode, symbols, bankroll_cents, cash_cents, created_by)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
                """,
                (model["id"], mode, symbols, bankroll_cents, bankroll_cents, actor),
            ).fetchone()
    except UniqueViolation:
        raise Conflict(f"stock model {model['id']} already has a live assignment") from None
    job = _insert_job(conn, int(row["id"]), f"stock_assignment:{row['id']}")
    add_audit(conn, "stock_assignment_created", f"stock_assignment:{row['id']}", actor, None,
              {"model_id": model["id"], "mode": mode, "symbols": symbols, "bankroll_cents": bankroll_cents,
               "job_id": str(job["id"])})
    out = get_assignment(conn, row["id"])
    out["job_id"] = str(job["id"])
    return out


def _set_status(conn: psycopg.Connection, assignment_id: int, status: str, reason: str | None) -> dict[str, Any]:
    row = conn.execute(
        "UPDATE stock_assignments SET status = %s, halt_reason = %s, updated_at = now() WHERE id = %s RETURNING *",
        (status, reason, assignment_id),
    ).fetchone()
    return dict(row)


def halt_assignment(conn: psycopg.Connection, assignment_id: Any, reason: str, actor: str) -> dict[str, Any]:
    """active -> halted: approved orders cancelled (reservation released), orders at
    Alpaca cancel_requested. Already halted: idempotent. Closed: 409."""
    approval_lock(conn, get_assignment(conn, assignment_id)["mode"])
    row = get_assignment(conn, assignment_id, for_update=True)
    if row["status"] == "halted":
        return row
    if row["status"] != "active":
        raise Conflict(f"stock assignment is {row['status']}")
    result = orders.cancel_orders(conn, actor, reason, assignment_ids=[row["id"]])
    after = _set_status(conn, row["id"], "halted", reason)
    add_audit(conn, "stock_assignment_halted", f"stock_assignment:{row['id']}", actor, {"status": "active"},
              {"status": "halted", "reason": reason, "orders_cancelled": len(result["cancelled"]),
               "orders_cancel_requested": len(result["requested"])})
    return after


def _job_alive(conn: psycopg.Connection, job_id: Any) -> bool:
    if job_id is None:
        return False
    row = conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()
    return row is not None and row["status"] in ("queued", "leased")


def _killed_locked(conn: psycopg.Connection) -> bool:
    """The kill flag read FOR SHARE: a kill that is committing is waited for."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'kill_switch' FOR SHARE").fetchone()
    return row is not None and row["value"] is True


def resume_assignment(conn: psycopg.Connection, assignment_id: Any, actor: str) -> dict[str, Any]:
    """halted -> active under the model, broker, live, symbol and cash gates of create
    (the count gate is not repeated: the assignment already holds its slot); the cash
    gate compares its cash + reserved with the broker cash the others leave free.
    Refused under kill. A stock_trade job that ended is replaced."""
    killed = _killed_locked(conn)
    approval_lock(conn, get_assignment(conn, assignment_id)["mode"])
    row = get_assignment(conn, assignment_id, for_update=True)
    if row["status"] == "active":
        return row
    if row["status"] != "halted":
        raise Conflict(f"stock assignment is {row['status']}")
    if killed:
        raise Conflict("the kill switch is on; reset it first")
    broker = _gates(conn, get_model(conn, row["model_id"]), row["mode"])
    check_symbols(conn, list(row["symbols"]))
    claim = int(row["cash_cents"]) + int(row["reserved_cents"])
    room = _room(conn, broker, row["mode"], exclude_id=int(row["id"]))
    if claim > room:
        raise Conflict(f"stock assignment {row['id']} needs {claim} cents but only {max(room, 0)} cents of"
                       f" {row['mode']} broker cash is free")
    if not _job_alive(conn, row["job_id"]):
        _insert_job(conn, int(row["id"]), None)
    after = _set_status(conn, row["id"], "active", None)
    add_audit(conn, "stock_assignment_resumed", f"stock_assignment:{row['id']}", actor, {"status": "halted"},
              {"status": "active"})
    return after


def close_assignment(conn: psycopg.Connection, assignment_id: Any, actor: str) -> dict[str, Any]:
    """active or halted -> closed, only with no positions and no active orders; its
    stock_trade job is cancelled. Already closed: idempotent."""
    from host.scheduling import cancel_job

    approval_lock(conn, get_assignment(conn, assignment_id)["mode"])
    row = get_assignment(conn, assignment_id, for_update=True)
    if row["status"] == "closed":
        return row
    held = orders.positions(conn, row["id"])
    if held:
        raise Conflict(f"stock assignment {row['id']} still holds {', '.join(sorted(held))}; sell them first")
    if orders.open_orders(conn, row["id"]):
        raise Conflict(f"stock assignment {row['id']} has active orders; halt it and wait for them to end")
    after = _set_status(conn, row["id"], "closed", row["halt_reason"])
    if _job_alive(conn, row["job_id"]):
        try:
            cancel_job(conn, row["job_id"], actor)
        except (Conflict, NotFound):
            pass
    add_audit(conn, "stock_assignment_closed", f"stock_assignment:{row['id']}", actor, {"status": row["status"]},
              {"status": "closed", "cash_cents": row["cash_cents"], "realized_cents": row["realized_cents"]})
    return after


def halt_mode(conn: psycopg.Connection, mode: str | None, reason: str, actor: str) -> list[int]:
    """Halt every active assignment of `mode` (every mode for None); the ids."""
    rows = conn.execute(
        "SELECT id FROM stock_assignments WHERE status = 'active' AND (%s::text IS NULL OR mode = %s) ORDER BY id",
        (mode, mode),
    ).fetchall()
    for r in rows:
        halt_assignment(conn, r["id"], reason, actor)
    return [int(r["id"]) for r in rows]


def list_assignments(conn: psycopg.Connection, status: str | None = None) -> list[dict[str, Any]]:
    """Assignments with the model's family and status, the open order count and the job status."""
    rows = conn.execute(
        f"""
        SELECT a.*, m.family, m.status AS model_status, j.status AS job_status, j.lease_worker_id,
               (SELECT count(*) FROM stock_orders o WHERE o.assignment_id = a.id
                   AND o.status IN ('{orders.ACTIVE_LIST}')) AS open_orders
          FROM stock_assignments a JOIN stock_models m ON m.id = a.model_id LEFT JOIN jobs j ON j.id = a.job_id
         WHERE (%(status)s::text IS NULL OR a.status = %(status)s) ORDER BY a.id
        """,
        {"status": status},
    ).fetchall()
    return [dict(r) for r in rows]
