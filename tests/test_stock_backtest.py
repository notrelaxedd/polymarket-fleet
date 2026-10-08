"""The stock backtest (fleet/stocks/backtest.py), its no-lookahead guarantee, the batch
stock job kinds (fleet/worker/stock_jobs.py) and the worker bar cache
(fleet/worker/stock_cache.py). No database; the cache tests use a local HTTP server."""

from __future__ import annotations

import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from fleet.sim.control import JobStopped
from fleet.stocks import backtest
from fleet.stocks.data import Bar, close_on, trading_days
from fleet.stocks.families import Decision, StockModel, make_model
from fleet.worker import stock_cache
from fleet.worker.context import ContextError
from fleet.worker.jobs import JOBS
from fleet.worker.stock_jobs import resolve_years
from tests.test_stock_models import BARS, SYMBOLS, line_bars, make_bars

NEVER = lambda: False  # noqa: E731


def _run(model: StockModel, bars: dict[str, list[Bar]] = BARS, symbols: list[str] = SYMBOLS, years: tuple[int, int] = (2017, 2018),
         cost: float = 5.0, **kw: Any) -> dict[str, Any]:
    return backtest.run(model, bars, symbols, years[0], years[1], cost, NEVER, **kw)


class Recorder(StockModel):
    """Checks every history it is given and holds the first symbol."""

    family = "recorder"

    def __init__(self) -> None:
        super().__init__({})
        self.days: list[str] = []

    def decide(self, hist: dict[str, list[Bar]], day: str) -> Decision:
        for rows in hist.values():
            assert rows[-1].date < day, f"saw {rows[-1].date} on {day}"
        self.days.append(day)
        return Decision({"S01": 1.0})


class Oracle(StockModel):
    """Cheats by reading the full data: holds the symbol with the best return on the day
    `shift` sessions after the decision day (0: day d itself, 1: day d + 1)."""

    family = "oracle"

    def __init__(self, bars: dict[str, list[Bar]], shift: int) -> None:
        super().__init__({})
        self.bars, self.shift = bars, shift
        self.index = {b.date: i for i, b in enumerate(bars["SPY"])}

    def decide(self, hist: dict[str, list[Bar]], day: str) -> Decision:
        i = self.index[day] + self.shift
        if i >= len(self.bars["SPY"]) or i < 1:
            return Decision()
        moves = {s: self.bars[s][i].close / self.bars[s][i - 1].close for s in ("A", "B")}
        return Decision({max(moves, key=lambda s: (moves[s], s)): 1.0})


def _two_walks(seed: int = 5, reversal: bool = False) -> dict[str, list[Bar]]:
    rng = random.Random(seed)
    n = 4 * 252
    a, b = [100.0], [100.0]
    for i in range(1, n):
        if reversal:
            ra = 0.02 if i % 2 == 0 else -0.02
            rb = -ra
        else:
            ra, rb = rng.gauss(0, 0.01), rng.gauss(0, 0.01)
        a.append(a[-1] * (1 + ra))
        b.append(b[-1] * (1 + rb))
    return line_bars({"SPY": [100.0] * n, "A": a, "B": b})


# ---------------------------------------------------------------- lookahead


def test_the_model_only_ever_sees_bars_before_the_decision_day() -> None:
    model = Recorder()
    out = _run(model)
    days = trading_days(BARS, SYMBOLS, 2017, 2018)
    assert model.days == days and out["days"] == len(days)
    assert out["first_day"] == days[0] and out["last_day"] == days[-1]


def test_knowing_day_d_gives_no_edge_but_knowing_d_plus_1_would() -> None:
    """A decision on day d trades at d's close, so a model that peeks at day d's own
    move earns nothing from it; the same peek one day later would be a fortune, which
    shows the measurement would catch a leak."""
    bars = _two_walks()
    years = (2020, 2022)
    today = _run(Oracle(bars, 0), bars, ["A", "B"], years, cost=0.0)
    tomorrow = _run(Oracle(bars, 1), bars, ["A", "B"], years, cost=0.0)
    assert abs(today["sharpe"]) < 1.5 and abs(today["cagr"]) < 0.15
    assert tomorrow["sharpe"] > 10 and tomorrow["cagr"] > 1.0


def test_day_d_information_loses_when_the_next_day_reverses() -> None:
    bars = _two_walks(reversal=True)
    out = _run(Oracle(bars, 0), bars, ["A", "B"], (2020, 2022), cost=0.0)
    assert out["cagr"] < -0.9, "the stock that rose on d falls on d + 1, which is what the position earns"


# ------------------------------------------------------------------ mechanics


