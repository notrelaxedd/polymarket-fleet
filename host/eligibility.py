"""Lineage eligibility (docs/MODELS.md "Eligibility", docs/ROBUSTNESS.md A4).

A lineage's status comes from its root model's metrics against the
`thresholds_backtest` settings: candidate -> paper_ok when every rule holds, back to
candidate when they stop holding. With `require_validation` (the default) the era
judged is the held-out validation era (`validation_metrics`) and every rule applies:
`n_bets >= min_bets`, `roi >= min_roi`, `max_drawdown <= max_drawdown`, the ROI 5th
percentile `>= min_roi_ci_low`, `market_p <= max_market_p` and none of `forbid_flags`
among the model's flags (validation and stress flags together); a lineage without
validation metrics is a candidate. With `require_validation` false the three step 3
rules judge `backtest_metrics` as before. paper_ok -> live_eligible is the paper gate
(host/paper_gate.py); an existing live_eligible row keeps it while this gate holds.
A retired lineage stays retired. The status is written to every row of the lineage.
Whenever a lineage leaves `live_eligible` its active live assignments are halted,
which cancels their orders (docs/LIVE.md).

Step 6 Part C: an ingame_wp lineage is judged by host/ingame_eligibility.py instead
(paper_ok when its held-out validation beats the vegas_wp baseline over at least
10000 plays, else candidate; never live_eligible, and the paper gate skips it).

`thresholds` reads the setting FOR SHARE, and every model write reads it before it
touches a model row: a write that overlaps a thresholds change waits for the new
value, and the thresholds UPDATE waits for in-flight model writes, so the
recompute_all that follows it sees their rows.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.events import add_audit
from host.ingame_eligibility import ingame_status, is_ingame, is_ingame_lineage

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "min_bets": 50, "min_roi": 0.02, "max_drawdown": 0.30, "require_validation": True,
    "min_roi_ci_low": 0.0, "max_market_p": 0.10, "forbid_flags": ["overfit", "fragile"],
}
BASE_RULES = ("min_bets", "min_roi", "max_drawdown")


def thresholds(conn: psycopg.Connection) -> dict[str, Any]:
    """The backtest thresholds in force (read FOR SHARE, see the module docstring),
    with the ROBUSTNESS.md defaults for missing keys."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'thresholds_backtest' FOR SHARE").fetchone()
    value = None if row is None else row["value"]
    out = dict(DEFAULT_THRESHOLDS)
    if isinstance(value, dict):
        out.update({k: v for k, v in value.items() if k in out and v is not None})
    return out


