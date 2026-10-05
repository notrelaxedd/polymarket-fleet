"""Sell approval (docs/TRADING.md "Selling (step 6 Part B)").

`approve_sell` decides a worker's request to sell contracts its assignment holds. It
takes the same per-mode approval lock as a buy, locks the bankroll row and the job the
same way (host.trading.limits._load), and is idempotent on `client_request_id` exactly
like a buy. Checks, in order, each with a stable reason code:

1. `duplicate` (the stored decision is returned), `killed`, `lease`, `assignment`,
   `market` (confirmed, mapped to the assignment's game, unresolved), `kickoff` (the
   same pregame cutoff as a buy), `mode` (the live gate), `stale_book`;
2. `participation`: size <= participation * bid depth at or above the price;
3. `price_band`: 0.01 <= price <= 0.99, on the tick, and price >= bid - 0.05;
4. `no_position`: the assignment holds no contracts on the market;
5. `sell_exceeds_position`: size > position size - open sell size (no shorting);
6. `open_sell_exists`: one open sell per market.

A sell reserves nothing (orders.cost_cents = 0, no ledger row at approval) and skips
the liquidity, max-bet, bankroll, daily-loss, exposure and buying-power checks: it
frees money instead of spending it. The order row (side 'sell') and its order_events
row are written like any other.
"""
from __future__ import annotations

import math
from typing import Any, Callable

import psycopg

from host import kill
from host.trading import limits
from host.trading.positions import positions

__all__ = ["approve_sell", "sell_fee_cents", "bid_depth_at_or_above", "held_contracts", "open_sell_totals", "SELL_CHECKS"]

BID_BAND = 0.05
EPS = 1e-9


def sell_fee_cents(price: float, size: int, fee_model: dict[str, Any] | None) -> int:
    """Estimated taker fee of a sale in cents: size * taker_rate * price * (1 - price) * 100, half up."""
    return int(math.floor(size * limits.fee_per_contract(price, fee_model) * 100 + 0.5))


def bid_depth_at_or_above(levels: Any, price: float) -> float:
    """Contracts bid at or above `price` across bid levels ([price, size] or {"price", "size"})."""
    total = 0.0
    for entry in levels if isinstance(levels, list) else []:
        level = limits._level(entry)
        if level is not None and level[0] >= price - EPS and level[1] > 0:
            total += level[1]
    return total


def held_contracts(conn: psycopg.Connection, assignment_id: Any, market_id: Any) -> int:
    """The assignment's signed position size on one market (buys minus sells), from
    host.trading.positions.positions."""
    return sum(int(p.get("size") or 0) for p in positions(conn, assignment_id) if str(p.get("market_id")) == str(market_id))


def open_sell_totals(conn: psycopg.Connection, assignment_id: Any, market_id: Any) -> tuple[int, int]:
    """(count, unfilled contracts) of the assignment's active sell orders on one market."""
    row = conn.execute(
        f"""
        SELECT count(*) AS n, COALESCE(SUM(size - filled_size), 0) AS s FROM orders
         WHERE assignment_id = %s AND market_id = %s AND side = 'sell' AND status IN ('{limits.ACTIVE_LIST}')
        """,
        (assignment_id, market_id),
    ).fetchone()
    return int(row["n"]), int(row["s"])


def _holdings(conn: psycopg.Connection, ctx: dict[str, Any]) -> dict[str, int]:
    """Position and open sells, read once under the approval lock."""
    if "holdings" not in ctx:
        aid, mid = ctx["assignment"]["id"], ctx["market"]["id"]
        count, unfilled = open_sell_totals(conn, aid, mid)
        ctx["holdings"] = {"position": held_contracts(conn, aid, mid), "open_sells": count, "open_sell_size": unfilled}
    return ctx["holdings"]


def _check_participation(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    share = float(ctx["settings"].get("participation", 0.5) or 0.0)
    depth = bid_depth_at_or_above(ctx["cited"].get("bid_depth"), ctx["req"]["price"])
    return ctx["req"]["size"] > share * depth + EPS


def _check_price_band(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    price = ctx["req"]["price"]
    if price < 0.01 - EPS or price > 0.99 + EPS:
        return True
    tick = float(ctx["market"]["tick"] or 0.01)
    if tick > 0 and abs(price / tick - round(price / tick)) > 1e-6:
        return True
    bid = ctx["latest"]["bid"] if ctx["latest"]["bid"] is not None else ctx["cited"]["bid"]
    return bid is not None and price < float(bid) - BID_BAND - EPS


def _check_no_position(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    return _holdings(conn, ctx)["position"] <= 0


def _check_exceeds(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    held = _holdings(conn, ctx)
    return ctx["req"]["size"] > held["position"] - held["open_sell_size"]


def _check_open_sell(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    return _holdings(conn, ctx)["open_sells"] > 0


SELL_CHECKS: tuple[tuple[str, Callable[[psycopg.Connection, dict[str, Any]], bool]], ...] = (
    ("killed", limits._check_killed), ("lease", limits._check_lease), ("assignment", limits._check_assignment),
    ("market", limits._check_market), ("kickoff", limits._check_kickoff), ("mode", limits._check_mode),
    ("stale_book", limits._check_book), ("participation", _check_participation), ("price_band", _check_price_band),
    ("no_position", _check_no_position), ("sell_exceeds_position", _check_exceeds),
    ("open_sell_exists", _check_open_sell),
)


def approve_sell(conn: psycopg.Connection, worker: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Decide one sell request: {"status": "approved"|"rejected", "order_id", "reason"}
    (a repeated client_request_id returns the stored decision with "duplicate": true)."""
    req = limits._parse(dict(body, order_side="sell"))
    stored = limits._stored_decision(conn, req["client_request_id"])
    if stored is not None:
        return stored
    first = limits._one(conn, "SELECT mode FROM assignments WHERE id = %s", req["assignment_id"])
    kill.approval_lock(conn, first["mode"] if first is not None else "paper")
    stored = limits._stored_decision(conn, req["client_request_id"])
    if stored is not None:
        return stored
    ctx = limits._load(conn, worker, req)
    ctx["cost"] = 0
    ctx["fee"] = sell_fee_cents(req["price"], req["size"], ctx["settings"].get("fee_model"))
    reason = next((name for name, check in SELL_CHECKS if check(conn, ctx)), None)
    if ctx["market"] is None:
        # orders.market_id is NOT NULL: a request for an unknown market cannot be stored.
        return {"status": "rejected", "order_id": None, "reason": reason or "market"}
    status = "rejected" if reason is not None else "approved"
    row = limits._insert(conn, ctx, status, reason)
    return {"status": status, "order_id": str(row["id"]), "reason": reason}
