"""Eligibility of ingame_wp lineages (docs/INGAME.md, contract section 6).

An in-game model has no moneyline backtest to judge: its root row carries the
validation of its search on held-out play-by-play seasons (`validation_metrics` with
`n_plays`, `log_loss`, `vegas_log_loss` and `beats_baseline`). The lineage is paper_ok
when that validation beats the vegas_wp baseline (`beats_baseline` true) over at least
MIN_PLAYS plays, otherwise candidate. In-game orders are paper-only in this step, so an
ingame_wp lineage is never live_eligible: the paper gate never promotes it, and a
lineage found live_eligible is brought back to paper_ok (or candidate). A retired
lineage stays retired.
"""
from __future__ import annotations

from typing import Any

import psycopg

INGAME_FAMILY = "ingame_wp"
MIN_PLAYS = 10000


def is_ingame(row: dict[str, Any] | None) -> bool:
    """True for a model row of the in-game family."""
    return isinstance(row, dict) and row.get("family") == INGAME_FAMILY


def is_ingame_lineage(conn: psycopg.Connection, lineage_id: Any) -> bool:
    row = conn.execute("SELECT family FROM models WHERE id = %s", (lineage_id,)).fetchone()
    return is_ingame(row)


def n_plays(metrics: dict[str, Any] | None) -> int | None:
    value = metrics.get("n_plays") if isinstance(metrics, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def meets_ingame(metrics: dict[str, Any] | None) -> bool:
    """The validation beats the vegas_wp baseline over at least MIN_PLAYS plays."""
    if not isinstance(metrics, dict) or metrics.get("beats_baseline") is not True:
        return False
    plays = n_plays(metrics)
    return plays is not None and plays >= MIN_PLAYS


def ingame_status(current: str, metrics: dict[str, Any] | None) -> str:
    """retired stays retired; paper_ok when meets_ingame, else candidate (never live_eligible)."""
    if current == "retired":
        return "retired"
    return "paper_ok" if meets_ingame(metrics) else "candidate"


def ingame_reason(metrics: dict[str, Any] | None) -> str:
    """One line on why the lineage has its status, for the Models pages."""
    if not isinstance(metrics, dict):
        return "not validated: run an ingame_wp model search"
    plays = n_plays(metrics) or 0
    if metrics.get("beats_baseline") is not True:
        return "its validation log-loss is worse than the Vegas WP baseline"
    if plays < MIN_PLAYS:
        return f"validated on {plays} plays, fewer than {MIN_PLAYS}"
    return f"beats Vegas WP over {plays} held-out plays; paper only"
