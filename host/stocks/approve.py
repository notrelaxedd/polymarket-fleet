"""Stock order approval (contract section 5): every limit, in order, first failing code wins.

kill | halted | environment | broker_stale | live_disabled | not_live_eligible |
market_closed | moc_cutoff | session | symbol | short | max_order | max_position | cash |
daily_loss

`request_orders` handles POST /api/v1/stock_orders/request: under the mode's approval
lock and SELECT ... FOR UPDATE of the assignment, each proposal is decided by
`approve` and stored as a stock_orders row `approved` (a buy reserves
ceil(qty * ref * (1 + stock_price_band)), moved from the assignment's cash to its
reserved) or `rejected` with the code, plus a stock_order_events row. It is idempotent
per (assignment_id, client_request_id) and marks last_decision_date = session_date even
when every order is rejected (one decision per session).

The reference price is the host's: the close of the newest bar before the session
(host.stocks.market.ref_prices). The worker's ref_price_cents is kept in the event
detail only, so a wrong worker price can never loosen a limit. daily_loss is judged
over the whole mode (the limit is per mode): the sum over its active and halted
assignments of the previous mark's equity (the bankroll before the first mark) minus
cash + reserved + positions at reference prices.
"""
from __future__ import annotations

import math
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

import psycopg

from host import kill
from host.errors import BadRequest, Conflict, NotFound
from host.leases import as_uuid
from host.settings import get_settings
from host.stocks import market, orders
from host.stocks.models import model_id_of

REASONS = ("kill", "halted", "environment", "broker_stale", "live_disabled", "not_live_eligible", "market_closed",
           "moc_cutoff", "session", "symbol", "short", "max_order", "max_position", "cash", "daily_loss")
SIDES = ("buy", "sell")
MAX_ORDERS = 200
MAX_QTY = 10_000_000


def reservation_cents(qty: int, ref_cents: int, band: Any) -> int:
    """ceil(qty * ref * (1 + band)) in exact decimal arithmetic."""
    return int(math.ceil(Decimal(int(qty) * int(ref_cents)) * (Decimal(1) + Decimal(str(band or 0)))))


def _open_qty(conn: psycopg.Connection, assignment_id: int, symbol: str, side: str) -> int:
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(qty - filled_qty), 0) AS q FROM stock_orders
         WHERE assignment_id = %s AND symbol = %s AND side = %s AND status IN ('{orders.ACTIVE_LIST}')
        """,
        (assignment_id, symbol, side),
    ).fetchone()
    return int(row["q"])


def _held(conn: psycopg.Connection, assignment_id: int, symbol: str) -> int:
    row = conn.execute(
        "SELECT qty FROM stock_positions WHERE assignment_id = %s AND symbol = %s", (assignment_id, symbol)
    ).fetchone()
    return int(row["qty"]) if row else 0


def mode_loss_cents(conn: psycopg.Connection, mode: str, session_date: date) -> int:
    """Today's loss of the mode: previous equity minus equity at reference prices."""
    rows = conn.execute(
        """
        SELECT a.id, a.bankroll_cents, a.cash_cents, a.reserved_cents,
               (SELECT m.equity_cents FROM stock_marks m WHERE m.assignment_id = a.id AND m.session_date < %s
                 ORDER BY m.session_date DESC LIMIT 1) AS prev
          FROM stock_assignments a WHERE a.mode = %s AND a.status IN ('active', 'halted')
        """,
        (session_date, mode),
    ).fetchall()
    loss = 0
    for r in rows:
        held = conn.execute("SELECT symbol, qty FROM stock_positions WHERE assignment_id = %s AND qty > 0", (r["id"],)).fetchall()
        prices = market.ref_prices(conn, [h["symbol"] for h in held], session_date)
        value = sum(int(h["qty"]) * prices.get(h["symbol"], {}).get("cents", 0) for h in held)
        now_equity = int(r["cash_cents"]) + int(r["reserved_cents"]) + value
        before = int(r["prev"]) if r["prev"] is not None else int(r["bankroll_cents"])
        loss += before - now_equity
    return loss


def _ctx(conn: psycopg.Connection, a: dict[str, Any], o: dict[str, Any], now: datetime) -> dict[str, Any]:
    settings = get_settings(conn)
    model = conn.execute("SELECT status FROM stock_models WHERE id = %s", (a["model_id"],)).fetchone()
    instrument = conn.execute("SELECT tradable FROM instruments WHERE symbol = %s", (o["symbol"],)).fetchone()
    return {"a": a, "o": o, "now": now, "settings": settings, "broker": market.broker_state(conn),
            "model_status": model["status"] if model else None, "tradable": bool(instrument and instrument["tradable"]),
            "max_age": market.max_broker_age_s(conn), "value": int(o["qty"]) * int(o["ref_price_cents"])}


