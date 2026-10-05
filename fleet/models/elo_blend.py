"""The elo_blend family: Elo ratings blended with the closing line by a logistic fit."""

from __future__ import annotations

import random
from typing import Any, Callable

from fleet.models.base import Model
from fleet.models.blend_fit import fit_blend
from fleet.models.elo import Elo
from fleet.sim.control import check_stop
from fleet.sim.data import game_key, has_moneylines, outcome_of
from fleet.sim.odds import clamp_prob, devig, expit, logit

PARAM_KEYS = ("k", "hfa", "regress", "rest_per_day", "mov_scale", "min_edge", "kelly_fraction")
FEW_BETS = 50  # below the leaderboard's ranking gate the summary says so
MARKET_BEATEN_P = 0.05  # the market test level at which the summary says "beats the closing line"
DEFAULT_PARAMS: dict[str, Any] = {
    "k": 24.0, "hfa": 55.0, "regress": 0.33, "rest_per_day": 1.0, "mov_scale": 1,
    "min_edge": 0.03, "kelly_fraction": 0.25,
}


def _uniform(rng: random.Random, low: float, high: float) -> float:
    return round(low + (high - low) * rng.random(), 6)


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
            if outcome is not None and has_moneylines(game):
                p_elo = self.elo.expect(game)
                p_market = devig(game["home_moneyline"], game["away_moneyline"])
                if p_market is not None:
                    rows.append((logit(p_elo), logit(p_market), outcome))
            self.elo.update(game)
            last_key = key
        if season is not None and on_season is not None:
            on_season(season)
        a, b, c = fit_blend(rows)
        self.blend = {"a": a, "b": b, "c": c}
        self.through = through if through is not None else last_key

    def observe(self, game: dict[str, Any]) -> None:
        self.elo.update(game)

    # prediction ---------------------------------------------------------

    def predict(self, game: dict[str, Any], market_p: float | None, features: dict[str, Any]) -> float:
        probe = dict(game)
        probe["home_rest"] = features.get("home_rest", game.get("home_rest"))
        probe["away_rest"] = features.get("away_rest", game.get("away_rest"))
        p_elo = self.elo.expect(probe)
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


def _num(metrics: dict[str, Any], key: str) -> float:
    value = metrics.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def build_summary(params: dict[str, Any], metrics: dict[str, Any]) -> str:
    """Exactly three sentences following the template in docs/MODELS.md."""
    weight = _weight_text(metrics.get("blend") or {})
    mov = "on" if params.get("mov_scale") else "off"
    first = (f"Elo blend (K {round(float(params.get('k', 0)))}, home edge "
             f"{round(float(params.get('hfa', 0)))}, {weight}margin scaling {mov}).")
    seasons = metrics.get("seasons") or []
    if not seasons:
        span = "no seasons"
    elif seasons[0] == seasons[-1]:
        span = str(seasons[0])
    else:
        span = f"{seasons[0]}-{seasons[-1]}"
    n_bets = int(_num(metrics, "n_bets"))
    if n_bets == 0:
        min_edge = 100 * float(params.get("min_edge", 0.0))
        second = (f"Across {span} it never found an edge above its {min_edge:.1f}% minimum after fees, "
                  "so it placed no bets.")
    else:
        note = " (too few bets to judge)" if n_bets < FEW_BETS else ""
        drawdown = metrics.get("max_drawdown")
        dd_text = "an unknown" if drawdown is None else f"a {100 * _num(metrics, 'max_drawdown'):.0f}%"
        second = (f"Across {span} it placed {n_bets} bets at an average edge of "
                  f"{100 * _num(metrics, 'avg_edge'):.1f}% and returned {100 * _num(metrics, 'roi'):+.1f}% on stake "
                  f"with {dd_text} max drawdown{note}.")
    ll, mll = _num(metrics, "log_loss"), _num(metrics, "market_log_loss")
    market_p = metrics.get("market_p")
    market_p = float(market_p) if isinstance(market_p, (int, float)) and not isinstance(market_p, bool) else None
    if metrics.get("n_games", 0) and ll < mll - 0.002 and (market_p is None or market_p < MARKET_BEATEN_P):
        verdict = "it beats the closing line on calibration, but treat the edge as unproven"
    elif metrics.get("n_games", 0) and ll < mll - 0.002:
        verdict = (f"it is ahead of the closing line but not significantly (market test p {market_p:.2f}), "
                   "so treat the edge as unproven")
    else:
        verdict = "it leans on the market and adds little, so treat the edge as unproven"
    third = (f"Log-loss {ll:.3f} against the market's {mll:.3f}; {verdict} until paper trading "
             f"shows positive CLV.")
    return f"{first} {second} {third}"
