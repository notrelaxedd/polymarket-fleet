"""The leaderboard (docs/MODELS.md, docs/TRADING.md "Leaderboard and P&L") and the
owner's model detail view.

One row per lineage. Each row carries the root model's backtest metrics and the
lineage's pooled paper (and live) record from model_scores. Rank mode: paper, by
shrunk CLV (avg_clv * bets / (bets + 25), ties broken by ROI), once the lineage has
PAPER_RANK_GAMES paper games and PAPER_RANK_BETS paper bets; otherwise the step 3
backtest ranking by shrunk ROI (roi * n_bets / (n_bets + 100)), ties broken by
log-loss, when the root has MIN_RANKED_BETS bets. Paper-ranked lineages come first.
Retired lineages are always unranked.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.models import get_model, lineage_rows

MIN_RANKED_BETS = 50
PAPER_RANK_GAMES, PAPER_RANK_BETS, CLV_SHRINK = 5, 30, 25
EMPTY_RECORD: dict[str, Any] = {"games": 0, "bets": 0, "pnl_cents": 0, "stake_cents": 0, "roi": None, "avg_clv": None}
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


def shrunk_clv(record: dict[str, Any] | None) -> float:
    """avg_clv * bets / (bets + 25); 0 without a CLV."""
    if not isinstance(record, dict) or record.get("avg_clv") is None:
        return 0.0
    bets = float(record.get("bets") or 0)
    return float(record["avg_clv"]) * bets / (bets + CLV_SHRINK)


def paper_ranked(record: dict[str, Any] | None) -> bool:
    """True once a lineage's paper record is big enough to rank on."""
    record = record or {}
    return int(record.get("games") or 0) >= PAPER_RANK_GAMES and int(record.get("bets") or 0) >= PAPER_RANK_BETS


def trading_records(conn: psycopg.Connection) -> dict[tuple[Any, str], dict[str, Any]]:
    """(lineage_id, mode) -> pooled {games, bets, pnl_cents, stake_cents, roi, avg_clv}
    from model_scores (CLV stake-weighted)."""
    rows = conn.execute(
        """
        SELECT lineage_id, mode, count(*) AS games, COALESCE(SUM(n_bets), 0) AS bets,
               COALESCE(SUM(pnl_cents), 0) AS pnl_cents, COALESCE(SUM(stake_cents), 0) AS stake_cents,
               SUM(CASE WHEN avg_clv IS NOT NULL THEN avg_clv * stake_cents END) AS clv_weight,
               SUM(CASE WHEN avg_clv IS NOT NULL THEN stake_cents END) AS clv_stake
          FROM model_scores GROUP BY lineage_id, mode
        """
    ).fetchall()
    out = {}
    for r in rows:
        stake = int(r["stake_cents"])
        clv_stake = float(r["clv_stake"] or 0)
        out[(r["lineage_id"], r["mode"])] = {
            "games": int(r["games"]), "bets": int(r["bets"]), "pnl_cents": int(r["pnl_cents"]), "stake_cents": stake,
            "roi": int(r["pnl_cents"]) / stake if stake > 0 else None,
            "avg_clv": float(r["clv_weight"]) / clv_stake if clv_stake > 0 else None,
        }
    return out


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


def lineage_row(
    root: dict[str, Any], members: int, records: dict[tuple[Any, str], dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The leaderboard entry of a lineage from its root row and the pooled trading records."""
    metrics = root.get("backtest_metrics")
    records = records or {}
    paper = records.get((root["lineage_id"], "paper"), EMPTY_RECORD)
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
        "paper": dict(paper),
        "live": dict(records.get((root["lineage_id"], "live"), EMPTY_RECORD)),
        "paper_score": shrunk_clv(paper),
        "rank_mode": "paper" if paper_ranked(paper) else "backtest",
        "members": members,
        "created_at": root["created_at"],
        "updated_at": root["updated_at"],
    }


def _rank_key(entry: dict[str, Any]) -> tuple[float, float, Any]:
    log_loss = _num(entry["metrics"].get("log_loss"))
    return (-entry["score"], log_loss if log_loss is not None else float("inf"), entry["created_at"])


def _paper_key(entry: dict[str, Any]) -> tuple[float, float, Any]:
    roi = entry["paper"].get("roi")
    return (-entry["paper_score"], -(roi if roi is not None else 0.0), entry["created_at"])


def leaderboard(conn: psycopg.Connection) -> dict[str, list[dict[str, Any]]]:
    """{"ranked": [...], "unranked": [...]} over every lineage with a root row: the
    paper-ranked lineages first, then the backtest-ranked ones, then the rest."""
    roots = conn.execute(
        """
        SELECT r.*, (SELECT count(*) FROM models m WHERE m.lineage_id = r.lineage_id) AS members
          FROM models r WHERE r.id = r.lineage_id ORDER BY r.created_at DESC, r.id
        """
    ).fetchall()
    records = trading_records(conn)
    by_paper, by_backtest, unranked = [], [], []
    for root in roots:
        entry = lineage_row(root, int(root["members"]), records)
        n_bets = _num(entry["metrics"].get("n_bets")) or 0.0
        if root["status"] == "retired":
            unranked.append(entry)
        elif entry["rank_mode"] == "paper":
            by_paper.append(entry)
        elif n_bets >= MIN_RANKED_BETS:
            by_backtest.append(entry)
        else:
            unranked.append(entry)
    by_paper.sort(key=_paper_key)
    by_backtest.sort(key=_rank_key)
    ranked = by_paper + by_backtest
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
    records = trading_records(conn)
    model["paper"] = dict(records.get((model["lineage_id"], "paper"), EMPTY_RECORD))
    model["live"] = dict(records.get((model["lineage_id"], "live"), EMPTY_RECORD))
    model["paper_score"] = shrunk_clv(model["paper"])
    model["rank_mode"] = "paper" if paper_ranked(model["paper"]) else "backtest"
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