def _number(metrics: dict[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def ci_low(metrics: dict[str, Any], key: str = "roi") -> float | None:
    """The lower bound of `metrics.ci[key]`; None when missing or malformed."""
    ci = metrics.get("ci")
    bounds = ci.get(key) if isinstance(ci, dict) else None
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
        return None
    low = bounds[0]
    if isinstance(low, bool) or not isinstance(low, (int, float)):
        return None
    return float(low)


def model_flags(metrics: dict[str, Any] | None, stress: dict[str, Any] | None = None) -> list[str]:
    """The flags of a model: `validation_metrics.flags` then `stress_metrics.flags`,
    each once, in that order."""
    out: list[str] = []
    for source in (metrics, stress):
        flags = source.get("flags") if isinstance(source, dict) else None
        for flag in flags if isinstance(flags, list) else []:
            if isinstance(flag, str) and flag not in out:
                out.append(flag)
    return out


def meets_thresholds(metrics: dict[str, Any] | None, limits: dict[str, Any], stress: dict[str, Any] | None = None) -> bool:
    """Every rule of `limits` holds on `metrics`: the three base rules always, and
    `min_roi_ci_low`, `max_market_p` and `forbid_flags` when `limits` carries them.
    A missing or malformed metric never passes."""
    if not isinstance(metrics, dict):
        return False
    n_bets, roi, drawdown = (_number(metrics, k) for k in ("n_bets", "roi", "max_drawdown"))
    if n_bets is None or roi is None or drawdown is None:
        return False
    if not (n_bets >= float(limits["min_bets"]) and roi >= float(limits["min_roi"]) and drawdown <= float(limits["max_drawdown"])):
        return False
    if limits.get("min_roi_ci_low") is not None:
        low = ci_low(metrics, "roi")
        if low is None or low < float(limits["min_roi_ci_low"]):
            return False
    if limits.get("max_market_p") is not None:
        market_p = _number(metrics, "market_p")
        if market_p is None or market_p > float(limits["max_market_p"]):
            return False
    forbid = limits.get("forbid_flags")
    if forbid and set(model_flags(metrics, stress)) & set(forbid):
        return False
    return True


def gate_limits(limits: dict[str, Any]) -> dict[str, Any]:
    """The rules that apply: all of them under `require_validation`, the three base
    rules otherwise (the step 3 gate on the search era, as before)."""
    if limits.get("require_validation", True):
        return limits
    return {key: limits[key] for key in BASE_RULES}


def gate_metrics(row: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any] | None:
    """The metrics object the gate judges for a root row: the validation era under
    `require_validation`, else the search-era backtest metrics."""
    if limits.get("require_validation", True):
        return row.get("validation_metrics")
    return row.get("backtest_metrics")


def status_for(current: str, metrics: dict[str, Any] | None, limits: dict[str, Any], stress: dict[str, Any] | None = None) -> str:
    """The lineage status that follows from the judged metrics."""
    if current == "retired":
        return "retired"
    if not meets_thresholds(metrics, gate_limits(limits), stress):
        return "candidate"
    return "live_eligible" if current == "live_eligible" else "paper_ok"


def root_status(root: dict[str, Any], current: str, limits: dict[str, Any]) -> str:
    """status_for over the era `limits` selects on the root row (the in-game rule for
    an ingame_wp root)."""
    if is_ingame(root):
        return ingame_status(current, root.get("validation_metrics"))
    return status_for(current, gate_metrics(root, limits), limits, root.get("stress_metrics"))


def recompute_lineage(
    conn: psycopg.Connection, lineage_id: Any, limits: dict[str, Any] | None = None, actor: str | None = "eligibility",
) -> str | None:
    """Recompute and store the status of every row of a lineage; the new status, or
    None when the lineage has no root row. `limits` are the thresholds already read
    by the caller (before it locked any model row), else they are read here. A
    lineage that was live_eligible and no longer is gets its live assignments halted
    (under the live approval lock, taken before the model rows as the kill and the
    settlement do)."""
    from host.kill import approval_lock

    was = conn.execute("SELECT status FROM models WHERE id = %s", (lineage_id,)).fetchone()
    if was is not None and was["status"] == "live_eligible":
        approval_lock(conn, "live")
    rows = conn.execute(
        "SELECT id, family, status, backtest_metrics, validation_metrics, stress_metrics FROM models"
        " WHERE lineage_id = %s FOR UPDATE",
        (lineage_id,),
    ).fetchall()
    root = next((r for r in rows if r["id"] == lineage_id), None)
    if root is None:
        return None
    current = "retired" if any(r["status"] == "retired" for r in rows) else root["status"]
    new = root_status(root, current, limits if limits is not None else thresholds(conn))
    conn.execute(
        "UPDATE models SET status = %s, updated_at = now() WHERE lineage_id = %s AND status <> %s",
        (new, lineage_id, new),
    )
    if current == "live_eligible" and new != "live_eligible":
        add_audit(conn, "eligibility_changed", str(lineage_id), actor, {"status": current}, {"status": new})
        halt_live_assignments(conn, lineage_id, actor, "lineage no longer live_eligible")
    return new


def recompute_all(conn: psycopg.Connection) -> int:
    """Recompute every lineage (after a thresholds change); the number of lineages."""
    limits = thresholds(conn)
    rows = conn.execute("SELECT DISTINCT lineage_id FROM models").fetchall()
    for row in rows:
        recompute_lineage(conn, row["lineage_id"], limits)
    return len(rows)


def halt_live_assignments(conn: psycopg.Connection, lineage_id: Any, actor: str | None, reason: str) -> int:
    """Halt every active live assignment of the lineage (orders cancelled by halt)."""
    from host.trading import assignments

    rows = conn.execute(
        "SELECT id FROM assignments WHERE lineage_id = %s AND mode = 'live' AND status = 'active'", (lineage_id,)
    ).fetchall()
    for row in rows:
        assignments.halt_assignment(conn, row["id"], actor, reason)
    return len(rows)


# The paper gate (step 4, with the step 6 CLV bootstrap) lives in host.paper_gate and
# is re-exported here, where the settlement and the settings forms look for it.
from host.paper_gate import (  # noqa: E402
    DEFAULT_PAPER_THRESHOLDS,
    meets_paper_thresholds,
    paper_ci,
    paper_stats,
    paper_thresholds,
)
from host.paper_gate import recompute_paper as _recompute_paper  # noqa: E402


def recompute_paper(conn: psycopg.Connection, lineage_id: Any, actor: str | None = "settle") -> str | None:
    """The paper gate (host.paper_gate.recompute_paper), except for an ingame_wp
    lineage: in-game orders are paper-only, so its paper record never promotes it and
    only the in-game validation rule applies."""
    if is_ingame_lineage(conn, lineage_id):
        return recompute_lineage(conn, lineage_id, actor=actor)
    return _recompute_paper(conn, lineage_id, actor)


__all__ = [
    "DEFAULT_PAPER_THRESHOLDS", "DEFAULT_THRESHOLDS", "ci_low", "gate_limits", "gate_metrics", "halt_live_assignments",
    "meets_paper_thresholds", "meets_thresholds", "model_flags", "paper_ci", "paper_stats", "paper_thresholds",
    "recompute_all", "recompute_lineage", "recompute_paper", "root_status", "status_for", "thresholds",
]
