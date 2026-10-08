"""Read-only queries behind the /stocks page and the Home "Needs attention" row
(contract section 8): the broker row in words, the bar feed per symbol, the stock
models with their headline metrics, the assignments with equity and P&L, the positions
and the latest session's orders.

Nothing here writes. Money stays in cents and fractions stay fractions; the templates
format them. A position is valued at the newest daily close (the price the marks and
the decisions use), so the equity shown between two marks is an estimate; the marks in
stock_marks stay the record the paper gate reads.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import psycopg

from host.settings import get_setting
from host.stocks.market import NEW_YORK, broker_problem, broker_state, max_broker_age_s, ref_prices, server_now

ACTIVE_ORDERS = ("approved", "submitting", "open", "partial", "cancel_requested")
MAX_ORDERS = 200
REASON_TEXT = {
    "kill": "kill switch on", "halted": "assignment not active", "environment": "keys of the other mode or none",
    "broker_stale": "broker check too old", "live_disabled": "live trading off",
    "not_live_eligible": "model not live_eligible", "market_closed": "no trading session",
    "moc_cutoff": "after the market-on-close cutoff", "session": "not the broker's session",
    "symbol": "symbol not tradable here", "short": "sell larger than the shares held", "max_order": "over max order",
    "max_position": "over max position", "cash": "not enough cash", "daily_loss": "daily loss limit",
}
# Colour means state (docs/UI.md principle 4); every chip also carries its word.
MODEL_TONE = {"candidate": "muted", "paper_ok": "ok", "live_eligible": "ok", "retired": "muted"}
ASSIGNMENT_TONE = {"active": "ok", "halted": "warn", "closed": "muted"}
ORDER_TONE = {
    "rejected": "bad", "rejected_by_exchange": "bad", "approved": "muted", "submitting": "muted", "open": "ok",
    "partial": "ok", "filled": "ok", "cancel_requested": "warn", "cancelled": "muted", "expired": "muted",
}
TONES = {"model": MODEL_TONE, "assignment": ASSIGNMENT_TONE, "order": ORDER_TONE}


def tone(kind: str, status: Any) -> str:
    """The chip state (ok | warn | bad | muted) of a model, assignment or order status."""
    return TONES.get(kind, {}).get(str(status), "muted")


def _number(metrics: Any, key: str) -> float | None:
    value = metrics.get(key) if isinstance(metrics, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


def short_params(params: Any) -> str:
    """{"lookback": 126, "top_k": 3} -> "lookback 126 top_k 3", in stored order."""
    if not isinstance(params, dict):
        return ""
    return " ".join(f"{k} {v}" for k, v in params.items())


def ny_date(value: Any) -> date | None:
    """A bar timestamp as its New York date (bars are stamped at midnight New York)."""
    return value.astimezone(NEW_YORK).date() if isinstance(value, datetime) else None


def broker(conn: psycopg.Connection) -> dict[str, Any]:
    """The broker row plus its age, whether it is stale, why the current environment
    could not trade now (None when it can) and the warnings as {kind, message, ts}."""
    row = broker_state(conn)
    now = server_now(conn)
    checked = row.get("checked_at")
    age = None if checked is None else max(0, int((now - checked).total_seconds()))
    warnings = row.get("warnings") if isinstance(row.get("warnings"), list) else []
    env = row.get("environment")
    return {
        **row,
        "age_s": age,
        "stale": age is None or age > max_broker_age_s(conn),
        "problem": broker_problem(conn, row, env, now) if env else (
            None if row.get("keys_present") else "the exchange process has no Alpaca keys (exchange.env)"),
        "warnings": [w if isinstance(w, dict) else {"kind": "note", "message": str(w)} for w in warnings],
    }


def feed(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """One entry per followed symbol (the stock_symbols setting) and per instrument row:
    bar count, newest bar date, last fetch and last error."""
    rows = {r["symbol"]: dict(r) for r in conn.execute(
        "SELECT symbol, name, tradable, bars_count, bars_through, fetched_at, last_error FROM instruments ORDER BY symbol"
    ).fetchall()}
    followed = get_setting(conn, "stock_symbols", [])
    followed = [s for s in followed if isinstance(s, str)] if isinstance(followed, list) else []
    out = []
    for symbol in list(dict.fromkeys(followed + sorted(rows))):
        r = rows.get(symbol) or {"symbol": symbol, "name": None, "tradable": None, "bars_count": 0, "bars_through": None,
                                  "fetched_at": None, "last_error": None}
        out.append({**r, "followed": symbol in followed, "newest": ny_date(r["bars_through"])})
    return out


def models(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every stock model, retired last, then best backtest Sharpe first, with the
    headline numbers pulled out of the metrics."""
    rows = conn.execute(
        """
        SELECT id, family, params, status, summary, backtest_metrics, validation_metrics, created_at
          FROM stock_models
         ORDER BY status = 'retired', (backtest_metrics ->> 'sharpe')::double precision DESC NULLS LAST, id
        """
    ).fetchall()
    out = []
    for r in rows:
        bt, val = r["backtest_metrics"], r["validation_metrics"]
        bench = bt.get("benchmark") if isinstance(bt, dict) else None
        out.append({**dict(r), "short_params": short_params(r["params"]), "sharpe": _number(bt, "sharpe"),
                    "max_drawdown": _number(bt, "max_drawdown"), "cagr": _number(bt, "cagr"),
                    "spy_cagr": _number(bench, "cagr"), "trades": _number(bt, "trades"),
                    "validated": isinstance(val, dict), "validation_sharpe": _number(val, "sharpe")})
    return out


