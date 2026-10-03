"""Training (docs/MODELS.md, "Training"): replay Elo through a (season, week) point with
the parent model's params, fit the blend on the moneyline games before it and return the
child model's artifact. Unit = one season of replay: the checkpoint records progress and
a resumed job replays from the first season again (one pass, well under a second).
"""

from __future__ import annotations

from typing import Any, Callable

from fleet.models.registry import get_family

Emit = Callable[[dict[str, Any], float], None]
ShouldStop = Callable[[], bool]


def parse_through(through: Any) -> tuple[int, int]:
    if isinstance(through, dict):
        return int(through["season"]), int(through["week"])
    if isinstance(through, (list, tuple)) and len(through) == 2:
        return int(through[0]), int(through[1])
    raise ValueError("through must be {'season': int, 'week': int}")


def run_train(games: list[dict[str, Any]], model: dict[str, Any], through: Any, emit: Emit,
              should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    """model is the parent row from the job context ({id, family, params, ...})."""
    if not model:
        raise ValueError("train needs the parent model in the job context")
    family = str(model["family"])
    params = dict(model.get("params") or {})
    point = parse_through(through)
    subset = [g for g in games if (g["season"], g["week"]) <= point]
    seasons = sorted({g["season"] for g in subset})
    instance = get_family(family)(params)
    done = {"count": 0}

    def on_season(season: int) -> None:
        done["count"] += 1
        emit({"next": done["count"], "seasons": seasons, "season": season}, done["count"] / max(1, len(seasons)))

    instance.fit(subset, point, should_stop, on_season)
    artifact = instance.to_json()
    artifact["through"] = list(point)
    child = {
        "family": family,
        "params": params,  # the parent's, verbatim: the child keeps its identity (params_hash)
        "artifact": artifact,
        "parent_model_id": model.get("id"),
        "trained_through": list(point),
    }
    return {"create_models": [child], "through": list(point), "games_seen": artifact["games_seen"]}
