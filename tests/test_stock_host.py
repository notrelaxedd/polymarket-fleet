"""Host stock routes and logic (contract sections 3 and 5): the bar feed with its ETag,
the worker's trade state and order request over HTTP, the owner routes and their gates,
results into stock_models, eligibility, job params and the CLI."""
from __future__ import annotations

from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

from host import queue
from host.errors import BadRequest
from host.stocks import eligibility, jobparams_stocks
from host.stocks.market import previous_weekday
from tests.conftest import insert_job, insert_worker
from tests.stock_host_helpers import (
    GOOD_BT, GOOD_VAL, add_bars, assignment_row, audit, db_now, insert_stock_model, lease, order, set_broker,
    set_setting, stock_setup,
)


# ------------------------------------------------------------------ bar feed


def test_stock_bars_feed_with_etag(client, conn):
    w = insert_worker(conn, "bars")
    add_bars(conn, "SPY", [previous_weekday(db_now(conn).date())], close=401.5)
    assert client.get("/api/v1/data/stock_bars").status_code == 401
    r = client.get("/api/v1/data/stock_bars", headers=w.headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"generated_at", "symbols"} and list(body["symbols"]) == ["SPY"]
    day, o, h, low, c, v = body["symbols"]["SPY"][0]
    assert day == previous_weekday(db_now(conn).date()).isoformat() and c == 401.5 and v == 1000
    etag = r.headers["etag"]
    again = client.get("/api/v1/data/stock_bars", headers={**w.headers, "If-None-Match": etag})
    assert again.status_code == 304
    add_bars(conn, "QQQ", [previous_weekday(db_now(conn).date())])
    changed = client.get("/api/v1/data/stock_bars", headers={**w.headers, "If-None-Match": etag})
    assert changed.status_code == 200 and set(changed.json()["symbols"]) == {"SPY", "QQQ"}


# ------------------------------------------------------------------ worker trade routes


def test_state_shape_and_decision_due(client, conn):
    s = stock_setup(conn)
    url = f"/api/v1/stock_trade/state?job_id={s.job_id}&lease_token={s.lease_token}"
    r = client.get(url, headers=s.worker.headers)
    assert r.status_code == 200, r.text
    state = r.json()
    assert set(state) >= {"kill", "assignment", "model", "positions", "open_orders", "broker", "decision", "settings"}
    assert state["kill"] is False and state["assignment"]["id"] == s.assignment["id"]
    assert set(state["assignment"]) == {"id", "mode", "status", "symbols", "cash_cents", "reserved_cents", "last_decision_date"}
    assert state["model"]["family"] == "momentum" and state["positions"] == {} and state["open_orders"] == []
    assert state["broker"]["environment"] == "paper" and state["broker"]["session_date"] == s.session.isoformat()
    d = state["decision"]
    assert d["due"] is True and d["bars_through"] == previous_weekday(s.session).isoformat()
    assert d["ref_prices_cents"] == {"SPY": 40_000, "AAPL": 10_000}
    assert state["settings"] == {"stock_trade_tick_s": 30, "stock_decision_lead_min": 20, "stock_price_band": 0.05,
                                 "stock_max_order_cents": 100_000, "stock_max_position_cents": 250_000}
    r = client.post("/api/v1/stock_orders/request", json=s.body([order(qty=2)]), headers=s.worker.headers)
    assert r.status_code == 200 and r.json()["orders"][0]["status"] == "approved"
    state = client.get(url, headers=s.worker.headers).json()
    assert state["decision"]["due"] is False, "one decision per session"
    assert state["open_orders"][0]["qty"] == 2 and state["assignment"]["last_decision_date"] == s.session.isoformat()


def test_decision_not_due_on_stale_bars_or_early(client, conn):
    s = stock_setup(conn)
    url = f"/api/v1/stock_trade/state?job_id={s.job_id}"
    conn.execute("DELETE FROM stock_bars WHERE symbol = 'AAPL' AND ts = (SELECT max(ts) FROM stock_bars WHERE symbol = 'AAPL')")
    state = client.get(url, headers=s.worker.headers).json()
    assert state["decision"]["due"] is False and state["decision"]["ref_prices_cents"]["AAPL"] == 10_000
    s2 = stock_setup(conn, symbols=("SPY",))
    set_broker(conn, next_close=db_now(conn) + timedelta(minutes=40), session_date=s2.session)
    state = client.get(f"/api/v1/stock_trade/state?job_id={s2.job_id}", headers=s2.worker.headers).json()
    assert state["decision"]["due"] is False, "40 minutes before the close is before the 20 minute lead"


