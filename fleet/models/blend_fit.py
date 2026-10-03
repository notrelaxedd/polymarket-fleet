"""Newton's method for the three-coefficient logistic blend
logit(p) = a * x1 + b * x2 + c on the log-loss, with an L2 penalty on a and b.

Loss = sum_i [-y ln p - (1 - y) ln(1 - p)] + L2 * (a^2 + b^2). Deterministic: a fixed
start (a 0, b 1, c 0), at most MAX_ITER full Newton steps with step halving when a step
does not reduce the loss, stopping when the step or the improvement is below TOL.
"""

from __future__ import annotations

import math

from fleet.sim.odds import expit

L2 = 1e-3
MAX_ITER = 25
TOL = 1e-8
START = (0.0, 1.0, 0.0)
LOG_EPS = 1e-12

Rows = list[tuple[float, float, float]]  # (x1, x2, y)


def blend_loss(rows: Rows, a: float, b: float, c: float) -> float:
    total = L2 * (a * a + b * b)
    for x1, x2, y in rows:
        p = expit(a * x1 + b * x2 + c)
        p = min(max(p, LOG_EPS), 1.0 - LOG_EPS)
        total -= y * math.log(p) + (1.0 - y) * math.log(1.0 - p)
    return total


def _gradient_hessian(rows: Rows, a: float, b: float, c: float) -> tuple[list[float], list[list[float]]]:
    g0 = 2.0 * L2 * a
    g1 = 2.0 * L2 * b
    g2 = 0.0
    h00 = 2.0 * L2
    h11 = 2.0 * L2
    h01 = h02 = h12 = h22 = 0.0
    for x1, x2, y in rows:
        p = expit(a * x1 + b * x2 + c)
        r = p - y
        w = p * (1.0 - p)
        g0 += r * x1
        g1 += r * x2
        g2 += r
        h00 += w * x1 * x1
        h01 += w * x1 * x2
        h02 += w * x1
        h11 += w * x2 * x2
        h12 += w * x2
        h22 += w
    return [g0, g1, g2], [[h00, h01, h02], [h01, h11, h12], [h02, h12, h22]]


def solve3(h: list[list[float]], g: list[float]) -> list[float] | None:
    """Solve h x = g by Gaussian elimination with partial pivoting; None when singular."""
    m = [row[:] + [g[i]] for i, row in enumerate(h)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-14:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(3):
            if r == col:
                continue
            f = m[r][col] / m[col][col]
            for k in range(col, 4):
                m[r][k] -= f * m[col][k]
    return [m[i][3] / m[i][i] for i in range(3)]


def fit_blend(rows: Rows) -> tuple[float, float, float]:
    """Return (a, b, c); the start point when there is nothing to fit."""
    a, b, c = START
    if not rows:
        return a, b, c
    loss = blend_loss(rows, a, b, c)
    for _ in range(MAX_ITER):
        g, h = _gradient_hessian(rows, a, b, c)
        step = solve3(h, g)
        if step is None:
            break
        t = 1.0
        improved = False
        for _half in range(30):
            na, nb, nc = a - t * step[0], b - t * step[1], c - t * step[2]
            new_loss = blend_loss(rows, na, nb, nc)
            if new_loss <= loss:
                improved = True
                break
            t *= 0.5
        if not improved:
            break
        moved = t * max(abs(step[0]), abs(step[1]), abs(step[2]))
        gain = loss - new_loss
        a, b, c, loss = na, nb, nc, new_loss
        if moved < TOL or gain < TOL:
            break
    return a, b, c
