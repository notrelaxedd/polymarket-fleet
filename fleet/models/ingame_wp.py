"""The ingame_wp family: in-game home win probability from the game state
(docs/INGAME.md, "In-game model family"; contract section 4).

A logistic regression on engineered features of the state dict
{"status", "period", "clock_seconds", "home_score", "away_score", "possession", "down",
"distance", "yardline_100", "home_timeouts", "away_timeouts"} and the pre-game home
probability, fitted by Newton (fleet.models.newton.fit_logistic, intercept unpenalised)
on nflverse play-by-play rows. Training rows go through state_from_row first, so the
model sees exactly the representation the live feed hands it; kickoffs included, which
host/pbp_rows stores as the live feed shows them (the kicking team in possession at
its own 35, yardline_100 65), not as nflverse does (the receiver at 35).

Features (FEATURE_NAMES order), with s = game seconds left (3600 at kickoff, 0 at the end
of regulation, the overtime clock in overtime), t = s / 3600 and sign = +1 when the home
team has the ball, -1 for the away team, 0 for nobody:
  const               1.0
  score_time          score_diff / (t + 0.01) ** (0.5 * time_scale)
  pregame_logit       logit(pregame_p)
  pregame_logit_time  logit(pregame_p) * t
  field_position      sign * (100 - yardline_100) / 100 * fp_scale
  late_down           sign * [down is 3 or 4]
  distance            sign * min(distance, 20) / 10
  timeouts            (home_timeouts - away_timeouts) / 3
  end_half1           sign * [last 120 s of the first half]
  end_game            sign * [last 120 s of the second half or of overtime]
A missing pre-game probability counts as 0.5, missing fields as 0 terms.

time_scale is an exponent (1 = the classic 1/sqrt(t) sharpening, 2 = 1/t), fp_scale a
factor. The L2 penalty is per 1000 plays: the fit uses l2 * n_plays / 1000, so the
searched range [0.01, 10] means the same shrinkage on a season and on a decade.
"""

from __future__ import annotations

import math
import random
from typing import Any, Callable, Iterable

from fleet.models.base import Model
from fleet.models.newton import fit_logistic
from fleet.sim.control import check_stop
from fleet.sim.odds import expit, logit

FAMILY = "ingame_wp"
PARAM_KEYS = ("l2", "time_scale", "fp_scale")
DEFAULT_PARAMS: dict[str, Any] = {"l2": 1.0, "time_scale": 1.0, "fp_scale": 1.0}
FEATURE_NAMES = ("const", "score_time", "pregame_logit", "pregame_logit_time", "field_position",
                 "late_down", "distance", "timeouts", "end_half1", "end_game")
P_MIN, P_MAX = 0.001, 0.999
GAME_SECONDS = 3600
HALF_SECONDS = 1800
QUARTER_SECONDS = 900
END_WINDOW = 120
MAX_DISTANCE = 20
STOP_EVERY = 20000
L2_PER_PLAYS = 1000
L2_RANGE = (0.01, 10.0)
SCALE_RANGE = (0.5, 2.0)


# state <-> row --------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value: Any) -> int | None:
    v = _num(value)
    return None if v is None else int(v)


def game_clock(state: dict[str, Any]) -> tuple[int, int]:
    """(seconds_remaining, half) of a state: regulation seconds left (3600..0) with half
    1 or 2, or the overtime clock with half 3 from period 5 on."""
    status = state.get("status")
    period = _int(state.get("period")) or 1
    clock = _int(state.get("clock_seconds"))
    if status == "pre":
        return GAME_SECONDS, 1
    if status == "final":
        return 0, 3 if period >= 5 else 2
    if period >= 5:
        return max(0, clock if clock is not None else 0), 3
    period = max(1, period)
    clock = QUARTER_SECONDS if clock is None else min(max(clock, 0), QUARTER_SECONDS)
    return (4 - period) * QUARTER_SECONDS + clock, 1 if period <= 2 else 2


