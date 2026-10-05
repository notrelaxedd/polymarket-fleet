"""elo_blend signal penalties: zero penalties change nothing, fit and predict apply the
same shift, and the search draws stay reproducible for old seeds (step 6B, B2)."""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from fleet.models.elo import expected_home
from fleet.models.elo_blend import DEFAULT_PARAMS, PARAM_KEYS, SIGNAL_DEFAULTS, EloBlend, signal_adjustment
from fleet.models.search_space import bounds_of, clip_params, perturb_params
from fleet.sim.backtest import run_fold
from fleet.sim.data import load_games
from fleet.sim.odds import devig
from fleet.worker.jobs import DEFAULT_LIMITS

FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "games_sample.csv")
PARAMS = {"k": 24, "hfa": 55, "regress": 0.33, "rest_per_day": 1.0, "mov_scale": 1,
          "min_edge": 0.03, "kelly_fraction": 0.25}
PENALTIES = {"qb_change_penalty": 40.0, "out_penalty_per_player": 6.0}


@pytest.fixture(scope="module")
def games() -> list[dict]:
    """The fixture with quarterback flags from the csv plus deterministic Out counts."""
    rng = random.Random("elo-penalties")
    out = load_games(FIXTURE)
    for g in out:
        g["signals"]["home_out_count"] = rng.randint(0, 4)
        g["signals"]["away_out_count"] = rng.randint(0, 4)
    return out


def _without_signals(games: list[dict]) -> list[dict]:
    return [{k: v for k, v in g.items() if k != "signals"} for g in games]


def _key(records: list[dict]) -> list[tuple]:
    return [(r["game_id"], r["p"], r["pnl_cents"]) for r in records]


def test_defaults_are_zero_and_keys_known() -> None:
    assert SIGNAL_DEFAULTS == {"qb_change_penalty": 0.0, "out_penalty_per_player": 0.0}
    assert set(PARAM_KEYS) == set(DEFAULT_PARAMS) | set(SIGNAL_DEFAULTS)
    # An existing model's params are not widened (its neighbourhood draws stay the same).
    assert EloBlend(PARAMS).params == {**DEFAULT_PARAMS, **PARAMS}
    assert signal_adjustment({}, {"home_qb_changed": 1, "home_out_count": 5}) == 0.0
    # In the clip bounds (the search space), so a stress perturbation never leaves it;
    # clip_params skips a bounded key a model does not carry, so old params stay narrow.
    assert bounds_of("elo_blend")["qb_change_penalty"] == (0.0, 80.0)
    assert bounds_of("elo_blend")["out_penalty_per_player"] == (0.0, 15.0)
    assert set(clip_params("elo_blend", dict(PARAMS))) == set(PARAMS), "no penalty keys added to old params"
    assert clip_params("elo_blend", {"qb_change_penalty": 88.0, "out_penalty_per_player": -1.0}) == {"qb_change_penalty": 80.0, "out_penalty_per_player": 0.0}


@pytest.mark.parametrize("season", [2021, 2025])
def test_zero_penalties_predict_exactly_as_before(games: list[dict], season: int) -> None:
    """An existing model (params without the new keys) predicts bit for bit what it did
    before the signals existed, whatever the signals say."""
    limits = dict(DEFAULT_LIMITS)
    before, blend_before = run_fold(_without_signals(games), "elo_blend", PARAMS, season, limits, lambda: False)
    for params in (PARAMS, {**PARAMS, "qb_change_penalty": 0, "out_penalty_per_player": 0.0}):
        after, blend_after = run_fold(games, "elo_blend", params, season, limits, lambda: False)
        assert blend_after == blend_before
        assert _key(after) == _key(before)


