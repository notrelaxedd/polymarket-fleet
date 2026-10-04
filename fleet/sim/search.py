"""Random model search (docs/MODELS.md, "Model search"; docs/ROBUSTNESS.md, A1 and A5).

Candidate i draws its params from random.Random(f"{seed}:{i}"), runs the full
walk-forward backtest on the search era and competes for the top list on shrunk ROI,
ties broken by lower log-loss. Selection sees the search era only. The kept candidates
are then validated (fleet.sim.validate) on the validation era, when one is given, and
the create_models entries carry validation_metrics and stress_metrics.

Single process (workers 1): unit = candidate x test season in the search phase and one
test season of one validation run afterwards. Multi-core (workers > 1): candidates,
then kept candidates, go through fleet.sim.parallel.ordered_map in index order, the
unit being a whole candidate; results are identical to the single-process run.

Checkpoint: {"next": [i, season_index], "current": {the running backtest's or
validation's own checkpoint}, "top": [...], "evaluated": i, "validated": [{"index",
"validation_metrics", "stress_metrics"} per kept candidate, in top order]}.
"""

from __future__ import annotations

import random
from typing import Any, Callable

from fleet.models.base import params_hash
from fleet.models.registry import get_family
from fleet.sim.backtest import run_backtest, season_plan
from fleet.sim.metrics import shrunk_roi
from fleet.sim.parallel import context, ordered_map
from fleet.sim.stress import NEIGHBOURHOOD_N
from fleet.sim.validate import run_validate

Emit = Callable[[dict[str, Any], float], None]
ShouldStop = Callable[[], bool]
NO_VALIDATION_NOTE = "no validation era: params.validation_seasons is absent, so the kept candidates were not validated"


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


