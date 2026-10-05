"""The epa_blend family (docs/ROBUSTNESS.md B2; docs/MODELS.md, "epa_blend").

A logistic regression of the home result on the features of fleet.models.epa_features
(Elo, shrunk rolling offensive and defensive EPA, rest, quarterback change, players
Out, divisional, market logit), fitted by Newton's method with an L2 penalty on every
coefficient except the intercept and the market logit. Elo runs with fixed constants
(ELO_*); the searched params are window, shrink, l2 and the betting params.
"""

from __future__ import annotations

import random
from typing import Any, Callable

from fleet.models.base import Model
from fleet.models.elo import Elo
from fleet.models.epa_features import FEATURE_NAMES, MARKET_INDEX, UNPENALISED, LeagueMean, feature_vector
from fleet.models.newton import fit_logistic
from fleet.models.summary_text import bets_sentence, calibration_sentence
from fleet.sim.control import check_stop
from fleet.sim.data import features_of, game_key, has_moneylines, outcome_of
from fleet.sim.odds import clamp_prob, devig, expit

PARAM_KEYS = ("window", "shrink", "l2", "min_edge", "kelly_fraction")
DEFAULT_PARAMS: dict[str, Any] = {"window": 8, "shrink": 3.0, "l2": 1.0, "min_edge": 0.03, "kelly_fraction": 0.25}
ELO_K, ELO_HFA, ELO_REGRESS, ELO_MOV = 20.0, 55.0, 0.33, 1
MAX_ITER = 50
TOL = 1e-9
# A typical home-minus-away gap per signal, used to say which signals moved the price:
# 100 Elo points, 0.1 EPA per play, 3 days of rest, one quarterback change, 3 players
# Out, a divisional game.
TYPICAL_GAP = {"elo": 0.25, "off_epa": 0.1, "def_epa": 0.1, "rest": 1.0, "qb_change": 1.0, "outs": 3.0,
               "divisional": 1.0}
LABELS = {"elo": "Elo", "off_epa": "offensive EPA", "def_epa": "defensive EPA", "rest": "rest",
          "qb_change": "quarterback change", "outs": "players Out", "divisional": "divisional games"}
CARRY_MIN = 0.03  # log-odds a typical gap must move to count as carrying the result
CARRY_TOP = 3


def start_coefficients() -> list[float]:
    """Zeros with the market logit at 1: the closing line alone."""
    start = [0.0] * len(FEATURE_NAMES)
    start[MARKET_INDEX] = 1.0
    return start


def _new_elo() -> Elo:
    return Elo(ELO_K, ELO_HFA, ELO_REGRESS, 0.0, ELO_MOV)


