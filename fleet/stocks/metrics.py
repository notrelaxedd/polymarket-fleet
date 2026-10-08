"""Metrics of a stock backtest (contract section 4), from streaming accumulators.

Track accumulates one equity path a day at a time (daily return, invested fraction,
traded value over equity, position changes) and serializes to a small dict, so a
backtest can checkpoint at every year boundary without keeping the daily series.
summarize() gives cagr, ann_vol, sharpe (rf 0, 252 days), max_drawdown (a positive
fraction), turnover (traded value over equity, both sides, per 252 days), trades,
exposure (mean invested fraction) and days.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

DAYS_PER_YEAR = 252


@dataclass
class Track:
    """One equity path, starting at equity 1.0."""

    days: int = 0
    sum_r: float = 0.0
    sum_r2: float = 0.0
    equity: float = 1.0
    peak: float = 1.0
    max_dd: float = 0.0
    exposure_sum: float = 0.0
    traded_sum: float = 0.0
    trades: int = 0

    def add_day(self, daily_return: float, exposure: float = 0.0, traded: float = 0.0, trades: int = 0) -> None:
        self.days += 1
        self.sum_r += daily_return
        self.sum_r2 += daily_return * daily_return
        self.equity *= 1.0 + daily_return
        self.peak = max(self.peak, self.equity)
        if self.peak > 0:
            self.max_dd = max(self.max_dd, 1.0 - self.equity / self.peak)
        self.exposure_sum += exposure
        self.traded_sum += traded
        self.trades += trades

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Track":
        if not isinstance(data, dict):
            return cls()
        fields = cls.__dataclass_fields__
        return cls(**{k: (int(v) if fields[k].type in ("int", int) else float(v)) for k, v in data.items() if k in fields})


def _r(value: float, digits: int = 6) -> float:
    return round(value, digits) if math.isfinite(value) else 0.0


def sharpe_and_vol(track: Track) -> tuple[float, float]:
    """(annualized Sharpe with rf 0, annualized volatility) of the daily returns."""
    if track.days < 2:
        return 0.0, 0.0
    mean = track.sum_r / track.days
    var = max(0.0, (track.sum_r2 - track.days * mean * mean) / (track.days - 1))
    std = math.sqrt(var)
    if std < 1e-12:
        return 0.0, 0.0
    return mean / std * math.sqrt(DAYS_PER_YEAR), std * math.sqrt(DAYS_PER_YEAR)


def summarize(track: Track) -> dict[str, Any]:
    """The whole-period metrics of one path."""
    sharpe, vol = sharpe_and_vol(track)
    years = track.days / DAYS_PER_YEAR if track.days else 0.0
    cagr = track.equity ** (1.0 / years) - 1.0 if years > 0 and track.equity > 0 else (-1.0 if track.equity <= 0 else 0.0)
    return {
        "cagr": _r(cagr), "ann_vol": _r(vol), "sharpe": _r(sharpe), "max_drawdown": _r(track.max_dd),
        "turnover": _r(track.traded_sum / years if years else 0.0), "trades": int(track.trades),
        "exposure": _r(track.exposure_sum / track.days if track.days else 0.0), "days": int(track.days),
    }


def year_entry(year: int, track: Track) -> dict[str, Any]:
    """{year, return, sharpe, max_drawdown} of one calendar year's path."""
    sharpe, _ = sharpe_and_vol(track)
    return {"year": int(year), "return": _r(track.equity - 1.0), "sharpe": _r(sharpe), "max_drawdown": _r(track.max_dd)}


def pct(value: Any) -> str:
    return f"{100.0 * float(value or 0.0):.1f}%"


def metrics_sentence(metrics: dict[str, Any]) -> str:
    """The second summary sentence: the backtest in plain words."""
    first, last = str(metrics.get("first_day") or "")[:4], str(metrics.get("last_day") or "")[:4]
    span = first if first == last else f"{first}-{last}"
    bench = metrics.get("benchmark") or {}
    versus = f" vs {pct(bench.get('cagr'))} for SPY" if bench else ""
    return (f"Backtest {span}: {pct(metrics.get('cagr'))} a year{versus}, Sharpe {float(metrics.get('sharpe') or 0):.2f}, "
            f"worst drawdown {pct(metrics.get('max_drawdown'))}, {int(metrics.get('trades') or 0)} trades.")
