"""The daily stock backtest (contract section 4).

For every trading day d of the years (SPY's dates, or the union of the symbols'):
1. holdings are marked at d's close (a symbol without a bar on d keeps its last close);
2. the model decides from history_before(bars, d): only bars dated before d;
3. when the weights differ from the last decision, the book is rebalanced to them at
   d's close, cost_bps per side on the traded value (fractional shares; a symbol with
   no bar on d is not traded that day).
So a decision taken with the bars through d - 1 earns the return from d's close on,
exactly like the live market-on-close order. Start equity 1.0.

The unit is one model-year: on_unit(state, year) after each calendar year, with a
small JSON state (holdings, cash, accumulators) that run(..., resume=state) continues
from. The result is the metrics dict of fleet.stocks.metrics plus per_year, benchmark
(SPY bought at the first close, same cost) and first_day, last_day, years.
"""

from __future__ import annotations

from typing import Any, Callable

from fleet.sim.control import check_stop
from fleet.stocks.data import BENCHMARK, Bars, close_on, dates_of, history_before, trading_days
from fleet.stocks.families import StockModel
from fleet.stocks.metrics import Track, summarize, year_entry

OnUnit = Callable[[dict[str, Any], int], None]
EPS = 1e-12


def clean_weights(weights: Any, tradable: set[str]) -> dict[str, float]:
    """Long-only weights of tradable symbols, each in [0, 1], scaled down to sum <= 1."""
    out: dict[str, float] = {}
    if isinstance(weights, dict):
        for symbol, w in weights.items():
            if symbol in tradable and isinstance(w, (int, float)) and not isinstance(w, bool) and w == w and w > 0:
                out[symbol] = min(1.0, float(w))
    total = sum(out.values())
    if total > 1.0:
        out = {s: w / total for s, w in out.items()}
    return {s: round(w, 12) for s, w in sorted(out.items())}


