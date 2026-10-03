"""The leaderboard (docs/MODELS.md, step 3 scope) and the owner's model detail view.

One row per lineage, judged on the root model's backtest metrics: ranked by shrunk
ROI (roi * n_bets / (n_bets + 100)), ties broken by log-loss, when the root has at
least MIN_RANKED_BETS bets and the lineage is not retired; everything else is listed
as unranked.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.models import get_model, lineage_rows

MIN_RANKED_BETS = 50
METRIC_KEYS = ("roi", "n_bets", "log_loss", "market_log_loss", "max_drawdown", "seasons", "hit_rate", "avg_edge", "pnl_cents")


def shrunk_roi(metrics: dict[str, Any] | None) -> float:
    """roi * n_bets / (n_bets + 100); 0 without usable metrics."""
    if not isinstance(metrics, dict):
        return 0.0
    try:
        n = float(metrics.get("n_bets") or 0)
        return float(metrics.get("roi") or 0.0) * n / (n + 100.0)
    except (TypeError, ValueError):
        return 0.0


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def short_params(family: str, params: dict[str, Any] | None) -> str:
    """A one-line label such as "K 24 · HFA 55 · MOV on" (elo_blend) or key value pairs."""
    params = params if isinstance(params, dict) else {}
    if family == "elo_blend":
        k, hfa = _num(params.get("k")), _num(params.get("hfa"))
        parts = [
            f"K {round(k)}" if k is not None else "K ?",
            f"HFA {round(hfa)}" if hfa is not None else "HFA ?",
            "MOV on" if params.get("mov_scale") else "MOV off",
        ]
        return " · ".join(parts)
    parts = []
    for key in sorted(params)[:3]:
        value = params[key]
        parts.append(f"{key} {round(value, 3) if isinstance(value, float) else value}")
    return " · ".join(parts) or "defaults"


def _metric_subset(metrics: dict[str, Any] | None) -> dict[str, Any]:
    metrics = metrics if isinstance(metrics, dict) else {}
    return {key: metrics.get(key) for key in METRIC_KEYS}


def lineage_row(root: dict[str, Any], members: int) -> dict[str, Any]:
    """The leaderboard entry of a lineage from its root row."""
    metrics = root.get("backtest_metrics")
    return {
        "id": root["id"],
        "lineage_id": root["lineage_id"],
        "family": root["family"],
        "params": root["params"],
        "short_params": short_params(root["family"], root["params"]),
        "status": root["status"],
        "summary": root.get("summary"),
        "metrics": _metric_subset(metrics),
        "score": shrunk_roi(metrics),
        "members": members,
        "created_at": root["created_at"],
        "updated_at": root["updated_at"],
    }


def _rank_key(entry: dict[str, Any]) -> tuple[float, float, Any]:
    log_loss = _num(entry["metrics"].get("log_loss"))
    return (-entry["score"], log_loss if log_loss is not None else float("inf"), entry["created_at"])


def leaderboard(conn: psycopg.Connection) -> dict[str, list[dict[str, Any]]]:
    """{"ranked": [...], "unranked": [...]} over every lineage with a root row."""
    roots = conn.execute(
        """
        SELECT r.*, (SELECT count(*) FROM models m WHERE m.lineage_id = r.lineage_id) AS members
          FROM models r WHERE r.id = r.lineage_id ORDER BY r.created_at DESC, r.id
        """
    ).fetchall()
    ranked, unranked = [], []
    for root in roots:
        entry = lineage_row(root, int(root["members"]))
        n_bets = _num(entry["metrics"].get("n_bets")) or 0.0
        if root["status"] != "retired" and n_bets >= MIN_RANKED_BETS:
            ranked.append(entry)
        else:
            unranked.append(entry)
    ranked.sort(key=_rank_key)
    for position, entry in enumerate(ranked, start=1):
        entry["rank"] = position
    return {"ranked": ranked, "unranked": unranked}


def model_jobs(conn: psycopg.Connection, model_id: Any) -> list[dict[str, Any]]:
    """Jobs that created this model or ran against it (params.model_id), newest first."""
    return conn.execute(
        """
        SELECT j.id, j.kind, j.status, j.created_at, j.finished_at, j.lease_worker_id, j.target_worker_id,
               (m.id IS NOT NULL) AS created_model
          FROM jobs j LEFT JOIN models m ON m.created_by_job_id = j.id AND m.id = %(id)s
         WHERE m.id IS NOT NULL OR j.params ->> 'model_id' = %(text)s
         ORDER BY j.created_at DESC, j.id LIMIT 50
        """,
        {"id": model_id, "text": str(model_id)},
    ).fetchall()


def model_detail(conn: psycopg.Connection, model_id: Any) -> dict[str, Any]:
    """GET /api/models/{id}: the row plus short params, lineage members and related jobs."""
    model = dict(get_model(conn, model_id))
    model["short_params"] = short_params(model["family"], model["params"])
    model["score"] = shrunk_roi(model.get("backtest_metrics"))
    model["lineage"] = [
        {
            "id": r["id"], "parent_model_id": r["parent_model_id"], "trained_through": r["trained_through"],
            "status": r["status"], "created_at": r["created_at"], "created_by_job_id": r["created_by_job_id"],
            "is_root": r["id"] == r["lineage_id"],
        }
        for r in lineage_rows(conn, model["lineage_id"])
    ]
    model["jobs"] = model_jobs(conn, model["id"])
    return model
