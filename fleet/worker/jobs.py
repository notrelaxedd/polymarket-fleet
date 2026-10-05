"""Job registry for the runner child process.

A job function has the signature run(params, checkpoint, emit, should_stop) -> result.
It calls emit(checkpoint, progress) after every unit of work (units must take < 3 s)
and raises JobStopped when should_stop() turns true between units. The runner child
merges job["context"] into params as params["_context"] ({"games_path", "model"}).
The host copies the relevant settings into params at job creation (fee_model,
default_bankroll_cents, max_bet_cents, trade_max_games, backtest_seasons, and for
model_search validation_seasons and workers); the docs/MODELS.md defaults apply when
they are absent. Step 6 adds the validate kind (docs/ROBUSTNESS.md): params
{"model_id", "seed" (default 1), "validation_seasons"}, the model from the context.
A backtest with params.price_source "snapshots" (docs/ROBUSTNESS.md B1) replays the
recorded prices of the context's prices_path on params.price_platform (platform sim
refused unless params.allow_sim_prices) with params.decision_minutes_before_kickoff
and params.participation (settings copied by the host; defaults 60 and 0.5).
Step 6C: a model_search with params.family "ingame_wp" runs the in-game search
(fleet.sim.ingame through fleet.worker.pbp_cache) on the play-by-play rows of the
context's pbp_path, with params train_seasons, validation_seasons, n, seed, top_k and
train_fraction; its result carries create_models like every search. The pre-game
backtest, validate and train kinds refuse the ingame_wp family.
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
DEFAULT_VALIDATION_SEASONS: list[int | None] = [2022, None]
PRICE_SOURCES = ("closing_line", "snapshots")
DEFAULT_SEED = 1
INGAME_FAMILY = "ingame_wp"


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
    participation = params.get("participation")
    if isinstance(participation, (int, float)) and not isinstance(participation, bool) and 0 <= participation <= 1:
        limits["participation"] = float(participation)
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


def refuse_ingame(family: Any, kind: str) -> None:
    """ValueError for the in-game family on a pre-game job kind (it predicts from a game
    state, not from a game row; the in-game search validates it)."""
    if family == INGAME_FAMILY:
        raise ValueError(f"{kind} does not run the {INGAME_FAMILY} family: run a model_search with family "
                         f"{INGAME_FAMILY} (the in-game search validates it on held-out plays)")


def _family_and_params(params: dict[str, Any], kind: str = "backtest") -> tuple[str, dict[str, Any]]:
    """family/params from the job, or from the context model when params.model_id is set."""
    if params.get("model_id"):
        model = _context(params).get("model")
        if not isinstance(model, dict) or not model.get("family"):
            raise ValueError("job context has no model for params.model_id")
        refuse_ingame(model["family"], kind)
        return str(model["family"]), dict(model.get("params") or {})
    family = params.get("family")
    if not family:
        raise ValueError("params need family or model_id")
    refuse_ingame(family, kind)
    return str(family), dict(params.get("params") or {})


def _seasons(params: dict[str, Any], limits: dict[str, Any]) -> list[int | None]:
    value = params.get("seasons")
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return [None if value[0] is None else int(value[0]), None if value[1] is None else int(value[1])]
    return list(limits["backtest_seasons"])


def _season_pair(value: Any) -> list[int | None] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return [None if value[0] is None else int(value[0]), None if value[1] is None else int(value[1])]
    return None


def _seed(params: dict[str, Any]) -> int:
    value = params.get("seed")
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else DEFAULT_SEED


def price_source(params: dict[str, Any]) -> str:
    """params.price_source, "closing_line" when absent; ValueError when unknown."""
    value = params.get("price_source") or "closing_line"
    if value not in PRICE_SOURCES:
        raise ValueError(f"unknown price_source {value!r}")
    return str(value)


def price_platform(params: dict[str, Any]) -> tuple[str, bool]:
    """(platform, allow_sim_prices) of a snapshot backtest; refuses sim unless allowed."""
    from fleet.sim.prices import DEFAULT_PLATFORM, SIM_PLATFORM, SimPricesRefused

    platform = str(params.get("price_platform") or DEFAULT_PLATFORM)
    allow_sim = params.get("allow_sim_prices") is True
    if platform == SIM_PLATFORM and not allow_sim:
        raise SimPricesRefused("price platform sim is refused while allow_sim_prices is off")
    return platform, allow_sim


def _replay(params: dict[str, Any]) -> Any:
    """The fleet.sim.prices.Replay of a snapshot backtest, None for closing_line."""
    if price_source(params) != "snapshots":
        return None
    from fleet.sim.prices import Replay, load_markets, params_decision_minutes

    platform, allow_sim = price_platform(params)
    path = _context(params).get("prices_path")
    if not path:
        raise ValueError("job context has no prices_path for a snapshot backtest")
    minutes = params_decision_minutes(params)
    games_minutes = _context(params).get("games_minutes")
    if games_minutes is not None and games_minutes != minutes:
        raise ValueError(f"the games feed cut injuries at {games_minutes} minutes, the replay decides at {minutes}")
    return Replay(load_markets(str(path)), platform, minutes, allow_sim)


def run_backtest_job(params: dict[str, Any], checkpoint: dict[str, Any] | None,
                     emit: Emit, should_stop: ShouldStop) -> dict[str, Any]:
    from fleet.sim.backtest import run_backtest

    limits = limits_from_params(params)
    family, model_params = _family_and_params(params)
    replay = _replay(params)
    games = _load_games(params)
    return run_backtest(games, family, model_params, _seasons(params, limits), limits, emit, should_stop, checkpoint,
                        "search", _seed(params), replay)


def run_model_search_job(params: dict[str, Any], checkpoint: dict[str, Any] | None,
                         emit: Emit, should_stop: ShouldStop) -> dict[str, Any]:
    """params.validation_seasons (absent: the kept candidates are not validated) and
    params.workers ("auto" or an integer, default 1) come from the host's settings."""
    from fleet.sim.parallel import resolve_workers
    from fleet.sim.search import run_search

    family = str(params.get("family") or "")
    if not family:
        raise ValueError("model_search needs params.family")
    if family == INGAME_FAMILY:
        from fleet.worker.pbp_cache import run_ingame_search_job

        return run_ingame_search_job(params, checkpoint, emit, should_stop)
    limits = limits_from_params(params)
    games = _load_games(params)
    return run_search(
        games, family, int(params.get("n", 200)), int(params.get("seed", 0)), _seasons(params, limits),
        int(params.get("top_k", 5)), limits, emit, should_stop, checkpoint,
        validation_seasons=_season_pair(params.get("validation_seasons")), workers=resolve_workers(params.get("workers")),
    )