def state_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """The state dict of a pbp_rows row (status "in"). Periods: half 1 maps to 1-2,
    half 2 to 3-4, half 3 (overtime) to 5 with the overtime clock; clock_seconds is the
    remainder of the period, so game_clock(state_from_row(row)) gives back the row's
    seconds_remaining at quarter boundaries too. The scores carry only the difference."""
    s = max(0, _int(row.get("seconds_remaining")) or 0)
    half = _int(row.get("half")) or (1 if s > HALF_SECONDS else 2)
    if half >= 3:
        period, clock = 5, s
    else:
        s = min(s, GAME_SECONDS)
        low, high = (1, 2) if half == 1 else (3, 4)
        period = min(max(1 + (GAME_SECONDS - s) // QUARTER_SECONDS, low), high)
        clock = s - (4 - period) * QUARTER_SECONDS
    diff = _int(row.get("score_diff")) or 0
    home = row.get("posteam_is_home")
    return {
        "status": "in", "period": period, "clock_seconds": clock,
        "home_score": max(diff, 0), "away_score": max(-diff, 0),
        "possession": None if home is None else ("home" if home else "away"),
        "down": _int(row.get("down")), "distance": _int(row.get("ydstogo")),
        "yardline_100": _int(row.get("yardline_100")),
        "home_timeouts": _int(row.get("home_timeouts")), "away_timeouts": _int(row.get("away_timeouts")),
    }


def raw_features(state: dict[str, Any], pregame_p: float | None) -> list[float]:
    """The unscaled features of a state plus one trailing element: FEATURE_NAMES order with
    the plain score difference in the score_time column and fp_scale 1, then
    t + 0.01 (the time base of score_time). finish_features turns it into the vector."""
    s, half = game_clock(state)
    t = min(s, GAME_SECONDS) / GAME_SECONDS
    diff = (_num(state.get("home_score")) or 0.0) - (_num(state.get("away_score")) or 0.0)
    p0 = _num(pregame_p)
    lp = logit(p0) if p0 is not None and 0.0 < p0 < 1.0 else 0.0
    possession = state.get("possession")
    sign = 1.0 if possession == "home" else -1.0 if possession == "away" else 0.0
    yardline = _num(state.get("yardline_100"))
    field = sign * (100.0 - yardline) / 100.0 if yardline is not None else 0.0
    down = _int(state.get("down"))
    distance = _num(state.get("distance"))
    late_down = sign if down in (3, 4) else 0.0
    dist = sign * min(max(distance, 0.0), MAX_DISTANCE) / 10.0 if distance is not None and down else 0.0
    ht, at = _num(state.get("home_timeouts")), _num(state.get("away_timeouts"))
    timeouts = (ht - at) / 3.0 if ht is not None and at is not None else 0.0
    end_half1 = sign if half == 1 and HALF_SECONDS < s <= HALF_SECONDS + END_WINDOW else 0.0
    end_game = sign if half >= 2 and s <= END_WINDOW else 0.0
    return [1.0, diff, lp, lp * t, field, late_down, dist, timeouts, end_half1, end_game, t + 0.01]


SCORE_TIME_INDEX = FEATURE_NAMES.index("score_time")
FIELD_INDEX = FEATURE_NAMES.index("field_position")
K = len(FEATURE_NAMES)


def finish_features(raw: list[float] | Any, time_scale: float, fp_scale: float) -> list[float]:
    """The feature vector (FEATURE_NAMES order) of raw_features(...) under the two scales:
    score_time = diff / (t + 0.01) ** (0.5 * time_scale), field_position * fp_scale.
    The search builds the raw rows once and finishes them per candidate."""
    out = [float(v) for v in raw[:K]]
    out[SCORE_TIME_INDEX] = out[SCORE_TIME_INDEX] / float(raw[K]) ** (0.5 * time_scale)
    out[FIELD_INDEX] *= fp_scale
    return out


def feature_vector(state: dict[str, Any], pregame_p: float | None, time_scale: float = 1.0,
                   fp_scale: float = 1.0) -> list[float]:
    """The features of a state in FEATURE_NAMES order."""
    return finish_features(raw_features(state, pregame_p), time_scale, fp_scale)


def effective_l2(l2: float, n_plays: int) -> float:
    """The penalty the fit uses: l2 per 1000 plays."""
    return float(l2) * n_plays / L2_PER_PLAYS


def _log_uniform(rng: random.Random, low: float, high: float) -> float:
    return round(math.exp(math.log(low) + (math.log(high) - math.log(low)) * rng.random()), 6)


def _uniform(rng: random.Random, low: float, high: float) -> float:
    return round(low + (high - low) * rng.random(), 6)


# the model ------------------------------------------------------------------------


class IngameWP(Model):
    family = FAMILY
    PARAM_KEYS = PARAM_KEYS
    SEARCH_BOUNDS = {"l2": L2_RANGE, "time_scale": SCALE_RANGE, "fp_scale": SCALE_RANGE}

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        merged = dict(DEFAULT_PARAMS)
        merged.update(params or {})
        super().__init__(merged)
        self.coef: list[float] = [0.0] * len(FEATURE_NAMES)
        self.n_train = 0
        self.train_seasons: list[int] = []

    def features(self, state: dict[str, Any], pregame_p: float | None) -> list[float]:
        return feature_vector(state, pregame_p, float(self.params["time_scale"]), float(self.params["fp_scale"]))

    def row_features(self, row: dict[str, Any]) -> list[float]:
        """The features of a pbp_rows row (through state_from_row)."""
        return self.features(state_from_row(row), row.get("pregame_p_home"))

    def fit(self, rows: Iterable[dict[str, Any]], through: Any = None,  # type: ignore[override]
            should_stop: Callable[[], bool] | None = None,
            on_season: Callable[[int], None] | None = None) -> None:
        """Fit on every row with a home_win (1, 0.5 or 0); `through` and `on_season` are
        accepted for the Model interface and unused."""
        xs: list[list[float]] = []
        ys: list[float] = []
        seasons: set[int] = set()
        for i, row in enumerate(rows):
            if should_stop is not None and i % STOP_EVERY == 0:
                check_stop(should_stop)
            y = _num(row.get("home_win"))
            if y is None:
                continue
            xs.append(self.row_features(row))
            ys.append(y)
            season = _int(row.get("season"))
            if season is not None:
                seasons.add(season)
        self.fit_matrix(xs, ys)
        self.train_seasons = sorted(seasons)

    def fit_matrix(self, xs: list[list[float]], ys: list[float]) -> None:
        """Fit on prebuilt feature rows (the search builds them once per candidate); the
        penalty is params l2 per 1000 rows."""
        l2 = effective_l2(float(self.params["l2"]), len(xs))
        self.coef = [float(w) for w in fit_logistic(xs, ys, l2=l2, unpenalised=[0])] if xs else [0.0] * K
        self.n_train = len(xs)

    def predict_features(self, x: list[float]) -> float:
        z = sum(w * v for w, v in zip(self.coef, x))
        return min(max(expit(z), P_MIN), P_MAX)

    def predict(self, state: dict[str, Any], pregame_p: float | None = None,  # type: ignore[override]
                features: dict[str, Any] | None = None) -> float:
        """p_home in [0.001, 0.999]; `features` is accepted for the Model interface."""
        return self.predict_features(self.features(state, pregame_p))

    def to_json(self) -> dict[str, Any]:
        return {"coef": [float(w) for w in self.coef], "features": list(FEATURE_NAMES),
                "n_train": self.n_train, "train_seasons": list(self.train_seasons)}

    @classmethod
    def from_json(cls, params: dict[str, Any], artifact: dict[str, Any]) -> "IngameWP":
        model = cls(params)
        names = list(artifact.get("features") or FEATURE_NAMES)
        if names != list(FEATURE_NAMES):
            raise ValueError(f"ingame_wp artifact has features {names}, expected {list(FEATURE_NAMES)}")
        coef = [float(w) for w in artifact.get("coef") or []]
        if len(coef) != len(FEATURE_NAMES):
            raise ValueError(f"ingame_wp artifact has {len(coef)} coefficients, expected {len(FEATURE_NAMES)}")
        model.coef = coef
        model.n_train = int(artifact.get("n_train") or 0)
        model.train_seasons = [int(s) for s in artifact.get("train_seasons") or []]
        return model

    @staticmethod
    def search_space(rng: random.Random) -> dict[str, Any]:
        return {"l2": _log_uniform(rng, *L2_RANGE), "time_scale": _uniform(rng, *SCALE_RANGE),
                "fp_scale": _uniform(rng, *SCALE_RANGE)}

    @staticmethod
    def summary(params: dict[str, Any], metrics: dict[str, Any]) -> str:
        return build_summary(params, metrics)


def _span(seasons: Any) -> str:
    if not isinstance(seasons, (list, tuple)) or not seasons:
        return "the held-out seasons"
    first, last = seasons[0], seasons[-1]
    return str(first) if first == last else f"{first}-{last}"


def build_summary(params: dict[str, Any], metrics: dict[str, Any]) -> str:
    """Exactly three sentences: what it is, how it validated, the verdict."""
    first = (f"In-game win probability from score, clock, pre-game line, possession and field position "
             f"(L2 {float(params.get('l2', 0)):.2g} per 1000 plays, time exponent {float(params.get('time_scale', 1)):.2f}, "
             f"field scale {float(params.get('fp_scale', 1)):.2f}).")
    n = int(metrics.get("n_plays") or 0)
    if n == 0:
        return (f"{first} It has not been validated on held-out plays yet. "
                "Treat it as unusable until it is validated against vegas_wp.")
    ll, vll = float(metrics.get("log_loss") or 0.0), float(metrics.get("vegas_log_loss") or 0.0)
    second = (f"On {n} held-out plays from {_span(metrics.get('seasons'))} its log-loss is {ll:.3f} "
              f"against vegas_wp's {vll:.3f}.")
    if metrics.get("beats_baseline"):
        third = "It is not worse than the baseline, so it is usable, but in-game edge is proven only by paper trading."
    else:
        third = "It is worse than the baseline: paper trade it only to gather evidence."
    return f"{first} {second} {third}"