def evaluate_candidate(games: list[dict[str, Any]], family: str, seed: int, index: int,
                       seasons: list[int | None] | None, limits: dict[str, Any], emit: Emit,
                       should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    """The top-list entry of candidate `index` (its search-era backtest)."""
    params = candidate_params(family, seed, index)
    result = run_backtest(games, family, params, seasons, limits, emit, should_stop, checkpoint, "search", f"{seed}:{index}")
    metrics = _whole_metrics(result)
    return {"index": index, "params": params, "params_hash": params_hash(params), "score": shrunk_roi(metrics), "metrics": metrics}


def validate_entry(games: list[dict[str, Any]], family: str, seed: int, entry: dict[str, Any],
                   validation_seasons: list[int | None], limits: dict[str, Any], emit: Emit,
                   should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    """{"index", "validation_metrics", "stress_metrics"} of a kept candidate."""
    out = run_validate(games, family, entry["params"], validation_seasons, limits, seed, emit, should_stop,
                       checkpoint, search_metrics=entry["metrics"])
    return {"index": entry["index"], **out}


# pool tasks (run inside a worker; the context comes from the pool initializer) --------


def _pool_evaluate(index: int) -> dict[str, Any]:
    c = context()
    return evaluate_candidate(c["games"], c["family"], c["seed"], index, c["seasons"], c["limits"], lambda cp, p: None, lambda: False)


def _pool_validate(entry: dict[str, Any]) -> dict[str, Any]:
    c = context()
    return validate_entry(c["games"], c["family"], c["seed"], entry, c["validation_seasons"], c["limits"], lambda cp, p: None, lambda: False)


# the search ---------------------------------------------------------------------------


class _State:
    """Progress bookkeeping shared by both execution paths."""

    def __init__(self, n: int, top_k: int, plan_len: int, vplan_len: int, checkpoint: dict[str, Any] | None) -> None:
        self.n = n
        self.search_units = max(1, plan_len)
        self.validate_units = (1 + NEIGHBOURHOOD_N) * max(1, vplan_len)
        self.total = max(1, n * self.search_units + top_k * self.validate_units)
        self.top: list[dict[str, Any]] = []
        self.evaluated = 0
        self.current: dict[str, Any] | None = None
        self.validated: list[dict[str, Any]] = []
        if checkpoint:
            self.top = list(checkpoint.get("top") or [])
            self.evaluated = min(n, int(checkpoint.get("evaluated") or 0))
            nxt = checkpoint.get("next") or [self.evaluated, 0]
            if int(nxt[0]) == self.evaluated:
                self.current = checkpoint.get("current") or None
            if self.evaluated == n:
                self.validated = [v for v in (checkpoint.get("validated") or []) if isinstance(v, dict)][:len(self.top)]

    def checkpoint(self, next_pair: list[int], current: dict[str, Any]) -> dict[str, Any]:
        return {"next": next_pair, "current": current, "top": self.top, "evaluated": self.evaluated, "validated": self.validated}

    def search_progress(self, candidate: int, seasons_done: int) -> float:
        return (candidate * self.search_units + seasons_done) / self.total

    def validate_progress(self, units_done: int) -> float:
        return (self.n * self.search_units + units_done) / self.total


def _search_single(games: list[dict[str, Any]], family: str, seed: int, seasons: list[int | None] | None,
                   top_k: int, limits: dict[str, Any], emit: Emit, should_stop: ShouldStop, st: _State) -> None:
    for i in range(st.evaluated, st.n):
        def inner_emit(cp: dict[str, Any], _progress: float, i: int = i) -> None:
            if cp["next"] >= st.search_units:
                return  # the candidate-complete checkpoint below covers the last season
            emit(st.checkpoint([i, cp["next"]], cp), st.search_progress(i, cp["next"]))

        entry = evaluate_candidate(games, family, seed, i, seasons, limits, inner_emit, should_stop, st.current)
        st.current = None
        _accept(st, entry, top_k, emit)


def _accept(st: _State, entry: dict[str, Any], top_k: int, emit: Emit) -> None:
    st.top = insert_top(st.top, entry, top_k)
    st.evaluated = entry["index"] + 1
    emit(st.checkpoint([st.evaluated, 0], {}), st.search_progress(st.evaluated, 0))


def _validate_single(games: list[dict[str, Any]], family: str, seed: int, validation_seasons: list[int | None],
                     limits: dict[str, Any], emit: Emit, should_stop: ShouldStop, st: _State) -> None:
    for j in range(len(st.validated), len(st.top)):
        def inner_emit(cp: dict[str, Any], progress: float, j: int = j) -> None:
            emit(st.checkpoint([st.n, 0], cp), st.validate_progress(j * st.validate_units + int(progress * st.validate_units)))

        current = st.current if j == len(st.validated) else None
        result = validate_entry(games, family, seed, st.top[j], validation_seasons, limits, inner_emit, should_stop, current)
        st.current = None
        _accept_validated(st, result, emit)


def _accept_validated(st: _State, result: dict[str, Any], emit: Emit) -> None:
    st.validated.append(result)
    emit(st.checkpoint([st.n, 0], {}), st.validate_progress(len(st.validated) * st.validate_units))


def run_search(games: list[dict[str, Any]], family: str, n: int, seed: int,
               seasons: list[int | None] | None, top_k: int, limits: dict[str, Any],
               emit: Emit, should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None,
               validation_seasons: list[int | None] | None = None, workers: int = 1) -> dict[str, Any]:
    family_cls = get_family(family)
    n = max(0, int(n))
    top_k = max(1, int(top_k))
    workers = max(1, int(workers))
    plan = season_plan(games, seasons)
    vplan = season_plan(games, validation_seasons) if validation_seasons else []
    st = _State(n, top_k, len(plan), len(vplan), checkpoint)
    ctx = {"games": games, "family": family, "seed": seed, "seasons": seasons, "limits": limits,
           "validation_seasons": validation_seasons}

    if st.evaluated < n:
        if workers > 1:
            st.current = None
            ordered_map(_pool_evaluate, list(range(st.evaluated, n)), workers, ctx, should_stop,
                        lambda entry: _accept(st, entry, top_k, emit))
        else:
            _search_single(games, family, seed, seasons, top_k, limits, emit, should_stop, st)
    if validation_seasons and len(st.validated) < len(st.top):
        if workers > 1:
            ordered_map(_pool_validate, st.top[len(st.validated):], workers, ctx, should_stop,
                        lambda result: _accept_validated(st, result, emit))
        else:
            _validate_single(games, family, seed, validation_seasons, limits, emit, should_stop, st)

    by_index = {v["index"]: v for v in st.validated}
    create_models = []
    for e in st.top:
        v = by_index.get(e["index"], {})
        create_models.append({
            "family": family, "params": e["params"], "artifact": None, "backtest_metrics": e["metrics"],
            "summary": family_cls.summary(e["params"], e["metrics"]), "trained_through": None,
            "validation_metrics": v.get("validation_metrics"), "stress_metrics": v.get("stress_metrics"),
        })
    result = {"evaluated": n, "seasons": plan, "top": st.top, "create_models": create_models,
              "validation_seasons": vplan, "validated": st.validated}
    if not validation_seasons:
        result["validation_note"] = NO_VALIDATION_NOTE
    return result
