"""Validation metrics of an ingame_wp model on play-by-play rows (contract section 4).

Every metric is computed on the same plays: rows with a home_win (1, 0.5 or 0) and a
vegas_wp, so the model and the vegas_wp baseline are compared like for like; rows
without a vegas_wp are counted in n_skipped_no_vegas. Ties count 0.5 in the log-loss.

validation = {"n_plays", "log_loss", "vegas_log_loss", "beats_baseline" (log_loss <=
vegas_log_loss, false without plays), "brier", "vegas_brier", "seasons", "n_skipped_no_vegas",
"by_period": {"1".."4", "5" (overtime)}: {"n_plays", "log_loss", "vegas_log_loss"},
"by_score_bucket": {"<=-9", "-8..-1", "0", "1..8", ">=9"} (home minus away before the
play): same shape, "calibration": ten buckets of the model's p, each {"count", "mean_p",
"mean_outcome", "vegas_mean_p"}}.
"""

from __future__ import annotations

from typing import Any

from fleet.models.ingame_wp import IngameWP, state_from_row
from fleet.sim.metrics import log_loss

N_BUCKETS = 10
SCORE_BUCKETS = ("<=-9", "-8..-1", "0", "1..8", ">=9")
PERIOD_KEYS = ("1", "2", "3", "4", "5")


def score_bucket(diff: int) -> str:
    if diff <= -9:
        return "<=-9"
    if diff < 0:
        return "-8..-1"
    if diff == 0:
        return "0"
    if diff <= 8:
        return "1..8"
    return ">=9"


def _group() -> list[float]:
    return [0, 0.0, 0.0]


class Accumulator:
    """Sufficient statistics of a validation pass, filled one row at a time."""

    def __init__(self) -> None:
        self.n = 0
        self.skipped = 0
        self.sum_ll = 0.0
        self.sum_vll = 0.0
        self.sum_brier = 0.0
        self.sum_vbrier = 0.0
        self.seasons: set[int] = set()
        self.by_period = {k: _group() for k in PERIOD_KEYS}
        self.by_score = {k: _group() for k in SCORE_BUCKETS}
        self.calibration = [[0, 0.0, 0.0, 0.0] for _ in range(N_BUCKETS)]

    def add_row(self, model: IngameWP, row: dict[str, Any]) -> None:
        """Score one pbp row (skipped without an outcome; counted as skipped without vegas_wp)."""
        y = row.get("home_win")
        if y is None:
            return
        vegas = row.get("vegas_wp")
        if vegas is None:
            self.skipped += 1
            return
        state = state_from_row(row)
        p = model.predict(state, row.get("pregame_p_home"))
        self.add(p, float(vegas), float(y), str(min(int(state["period"]), 5)),
                 int(row.get("score_diff") or 0), row.get("season"))

    def add(self, p: float, vegas: float, y: float, period: str, diff: int, season: Any = None) -> None:
        ll, vll = log_loss(p, y), log_loss(vegas, y)
        self.n += 1
        self.sum_ll += ll
        self.sum_vll += vll
        self.sum_brier += (p - y) ** 2
        self.sum_vbrier += (vegas - y) ** 2
        if isinstance(season, int) and not isinstance(season, bool):
            self.seasons.add(season)
        for group in (self.by_period[period], self.by_score[score_bucket(diff)]):
            group[0] += 1
            group[1] += ll
            group[2] += vll
        bucket = self.calibration[min(int(p * N_BUCKETS), N_BUCKETS - 1)]
        bucket[0] += 1
        bucket[1] += p
        bucket[2] += y
        bucket[3] += vegas

    def metrics(self) -> dict[str, Any]:
        n = self.n
        ll = self.sum_ll / n if n else None
        vll = self.sum_vll / n if n else None
        return {
            "n_plays": n,
            "log_loss": ll,
            "vegas_log_loss": vll,
            "beats_baseline": bool(n and ll is not None and vll is not None and ll <= vll),
            "brier": self.sum_brier / n if n else None,
            "vegas_brier": self.sum_vbrier / n if n else None,
            "seasons": sorted(self.seasons),
            "n_skipped_no_vegas": self.skipped,
            "by_period": {k: _group_metrics(g) for k, g in self.by_period.items()},
            "by_score_bucket": {k: _group_metrics(g) for k, g in self.by_score.items()},
            "calibration": [
                {"count": c, "mean_p": sp / c if c else 0.0, "mean_outcome": sy / c if c else 0.0,
                 "vegas_mean_p": sv / c if c else 0.0}
                for c, sp, sy, sv in self.calibration
            ],
        }


def _group_metrics(group: list[float]) -> dict[str, Any]:
    n = int(group[0])
    return {"n_plays": n, "log_loss": group[1] / n if n else None, "vegas_log_loss": group[2] / n if n else None}


def validate_model(model: IngameWP, rows: Any) -> dict[str, Any]:
    """The validation dict of `model` over an iterable of pbp rows (the caller filters
    the seasons)."""
    acc = Accumulator()
    for row in rows:
        acc.add_row(model, row)
    return acc.metrics()
