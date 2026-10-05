"""The elo_blend family: Elo ratings blended with the closing line by a logistic fit.

The signal penalties (docs/ROBUSTNESS.md B2) move the pre-game Elo edge: a team whose
starting quarterback changed loses qb_change_penalty points and each player it lists
Out costs out_penalty_per_player points. The same shift enters the expectation the
blend is fitted on, the prediction and the rating update, so fit and predict agree;
both default to 0, which leaves every earlier model's predictions unchanged.
"""

from __future__ import annotations

import random
from typing import Any, Callable

from fleet.models.base import Model
from fleet.models.blend_fit import fit_blend
from fleet.models.elo import Elo
from fleet.models.summary_text import FEW_BETS, MARKET_BEATEN_P, bets_sentence, calibration_sentence
from fleet.sim.control import check_stop
from fleet.sim.data import game_key, has_moneylines, outcome_of
from fleet.sim.odds import clamp_prob, devig, expit, logit
from fleet.sim.signals import normalise_signals

PARAM_KEYS = ("k", "hfa", "regress", "rest_per_day", "mov_scale", "min_edge", "kelly_fraction",
              "qb_change_penalty", "out_penalty_per_player")
DEFAULT_PARAMS: dict[str, Any] = {
    "k": 24.0, "hfa": 55.0, "regress": 0.33, "rest_per_day": 1.0, "mov_scale": 1,
    "min_edge": 0.03, "kelly_fraction": 0.25,
}
# The signal penalties default to 0 but are read with .get instead of being merged into
# a model's params: an existing model's params dict (and so the order of the draws that
# perturb it in the neighbourhood stress test) stays exactly what it was.
SIGNAL_DEFAULTS: dict[str, float] = {"qb_change_penalty": 0.0, "out_penalty_per_player": 0.0}
__all__ = ["EloBlend", "FEW_BETS", "SIGNAL_DEFAULTS", "build_summary", "signal_adjustment"]


def _uniform(rng: random.Random, low: float, high: float) -> float:
    return round(low + (high - low) * rng.random(), 6)


def signal_adjustment(params: dict[str, Any], signals: Any) -> float:
    """Elo points added to the home side's pre-game edge by the signal penalties."""
    qb = float(params.get("qb_change_penalty") or SIGNAL_DEFAULTS["qb_change_penalty"])
    out = float(params.get("out_penalty_per_player") or SIGNAL_DEFAULTS["out_penalty_per_player"])
    if qb == 0.0 and out == 0.0:
        return 0.0
    s = normalise_signals(signals)
    home = qb * s["home_qb_changed"] + out * s["home_out_count"]
    away = qb * s["away_qb_changed"] + out * s["away_out_count"]
    return away - home


