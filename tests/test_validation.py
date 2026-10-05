"""The validation era (fleet.sim.validate, fleet.sim.search, the validate job):
selection never sees validation numbers, era labels and shapes, the overfit rule,
resume equivalence of validate and the job registry entry. Fixture based."""
from __future__ import annotations

import copy
import json
import random
from pathlib import Path

import pytest

from fleet.sim.backtest import run_backtest
from fleet.sim.control import JobStopped
from fleet.sim.data import load_games
from fleet.sim.robust import overfit_flags
from fleet.sim.search import run_search
from fleet.sim.validate import neighbourhood_params, run_validate
from fleet.worker.jobs import DEFAULT_LIMITS, JOBS

FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "games_sample.csv")
PARAMS = {"k": 24, "hfa": 55, "regress": 0.33, "rest_per_day": 1.0, "mov_scale": 1, "min_edge": 0.01, "kelly_fraction": 0.25}
ZERO_FEES = dict(DEFAULT_LIMITS, fee_model={"taker_rate": 0.0, "half_spread": 0.0})
SEARCH = [2016, 2021]
VALIDATION = [2022, 2025]
METRIC_KEYS = {"n_games", "n_bets", "total_stake_cents", "pnl_cents", "roi", "hit_rate", "avg_edge", "avg_stake_cents",
               "log_loss", "brier", "market_log_loss", "calibration", "max_drawdown_cents", "max_drawdown", "seasons",
               "per_season", "blend", "ci", "mean_ll_gain", "market_p", "brier_decomposition", "calib_slope",
               "calib_intercept", "shrunk_roi", "era"}
REGIMES = {"favourite", "underdog", "home", "away", "divisional", "non_divisional", "primetime", "day", "cold_or_windy", "other_weather"}


@pytest.fixture(scope="module")
def games() -> list[dict]:
    return load_games(FIXTURE)


def _scramble_validation_scores(games: list[dict], rng: random.Random) -> list[dict]:
    out = copy.deepcopy(games)
    for g in out:
        if g["season"] >= VALIDATION[0]:
            g["home_score"], g["away_score"] = rng.randint(0, 45), rng.randint(0, 45)
    return out


def _check_shapes(validation: dict, stress: dict) -> None:
    assert set(validation) == METRIC_KEYS | {"flags"}
    assert validation["era"] == "validation" and set(validation["flags"]) <= {"overfit"}
    assert set(validation["ci"]) == {"roi", "avg_clv", "max_drawdown", "hit_rate", "avg_edge"}
    for lo, hi in validation["ci"].values():
        assert lo <= hi
    assert 0.0 < validation["market_p"] <= 1.0
    assert set(validation["brier_decomposition"]) == {"reliability", "resolution", "uncertainty", "within_variance", "within_covariance"}
    assert set(stress) == {"prices", "neighbourhood", "regimes", "flags", "seed"}
    assert [p["name"] for p in stress["prices"]] == ["spread+0.01", "spread+0.02", "fee x1.5"]
    assert set(stress["neighbourhood"]) == {"n", "shrunk_roi_median", "shrunk_roi_p10", "ll_gain_median", "ll_gain_p10"}
    assert stress["neighbourhood"]["n"] == 10
    assert set(stress["regimes"]) == REGIMES and set(stress["flags"]) <= {"fragile", "regime_dependent"}


# selection never sees validation ----------------------------------------------------


def test_scrambling_validation_scores_changes_no_kept_candidate(games: list[dict]) -> None:
    kw = dict(family="elo_blend", n=6, seed=5, seasons=SEARCH, top_k=3, limits=ZERO_FEES, validation_seasons=VALIDATION)
    clean = run_search(games, emit=lambda cp, p: None, should_stop=lambda: False, **kw)
    scrambled = run_search(_scramble_validation_scores(games, random.Random(1)), emit=lambda cp, p: None, should_stop=lambda: False, **kw)
    assert clean["seasons"] == [2019, 2020, 2021] and clean["validation_seasons"] == [2022, 2023, 2024, 2025]
    assert clean["top"] == scrambled["top"], "selection and the search-era metrics only depend on the search era"
    assert [c["backtest_metrics"] for c in clean["create_models"]] == [c["backtest_metrics"] for c in scrambled["create_models"]]
    assert [c["backtest_metrics"]["era"] for c in clean["create_models"]] == ["search"] * 3
    changed = [a["validation_metrics"] != b["validation_metrics"] for a, b in zip(clean["create_models"], scrambled["create_models"])]
    assert all(changed), "the validation numbers follow the validation-era scores"
    for entry in clean["create_models"]:
        _check_shapes(entry["validation_metrics"], entry["stress_metrics"])
        assert entry["validation_metrics"]["seasons"] == [2022, 2023, 2024, 2025]
        assert entry["stress_metrics"]["seed"] == 5
    assert [v["index"] for v in clean["validated"]] == [e["index"] for e in clean["top"]]
    assert "validation_note" not in clean
    json.dumps(clean)


