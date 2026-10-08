"""End to end, step 9 (stocks on Alpaca), run by tests/test_e2e.py::test_stocks_end_to_end.

A real host (uvicorn + Postgres + the host loop), fake workers that use the worker's
own code (fleet.common.http heartbeats, the stock bar cache, the stock job functions
and StockTradeLoop) and the exchange's stock tasks against the in-memory Alpaca of
tests/fake_alpaca.py:

daily bars in stock_bars -> a stock_search job claimed by a model_search worker and run
by the worker job function on its cached bars -> stock_models rows -> a stock_validate
job -> the backtest gate makes the best model paper_ok -> the broker check reads the
fake account and clock -> a paper assignment -> the trade worker claims its stock_trade
job and StockTradeLoop decides 20 minutes before the close (15:40 on a normal day) ->
host approval -> the executor places cls orders -> the fake close fills them -> fills
booked, positions and cash right -> the marks 30 minutes after the close -> a second
assignment decides in the next window and the kill switch cancels its open order.

The host compares the broker's next_close with the real database clock, so the fake
Alpaca's session closes 20 minutes after the real now (SessionAlpaca): the decision is
the session's "15:40" whatever the wall clock says, and the exchange tasks get the
fake's time explicitly (fills at the close, marks after it).
"""
from __future__ import annotations

import math
import random
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

from fleet.common import http as worker_http
from fleet.worker import stock_cache
from fleet.worker.jobs import JOBS
from fleet.worker.stock_trade import StockTradeLoop
from host import db
from host.exchange.stock_tasks import StockTasks
from host.stocks.market import previous_weekday
from tests.conftest import heartbeat_body, insert_worker
from tests.fake_alpaca import NY, FakeAlpaca

SYMBOLS = ["SPY", "AAA", "BBB", "CCC"]
DRIFT = {"SPY": 0.0004, "AAA": 0.0012, "BBB": 0.0, "CCC": -0.0004}  # AAA trends up, so momentum likes it
BANKROLL = 1_000_000  # $10,000


class SessionAlpaca(FakeAlpaca):
    """FakeAlpaca whose one session closes at `close_at` (see the module doc)."""

    close_at: datetime

    def open_session(self, close_at: datetime) -> None:
        self.close_at = close_at
        self.now = datetime.now(timezone.utc)

    def session(self) -> date:
        return self.close_at.astimezone(NY).date()

    def clock(self) -> dict[str, Any]:
        is_open = self.now < self.close_at
        nxt = self.close_at + timedelta(days=1)
        return {"timestamp": self.now.isoformat(), "is_open": is_open,
                "next_open": (nxt - timedelta(hours=6, minutes=30)).isoformat(),
                "next_close": (self.close_at if is_open else nxt).isoformat()}

    def after_cutoff(self) -> bool:
        return self.close_at - timedelta(minutes=10) <= self.now < self.close_at

    def _calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        local = self.close_at.astimezone(NY)
        return [{"date": local.date().isoformat(), "open": "09:30", "close": local.strftime("%H:%M")}] \
            if start <= local.date() <= end else []


def series(last: date) -> dict[str, list[tuple[date, float]]]:
    """Seeded random walks on every weekday from 2019 through `last`."""
    rng, px = random.Random(7), {s: 100.0 for s in SYMBOLS}
    out: dict[str, list[tuple[date, float]]] = {s: [] for s in SYMBOLS}
    day = date(2019, 1, 2)
    while day <= last:
        if day.weekday() < 5:
            for s in SYMBOLS:
                px[s] *= math.exp(DRIFT[s] + rng.gauss(0, 0.01))
                out[s].append((day, round(px[s], 2)))
        day += timedelta(days=1)
    return out


def load_bars(conn: psycopg.Connection, bars: dict[str, list[tuple[date, float]]]) -> None:
    """What the exchange's daily feed leaves behind: instruments and adjusted 1Day bars."""
    with conn.transaction():
        for s, rows in bars.items():
            conn.execute("INSERT INTO instruments (symbol, tradable, status, fetched_at) VALUES (%s, true, 'active', now())", (s,))
            with conn.cursor().copy("COPY stock_bars (symbol, timeframe, ts, open, high, low, close, volume, feed)"
                                    " FROM STDIN") as copy:
                for day, c in rows:
                    copy.write_row((s, "1Day", datetime.combine(day, dtime(0), tzinfo=NY), c, c, c, c, 1000, "sip"))
            conn.execute("UPDATE instruments SET bars_count = %s, bars_through = %s WHERE symbol = %s",
                         (len(rows), datetime.combine(rows[-1][0], dtime(0), tzinfo=NY), s))


