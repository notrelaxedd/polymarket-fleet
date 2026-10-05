"""The leaderboard (docs/MODELS.md, docs/TRADING.md "Leaderboard and P&L",
docs/ROBUSTNESS.md A1) and the owner's model detail view.

One row per lineage. Each row carries the root model's search-era backtest metrics,
its validation-era metrics with the step 6 extras (the ROI interval, the market test,
the flags), the lineage's pooled paper (and live) record from model_scores and the
cached paper CLV interval. Rank mode: paper, by shrunk CLV (avg_clv * bets / (bets +
25), ties broken by ROI), once the lineage has PAPER_RANK_GAMES paper games and
PAPER_RANK_BETS paper bets; otherwise validation, by the validation era's shrunk ROI
(roi * n_bets / (n_bets + 100)), ties broken by the mean log-loss gain over the
market. Paper-ranked lineages come first. A lineage without validation metrics is
unranked with the reason "not validated", whatever its paper record; a retired
lineage is always unranked.

Step 6 Part B (host/leaderboard_snapshot.py): a lineage with snapshot replay metrics
carries a "snapshot" group, and between paper and validation sits rank mode
"snapshot" (30 snapshot-scored bets, by shrunk snapshot CLV). Like paper, it only
ranks a lineage the held-out era has judged.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.eligibility import model_flags
from host.leaderboard_snapshot import rank_mode, snapshot_key, snapshot_summary
from host.models import get_model, lineage_rows
from host.stats import shrunk_roi

PAPER_RANK_GAMES, PAPER_RANK_BETS, CLV_SHRINK = 5, 30, 25
EMPTY_RECORD: dict[str, Any] = {"games": 0, "bets": 0, "pnl_cents": 0, "stake_cents": 0, "roi": None, "avg_clv": None}
METRIC_KEYS = ("roi", "n_bets", "log_loss", "market_log_loss", "max_drawdown", "seasons", "hit_rate", "avg_edge", "pnl_cents")
VALIDATION_KEYS = METRIC_KEYS + (
    "shrunk_roi", "ci", "mean_ll_gain", "market_p", "flags", "calib_slope", "calib_intercept", "brier_decomposition",
)
MARKET_BEATEN_P = 0.05
FLAG_MEANINGS = {
    "overfit": "the search era looked better than the held-out era: its shrunk ROI was more than 3 points higher, "
               "or it beat the market on log-loss while the validation era did not",
    "fragile": "a spread two cents wider removes half the bets or turns the ROI negative, or a 10% nudge of the "
               "parameters does",
    "regime_dependent": "in one regime pair (favourite or underdog, home or away, divisional, primetime, cold or "
                        "windy) one side makes the profit while the other, holding at least a fifth of the bets, "
                        "gives back more than half of it",
}
UNRANKED_REASONS = {"retired": "retired", "not_validated": "not validated"}


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


def paper_intervals(conn: psycopg.Connection) -> dict[Any, dict[str, Any]]:
    """lineage_id -> the cached paper CLV bootstrap {n_bets, avg_clv, ci, computed_at}."""
    rows = conn.execute("SELECT * FROM lineage_paper_ci").fetchall()
    return {r["lineage_id"]: _paper_ci(r) for r in rows}


def _paper_ci(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    ci = [float(row["clv_low"]), float(row["clv_high"])] if row["clv_low"] is not None and row["clv_high"] is not None else None
    return {"n_bets": int(row["n_bets"]), "avg_clv": _num(row["avg_clv"]), "ci": ci, "computed_at": row["computed_at"]}


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
    if family == "epa_blend":
        window, shrink, l2 = _num(params.get("window")), _num(params.get("shrink")), _num(params.get("l2"))
        return " · ".join([
            f"window {round(window)}" if window is not None else "window ?",
            f"shrink {shrink:.1f}" if shrink is not None else "shrink ?",
            f"L2 {l2:.2f}" if l2 is not None else "L2 ?",
        ])
    parts = []
    for key in sorted(params)[:3]:
        value = params[key]
        parts.append(f"{key} {round(value, 3) if isinstance(value, float) else value}")
    return " · ".join(parts) or "defaults"


def _subset(metrics: dict[str, Any] | None, keys: tuple[str, ...]) -> dict[str, Any]:
    metrics = metrics if isinstance(metrics, dict) else {}
    return {key: metrics.get(key) for key in keys}


def validation_summary(root: dict[str, Any]) -> dict[str, Any] | None:
    """The validation-era numbers a leaderboard row shows, with `shrunk_roi` filled
    in and `beats_market` (market_p below MARKET_BEATEN_P); None when not validated."""
    metrics = root.get("validation_metrics")
    if not isinstance(metrics, dict):
        return None
    out = _subset(metrics, VALIDATION_KEYS)
    out["shrunk_roi"] = shrunk_roi(metrics)
    out["flags"] = model_flags(metrics)
    market_p = _num(metrics.get("market_p"))
    out["beats_market"] = market_p is not None and market_p < MARKET_BEATEN_P
    return out


def lineage_row(
    root: dict[str, Any], members: int, records: dict[tuple[Any, str], dict[str, Any]] | None = None,
    paper_cis: dict[Any, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The leaderboard entry of a lineage from its root row, the pooled trading
    records and the cached paper intervals."""
    records = records or {}
    paper = records.get((root["lineage_id"], "paper"), EMPTY_RECORD)
    validation = validation_summary(root)
    stress = root.get("stress_metrics") if isinstance(root.get("stress_metrics"), dict) else None
    snapshot = snapshot_summary(root)
    return {
        "id": root["id"],
        "lineage_id": root["lineage_id"],
        "family": root["family"],
        "params": root["params"],
        "short_params": short_params(root["family"], root["params"]),
        "status": root["status"],
        "summary": root.get("summary"),
        "metrics": _subset(root.get("backtest_metrics"), METRIC_KEYS),
        "search_score": shrunk_roi(root.get("backtest_metrics")),
        "validation": validation,
        "validated": validation is not None,
        "score": validation["shrunk_roi"] if validation else 0.0,
        "ll_gain": _num(validation.get("mean_ll_gain")) if validation else None,
        "flags": model_flags(root.get("validation_metrics"), stress),
        "stress_flags": model_flags(None, stress),
        "paper": dict(paper),
        "live": dict(records.get((root["lineage_id"], "live"), EMPTY_RECORD)),
        "paper_score": shrunk_clv(paper),
        "paper_ci": (paper_cis or {}).get(root["lineage_id"]),
        "snapshot": snapshot,
        "snapshot_score": snapshot["score"] if snapshot else 0.0,
        "rank_mode": rank_mode(paper_ranked(paper), snapshot),
        "members": members,
        "created_at": root["created_at"],
        "updated_at": root["updated_at"],
    }


