"""Stock model status (contract section 5): candidate, paper_ok, live_eligible, retired.

- paper_ok needs the backtest gate (`thresholds_stock_backtest`): backtest sharpe >=
  min_sharpe, max_drawdown <= max_drawdown, trades >= min_trades, and validation_metrics
  present with sharpe >= min_validation_sharpe;
- live_eligible also needs the paper gate (`thresholds_stock_paper`) on the model's
  paper assignments' daily marks: >= min_days distinct session dates marked, a total
  paper return >= min_return and a paper max drawdown <= max_drawdown;
- retired is set only by the owner (host.stocks.models.retire_model) and is final.

`recompute(conn, model_id)` re-derives the status from scratch, so a model that stops
meeting a gate is demoted the same way it was promoted. A demotion out of live_eligible
halts the model's live assignments (their orders cancelled, cancel_requested at
Alpaca). Called after every new result, a thresholds change, every paper mark (by the
exchange's stock_marks task) and at host start (recompute_all).

The paper record over several assignments: return = (sum of each assignment's latest
marked equity - sum of their bankrolls) / sum of their bankrolls; max drawdown = the
largest drawdown of any one assignment's equity series (the bankroll is its first peak).
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.events import add_audit

DEFAULT_BACKTEST = {"min_sharpe": 0.5, "max_drawdown": 0.30, "min_trades": 30, "min_validation_sharpe": 0.0}
DEFAULT_PAPER = {"min_days": 20, "min_return": -0.02, "max_drawdown": 0.15}


def _number(metrics: Any, key: str) -> float | None:
    value = metrics.get(key) if isinstance(metrics, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


def _limits(conn: psycopg.Connection, key: str, defaults: dict[str, Any]) -> dict[str, Any]:
    """A thresholds setting read FOR SHARE (a thresholds save waits for this model write)."""
    row = conn.execute("SELECT value FROM settings WHERE key = %s FOR SHARE", (key,)).fetchone()
    out = dict(defaults)
    if row is not None and isinstance(row["value"], dict):
        out.update({k: v for k, v in row["value"].items() if k in out and v is not None})
    return out


def thresholds(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    """{"backtest": ..., "paper": ...} in force."""
    return {"backtest": _limits(conn, "thresholds_stock_backtest", DEFAULT_BACKTEST),
            "paper": _limits(conn, "thresholds_stock_paper", DEFAULT_PAPER)}


def backtest_misses(model: dict[str, Any], limits: dict[str, Any]) -> list[str]:
    """The backtest-gate rules the model fails (empty when it passes)."""
    bt, val = model.get("backtest_metrics"), model.get("validation_metrics")
    sharpe, drawdown, trades = _number(bt, "sharpe"), _number(bt, "max_drawdown"), _number(bt, "trades")
    misses = []
    if sharpe is None or sharpe < float(limits["min_sharpe"]):
        misses.append("min_sharpe")
    if drawdown is None or drawdown > float(limits["max_drawdown"]):
        misses.append("max_drawdown")
    if trades is None or trades < float(limits["min_trades"]):
        misses.append("min_trades")
    val_sharpe = _number(val, "sharpe")
    if val_sharpe is None or val_sharpe < float(limits["min_validation_sharpe"]):
        misses.append("min_validation_sharpe")
    return misses


def paper_stats(conn: psycopg.Connection, model_id: int) -> dict[str, Any]:
    """{"days", "return", "max_drawdown", "assignments"} of the model's paper marks."""
    rows = conn.execute(
        """
        SELECT a.id, a.bankroll_cents, m.session_date, m.equity_cents
          FROM stock_assignments a JOIN stock_marks m ON m.assignment_id = a.id
         WHERE a.model_id = %s AND a.mode = 'paper'
         ORDER BY a.id, m.session_date
        """,
        (model_id,),
    ).fetchall()
    series: dict[int, dict[str, Any]] = {}
    days: set[Any] = set()
    for r in rows:
        days.add(r["session_date"])
        s = series.setdefault(r["id"], {"bankroll": int(r["bankroll_cents"]), "peak": int(r["bankroll_cents"]),
                                        "last": None, "dd": 0.0})
        equity = int(r["equity_cents"])
        s["peak"] = max(s["peak"], equity)
        if s["peak"] > 0:
            s["dd"] = max(s["dd"], (s["peak"] - equity) / s["peak"])
        s["last"] = equity
    base = sum(s["bankroll"] for s in series.values())
    total = sum(s["last"] for s in series.values())
    return {"days": len(days), "return": (total - base) / base if base > 0 else None,
            "max_drawdown": max((s["dd"] for s in series.values()), default=None), "assignments": len(series)}