def test_buyhold_pays_the_cost_once_and_matches_the_benchmark() -> None:
    bars = line_bars({"SPY": [100.0] * 300})
    out = _run(make_model("buyhold", {"symbol": "SPY"}), bars, ["SPY"], (2020, 2021), cost=10.0)
    assert out["trades"] == 1 and out["max_drawdown"] == pytest.approx(0.001, rel=1e-3)
    assert out["exposure"] == pytest.approx(1 - 1 / 300), "cash on the first day: no history yet"
    assert out["benchmark"]["max_drawdown"] == pytest.approx(0.001, rel=1e-3)
    real = _run(make_model("buyhold", {"symbol": "SPY"}), years=(2017, 2019))
    for key in ("cagr", "sharpe", "max_drawdown", "trades", "days"):
        assert real[key] == pytest.approx(real["benchmark"][key]), key
    assert [y["year"] for y in real["per_year"]] == [2017, 2018, 2019] == [y["year"] for y in real["benchmark"]["per_year"]]


def test_equity_follows_the_closes_of_the_held_symbol() -> None:
    model = make_model("buyhold", {"symbol": "S03"})
    out = _run(model, symbols=["S03"], years=(2018, 2018), cost=0.0)
    days = trading_days(BARS, SYMBOLS, 2018, 2018)
    expected = close_on(BARS, "S03", days[-1]) / close_on(BARS, "S03", days[0]) - 1
    assert out["per_year"][0]["return"] == pytest.approx(expected, abs=1e-6)


def test_metrics_have_the_contract_keys() -> None:
    out = _run(make_model("momentum", {"lookback": 63, "skip": 5, "top_k": 3, "rebalance_days": 21, "trend_sma": 0}))
    for key in ("cagr", "ann_vol", "sharpe", "max_drawdown", "turnover", "trades", "exposure", "days", "per_year",
                "benchmark", "first_day", "last_day"):
        assert key in out
    assert set(out["per_year"][0]) == {"year", "return", "sharpe", "max_drawdown"}
    assert out["max_drawdown"] >= 0 and 0 <= out["exposure"] <= 1 and out["trades"] > 10
    json.dumps(out)


def test_resume_from_a_year_checkpoint_gives_the_same_result() -> None:
    states: list[dict[str, Any]] = []
    model = make_model("trend", {"fast": 10, "slow": 60, "max_names": 4}, SYMBOLS)
    full = _run(model, years=(2017, 2019), on_unit=lambda s, y: states.append(json.loads(json.dumps(s))))
    assert len(states) == 3
    assert _run(model, years=(2017, 2019), resume=states[1]) == full


def test_stop_between_units_raises_jobstopped() -> None:
    calls = []
    with pytest.raises(JobStopped):
        backtest.run(make_model("buyhold", {}), BARS, SYMBOLS, 2017, 2019, 5.0, lambda: bool(calls),
                     lambda s, y: calls.append(y))
    assert calls == [2017]


def test_clean_weights() -> None:
    assert backtest.clean_weights({"A": 0.8, "B": 0.8, "C": 1.0, "D": -1, "E": True}, {"A", "B", "D", "E"}) == {"A": 0.5, "B": 0.5}
    assert backtest.clean_weights(None, {"A"}) == {}


# ------------------------------------------------------------------ job kinds


def _bars_file(tmp_path: Any, bars: dict[str, list[Bar]] = BARS) -> str:
    path = tmp_path / "stock_bars.json"
    path.write_text(json.dumps({"generated_at": "2025-01-01T00:00:00Z", "symbols": {s: [list(b) for b in rows] for s, rows in bars.items()}}))
    return str(path)


def _job(kind: str, params: dict[str, Any], path: str, checkpoint: dict[str, Any] | None = None) -> tuple[Any, list]:
    emits: list = []
    params = dict(params, _context={"stock_bars_path": path})
    return JOBS[kind](params, checkpoint, lambda cp, p: emits.append((cp, p)), NEVER), emits


def test_stock_search_job(tmp_path: Any) -> None:
    path = _bars_file(tmp_path)
    params = {"n": 5, "seed": 2, "families": ["momentum", "trend"], "symbols": SYMBOLS + ["NOPE"], "years": [2017, 2018],
              "cost_bps": 5, "top_k": 2}
    out, emits = _job("stock_search", params, path)
    assert set(out) >= {"create_stock_models"} and len(out["create_stock_models"]) <= 2
    assert len([e for e in emits if e[0]["state"] is not None]) == 5 * 2, "one checkpoint per candidate-year"
    for entry in out["create_stock_models"]:
        assert set(entry) == {"family", "params", "params_hash", "summary", "backtest_metrics"}
    again, _ = _job("stock_search", params, path, checkpoint=emits[3][0])
    assert again == out


