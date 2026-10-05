"""Resampling statistics (fleet.sim.stats): bootstrap determinism and sanity, the
sign-flip market test, the Brier decomposition identity, the recalibration fit and
the timing budget. Pure functions, no fixture, no database."""
from __future__ import annotations

import itertools
import math
import random
import statistics
import time

import pytest

from fleet.sim.stats import (bet_bootstrap, bootstrap_ci, brier_decomposition, drawdown_bootstrap,
                             logistic_recalibration, max_drawdown, percentile, permutation_market_test)


def _bets(rng: random.Random, n: int, mean_pnl: float, spread: float) -> list[tuple[float, float, float, float, float]]:
    out = []
    for _ in range(n):
        pnl = rng.gauss(mean_pnl, spread)
        out.append((pnl, 200.0, 1.0 if pnl > 0 else 0.0, 0.04, 0.01))
    return out


# bootstrap ----------------------------------------------------------------------


def test_percentile_interpolates() -> None:
    assert percentile([], 0.5) == 0.0
    assert percentile([3.0], 0.05) == 3.0 and percentile([3.0], 0.95) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert percentile([5.0, 1.0, 3.0], 0.25) == pytest.approx(2.0)
    assert percentile([1.0, 2.0], 0.95) == pytest.approx(1.95)


def test_bootstrap_is_deterministic_for_a_seed() -> None:
    source = random.Random(3)
    values = [source.gauss(0, 1) for _ in range(200)]
    first = bootstrap_ci(values, statistics.fmean, 500, random.Random("1:boot"))
    second = bootstrap_ci(values, statistics.fmean, 500, random.Random("1:boot"))
    other = bootstrap_ci(values, statistics.fmean, 500, random.Random("2:boot"))
    assert first == second and first != other
    bets = _bets(random.Random(4), 300, 10.0, 100.0)
    assert bet_bootstrap(bets, 300, random.Random("s:boot")) == bet_bootstrap(bets, 300, random.Random("s:boot"))


def test_ci_of_a_constant_is_degenerate_and_empty_input_is_handled() -> None:
    assert bootstrap_ci([2.5] * 50, statistics.fmean, 200, random.Random(1)) == [2.5, 2.5]
    assert bootstrap_ci([], lambda v: 0.0, 200, random.Random(1)) == [0.0, 0.0]
    constant = [(50.0, 200.0, 1.0, 0.03, 0.0)] * 40
    ci = bet_bootstrap(constant, 200, random.Random(1))
    assert ci["roi"] == [0.25, 0.25] and ci["hit_rate"] == [1.0, 1.0] and ci["avg_clv"] == [0.0, 0.0]
    assert ci["avg_edge"] == pytest.approx([0.03, 0.03])
    empty = bet_bootstrap([], 200, random.Random(1))
    assert empty == {"roi": [0.0, 0.0], "hit_rate": [0.0, 0.0], "avg_edge": [0.0, 0.0], "avg_clv": [0.0, 0.0]}


def test_ci_widens_with_variance_and_contains_the_point_estimate() -> None:
    narrow = _bets(random.Random(5), 400, 5.0, 20.0)
    wide = _bets(random.Random(5), 400, 5.0, 200.0)
    ci_narrow = bet_bootstrap(narrow, 500, random.Random("n"))
    ci_wide = bet_bootstrap(wide, 500, random.Random("w"))
    assert ci_wide["roi"][1] - ci_wide["roi"][0] > 3 * (ci_narrow["roi"][1] - ci_narrow["roi"][0])
    for bets, ci in ((narrow, ci_narrow), (wide, ci_wide)):
        roi = sum(b[0] for b in bets) / sum(b[1] for b in bets)
        hit = sum(b[2] for b in bets) / len(bets)
        assert ci["roi"][0] <= roi <= ci["roi"][1]
        assert ci["hit_rate"][0] <= hit <= ci["hit_rate"][1]
        assert ci["avg_clv"] == pytest.approx([0.01, 0.01])


def test_drawdown_block_bootstrap() -> None:
    assert max_drawdown([100, -50, -80, 200, -300]) == 300
    assert max_drawdown([]) == 0.0 and max_drawdown([-10]) == 10
    one = [[100, -300, 50]]
    assert drawdown_bootstrap(one, 50, random.Random(1)) == [300.0, 300.0], "a single season resamples to itself"
    seasons = [[100, -300], [50, 50], [-400, 100]]
    lo, hi = drawdown_bootstrap(seasons, 300, random.Random("dd"))
    assert 0.0 <= lo <= hi <= 1200.0  # three draws of the worst season back to back
    assert drawdown_bootstrap([], 10, random.Random(1)) == [0.0, 0.0]
    assert drawdown_bootstrap(seasons, 300, random.Random("dd")) == [lo, hi]


# the market test ----------------------------------------------------------------