class EpaBlend(Model):
    family = "epa_blend"
    PARAM_KEYS = PARAM_KEYS
    SEARCH_BOUNDS = {"window": (4.0, 12.0), "shrink": (0.0, 8.0), "l2": (0.01, 10.0),
                     "min_edge": (0.01, 0.08), "kelly_fraction": (0.1, 0.5)}

    def __init__(self, params: dict[str, Any]) -> None:
        merged = dict(DEFAULT_PARAMS)
        merged.update(params or {})
        super().__init__(merged)
        self.window = max(1, int(round(float(self.params["window"]))))
        self.shrink = max(0.0, float(self.params["shrink"]))
        self.l2 = max(0.0, float(self.params["l2"]))
        self.elo = _new_elo()
        self.league = LeagueMean()
        self.coef = start_coefficients()
        self.blend: dict[str, float] = dict(zip(FEATURE_NAMES, self.coef))
        self.through: tuple[int, int] | None = None
        self.n_train = 0

    # features -------------------------------------------------------------

    def _features(self, game: dict[str, Any], features: dict[str, Any] | None) -> dict[str, Any]:
        merged = features_of(game)
        merged.update(features or {})
        return merged

    def _vector(self, game: dict[str, Any], features: dict[str, Any], p_market: float) -> list[float]:
        self.elo.begin_season(game["season"])
        diff = self.elo.rating(game["home_team"]) - self.elo.rating(game["away_team"])
        return feature_vector(game, features, diff, self.window, self.shrink, self.league.mean(game["season"]), p_market)

    def _learn(self, game: dict[str, Any], features: dict[str, Any]) -> None:
        self.league.absorb(features.get("team_stats") or {})
        self.elo.update({"home_rest": None, "away_rest": None, **game})

    # fitting ----------------------------------------------------------------

    def fit(self, games: list[dict[str, Any]], through: tuple[int, int] | None,
            should_stop: Callable[[], bool],
            on_season: Callable[[int], None] | None = None) -> None:
        """Replay games up to `through` (inclusive; None = all), building each finished
        moneyline game's feature row before that game is learned from, then fit."""
        self.elo = _new_elo()
        self.league = LeagueMean()
        xs: list[list[float]] = []
        ys: list[float] = []
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
            features = features_of(game)
            outcome = outcome_of(game)
            if outcome is not None and has_moneylines(game):
                p_market = devig(game["home_moneyline"], game["away_moneyline"])
                if p_market is not None:
                    xs.append(self._vector(game, features, p_market))
                    ys.append(outcome)
            self._learn(game, features)
            last_key = key
        if season is not None and on_season is not None:
            on_season(season)
        self.coef = fit_logistic(xs, ys, l2=self.l2, start=start_coefficients(), unpenalised=UNPENALISED,
                                 max_iter=MAX_ITER, tol=TOL)
        self.blend = dict(zip(FEATURE_NAMES, self.coef))
        self.n_train = len(xs)
        self.through = through if through is not None else last_key

    def observe(self, game: dict[str, Any]) -> None:
        self._learn(game, features_of(game))

    # prediction -------------------------------------------------------------

    def predict(self, game: dict[str, Any], market_p: float | None, features: dict[str, Any]) -> float:
        """The fitted logistic; without a market price, the Elo expectation with its home edge."""
        if market_p is None:
            return clamp_prob(self.elo.expect({**game, "home_rest": None, "away_rest": None}))
        x = self._vector(game, self._features(game, features), market_p)
        return clamp_prob(expit(sum(w * v for w, v in zip(self.coef, x))))

    # artifacts --------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "features": list(FEATURE_NAMES),
            "coef": [float(w) for w in self.coef],
            "ratings": {t: float(r) for t, r in sorted(self.elo.ratings.items())},
            "season": self.elo.season,
            "games_seen": self.elo.games_seen,
            "league": self.league.to_json(),
            "n_train": self.n_train,
            "through": list(self.through) if self.through is not None else None,
        }

    @classmethod
    def from_json(cls, params: dict[str, Any], artifact: dict[str, Any]) -> "EpaBlend":
        model = cls(params)
        through = artifact.get("through")
        model.through = (int(through[0]), int(through[1])) if through else None
        season = artifact.get("season")
        if season is None and model.through is not None:
            season = model.through[0]
        model.elo.load_state(artifact.get("ratings") or {}, None if season is None else int(season),
                             artifact.get("games_seen") or 0)
        model.league = LeagueMean.from_json(artifact.get("league"))
        named = dict(zip(artifact.get("features") or FEATURE_NAMES, artifact.get("coef") or []))
        model.coef = [float(named.get(name, model.coef[i])) for i, name in enumerate(FEATURE_NAMES)]
        model.blend = dict(zip(FEATURE_NAMES, model.coef))
        model.n_train = int(artifact.get("n_train") or 0)
        return model

    # search and summary -----------------------------------------------------

    @staticmethod
    def search_space(rng: random.Random) -> dict[str, Any]:
        return {
            "window": rng.randint(4, 12),
            "shrink": round(8.0 * rng.random(), 6),
            "l2": round(10.0 ** (-2.0 + 3.0 * rng.random()), 6),
            "min_edge": round(0.01 + 0.07 * rng.random(), 6),
            "kelly_fraction": round(0.1 + 0.4 * rng.random(), 6),
        }

    @staticmethod
    def summary(params: dict[str, Any], metrics: dict[str, Any]) -> str:
        return build_summary(params, metrics)


def carried_signals(blend: dict[str, Any]) -> list[tuple[str, float]]:
    """(signal, log-odds moved by a typical gap) of the signals at or above CARRY_MIN,
    largest first, at most CARRY_TOP."""
    moved = []
    for name, gap in TYPICAL_GAP.items():
        value = blend.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            impact = abs(float(value)) * gap
            if impact >= CARRY_MIN:
                moved.append((name, impact))
    moved.sort(key=lambda item: (-item[1], item[0]))
    return moved[:CARRY_TOP]


def _carried_text(blend: dict[str, Any]) -> str:
    if not any(name in blend for name in TYPICAL_GAP):
        return "no coefficients were fitted, so no signal carried it"
    carried = carried_signals(blend)
    if not carried:
        return f"no signal moved the price by {CARRY_MIN:.2f} in log-odds, so it is close to the closing line alone"
    parts = [f"{LABELS[name]} ({impact:.2f})" for name, impact in carried]
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"the signals that carried it were {listed} in log-odds per typical gap"


def build_summary(params: dict[str, Any], metrics: dict[str, Any]) -> str:
    """Exactly three sentences: what it is and which signals carried it, how it bet,
    how it is calibrated against the market."""
    blend = metrics.get("blend") if isinstance(metrics.get("blend"), dict) else {}
    p = {**DEFAULT_PARAMS, **(params or {})}
    market = blend.get("market")
    market_text = f", market weight {float(market):.2f}" if isinstance(market, (int, float)) else ""
    first = (f"EPA blend ({int(round(float(p['window'])))}-game window, shrink {float(p['shrink']):.1f}, "
             f"L2 {float(p['l2']):.2f}{market_text}); {_carried_text(blend)}.")
    return f"{first} {bets_sentence(p, metrics)} {calibration_sentence(metrics)}"