def search_era_overlap(search_metrics: dict[str, Any] | None, validation_seasons: list[int | None]) -> list[int]:
    """The seasons of the stored search-era metrics at or after the first validation
    season (empty when the eras are clean or a bound is unknown)."""
    first = validation_seasons[0] if validation_seasons else None
    tested = (search_metrics or {}).get("seasons")
    if first is None or not isinstance(tested, list):
        return []
    return [int(s) for s in tested if isinstance(s, int) and not isinstance(s, bool) and s >= first]


def run_validate_job(params: dict[str, Any], checkpoint: dict[str, Any] | None,
                     emit: Emit, should_stop: ShouldStop) -> dict[str, Any]:
    """{"validation_metrics", "stress_metrics"} of the context model on the validation
    era (params.validation_seasons, default [2022, last complete]); the model's stored
    backtest_metrics feed the overfit flag. Refused (ValueError, the job errors) when
    those search-era metrics cover a validation-era season: the overfit check would
    compare two overlapping eras and the validation would be in-sample (the host
    refuses such a job at creation too, host/eras.py)."""
    from fleet.sim.validate import run_validate

    model = _context(params).get("model")
    if not isinstance(model, dict) or not model.get("family"):
        raise ValueError("validate needs the model in the job context (params.model_id)")
    refuse_ingame(model["family"], "validate")
    limits = limits_from_params(params)
    seasons = _season_pair(params.get("validation_seasons")) or list(DEFAULT_VALIDATION_SEASONS)
    search_metrics = model.get("backtest_metrics") if isinstance(model.get("backtest_metrics"), dict) else None
    overlap = search_era_overlap(search_metrics, seasons)
    if overlap:
        raise ValueError(f"the model's search-era metrics cover {overlap}, inside the validation era from {seasons[0]}:"
                         " search again on seasons before the validation era")
    games = _load_games(params)
    return run_validate(games, str(model["family"]), dict(model.get("params") or {}), seasons, limits, _seed(params),
                        emit, should_stop, checkpoint, search_metrics=search_metrics)


def run_train_job(params: dict[str, Any], checkpoint: dict[str, Any] | None,
                  emit: Emit, should_stop: ShouldStop) -> dict[str, Any]:
    from fleet.sim.train import run_train

    model = _context(params).get("model")
    if not isinstance(model, dict):
        raise ValueError("train needs the parent model in the job context (params.model_id)")
    refuse_ingame(model.get("family"), "train")
    games = _load_games(params)
    return run_train(games, model, params.get("through"), emit, should_stop, checkpoint)


JOBS: dict[str, JobFunc] = {
    "sleep": run_sleep,
    "backtest": run_backtest_job,
    "model_search": run_model_search_job,
    "train": run_train_job,
    "validate": run_validate_job,
}
