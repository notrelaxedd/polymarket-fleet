"""The stock model families (contract section 4).

A StockModel turns the daily history before a decision day into long-only target
weights: weights(hist, day) -> {symbol: w}, each w in [0, 1], sum <= 1, the same answer
for the same input. hist holds only bars dated before day (fleet.stocks.data.
history_before cuts it); the model never sees day itself. decide() also returns one
plain-words note per ranked symbol ("momentum rank 2/20") for the order rationale.

Rebalancing is stateless: momentum and meanrev rank on the history up to the last
"anchor", the newest multiple of rebalance_days (hold_days) bars of the reference
series (SPY when present, else the longest series), so the backtest and the live tick
agree without remembering anything between calls. symbols (optional) is the universe
a model may hold; SPY may be in hist as a signal only (the trend filter).
"""

from __future__ import annotations

import bisect
import random
from dataclasses import dataclass, field
from typing import Any

from fleet.stocks.data import BENCHMARK, Bar

Hist = dict[str, list[Bar]]


@dataclass
class Decision:
    """Target weights and one note per symbol the model ranked."""

    weights: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)


def _int_param(params: dict[str, Any], key: str, default: int, low: int, high: int, allow_zero: bool = False) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ValueError(f"{key} must be a whole number")
    value = int(value)
    if not (low <= value <= high or (allow_zero and value == 0)):
        raise ValueError(f"{key} must be {'0 or ' if allow_zero else ''}between {low} and {high}")
    return value


def reference(hist: Hist) -> str | None:
    """SPY when it has history, else the symbol with the most bars (ties by name)."""
    if hist.get(BENCHMARK):
        return BENCHMARK
    best = sorted((s for s in hist if hist[s]), key=lambda s: (-len(hist[s]), s))
    return best[0] if best else None


def cut_at(rows: list[Bar], last_date: str) -> int:
    """How many of rows are dated on or before last_date."""
    return bisect.bisect_right(rows, last_date, key=lambda b: b.date)


def sma(rows: list[Bar], end: int, n: int) -> float | None:
    """Mean close of rows[end - n:end]; None without n bars."""
    if n <= 0 or end < n:
        return None
    return sum(rows[i].close for i in range(end - n, end)) / n


def anchor_date(hist: Hist, every: int) -> str | None:
    """The date of the last bar of the newest rebalance anchor (multiple of `every`
    reference bars), None before the first anchor."""
    ref = reference(hist)
    if ref is None:
        return None
    n = len(hist[ref])
    anchor = n - (n % every)
    return hist[ref][anchor - 1].date if anchor > 0 else None


def names(k: int) -> str:
    return "1 stock" if k == 1 else f"{k} stocks"


def _equal(chosen: list[str], slots: int) -> dict[str, float]:
    return {s: 1.0 / slots for s in chosen} if chosen and slots > 0 else {}


class StockModel:
    """Base class: subclasses set family, PARAM_KEYS and implement decide()."""

    family = ""
    PARAM_KEYS: tuple[str, ...] = ()

    def __init__(self, params: dict[str, Any], symbols: list[str] | None = None) -> None:
        self.params: dict[str, Any] = dict(params)
        self.symbols: list[str] | None = sorted(set(symbols)) if symbols is not None else None

    def universe(self, hist: Hist) -> list[str]:
        names = hist.keys() if self.symbols is None else [s for s in self.symbols if s in hist]
        return sorted(s for s in names if hist.get(s))

    def decide(self, hist: Hist, day: str) -> Decision:
        raise NotImplementedError

    def weights(self, hist: Hist, day: str) -> dict[str, float]:
        return self.decide(hist, day).weights

    def extra_symbols(self) -> list[str]:
        """Symbols the model trades beyond the universe (buyhold's symbol)."""
        return []

    @staticmethod
    def search_space(rng: random.Random) -> dict[str, Any]:
        raise NotImplementedError

    def describe(self) -> str:
        """One plain-words sentence on what the model holds."""
        raise NotImplementedError


def _ranked(family: str, scores: dict[str, float], descending: bool, top_k: int) -> Decision:
    order = sorted(scores, key=lambda s: ((-scores[s] if descending else scores[s]), s))
    notes = {s: f"{family} rank {i + 1}/{len(order)}" for i, s in enumerate(order)}
    return Decision(_equal(order[:top_k], top_k), notes)


class Momentum(StockModel):
    family = "momentum"
    PARAM_KEYS = ("lookback", "skip", "top_k", "rebalance_days", "trend_sma")

    def __init__(self, params: dict[str, Any], symbols: list[str] | None = None) -> None:
        super().__init__(params, symbols)
        self.lookback = _int_param(params, "lookback", 126, 21, 252)
        self.skip = _int_param(params, "skip", 5, 0, 21)
        self.top_k = _int_param(params, "top_k", 3, 1, 10)
        self.every = _int_param(params, "rebalance_days", 21, 1, 21)
        self.trend_sma = _int_param(params, "trend_sma", 0, 50, 250, allow_zero=True)

    def decide(self, hist: Hist, day: str) -> Decision:
        last = anchor_date(hist, self.every)
        if last is None:
            return Decision()
        if self.trend_sma:
            spy = hist.get(BENCHMARK) or []
            end = cut_at(spy, last)
            mean = sma(spy, end, self.trend_sma)
            if mean is None or spy[end - 1].close < mean:
                return Decision(notes={s: "momentum: SPY below its average, all cash" for s in self.universe(hist)})
        scores: dict[str, float] = {}
        for symbol in self.universe(hist):
            rows = hist[symbol]
            end = cut_at(rows, last) - self.skip
            start = end - 1 - self.lookback
            if start >= 0:
                scores[symbol] = rows[end - 1].close / rows[start].close - 1.0
        return _ranked("momentum", scores, True, self.top_k)

    @staticmethod
    def search_space(rng: random.Random) -> dict[str, Any]:
        return {"lookback": rng.randint(21, 252), "skip": rng.randint(0, 21), "top_k": rng.randint(1, 10),
                "rebalance_days": rng.randint(1, 21), "trend_sma": 0 if rng.random() < 0.5 else rng.randint(50, 250)}

    def describe(self) -> str:
        trend = f", all cash while SPY is below its {self.trend_sma}-day average" if self.trend_sma else ""
        return (f"Momentum: holds the {names(self.top_k)} with the best {self.lookback}-day return "
                f"(skipping the last {self.skip} days), rebalanced every {self.every} trading days{trend}.")