def test_holiday_gap_is_accepted_when_the_feed_ran_after_the_missing_day(conn):
    from host.stocks import market

    s = stock_setup(conn, symbols=("SPY",))
    expected = previous_weekday(s.session)
    add_bars(conn, "SPY", [previous_weekday(expected)])
    conn.execute("DELETE FROM stock_bars WHERE symbol = 'SPY' AND (ts AT TIME ZONE 'America/New_York')::date = %s",
                 (expected,))
    prices = market.ref_prices(conn, ["SPY"], s.session)
    ok, through = market.bars_reach_previous_session(conn, ["SPY"], prices, s.session)
    assert through < expected and ok is True, "the feed ran (fetched_at now) after the missing weekday: a holiday"
    conn.execute("UPDATE instruments SET fetched_at = now() - interval '30 days'")
    assert market.bars_reach_previous_session(conn, ["SPY"], prices, s.session)[0] is False, "a stalled feed"


def test_state_refuses_other_workers_and_bad_tokens(client, conn):
    s = stock_setup(conn)
    other = insert_worker(conn, "other", role="trade")
    assert client.get(f"/api/v1/stock_trade/state?job_id={s.job_id}", headers=other.headers).status_code == 409
    bad = f"/api/v1/stock_trade/state?job_id={s.job_id}&lease_token=00000000-0000-0000-0000-000000000000"
    assert client.get(bad, headers=s.worker.headers).status_code == 409
    assert client.get("/api/v1/stock_trade/state?job_id=nope", headers=s.worker.headers).status_code == 404


def test_release_route(client, conn):
    s = stock_setup(conn)
    client.post("/api/v1/stock_orders/request", json=s.body([order()]), headers=s.worker.headers)
    r = client.post("/api/v1/stock_trade/release", json={"job_ids": [s.job_id]}, headers=s.worker.headers)
    assert r.status_code == 200 and r.json() == {"cancelled": 1, "released": [s.job_id]}


def test_stock_trade_job_counts_as_a_trade_slot(conn):
    from host.heartbeat import _trade_slots_left

    s = stock_setup(conn)
    set_setting(conn, "trade_max_games", 2)
    assert _trade_slots_left(conn, s.worker.id) == 1


# ------------------------------------------------------------------ owner routes


def test_create_assignment_route_and_gates(client, conn):
    broker = set_broker(conn, cash_cents=1_500_000)
    add_bars(conn, "SPY", [previous_weekday(broker["session_date"])])
    add_bars(conn, "XOM", [previous_weekday(broker["session_date"])], tradable=False)
    model = insert_stock_model(conn)
    body = {"model_id": model["id"], "mode": "paper", "bankroll_cents": 1_000_000, "symbols": ["SPY"]}
    r = client.post("/api/stocks/assignments", json=body)
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["cash_cents"] == 1_000_000 and a["status"] == "active" and a["symbols"] == ["SPY"]
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (a["job_id"],)).fetchone()
    assert job["kind"] == "stock_trade" and job["role"] == "trade" and job["max_expiries"] is None
    assert job["params"] == {"assignment_id": a["id"]} and audit(conn, "stock_assignment_created")
    over = client.post("/api/stocks/assignments", json={**body, "bankroll_cents": 600_000})
    assert over.status_code == 409 and "cash" in over.json()["detail"], "1.5M cash minus the 1M already assigned"
    assert client.post("/api/stocks/assignments", json={**body, "symbols": ["XOM"]}).status_code == 400
    assert client.post("/api/stocks/assignments", json={**body, "symbols": ["NOPE"]}).status_code == 400
    assert client.post("/api/stocks/assignments", json={**body, "mode": "live"}).status_code == 400
    cand = insert_stock_model(conn, status="candidate")
    assert client.post("/api/stocks/assignments", json={**body, "model_id": cand["id"]}).status_code == 400
    set_broker(conn, checked_at=db_now(conn) - timedelta(minutes=10))
    stale = client.post("/api/stocks/assignments", json={**body, "bankroll_cents": 1000})
    assert stale.status_code == 400 and "broker check" in stale.json()["detail"]
    set_broker(conn, cash_cents=1_500_000)
    set_setting(conn, "stock_max_assignments", 1)
    assert client.post("/api/stocks/assignments", json={**body, "bankroll_cents": 1000}).status_code == 409


def test_live_assignment_needs_live_switch_and_live_eligible(client, conn):
    broker = set_broker(conn, environment="live")
    add_bars(conn, "SPY", [previous_weekday(broker["session_date"])])
    model = insert_stock_model(conn, status="live_eligible")
    body = {"model_id": model["id"], "mode": "live", "bankroll_cents": 1000, "symbols": ["SPY"]}
    r = client.post("/api/stocks/assignments", json=body)
    assert r.status_code == 400 and "live trading is off" in r.json()["detail"]
    set_setting(conn, "live_enabled", True)
    paper_ok = insert_stock_model(conn, status="paper_ok")
    assert client.post("/api/stocks/assignments", json={**body, "model_id": paper_ok["id"]}).status_code == 400
    assert client.post("/api/stocks/assignments", json=body).status_code == 201
    assert client.post("/api/stocks/assignments", json=body).status_code == 409, "one live assignment per model"


