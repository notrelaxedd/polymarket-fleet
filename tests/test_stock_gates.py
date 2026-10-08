"""Host stock gates fixed after review: the shared-account bankroll gate on create and
resume, a stock_validate result that names another model, and a partial bar from an
intraday fetch taken for the previous session's close."""
from __future__ import annotations

from datetime import datetime, time as dtime

from psycopg.types.json import Jsonb

from host import queue
from host.stocks import market
from host.stocks.market import previous_weekday
from tests.conftest import insert_job, insert_worker
from tests.stock_host_helpers import (
    GOOD_VAL, add_bars, assignment_row, insert_stock_model, lease, set_broker, stock_setup,
)


def _complete(conn, kind: str, params: dict, result: dict):
    w = insert_worker(conn, f"w-{kind}", role="backtest")
    job = insert_job(conn, kind, params=Jsonb(params))
    token = lease(conn, job["id"], w)
    with conn.transaction():
        return queue.complete(conn, job["id"], str(token), result, w.id)


def test_bankroll_gate_counts_halted_assignments_and_resume_rechecks_it(client, conn):
    """One Alpaca account is shared: a halted assignment keeps its cash, and resume
    refuses when a newer assignment took the cash it needs (else Alpaca fills on margin)."""
    broker = set_broker(conn, cash_cents=1_000_000)
    add_bars(conn, "SPY", [previous_weekday(broker["session_date"])])
    body = {"model_id": insert_stock_model(conn)["id"], "mode": "paper", "bankroll_cents": 1_000_000, "symbols": ["SPY"]}
    a = client.post("/api/stocks/assignments", json=body).json()
    assert client.post(f"/api/stocks/assignments/{a['id']}/halt", json={"reason": "look"}).json()["status"] == "halted"
    refused = client.post("/api/stocks/assignments", json={**body, "model_id": insert_stock_model(conn)["id"]})
    assert refused.status_code == 409 and "cash" in refused.json()["detail"], "the halted assignment still claims it"
    set_broker(conn, cash_cents=1_500_000)
    b = client.post("/api/stocks/assignments", json={**body, "model_id": insert_stock_model(conn)["id"],
                                                     "bankroll_cents": 500_000})
    assert b.status_code == 201
    conn.execute("UPDATE stock_assignments SET cash_cents = 900_000 WHERE id = %s", (b.json()["id"],))  # b grew
    r = client.post(f"/api/stocks/assignments/{a['id']}/resume")
    assert r.status_code == 409 and "600000 cents" in r.json()["detail"], "1.5M cash minus b's 900k claim"
    assert assignment_row(conn, a["id"])["status"] == "halted"
    set_broker(conn, cash_cents=1_900_000)
    assert client.post(f"/api/stocks/assignments/{a['id']}/resume").json()["status"] == "active"


def test_validate_result_cannot_retarget_another_model(conn):
    job_model = insert_stock_model(conn, status="candidate", validation=None)
    other = insert_stock_model(conn, status="candidate", validation=None)
    row = _complete(conn, "stock_validate", {"model_id": job_model["id"]},
                    {"model_id": other["id"], "validation_metrics": GOOD_VAL})
    assert row["result"]["validated_stock_model"] is None
    for m in (job_model, other):
        after = conn.execute("SELECT * FROM stock_models WHERE id = %s", (m["id"],)).fetchone()
        assert after["validation_metrics"] is None and after["status"] == "candidate"
    skipped = conn.execute("SELECT detail FROM job_events WHERE job_id = %s AND event = 'stock_result_skipped'",
                           (row["id"],)).fetchone()
    assert "the result names model" in skipped["detail"]["problems"][0]


def test_a_partial_bar_from_an_intraday_fetch_is_not_the_close(conn):
    s = stock_setup(conn, symbols=("SPY",))
    expected = previous_weekday(s.session)
    prices = market.ref_prices(conn, ["SPY"], s.session)
    assert market.bars_reach_previous_session(conn, ["SPY"], prices, s.session) == (True, expected)
    midday = datetime.combine(expected, dtime(12, 0), tzinfo=market.NEW_YORK)
    conn.execute("UPDATE instruments SET fetched_at = %s WHERE symbol = 'SPY'", (midday,))
    assert market.bars_reach_previous_session(conn, ["SPY"], prices, s.session) == (False, expected)