def test_permutation_p_is_one_for_an_identical_model_by_convention() -> None:
    """d all zero: every flip ties the observed mean, so p = 1.0 (no evidence at all)."""
    assert permutation_market_test([0.0] * 100, 1000, random.Random(1)) == (0.0, 1.0)
    assert permutation_market_test([], 1000, random.Random(1)) == (0.0, 1.0)


def test_permutation_p_is_near_half_for_a_symmetric_gain_pattern() -> None:
    d = [0.01, -0.01] * 150
    mean, p = permutation_market_test(d, 4000, random.Random("sym"))
    assert mean == pytest.approx(0.0, abs=1e-12)
    assert 0.4 <= p <= 0.65  # exactly half of the flips land above zero plus the ties at zero


def test_permutation_detects_a_clearly_better_and_a_clearly_worse_model() -> None:
    rng = random.Random(9)
    better = [rng.gauss(0.01, 0.02) for _ in range(500)]
    worse = [-x for x in better]
    mean_b, p_b = permutation_market_test(better, 10_000, random.Random("1:perm"))
    mean_w, p_w = permutation_market_test(worse, 10_000, random.Random("1:perm"))
    assert mean_b > 0 and p_b < 0.01
    assert mean_w < 0 and p_w > 0.9
    assert permutation_market_test(better, 10_000, random.Random("1:perm")) == (mean_b, p_b)


def test_permutation_matches_the_exact_enumeration_on_a_small_sample() -> None:
    d = [0.3, -0.1, 0.2, -0.4, 0.05, 0.1, 0.02, -0.03, 0.07, 0.01, -0.02]
    total = sum(d)
    hits = sum(1 for signs in itertools.product((1, -1), repeat=len(d)) if sum(s * x for s, x in zip(signs, d)) >= total - 1e-12)
    exact = hits / 2 ** len(d)
    mean, p = permutation_market_test(d, 20_000, random.Random("enum"))
    assert mean == pytest.approx(total / len(d))
    assert p == pytest.approx(exact, abs=0.02)


# calibration ----------------------------------------------------------------------


def test_brier_decomposition_identity_with_bucket_constant_forecasts() -> None:
    rng = random.Random(21)
    centres = [0.05 + 0.1 * k for k in range(10)]
    p = [rng.choice(centres) for _ in range(2000)]
    y = [1.0 if rng.random() < q else 0.0 for q in p]
    parts = brier_decomposition(p, y)
    brier = sum((pi - yi) ** 2 for pi, yi in zip(p, y)) / len(p)
    assert parts["reliability"] - parts["resolution"] + parts["uncertainty"] == pytest.approx(brier, abs=1e-12)
    o_bar = sum(y) / len(y)
    assert parts["uncertainty"] == pytest.approx(o_bar * (1 - o_bar))
    assert parts["reliability"] < 0.005 and parts["resolution"] > 0.05
    assert brier_decomposition([], []) == {"reliability": 0.0, "resolution": 0.0, "uncertainty": 0.0}


def test_brier_decomposition_flags_a_miscalibrated_model() -> None:
    rng = random.Random(22)
    truth = [rng.choice([0.2, 0.5, 0.8]) for _ in range(3000)]
    y = [1.0 if rng.random() < q else 0.0 for q in truth]
    overconfident = [min(max(q + (0.15 if q > 0.5 else -0.15 if q < 0.5 else 0.0), 0.01), 0.99) for q in truth]
    assert brier_decomposition(overconfident, y)["reliability"] > 5 * brier_decomposition(truth, y)["reliability"]


def test_recalibration_recovers_the_identity_on_calibrated_data() -> None:
    rng = random.Random(23)
    p = [rng.random() * 0.9 + 0.05 for _ in range(20000)]
    y = [1.0 if rng.random() < q else 0.0 for q in p]
    slope, intercept = logistic_recalibration(p, y)
    assert slope == pytest.approx(1.0, abs=0.05) and intercept == pytest.approx(0.0, abs=0.05)
    assert logistic_recalibration([], []) == (1.0, 0.0)
    # an overconfident model gets a slope below one
    z = [min(max(0.5 + 2 * (q - 0.5), 0.01), 0.99) for q in p]
    over_slope, _ = logistic_recalibration(z, y)
    assert over_slope < 0.8
    assert logistic_recalibration(p, y) == (slope, intercept)


# timing ------------------------------------------------------------------------------


def test_bootstrap_and_permutation_fit_the_time_budget() -> None:
    rng = random.Random(31)
    bets = _bets(rng, 1000, 5.0, 150.0)
    start = time.perf_counter()
    bet_bootstrap(bets, 1000, random.Random("t:boot"))
    drawdown_bootstrap([[rng.gauss(0, 100) for _ in range(250)] for _ in range(4)], 1000, random.Random("t:dd"))
    assert time.perf_counter() - start < 2.0
    d = [rng.gauss(0.001, 0.03) for _ in range(1000)]
    start = time.perf_counter()
    permutation_market_test(d, 10_000, random.Random("t:perm"))
    assert time.perf_counter() - start < 3.0
    assert math.isfinite(sum(d))
