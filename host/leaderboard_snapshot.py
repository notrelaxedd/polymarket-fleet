"""The leaderboard's snapshot replay column group and the rank basis precedence
(docs/ROBUSTNESS.md B1, docs/MODELS.md "Leaderboard").

A lineage with `snapshot_metrics` (a backtest replayed on recorded prices, stored by
host/snapshot_store.py) shows a "snapshot" group: games, bets, ROI and CLV with its
90% range. The rank basis of a lineage is, in order: paper (the existing rule: 5 paper
games and 30 paper bets, by shrunk paper CLV), snapshot (at least SNAPSHOT_RANK_BETS
snapshot-scored bets, by shrunk snapshot CLV `clv * bets / (bets + 25)`, ties by the
snapshot ROI), then validation. CLV is the frozen closing price minus the entry price,
so a positive number means the price moved the model's way after it bought.
"""
from __future__ import annotations

from typing import Any

SNAPSHOT_RANK_BETS, CLV_SHRINK = 30, 25
RANK_MODES = ("paper", "snapshot", "validation")


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int(value: Any) -> int:
    number = _num(value)
    return int(number) if number is not None else 0


def _pair(value: Any) -> list[float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        low, high = _num(value[0]), _num(value[1])
        if low is not None and high is not None:
            return [low, high]
    return None


def snapshot_clv(metrics: dict[str, Any]) -> tuple[float | None, bool]:
    """(average CLV, estimated): the metrics' `avg_clv` when reported, else the
    midpoint of the bootstrap range `ci.avg_clv` (estimated = True), else None."""
    value = _num(metrics.get("avg_clv"))
    if value is not None:
        return value, False
    ci = metrics.get("ci") if isinstance(metrics.get("ci"), dict) else {}
    pair = _pair(ci.get("avg_clv"))
    if pair is not None:
        return (pair[0] + pair[1]) / 2.0, True
    return None, False


def shrunk_snapshot_clv(bets: int, clv: float | None) -> float:
    """clv * bets / (bets + 25); 0 without a CLV."""
    if clv is None or bets <= 0:
        return 0.0
    return clv * bets / (bets + CLV_SHRINK)


def snapshot_summary(row: dict[str, Any]) -> dict[str, Any] | None:
    """The snapshot group of a lineage from a model row's `snapshot_metrics`: games,
    bets, ROI (None without bets), P&L, CLV with its 90% range, the platform, the games
    skipped for lack of recorded prices, the seasons and the shrunk CLV (`score`).
    None when the lineage has no snapshot replay."""
    metrics = row.get("snapshot_metrics")
    if not isinstance(metrics, dict):
        return None
    bets = _int(metrics.get("n_bets"))
    clv, estimated = snapshot_clv(metrics)
    ci = metrics.get("ci") if isinstance(metrics.get("ci"), dict) else {}
    return {
        "n_games": _int(metrics.get("n_games")),
        "n_bets": bets,
        "roi": _num(metrics.get("roi")) if bets else None,
        "pnl_cents": _int(metrics.get("pnl_cents")),
        "avg_clv": clv,
        "clv_estimated": estimated,
        "clv_ci": _pair(ci.get("avg_clv")),
        "roi_ci": _pair(ci.get("roi")),
        "log_loss": _num(metrics.get("log_loss")),
        "market_log_loss": _num(metrics.get("market_log_loss")),
        "platform": metrics.get("platform") if isinstance(metrics.get("platform"), str) else None,
        "n_unscored_no_prices": _int(metrics.get("n_unscored_no_prices")),
        "seasons": metrics.get("seasons") if isinstance(metrics.get("seasons"), list) else [],
        "score": shrunk_snapshot_clv(bets, clv),
    }


def snapshot_ranked(summary: dict[str, Any] | None) -> bool:
    """True once a lineage has SNAPSHOT_RANK_BETS snapshot-scored bets with a CLV."""
    return bool(summary) and summary["n_bets"] >= SNAPSHOT_RANK_BETS and summary["avg_clv"] is not None


def rank_mode(paper_is_ranked: bool, snapshot: dict[str, Any] | None) -> str:
    """The rank basis: paper > snapshot > validation."""
    if paper_is_ranked:
        return "paper"
    if snapshot_ranked(snapshot):
        return "snapshot"
    return "validation"


def snapshot_key(entry: dict[str, Any]) -> tuple[float, float, Any]:
    """Sort key of a snapshot-ranked entry: shrunk CLV, then ROI, then age."""
    snap = entry["snapshot"]
    roi = snap.get("roi")
    return (-snap["score"], -(roi if roi is not None else 0.0), entry["created_at"])
