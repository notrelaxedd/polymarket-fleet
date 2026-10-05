"""fleet.models.ingame_wp: features, state mapping, the fit, predictions, the artifact
and the validation metrics (fleet.sim.ingame_eval). Synthetic plays only, no database."""
from __future__ import annotations

import json
import math
import random
from typing import Any

import pytest

from fleet.models import ingame_wp
from fleet.models.base import params_hash
from fleet.models.ingame_wp import FEATURE_NAMES, IngameWP, feature_vector, game_clock, scale_features, state_from_row
from fleet.sim.ingame_eval import validate_model
from fleet.sim.odds import expit

TRUE_COEF = [0.1, 0.12, 0.4, 0.6, 0.8, -0.3, -0.2, 0.3, 0.5, 0.7]


def _seconds(rng: random.Random) -> tuple[int, int]:
    """(seconds_remaining, half) with extra mass in the two end-of-half windows and some overtime."""
    u = rng.random()
    if u < 0.12:
        return rng.randint(1801, 1920), 1
    if u < 0.24:
        return rng.randint(0, 120), 2
    if u < 0.28:
        return rng.randint(0, 600), 3
    s = rng.randint(0, 3600)
    return s, 1 if s > 1800 else 2


def synthetic_rows(n: int, seed: str, seasons: tuple[int, ...] = (2020,), coef: list[float] | None = None,
                   plays_per_game: int = 25, vegas_noise: float = 0.0) -> list[dict[str, Any]]:
    """Independent plays whose home_win is drawn from expit(coef . feature_vector(state, p))
    (scales 1). vegas_wp is the true probability, pushed by vegas_noise logit units of noise."""
    rng = random.Random(f"ingame-synthetic:{seed}")
    coef = coef or TRUE_COEF
    rows = []
    for i in range(n):
        season = seasons[i % len(seasons)]
        s, half = _seconds(rng)
        u = rng.random()
        posteam = None if u < 0.05 else u < 0.525
        row = {
            "game_id": f"{season}_{seed}_{i // plays_per_game:05d}", "play_id": str(i), "season": season,
            "score_diff": int(round(rng.gauss(0, 8))), "seconds_remaining": s, "half": half,
            "down": None if posteam is None else rng.randint(1, 4),
            "ydstogo": None if posteam is None else rng.randint(1, 20),
            "yardline_100": None if posteam is None else rng.randint(1, 99),
            "posteam_is_home": posteam, "home_timeouts": rng.randint(0, 3), "away_timeouts": rng.randint(0, 3),
            "pregame_p_home": None if rng.random() < 0.05 else round(rng.uniform(0.15, 0.85), 4),
        }
        x = feature_vector(state_from_row(row), row["pregame_p_home"])
        p = expit(sum(w * v for w, v in zip(coef, x)))
        row["home_win"] = 1.0 if rng.random() < p else 0.0
        noisy = expit(math.log(p / (1 - p)) + rng.gauss(0, vegas_noise)) if vegas_noise else p
        row["vegas_wp"] = round(min(max(noisy, 0.001), 0.999), 6)
        rows.append(row)
    return rows


def _increasing(ps: list[float]) -> bool:
    """Strictly increasing except where both neighbours sit on the same clamp."""
    return all(b > a or a == b in (ingame_wp.P_MIN, ingame_wp.P_MAX) for a, b in zip(ps, ps[1:]))


def _state(**kw: Any) -> dict[str, Any]:
    base = {"status": "in", "period": 4, "clock_seconds": 600, "home_score": 0, "away_score": 0,
            "possession": None, "down": None, "distance": None, "yardline_100": None,
            "home_timeouts": 3, "away_timeouts": 3}
    base.update(kw)
    return base


