"""Search and validation of the ingame_wp family on play-by-play rows (contract section 4).

run_ingame_search fits each candidate on the train seasons only, ranks the candidates
without looking at the validation era, and reports the validation numbers of the kept
ones:

- Fit set: the plays of the train-season games whose game_id hashes below
  train_fraction (blake2b of "ingame-fit:<game_id>", so the split is the same in every
  search and on every resume). Their features are built once (scales 1) and rescaled
  per candidate (fleet.models.ingame_wp.scale_features).
- Selection: the log-loss on the other train-season games (out of sample, whole games),
  ties broken by the candidate index. With train_fraction 1 there is no selection set and
  the ranking is the candidate order.
- Validation: fleet.sim.ingame_eval on the validation seasons, which must come strictly
  after the train seasons; the validation rows never reach a fit or the ranking.

Candidate i draws its params from IngameWP.search_space(random.Random(f"{seed}:{i}"))
unless a params grid is given. Rows come from rows_iter_factory(), called once for the fit
set and once per candidate (one lazy pass for selection and validation). Checkpoint after
every candidate: {"evaluated": i, "top": [entries with their artifacts]}; a resumed
search rebuilds the fit set (deterministic) and continues at candidate i.
"""

from __future__ import annotations

import hashlib
import math
import random
from array import array
from typing import Any, Callable, Iterable, Sequence

from fleet.models.base import params_hash
from fleet.models.ingame_wp import FAMILY, FEATURE_NAMES, IngameWP, feature_vector, scale_features, state_from_row
from fleet.sim.control import check_stop
from fleet.sim.ingame_eval import Accumulator
from fleet.sim.metrics import log_loss

Emit = Callable[[dict[str, Any], float], None]
ShouldStop = Callable[[], bool]
RowsFactory = Callable[[], Iterable[dict[str, Any]]]
SeasonRange = Sequence[int | None]

DEFAULT_N = 20
DEFAULT_TOP_K = 5
DEFAULT_TRAIN_FRACTION = 0.3
DEFAULT_TRAIN_SEASONS: list[int | None] = [2012, 2021]
DEFAULT_VALIDATION_SEASONS: list[int | None] = [2022, None]
STOP_EVERY = 20000
K = len(FEATURE_NAMES)


def season_pair(value: Any, default: SeasonRange | None = None) -> list[int | None] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return [None if v is None else int(v) for v in value]
    return None if default is None else list(default)


def in_seasons(season: Any, pair: SeasonRange) -> bool:
    if not isinstance(season, int) or isinstance(season, bool):
        return False
    first, last = pair
    return (first is None or season >= first) and (last is None or season <= last)


def check_eras(train: SeasonRange, validation: SeasonRange) -> None:
    """The train seasons must end before the validation seasons start."""
    if train[1] is None or validation[0] is None or int(train[1]) >= int(validation[0]):
        raise ValueError(f"train seasons {list(train)} must end before validation seasons {list(validation)} start")


