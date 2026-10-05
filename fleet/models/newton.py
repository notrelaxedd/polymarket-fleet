"""Newton's method for an L2-penalised logistic regression with k coefficients.

logit(p_i) = sum_j w_j * x_ij, where the caller includes a constant 1.0 column for an
intercept. Loss = sum_i [-y ln p - (1 - y) ln(1 - p)] + l2 * sum over penalised j of
w_j^2 (every coefficient except those listed in `unpenalised`, typically the
intercept). Deterministic: a given start (zeros by default), at most `max_iter` full
Newton steps with step halving when a step does not reduce the loss, stopping when the
step or the improvement is below `tol`. Used by epa_blend (step 6B) and ingame_wp
(step 6C); fleet/models/blend_fit.py keeps its own three-coefficient solver.
"""

from __future__ import annotations

import math
from typing import Sequence

from fleet.sim.odds import expit

LOG_EPS = 1e-12
PIVOT_EPS = 1e-14


def loss(xs: Sequence[Sequence[float]], ys: Sequence[float], w: Sequence[float], l2: float,
         unpenalised: frozenset[int] = frozenset()) -> float:
    """The penalised log-loss of coefficients `w`."""
    total = l2 * sum(v * v for j, v in enumerate(w) if j not in unpenalised)
    for x, y in zip(xs, ys):
        p = expit(sum(wj * xj for wj, xj in zip(w, x)))
        p = min(max(p, LOG_EPS), 1.0 - LOG_EPS)
        total -= y * math.log(p) + (1.0 - y) * math.log(1.0 - p)
    return total


def gradient_hessian(xs: Sequence[Sequence[float]], ys: Sequence[float], w: Sequence[float], l2: float,
                     unpenalised: frozenset[int] = frozenset()) -> tuple[list[float], list[list[float]]]:
    """Gradient and Hessian of `loss` at `w`."""
    k = len(w)
    g = [0.0 if j in unpenalised else 2.0 * l2 * w[j] for j in range(k)]
    h = [[0.0] * k for _ in range(k)]
    for j in range(k):
        if j not in unpenalised:
            h[j][j] = 2.0 * l2
    for x, y in zip(xs, ys):
        p = expit(sum(wj * xj for wj, xj in zip(w, x)))
        r = p - y
        s = p * (1.0 - p)
        for a in range(k):
            xa = x[a]
            if xa == 0.0:
                continue
            g[a] += r * xa
            row = h[a]
            for b in range(a, k):
                row[b] += s * xa * x[b]
    for a in range(k):
        for b in range(a):
            h[a][b] = h[b][a]
    return g, h


def solve(h: list[list[float]], g: list[float]) -> list[float] | None:
    """Solve h x = g by Gaussian elimination with partial pivoting; None when singular."""
    k = len(g)
    m = [row[:] + [g[i]] for i, row in enumerate(h)]
    for col in range(k):
        pivot = max(range(col, k), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < PIVOT_EPS:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(k):
            if r == col:
                continue
            f = m[r][col] / m[col][col]
            if f == 0.0:
                continue
            for c in range(col, k + 1):
                m[r][c] -= f * m[col][c]
    return [m[i][k] / m[i][i] for i in range(k)]


def fit_logistic(xs: Sequence[Sequence[float]], ys: Sequence[float], l2: float = 1e-3,
                 start: Sequence[float] | None = None, unpenalised: Sequence[int] = (),
                 max_iter: int = 50, tol: float = 1e-9) -> list[float]:
    """The coefficients minimising the penalised log-loss; the start when there are no
    rows or the Hessian is singular at the start."""
    if xs and any(len(x) != len(xs[0]) for x in xs):
        raise ValueError("every row must have the same number of features")
    k = len(xs[0]) if xs else len(start or ())
    w = list(start) if start is not None else [0.0] * k
    if len(w) != k:
        raise ValueError(f"start has {len(w)} coefficients for {k} features")
    if not xs:
        return w
    free = frozenset(int(j) for j in unpenalised)
    current = loss(xs, ys, w, l2, free)
    for _ in range(max_iter):
        g, h = gradient_hessian(xs, ys, w, l2, free)
        step = solve(h, g)
        if step is None:
            break
        t = 1.0
        improved = False
        for _half in range(30):
            trial = [wj - t * sj for wj, sj in zip(w, step)]
            trial_loss = loss(xs, ys, trial, l2, free)
            if trial_loss <= current:
                improved = True
                break
            t *= 0.5
        if not improved:
            break
        moved = t * max(abs(s) for s in step)
        gain = current - trial_loss
        w, current = trial, trial_loss
        if moved < tol or gain < tol:
            break
    return w
