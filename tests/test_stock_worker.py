"""The stock trade tick (fleet/worker/stock_trade.py) against a fake host serving the
stock worker routes, plus the agent hooks for stock jobs. No database."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from fleet.stocks import backtest
from fleet.stocks.data import Bar, history_before, trading_days
from fleet.stocks.families import Decision, StockModel, make_model
from fleet.worker import config, stock_trade
from fleet.worker.agent import Agent, AgentOptions
from fleet.worker.stock_trade import StockTradeLoop, client_request_id, plan_orders
from tests.test_stock_models import BARS, SYMBOLS

MOMENTUM = {"lookback": 63, "skip": 0, "top_k": 3, "rebalance_days": 1, "trend_sma": 0}
UNIVERSE = SYMBOLS[1:9]  # SPY stays a signal only
SESSION = trading_days(BARS, SYMBOLS, 2024, 2024)[100]
PREVIOUS = trading_days(BARS, SYMBOLS, 2024, 2024)[99]


def _cents(symbol: str) -> int:
    return round(history_before(BARS, SESSION, [symbol])[symbol][-1].close * 100)


def _state(**over: Any) -> dict[str, Any]:
    state = {
        "kill": False,
        "assignment": {"id": 11, "mode": "paper", "status": "active", "symbols": UNIVERSE, "cash_cents": 600_000,
                       "reserved_cents": 0, "last_decision_date": None},
        "model": {"id": 4, "family": "momentum", "params": MOMENTUM},
        "positions": {"S01": 8, "S05": 30},
        "open_orders": [],
        "broker": {"environment": "paper", "market_open": True, "session_date": SESSION},
        "decision": {"due": True, "session_date": SESSION, "bars_through": PREVIOUS,
                     "ref_prices_cents": {s: _cents(s) for s in UNIVERSE}},
        "settings": {"stock_trade_tick_s": 30, "stock_decision_lead_min": 20},
    }
    state.update(over)
    return state


class FakeStockHost:
    """The stock worker routes: state per job, order batches, bars with an ETag, release."""

    def __init__(self, bars: dict[str, list[Bar]]) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.batches: list[dict[str, Any]] = []
        self.releases: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.set_bars(bars)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: Any = None, etag: str | None = None) -> None:
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                if etag:
                    self.send_header("ETag", etag)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                url = urlparse(self.path)
                outer.gets.append(url.path)
                if url.path == "/api/v1/data/stock_bars":
                    if self.headers.get("If-None-Match") == outer.etag:
                        return self._send(304, etag=outer.etag)
                    return self._send(200, outer.bars_body, outer.etag)
                if url.path == "/api/v1/stock_trade/state":
                    job_id = parse_qs(url.query).get("job_id", [""])[0]
                    if job_id not in outer.states:
                        return self._send(404, {"detail": "job not found"})
                    return self._send(200, outer.states[job_id])
                self._send(404, {"detail": "no route"})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/v1/stock_orders/request":
                    outer.batches.append(body)
                    state = outer.states[body["job_id"]]
                    state["assignment"]["last_decision_date"] = body["session_date"]
                    state["decision"]["due"] = False
                    return self._send(200, {"orders": [{"client_request_id": o["client_request_id"], "order_id": f"o{i}",
                                                        "status": "approved", "reason": None}
                                                       for i, o in enumerate(body["orders"])]})
                if self.path == "/api/v1/stock_trade/release":
                    outer.releases.append(body)
                    return self._send(200, {"cancelled": 0, "released": body.get("job_ids", [])})
                self._send(404, {"detail": "no route"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def set_bars(self, bars: dict[str, list[Bar]]) -> None:
        self.bars_body = {"generated_at": "now", "symbols": {s: [list(b) for b in rows] for s, rows in bars.items()}}
        self.etag = f'"{len(self.bars_body["symbols"]["SPY"])}"'

    def bar_gets(self) -> int:
        return self.gets.count("/api/v1/data/stock_bars")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _through(day: str) -> dict[str, list[Bar]]:
    return history_before(BARS, day, SYMBOLS)


@pytest.fixture()
def host() -> Any:
    h = FakeStockHost(_through(SESSION))
    yield h
    h.close()


def _loop(host: FakeStockHost, tmp_path: Any, **jobs: str) -> StockTradeLoop:
    agent = SimpleNamespace(
        conf={"host_url": host.url, "worker_token": "wtok"}, state_dir=str(tmp_path), kill=False, _clock=time.monotonic,
        options=SimpleNamespace(http_timeout=4.0, data_timeout=10.0, stock_trade_tick_s=None),
        trade_jobs={j: {"id": j, "lease_token": f"lt-{j}", "params": {}, "kind": "stock_trade"} for j in jobs.values()},
    )
    return StockTradeLoop(agent)


# ------------------------------------------------------------------ the tick


def test_a_due_decision_posts_one_batch_sells_first(host: FakeStockHost, tmp_path: Any) -> None:
    host.states["j1"] = _state()
    loop = _loop(host, tmp_path, a="j1")
    outcome = loop.run()[0]
    assert outcome["action"] == "posted" and len(host.batches) == 1
    batch = host.batches[0]
    assert batch["job_id"] == "j1" and batch["assignment_id"] == 11 and batch["session_date"] == SESSION
    orders = batch["orders"]
    sides = [o["side"] for o in orders]
    assert sides == sorted(sides, key=lambda s: s != "sell"), "sells first, then buys"
    st = host.states["j1"]
    refs = st["decision"]["ref_prices_cents"]
    equity = 600_000 + 8 * refs["S01"] + 30 * refs["S05"]
    weights = make_model("momentum", MOMENTUM, UNIVERSE).weights(history_before(BARS, SESSION, SYMBOLS), SESSION)
    assert len(weights) == 3
    expected = {}
    for s in UNIVERSE:
        target = math.floor(weights.get(s, 0.0) * equity / refs[s] + 1e-9)
        delta = target - {"S01": 8, "S05": 30}.get(s, 0)
        if delta:
            expected[s] = ("buy" if delta > 0 else "sell", abs(delta))
    assert {o["symbol"]: (o["side"], o["qty"]) for o in orders} == expected
    for o in orders:
        crid = hashlib.sha256(f"j1|{SESSION}|{o['symbol']}|{o['side']}".encode()).hexdigest()[:24]
        assert o["client_request_id"] == crid == client_request_id("j1", SESSION, o["symbol"], o["side"])
        assert o["ref_price_cents"] == refs[o["symbol"]]
        assert re.fullmatch(r"momentum (rank \d+/\d+|not ranked), w \d\.\d\d, target \d+ held \d+", o["rationale"]), o["rationale"]
    assert loop.run()[0]["reason"] == "not due", "the host marked the session decided"
    assert len(host.batches) == 1


def test_nothing_is_proposed_under_kill_halt_or_when_not_due(host: FakeStockHost, tmp_path: Any) -> None:
    host.states["k"] = _state(kill=True)
    host.states["h"] = _state(assignment=dict(_state()["assignment"], status="halted"))
    host.states["n"] = _state(decision=dict(_state()["decision"], due=False))
    loop = _loop(host, tmp_path, a="k", b="h", c="n")
    reasons = sorted(o["reason"] for o in loop.run())
    assert reasons == ["assignment halted", "kill", "not due"]
    assert host.batches == [] and host.bar_gets() == 0
    host.states["n"]["decision"]["due"] = True
    loop.agent.kill = True
    assert {o["reason"] for o in loop.run()} >= {"kill"} and host.batches == []


def test_bars_behind_the_decision_skip_the_tick(host: FakeStockHost, tmp_path: Any) -> None:
    host.set_bars(_through(PREVIOUS))  # the cache ends one session short
    host.states["j1"] = _state()
    loop = _loop(host, tmp_path, a="j1")
    assert loop.run()[0]["reason"] == "bars behind"
    assert host.bar_gets() == 2 and host.batches == [], "fetched again once, then skipped"
    host.set_bars(_through(SESSION))
    assert loop.run()[0]["action"] == "posted"


def test_open_orders_count_as_held(host: FakeStockHost, tmp_path: Any) -> None:
    base = plan_orders(_state(), _through(SESSION), "j1")
    buy = next(o for o in base if o["side"] == "buy")
    pending = [{"symbol": buy["symbol"], "side": "buy", "qty": buy["qty"], "filled_qty": 0, "status": "open"},
               {"symbol": "S05", "side": "sell", "qty": 30, "filled_qty": 0, "status": "approved"},
               {"symbol": "S07", "side": "buy", "qty": 99, "filled_qty": 0, "status": "cancelled"}]
    again = plan_orders(_state(open_orders=pending), _through(SESSION), "j1")
    assert buy["symbol"] not in {o["symbol"] for o in again}
    assert "S05" not in {o["symbol"] for o in again if o["side"] == "sell"}, "never sell what an open sell already covers"


def test_an_empty_decision_is_still_posted(host: FakeStockHost, tmp_path: Any) -> None:
    host.states["j1"] = _state(model={"id": 9, "family": "buyhold", "params": {"symbol": "SPY"}},
                               assignment=dict(_state()["assignment"], symbols=["S01"]), positions={})
    assert plan_orders(host.states["j1"], _through(SESSION), "j1") == [], "SPY is not in the assignment's symbols"
    loop = _loop(host, tmp_path, a="j1")
    assert loop.run()[0]["action"] == "posted"
    assert host.batches[0]["orders"] == [] and host.states["j1"]["assignment"]["last_decision_date"] == SESSION


def test_release_handshake_names_the_jobs(host: FakeStockHost, tmp_path: Any) -> None:
    loop = _loop(host, tmp_path, a="j1", b="j2")
    answer = loop.release_jobs(list(loop.agent.trade_jobs.values()))
    assert answer == {"cancelled": 0, "released": ["j1", "j2"]}
    assert host.releases[0]["job_ids"] == ["j1", "j2"]
    assert host.releases[0]["jobs"][0] == {"id": "j1", "lease_token": "lt-j1"}


def test_tick_schedule_follows_the_setting(host: FakeStockHost, tmp_path: Any) -> None:
    host.states["j1"] = _state(decision=dict(_state()["decision"], due=False))
    loop = _loop(host, tmp_path, a="j1")
    assert loop.maybe_run() is True and loop.maybe_run() is False, "30 s between ticks"
    assert loop.tick_seconds() == 30.0
    loop.agent.trade_jobs = {}
    loop._next_at = 0.0
    assert loop.maybe_run() is False, "no stock job, no tick"


# -------------------------------------------------------- live == backtest


class Spy(StockModel):
    family = "spy"

    def __init__(self, inner: StockModel) -> None:
        super().__init__({})
        self.inner, self.seen = inner, {}

    def decide(self, hist: dict[str, list[Bar]], day: str) -> Decision:
        self.seen[day] = copy.deepcopy(hist)
        return self.inner.decide(hist, day)


def test_the_live_decision_sees_exactly_what_the_backtest_saw() -> None:
    inner = make_model("momentum", MOMENTUM, UNIVERSE)
    in_backtest = Spy(inner)
    backtest.run(in_backtest, BARS, UNIVERSE, 2024, 2024, 5.0, lambda: False)
    live = Spy(inner)
    plan_orders(_state(), _through(SESSION), "j1", model=live)
    assert live.seen[SESSION] == in_backtest.seen[SESSION]
    assert inner.weights(live.seen[SESSION], SESSION) == inner.weights(in_backtest.seen[SESSION], SESSION)


# --------------------------------------------------------------- agent hooks


def test_agent_holds_stock_trade_jobs_in_trade_slots(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agent(state_dir=str(tmp_path), options=AgentOptions(trade_max_games=4))
    agent.conf = {"host_url": "http://127.0.0.1:9", "worker_token": "w", "worker_id": "wid"}
    agent.role = "trade"
    agent._start_job({"id": "s1", "lease_token": "t1", "kind": "stock_trade", "params": {"assignment_id": 3}})
    agent._start_job({"id": "n1", "lease_token": "t2", "kind": "trade", "params": {}})
    assert agent.trade_jobs["s1"]["kind"] == "stock_trade" and agent.running == {}
    assert agent.want_jobs() == 2, "a stock_trade job is one trade slot"
    assert {j["id"] for j in agent.build_heartbeat()["jobs"]} == {"s1", "n1"}
    assert [j["id"] for j in agent.stock_trade.held_jobs()] == ["s1"]
    groups: dict[str, list[str]] = {}
    monkeypatch.setattr(agent.trade, "release_jobs", lambda jobs: groups.setdefault("nfl", [j["id"] for j in jobs]) and {"released": ["n1"]})
    monkeypatch.setattr(agent.stock_trade, "release_jobs", lambda jobs: groups.setdefault("stock", [j["id"] for j in jobs]) and None)
    agent._release_trade_jobs("drain")
    assert groups == {"nfl": ["n1"], "stock": ["s1"]}
    assert agent.trade_jobs == {} and [r["id"] for r in agent.pending_releases] == ["s1"], "unconfirmed: carried in released[]"


def test_agent_builds_the_stock_context_and_ticks_the_stock_loop(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agent(state_dir=str(tmp_path))
    agent.conf = {"host_url": "http://127.0.0.1:9", "worker_token": "w", "worker_id": "wid"}
    calls: list[str] = []
    monkeypatch.setattr(stock_trade.stock_cache, "build_context",
                        lambda url, tok, state, job, timeout, data_timeout: calls.append(job["kind"]) or {"stock_bars_path": "/x"})
    assert agent._build_context({"id": "b", "kind": "stock_validate", "params": {}}) == {"stock_bars_path": "/x"}
    assert calls == ["stock_validate"]
    ticks: list[int] = []
    monkeypatch.setattr(agent.stock_trade, "maybe_run", lambda: ticks.append(1) or False)
    monkeypatch.setattr(agent.trade, "run", lambda: [])
    agent.role = "trade"
    agent._maybe_trade_tick()
    assert ticks == [1]
    agent._write_status()
    assert config.load_status(str(tmp_path))["stock_trade"]["jobs"] == []
