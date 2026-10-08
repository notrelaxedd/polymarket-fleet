"""Random search over the stock families (contract section 4).

The n candidates are drawn up front from random.Random(seed): a family from the
allowed list, then its params from the family's search_space, so a resumed search
draws the same list. A candidate already seen (same family and params_hash) is skipped.
Each one is backtested on the search years; the unit is one candidate-year and the
checkpoint after each is {"i": candidate index, "state": its backtest state, "top":
[the running top list], "dups": duplicates skipped so far}. The top list keeps the
top_k by Sharpe (ties: lower candidate index) among candidates with at least
MIN_TRADES trades.
"""

from __future__ import annotations

import random
from typing import Any, Callable

from fleet.models.base import params_hash
from fleet.sim.control import check_stop
from fleet.stocks import backtest
from fleet.stocks.data import Bars
from fleet.stocks.families import FAMILIES, make_model
from fleet.stocks.metrics import metrics_sentence

Emit = Callable[[dict[str, Any], float], None]
ShouldStop = Callable[[], bool]
ALL_FAMILIES = ("momentum", "meanrev", "trend", "buyhold")
MIN_TRADES = 10


def families_of(value: Any) -> list[str]:
    """The families to search (default all four); ValueError for an unknown one."""
    if value is None or value == []:
        return list(ALL_FAMILIES)
    if not isinstance(value, list) or not all(isinstance(f, str) for f in value):
        raise ValueError("families must be a list of family names")
    unknown = [f for f in value if f not in FAMILIES]
    if unknown:
        raise ValueError(f"unknown stock families: {', '.join(unknown)}")
    return list(dict.fromkeys(value))


def candidates(n: int, seed: int, families: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """The n (family, params) draws of random.Random(seed)."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        family = rng.choice(families)
        out.append((family, FAMILIES[family].search_space(rng)))
    return out


def summary(family: str, params: dict[str, Any], metrics: dict[str, Any], symbols: list[str] | None = None) -> str:
    """Two plain sentences: what the model holds, and how the backtest went."""
    return make_model(family, params, symbols).describe() + " " + metrics_sentence(metrics)


def _rank(entry: dict[str, Any]) -> tuple[float, int]:
    return (-float(entry["backtest_metrics"].get("sharpe") or 0.0), int(entry["index"]))


def run_search(bars: Bars, symbols: list[str], families: list[str], n: int, seed: int, first_year: int,
               last_year: int, cost_bps: float, top_k: int, emit: Emit, should_stop: ShouldStop,
               checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    """{"create_stock_models": [...], "evaluated", "kept", "skipped_duplicates"}."""
    draws = candidates(n, seed, families)
    per = backtest.total_units(first_year, last_year)
    total = max(1, n * per)
    cp = checkpoint if isinstance(checkpoint, dict) else {}
    start = int(cp.get("i", 0))
    top: list[dict[str, Any]] = list(cp.get("top") or [])
    resume = cp.get("state") if isinstance(cp.get("state"), dict) else None
    seen = {(f, params_hash(p)) for f, p in draws[:start]}
    duplicates = int(cp.get("dups", 0))
    for i in range(start, n):
        check_stop(should_stop)
        family, params = draws[i]
        key = (family, params_hash(params))
        if key in seen:
            duplicates += 1
            emit({"i": i + 1, "state": None, "top": top, "dups": duplicates}, (i + 1) * per / total)
            continue
        seen.add(key)
        model = make_model(family, params, symbols)

        def on_unit(state: dict[str, Any], year: int, i: int = i) -> None:
            emit({"i": i, "state": state, "top": top, "dups": duplicates}, (i * per + int(state["next_year_index"])) / total)

        metrics = backtest.run(model, bars, symbols, first_year, last_year, cost_bps, should_stop, on_unit, resume)
        resume = None
        if int(metrics.get("trades") or 0) >= MIN_TRADES:
            entry = {"index": i, "family": family, "params": params, "params_hash": key[1], "backtest_metrics": metrics}
            top = sorted(top + [entry], key=_rank)[:max(0, top_k)]
        emit({"i": i + 1, "state": None, "top": top, "dups": duplicates}, (i + 1) * per / total)
    created = [{"family": e["family"], "params": e["params"], "params_hash": e["params_hash"],
                "summary": summary(e["family"], e["params"], e["backtest_metrics"], symbols),
                "backtest_metrics": e["backtest_metrics"]} for e in top]
    return {"create_stock_models": created, "evaluated": n, "kept": len(created), "skipped_duplicates": duplicates}
