"""Bodies of the stock JSON routes (host/api/stocks.py): the worker's trade state, the
daily bar feed with its ETag, the release handshake and the owner's summary.

A decision is due (contract section 1) when the broker is fresh and says the market is
open, now >= next_close - stock_decision_lead_min and before the 15:49 cutoff (approval
refuses later orders with moc_cutoff), the assignment has not decided this session, and
every symbol's bars reach the previous session (host.stocks.market).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from host.errors import BadRequest
from host.kill import approval_locks, is_killed
from host.leases import as_uuid, release
from host.settings import get_int_setting, get_setting
from host.stocks import market, orders
from host.stocks.approve import held_job
from host.stocks.models import model_id_of

MAX_RELEASE = 64


def _decision(conn: psycopg.Connection, a: dict[str, Any], broker: dict[str, Any], now: datetime) -> dict[str, Any]:
    session = broker.get("session_date")
    symbols = list(a["symbols"] or [])
    prices = market.ref_prices(conn, symbols, session) if session else {}
    fresh, through = market.bars_reach_previous_session(conn, symbols, prices, session) if session else (False, None)
    lead = timedelta(minutes=get_int_setting(conn, "stock_decision_lead_min", 20))
    close = broker.get("next_close")
    timely = (close is not None and broker.get("market_open") is True
              and close - lead <= now <= close - market.MOC_CUTOFF)
    due = (fresh and timely and a["last_decision_date"] != session
           and not market.broker_stale(broker, now, market.max_broker_age_s(conn)))
    return {"due": bool(due), "session_date": session, "bars_through": through,
            "ref_prices_cents": {s: p["cents"] for s, p in prices.items()}}


def trade_state(conn: psycopg.Connection, worker: dict[str, Any], job_id: Any, lease_token: Any = None) -> dict[str, Any]:
    """GET /api/v1/stock_trade/state?job_id=...: everything one tick of one held job needs."""
    job = held_job(conn, worker["id"], job_id, lease_token)
    params = job["params"] if isinstance(job["params"], dict) else {}
    aid = model_id_of(params.get("assignment_id"))
    row = conn.execute("SELECT * FROM stock_assignments WHERE id = %s", (aid,)).fetchone() if aid else None
    if row is None:
        raise BadRequest("the job names no stock assignment")
    a = dict(row)
    model = conn.execute("SELECT id, family, params FROM stock_models WHERE id = %s", (a["model_id"],)).fetchone()
    broker = market.broker_state(conn)
    now = market.server_now(conn)
    return {
        "kill": is_killed(conn),
        "server_time": now,
        "assignment": {k: a[k] for k in ("id", "mode", "status", "symbols", "cash_cents", "reserved_cents",
                                         "last_decision_date")},
        "model": dict(model) if model else None,
        "positions": orders.positions(conn, a["id"]),
        "open_orders": orders.open_orders(conn, a["id"]),
        "broker": {k: broker.get(k) for k in ("environment", "market_open", "session_date", "next_close", "checked_at")},
        "decision": _decision(conn, a, broker, now),
        "settings": {"stock_trade_tick_s": get_int_setting(conn, "stock_trade_tick_s", 30),
                     "stock_decision_lead_min": get_int_setting(conn, "stock_decision_lead_min", 20)},
    }


def bars_etag(conn: psycopg.Connection) -> str:
    """Changes whenever a symbol is fetched again or its bar count changes (old bars move
    when a split or a dividend lands, and every refresh re-fetches the whole history)."""
    rows = conn.execute(
        "SELECT symbol, fetched_at, bars_count, bars_through FROM instruments ORDER BY symbol"
    ).fetchall()
    count = conn.execute("SELECT count(*) AS n FROM stock_bars WHERE timeframe = '1Day'").fetchone()["n"]
    text = json.dumps([[r["symbol"], str(r["fetched_at"]), r["bars_count"], str(r["bars_through"])] for r in rows] + [count])
    return "sb-" + hashlib.sha256(text.encode()).hexdigest()[:24]


def dumps_bars(conn: psycopg.Connection) -> bytes:
    """{"generated_at", "symbols": {sym: [[date, o, h, l, c, v], ...]}}, oldest first, New York dates."""
    symbols: dict[str, list[list[Any]]] = {}
    cur = conn.execute(
        """
        SELECT symbol, to_char((ts AT TIME ZONE 'America/New_York')::date, 'YYYY-MM-DD') AS day,
               open, high, low, close, volume
          FROM stock_bars WHERE timeframe = '1Day' ORDER BY symbol, ts
        """
    )
    for r in cur:
        symbols.setdefault(r["symbol"], []).append([r["day"], r["open"], r["high"], r["low"], r["close"], r["volume"]])
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return json.dumps({"generated_at": generated, "symbols": symbols}, separators=(",", ":")).encode()


def release_jobs(conn: psycopg.Connection, worker: dict[str, Any], job_ids: Any) -> dict[str, Any]:
    """POST /api/v1/stock_trade/release: cancel the approved (never submitted) orders of
    the listed jobs' assignments and hand those jobs back (reason drain), as the NFL
    release does. Jobs this worker does not hold are ignored, so a repeat is a no-op.
    Orders already at Alpaca stay: a market-on-close order fills at the close the
    decision assumed."""
    if not isinstance(job_ids, list) or len(job_ids) > MAX_RELEASE:
        raise BadRequest(f"job_ids must be a list of at most {MAX_RELEASE} ids")
    approval_locks(conn)  # an approval that read these jobs as leased commits before they go back
    held = []
    for value in job_ids:
        jid = as_uuid(value) if isinstance(value, str) else None
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = %s AND kind = 'stock_trade' AND lease_worker_id = %s"
            " AND status IN ('leased', 'cancel_requested') FOR UPDATE",
            (jid, worker["id"]),
        ).fetchone() if jid else None
        if job is not None:
            held.append(dict(job))
    ids = [model_id_of((j["params"] or {}).get("assignment_id")) for j in held if isinstance(j["params"], dict)]
    ids = [i for i in ids if i is not None]
    result = orders.cancel_orders(conn, f"worker:{worker['id']}", "drain", assignment_ids=ids, approved_only=True)
    released = [str(j["id"]) for j in held
                if release(conn, j["id"], j["lease_token"], None, None, worker["id"], "drain") is not None]
    return {"cancelled": len(result["cancelled"]), "released": released}


def summary(conn: psycopg.Connection) -> dict[str, Any]:
    """GET /api/stocks/summary: broker, models by status, assignments, today's orders."""
    from host.stocks.assignments import list_assignments

    broker = market.broker_state(conn)
    counts = {r["status"]: int(r["n"]) for r in conn.execute(
        "SELECT status, count(*) AS n FROM stock_models GROUP BY status").fetchall()}
    session = broker.get("session_date")
    todays = conn.execute(
        "SELECT status, count(*) AS n FROM stock_orders WHERE session_date = %s GROUP BY status", (session,)
    ).fetchall() if session else []
    return {
        "kill": is_killed(conn), "live_enabled": get_setting(conn, "live_enabled", False) is True,
        "broker": broker, "models": counts, "assignments": list_assignments(conn),
        "orders_today": {r["status"]: int(r["n"]) for r in todays},
        "open_orders": int(conn.execute(
            f"SELECT count(*) AS n FROM stock_orders WHERE status IN ('{orders.ACTIVE_LIST}')").fetchone()["n"]),
    }
