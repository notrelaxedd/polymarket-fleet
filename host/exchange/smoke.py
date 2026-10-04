"""The smoke order (docs/LIVE.md "Smoke order"): one live contract resting 5 cents
below the bid, cancelled after `smoke_hold_seconds`, no assignment, no bankroll. It
proves keys, signing, placement and cancellation without giving any model money.

`run_smoke` needs the typed phrase "SMOKE YYYY-MM-DD" (today, owner tz), live on,
auth_ok and the kill off. The row goes through the normal outbox: with a `gateway`
(tests, or the CLI running with the exchange service stopped) this function drives an
Executor itself; otherwise it waits for the running exchange process to submit and
cancel the row. The result is the timeline of the order's events.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

import psycopg
from psycopg_pool import ConnectionPool

from host import kill
from host.errors import BadRequest, Conflict, NotFound
from host.events import add_audit
from host.exchange.adapters.base import OrderGateway, PaperGateway, utcnow
from host.exchange.executor import Executor
from host.settings import get_int_setting, get_setting
from host.trading import limits, orders
from host.trading.positions import owner_tz, server_now

PHRASE_PREFIX = "SMOKE "
BELOW_BID = 0.05
WAIT_STEP_S = 0.5
WAIT_MAX_S = 30.0
DRIVEN_TICKS = 10


def expected_phrase(conn: psycopg.Connection, now: datetime | None = None) -> str:
    moment = (now or server_now(conn)).astimezone(owner_tz(conn))
    return PHRASE_PREFIX + moment.date().isoformat()


def check_preconditions(conn: psycopg.Connection, confirm: str, now: datetime | None = None) -> None:
    """400 on a wrong phrase; 409 when live is off, auth is not ok or the fleet is killed."""
    phrase = expected_phrase(conn, now)
    if not isinstance(confirm, str) or confirm != phrase:
        raise BadRequest(f'confirmation must be exactly "{phrase}"')
    if kill.is_killed(conn):
        raise Conflict("the kill switch is on")
    if get_setting(conn, "live_enabled", False) is not True:
        raise Conflict("live trading is off (enable it first)")
    state = conn.execute("SELECT auth_ok FROM exchange_state WHERE id").fetchone()
    if state is None or not state["auth_ok"]:
        raise Conflict("the exchange has not confirmed its credentials (auth_ok is false)")


def pick_market(conn: psycopg.Connection, market_id: Any = None) -> dict[str, Any]:
    """The given market, or the most liquid confirmed, open market of a scheduled game
    on the live platform (any platform but sim)."""
    if market_id is not None:
        row = conn.execute("SELECT * FROM markets WHERE id = %s::uuid", (str(market_id),)).fetchone()
        if row is None:
            raise NotFound("market not found")
        if not row["mapping_confirmed"] or row["status"] != "open":
            raise Conflict("the market is not confirmed and open")
        return dict(row)
    row = conn.execute(
        """
        SELECT m.* FROM markets m JOIN games g ON g.game_id = m.game_id
         WHERE m.mapping_confirmed AND m.status = 'open' AND m.platform <> 'sim' AND m.best_bid IS NOT NULL
           AND g.status <> 'final' AND (g.kickoff_at IS NULL OR g.kickoff_at > now())
         ORDER BY m.liquidity_usd_cents DESC NULLS LAST, m.last_snapshot_at DESC NULLS LAST LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise Conflict("no confirmed, unresolved live-tradable market with a price")
    return dict(row)


def smoke_price(market: dict[str, Any]) -> float:
    """best_bid - 0.05 floored at 0.01, on the market's tick: it rests and never fills."""
    bid = market.get("best_bid")
    if bid is None:
        raise Conflict("the market has no bid yet")
    tick = float(market.get("tick") or 0.01)
    price = max(0.01, float(bid) - BELOW_BID)
    return round(round(price / tick) * tick, 4)


def timeline(conn: psycopg.Connection, order_id: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT ts, from_status, to_status, actor, detail FROM order_events WHERE order_id = %s ORDER BY id", (order_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _wait_for(pool: ConnectionPool, order_id: Any, statuses: tuple[str, ...], sleep: Callable[[float], None], tick: Callable[[], None] | None) -> str:
    """Drive the executor (when given) or wait for the exchange process until the
    order reaches one of `statuses` or a terminal status, at most WAIT_MAX_S."""
    waited, ticks = 0.0, 0
    while True:
        if tick is not None:
            tick()
            ticks += 1
        with pool.connection() as conn:
            status = orders.get_order(conn, order_id)["status"]
        if status in statuses or status in orders.TERMINAL_STATUSES:
            return status
        if tick is not None and ticks >= DRIVEN_TICKS:
            return status
        if waited >= WAIT_MAX_S:
            return status
        sleep(WAIT_STEP_S)
        waited += WAIT_STEP_S


def run_smoke(
    pool: ConnectionPool, confirm: str, market_id: Any = None, hold_seconds: int | None = None,
    gateway: OrderGateway | None = None, now: datetime | None = None, sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Create, submit, hold and cancel the smoke order; the timeline (docs/LIVE.md)."""
    with pool.connection() as conn:
        check_preconditions(conn, confirm, now)
        market = pick_market(conn, market_id)
        price, size = smoke_price(market), max(1, int(market.get("min_size") or 1))
        decision = limits.approve_smoke(conn, market, price, size, "cli")
        hold = hold_seconds if hold_seconds is not None else get_int_setting(conn, "smoke_hold_seconds", 10)
        add_audit(conn, "smoke_order", decision["order_id"], "cli", None,
                  {"market_id": str(market["id"]), "price": price, "size": size, "status": decision["status"], "reason": decision["reason"]},
                  confirmation_text=confirm)
    result: dict[str, Any] = {
        "order_id": decision["order_id"], "market_id": str(market["id"]), "market_ref": market["market_ref"],
        "price": price, "size": size, "approved": decision["status"] == "approved", "reason": decision["reason"],
        "hold_seconds": hold, "exchange_order_id": None, "status": decision["status"], "timeline": [],
    }
    if decision["status"] != "approved":
        with pool.connection() as conn:
            result["timeline"] = timeline(conn, decision["order_id"])
        return result
    executor = Executor(PaperGateway(), gateway) if gateway is not None else None

    def tick() -> None:
        with pool.connection() as conn:
            executor.tick(conn, now or utcnow())  # type: ignore[union-attr]

    drive = tick if executor is not None else None
    result["status"] = _wait_for(pool, decision["order_id"], ("open", "partial"), sleep, drive)
    if result["status"] in ("open", "partial"):
        sleep(hold)
        with pool.connection() as conn:
            orders.cancel_order(conn, decision["order_id"], "cli", "smoke hold over")
        result["status"] = _wait_for(pool, decision["order_id"], ("cancelled",), sleep, drive)
    with pool.connection() as conn:
        row = orders.get_order(conn, decision["order_id"])
        result["status"], result["exchange_order_id"] = row["status"], row["exchange_order_id"]
        result["timeline"] = timeline(conn, decision["order_id"])
    return result
