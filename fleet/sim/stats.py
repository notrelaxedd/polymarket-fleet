"""Resampling statistics for a backtest (docs/ROBUSTNESS.md, A2): bootstrap
confidence intervals, the sign-flip market test, the Brier decomposition and a
logistic recalibration fit. Pure Python, deterministic for a given random.Random.

Speed notes. The bet bootstrap resamples a list of tuples once per draw and sums the
columns with zip, so 1000 bets x 1000 draws takes well under a second. The permutation
test precomputes, for every block of 8 games, the sum of d over each of the 256 sign
subsets of the block; one flip is then one random byte per block (rng.randbytes) and
one table lookup each, so 10 000 flips over 1000 games take a fraction of a second.
"""

from __future__ import annotations

import math
import random
from typing import Any, Callable, Sequence

B_DEFAULT = 1000
N_FLIPS_DEFAULT = 10_000
CI_LOW = 0.05
CI_HIGH = 0.95
BLOCK_BITS = 8
LOG_EPS = 1e-12
RECAL_MAX_ITER = 50
RECAL_TOL = 1e-10


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (q in [0, 1]) of an unsorted sequence; 0 when empty."""
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def ci_bounds(draws: Sequence[float]) -> list[float]:
    return [percentile(draws, CI_LOW), percentile(draws, CI_HIGH)]


def bootstrap_ci(values: Sequence[Any], stat: Callable[[list[Any]], float], B: int, rng: random.Random) -> list[float]:
    """[5th, 95th] percentile of stat over B resamples (with replacement) of values.
    An empty list gives [stat([]), stat([])] without drawing."""
    if not values:
        point = float(stat([]))
        return [point, point]
    n = len(values)
    draws = [float(stat(rng.choices(values, k=n))) for _ in range(B)]
    return ci_bounds(draws)


def bet_bootstrap(bets: Sequence[tuple[float, float, float, float, float]], B: int,
                  rng: random.Random) -> dict[str, list[float]]:
    """CI of roi, hit_rate, avg_edge and avg_clv from one joint resample per draw.

    bets are (pnl_cents, stake_cents, hit, edge, clv) tuples; roi is sum pnl / sum stake
    of the resample (0 when the resampled stake is 0)."""
    if not bets:
        return {"roi": [0.0, 0.0], "hit_rate": [0.0, 0.0], "avg_edge": [0.0, 0.0], "avg_clv": [0.0, 0.0]}
    n = len(bets)
    rois: list[float] = []
    hits: list[float] = []
    edges: list[float] = []
    clvs: list[float] = []
    for _ in range(B):
        pnl, stake, hit, edge, clv = (sum(col) for col in zip(*rng.choices(bets, k=n)))
        rois.append(pnl / stake if stake else 0.0)
        hits.append(hit / n)
        edges.append(edge / n)
        clvs.append(clv / n)
    return {"roi": ci_bounds(rois), "hit_rate": ci_bounds(hits), "avg_edge": ci_bounds(edges), "avg_clv": ci_bounds(clvs)}


def max_drawdown(pnls: Sequence[float]) -> float:
    """Largest peak-to-trough fall of the cumulative series (the start is a peak of 0)."""
    peak = 0.0
    cum = 0.0
    worst = 0.0
    for pnl in pnls:
        cum += pnl
        if cum > peak:
            peak = cum
        elif peak - cum > worst:
            worst = peak - cum
    return worst


def drawdown_bootstrap(season_pnls: Sequence[Sequence[float]], B: int, rng: random.Random) -> list[float]:
    """Season-block bootstrap of the max drawdown: resample whole seasons with
    replacement, concatenate their pnl sequences in draw order, take the drawdown."""
    blocks = [list(s) for s in season_pnls]
    if not blocks:
        return [0.0, 0.0]
    draws = []
    for _ in range(B):
        series: list[float] = []
        for block in rng.choices(blocks, k=len(blocks)):
            series.extend(block)
        draws.append(max_drawdown(series))
    return ci_bounds(draws)


def _subset_tables(d: Sequence[float]) -> list[list[float]]:
    """Per block of BLOCK_BITS games, the sum of d over the games whose bit is set, for
    every byte value (bit j of the byte = game j of the block)."""
    tables: list[list[float]] = []
    size = 1 << BLOCK_BITS
    for start in range(0, len(d), BLOCK_BITS):
        block = list(d[start:start + BLOCK_BITS]) + [0.0] * BLOCK_BITS  # a short last block pads with 0
        table = [0.0] * size
        for value in range(1, size):
            low = value & -value  # the lowest set bit
            table[value] = table[value ^ low] + block[low.bit_length() - 1]
        tables.append(table)
    return tables


def permutation_market_test(d: Sequence[float], n_flips: int, rng: random.Random) -> tuple[float, float]:
    """(mean_gain, p): the sign-flip permutation p-value for mean(d) > 0.

    p = (1 + number of flips whose mean is >= the observed mean) / (1 + n_flips). With
    d all zero (a model identical to the market) every flip ties the observed mean, so
    p is 1.0 by convention; an empty d gives (0.0, 1.0)."""
    n = len(d)
    if n == 0:
        return 0.0, 1.0
    total = math.fsum(d)
    tables = _subset_tables(list(d))
    n_bytes = len(tables)
    # a flip keeps the sign where the bit is set: flipped sum = 2 * subset - total >= total
    threshold = total - 1e-9 * max(1.0, abs(total))
    hits = 0
    for _ in range(n_flips):
        subset = 0.0
        for table, byte in zip(tables, rng.randbytes(n_bytes)):
            subset += table[byte]
        if subset >= threshold:
            hits += 1
    return total / n, (1 + hits) / (1 + n_flips)


def brier_decomposition(p: Sequence[float], outcomes: Sequence[float], buckets: int = 10) -> dict[str, float]:
    """Murphy's decomposition over equal-width forecast buckets, with the two
    within-bucket terms of Stephenson, Coelho and Jolliffe (2008) so that

        brier = reliability - resolution + uncertainty + within_variance - within_covariance

    holds exactly for any forecasts. reliability = mean over games of (bucket mean
    forecast - bucket mean outcome)^2, resolution = mean of (bucket mean outcome -
    overall mean outcome)^2, uncertainty = the variance of the outcomes (ties count
    0.5), within_variance = mean of (forecast - bucket mean forecast)^2 and
    within_covariance = 2 * mean of (outcome - bucket mean outcome) * (forecast -
    bucket mean forecast). Both within terms are 0 when the forecasts inside a bucket
    are equal."""
    n = len(p)
    zero = {"reliability": 0.0, "resolution": 0.0, "uncertainty": 0.0, "within_variance": 0.0, "within_covariance": 0.0}
    if n == 0:
        return zero
    keys = [min(int(pi * buckets), buckets - 1) for pi in p]
    counts = [0] * buckets
    sum_p = [0.0] * buckets
    sum_y = [0.0] * buckets
    for k, pi, yi in zip(keys, p, outcomes):
        counts[k] += 1
        sum_p[k] += pi
        sum_y[k] += yi
    mean_p = [sp / c if c else 0.0 for sp, c in zip(sum_p, counts)]
    mean_y = [sy / c if c else 0.0 for sy, c in zip(sum_y, counts)]
    o_bar = sum(outcomes) / n
    reliability = math.fsum(c * (mean_p[k] - mean_y[k]) ** 2 for k, c in enumerate(counts) if c)
    resolution = math.fsum(c * (mean_y[k] - o_bar) ** 2 for k, c in enumerate(counts) if c)
    within_variance = math.fsum((pi - mean_p[k]) ** 2 for k, pi in zip(keys, p))
    within_covariance = 2.0 * math.fsum((yi - mean_y[k]) * (pi - mean_p[k]) for k, pi, yi in zip(keys, p, outcomes))
    uncertainty = math.fsum((yi - o_bar) ** 2 for yi in outcomes) / n
    return {
        "reliability": reliability / n, "resolution": resolution / n, "uncertainty": uncertainty,
        "within_variance": within_variance / n, "within_covariance": within_covariance / n,
    }


def _logit(p: float) -> float:
    p = min(max(p, LOG_EPS), 1.0 - LOG_EPS)
    return math.log(p / (1.0 - p))


def _expit(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _recal_loss(rows: list[tuple[float, float]], slope: float, intercept: float) -> float:
    total = 0.0
    for x, y in rows:
        q = min(max(_expit(slope * x + intercept), LOG_EPS), 1.0 - LOG_EPS)
        total -= y * math.log(q) + (1.0 - y) * math.log(1.0 - q)
    return total


def logistic_recalibration(p: Sequence[float], outcomes: Sequence[float]) -> tuple[float, float]:
    """(slope, intercept) of the fit outcome ~ expit(slope * logit(p) + intercept) by
    Newton's method with step halving from (1, 0). A calibrated model gives (1, 0);
    (1, 0) is returned when there is nothing to fit or the Hessian is singular."""
    rows = [(_logit(pi), float(yi)) for pi, yi in zip(p, outcomes)]
    slope, intercept = 1.0, 0.0
    if not rows:
        return slope, intercept
    loss = _recal_loss(rows, slope, intercept)
    for _ in range(RECAL_MAX_ITER):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in rows:
            q = _expit(slope * x + intercept)
            r = q - y
            w = q * (1.0 - q)
            g0 += r * x
            g1 += r
            h00 += w * x * x
            h01 += w * x
            h11 += w
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-14:
            break
        step0 = (h11 * g0 - h01 * g1) / det
        step1 = (h00 * g1 - h01 * g0) / det
        t = 1.0
        improved = False
        for _half in range(30):
            ns, ni = slope - t * step0, intercept - t * step1
            new_loss = _recal_loss(rows, ns, ni)
            if new_loss <= loss:
                improved = True
                break
            t *= 0.5
        if not improved:
            break
        moved = t * max(abs(step0), abs(step1))
        gain = loss - new_loss
        slope, intercept, loss = ns, ni, new_loss
        if moved < RECAL_TOL or gain < RECAL_TOL:
            break
    return slope, intercept