class MeanRev(StockModel):
    family = "meanrev"
    PARAM_KEYS = ("lookback", "hold_days", "top_k", "long_sma")

    def __init__(self, params: dict[str, Any], symbols: list[str] | None = None) -> None:
        super().__init__(params, symbols)
        self.lookback = _int_param(params, "lookback", 5, 2, 10)
        self.every = _int_param(params, "hold_days", 5, 1, 10)
        self.top_k = _int_param(params, "top_k", 3, 1, 10)
        self.long_sma = _int_param(params, "long_sma", 0, 100, 250, allow_zero=True)

    def decide(self, hist: Hist, day: str) -> Decision:
        last = anchor_date(hist, self.every)
        if last is None:
            return Decision()
        scores: dict[str, float] = {}
        for symbol in self.universe(hist):
            rows = hist[symbol]
            end = cut_at(rows, last)
            if end - 1 - self.lookback < 0:
                continue
            if self.long_sma:
                mean = sma(rows, end, self.long_sma)
                if mean is None or rows[end - 1].close <= mean:
                    continue
            scores[symbol] = rows[end - 1].close / rows[end - 1 - self.lookback].close - 1.0
        return _ranked("meanrev", scores, False, self.top_k)

    @staticmethod
    def search_space(rng: random.Random) -> dict[str, Any]:
        return {"lookback": rng.randint(2, 10), "hold_days": rng.randint(1, 10), "top_k": rng.randint(1, 10),
                "long_sma": 0 if rng.random() < 0.5 else rng.randint(100, 250)}

    def describe(self) -> str:
        above = f" among names above their {self.long_sma}-day average" if self.long_sma else ""
        return (f"Mean reversion: buys the {names(self.top_k)} that fell the most over {self.lookback} days{above} "
                f"and holds them {self.every} trading days.")


class Trend(StockModel):
    family = "trend"
    PARAM_KEYS = ("fast", "slow", "max_names")

    def __init__(self, params: dict[str, Any], symbols: list[str] | None = None) -> None:
        super().__init__(params, symbols)
        self.fast = _int_param(params, "fast", 20, 5, 50)
        self.slow = _int_param(params, "slow", 100, 50, 250)
        self.max_names = _int_param(params, "max_names", 5, 1, 20)
        if self.fast >= self.slow:
            raise ValueError("fast must be shorter than slow")

    def decide(self, hist: Hist, day: str) -> Decision:
        scores: dict[str, float] = {}
        for symbol in self.universe(hist):
            rows = hist[symbol]
            slow = sma(rows, len(rows), self.slow)
            fast = sma(rows, len(rows), self.fast)
            if slow is not None and fast is not None and fast > slow:
                scores[symbol] = fast / slow
        decision = _ranked("trend", scores, True, self.max_names)
        chosen = list(decision.weights)
        decision.weights = _equal(chosen, len(chosen))
        return decision

    @staticmethod
    def search_space(rng: random.Random) -> dict[str, Any]:
        fast = rng.randint(5, 50)
        return {"fast": fast, "slow": rng.randint(max(50, fast + 1), 250), "max_names": rng.randint(1, 20)}

    def describe(self) -> str:
        return (f"Trend: holds up to {names(self.max_names)} whose {self.fast}-day average is above their "
                f"{self.slow}-day average, in equal parts.")


class BuyHold(StockModel):
    family = "buyhold"
    PARAM_KEYS = ("symbol",)

    def __init__(self, params: dict[str, Any], symbols: list[str] | None = None) -> None:
        super().__init__(params, symbols)
        symbol = params.get("symbol", BENCHMARK)
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a ticker")
        self.symbol = symbol

    def decide(self, hist: Hist, day: str) -> Decision:
        if not hist.get(self.symbol):
            return Decision()
        return Decision({self.symbol: 1.0}, {self.symbol: "buyhold"})

    def extra_symbols(self) -> list[str]:
        return [self.symbol]

    @staticmethod
    def search_space(rng: random.Random) -> dict[str, Any]:
        return {"symbol": BENCHMARK}

    def describe(self) -> str:
        return f"Buy and hold: keeps everything in {self.symbol}."


FAMILIES: dict[str, type[StockModel]] = {cls.family: cls for cls in (Momentum, MeanRev, Trend, BuyHold)}


def make_model(family: str, params: dict[str, Any], symbols: list[str] | None = None) -> StockModel:
    """The model of a family; ValueError for an unknown family, key or bad value."""
    cls = FAMILIES.get(str(family))
    if cls is None:
        raise ValueError(f"unknown stock family {family!r}")
    unknown = set(params) - set(cls.PARAM_KEYS)
    if unknown:
        raise ValueError(f"unknown {family} params: {', '.join(sorted(unknown))}")
    return cls(params, symbols)
