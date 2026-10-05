"""The in-game parts of the leaderboard and the model detail (docs/INGAME.md "Scoring
and dashboard", contract sections 11 and 12).

- The in-game column group: a lineage's in-game paper (and live) record from
  model_scores (`ingame_n_bets`, `ingame_pnl_cents`, summed over its games); None for
  a lineage without in-game bets. The pooled `paper` record keeps counting every bet.
- An ingame_wp lineage is not ranked with the pre-game models (it has no moneyline ROI
  or CLV): it is listed apart, ordered by its validation, the models beating the
  vegas_wp baseline first, then by the log-loss gain over the baseline (vegas_wp
  log-loss minus the model's, per play). Its validation shows the log-loss against
  vegas_wp overall, per period, per score bucket, and the calibration buckets.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.ingame_eligibility import INGAME_FAMILY, MIN_PLAYS, ingame_reason, is_ingame

PERIOD_LABELS = {"1": "Q1", "2": "Q2", "3": "Q3", "4": "Q4", "5": "OT"}
SCORE_BUCKETS = ("<=-9", "-8..-1", "0", "1..8", ">=9")
INGAME_REASON = "in-game model"


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def short_params(params: dict[str, Any] | None) -> str:
    """"L2 1.00 · time 1.00 · field 1.00" for an ingame_wp lineage."""
    params = params if isinstance(params, dict) else {}
    parts = []
    for key, label in (("l2", "L2"), ("time_scale", "time"), ("fp_scale", "field")):
        value = _num(params.get(key))
        parts.append(f"{label} {value:.2f}" if value is not None else f"{label} ?")
    return " · ".join(parts)


def ingame_records(conn: psycopg.Connection) -> dict[Any, dict[str, Any]]:
    """lineage_id -> {"games", "bets", "pnl_cents"} of its in-game bets (both modes),
    only for lineages that have in-game bets."""
    rows = conn.execute(
        """
        SELECT lineage_id, count(*) FILTER (WHERE ingame_n_bets > 0) AS games,
               COALESCE(SUM(ingame_n_bets), 0) AS bets, COALESCE(SUM(ingame_pnl_cents), 0) AS pnl_cents
          FROM model_scores GROUP BY lineage_id HAVING COALESCE(SUM(ingame_n_bets), 0) > 0
        """
    ).fetchall()
    return {r["lineage_id"]: {"games": int(r["games"]), "bets": int(r["bets"]), "pnl_cents": int(r["pnl_cents"])}
            for r in rows}


def lineage_ingame_record(conn: psycopg.Connection, lineage_id: Any) -> dict[str, Any] | None:
    """The in-game record of one lineage (None without in-game bets)."""
    row = conn.execute(
        """
        SELECT count(*) FILTER (WHERE ingame_n_bets > 0) AS games, COALESCE(SUM(ingame_n_bets), 0) AS bets,
               COALESCE(SUM(ingame_pnl_cents), 0) AS pnl_cents FROM model_scores WHERE lineage_id = %s
        """,
        (lineage_id,),
    ).fetchone()
    if row is None or int(row["bets"]) <= 0:
        return None
    return {"games": int(row["games"]), "bets": int(row["bets"]), "pnl_cents": int(row["pnl_cents"])}


def _group_rows(groups: Any, keys: tuple[str, ...], labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
    groups = groups if isinstance(groups, dict) else {}
    out = []
    for key in keys:
        group = groups.get(key) if isinstance(groups.get(key), dict) else {}
        ll, vll = _num(group.get("log_loss")), _num(group.get("vegas_log_loss"))
        out.append({
            "key": key, "label": (labels or {}).get(key, key), "n_plays": int(_num(group.get("n_plays")) or 0),
            "log_loss": ll, "vegas_log_loss": vll, "ll_gain": vll - ll if ll is not None and vll is not None else None,
        })
    return out


def _calibration(rows: Any) -> list[dict[str, Any]]:
    rows = rows if isinstance(rows, list) else []
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        out.append({
            "bucket": f"{i / len(rows):.1f}-{(i + 1) / len(rows):.1f}", "count": int(_num(row.get("count")) or 0),
            "mean_p": _num(row.get("mean_p")), "mean_outcome": _num(row.get("mean_outcome")),
            "vegas_mean_p": _num(row.get("vegas_mean_p")),
        })
    return out


def ingame_validation(root: dict[str, Any]) -> dict[str, Any] | None:
    """The held-out validation of an ingame_wp model as the Models pages show it, or
    None when the row is not ingame_wp or carries no validation."""
    metrics = root.get("validation_metrics")
    if not is_ingame(root) or not isinstance(metrics, dict):
        return None
    ll, vll = _num(metrics.get("log_loss")), _num(metrics.get("vegas_log_loss"))
    seasons = metrics.get("seasons") if isinstance(metrics.get("seasons"), list) else []
    return {
        "n_plays": int(_num(metrics.get("n_plays")) or 0), "min_plays": MIN_PLAYS,
        "log_loss": ll, "vegas_log_loss": vll, "ll_gain": vll - ll if ll is not None and vll is not None else None,
        "beats_baseline": metrics.get("beats_baseline") is True,
        "brier": _num(metrics.get("brier")), "vegas_brier": _num(metrics.get("vegas_brier")),
        "seasons": [s for s in seasons if isinstance(s, int)],
        "n_skipped_no_vegas": int(_num(metrics.get("n_skipped_no_vegas")) or 0),
        "by_period": _group_rows(metrics.get("by_period"), tuple(PERIOD_LABELS), PERIOD_LABELS),
        "by_score_bucket": _group_rows(metrics.get("by_score_bucket"), SCORE_BUCKETS),
        "calibration": _calibration(metrics.get("calibration")),
    }


def decorate(entry: dict[str, Any], root: dict[str, Any], record: dict[str, Any] | None) -> dict[str, Any]:
    """Add the in-game fields to a leaderboard entry or a model detail: `is_ingame`,
    `ingame` (the in-game record or None), `ingame_validation` and `ingame_reason`; an
    ingame_wp row counts as validated when its search validation is stored (its
    pre-game `validation` summary is dropped: it has no ROI to show)."""
    entry["ingame"] = record
    entry["is_ingame"] = is_ingame(root)
    entry["ingame_validation"] = ingame_validation(root)
    entry["ingame_reason"] = ingame_reason(root.get("validation_metrics")) if entry["is_ingame"] else None
    if entry["is_ingame"]:
        entry["validation"] = None
        entry["validated"] = entry["ingame_validation"] is not None
        entry["score"] = 0.0
        entry["ll_gain"] = entry["ingame_validation"]["ll_gain"] if entry["ingame_validation"] else None
    return entry


def ingame_key(entry: dict[str, Any]) -> tuple[int, float, Any]:
    """Beating the baseline first, then the log-loss gain, newest first on ties."""
    v = entry.get("ingame_validation") or {}
    gain = v.get("ll_gain")
    created = entry["created_at"]
    return (0 if v.get("beats_baseline") else 1, -(gain if gain is not None else float("-inf")),
            -created.timestamp() if hasattr(created, "timestamp") else 0)


def has_ingame_columns(entries: list[dict[str, Any]]) -> bool:
    """Whether the Models page shows the in-game column group."""
    return any(e.get("ingame") for e in entries)


__all__ = ["INGAME_FAMILY", "INGAME_REASON", "decorate", "has_ingame_columns", "ingame_key", "ingame_records",
           "ingame_validation", "lineage_ingame_record", "short_params"]
