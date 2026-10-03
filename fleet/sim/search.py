"""Random model search (docs/MODELS.md, "Model search").

Candidate i draws its params from random.Random(f"{seed}:{i}"), runs the full
walk-forward backtest (unit = candidate x test season) and competes for the top list on
shrunk ROI, ties broken by lower log-loss.
"""

from __future__ import annotations

import random
from typing import Any, Callable

from fleet.models.base import params_hash
from fleet.models.registry import get_family
from fleet.sim.backtest import run_backtest, season_plan
from fleet.sim.metrics import shrunk_roi

Emit = Callable[[dict[str, Any], float], None]
ShouldStop = Callable[[], bool]


def candidate_params(family: str, seed: int, index: int) -> dict[str, Any]:
    return get_family(family).search_space(random.Random(f"{seed}:{index}"))


def rank_key(entry: dict[str, Any]) -> tuple[float, float, int]:
    """Shrunk ROI descending, then log-loss ascending, then candidate index."""
    return (-float(entry["score"]), float(entry["metrics"].get("log_loss", 0.0)), int(entry["index"]))


def insert_top(top: list[dict[str, Any]], entry: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
    ranked = sorted(top + [entry], key=rank_key)
    return ranked[:top_k]


def _whole_metrics(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k != "per_season"}


def run_search(games: list[dict[str, Any]], family: str, n: int, seed: int,
               seasons: list[int | None] | None, top_k: int, limits: dict[str, Any],
               emit: Emit, should_stop: ShouldStop,
               checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    family_cls = get_family(family)
    n = max(0, int(n))
    top_k = max(1, int(top_k))
    plan = season_plan(games, seasons)
    units = max(1, n * max(1, len(plan)))
    top: list[dict[str, Any]] = []
    evaluated = 0
    current: dict[str, Any] | None = None
    if checkpoint:
        top = list(checkpoint.get("top") or [])
        evaluated = min(n, int(checkpoint.get("evaluated") or 0))
        nxt = checkpoint.get("next") or [evaluated, 0]
        if int(nxt[0]) == evaluated:
            current = checkpoint.get("current") or None

    for i in range(evaluated, n):
        params = candidate_params(family, seed, i)

        def inner_emit(cp: dict[str, Any], _progress: float, i: int = i) -> None:
            if cp["next"] >= len(plan):
                return  # the candidate-complete checkpoint below covers the last season
            emit({"next": [i, cp["next"]], "current": cp, "top": top, "evaluated": i},
                 (i * len(plan) + cp["next"]) / units)

        result = run_backtest(games, family, params, seasons, limits, inner_emit, should_stop, current)
        current = None
        metrics = _whole_metrics(result)
        entry = {"index": i, "params": params, "params_hash": params_hash(params),
                 "score": shrunk_roi(metrics), "metrics": metrics}
        top = insert_top(top, entry, top_k)
        evaluated = i + 1
        emit({"next": [evaluated, 0], "current": {}, "top": top, "evaluated": evaluated},
             (evaluated * len(plan)) / units)

    create_models = [
        {"family": family, "params": e["params"], "artifact": None, "backtest_metrics": e["metrics"],
         "summary": family_cls.summary(e["params"], e["metrics"]), "trained_through": None}
        for e in top
    ]
    return {"evaluated": n, "seasons": plan, "top": top, "create_models": create_models}
