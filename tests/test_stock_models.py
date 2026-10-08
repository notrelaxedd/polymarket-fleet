"""Stock model families, data helpers, metrics and the search (fleet/stocks). Pure, no
database, no network. make_bars() is the synthetic data the other stock tests share."""

from __future__ import annotations

import datetime as dt
import math
import random
from typing import Any

import pytest

from fleet.models.base import params_hash
from fleet.stocks import search
from fleet.stocks.data import Bar, history_before, last_date, parse_bars, trading_days
from fleet.stocks.families import FAMILIES, anchor_date, make_model
from fleet.stocks.metrics import Track, summarize, year_entry

SYMBOLS = ["SPY"] + [f"S{i:02d}" for i in range(12)]


def make_bars(symbols: list[str] = SYMBOLS, start: dt.date = dt.date(2015, 1, 2), end: dt.date = dt.date(2024, 12, 31),
              seed: int = 1, drifts: dict[str, float] | None = None) -> dict[str, list[Bar]]:
    """Weekday bars of independent random walks (1.5% daily vol, a drift per symbol)."""
    rng = random.Random(seed)
    price = {s: 100.0 for s in symbols}
    drift = {s: (drifts or {}).get(s, rng.uniform(-0.0002, 0.0008)) for s in symbols}
    out: dict[str, list[Bar]] = {s: [] for s in symbols}
    day = start
    while day <= end:
        if day.weekday() < 5:
            for s in symbols:
                o = price[s]
                price[s] = max(1.0, price[s] * (1 + drift[s] + rng.gauss(0, 0.015)))
                out[s].append(Bar(day.isoformat(), o, max(o, price[s]), min(o, price[s]), price[s], 1e6))
        day += dt.timedelta(days=1)
    return out


def line_bars(closes: dict[str, list[float]], start: dt.date = dt.date(2020, 1, 1)) -> dict[str, list[Bar]]:
    """Bars with the given closes on consecutive weekdays."""
    days: list[str] = []
    day = start
    while len(days) < max(len(v) for v in closes.values()):
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += dt.timedelta(days=1)
    return {s: [Bar(days[i], c, c, c, c, 1.0) for i, c in enumerate(v)] for s, v in closes.items()}


BARS = make_bars()


def _check_weights(w: dict[str, float], allowed: set[str]) -> None:
    assert set(w) <= allowed
    assert all(0 <= v <= 1 for v in w.values())
    assert sum(w.values()) <= 1 + 1e-9


# ------------------------------------------------------------------ families


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_weights_are_long_only_bounded_and_deterministic(family: str) -> None:
    rng = random.Random(7)
    days = trading_days(BARS, SYMBOLS, 2017, 2018)[::17]
    for _ in range(8):
        params = FAMILIES[family].search_space(rng)
        model = make_model(family, params, SYMBOLS)
        again = make_model(family, params, SYMBOLS)
        for day in days:
            hist = history_before(BARS, day, SYMBOLS)
            w = model.weights(hist, day)
            _check_weights(w, set(SYMBOLS))
            assert w == again.weights(hist, day) == model.weights(hist, day)


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_search_space_stays_in_the_contract_ranges(family: str) -> None:
    for seed in range(200):
        p = FAMILIES[family].search_space(random.Random(seed))
        make_model(family, p)  # validates every range
        if family == "momentum":
            assert 21 <= p["lookback"] <= 252 and 0 <= p["skip"] <= 21 and 1 <= p["top_k"] <= 10
            assert 1 <= p["rebalance_days"] <= 21 and (p["trend_sma"] == 0 or 50 <= p["trend_sma"] <= 250)
        elif family == "meanrev":
            assert 2 <= p["lookback"] <= 10 and 1 <= p["hold_days"] <= 10 and 1 <= p["top_k"] <= 10
            assert p["long_sma"] == 0 or 100 <= p["long_sma"] <= 250
        elif family == "trend":
            assert 5 <= p["fast"] <= 50 and 50 <= p["slow"] <= 250 and p["fast"] < p["slow"] and 1 <= p["max_names"] <= 20
        else:
            assert p == {"symbol": "SPY"}


def test_make_model_refuses_bad_input() -> None:
    with pytest.raises(ValueError, match="unknown stock family"):
        make_model("crystal_ball", {})
    with pytest.raises(ValueError, match="unknown momentum params"):
        make_model("momentum", {"lookback": 30, "leverage": 2})
    with pytest.raises(ValueError, match="lookback"):
        make_model("momentum", {"lookback": 5})
    with pytest.raises(ValueError, match="trend_sma"):
        make_model("momentum", {"trend_sma": 20})
    with pytest.raises(ValueError, match="fast must be shorter"):
        make_model("trend", {"fast": 50, "slow": 50})
    with pytest.raises(ValueError, match="whole number"):
        make_model("meanrev", {"top_k": 2.5})


