"""Search and validation of the ingame_wp family on play-by-play rows (contract section 4).

run_ingame_search fits each candidate on the train seasons only, ranks the candidates
without looking at the validation era, and reports the validation numbers of the kept
ones:

- Fit set: the plays of the train-season games whose game_id hashes below
  train_fraction (blake2b of "ingame-fit:<game_id>", so the split is the same in every
  search and on every resume). Their raw features are built once and finished per
  candidate (fleet.models.ingame_wp.finish_features: time exponent and field scale).
- Selection: the log-loss on the other train-season games (out of sample, whole games),
  ties broken by the candidate index. With train_fraction 1 there is no selection set and
  the ranking is the candidate order. "vs_vegas" repeats the comparison on the selection
  plays that carry a vegas_wp.
- Validation: fleet.sim.ingame_eval on the validation seasons, which must come strictly
  after the train seasons; the validation rows never reach a fit or the ranking.

Candidate i draws its params from IngameWP.search_space(random.Random(f"{seed}:{i}"))
unless a params grid is given. Rows come from rows_iter_factory(), called once for the fit
set and once per candidate (one lazy pass for selection and validation). Checkpoint after
every candidate: {"evaluated": i, "top": [entries with their artifacts]}; a resumed
search rebuilds the fit set (deterministic) and continues at candidate i.

The result carries "create_models" like the other searches (contract section 6), which
the agent posts unchanged: per kept candidate {"family": "ingame_wp", "params",
"artifact", "summary", "trained_through": None, "parent_model_id": None, "backtest_metrics": the selection
metrics with "era": "search" and "seasons" = the train seasons seen (fit and selection plays),
"validation_metrics": the fleet.sim.ingame_eval dict with "era": "validation",
"stress_metrics": None}.
"""

from __future__ import annotations

import hashlib
import math
import random
from array import array
from typing import Any, Callable, Iterable, Sequence

from fleet.models.base import params_hash
from fleet.models.ingame_wp import FAMILY, K, IngameWP, finish_features, raw_features, state_from_row
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
DEFAULT_SEED = 1
DEFAULT_TRAIN_SEASONS: list[int | None] = [2012, 2021]
DEFAULT_VALIDATION_SEASONS: list[int | None] = [2022, None]
STOP_EVERY = 20000
RAW_K = K + 1  # raw_features: K columns plus the time base


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
    """Raw feature rows (flat array, RAW_K per row) and outcomes of the fit plays."""

    def __init__(self) -> None:
        self.x = array("d")
        self.y = array("d")
        self.seasons: set[int] = set()

    def __len__(self) -> int:
        return len(self.y)

    def matrix(self, time_scale: float, fp_scale: float) -> list[list[float]]:
        x = self.x
        return [finish_features(x[i * RAW_K:(i + 1) * RAW_K], time_scale, fp_scale) for i in range(len(self.y))]


def collect_fit_set(factory: RowsFactory, train: SeasonRange, fraction: float, should_stop: ShouldStop) -> FitSet:
    fit = FitSet()
    for i, row in enumerate(factory()):
        if i % STOP_EVERY == 0:
            check_stop(should_stop)
        y = row.get("home_win")
        if y is None or not in_seasons(row.get("season"), train) or not in_fit_set(row.get("game_id"), fraction):
            continue
        fit.x.extend(raw_features(state_from_row(row), row.get("pregame_p_home")))
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
    sel_seasons: set[int] = set()
    sel_vegas = Accumulator()
    acc = Accumulator()
    for i, row in enumerate(factory()):
        if i % STOP_EVERY == 0:
            check_stop(should_stop)
        season = row.get("season")
        if in_seasons(season, validation):
            acc.add_row(model, row)
        elif in_seasons(season, train) and row.get("home_win") is not None \
                and not in_fit_set(row.get("game_id"), fraction):
            state = state_from_row(row)
            p = model.predict(state, row.get("pregame_p_home"))
            y = float(row["home_win"])
            n_sel += 1
            sum_sel += log_loss(p, y)
            sel_seasons.add(int(season))
            if row.get("vegas_wp") is not None:
                sel_vegas.add(p, float(row["vegas_wp"]), y, str(min(int(state["period"]), 5)),
                              int(row.get("score_diff") or 0), season)
    vegas = sel_vegas.metrics()
    selection = {"n_plays": n_sel, "log_loss": sum_sel / n_sel if n_sel else None, "seasons": sorted(sel_seasons),
                 "vs_vegas": {k: vegas[k] for k in ("n_plays", "log_loss", "vegas_log_loss", "beats_baseline")}}
    return selection, acc.metrics()


