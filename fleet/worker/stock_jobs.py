"""The batch stock job kinds (contract section 4), registered in fleet.worker.jobs.JOBS.

Every kind reads the bars of params["_context"]["stock_bars_path"] (the worker's cache,
fleet.worker.stock_cache) and takes symbols, years [first, last] and cost_bps from the
params the host filled in; a null last year is the last complete calendar year.

- stock_search {n, seed, families, symbols, years, cost_bps, top_k}: result
  {"create_stock_models": [{family, params, params_hash, summary, backtest_metrics}]}
  (fleet.stocks.search); the checkpoint is the search's.
- stock_backtest {model: {family, params}, symbols, years, cost_bps, model_id}: result
  {"model_id", "backtest_metrics"}; refused when params.validation_years is given for a
  stored model and the years reach into it (it would mix the eras).
- stock_validate {model_id, model, symbols, years, cost_bps}: result {"model_id",
  "validation_metrics"}; refused when the model's search years (params.search_years or
  model.backtest_metrics.years / first_day..last_day) overlap the validation years.
Unit: one model-year; the backtest checkpoint is {"state": the backtest state}.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from fleet.stocks import backtest
from fleet.stocks.data import Bars, data_years, load_bars
from fleet.stocks.families import make_model
from fleet.stocks.search import families_of, run_search

Emit = Callable[[dict[str, Any], float], None]
ShouldStop = Callable[[], bool]
DEFAULT_COST_BPS = 5.0


def _context(params: dict[str, Any]) -> dict[str, Any]:
    ctx = params.get("_context")
    return ctx if isinstance(ctx, dict) else {}


def load_job_bars(params: dict[str, Any]) -> Bars:
    path = _context(params).get("stock_bars_path")
    if not path:
        raise ValueError("job context has no stock_bars_path")
    return load_bars(str(path))


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ValueError(f"{name} must be a whole number")
    return int(value)


def resolve_years(value: Any, bars: Bars | None = None, now_year: int | None = None) -> tuple[int, int]:
    """[first, last] with last null = the last complete calendar year (capped by the data)."""
    if not isinstance(value, (list, tuple)) or len(value) != 2 or value[0] is None:
        raise ValueError("years must be [first, last or null]")
    first = _int(value[0], "years[0]")
    if value[1] is None:
        last = (now_year if now_year is not None else time.gmtime().tm_year) - 1
        span = data_years(bars) if bars else None
        if span is not None:
            last = min(last, span[1])
    else:
        last = _int(value[1], "years[1]")
    if last < first:
        raise ValueError(f"years {first}..{last}: the last year is before the first")
    return first, last


def overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def job_symbols(params: dict[str, Any], bars: Bars) -> list[str]:
    """params.symbols present in the bars (all bar symbols when absent); ValueError when none."""
    value = params.get("symbols")
    if value is None:
        names = sorted(bars)
    elif isinstance(value, list):
        names = [s for s in value if isinstance(s, str)]
    else:
        raise ValueError("symbols must be a list of tickers")
    present = sorted({s for s in names if bars.get(s)})
    if not present:
        raise ValueError("none of the job's symbols has bars in the cache")
    return present


def cost_bps(params: dict[str, Any]) -> float:
    value = params.get("cost_bps")
    if value is None:
        return DEFAULT_COST_BPS
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1000:
        raise ValueError("cost_bps must be a number between 0 and 1000")
    return float(value)


def _model_spec(params: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    model = params.get("model")
    if not isinstance(model, dict) or not model.get("family"):
        raise ValueError("params.model needs a family and params")
    return str(model["family"]), dict(model.get("params") or {}), model


def _backtest(params: dict[str, Any], checkpoint: dict[str, Any] | None, emit: Emit, should_stop: ShouldStop,
              years: tuple[int, int], bars: Bars, family: str, model_params: dict[str, Any]) -> dict[str, Any]:
    symbols = job_symbols(params, bars)
    model = make_model(family, model_params, symbols)
    units = backtest.total_units(*years)
    resume = (checkpoint or {}).get("state") if isinstance(checkpoint, dict) else None

    def on_unit(state: dict[str, Any], year: int) -> None:
        emit({"state": state}, int(state["next_year_index"]) / units)

    return backtest.run(model, bars, symbols, years[0], years[1], cost_bps(params), should_stop, on_unit,
                        resume if isinstance(resume, dict) else None)


def run_stock_search(params: dict[str, Any], checkpoint: dict[str, Any] | None,
                     emit: Emit, should_stop: ShouldStop) -> dict[str, Any]:
    bars = load_job_bars(params)
    first, last = resolve_years(params.get("years"), bars)
    n = _int(params.get("n", 50), "n")
    top_k = _int(params.get("top_k", 5), "top_k")
    if n < 1 or top_k < 0:
        raise ValueError("n must be 1 or more and top_k 0 or more")
    return run_search(bars, job_symbols(params, bars), families_of(params.get("families")), n,
                      _int(params.get("seed", 0), "seed"), first, last, cost_bps(params), top_k, emit, should_stop,
                      checkpoint)


def run_stock_backtest(params: dict[str, Any], checkpoint: dict[str, Any] | None,
                       emit: Emit, should_stop: ShouldStop) -> dict[str, Any]:
    family, model_params, _ = _model_spec(params)
    bars = load_job_bars(params)
    years = resolve_years(params.get("years"), bars)
    validation = params.get("validation_years")
    if params.get("model_id") and validation is not None:
        era = resolve_years(validation, bars)
        if overlap(years, era):
            raise ValueError(f"years {years[0]}-{years[1]} reach into the validation era {era[0]}-{era[1]}: "
                             "a stored model's backtest must stay before it")
    metrics = _backtest(params, checkpoint, emit, should_stop, years, bars, family, model_params)
    return {"model_id": params.get("model_id"), "backtest_metrics": metrics}


def search_years(params: dict[str, Any], model: dict[str, Any]) -> tuple[int, int] | None:
    """The search era of the model to validate, when the job says what it was."""
    value = params.get("search_years")
    if isinstance(value, (list, tuple)) and len(value) == 2 and None not in value:
        return _int(value[0], "search_years[0]"), _int(value[1], "search_years[1]")
    metrics = model.get("backtest_metrics")
    if isinstance(metrics, dict):
        years = metrics.get("years")
        if isinstance(years, list) and len(years) == 2 and None not in years:
            return _int(years[0], "years[0]"), _int(years[1], "years[1]")
        first, last = metrics.get("first_day"), metrics.get("last_day")
        if isinstance(first, str) and isinstance(last, str) and first[:4].isdigit() and last[:4].isdigit():
            return int(first[:4]), int(last[:4])
    return None


def run_stock_validate(params: dict[str, Any], checkpoint: dict[str, Any] | None,
                       emit: Emit, should_stop: ShouldStop) -> dict[str, Any]:
    family, model_params, model = _model_spec(params)
    bars = load_job_bars(params)
    years = resolve_years(params.get("years"), bars)
    era = search_years(params, model)
    if era is not None and overlap(era, years):
        raise ValueError(f"the model's search years {era[0]}-{era[1]} overlap the validation years "
                         f"{years[0]}-{years[1]}: search again on years before the validation era")
    metrics = _backtest(params, checkpoint, emit, should_stop, years, bars, family, model_params)
    return {"model_id": params.get("model_id"), "validation_metrics": metrics}


STOCK_JOBS: dict[str, Callable[..., Any]] = {
    "stock_search": run_stock_search,
    "stock_backtest": run_stock_backtest,
    "stock_validate": run_stock_validate,
}