def test_penalties_change_predictions_in_the_right_direction(games: list[dict]) -> None:
    model = EloBlend({**PARAMS, **PENALTIES})
    model.fit(games, (2023, 22), lambda: False)
    probe = {"season": 2024, "week": 1, "home_team": "KC", "away_team": "BAL", "home_rest": 7, "away_rest": 7}
    for market in (None, 0.6):
        base = model.predict(probe, market, {"signals": {}})
        home_qb = model.predict(probe, market, {"signals": {"home_qb_changed": 1}})
        away_outs = model.predict(probe, market, {"signals": {"away_out_count": 3}})
        # The Elo part always moves the right way; through the blend it follows the sign of a.
        sign = 1.0 if market is None or model.blend["a"] > 0 else -1.0
        assert sign * (home_qb - base) < 0 < sign * (away_outs - base)
    # Without a market price the prediction is the shifted Elo expectation itself.
    r_home, r_away = model.elo.rating("KC"), model.elo.rating("BAL")
    shift = signal_adjustment(model.params, {"home_qb_changed": 1, "away_out_count": 2})
    assert shift == pytest.approx(-40.0 + 12.0)
    p = model.predict(probe, None, {"signals": {"home_qb_changed": 1, "away_out_count": 2}})
    assert p == pytest.approx(expected_home(r_home, r_away, 55.0, shift))


def test_fit_and_walk_forward_apply_the_same_shift(games: list[dict]) -> None:
    """Fitting through week 6 equals fitting through week 5 and observing week 6, so the
    rating updates in fit and observe use the same penalties; the fold's predictions
    equal predict on the fitted model with the game's own signals."""
    params = {**PARAMS, **PENALTIES}
    a = EloBlend(params)
    a.fit(games, (2020, 6), lambda: False)
    b = EloBlend(params)
    b.fit(games, (2020, 5), lambda: False)
    for g in games:
        if (g["season"], g["week"]) == (2020, 6):
            b.observe(g)
    assert b.elo.ratings == pytest.approx(a.elo.ratings)
    assert a.elo.ratings != pytest.approx(EloBlend(PARAMS).elo.ratings)
    # predict with features {} falls back to the game's own signals, as fit does.
    g = next(x for x in games if (x["season"], x["week"]) == (2020, 7) and x["home_moneyline"] is not None)
    pm = devig(g["home_moneyline"], g["away_moneyline"])
    assert a.predict(g, pm, {}) == a.predict(g, pm, {"signals": g["signals"]})


def test_search_draws_keep_the_old_params_for_old_seeds() -> None:
    def old_space(rng: random.Random) -> dict:
        def u(low: float, high: float) -> float:
            return round(low + (high - low) * rng.random(), 6)
        return {"k": u(10, 40), "hfa": u(20, 90), "regress": u(0.1, 0.6), "rest_per_day": u(0, 4),
                "mov_scale": 1 if rng.random() < 0.5 else 0, "min_edge": u(0.01, 0.08), "kelly_fraction": u(0.1, 0.5)}

    for i in range(40):
        new = EloBlend.search_space(random.Random(f"7:{i}"))
        old = old_space(random.Random(f"7:{i}"))
        assert {k: new[k] for k in old} == old
        assert 0 <= new["qb_change_penalty"] <= 80 and 0 <= new["out_penalty_per_player"] <= 15
        assert list(new)[-2:] == ["qb_change_penalty", "out_penalty_per_player"]
    perturbed = perturb_params("elo_blend", {**PARAMS, "qb_change_penalty": 79.0, "out_penalty_per_player": 0.0},
                               random.Random("x"))
    assert 71.1 - 1e-9 <= perturbed["qb_change_penalty"] <= 80.0 and perturbed["out_penalty_per_player"] == 0.0


def test_summary_mentions_nonzero_penalties_and_stays_three_sentences() -> None:
    metrics = {"n_games": 500, "n_bets": 80, "avg_edge": 0.03, "roi": 0.02, "max_drawdown": 0.1,
               "log_loss": 0.66, "market_log_loss": 0.661, "seasons": [2018, 2025],
               "blend": {"a": 0.4, "b": 1.0, "c": 0.0}}
    text = EloBlend.summary({**PARAMS, **PENALTIES}, metrics)
    assert "QB change costs 40 points, 6.0 points per player Out)." in text
    assert len(re.findall(r"[.!?](?=\s|$)", text)) == 3
    plain = EloBlend.summary(PARAMS, metrics)
    assert "QB change" not in plain and "margin scaling on)." in plain