class EloBlend(Model):
    family = "elo_blend"
    PARAM_KEYS = PARAM_KEYS

    def __init__(self, params: dict[str, Any]) -> None:
        merged = dict(DEFAULT_PARAMS)
        merged.update(params or {})
        super().__init__(merged)
        p = self.params
        self.elo = Elo(p["k"], p["hfa"], p["regress"], p["rest_per_day"], p["mov_scale"])
        self.blend: dict[str, float] = {"a": 0.0, "b": 1.0, "c": 0.0}
        self.through: tuple[int, int] | None = None

    # fitting ------------------------------------------------------------

    def fit(self, games: list[dict[str, Any]], through: tuple[int, int] | None,
            should_stop: Callable[[], bool],
            on_season: Callable[[int], None] | None = None) -> None:
        """Replay Elo over games up to `through` (inclusive) and fit the blend on the
        finished moneyline games up to and including it; `through` None means every game."""
        p = self.params
        self.elo = Elo(p["k"], p["hfa"], p["regress"], p["rest_per_day"], p["mov_scale"])
        rows: list[tuple[float, float, float]] = []
        last_key: tuple[int, int] | None = None
        season: int | None = None
        for game in games:
            key = game_key(game)
            if through is not None and key > through:
                continue
            if game["season"] != season:
                if season is not None and on_season is not None:
                    on_season(season)
                check_stop(should_stop)
                season = game["season"]
            outcome = outcome_of(game)
            extra = signal_adjustment(p, game.get("signals"))
            if outcome is not None and has_moneylines(game):
                p_elo = self.elo.expect(game, extra)
                p_market = devig(game["home_moneyline"], game["away_moneyline"])
                if p_market is not None:
                    rows.append((logit(p_elo), logit(p_market), outcome))
            self.elo.update(game, extra)
            last_key = key
        if season is not None and on_season is not None:
            on_season(season)
        a, b, c = fit_blend(rows)
        self.blend = {"a": a, "b": b, "c": c}
        self.through = through if through is not None else last_key

    def observe(self, game: dict[str, Any]) -> None:
        self.elo.update(game, signal_adjustment(self.params, game.get("signals")))

    # prediction ---------------------------------------------------------

    def predict(self, game: dict[str, Any], market_p: float | None, features: dict[str, Any]) -> float:
        probe = dict(game)
        probe["home_rest"] = features.get("home_rest", game.get("home_rest"))
        probe["away_rest"] = features.get("away_rest", game.get("away_rest"))
        signals = features["signals"] if "signals" in features else game.get("signals")
        p_elo = self.elo.expect(probe, signal_adjustment(self.params, signals))
        if market_p is None:
            return clamp_prob(p_elo)
        z = self.blend["a"] * logit(p_elo) + self.blend["b"] * logit(market_p) + self.blend["c"]
        return clamp_prob(expit(z))

    # artifacts ----------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """The artifact: ratings, blend, through, the season the ratings sit in (so the
        between-season regression is applied exactly once after a reload) and games_seen."""
        return {
            "ratings": {t: float(r) for t, r in sorted(self.elo.ratings.items())},
            "blend": {k: float(v) for k, v in self.blend.items()},
            "through": list(self.through) if self.through is not None else None,
            "season": self.elo.season,
            "games_seen": self.elo.games_seen,
        }

    @classmethod
    def from_json(cls, params: dict[str, Any], artifact: dict[str, Any]) -> "EloBlend":
        model = cls(params)
        through = artifact.get("through")
        model.through = (int(through[0]), int(through[1])) if through else None
        season = artifact.get("season")
        if season is None and model.through is not None:
            season = model.through[0]
        model.elo.load_state(artifact.get("ratings") or {}, None if season is None else int(season),
                             artifact.get("games_seen") or 0)
        blend = artifact.get("blend") or {}
        model.blend = {k: float(blend.get(k, model.blend[k])) for k in ("a", "b", "c")}
        return model

    # search and summary ---------------------------------------------------

    @staticmethod
    def search_space(rng: random.Random) -> dict[str, Any]:
        return {
            "k": _uniform(rng, 10, 40),
            "hfa": _uniform(rng, 20, 90),
            "regress": _uniform(rng, 0.1, 0.6),
            "rest_per_day": _uniform(rng, 0, 4),
            "mov_scale": 1 if rng.random() < 0.5 else 0,
            "min_edge": _uniform(rng, 0.01, 0.08),
            "kelly_fraction": _uniform(rng, 0.1, 0.5),
            # Appended after the original draws so a seed reproduces the earlier params.
            "qb_change_penalty": _uniform(rng, 0, 80),
            "out_penalty_per_player": _uniform(rng, 0, 15),
        }

    @staticmethod
    def summary(params: dict[str, Any], metrics: dict[str, Any]) -> str:
        return build_summary(params, metrics)


def _weight_text(blend: dict[str, Any]) -> str:
    """"71% weight on the closing line, " from the fitted a and b; nothing without a fit."""
    a, b = float(blend.get("a", 0.0)), float(blend.get("b", 0.0))
    if a <= 0 < b:
        return "all weight on the closing line, Elo adds nothing, "
    if a + b <= 0:
        return ""
    return f"{round(100 * b / (a + b))}% weight on the closing line, "


def _penalty_text(params: dict[str, Any]) -> str:
    """", QB change costs 40 points, 5 points per player Out" for the nonzero penalties."""
    qb = round(float(params.get("qb_change_penalty") or 0.0))
    out = float(params.get("out_penalty_per_player") or 0.0)
    parts = []
    if qb > 0:
        parts.append(f"QB change costs {qb} points")
    if round(out, 1) > 0:
        parts.append(f"{out:.1f} points per player Out")
    return "".join(f", {part}" for part in parts)


def build_summary(params: dict[str, Any], metrics: dict[str, Any]) -> str:
    """Exactly three sentences following the template in docs/MODELS.md."""
    weight = _weight_text(metrics.get("blend") or {})
    mov = "on" if params.get("mov_scale") else "off"
    first = (f"Elo blend (K {round(float(params.get('k', 0)))}, home edge "
             f"{round(float(params.get('hfa', 0)))}, {weight}margin scaling {mov}{_penalty_text(params)}).")
    return f"{first} {bets_sentence(params, metrics)} {calibration_sentence(metrics)}"
