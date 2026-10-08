"""The gate verdict and the newest informational backtest of each stock model row on
/stocks (host.stocks.views.models): read-only, and the thresholds are read without the
FOR SHARE lock host.stocks.eligibility takes, so a page read never waits on a save.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.settings import get_setting
from host.stocks import eligibility


def metric(metrics: Any, key: str) -> float | None:
    """A finite number out of a metrics object (None when absent or not a number)."""
    value = metrics.get(key) if isinstance(metrics, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


def thresholds(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    """The two stock thresholds in force, read without the FOR SHARE lock
    eligibility.thresholds takes (a page read must not wait on a thresholds save)."""
    out = {}
    for name, key, defaults in (("backtest", "thresholds_stock_backtest", eligibility.DEFAULT_BACKTEST),
                                ("paper", "thresholds_stock_paper", eligibility.DEFAULT_PAPER)):
        stored = get_setting(conn, key)
        out[name] = {**defaults, **({k: v for k, v in stored.items() if k in defaults and v is not None}
                                    if isinstance(stored, dict) else {})}
    return out


def verdict(conn: psycopg.Connection, model: dict[str, Any], limits: dict[str, dict[str, Any]]) -> str | None:
    """The first gate a candidate or paper_ok model still fails, in words, with its own
    number in brackets (eligibility.backtest_misses / paper_misses); None for a model
    with nothing left to pass, a live_eligible or a retired one."""
    if model["status"] == "candidate":
        bt, val, b = model.get("backtest_metrics"), model.get("validation_metrics"), limits["backtest"]
        miss = eligibility.backtest_misses(model, b)
        if not miss:
            return None
        if not isinstance(bt, dict):
            return "no backtest metrics yet"
        if miss[0] == "min_sharpe":
            return f"needs Sharpe {float(b['min_sharpe']):.2f} ({metric(bt, 'sharpe') or 0:.2f})"
        if miss[0] == "max_drawdown":
            return f"drawdown over {float(b['max_drawdown']):.0%} ({metric(bt, 'max_drawdown') or 0:.1%})"
        if miss[0] == "min_trades":
            return f"needs {int(b['min_trades'])} trades ({int(metric(bt, 'trades') or 0)})"
        if not isinstance(val, dict):
            return "not validated: press Validate"
        return (f"validation Sharpe {metric(val, 'sharpe') or 0:.2f} below"
                f" {float(b['min_validation_sharpe']):.2f}")
    if model["status"] == "paper_ok":
        p = limits["paper"]
        stats = eligibility.paper_stats(conn, int(model["id"]))
        miss = eligibility.paper_misses(stats, p)
        if not miss:
            return None
        if miss[0] == "min_days":
            return f"not yet live eligible: needs {int(p['min_days'])} paper sessions ({stats['days']} so far)"
        if miss[0] == "min_return":
            return f"not yet live eligible: paper return below {float(p['min_return']):.0%}"
        return f"not yet live eligible: paper drawdown over {float(p['max_drawdown']):.0%}"
    return None


def last_backtests(conn: psycopg.Connection) -> dict[int, dict[str, Any]]:
    """{model_id: {"job_id", "status", "sharpe", "cagr"}} of each model's newest
    stock_backtest job (the informational backtest of a row's menu)."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (params ->> 'model_id') params ->> 'model_id' AS model_id, id, status, result
          FROM jobs WHERE kind = 'stock_backtest' AND params ? 'model_id'
         ORDER BY params ->> 'model_id', created_at DESC
        """
    ).fetchall()
    out = {}
    for r in rows:
        if not str(r["model_id"]).isdigit():
            continue
        metrics = r["result"].get("backtest_metrics") if isinstance(r["result"], dict) else None
        out[int(r["model_id"])] = {"job_id": str(r["id"]), "status": r["status"], "sharpe": metric(metrics, "sharpe"),
                                   "cagr": metric(metrics, "cagr")}
    return out
