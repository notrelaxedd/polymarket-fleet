"""fleet.models.newton: k-coefficient L2 logistic regression by Newton's method."""
from __future__ import annotations

import math
import random

from fleet.models import newton


def _synthetic(n: int, coef: list[float], seed: int) -> tuple[list[list[float]], list[float]]:
    rng = random.Random(seed)
    xs, ys = [], []
    for _ in range(n):
        x = [1.0] + [rng.gauss(0.0, 1.0) for _ in coef[1:]]
        p = 1.0 / (1.0 + math.exp(-sum(c * v for c, v in zip(coef, x))))
        xs.append(x)
        ys.append(1.0 if rng.random() < p else 0.0)
    return xs, ys


def test_recovers_known_coefficients() -> None:
    truth = [0.3, 1.2, -0.8, 0.5]
    xs, ys = _synthetic(6000, truth, 7)
    fit = newton.fit_logistic(xs, ys, l2=1e-3, unpenalised=[0])
    assert all(abs(a - b) < 0.12 for a, b in zip(fit, truth)), fit


def test_l2_shrinks_and_fit_is_deterministic() -> None:
    xs, ys = _synthetic(500, [0.0, 2.0], 3)
    loose = newton.fit_logistic(xs, ys, l2=1e-6)
    tight = newton.fit_logistic(xs, ys, l2=50.0)
    assert abs(tight[1]) < abs(loose[1])
    assert newton.fit_logistic(xs, ys, l2=1e-6) == loose


def test_empty_and_singular_inputs_return_the_start() -> None:
    assert newton.fit_logistic([], [], start=[1.0, 2.0]) == [1.0, 2.0]
    assert newton.fit_logistic([[0.0, 0.0]] * 3, [1.0, 0.0, 1.0], l2=0.0) == [0.0, 0.0]