def load_session_bars(conn: psycopg.Connection, session: date, closes: dict[str, Decimal], fetched_at: datetime) -> None:
    """The session's own daily bars, fetched after its close (what the marks wait for)."""
    for s, c in closes.items():
        conn.execute("INSERT INTO stock_bars (symbol, timeframe, ts, open, high, low, close, volume, feed)"
                     " VALUES (%s, '1Day', %s, %s, %s, %s, %s, 1000, 'sip')",
                     (s, datetime.combine(session, dtime(0), tzinfo=NY), c, c, c, c))
        conn.execute("UPDATE instruments SET fetched_at = %s WHERE symbol = %s", (fetched_at, s))


class FakeWorker:
    """A worker driven by the test: real heartbeats and job posts over HTTP."""

    def __init__(self, host: Any, conn: psycopg.Connection, name: str, role: str) -> None:
        w = insert_worker(conn, name, role=role)
        self.host, self.id, self.token, self.role, self.held = host, w.id, w.token, role, []

    def beat(self, **kw: Any) -> dict[str, Any]:
        jobs = [{"id": j["id"], "lease_token": j["lease_token"]} for j in self.held]
        body = heartbeat_body(reported_role=self.role, jobs=jobs, **kw)
        reply = worker_http.post_json(f"{self.host.url}/api/v1/workers/{self.id}/heartbeat", body, token=self.token)
        self.held += reply["claimed"]
        return reply

    def run_batch(self, state_dir: str) -> dict[str, Any]:
        """Claim one job and run it the way the runner does: context, job function, /complete."""
        job = self.beat(want_job=True)["claimed"][0]
        ctx = stock_cache.build_context(self.host.url, self.token, state_dir, job, timeout=5.0, data_timeout=30.0)
        result = JOBS[job["kind"]](dict(job["params"], _context=ctx), None, lambda c, p: None, lambda: False)
        worker_http.post_json(f"{self.host.url}/api/v1/jobs/{job['id']}/complete",
                              {"lease_token": job["lease_token"], "result": result}, token=self.token)
        self.held.remove(job)
        return result


def trade_loop(worker: FakeWorker, state_dir: str) -> StockTradeLoop:
    agent = SimpleNamespace(conf={"host_url": worker.host.url, "worker_token": worker.token}, state_dir=state_dir,
                            options=SimpleNamespace(http_timeout=5.0, data_timeout=30.0, stock_trade_tick_s=None),
                            trade_jobs={}, kill=False, _clock=time.monotonic)
    return StockTradeLoop(agent)


def hold(loop: StockTradeLoop, worker: FakeWorker) -> None:
    worker.beat(want_job=False, want_jobs=2)
    loop.agent.trade_jobs = {j["id"]: dict(j) for j in worker.held}


def rows(conn: psycopg.Connection, sql: str, *args: Any) -> list[dict[str, Any]]:
    return conn.execute(sql, args).fetchall()


def phase_stocks(host: Any, tmp_path: Any, wait_for: Callable[..., Any]) -> None:
    conn = psycopg.connect(host.database_url, autocommit=True, row_factory=dict_row)
    pool = db.make_pool(host.database_url, min_size=1, max_size=4)
    fake = SessionAlpaca(cash="100000")
    fake.open_session((datetime.now(timezone.utc) + timedelta(minutes=20)).replace(second=0, microsecond=0))
    tasks = StockTasks(pool, clock=lambda: fake.now, client_factory=fake.client)
    try:
        _run(host, conn, fake, tasks, str(tmp_path / "stock-workers"))
    finally:
        pool.close()
        conn.close()


