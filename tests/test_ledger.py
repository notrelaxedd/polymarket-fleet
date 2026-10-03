"""Ledger conventions, overdraw protection, append-only trigger and replay checks."""
from __future__ import annotations

import uuid

import psycopg
import pytest

from host.errors import Conflict
from host.trading import ledger, orders


def _assignment(conn: psycopg.Connection) -> str:
    conn.execute(
        "INSERT INTO games (game_id, season, game_type, week, gameday, home_team, away_team, raw) "
        "VALUES ('2026_05_KC_LV', 2026, 'REG', 5, '2026-10-05', 'LV', 'KC', '{}') ON CONFLICT DO NOTHING"
    )
    model = conn.execute(
        "INSERT INTO models (lineage_id, family, params, params_hash) VALUES (%s, 'elo_blend', '{}', %s) RETURNING id, lineage_id",
        (uuid.uuid4(), uuid.uuid4().hex[:16]),
    ).fetchone()
    conn.execute("UPDATE models SET lineage_id = id WHERE id = %s", (model["id"],))
    row = conn.execute(
        "INSERT INTO assignments (game_id, model_id, lineage_id, mode) VALUES ('2026_05_KC_LV', %s, %s, 'paper') RETURNING id",
        (model["id"], model["id"]),
    ).fetchone()
    return str(row["id"])


def _market(conn: psycopg.Connection) -> str:
    row = conn.execute(
        "INSERT INTO markets (platform, market_ref, title, game_id, side, mapping_confirmed) "
        "VALUES ('sim', %s, 'KC wins', '2026_05_KC_LV', 'away', true) RETURNING id",
        (uuid.uuid4().hex,),
    ).fetchone()
    return str(row["id"])


def _order(conn: psycopg.Connection, assignment_id: str, market_id: str, price: float, size: int, cost: int, mode: str = "paper") -> dict:
    return dict(conn.execute(
        "INSERT INTO orders (client_request_id, assignment_id, market_id, mode, price, size, cost_cents, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'approved') RETURNING *",
        (uuid.uuid4().hex, assignment_id, market_id, mode, price, size, cost),
    ).fetchone())


def test_fund_reserve_fill_settle_follow_the_conventions(conn: psycopg.Connection) -> None:
    aid = _assignment(conn)
    bank = ledger.create_bankroll(conn, aid, "paper", 10_000)
    assert bank["available_cents"] == 10_000 and bank["initial_cents"] == 10_000
    oid = uuid.uuid4()
    ledger.reserve(conn, bank["id"], 2_600, oid)
    b = ledger.get_bankroll(conn, bank["id"])
    assert (b["available_cents"], b["reserved_cents"]) == (7_400, 2_600)
    ledger.fill(conn, bank["id"], 2_500, 100, oid)
    b = ledger.get_bankroll(conn, bank["id"])
    assert (b["available_cents"], b["reserved_cents"], b["open_cost_cents"], b["realized_pnl_cents"]) == (7_400, 0, 2_500, -100)
    ledger.settle(conn, bank["id"], 2_500, 5_000, aid)
    b = ledger.get_bankroll(conn, bank["id"])
    assert (b["available_cents"], b["open_cost_cents"], b["realized_pnl_cents"]) == (12_400, 0, 2_400)
    assert ledger.replay_problems(conn, bank["id"]) == []
    assert b["initial_cents"] + b["realized_pnl_cents"] == b["available_cents"] + b["reserved_cents"] + b["open_cost_cents"]


def test_overdraw_is_refused_and_nothing_is_written(conn: psycopg.Connection) -> None:
    aid = _assignment(conn)
    bank = ledger.create_bankroll(conn, aid, "paper", 1_000)
    with pytest.raises(Conflict):
        ledger.reserve(conn, bank["id"], 1_001, uuid.uuid4())
    assert ledger.replay(conn, bank["id"])["reserved_cents"] == 0
    assert ledger.get_bankroll(conn, bank["id"])["available_cents"] == 1_000


def test_ledger_rows_are_append_only(conn: psycopg.Connection) -> None:
    aid = _assignment(conn)
    bank = ledger.create_bankroll(conn, aid, "paper", 500)
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE ledger SET d_available = 0 WHERE bankroll_id = %s", (bank["id"],))
    conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM ledger WHERE bankroll_id = %s", (bank["id"],))
    conn.rollback()


def test_replay_detects_a_tampered_cache(conn: psycopg.Connection) -> None:
    aid = _assignment(conn)
    bank = ledger.create_bankroll(conn, aid, "paper", 5_000)
    conn.execute("UPDATE bankrolls SET available_cents = 4_000 WHERE id = %s", (bank["id"],))
    problems = ledger.replay_problems(conn, bank["id"])
    assert len(problems) == 2 and "cached 4000" in problems[0]


def test_cancel_releases_unfilled_part_and_logs_events(conn: psycopg.Connection) -> None:
    aid = _assignment(conn)
    bank = ledger.create_bankroll(conn, aid, "paper", 10_000)
    mid = _market(conn)
    order = _order(conn, aid, mid, 0.52, 10, 560)
    ledger.reserve(conn, bank["id"], 560, order["id"])
    orders.set_status(conn, order["id"], "open", "executor", expected=("approved",))
    orders.record_fill(conn, order["id"], 0.52, 4, 10, "paper", "paper-sim")
    b = ledger.get_bankroll(conn, bank["id"])
    assert b["open_cost_cents"] == 208 and b["reserved_cents"] == 560 - 218
    status = orders.cancel_order(conn, order["id"], "owner", "test")
    assert status == "cancelled"
    b = ledger.get_bankroll(conn, bank["id"])
    assert b["reserved_cents"] == 0 and b["available_cents"] == 10_000 - 218
    events = [r["to_status"] for r in conn.execute("SELECT to_status FROM order_events WHERE order_id = %s ORDER BY id", (order["id"],)).fetchall()]
    assert events == ["open", "partial", "cancelled"]
    assert ledger.replay_problems(conn, bank["id"]) == []


def test_full_fill_releases_fee_surplus_and_live_cancel_is_requested(conn: psycopg.Connection) -> None:
    aid = _assignment(conn)
    bank = ledger.create_bankroll(conn, aid, "paper", 10_000)
    mid = _market(conn)
    order = _order(conn, aid, mid, 0.50, 10, 530)  # 500 cost + 30 fee estimate
    ledger.reserve(conn, bank["id"], 530, order["id"])
    orders.set_status(conn, order["id"], "open", "executor", expected=("approved",))
    row = orders.record_fill(conn, order["id"], 0.50, 10, 20, "paper", "paper-sim")
    assert row["status"] == "filled" and float(row["avg_fill_price"]) == 0.5
    b = ledger.get_bankroll(conn, bank["id"])
    assert b["reserved_cents"] == 0 and b["open_cost_cents"] == 500 and b["available_cents"] == 10_000 - 520
    live = _order(conn, aid, mid, 0.50, 1, 53, mode="live")
    orders.set_status(conn, live["id"], "open", "executor", expected=("approved",))
    assert orders.cancel_order(conn, live["id"], "owner", "test") == "cancel_requested"
    with pytest.raises(Conflict):
        orders.record_fill(conn, live["id"], 0.5, 2, 0, "live", "x")