def test_search_era_backtest_never_touches_validation_games(games: list[dict]) -> None:
    scrambled = _scramble_validation_scores(games, random.Random(2))
    base = run_backtest(games, "elo_blend", PARAMS, SEARCH, ZERO_FEES, lambda cp, p: None, lambda: False)
    other = run_backtest(scrambled, "elo_blend", PARAMS, SEARCH, ZERO_FEES, lambda cp, p: None, lambda: False)
    assert base == other


# validate -------------------------------------------------------------------------------


def test_validate_shapes_eras_and_stress_on_the_fixture(games: list[dict]) -> None:
    emitted: list[tuple[dict, float]] = []
    result = run_validate(games, "elo_blend", PARAMS, VALIDATION, ZERO_FEES, 1, lambda cp, p: emitted.append((copy.deepcopy(cp), p)), lambda: False)
    assert set(result) == {"validation_metrics", "stress_metrics"}
    validation, stress = result["validation_metrics"], result["stress_metrics"]
    _check_shapes(validation, stress)
    assert validation["n_bets"] > 100 and validation["seasons"] == [2022, 2023, 2024, 2025]
    assert validation["shrunk_roi"] == pytest.approx(validation["roi"] * validation["n_bets"] / (validation["n_bets"] + 100))
    assert validation["ci"]["roi"][0] <= validation["roi"] <= validation["ci"]["roi"][1]
    assert validation["ci"]["avg_clv"] == [0.0, 0.0], "closing-line entries have no CLV"
    assert validation["ci"]["max_drawdown"][0] <= validation["max_drawdown"] <= validation["ci"]["max_drawdown"][1] + 1e-12
    assert validation["mean_ll_gain"] == pytest.approx(validation["market_log_loss"] - validation["log_loss"])
    assert validation["flags"] == [], "without search-era metrics there is no overfit rule"
    assert stress["prices"][0]["n_bets"] <= validation["n_bets"]
    assert sum(stress["regimes"][name]["n_games"] for name in ("home", "away")) == validation["n_games"]
    assert stress["seed"] == 1
    # units: 4 seasons x (base + 10 neighbourhood runs); progress is monotonic and ends at 1
    progresses = [p for _, p in emitted]
    assert progresses == sorted(progresses) and progresses[-1] == pytest.approx(1.0)
    assert emitted[-1][0]["stage"] == 11 and len(emitted[-1][0]["runs"]) == 10
    assert [cp["stage"] for cp, _ in emitted][:5] == [0, 0, 0, 0, 1]
    json.dumps(result)


def test_validate_resumes_from_any_checkpoint_with_the_same_result(games: list[dict]) -> None:
    full = run_validate(games, "elo_blend", PARAMS, VALIDATION, ZERO_FEES, 3, lambda cp, p: None, lambda: False)
    for cut in (2, 5, 23, 44):
        emitted: list[tuple[dict, float]] = []
        with pytest.raises(JobStopped):
            run_validate(games, "elo_blend", PARAMS, VALIDATION, ZERO_FEES, 3,
                         lambda cp, p: emitted.append((copy.deepcopy(cp), p)), lambda: len(emitted) >= cut)
        checkpoint = json.loads(json.dumps(emitted[-1][0]))
        resumed_emits: list[float] = []
        resumed = run_validate(games, "elo_blend", PARAMS, VALIDATION, ZERO_FEES, 3,
                               lambda cp, p: resumed_emits.append(p), lambda: False, checkpoint)
        assert resumed == full, cut
        if resumed_emits:
            assert min(resumed_emits) >= emitted[-1][1] - 1e-12
    assert run_validate(games, "elo_blend", PARAMS, VALIDATION, ZERO_FEES, 3, lambda cp, p: None, lambda: False, {"garbage": 1}) == full


def test_validate_is_deterministic_and_the_seed_moves_only_the_resampling(games: list[dict]) -> None:
    one = run_validate(games, "elo_blend", PARAMS, VALIDATION, ZERO_FEES, 1, lambda cp, p: None, lambda: False)
    again = run_validate(games, "elo_blend", PARAMS, VALIDATION, ZERO_FEES, 1, lambda cp, p: None, lambda: False)
    other = run_validate(games, "elo_blend", PARAMS, VALIDATION, ZERO_FEES, 2, lambda cp, p: None, lambda: False)
    assert one == again
    for key in ("n_bets", "roi", "log_loss", "mean_ll_gain", "shrunk_roi", "calib_slope"):
        assert one["validation_metrics"][key] == other["validation_metrics"][key]
    assert one["validation_metrics"]["ci"] != other["validation_metrics"]["ci"]
    assert one["stress_metrics"]["neighbourhood"] != other["stress_metrics"]["neighbourhood"], "another seed, other perturbations"
    assert one["stress_metrics"]["regimes"] == other["stress_metrics"]["regimes"]
    assert neighbourhood_params("elo_blend", PARAMS, 1, 0) != neighbourhood_params("elo_blend", PARAMS, 2, 0)