def test_momentum_holds_the_best_lookback_returns() -> None:
    n = 60
    bars = line_bars({"SPY": [100.0] * n, "UP": [100 + i for i in range(n)], "MID": [100 + 0.5 * i for i in range(n)],
                      "DOWN": [100 - 0.5 * i for i in range(n)]})
    model = make_model("momentum", {"lookback": 21, "skip": 0, "top_k": 2, "rebalance_days": 1, "trend_sma": 0})
    day = "2099-01-01"
    d = model.decide(bars, day)
    assert d.weights == {"UP": 0.5, "MID": 0.5}
    assert d.notes["UP"] == "momentum rank 1/4" and d.notes["DOWN"] == "momentum rank 4/4"


def test_momentum_trend_filter_goes_to_cash_below_the_spy_average() -> None:
    n = 80
    up = {"SPY": [100 + i for i in range(n)], "A": [100 + i for i in range(n)]}
    down = {"SPY": [200 - i for i in range(n)], "A": [100 + i for i in range(n)]}
    params = {"lookback": 21, "skip": 0, "top_k": 1, "rebalance_days": 1, "trend_sma": 50}
    assert make_model("momentum", params).weights(line_bars(up), "2099-01-01") == {"A": 1.0}
    assert make_model("momentum", params).weights(line_bars(down), "2099-01-01") == {}
    no_spy = {"A": [100 + i for i in range(n)]}
    assert make_model("momentum", params).weights(line_bars(no_spy), "2099-01-01") == {}, "needs SPY in the data"


def test_momentum_only_changes_on_rebalance_anchors() -> None:
    model = make_model("momentum", {"lookback": 63, "skip": 0, "top_k": 3, "rebalance_days": 10, "trend_sma": 0}, SYMBOLS)
    days = trading_days(BARS, SYMBOLS, 2018, 2018)
    changes = []
    last = None
    for day in days:
        hist = history_before(BARS, day, SYMBOLS)
        w = model.weights(hist, day)
        if last is not None and w != last:
            changes.append(len(hist["SPY"]) % 10)
        last = w
    assert changes and set(changes) == {0}, "the weights only move on a day whose history is a multiple of 10 bars"
    assert anchor_date({"SPY": BARS["SPY"][:9]}, 10) is None


def test_meanrev_buys_the_biggest_losers_above_their_average() -> None:
    n = 120
    closes = {"SPY": [100.0] * n, "A": [100.0 + i for i in range(n)], "B": [100.0 + i for i in range(n)],
              "C": [300.0 - i for i in range(n)]}
    closes["A"][-1] = closes["A"][-4] - 10  # A fell hard over 3 days, still above its average
    closes["B"][-1] = closes["B"][-4] - 2
    bars = line_bars(closes)
    plain = make_model("meanrev", {"lookback": 3, "hold_days": 1, "top_k": 2, "long_sma": 0})
    assert plain.weights(bars, "2099-01-01") == {"A": 0.5, "C": 0.5}
    filtered = make_model("meanrev", {"lookback": 3, "hold_days": 1, "top_k": 2, "long_sma": 100})
    assert filtered.weights(bars, "2099-01-01") == {"A": 0.5, "B": 0.5}, "C is below its 100-day average"


def test_trend_equal_weights_names_with_fast_above_slow() -> None:
    n = 120
    bars = line_bars({"UP": [100 + i for i in range(n)], "UP2": [100 + 2 * i for i in range(n)],
                      "DOWN": [300 - i for i in range(n)]})
    d = make_model("trend", {"fast": 10, "slow": 50, "max_names": 5}).decide(bars, "2099-01-01")
    assert d.weights == {"UP": 0.5, "UP2": 0.5}
    capped = make_model("trend", {"fast": 10, "slow": 50, "max_names": 1}).weights(bars, "2099-01-01")
    assert capped == {"UP2": 1.0}, "the strongest fast/slow ratio wins the one slot"


def test_buyhold_holds_its_symbol_once_it_has_history() -> None:
    model = make_model("buyhold", {"symbol": "SPY"})
    assert model.weights({}, "2020-01-02") == {}
    assert model.weights({"SPY": BARS["SPY"][:3]}, "2020-01-02") == {"SPY": 1.0}
    assert model.extra_symbols() == ["SPY"]


def test_universe_limits_what_a_model_may_hold() -> None:
    model = make_model("momentum", {"lookback": 21, "skip": 0, "top_k": 10, "rebalance_days": 1, "trend_sma": 0},
                       ["S01", "S02"])
    day = trading_days(BARS, SYMBOLS, 2019, 2019)[5]
    assert set(model.weights(history_before(BARS, day, SYMBOLS), day)) <= {"S01", "S02"}


# ---------------------------------------------------------------------- data