def _rank_key(entry: dict[str, Any]) -> tuple[float, float, Any]:
    gain = entry.get("ll_gain")
    return (-entry["score"], -(gain if gain is not None else float("-inf")), entry["created_at"])


def _paper_key(entry: dict[str, Any]) -> tuple[float, float, Any]:
    roi = entry["paper"].get("roi")
    return (-entry["paper_score"], -(roi if roi is not None else 0.0), entry["created_at"])


def unranked_reason(entry: dict[str, Any]) -> str | None:
    """Why a lineage sits in the unranked list, or None when it ranks."""
    if entry["status"] == "retired":
        return UNRANKED_REASONS["retired"]
    if not entry["validated"]:  # a paper record never ranks a lineage the held-out era has not judged
        return UNRANKED_REASONS["not_validated"]
    return None


def leaderboard(conn: psycopg.Connection) -> dict[str, list[dict[str, Any]]]:
    """{"ranked": [...], "unranked": [...]} over every lineage with a root row: the
    paper-ranked lineages first, then the snapshot-ranked ones, then the
    validation-ranked ones, then the rest (each with its `unranked_reason`)."""
    roots = conn.execute(
        """
        SELECT r.*, (SELECT count(*) FROM models m WHERE m.lineage_id = r.lineage_id) AS members
          FROM models r WHERE r.id = r.lineage_id ORDER BY r.created_at DESC, r.id
        """
    ).fetchall()
    records = trading_records(conn)
    cis = paper_intervals(conn)
    by_paper, by_snapshot, by_validation, unranked = [], [], [], []
    for root in roots:
        entry = lineage_row(root, int(root["members"]), records, cis)
        reason = unranked_reason(entry)
        if reason is not None:
            entry["unranked_reason"] = reason
            unranked.append(entry)
        elif entry["rank_mode"] == "paper":
            by_paper.append(entry)
        elif entry["rank_mode"] == "snapshot":
            by_snapshot.append(entry)
        else:
            by_validation.append(entry)
    by_paper.sort(key=_paper_key)
    by_snapshot.sort(key=snapshot_key)
    by_validation.sort(key=_rank_key)
    ranked = by_paper + by_snapshot + by_validation
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
    """GET /api/models/{id}: the row plus short params, the validation summary, flags
    with their meanings, the paper interval, lineage members and related jobs."""
    model = dict(get_model(conn, model_id))
    model["short_params"] = short_params(model["family"], model["params"])
    model["search_score"] = shrunk_roi(model.get("backtest_metrics"))
    model["validation"] = validation_summary(model)
    model["validated"] = model["validation"] is not None
    model["score"] = model["validation"]["shrunk_roi"] if model["validation"] else 0.0
    model["flags"] = model_flags(model.get("validation_metrics"), model.get("stress_metrics"))
    model["flag_meanings"] = {flag: FLAG_MEANINGS.get(flag, "") for flag in model["flags"]}
    records = trading_records(conn)
    model["paper"] = dict(records.get((model["lineage_id"], "paper"), EMPTY_RECORD))
    model["live"] = dict(records.get((model["lineage_id"], "live"), EMPTY_RECORD))
    model["paper_score"] = shrunk_clv(model["paper"])
    model["paper_ci"] = _paper_ci(conn.execute("SELECT * FROM lineage_paper_ci WHERE lineage_id = %s", (model["lineage_id"],)).fetchone())
    model["snapshot"] = snapshot_summary(model)
    model["rank_mode"] = rank_mode(paper_ranked(model["paper"]), model["snapshot"])
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