def _int(c: dict[str, Any], key: str) -> int:
    return int(c["settings"].get(key) or 0)


Check = Callable[[psycopg.Connection, dict[str, Any]], bool]
CHECKS: tuple[tuple[str, Check], ...] = (
    ("kill", lambda conn, c: kill.is_killed(conn)),
    ("halted", lambda conn, c: c["a"]["status"] != "active"),
    ("environment", lambda conn, c: c["a"]["mode"] != c["broker"]["environment"] or not c["broker"]["keys_present"]),
    ("broker_stale", lambda conn, c: market.broker_stale(c["broker"], c["now"], c["max_age"])),
    ("live_disabled", lambda conn, c: c["a"]["mode"] == "live" and c["settings"].get("live_enabled") is not True),
    ("not_live_eligible", lambda conn, c: c["a"]["mode"] == "live" and c["model_status"] != "live_eligible"),
    ("market_closed", lambda conn, c: c["broker"]["market_open"] is not True),
    ("moc_cutoff", lambda conn, c: c["broker"]["next_close"] is None
     or c["now"] > c["broker"]["next_close"] - market.MOC_CUTOFF),
    ("session", lambda conn, c: c["o"]["session_date"] != c["broker"]["session_date"]),
    ("symbol", lambda conn, c: c["o"]["symbol"] not in (c["a"]["symbols"] or []) or not c["tradable"]
     or not c["o"].get("ref_ok", True)),
    ("short", lambda conn, c: c["o"]["side"] == "sell" and c["o"]["qty"] > _held(conn, c["a"]["id"], c["o"]["symbol"])
     - _open_qty(conn, c["a"]["id"], c["o"]["symbol"], "sell")),
    ("max_order", lambda conn, c: c["value"] > _int(c, "stock_max_order_cents")),
    ("max_position", lambda conn, c: c["o"]["side"] == "buy" and (
        _held(conn, c["a"]["id"], c["o"]["symbol"]) + _open_qty(conn, c["a"]["id"], c["o"]["symbol"], "buy")
        + c["o"]["qty"]) * c["o"]["ref_price_cents"] > _int(c, "stock_max_position_cents")),
    ("cash", lambda conn, c: c["o"]["side"] == "buy" and reservation_cents(
        c["o"]["qty"], c["o"]["ref_price_cents"], c["settings"].get("stock_price_band")) > int(c["a"]["cash_cents"])),
    ("daily_loss", lambda conn, c: c["o"]["side"] == "buy" and mode_loss_cents(conn, c["a"]["mode"], c["o"]["session_date"])
     > int((c["settings"].get("stock_max_daily_loss_cents") or {}).get(c["a"]["mode"], 0) or 0)),
)


def approve(conn: psycopg.Connection, assignment: dict[str, Any], order: dict[str, Any], now: datetime) -> tuple[str, str | None]:
    """("approved", None) or ("rejected", code) for one order {symbol, side, qty,
    ref_price_cents, session_date} on a locked assignment row."""
    ctx = _ctx(conn, assignment, order, now)
    reason = next((name for name, check in CHECKS if check(conn, ctx)), None)
    return ("rejected", reason) if reason else ("approved", None)


def _parse_order(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BadRequest("every order must be an object")
    crid, symbol, side, qty, ref = (raw.get(k) for k in ("client_request_id", "symbol", "side", "qty", "ref_price_cents"))
    if not isinstance(crid, str) or not 1 <= len(crid) <= 64:
        raise BadRequest("client_request_id must be a string of 1 to 64 characters")
    if not isinstance(symbol, str) or not 1 <= len(symbol) <= 10:
        raise BadRequest("symbol must be a ticker symbol")
    if side not in SIDES:
        raise BadRequest("side must be buy or sell")
    for name, value in (("qty", qty), ("ref_price_cents", ref)):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_QTY * 100:
            raise BadRequest(f"{name} must be a positive whole number")
    if qty > MAX_QTY:
        raise BadRequest(f"qty must be at most {MAX_QTY}")
    rationale = raw.get("rationale")
    return {"client_request_id": crid, "symbol": symbol, "side": side, "qty": qty, "worker_ref_price_cents": ref,
            "rationale": str(rationale)[:512] if rationale is not None else None}


def held_job(conn: psycopg.Connection, worker_id: str, job_id: Any, lease_token: Any = None) -> dict[str, Any]:
    """The stock_trade job this worker holds (with this lease token when one is sent);
    404/409 otherwise."""
    jid = as_uuid(job_id) if isinstance(job_id, str) else None
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (jid,)).fetchone() if jid else None
    if job is None or job["kind"] != "stock_trade":
        raise NotFound("stock_trade job not found")
    if job["lease_worker_id"] != worker_id or job["status"] not in ("leased", "cancel_requested"):
        raise Conflict("the stock_trade job is not leased by this worker")
    if lease_token not in (None, "") and as_uuid(str(lease_token)) != job["lease_token"]:
        raise Conflict("lease token mismatch")
    return dict(job)