def _closes(conn: psycopg.Connection, symbols: list[str]) -> dict[str, int]:
    """{symbol: newest daily close in cents}."""
    return {s: p["cents"] for s, p in ref_prices(conn, sorted(set(symbols)), None).items()}


def _position_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """
        SELECT p.assignment_id, p.symbol, p.qty, p.cost_cents, p.updated_at, a.mode, a.status AS assignment_status
          FROM stock_positions p JOIN stock_assignments a ON a.id = p.assignment_id
         WHERE p.qty > 0 ORDER BY p.assignment_id, p.symbol
        """
    ).fetchall()]


def positions(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Held positions with the newest close, the value at it and the unrealized P&L
    (value minus cost; None without a close)."""
    rows = _position_rows(conn)
    closes = _closes(conn, [r["symbol"] for r in rows])
    for r in rows:
        close = closes.get(r["symbol"])
        r["close_cents"] = close
        r["avg_cost_cents"] = round(r["cost_cents"] / r["qty"]) if r["qty"] else None
        r["value_cents"] = None if close is None else close * r["qty"]
        r["unrealized_cents"] = None if close is None else close * r["qty"] - r["cost_cents"]
    return rows


def assignments(conn: psycopg.Connection, held: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every assignment, closed last, with its model, open order count, equity (cash +
    reserved + positions at the newest close), today's P&L (equity minus the last mark
    before today in New York, or the bankroll without one) and total return."""
    held = positions(conn) if held is None else held
    today = server_now(conn).astimezone(NEW_YORK).date()
    rows = conn.execute(
        f"""
        SELECT a.*, m.family, m.params, m.status AS model_status, j.status AS job_status, j.lease_worker_id,
               (SELECT count(*) FROM stock_orders o WHERE o.assignment_id = a.id
                   AND o.status IN ('{"', '".join(ACTIVE_ORDERS)}')) AS open_orders,
               (SELECT k.equity_cents FROM stock_marks k WHERE k.assignment_id = a.id AND k.session_date < %s
                 ORDER BY k.session_date DESC LIMIT 1) AS base_cents
          FROM stock_assignments a JOIN stock_models m ON m.id = a.model_id LEFT JOIN jobs j ON j.id = a.job_id
         ORDER BY a.status = 'closed', a.id
        """,
        (today,),
    ).fetchall()
    out = []
    for r in rows:
        a = dict(r)
        mine = [p for p in held if p["assignment_id"] == a["id"]]
        a["positions_cents"] = sum(p["value_cents"] or p["cost_cents"] for p in mine)
        a["equity_cents"] = a["cash_cents"] + a["reserved_cents"] + a["positions_cents"]
        base = a["base_cents"] if a["base_cents"] is not None else a["bankroll_cents"]
        a["today_cents"] = a["equity_cents"] - base
        a["total_return"] = (a["equity_cents"] - a["bankroll_cents"]) / a["bankroll_cents"] if a["bankroll_cents"] else None
        a["short_params"] = short_params(a["params"])
        a["held_symbols"] = [p["symbol"] for p in mine]
        out.append(a)
    return out


def orders(conn: psycopg.Connection, limit: int = MAX_ORDERS) -> dict[str, Any]:
    """The orders of the newest session that has any (today's, once the decision ran),
    newest first, with the reject or cancel reason in words, plus that session date."""
    row = conn.execute("SELECT max(session_date) AS d FROM stock_orders").fetchone()
    session = row["d"] if row else None
    if session is None:
        return {"session_date": None, "rows": []}
    rows = conn.execute(
        """
        SELECT o.id, o.assignment_id, o.mode, o.session_date, o.symbol, o.side, o.qty, o.ref_price_cents,
               o.reserved_cents, o.status, o.reason, o.rationale, o.exchange_order_id, o.filled_qty,
               o.avg_fill_price, o.created_at, o.updated_at
          FROM stock_orders o WHERE o.session_date = %s ORDER BY o.created_at DESC, o.id LIMIT %s
        """,
        (session, max(1, int(limit))),
    ).fetchall()
    out = []
    for r in rows:
        o = dict(r)
        o["reason_text"] = REASON_TEXT.get(o["reason"] or "", None)
        o["value_cents"] = o["qty"] * o["ref_price_cents"]
        out.append(o)
    return {"session_date": session, "rows": out}


def attention_items(conn: psycopg.Connection) -> list[dict[str, str]]:
    """Home's "Needs attention" Stocks row: halted stock assignments or broker
    warnings (one row, linking to /stocks); empty when neither."""
    halted = int(conn.execute("SELECT count(*) AS n FROM stock_assignments WHERE status = 'halted'").fetchone()["n"])
    row = conn.execute("SELECT warnings FROM stock_broker_state WHERE id = 1").fetchone()
    warnings = row["warnings"] if row and isinstance(row["warnings"], list) else []
    if not halted and not warnings:
        return []
    parts = []
    if halted:
        parts.append(f"{halted} assignment{'' if halted == 1 else 's'} halted")
    if warnings:
        parts.append(f"{len(warnings)} broker warning{'' if len(warnings) == 1 else 's'}")
    last = warnings[-1] if warnings else None
    meta = (last.get("message") if isinstance(last, dict) else str(last)) if last else "Resume or close them on Stocks"
    return [{"key": "stocks", "state": "warn", "word": "stocks", "title": "Stocks: " + ", ".join(parts),
             "meta": str(meta or "see Stocks")[:120], "href": "/stocks"}]