def test_halt_resume_close_routes(client, conn):
    s = stock_setup(conn, symbols=("SPY",))
    aid = s.assignment["id"]
    client.post("/api/v1/stock_orders/request", json=s.body([order(symbol="SPY")]), headers=s.worker.headers)
    assert client.post(f"/api/stocks/assignments/{aid}/halt", json={"reason": "look"}).json()["status"] == "halted"
    assert client.post(f"/api/stocks/assignments/{aid}/resume").json()["status"] == "active"
    conn.execute("INSERT INTO stock_positions (assignment_id, symbol, qty, cost_cents) VALUES (%s, 'SPY', 1, 40000)", (aid,))
    r = client.post(f"/api/stocks/assignments/{aid}/close")
    assert r.status_code == 409 and "SPY" in r.json()["detail"]
    conn.execute("UPDATE stock_positions SET qty = 0 WHERE assignment_id = %s", (aid,))
    r = client.post(f"/api/stocks/assignments/{aid}/close")
    assert r.status_code == 200 and r.json()["status"] == "closed"
    assert conn.execute("SELECT status FROM jobs WHERE id = %s", (s.job_id,)).fetchone()["status"] == "cancel_requested"
    assert client.post(f"/api/stocks/assignments/{aid}/resume").status_code == 409
    assert client.post("/api/stocks/assignments/999999/halt").status_code == 404


def test_close_refuses_active_orders(client, conn):
    s = stock_setup(conn, symbols=("SPY",))
    client.post("/api/v1/stock_orders/request", json=s.body([order(symbol="SPY")]), headers=s.worker.headers)
    r = client.post(f"/api/stocks/assignments/{s.assignment['id']}/close")
    assert r.status_code == 409 and "active orders" in r.json()["detail"]


def test_retire_summary_and_models_routes(client, conn):
    s = stock_setup(conn)
    assert client.get("/api/stocks/models").json()[0]["id"] == s.model["id"]
    summary = client.get("/api/stocks/summary").json()
    assert summary["models"] == {"paper_ok": 1} and summary["assignments"][0]["id"] == s.assignment["id"]
    assert summary["broker"]["environment"] == "paper" and summary["kill"] is False
    r = client.post(f"/api/stocks/models/{s.model['id']}/retire")
    assert r.status_code == 200 and r.json()["status"] == "retired"
    assert assignment_row(conn, s.assignment["id"])["status"] == "halted"


def test_jobs_route_fills_params(client, conn):
    year = jobparams_stocks.last_complete_year()
    r = client.post("/api/stocks/jobs", json={"kind": "stock_search", "params": {"n": 10, "families": ["momentum"]}})
    assert r.status_code == 201, r.text
    p = r.json()["params"]
    assert p["n"] == 10 and p["families"] == ["momentum"] and p["seed"] == 0 and p["top_k"] == 5
    assert p["symbols"][0] == "SPY" and p["cost_bps"] == 5 and p["years"] == [2017, 2023]
    assert r.json()["kind"] == "stock_search" and r.json()["role"] == "model_search"
    set_setting(conn, "stock_backtest_years", [2017, None])
    assert client.post("/api/stocks/jobs", json={"kind": "stock_search"}).json()["params"]["years"] == [2017, 2023]
    assert client.post("/api/stocks/jobs", json={"kind": "stock_search", "params": {"years": [2018, 2024]}}).status_code == 400
    assert client.post("/api/stocks/jobs", json={"kind": "stock_search", "params": {"bogus": 1}}).status_code == 400
    assert client.post("/api/stocks/jobs", json={"kind": "stock_trade"}).status_code == 400
    m = insert_stock_model(conn)
    v = client.post("/api/stocks/jobs", json={"kind": "stock_validate", "params": {"model_id": m["id"]}}).json()["params"]
    assert v["years"] == [2024, year] and v["model"] == {"family": "momentum", "params": m["params"]}
    late = insert_stock_model(conn, backtest={**GOOD_BT, "last_day": "2024-06-28"})
    assert client.post("/api/stocks/jobs", json={"kind": "stock_validate", "params": {"model_id": late["id"]}}).status_code == 400
    b = client.post("/api/stocks/jobs", json={"kind": "stock_backtest", "params": {"model_id": m["id"]}}).json()["params"]
    assert b["model_id"] == m["id"] and b["years"] == [2017, 2023]
    bad = {"kind": "stock_backtest", "params": {"model_id": m["id"], "years": [2020, 2025]}}
    assert client.post("/api/stocks/jobs", json=bad).status_code == 400
    with pytest.raises(BadRequest):
        queue.create_job(conn, "stock_trade", {"assignment_id": 1})