def _run(host: Any, conn: psycopg.Connection, fake: SessionAlpaca, tasks: StockTasks, state_dir: str) -> None:
    session = fake.session()
    bars = series(previous_weekday(session))  # the feed has every bar through the previous session
    load_bars(conn, bars)
    host.post("/api/settings", {"stock_symbols": SYMBOLS, "stock_backtest_years": [2020, 2022],
                                "stock_validation_years": [2023, 2024], "stock_max_order_cents": 1_000_000,
                                "stock_max_position_cents": 1_000_000})

    # search -> stock_models (the job function on the worker's cached bars)
    searcher = FakeWorker(host, conn, "stock-search", "model_search")
    search = host.post("/api/stocks/jobs", {"kind": "stock_search", "params": {
        "n": 8, "seed": 1, "families": ["momentum", "trend"], "top_k": 2}}, expect=201)
    assert search["params"]["symbols"] == SYMBOLS and search["params"]["years"] == [2020, 2022]
    result = searcher.run_batch(state_dir)
    assert len(result["create_stock_models"]) == 2 and result["evaluated"] == 8
    assert host.job(str(search["id"]))["status"] == "succeeded"
    models = host.get("/api/stocks/models")
    assert len(models) == 2 and {m["status"] for m in models} == {"candidate"}, "no validation yet"
    best = models[0]  # best backtest Sharpe first
    assert best["backtest_metrics"]["sharpe"] >= 0.5 and best["lineage_id"] == best["id"]

    # validate on the held-out years -> paper_ok
    validator = FakeWorker(host, conn, "stock-validate", "backtest")
    job = host.post("/api/stocks/jobs", {"kind": "stock_validate", "params": {"model_id": best["id"]}}, expect=201)
    assert job["params"]["years"] == [2023, 2024] and job["params"]["search_years"] == [2020, 2022]
    validated = validator.run_batch(state_dir)
    assert validated["model_id"] == best["id"] and validated["validation_metrics"]["sharpe"] > 0
    model = next(m for m in host.get("/api/stocks/models") if m["id"] == best["id"])
    assert model["status"] == "paper_ok" and model["validation_metrics"]["first_day"].startswith("2023")

    # the broker check, then a paper assignment
    assert tasks.run_task("stock_broker", fake.now)["market_open"] is True
    broker = host.get("/api/stocks/summary")["broker"]
    assert broker["environment"] == "paper" and broker["session_date"] == session.isoformat()
    a = host.post("/api/stocks/assignments", {"model_id": best["id"], "mode": "paper", "bankroll_cents": BANKROLL,
                                              "symbols": SYMBOLS}, expect=201)

    # the trade worker holds the stock_trade job and decides in the window
    trader = FakeWorker(host, conn, "stock-trade", "trade")
    loop = trade_loop(trader, state_dir)
    hold(loop, trader)
    assert list(loop.agent.trade_jobs) == [a["job_id"]]
    outcome = loop.run()[0]
    assert outcome["action"] == "posted" and outcome["session_date"] == session.isoformat(), outcome
    placed = rows(conn, "SELECT * FROM stock_orders WHERE assignment_id = %s ORDER BY created_at", a["id"])
    assert placed and {o["status"] for o in placed} == {"approved"}, [(o["status"], o["reason"]) for o in placed]
    assert {o["side"] for o in placed} == {"buy"} and all(o["rationale"] for o in placed)
    assert loop.run()[0]["reason"] == "not due", "the host recorded the decision for this session"
    row = rows(conn, "SELECT * FROM stock_assignments WHERE id = %s", a["id"])[0]
    assert row["last_decision_date"] == session and row["reserved_cents"] > 0
    assert row["cash_cents"] + row["reserved_cents"] == BANKROLL

    # the executor places market-on-close orders at Alpaca
    assert tasks.run_task("stock_executor", fake.now)["submitted"] == len(placed)
    remote = {o["client_order_id"]: o for o in fake.orders.values()}
    for o in placed:
        r = remote[str(o["id"])]
        assert (r["symbol"], r["side"], r["qty"], r["type"], r["time_in_force"]) == (o["symbol"], "buy", str(o["qty"]), "market", "cls")
    assert {o["status"] for o in rows(conn, "SELECT status FROM stock_orders")} == {"open"}

    # the close fills them; the poll books the fills
    last = {s: Decimal(str(bars[s][-1][1])) for s in SYMBOLS}
    fake.close_prices = {s: (p * Decimal("1.01")).quantize(Decimal("0.01")) for s, p in last.items()}
    fake.current_prices = dict(fake.close_prices)
    fake.now = fake.close_at + timedelta(minutes=1)
    assert fake.run_close() == len(placed)
    tasks.run_task("stock_executor", fake.now)
    cost = 0
    for o in placed:
        o2 = rows(conn, "SELECT * FROM stock_orders WHERE id = %s", o["id"])[0]
        assert (o2["status"], o2["filled_qty"], o2["reserved_cents"]) == ("filled", o["qty"], 0)
        pos = rows(conn, "SELECT * FROM stock_positions WHERE assignment_id = %s AND symbol = %s", a["id"], o["symbol"])[0]
        paid = int((o["qty"] * fake.close_prices[o["symbol"]] * 100).quantize(Decimal("1")))
        assert (pos["qty"], pos["cost_cents"]) == (o["qty"], paid)
        cost += paid
    row = rows(conn, "SELECT * FROM stock_assignments WHERE id = %s", a["id"])[0]
    assert (row["cash_cents"], row["reserved_cents"]) == (BANKROLL - cost, 0)
    assert len(rows(conn, "SELECT * FROM stock_fills")) == len(placed)

    # the broker check after the close finds Alpaca and our book agree; the marks
    assert tasks.run_task("stock_broker", fake.now)["mismatches"] == []
    fake.now = fake.close_at + timedelta(minutes=31)
    assert tasks.run_task("stock_marks", fake.now)["waiting_for_bars"], "the mark waits for the session's bars"
    load_session_bars(conn, session, fake.close_prices, fake.now)  # the feed's run after stock_bars_hour
    marked = tasks.run_task("stock_marks", fake.now)
    assert marked["marked"] == 1 and marked["session"] == session.isoformat(), marked
    mark = rows(conn, "SELECT * FROM stock_marks WHERE assignment_id = %s", a["id"])[0]
    value = sum(int((o["qty"] * fake.close_prices[o["symbol"]] * 100).quantize(Decimal("1"))) for o in placed)
    assert (mark["session_date"], mark["equity_cents"]) == (session, BANKROLL - cost + value)
    assert tasks.run_task("stock_marks", fake.now)["marked"] == 0, "a session is marked once"

    # the next decision window: the first assignment already decided this session, a
    # second one decides, its order reaches Alpaca, and the kill switch cancels it
    fake.open_session((datetime.now(timezone.utc) + timedelta(minutes=20)).replace(second=0, microsecond=0))
    tasks.run_task("stock_broker", fake.now)
    b = host.post("/api/stocks/assignments", {"model_id": best["id"], "mode": "paper", "bankroll_cents": BANKROLL,
                                              "symbols": SYMBOLS}, expect=201)
    hold(loop, trader)
    by_job = {o["job_id"]: o for o in loop.run()}
    assert by_job[a["job_id"]]["reason"] == "not due", "one decision per assignment per session"
    assert by_job[b["job_id"]]["action"] == "posted", by_job[b["job_id"]]
    tasks.run_task("stock_executor", fake.now)
    opened = rows(conn, "SELECT * FROM stock_orders WHERE assignment_id = %s", b["id"])
    assert opened and {o["status"] for o in opened} == {"open"}
    host.post("/api/kill")
    assert {o["status"] for o in rows(conn, "SELECT status FROM stock_orders WHERE assignment_id = %s", b["id"])} == {"cancel_requested"}
    tasks.run_task("stock_executor", fake.now)
    closed = rows(conn, "SELECT * FROM stock_orders WHERE assignment_id = %s", b["id"])
    assert {(o["status"], o["reserved_cents"]) for o in closed} == {("cancelled", 0)}
    assert {fake.by_client(str(o["id"]))["status"] for o in closed} == {"canceled"}
    after = {r["id"]: r for r in rows(conn, "SELECT * FROM stock_assignments WHERE id IN (%s, %s)", a["id"], b["id"])}
    assert {r["status"] for r in after.values()} == {"halted"}
    assert (after[b["id"]]["cash_cents"], after[b["id"]]["reserved_cents"]) == (BANKROLL, 0)
    assert rows(conn, "SELECT count(*) AS n FROM stock_positions WHERE assignment_id = %s AND qty > 0", a["id"])[0]["n"] == len(placed), \
        "the kill keeps positions"
    assert all(o["reason"] == "kill" for o in loop.run()), "nothing is decided under kill"
    page = host.client.get("/stocks")
    assert page.status_code == 200 and "PAPER" in page.text and f"{best['family']}" in page.text