def test_state_from_row_round_trips_the_clock() -> None:
    for s, half, period, clock in [(3600, 1, 1, 900), (2700, 1, 2, 900), (2699, 1, 2, 899), (1800, 1, 2, 0),
                                   (1800, 2, 3, 900), (900, 2, 4, 900), (899, 2, 4, 899), (0, 2, 4, 0), (480, 3, 5, 480)]:
        state = state_from_row({"seconds_remaining": s, "half": half, "score_diff": -3, "posteam_is_home": False})
        assert (state["period"], state["clock_seconds"]) == (period, clock), (s, half)
        assert game_clock(state) == (s, half)
        assert state["home_score"] - state["away_score"] == -3 and state["possession"] == "away"
    keys = {"status", "period", "clock_seconds", "home_score", "away_score", "possession", "down", "distance",
            "yardline_100", "home_timeouts", "away_timeouts"}
    assert set(state_from_row({"seconds_remaining": 100, "half": 2, "score_diff": 0})) == keys
    assert game_clock(_state(status="pre")) == (3600, 1)
    assert game_clock(_state(status="final", period=5)) == (0, 3)


def test_feature_vector_terms() -> None:
    x = feature_vector(_state(period=4, clock_seconds=100, home_score=10, away_score=3, possession="away",
                              down=3, distance=25, yardline_100=30, home_timeouts=1, away_timeouts=3), 0.6,
                       time_scale=2.0, fp_scale=0.5)
    named = dict(zip(FEATURE_NAMES, x))
    t = 100 / 3600
    assert named["const"] == 1.0
    assert named["score_time"] == pytest.approx(7 / math.sqrt(t + 0.01) * 2.0)
    assert named["pregame_logit"] == pytest.approx(math.log(0.6 / 0.4))
    assert named["pregame_logit_time"] == pytest.approx(math.log(0.6 / 0.4) * t)
    assert named["field_position"] == pytest.approx(-0.7 * 0.5)
    assert named["late_down"] == -1.0 and named["distance"] == pytest.approx(-2.0)
    assert named["timeouts"] == pytest.approx(-2 / 3)
    assert named["end_half1"] == 0.0 and named["end_game"] == -1.0
    assert dict(zip(FEATURE_NAMES, feature_vector(_state(period=2, clock_seconds=60, possession="home"), None)))[
        "end_half1"] == 1.0
    base = feature_vector(_state(home_score=3, possession="home", down=1, distance=10, yardline_100=60), 0.4)
    assert scale_features(base, 1.7, 0.6) == feature_vector(
        _state(home_score=3, possession="home", down=1, distance=10, yardline_100=60), 0.4, 1.7, 0.6)


def test_fit_recovers_known_coefficients() -> None:
    rows = synthetic_rows(30000, "recover")
    model = IngameWP({"l2": 0.01, "time_scale": 1.0, "fp_scale": 1.0})
    model.fit(rows)
    assert model.n_train == 30000 and model.train_seasons == [2020]
    for name, got, want in zip(FEATURE_NAMES, model.coef, TRUE_COEF):
        tol = 0.3 if name.startswith("end_") else 0.15
        assert abs(got - want) < tol, (name, got, want)


def test_predictions_monotone_in_score_and_sharpen_late() -> None:
    model = IngameWP.from_json({}, {"coef": TRUE_COEF, "features": list(FEATURE_NAMES)})
    for clock in (900, 300, 30):
        ps = [model.predict(_state(clock_seconds=clock, home_score=max(d, 0), away_score=max(-d, 0)), 0.5)
              for d in range(-21, 22)]
        assert _increasing(ps) and ps[0] < 0.5 < ps[-1], clock
    lead = [model.predict(_state(period=q, clock_seconds=c, home_score=7), 0.5) for q, c in
            [(1, 900), (2, 450), (3, 300), (4, 600), (4, 120), (4, 10)]]
    assert all(b > a for a, b in zip(lead, lead[1:])), lead
    trail = model.predict(_state(period=4, clock_seconds=10, away_score=7), 0.5)
    assert trail < 0.01 and lead[-1] > 0.99
    assert ingame_wp.P_MIN <= model.predict(_state(clock_seconds=0, home_score=60), 0.99) <= ingame_wp.P_MAX


def test_fitted_model_is_monotone_and_sharpens() -> None:
    model = IngameWP({"l2": 0.1})
    model.fit(synthetic_rows(8000, "mono"))
    early = [model.predict(_state(period=1, clock_seconds=800, home_score=max(d, 0), away_score=max(-d, 0)), 0.5)
             for d in range(-14, 15)]
    late = [model.predict(_state(period=4, clock_seconds=60, home_score=max(d, 0), away_score=max(-d, 0)), 0.5)
            for d in range(-14, 15)]
    assert _increasing(early) and _increasing(late)
    assert abs(late[-1] - 0.5) > abs(early[-1] - 0.5) and abs(late[0] - 0.5) > abs(early[0] - 0.5)


