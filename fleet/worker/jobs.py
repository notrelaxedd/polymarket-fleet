"""Job registry for the runner child process.

A job function has the signature run(params, checkpoint, emit, should_stop) -> result.
It calls emit(checkpoint, progress) after every unit of work (units must take < 3 s)
and raises JobStopped when should_stop() turns true between units. The runner child
merges job["context"] into params as params["_context"] ({"games_path", "model"}).
The host copies the relevant settings into params at job creation (fee_model,
default_bankroll_cents, max_bet_cents, trade_max_games, backtest_seasons); the
docs/MODELS.md defaults apply when they are absent.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from fleet.sim.control import JobStopped  # noqa: F401  (re-exported for the runner)

Emit = Callable[[dict[str, Any], float], None]
ShouldStop = Callable[[], bool]
JobFunc = Callable[[dict[str, Any], dict[str, Any] | None, Emit, ShouldStop], Any]

DEFAULT_LIMITS: dict[str, Any] = {
    "fee_model": {"taker_rate": 0.05, "half_spread": 0.01},
    "default_bankroll_cents": 10000,
    "max_bet_cents": 2500,
    "trade_max_games": 6,
    "backtest_seasons": [2010, None],
}


def run_sleep(
    params: dict[str, Any],
    checkpoint: dict[str, Any] | None,
    emit: Emit,
    should_stop: ShouldStop,
) -> dict[str, Any]:
    """Sleep params["seconds"] in 1 s units; checkpoint {"elapsed": n}; result {"slept": seconds}."""
    seconds = int(params.get("seconds", 0))
    elapsed = 0
    if checkpoint:
        elapsed = int(checkpoint.get("elapsed", 0))
    elapsed = max(0, min(elapsed, seconds))
    while elapsed < seconds:
        if should_stop():
            raise JobStopped()
        time.sleep(1.0)
        elapsed += 1
        emit({"elapsed": elapsed}, elapsed / seconds)
    return {"slept": seconds}


# ------------------------------------------------------------ step 3 kinds


def limits_from_params(params: dict[str, Any]) -> dict[str, Any]:
    """The betting limits the host copied into params, with MODELS.md defaults."""
    limits: dict[str, Any] = {}
    for key, default in DEFAULT_LIMITS.items():
        value = params.get(key)
        if key == "fee_model":
            fee = dict(default)
            if isinstance(value, dict):
                fee.update({k: float(v) for k, v in value.items() if k in fee and v is not None})
            limits[key] = fee
        elif key == "backtest_seasons":
            limits[key] = list(value) if isinstance(value, (list, tuple)) and len(value) == 2 else list(default)
        else:
            limits[key] = int(value) if value is not None else default
    return limits


def _context(params: dict[str, Any]) -> dict[str, Any]:
    ctx = params.get("_context")
    return ctx if isinstance(ctx, dict) else {}


def _load_games(params: dict[str, Any]) -> list[dict[str, Any]]:
    from fleet.sim.data import load_games

    path = _context(params).get("games_path")
    if not path:
        raise ValueError("job context has no games_path")
    return load_games(str(path))


def _family_and_params(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """family/params from the job, or from the context model when params.model_id is set."""
    if params.get("model_id"):
        model = _context(params).get("model")
        if not isinstance(model, dict) or not model.get("family"):
            raise ValueError("job context has no model for params.model_id")
        return str(model["family"]), dict(model.get("params") or {})
    family = params.get("family")
    if not family:
        raise ValueError("params need family or model_id")
    return str(family), dict(params.get("params") or {})


def _seasons(params: dict[str, Any], limits: dict[str, Any]) -> list[int | None]:
    value = params.get("seasons")
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return [None if value[0] is None else int(value[0]), None if value[1] is None else int(value[1])]
    return list(limits["backtest_seasons"])


def run_backtest_job(params: dict[str, Any], checkpoint: dict[str, Any] | None,
                     emit: Emit, should_stop: ShouldStop) -> dict[str, Any]:
    from fleet.sim.backtest import run_backtest

    limits = limits_from_params(params)
    family, model_params = _family_and_params(params)
    games = _load_games(params)
    return run_backtest(games, family, model_params, _seasons(params, limits), limits, emit, should_stop, checkpoint)


def run_model_search_job(params: dict[str, Any], checkpoint: dict[str, Any] | None,
                         emit: Emit, should_stop: ShouldStop) -> dict[str, Any]:
    from fleet.sim.search import run_search

    limits = limits_from_params(params)
    family = str(params.get("family") or "")
    if not family:
        raise ValueError("model_search needs params.family")
    games = _load_games(params)
    return run_search(
        games, family, int(params.get("n", 200)), int(params.get("seed", 0)), _seasons(params, limits),
        int(params.get("top_k", 5)), limits, emit, should_stop, checkpoint,
    )


def run_train_job(params: dict[str, Any], checkpoint: dict[str, Any] | None,
                  emit: Emit, should_stop: ShouldStop) -> dict[str, Any]:
    from fleet.sim.train import run_train

    model = _context(params).get("model")
    if not isinstance(model, dict):
        raise ValueError("train needs the parent model in the job context (params.model_id)")
    games = _load_games(params)
    return run_train(games, model, params.get("through"), emit, should_stop, checkpoint)


JOBS: dict[str, JobFunc] = {
    "sleep": run_sleep,
    "backtest": run_backtest_job,
    "model_search": run_model_search_job,
    "train": run_train_job,
}