def paper_misses(stats: dict[str, Any], limits: dict[str, Any]) -> list[str]:
    """The paper-gate rules the record fails (empty when it passes)."""
    misses = []
    if stats["days"] < int(limits["min_days"]):
        misses.append("min_days")
    if stats["return"] is None or stats["return"] < float(limits["min_return"]):
        misses.append("min_return")
    if stats["max_drawdown"] is None or stats["max_drawdown"] > float(limits["max_drawdown"]):
        misses.append("max_drawdown")
    return misses


def status_for(conn: psycopg.Connection, model: dict[str, Any], limits: dict[str, dict[str, Any]]) -> str:
    if model["status"] == "retired":
        return "retired"
    if backtest_misses(model, limits["backtest"]):
        return "candidate"
    if paper_misses(paper_stats(conn, int(model["id"])), limits["paper"]):
        return "paper_ok"
    return "live_eligible"


def halt_live(conn: psycopg.Connection, model_id: int, actor: str, reason: str) -> list[int]:
    """Halt the model's active live assignments (their orders cancelled); the ids."""
    from host.stocks import assignments

    rows = conn.execute(
        "SELECT id FROM stock_assignments WHERE model_id = %s AND mode = 'live' AND status = 'active' ORDER BY id",
        (model_id,),
    ).fetchall()
    for row in rows:
        assignments.halt_assignment(conn, row["id"], reason, actor)
    return [int(r["id"]) for r in rows]


def recompute(
    conn: psycopg.Connection, model_id: Any, actor: str = "eligibility",
    limits: dict[str, dict[str, Any]] | None = None,
) -> str | None:
    """Recompute and store one stock model's status; the new status (None when the
    model does not exist). Thresholds are read before the model row is locked; a model
    that is live_eligible takes the live approval lock first (as live_off does), so a
    demotion and its halts commit before any live approval that would still see it."""
    from host.kill import approval_lock
    from host.stocks.models import model_id_of

    mid = model_id_of(model_id)
    if mid is None:
        return None
    limits = limits if limits is not None else thresholds(conn)
    was = conn.execute("SELECT status FROM stock_models WHERE id = %s", (mid,)).fetchone()
    if was is None:
        return None
    if was["status"] == "live_eligible":
        approval_lock(conn, "live")
    model = conn.execute("SELECT * FROM stock_models WHERE id = %s FOR UPDATE", (mid,)).fetchone()
    current = model["status"]
    new = status_for(conn, dict(model), limits)
    if new == current:
        return new
    conn.execute("UPDATE stock_models SET status = %s, updated_at = now() WHERE id = %s", (new, mid))
    halted: list[int] = []
    if current == "live_eligible":
        halted = halt_live(conn, mid, actor, "model no longer live_eligible")
    add_audit(conn, "stock_eligibility_changed", f"stock_model:{mid}", actor, {"status": current},
              {"status": new, "live_assignments_halted": halted})
    return new


def recompute_all(conn: psycopg.Connection, actor: str = "eligibility") -> int:
    """Recompute every stock model that is not retired (host start, thresholds change);
    the number recomputed."""
    limits = thresholds(conn)
    rows = conn.execute("SELECT id FROM stock_models WHERE status <> 'retired' ORDER BY id").fetchall()
    for row in rows:
        recompute(conn, row["id"], actor, limits)
    return len(rows)