def test_to_json_from_json_round_trip() -> None:
    model = IngameWP({"l2": 0.5, "time_scale": 1.3, "fp_scale": 0.7})
    model.fit(synthetic_rows(3000, "json", seasons=(2018, 2019)))
    artifact = json.loads(json.dumps(model.to_json()))
    again = IngameWP.from_json(model.params, artifact)
    assert again.coef == model.coef and again.to_json() == model.to_json()
    assert again.train_seasons == [2018, 2019] and again.params == model.params
    state = _state(period=3, clock_seconds=412, home_score=10, away_score=14, possession="home", down=2,
                   distance=7, yardline_100=44)
    assert again.predict(state, 0.62) == model.predict(state, 0.62)
    assert params_hash(again.params) == params_hash(model.params)
    with pytest.raises(ValueError):
        IngameWP.from_json({}, {"coef": [0.0, 1.0]})
    with pytest.raises(ValueError):
        IngameWP.from_json({}, {"coef": TRUE_COEF, "features": ["const"]})


def test_search_space_and_summary() -> None:
    params = IngameWP.search_space(random.Random("1:0"))
    assert set(params) == set(ingame_wp.PARAM_KEYS)
    assert 0.01 <= params["l2"] <= 10 and 0.5 <= params["time_scale"] <= 2 and 0.5 <= params["fp_scale"] <= 2
    assert IngameWP.search_space(random.Random("1:0")) == params
    good = IngameWP.summary(params, {"n_plays": 900, "log_loss": 0.41, "vegas_log_loss": 0.43,
                                     "beats_baseline": True, "seasons": [2022, 2024]})
    bad = IngameWP.summary(params, {"n_plays": 900, "log_loss": 0.45, "vegas_log_loss": 0.43,
                                    "beats_baseline": False, "seasons": [2022, 2022]})
    empty = IngameWP.summary(params, {})
    for text in (good, bad, empty):
        assert text.count(". ") == 2 and text.endswith(".") and chr(0x2014) not in text
    assert "2022-2024" in good and "not worse" in good and "must not be used" in bad


def test_calibration_and_vegas_baseline_where_truth_is_known() -> None:
    truth = IngameWP.from_json({}, {"coef": TRUE_COEF, "features": list(FEATURE_NAMES)})
    rows = synthetic_rows(6000, "calib", seasons=(2023,), vegas_noise=1.0)
    rows.append(dict(rows[0], vegas_wp=None))
    rows.append(dict(rows[1], home_win=None))
    val = validate_model(truth, rows)
    assert val["n_plays"] == 6000 and val["n_skipped_no_vegas"] == 1 and val["seasons"] == [2023]
    assert val["beats_baseline"] is True and val["log_loss"] < val["vegas_log_loss"] - 0.02
    assert len(val["calibration"]) == 10 and sum(b["count"] for b in val["calibration"]) == 6000
    for bucket in val["calibration"]:
        if bucket["count"] >= 300:
            assert abs(bucket["mean_p"] - bucket["mean_outcome"]) < 0.06, bucket
    assert set(val["by_period"]) == {"1", "2", "3", "4", "5"}
    assert sum(g["n_plays"] for g in val["by_period"].values()) == 6000
    assert set(val["by_score_bucket"]) == {"<=-9", "-8..-1", "0", "1..8", ">=9"}
    assert sum(g["n_plays"] for g in val["by_score_bucket"].values()) == 6000
    # the baseline is the truth here: a coin-flip model must not beat it
    coin = IngameWP.from_json({}, {"coef": [0.0] * len(FEATURE_NAMES), "features": list(FEATURE_NAMES)})
    exact = validate_model(coin, synthetic_rows(3000, "calib2", seasons=(2023,)))
    assert exact["beats_baseline"] is False and exact["log_loss"] == pytest.approx(math.log(2))
    assert validate_model(coin, [])["beats_baseline"] is False