class _Book:
    """Fractional holdings and cash of one path."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        state = state or {}
        self.cash = float(state.get("cash", 1.0))
        self.shares: dict[str, float] = {k: float(v) for k, v in (state.get("shares") or {}).items()}
        self.price: dict[str, float] = {k: float(v) for k, v in (state.get("price") or {}).items()}

    def equity(self) -> float:
        return self.cash + sum(q * self.price.get(s, 0.0) for s, q in self.shares.items())

    def invested(self) -> float:
        return sum(q * self.price.get(s, 0.0) for s, q in self.shares.items())

    def rebalance(self, weights: dict[str, float], priced: set[str], cost_bps: float) -> tuple[float, int]:
        """Trade to weights at the current prices; (traded value, position changes)."""
        equity = self.equity()
        rate = cost_bps / 10_000.0
        names = sorted((set(self.shares) | set(weights)) & priced)
        current = {s: self.shares.get(s, 0.0) * self.price[s] for s in names}
        rough = sum(abs(weights.get(s, 0.0) * equity - current[s]) for s in names) * rate
        base = max(0.0, equity - rough)
        traded, changes = 0.0, 0
        for s in names:
            target = weights.get(s, 0.0) * base
            delta = target - current[s]
            if abs(delta) <= EPS * max(1.0, equity):
                continue
            traded += abs(delta)
            changes += 1
            self.cash -= delta
            if target <= EPS:
                self.shares.pop(s, None)
            else:
                self.shares[s] = target / self.price[s]
        self.cash -= traded * rate
        return traded, changes

    def to_dict(self) -> dict[str, Any]:
        return {"cash": self.cash, "shares": dict(self.shares), "price": dict(self.price)}


def _years(first_year: int, last_year: int) -> list[int]:
    return list(range(int(first_year), int(last_year) + 1))


def total_units(first_year: int, last_year: int) -> int:
    return len(_years(first_year, last_year))


def run(model: StockModel, bars: Bars, symbols: list[str], first_year: int, last_year: int, cost_bps: float,
        should_stop: Callable[[], bool], on_unit: OnUnit | None = None,
        resume: dict[str, Any] | None = None) -> dict[str, Any]:
    """Backtest model on the years first..last; see the module docstring."""
    tradable = set(symbols) | set(model.extra_symbols())
    seen = sorted((tradable | {BENCHMARK}) & set(bars))
    dates = dates_of({s: bars[s] for s in seen})
    days = trading_days(bars, sorted(tradable), first_year, last_year)
    years = _years(first_year, last_year)
    state = resume if isinstance(resume, dict) else {}
    start = int(state.get("next_year_index", 0))
    book, bench = _Book(state.get("book")), _Book(state.get("bench"))
    track, bench_track = Track.from_dict(state.get("track")), Track.from_dict(state.get("bench_track"))
    per_year: list[dict[str, Any]] = list(state.get("per_year") or [])
    bench_years: list[dict[str, Any]] = list(state.get("bench_years") or [])
    last_weights: dict[str, float] | None = state.get("last_weights")
    first_day, last_day = state.get("first_day"), state.get("last_day")
    for index in range(start, len(years)):
        check_stop(should_stop)
        year = years[index]
        ytrack, ybench = Track(), Track()
        for day in (d for d in days if d.startswith(f"{year:04d}-")):
            before = book.equity()
            priced = set()
            for s in seen:
                close = close_on(bars, s, day, dates)
                if close is not None:
                    book.price[s] = close
                    priced.add(s)
            weights = clean_weights(model.weights(history_before(bars, day, seen, dates), day), tradable)
            traded, changes = 0.0, 0
            mark = book.equity()
            if weights != last_weights:
                traded, changes = book.rebalance(weights, priced & tradable, cost_bps)
                # a name without a bar today is retried tomorrow
                last_weights = weights if (set(weights) | set(book.shares)) <= priced else None
            after = book.equity()
            r = after / before - 1.0 if before > EPS else 0.0
            exposure = book.invested() / after if after > EPS else 0.0
            frac = traded / mark if mark > EPS else 0.0
            track.add_day(r, exposure, frac, changes)
            ytrack.add_day(r, exposure, frac, changes)
            _bench_day(bench, bench_track, ybench, bars, day, dates, cost_bps, BENCHMARK in priced)
            first_day = first_day or day
            last_day = day
        if ytrack.days:
            per_year.append(year_entry(year, ytrack))
            if ybench.days:
                bench_years.append(year_entry(year, ybench))
        state = {"next_year_index": index + 1, "book": book.to_dict(), "bench": bench.to_dict(),
                 "track": track.to_dict(), "bench_track": bench_track.to_dict(), "per_year": list(per_year),
                 "bench_years": list(bench_years), "last_weights": last_weights, "first_day": first_day, "last_day": last_day}
        if on_unit is not None:
            on_unit(state, year)
    return result(track, per_year, bench_track, bench_years, first_day, last_day, first_year, last_year)


def _bench_day(bench: _Book, bench_track: Track, ybench: Track, bars: Bars, day: str,
               dates: dict[str, list[str]], cost_bps: float, priced: bool) -> None:
    """SPY buy and hold: bought at the first close it has, then marked daily."""
    if not bars.get(BENCHMARK):
        return
    before = bench.equity()
    close = close_on(bars, BENCHMARK, day, dates) if priced else None
    if close is not None:
        bench.price[BENCHMARK] = close
    traded, changes = 0.0, 0
    if close is not None and not bench.shares:
        traded, changes = bench.rebalance({BENCHMARK: 1.0}, {BENCHMARK}, cost_bps)
    after = bench.equity()
    r = after / before - 1.0 if before > EPS else 0.0
    exposure = bench.invested() / after if after > EPS else 0.0
    bench_track.add_day(r, exposure, traded / before if before > EPS else 0.0, changes)
    ybench.add_day(r, exposure, 0.0, changes)


def result(track: Track, per_year: list[dict[str, Any]], bench_track: Track, bench_years: list[dict[str, Any]],
           first_day: str | None, last_day: str | None, first_year: int, last_year: int) -> dict[str, Any]:
    out = summarize(track)
    out["per_year"] = per_year
    out["benchmark"] = dict(summarize(bench_track), per_year=bench_years) if bench_track.days else None
    out["first_day"], out["last_day"] = first_day, last_day
    out["years"] = [int(first_year), int(last_year)]
    return out
