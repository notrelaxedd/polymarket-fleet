"""Small deterministic statistics for the host: the bootstrap behind the paper CLV
gate (docs/ROBUSTNESS.md A2) and the shrunk ROI the leaderboard ranks on.

Everything is seeded from a string so a recompute gives the same interval; no
randomness is drawn from the clock.
"""
from __future__ import annotations

import random
from typing import Any, Callable, Sequence

B_DEFAULT = 1000
CI_LOW, CI_HIGH = 5.0, 95.0


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """The q-th percentile (0..100) of already sorted values, linear interpolation
    between order statistics (the numpy default)."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("no values")
    if n == 1:
        return float(sorted_values[0])
    position = (q / 100.0) * (n - 1)
    low = int(position)
    high = min(low + 1, n - 1)
    weight = position - low
    return float(sorted_values[low]) * (1.0 - weight) + float(sorted_values[high]) * weight


def weighted_mean(values: Sequence[float], weights: Sequence[float] | None = None) -> float:
    """Mean of values, weighted when weights are given (equal weights otherwise)."""
    if not values:
        return 0.0
    if weights is None:
        return sum(values) / len(values)
    total = sum(weights)
    if total <= 0:
        return sum(values) / len(values)
    return sum(v * w for v, w in zip(values, weights)) / total


def bootstrap_ci(
    values: Sequence[float],
    seed: str,
    weights: Sequence[float] | None = None,
    b: int = B_DEFAULT,
    statistic: Callable[[Sequence[float], Sequence[float] | None], float] = weighted_mean,
) -> list[float] | None:
    """[5th, 95th] percentile of `statistic` over `b` resamples of values (with their
    weights) drawn with replacement by `random.Random(f"{seed}:boot")`; None without
    values."""
    n = len(values)
    if n == 0:
        return None
    rng = random.Random(f"{seed}:boot")
    draws = []
    for _ in range(b):
        picks = [rng.randrange(n) for _ in range(n)]
        sample = [values[i] for i in picks]
        sample_weights = None if weights is None else [weights[i] for i in picks]
        draws.append(statistic(sample, sample_weights))
    draws.sort()
    return [percentile(draws, CI_LOW), percentile(draws, CI_HIGH)]


def shrunk_roi(metrics: dict[str, Any] | None) -> float:
    """roi * n_bets / (n_bets + 100); the stored `shrunk_roi` when the metrics carry
    one; 0 without usable metrics."""
    if not isinstance(metrics, dict):
        return 0.0
    stored = metrics.get("shrunk_roi")
    if isinstance(stored, (int, float)) and not isinstance(stored, bool):
        return float(stored)
    try:
        n = float(metrics.get("n_bets") or 0)
        return float(metrics.get("roi") or 0.0) * n / (n + 100.0)
    except (TypeError, ValueError):
        return 0.0