def test_parse_bars_sorts_dedupes_and_drops_malformed_rows() -> None:
    body = {"generated_at": "x", "symbols": {
        "SPY": [["2020-01-03", 1, 2, 0.5, 1.5, 10], ["2020-01-02", 1, 2, 0.5, 1.2, 10], ["2020-01-03", 1, 2, 0.5, 1.6, 9],
                ["bad", 1, 1, 1, 1, 1], ["2020-01-06", 1, 1, 1, 0, 1], ["2020-01-07", 1, 1, 1, float("nan"), 1]],
        "EMPTY": [], "JUNK": "nope"}}
    bars = parse_bars(body)
    assert list(bars) == ["SPY"]
    assert [(b.date, b.close) for b in bars["SPY"]] == [("2020-01-02", 1.2), ("2020-01-03", 1.6)]
    assert last_date(bars) == "2020-01-03"
    with pytest.raises(ValueError):
        parse_bars(["not", "an", "object"])


def test_history_before_is_strictly_before_the_day() -> None:
    day = BARS["SPY"][300].date
    hist = history_before(BARS, day, ["SPY", "S01", "MISSING"])
    assert set(hist) == {"SPY", "S01"}
    assert hist["SPY"][-1].date < day and len(hist["SPY"]) == 300
    assert history_before(BARS, BARS["SPY"][0].date, ["SPY"]) == {}


# ------------------------------------------------------------------- metrics


def test_metrics_of_a_known_path() -> None:
    track = Track()
    for i in range(252):
        track.add_day(0.001 if i % 2 == 0 else -0.0005, exposure=0.5, traded=0.1, trades=1)
    m = summarize(track)
    assert m["days"] == 252 and m["trades"] == 252
    assert m["cagr"] == pytest.approx(track.equity - 1, abs=1e-6)
    assert m["exposure"] == pytest.approx(0.5) and m["turnover"] == pytest.approx(25.2)
    mean, std = 0.00025, math.sqrt(sum((r - 0.00025) ** 2 for r in [0.001, -0.0005] * 126) / 251)
    assert m["sharpe"] == pytest.approx(mean / std * math.sqrt(252), rel=1e-4)
    assert m["ann_vol"] == pytest.approx(std * math.sqrt(252), rel=1e-4)
    drop = Track()
    for r in (0.1, -0.5, 0.2):
        drop.add_day(r)
    assert summarize(drop)["max_drawdown"] == pytest.approx(0.5)
    assert year_entry(2020, drop)["return"] == pytest.approx(1.1 * 0.5 * 1.2 - 1)
    assert Track.from_dict(drop.to_dict()) == drop


# -------------------------------------------------------------------- search


def _search(checkpoint: dict[str, Any] | None = None, emits: list | None = None, **kw: Any) -> dict[str, Any]:
    args = dict(n=8, seed=3, families=["momentum", "meanrev", "trend"], top_k=3)
    args.update(kw)
    return search.run_search(BARS, SYMBOLS, args["families"], args["n"], args["seed"], 2017, 2018, 5.0, args["top_k"],
                             lambda cp, p: (emits if emits is not None else []).append((cp, p)), lambda: False, checkpoint)


def test_search_keeps_the_top_k_by_sharpe_with_enough_trades() -> None:
    emits: list = []
    out = _search(emits=emits)
    created = out["create_stock_models"]
    assert 1 <= len(created) <= 3 and out["evaluated"] == 8
    sharpes = [c["backtest_metrics"]["sharpe"] for c in created]
    assert sharpes == sorted(sharpes, reverse=True)
    for c in created:
        assert c["params_hash"] == params_hash(c["params"]) and c["family"] in ("momentum", "meanrev", "trend")
        assert c["backtest_metrics"]["trades"] >= search.MIN_TRADES
        assert c["summary"].count(". ") == 1 and c["summary"].endswith(".") and "Backtest 2017-2018" in c["summary"]
        assert chr(0x2014) not in c["summary"] and chr(0x2013) not in c["summary"]
    progress = [p for _, p in emits]
    assert progress == sorted(progress) and progress[-1] == pytest.approx(1.0)
    assert out == _search(), "same seed, same result"


def test_search_resumes_from_a_mid_candidate_checkpoint() -> None:
    emits: list = []
    full = _search(emits=emits)
    mid = next(cp for cp, _ in emits if cp["i"] == 4 and cp["state"] is not None)
    assert _search(checkpoint=mid) == full


def test_search_families_default_and_refusal() -> None:
    assert search.families_of(None) == ["momentum", "meanrev", "trend", "buyhold"]
    with pytest.raises(ValueError, match="unknown stock families"):
        search.families_of(["momentum", "astrology"])
    out = _search(families=["buyhold"], n=3)
    assert out["create_stock_models"] == [] and out["skipped_duplicates"] == 2, "one buyhold candidate, under 10 trades"
