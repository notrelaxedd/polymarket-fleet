"""Search-space bounds per family and the parameter perturbation used by the
neighbourhood stress test (docs/ROBUSTNESS.md, A3).

A family may also carry its own SEARCH_BOUNDS ({name: (low, high)}) and FIXED_PARAMS
(names never perturbed) class attributes; they are merged over the tables here, so a
family added later needs no change in this module.
"""

from __future__ import annotations

import random
from typing import Any

from fleet.models.registry import get_family

BOUNDS: dict[str, dict[str, tuple[float, float]]] = {
    "elo_blend": {
        "k": (10.0, 40.0),
        "hfa": (20.0, 90.0),
        "regress": (0.1, 0.6),
        "rest_per_day": (0.0, 4.0),
        "min_edge": (0.01, 0.08),
        "kelly_fraction": (0.1, 0.5),
        "qb_change_penalty": (0.0, 80.0),
        "out_penalty_per_player": (0.0, 15.0),
    },
}
FIXED: dict[str, tuple[str, ...]] = {"elo_blend": ("mov_scale",)}
PERTURB_LOW = 0.9
PERTURB_HIGH = 1.1


def bounds_of(family: str) -> dict[str, tuple[float, float]]:
    cls = get_family(family)
    merged = dict(BOUNDS.get(family, {}))
    merged.update({k: (float(v[0]), float(v[1])) for k, v in (getattr(cls, "SEARCH_BOUNDS", None) or {}).items()})
    return merged


def fixed_of(family: str) -> set[str]:
    cls = get_family(family)
    return set(FIXED.get(family, ())) | set(getattr(cls, "FIXED_PARAMS", None) or ())


def clip_params(family: str, params: dict[str, Any]) -> dict[str, Any]:
    """params with every bounded numeric value clipped into its search-space range."""
    bounds = bounds_of(family)
    out = dict(params)
    for name, (low, high) in bounds.items():
        value = out.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out[name] = min(max(float(value), low), high)
    return out


def perturb_params(family: str, params: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """Every numeric param (except the family's fixed ones) scaled by a uniform factor
    in [0.9, 1.1], drawn in sorted key order, then clipped to the search space."""
    fixed = fixed_of(family)
    out = dict(params)
    for name in sorted(params):
        value = params[name]
        if name in fixed or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out[name] = round(float(value) * (PERTURB_LOW + (PERTURB_HIGH - PERTURB_LOW) * rng.random()), 6)
    return clip_params(family, out)