# ------------------------------------------------------------------ results and eligibility


def _complete(conn, kind: str, params: dict, result: dict):
    w = insert_worker(conn, f"w-{kind}", role="backtest")
    job = insert_job(conn, kind, params=Jsonb(params))
    token = lease(conn, job["id"], w)
    with conn.transaction():
        return queue.complete(conn, job["id"], str(token), result, w.id)


def test_search_result_creates_models_once(conn):
    entry = {"family": "momentum", "params": {"lookback": 60, "top_k": 3}, "params_hash": "ignored", "summary": "Buys winners.",
             "backtest_metrics": GOOD_BT}
    row = _complete(conn, "stock_search", {"years": [2017, 2023]}, {"create_stock_models": [entry, {"family": "nope"}]})
    ids = row["result"]["created_stock_models"]
    assert len(ids) == 1 and row["result"]["create_stock_models"][0]["summary"] == "Buys winners."
    model = conn.execute("SELECT * FROM stock_models WHERE id = %s", (ids[0],)).fetchone()
    assert model["lineage_id"] == model["id"] and model["status"] == "candidate", "not validated yet"
    assert model["created_by_job_id"] == row["id"] and len(model["params_hash"]) == 16
    again = _complete(conn, "stock_search", {}, {"create_stock_models": [entry]})
    assert again["result"]["created_stock_models"] == ids
    assert conn.execute("SELECT count(*) AS n FROM stock_models").fetchone()["n"] == 1
    skipped = conn.execute("SELECT detail FROM job_events WHERE job_id = %s AND event = 'stock_result_skipped'",
                           (row["id"],)).fetchone()
    assert "unknown stock model family" in skipped["detail"]["problems"][0]


def test_validate_result_promotes_to_paper_ok(conn):
    m = insert_stock_model(conn, status="candidate", validation=None)
    row = _complete(conn, "stock_validate", {"model_id": m["id"]}, {"model_id": m["id"], "validation_metrics": GOOD_VAL})
    assert row["result"]["validated_stock_model"] == m["id"]
    after = conn.execute("SELECT * FROM stock_models WHERE id = %s", (m["id"],)).fetchone()
    assert after["validation_metrics"] == GOOD_VAL and after["status"] == "paper_ok"
    weak = insert_stock_model(conn, status="candidate", validation=None)
    _complete(conn, "stock_validate", {"model_id": weak["id"]}, {"model_id": weak["id"], "validation_metrics": {"sharpe": -0.2}})
    assert conn.execute("SELECT status FROM stock_models WHERE id = %s", (weak["id"],)).fetchone()["status"] == "candidate"


def test_paper_marks_promote_to_live_eligible_and_drawdown_demotes(conn):
    s = stock_setup(conn, symbols=("SPY",))
    base = s.session - timedelta(days=60)
    for k in range(20):
        conn.execute("INSERT INTO stock_marks VALUES (%s, %s, %s, 0)", (s.assignment["id"], base + timedelta(days=k), 1_000_000 + k * 100))
    stats = eligibility.paper_stats(conn, s.model["id"])
    assert stats["days"] == 20 and stats["max_drawdown"] == 0.0 and abs(stats["return"] - 0.0019) < 1e-9
    with conn.transaction():
        assert eligibility.recompute(conn, s.model["id"]) == "live_eligible"
    conn.execute("INSERT INTO stock_marks VALUES (%s, %s, 800000, 0)", (s.assignment["id"], base + timedelta(days=30)))
    with conn.transaction():
        assert eligibility.recompute(conn, s.model["id"]) == "paper_ok", "a 20% paper drawdown fails the paper gate"


def test_startup_recomputes_stock_models(conn):
    from host import startup

    m = insert_stock_model(conn, status="live_eligible")
    with conn.transaction():
        out = startup.recompute_statuses(conn)
    assert out["stock_models"] == 1
    assert conn.execute("SELECT status FROM stock_models WHERE id = %s", (m["id"],)).fetchone()["status"] == "paper_ok"


# ------------------------------------------------------------------ CLI


def test_cli_commands(conn, config, capsys, monkeypatch):
    from host import cli

    stock_setup(conn, symbols=("SPY",))
    monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: config))
    assert cli.main(["stock-models"]) == 0 and "momentum" in capsys.readouterr().out
    assert cli.main(["stock-assignments"]) == 0 and "SPY" in capsys.readouterr().out
    assert cli.main(["stock-orders", "--status", "active"]) == 0
    capsys.readouterr()
    m = insert_stock_model(conn)
    assert cli.main(["stock-assign", str(m["id"]), "--bankroll", "100", "--symbols", "spy"]) == 0
    assert "bankroll=$100.00" in capsys.readouterr().out
    assert cli.main(["stock-assign", "999999", "--symbols", "SPY"]) == 1