def _stored(conn: psycopg.Connection, assignment_id: int, crid: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, status, reason FROM stock_orders WHERE assignment_id = %s AND client_request_id = %s",
        (assignment_id, crid),
    ).fetchone()
    if row is None:
        return None
    rejected = row["status"] == "rejected"
    return {"client_request_id": crid, "order_id": str(row["id"]), "status": "rejected" if rejected else "approved",
            "reason": row["reason"] if rejected else None, "duplicate": True}


def _store(conn: psycopg.Connection, a: dict[str, Any], o: dict[str, Any], status: str, reason: str | None,
           worker_id: str, band: Any) -> dict[str, Any]:
    reserved = reservation_cents(o["qty"], o["ref_price_cents"], band) if status == "approved" and o["side"] == "buy" else 0
    oid = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO stock_orders (id, assignment_id, mode, session_date, client_request_id, symbol, side, qty,
                                  ref_price_cents, reserved_cents, status, reason, rationale)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (oid, a["id"], a["mode"], o["session_date"], o["client_request_id"], o["symbol"], o["side"], o["qty"],
         o["ref_price_cents"], reserved, status, reason, o["rationale"]),
    )
    if reserved:
        conn.execute(
            "UPDATE stock_assignments SET cash_cents = cash_cents - %s, reserved_cents = reserved_cents + %s,"
            " updated_at = now() WHERE id = %s",
            (reserved, reserved, a["id"]),
        )
        a["cash_cents"] = int(a["cash_cents"]) - reserved
        a["reserved_cents"] = int(a["reserved_cents"]) + reserved
    detail = {"reason": reason} if reason else {"reserved_cents": reserved}
    detail.update(worker=worker_id, worker_ref_price_cents=o["worker_ref_price_cents"])
    orders.add_event(conn, oid, None, status, "host", detail)
    return {"client_request_id": o["client_request_id"], "order_id": str(oid), "status": status, "reason": reason}


def request_orders(conn: psycopg.Connection, worker: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """POST /api/v1/stock_orders/request: {"orders": [{client_request_id, order_id, status, reason}]}."""
    job = held_job(conn, worker["id"], body.get("job_id"), body.get("lease_token"))
    params = job["params"] if isinstance(job["params"], dict) else {}
    if str(params.get("assignment_id")) != str(body.get("assignment_id")):
        raise Conflict("the job does not trade this assignment")
    try:
        session = date.fromisoformat(str(body.get("session_date")))
    except ValueError:
        raise BadRequest("session_date must be a date such as 2026-10-08") from None
    raw = body.get("orders")
    if not isinstance(raw, list) or len(raw) > MAX_ORDERS:
        raise BadRequest(f"orders must be a list of at most {MAX_ORDERS}")
    parsed = [_parse_order(r) for r in raw]
    aid = model_id_of(params.get("assignment_id"))
    first = conn.execute("SELECT mode FROM stock_assignments WHERE id = %s", (aid,)).fetchone() if aid else None
    if first is None:
        raise NotFound("stock assignment not found")
    kill.approval_lock(conn, first["mode"])
    job = held_job(conn, worker["id"], body.get("job_id"), body.get("lease_token"))  # a release that committed meanwhile
    a = dict(conn.execute("SELECT * FROM stock_assignments WHERE id = %s FOR UPDATE", (aid,)).fetchone())
    if a["job_id"] != job["id"]:
        raise Conflict("the job is no longer this assignment's stock_trade job")
    now = market.server_now(conn)
    band = get_settings(conn).get("stock_price_band")
    prices = market.ref_prices(conn, sorted({o["symbol"] for o in parsed}), session)
    out = []
    for o in parsed:
        stored = _stored(conn, a["id"], o["client_request_id"])
        if stored is not None:
            out.append(stored)
            continue
        host_ref = prices.get(o["symbol"], {}).get("cents")
        o.update(session_date=session, ref_ok=host_ref is not None,
                 ref_price_cents=host_ref if host_ref is not None else o["worker_ref_price_cents"])
        status, reason = approve(conn, a, o, now)
        out.append(_store(conn, a, o, status, reason, worker["id"], band))
    conn.execute("UPDATE stock_assignments SET last_decision_date = %s, updated_at = now() WHERE id = %s", (session, a["id"]))
    return {"orders": out}