def in_fit_set(game_id: Any, fraction: float) -> bool:
    if fraction >= 1.0:
        return True
    digest = hashlib.blake2b(f"ingame-fit:{game_id}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2.0 ** 64 < fraction


class FitSet:
    """Base feature rows (flat array, K per row) and outcomes of the fit plays."""

    def __init__(self) -> None:
        self.x = array("d")
        self.y = array("d")
        self.seasons: set[int] = set()

    def __len__(self) -> int:
        return len(self.y)

    def matrix(self, time_scale: float, fp_scale: float) -> list[list[float]]:
        x = self.x
        return [scale_features(list(x[i * K:(i + 1) * K]), time_scale, fp_scale) for i in range(len(self.y))]


def collect_fit_set(factory: RowsFactory, train: SeasonRange, fraction: float, should_stop: ShouldStop) -> FitSet:
    fit = FitSet()
    for i, row in enumerate(factory()):
        if i % STOP_EVERY == 0:
            check_stop(should_stop)
        y = row.get("home_win")
        if y is None or not in_seasons(row.get("season"), train) or not in_fit_set(row.get("game_id"), fraction):
            continue
        fit.x.extend(feature_vector(state_from_row(row), row.get("pregame_p_home")))
        fit.y.append(float(y))
        fit.seasons.add(int(row["season"]))
    return fit


def candidate_list(candidates: int | Sequence[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    if isinstance(candidates, int):
        return [IngameWP.search_space(random.Random(f"{seed}:{i}")) for i in range(max(0, candidates))]
    return [dict(IngameWP(c).params) for c in candidates]


def evaluate(model: IngameWP, factory: RowsFactory, train: SeasonRange, validation: SeasonRange,
             fraction: float, should_stop: ShouldStop) -> tuple[dict[str, Any], dict[str, Any]]:
    """(selection, validation) of a fitted model in one pass over the rows."""
    n_sel, sum_sel = 0, 0.0
    acc = Accumulator()
    for i, row in enumerate(factory()):
        if i % STOP_EVERY == 0:
            check_stop(should_stop)
        season = row.get("season")
        if in_seasons(season, validation):
            acc.add_row(model, row)
        elif in_seasons(season, train) and row.get("home_win") is not None \
                and not in_fit_set(row.get("game_id"), fraction):
            p = model.predict(state_from_row(row), row.get("pregame_p_home"))
            n_sel += 1
            sum_sel += log_loss(p, float(row["home_win"]))
    selection = {"n_plays": n_sel, "log_loss": sum_sel / n_sel if n_sel else None}
    return selection, acc.metrics()


def rank_key(entry: dict[str, Any]) -> tuple[float, int]:
    ll = entry["selection"].get("log_loss")
    return (math.inf if ll is None else float(ll), int(entry["index"]))


def run_ingame_search(rows_iter_factory: RowsFactory, candidates: int | Sequence[dict[str, Any]], seed: int,
                      train_seasons: SeasonRange, validation_seasons: SeasonRange, emit: Emit,
                      should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None,
                      top_k: int = DEFAULT_TOP_K, train_fraction: float = DEFAULT_TRAIN_FRACTION) -> dict[str, Any]:
    """{"evaluated", "train_seasons", "validation_seasons", "n_fit_plays", "top", "models"}."""
    train = season_pair(train_seasons)
    validation = season_pair(validation_seasons)
    if train is None or validation is None:
        raise ValueError("train_seasons and validation_seasons must be [first, last] pairs")
    check_eras(train, validation)
    fraction = min(max(float(train_fraction), 0.0), 1.0)
    if fraction <= 0.0:
        raise ValueError("train_fraction must be above 0")
    top_k = max(1, int(top_k))
    plan = candidate_list(candidates, seed)
    n = len(plan)
    top: list[dict[str, Any]] = list((checkpoint or {}).get("top") or [])
    evaluated = min(n, int((checkpoint or {}).get("evaluated") or 0))
    collected = evaluated < n
    fit = collect_fit_set(rows_iter_factory, train, fraction, should_stop) if collected else FitSet()
    for i in range(evaluated, n):
        check_stop(should_stop)
        model = IngameWP(plan[i])
        model.fit_matrix(fit.matrix(float(model.params["time_scale"]), float(model.params["fp_scale"])), list(fit.y))
        model.train_seasons = sorted(fit.seasons)
        selection, val = evaluate(model, rows_iter_factory, train, validation, fraction, should_stop)
        entry = {"index": i, "params": model.params, "params_hash": params_hash(model.params),
                 "selection": selection, "validation": val, "artifact": model.to_json()}
        top = sorted(top + [entry], key=rank_key)[:top_k]
        emit({"evaluated": i + 1, "top": top}, (i + 1) / max(1, n))
    models = [{"family": FAMILY, "params": e["params"], "artifact": e["artifact"], "validation": e["validation"],
               "summary": IngameWP.summary(e["params"], e["validation"])} for e in top]
    return {
        "evaluated": n, "train_seasons": train, "validation_seasons": validation,
        "n_fit_plays": len(fit) if collected or not top else int(top[0]["artifact"].get("n_train") or 0),
        "top": [{k: v for k, v in e.items() if k != "artifact"} for e in top],
        "models": models,
    }


def search_from_params(params: dict[str, Any], rows_iter_factory: RowsFactory, emit: Emit,
                       should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    """run_ingame_search from job params: "n" (default 20) or "grid" (a list of params),
    "seed", "train_seasons" (or "seasons"; default [2012, 2021]), "validation_seasons"
    (default [2022, null]), "top_k" (default 5), "train_fraction" (default 0.3)."""
    family = params.get("family", FAMILY)
    if family != FAMILY:
        raise ValueError(f"the in-game search runs family {FAMILY!r}, not {family!r}")
    grid = params.get("grid")
    candidates: int | list[dict[str, Any]] = (
        [dict(g) for g in grid] if isinstance(grid, list) else int(params.get("n") or DEFAULT_N))
    train = season_pair(params.get("train_seasons"), season_pair(params.get("seasons"), DEFAULT_TRAIN_SEASONS))
    validation = season_pair(params.get("validation_seasons"), DEFAULT_VALIDATION_SEASONS)
    assert train is not None and validation is not None
    return run_ingame_search(
        rows_iter_factory, candidates, int(params.get("seed") or 0), train, validation, emit, should_stop,
        checkpoint, top_k=int(params.get("top_k") or DEFAULT_TOP_K),
        train_fraction=float(params.get("train_fraction") or DEFAULT_TRAIN_FRACTION))