def test_stock_backtest_job_and_its_era_refusal(tmp_path: Any) -> None:
    path = _bars_file(tmp_path)
    params = {"model": {"family": "buyhold", "params": {"symbol": "SPY"}}, "symbols": ["SPY"], "years": [2017, 2018],
              "cost_bps": 5, "model_id": 7}
    out, emits = _job("stock_backtest", params, path)
    assert out["model_id"] == 7 and out["backtest_metrics"]["years"] == [2017, 2018]
    assert [p for _, p in emits] == [0.5, 1.0]
    resumed, _ = _job("stock_backtest", params, path, checkpoint=emits[0][0])
    assert resumed == out
    with pytest.raises(ValueError, match="validation era"):
        _job("stock_backtest", dict(params, years=[2020, 2023], validation_years=[2022, None]), path)
    ok, _ = _job("stock_backtest", dict(params, validation_years=[2022, None]), path)
    assert ok == out


def test_stock_validate_job_and_its_overlap_refusal(tmp_path: Any) -> None:
    path = _bars_file(tmp_path)
    model = {"family": "trend", "params": {"fast": 10, "slow": 60, "max_names": 3}, "backtest_metrics": {"years": [2017, 2021]}}
    params = {"model_id": 3, "model": model, "symbols": SYMBOLS, "years": [2022, 2023], "cost_bps": 5}
    out, _ = _job("stock_validate", params, path)
    assert out["model_id"] == 3 and out["validation_metrics"]["first_day"].startswith("2022")
    with pytest.raises(ValueError, match="overlap"):
        _job("stock_validate", dict(params, years=[2021, None]), path)
    by_days = dict(model, backtest_metrics={"first_day": "2017-01-03", "last_day": "2022-12-30"})
    with pytest.raises(ValueError, match="overlap"):
        _job("stock_validate", dict(params, model=by_days), path)
    with pytest.raises(ValueError, match="overlap"):
        _job("stock_validate", dict(params, search_years=[2018, 2022]), path)


def test_job_input_errors(tmp_path: Any) -> None:
    path = _bars_file(tmp_path)
    with pytest.raises(ValueError, match="stock_bars_path"):
        JOBS["stock_search"]({"years": [2017, 2018]}, None, lambda *a: None, NEVER)
    with pytest.raises(ValueError, match="none of the job's symbols"):
        _job("stock_search", {"years": [2017, 2018], "symbols": ["ZZZ"]}, path)
    with pytest.raises(ValueError, match="unknown stock family"):
        _job("stock_backtest", {"model": {"family": "x", "params": {}}, "years": [2017, 2017]}, path)


def test_resolve_years() -> None:
    assert resolve_years([2017, 2020]) == (2017, 2020)
    assert resolve_years([2017, None], now_year=2026) == (2017, 2025)
    assert resolve_years([2017, None], BARS, now_year=2030) == (2017, 2024), "capped by the data"
    with pytest.raises(ValueError):
        resolve_years([2020, 2019])
    with pytest.raises(ValueError):
        resolve_years("2020")


# --------------------------------------------------------------- bar cache


class _BarsServer:
    """GET /api/v1/data/stock_bars with ETag and If-None-Match; `fail` answers 503."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body, self.etag, self.fail, self.requests = body, '"v1"', False, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                outer.requests.append((self.path, self.headers.get("If-None-Match"), self.headers.get("Authorization")))
                if outer.fail:
                    self.send_response(503)
                    self.end_headers()
                    return
                if self.headers.get("If-None-Match") == outer.etag:
                    self.send_response(304)
                    self.send_header("ETag", outer.etag)
                    self.end_headers()
                    return
                raw = json.dumps(outer.body).encode()
                self.send_response(200)
                self.send_header("ETag", outer.etag)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_bar_cache_conditional_get_and_fallback(tmp_path: Any) -> None:
    small = make_bars(["SPY", "AAA"], seed=3)
    server = _BarsServer({"generated_at": "t", "symbols": {s: [list(b) for b in rows] for s, rows in small.items()}})
    try:
        state = str(tmp_path)
        with pytest.raises(stock_cache.StockCacheError):
            server.fail = True
            stock_cache.refresh_stock_bars(server.url, "tok", state, 5.0)
        server.fail = False
        path = stock_cache.refresh_stock_bars(server.url, "tok", state, 5.0)
        assert server.requests[-1] == ("/api/v1/data/stock_bars", None, "Bearer tok")
        memo = stock_cache.BarsMemo()
        bars = memo.load(path)
        assert bars == small and memo.load(path) is bars
        assert stock_cache.refresh_stock_bars(server.url, "tok", state, 5.0) == path
        assert server.requests[-1][1] == '"v1"', "the stored ETag goes out as If-None-Match"
        server.fail = True
        assert stock_cache.refresh_stock_bars(server.url, "tok", state, 5.0) == path, "falls back to the cached file"
        ctx = stock_cache.build_context(server.url, "tok", state, {"kind": "stock_search"}, 5.0, 5.0)
        assert ctx == {"stock_bars_path": path}
        with pytest.raises(ContextError):
            stock_cache.build_context(server.url, "tok", str(tmp_path / "empty"), {"kind": "stock_search"}, 5.0, 5.0)
        assert stock_cache.needs_context("stock_validate") and not stock_cache.needs_context("stock_trade")
    finally:
        server.close()