def rank_key(entry: dict[str, Any]) -> tuple[float, int]:
    ll = entry["selection"].get("log_loss")
    return (math.inf if ll is None else float(ll), int(entry["index"]))


def create_model_entry(entry: dict[str, Any], fraction: float) -> dict[str, Any]:
    """The create_models entry of a kept candidate (posted by the agent unchanged)."""
    artifact = entry["artifact"]
    seasons = sorted(set(artifact.get("train_seasons") or []) | set(entry["selection"].get("seasons") or []))
    backtest = dict(entry["selection"], era="search", seasons=seasons,
                    n_fit_plays=int(artifact.get("n_train") or 0), train_fraction=fraction)
    validation = dict(entry["validation"], era="validation")
    return {"family": FAMILY, "params": entry["params"], "artifact": artifact, "backtest_metrics": backtest,
            "validation_metrics": validation, "stress_metrics": None, "trained_through": None, "parent_model_id": None,
            "summary": IngameWP.summary(entry["params"], validation)}


def run_ingame_search(rows_iter_factory: RowsFactory, candidates: int | Sequence[dict[str, Any]], seed: int,
                      train_seasons: SeasonRange, validation_seasons: SeasonRange, emit: Emit,
                      should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None,
                      top_k: int = DEFAULT_TOP_K, train_fraction: float = DEFAULT_TRAIN_FRACTION) -> dict[str, Any]:
    """{"evaluated", "train_seasons", "validation_seasons", "train_fraction", "n_fit_plays",
    "top", "create_models"}."""
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
    return {
        "evaluated": n, "train_seasons": train, "validation_seasons": validation, "train_fraction": fraction,
        "n_fit_plays": len(fit) if collected or not top else int(top[0]["artifact"].get("n_train") or 0),
        "top": [{k: v for k, v in e.items() if k not in ("artifact", "validation")} for e in top],
        "create_models": [create_model_entry(e, fraction) for e in top],
    }


def _int_or(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    return int(value)


def job_eras(params: dict[str, Any]) -> tuple[list[int | None], list[int | None]]:
    """(train, validation) of job params: "train_seasons" (or "seasons"; default
    [2012, 2021]) and "validation_seasons" (default [2022, null]). An open train end is
    capped at the season before the validation era (as the host caps the search era)."""
    validation = season_pair(params.get("validation_seasons"), DEFAULT_VALIDATION_SEASONS)
    train = season_pair(params.get("train_seasons"), season_pair(params.get("seasons"), DEFAULT_TRAIN_SEASONS))
    assert train is not None and validation is not None
    if train[1] is None and validation[0] is not None:
        train = [train[0], int(validation[0]) - 1]
    return train, validation


def search_from_params(params: dict[str, Any], rows_iter_factory: RowsFactory, emit: Emit,
                       should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    """run_ingame_search from job params: "n" (default 20) or "grid" (a list of params),
    "seed" (default 1), the eras of job_eras, "top_k" (default 5), "train_fraction"
    (default 0.3)."""
    family = params.get("family", FAMILY)
    if family != FAMILY:
        raise ValueError(f"the in-game search runs family {FAMILY!r}, not {family!r}")
    grid = params.get("grid")
    candidates: int | list[dict[str, Any]] = (
        [dict(g) for g in grid] if isinstance(grid, list) else _int_or(params.get("n"), DEFAULT_N))
    train, validation = job_eras(params)
    fraction = params.get("train_fraction")
    return run_ingame_search(
        rows_iter_factory, candidates, _int_or(params.get("seed"), DEFAULT_SEED), train, validation, emit,
        should_stop, checkpoint, top_k=_int_or(params.get("top_k"), DEFAULT_TOP_K),
        train_fraction=DEFAULT_TRAIN_FRACTION if fraction is None else float(fraction))
