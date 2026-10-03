"""Odds conversion and probability helpers."""

from __future__ import annotations

import math

EPS = 1e-6


def american_to_implied(odds: int | float | None) -> float | None:
    """American odds to the implied probability (with the vig still in it)."""
    if odds is None:
        return None
    o = float(odds)
    if o < 0:
        return -o / (-o + 100.0)
    return 100.0 / (o + 100.0)


def devig(home_ml: int | float | None, away_ml: int | float | None) -> float | None:
    """Vig-free market probability of a home win, None when a moneyline is missing."""
    if home_ml is None or away_ml is None:
        return None
    imp_home = american_to_implied(home_ml)
    imp_away = american_to_implied(away_ml)
    assert imp_home is not None and imp_away is not None
    total = imp_home + imp_away
    if total <= 0:
        return None
    return imp_home / total


def clamp_prob(p: float) -> float:
    """Keep a probability strictly inside (0, 1) so logs and logits stay finite."""
    if p < EPS:
        return EPS
    if p > 1.0 - EPS:
        return 1.0 - EPS
    return p


def logit(p: float) -> float:
    p = clamp_prob(p)
    return math.log(p / (1.0 - p))


def expit(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)
