"""Step 5 rows for the screenshot database: the live switch on with a fresh auth
probe and balances, one lineage graduated to live_eligible with a live assignment on
an upcoming game, its open live order carrying an exchange id, a resting smoke order,
and the auto-kill the exchange process pulls. Used by tests/hw/screenshots.py after
the paper captures.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from host import kill
from host.events import add_audit
from host.exchange.smoke import expected_phrase as smoke_phrase
from host.trading import orders
from host.trading.limits import approve_order
from host.trading.live import expected_phrase as live_phrase
from tests.conftest import FakeWorker, enable_live, insert_snapshot, lease_trade_job, make_assignment, order_body, worker_row

OWNER = "owner@example.com"
LIVE_GAME = "2026_05_DAL_PHI"
SMOKE_GAME = "2026_05_KC_LV"


def _connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def seed_live(url: str, trader_id: str) -> dict[str, str]:
    """Live on (by the owner, 26 minutes ago), a live assignment with an open live
    order, a smoke order resting below the bid; returns the ids the captures need."""
    with _connect(url) as conn:
        # The lineage holding the capped paper assignment on KC @ LV graduates to live_eligible.
        model = conn.execute(
            "SELECT m.* FROM models m JOIN assignments a ON a.model_id = m.id"
            " WHERE a.game_id = %s AND a.max_bet_cents IS NOT NULL LIMIT 1", (SMOKE_GAME,),
        ).fetchone()
        conn.execute(
            "UPDATE models SET status = 'live_eligible', updated_at = now() WHERE lineage_id = %s AND status <> 'retired'",
            (model["lineage_id"],),
        )
        enable_live(conn, buying_power_cents=250_000)
        conn.execute(
            """
            UPDATE exchange_state SET balance_cents = 312_550, clock_skew_ms = 140, market_source = 'polymarket_us',
                   live_enabled_by = %s, live_enabled_at = now() - interval '26 minutes', open_orders_checked_at = now()
            """,
            (OWNER,),
        )
        phrase = live_phrase(conn)  # the date in the Settings time zone, what the form really asks for
        add_audit(conn, "live_on", "live_enabled", OWNER, {"live_enabled": False},
                  {"live_enabled": True, "balance_cents": 312_550, "clock_skew_ms": 140}, confirmation_text=phrase)
        conn.execute("UPDATE audit_log SET ts = now() - interval '26 minutes' WHERE action = 'live_on'")
        # The live assignment through the real create path (the three live gates are on).
        live = make_assignment(conn, LIVE_GAME, model["id"], "live", 25_000, actor=OWNER)
        job = lease_trade_job(conn, FakeWorker(trader_id, ""), live)
        market = conn.execute(
            "SELECT * FROM markets WHERE game_id = %s AND mapping_confirmed ORDER BY created_at LIMIT 1", (LIVE_GAME,)
        ).fetchone()
        snapshot = insert_snapshot(conn, market["id"], bid=0.62, ask=0.64, liquidity_usd_cents=190_000)
        body = order_body(live, job, market, snapshot, price=0.64, size=15, my_p=0.69, market_p=0.63, edge=0.048,
                          rationale="my 0.69 vs ask 0.64, fee 0.012, edge 0.048")
        decision = approve_order(conn, worker_row(conn, trader_id), body)
        assert decision["status"] == "approved", decision
        orders.set_status(conn, decision["order_id"], "submitting", "exchange", expected=("approved",),
                          submitted_at=datetime.now(timezone.utc))
        orders.set_status(conn, decision["order_id"], "open", "exchange", {"exchange_order_id": "pm-7f3a21c9"},
                          expected=("submitting",), exchange_order_id="pm-7f3a21c9")
        conn.execute("UPDATE orders SET created_at = now() - interval '50 seconds' WHERE id = %s", (decision["order_id"],))
        # The smoke order: one contract five cents under the bid of the most liquid market.
        home = conn.execute(
            "SELECT * FROM markets WHERE game_id = %s AND side = 'home' AND mapping_confirmed ORDER BY created_at LIMIT 1", (SMOKE_GAME,)
        ).fetchone()
        smoke = conn.execute(
            """
            INSERT INTO orders (client_request_id, kind, market_id, mode, price, size, cost_cents, fee_cents_est, snapshot_id,
                                status, exchange_order_id, submitted_at, created_at, rationale)
            VALUES (%s, 'smoke', %s, 'live', 0.51, 1, 52, 1, (SELECT id FROM price_snapshots WHERE market_id = %s ORDER BY ts DESC LIMIT 1),
                    'open', 'pm-smoke-4e1b', now() - interval '6 seconds', now() - interval '7 seconds', 'smoke order')
            RETURNING *
            """,
            ("smoke-" + uuid.uuid4().hex[:24], home["id"], home["id"]),
        ).fetchone()
        orders.add_order_event(conn, smoke["id"], None, "approved", "owner", {"cost_cents": 52, "smoke": True})
        orders.add_order_event(conn, smoke["id"], "approved", "submitting", "exchange", None)
        orders.add_order_event(conn, smoke["id"], "submitting", "open", "exchange", {"exchange_order_id": "pm-smoke-4e1b"})
        add_audit(conn, "smoke_order", str(smoke["id"]), OWNER, None, {"market_id": str(home["id"]), "price": 0.51, "size": 1},
                  confirmation_text=smoke_phrase(conn))
        return {"live_assignment": str(live["id"]), "live_order": str(decision["order_id"]), "smoke_order": str(smoke["id"])}


def auto_kill(url: str, reason: str = "clock_skew", detail: dict[str, Any] | None = None) -> None:
    """The exchange process pulls the switch (host.kill.auto_kill): live off, orders
    cancelled or cancel requested, assignments halted, the reason in the audit log.
    A clock_skew kill also records the measured skew, as the loop does, so the
    pages show one figure."""
    detail = detail or {"skew_ms": 48_213, "limit_ms": 30_000}
    with _connect(url) as conn:
        kill.auto_kill(conn, reason, detail)
        if reason == "clock_skew" and detail.get("skew_ms") is not None:
            conn.execute("UPDATE exchange_state SET clock_skew_ms = %s, updated_at = now()", (int(detail["skew_ms"]),))


def touch_live(conn: psycopg.Connection) -> None:
    """Keep the auth probe and the balance fresh while the live captures run (only
    once seed_live has given the exchange its credentials)."""
    conn.execute(
        "UPDATE exchange_state SET auth_checked_at = now() - interval '2 minutes', balance_checked_at = now() - interval '2 minutes'"
        " WHERE credentials_present"
    )
