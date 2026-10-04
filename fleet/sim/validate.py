"""The validate job (docs/ROBUSTNESS.md, A1-A3): a validation-era backtest with the
resampling fields, then the stress tests, for one model.

Units: one test season of one backtest run, the base run first and then the ten
neighbourhood runs (params perturbed by random.Random(f"{seed}:nbhd:{i}") and clipped
to the search space). The price stress and the regimes come from the base run's
records (fleet.sim.stress). Checkpoint: {"stage": run index (0 = base), "base": the
base run's per-season entries once finished, "runs": the finished neighbourhood
summaries, "current": the running backtest's own checkpoint}; a resumed job finishes
with exactly the result of an uninterrupted one.
"""

from __future__ import annotations

import random
from typing import Any, Callable

from fleet.models.registry import get_family
from fleet.models.search_space import perturb_params
from fleet.sim.backtest import ERA_VALIDATION, assemble, records_of, run_seasons, season_plan
from fleet.sim.metrics import merge_stats, metrics_from_stats, shrunk_roi
from fleet.sim.records import mean_ll_gain
from fleet.sim.robust import overfit_flags
from fleet.sim.stress import NEIGHBOURHOOD_N, neighbourhood_summary, price_stress, regime_table, stress_flags

Emit = Callable[[dict[str, Any], float], None]
ShouldStop = Callable[[], bool]


def neighbourhood_params(family: str, params: dict[str, Any], seed: int | str, index: int) -> dict[str, Any]:
    return perturb_params(family, params, random.Random(f"{seed}:nbhd:{index}"))


def _run_summary(per_season: list[dict[str, Any]], limits: dict[str, Any]) -> dict[str, Any]:
    """{"shrunk_roi", "mean_ll_gain"} of a finished neighbourhood run."""
    metrics = metrics_from_stats(merge_stats([e["stats"] for e in per_season]), limits, [e["season"] for e in per_season])
    records = [r for season in records_of(per_season) for r in season]
    return {"shrunk_roi": shrunk_roi(metrics), "mean_ll_gain": mean_ll_gain(records)}


def _resume(checkpoint: dict[str, Any] | None) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]], dict[str, Any] | None]:
    if not checkpoint or not isinstance(checkpoint.get("runs"), list):
        return None, [], None
    base = checkpoint.get("base")
    runs = [r for r in checkpoint["runs"] if isinstance(r, dict)]
    current = checkpoint.get("current") if isinstance(checkpoint.get("current"), dict) else None
    return (base if isinstance(base, list) else None), runs, current


def run_validate(games: list[dict[str, Any]], family: str, params: dict[str, Any],
                 validation_seasons: list[int | None] | None, limits: dict[str, Any], seed: int,
                 emit: Emit, should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None,
                 search_metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    """{"validation_metrics", "stress_metrics"} for the model (family, params) on the
    validation era; search_metrics (the search-era backtest) feeds the overfit flag."""
    full_params = get_family(family)(params).params
    plan = season_plan(games, validation_seasons)
    n_seasons = max(1, len(plan))
    units = n_seasons * (1 + NEIGHBOURHOOD_N)
    base, runs, current = _resume(checkpoint)

    def stage_emit(stage: int, cp: dict[str, Any], _progress: float) -> None:
        done = stage * n_seasons + int(cp.get("next", 0))
        emit({"stage": stage, "base": base, "runs": runs, "current": cp}, done / units)

    if base is None:
        base = run_seasons(games, family, full_params, validation_seasons, limits,
                           lambda cp, p: stage_emit(0, cp, p), should_stop, current)
        current = None
        if plan:
            emit({"stage": 1, "base": base, "runs": runs, "current": {}}, n_seasons / units)
    for index in range(len(runs), NEIGHBOURHOOD_N):
        stage = index + 1
        perturbed = neighbourhood_params(family, full_params, seed, index)
        per_season = run_seasons(games, family, perturbed, validation_seasons, limits,
                                 lambda cp, p: stage_emit(stage, cp, p), should_stop, current)
        current = None
        runs.append(_run_summary(per_season, limits))
        if plan:
            emit({"stage": stage + 1, "base": base, "runs": runs, "current": {}}, (stage + 1) * n_seasons / units)

    validation_metrics = assemble(base, limits, ERA_VALIDATION, seed)
    validation_metrics["flags"] = overfit_flags(search_metrics, validation_metrics)
    records = [r for season in records_of(base) for r in season]
    prices = price_stress(records, full_params, limits)
    neighbourhood = neighbourhood_summary(runs)
    regimes = regime_table(records, limits)
    stress_metrics = {
        "prices": prices,
        "neighbourhood": neighbourhood,
        "regimes": regimes,
        "flags": stress_flags(validation_metrics, prices, neighbourhood, regimes),
        "seed": int(seed),
    }
    return {"validation_metrics": validation_metrics, "stress_metrics": stress_metrics}