def test_validate_without_a_validation_era_keeps_the_shape(games: list[dict]) -> None:
    result = run_validate(games, "elo_blend", PARAMS, [2030, None], ZERO_FEES, 1, lambda cp, p: None, lambda: False)
    validation = result["validation_metrics"]
    assert validation["n_games"] == 0 and validation["seasons"] == [] and validation["market_p"] == 1.0
    assert validation["ci"]["roi"] == [0.0, 0.0] and validation["era"] == "validation"
    assert result["stress_metrics"]["neighbourhood"]["n"] == 10 and result["stress_metrics"]["flags"] == []


def test_overfit_rule() -> None:
    search = {"roi": 0.10, "n_bets": 400, "market_p": 0.5}
    good = {"roi": 0.09, "n_bets": 400, "market_p": 0.5}
    assert overfit_flags(search, good) == []
    assert overfit_flags(search, {"roi": 0.05, "n_bets": 400, "market_p": 0.5}) == ["overfit"], "shrunk ROI gap above 0.03"
    assert overfit_flags({"shrunk_roi": 0.08}, {"shrunk_roi": 0.049}) == ["overfit"], "stored shrunk_roi is used when present"
    assert overfit_flags({"shrunk_roi": 0.08}, {"shrunk_roi": 0.05}) == []
    assert overfit_flags({"roi": 0.05, "n_bets": 400, "market_p": 0.05}, {"roi": 0.05, "n_bets": 400, "market_p": 0.2}) == ["overfit"]
    assert overfit_flags({"roi": 0.05, "n_bets": 400, "market_p": 0.05}, {"roi": 0.05, "n_bets": 400, "market_p": 0.09}) == []
    assert overfit_flags({"roi": 0.05, "n_bets": 400}, {"roi": 0.05, "n_bets": 400, "market_p": 0.5}) == [], "no market test, no verdict"
    assert overfit_flags(None, good) == [] and overfit_flags({}, good) == []


def test_validate_job_reads_the_context_model_and_the_overfit_flag(games: list[dict]) -> None:
    model = {"id": "m1", "family": "elo_blend", "params": PARAMS, "backtest_metrics": {"roi": 0.5, "n_bets": 500, "market_p": 0.5}}
    params = {"model_id": "m1", "validation_seasons": VALIDATION, "fee_model": ZERO_FEES["fee_model"],
              "_context": {"games_path": FIXTURE, "model": model}}
    emitted: list[float] = []
    result = JOBS["validate"](params, None, lambda cp, p: emitted.append(p), lambda: False)
    assert set(result) == {"validation_metrics", "stress_metrics"}
    assert result["validation_metrics"]["flags"] == ["overfit"], "a search-era ROI far above the validation one"
    assert result["stress_metrics"]["seed"] == 1 and emitted[-1] == pytest.approx(1.0)
    direct = run_validate(games, "elo_blend", PARAMS, VALIDATION, ZERO_FEES, 1, lambda cp, p: None, lambda: False, search_metrics=model["backtest_metrics"])
    assert result == direct
    seeded = JOBS["validate"](dict(params, seed=4), None, lambda cp, p: None, lambda: False)
    assert seeded["stress_metrics"]["seed"] == 4
    with pytest.raises(ValueError):
        JOBS["validate"]({"model_id": "m1", "_context": {"games_path": FIXTURE, "model": None}}, None, lambda cp, p: None, lambda: False)


def test_search_job_passes_validation_seasons_and_workers_through(games: list[dict]) -> None:
    params = {"family": "elo_blend", "n": 2, "seed": 5, "seasons": SEARCH, "top_k": 2, "validation_seasons": VALIDATION,
              "workers": 2, "_context": {"games_path": FIXTURE, "model": None}}
    result = JOBS["model_search"](params, None, lambda cp, p: None, lambda: False)
    assert result["validation_seasons"] == [2022, 2023, 2024, 2025] and len(result["validated"]) == 2
    assert all(c["validation_metrics"]["era"] == "validation" for c in result["create_models"])
    single = JOBS["model_search"](dict(params, workers="auto"), None, lambda cp, p: None, lambda: False)
    assert single == result
    without = JOBS["model_search"]({k: v for k, v in params.items() if k != "validation_seasons"}, None, lambda cp, p: None, lambda: False)
    assert without["validated"] == [] and "no validation era" in without["validation_note"]
    assert all(c["validation_metrics"] is None for c in without["create_models"])
