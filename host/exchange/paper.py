"""Paper fill simulation (docs/TRADING.md, "Executor and paper fills").

For every open or partial paper order and every snapshot of its market newer than the
order's submission (and newer than the last snapshot that filled it): a buy walks the
ask levels with `price <= order.price`, a sell (step 6 Part B) the bid levels with
`price >= order.price`, filling `min(remaining, participation * level_size)` per level
(fleet.sim.book.walk), fee per contract from `fee_model`, one fills row per level
through `orders.record_fill` (idempotent on the paper fill id). A buy fill never
consumes more than what is left of the order's reservation; a sell fill never sells
more than the assignment holds.

Three rules keep the simulation honest:
- A resting order (the first snapshot after submission did not cross its limit: a
  buy's ask above it, a sell's bid below it) fills at the order's own limit price when
  a later book crosses it, as a resting limit order does on a real book; a marketable
  order takes the levels as they are.
- One snapshot's level offers `participation * size` contracts to all paper orders on
  the market together, in submission order, not to each order separately; ask levels
  (buys) and bid levels (sells) are shared separately. The paper fill id is
  `paper:{order}:{snapshot}:{level}` for an ask level and `...:b{level}` for a bid level.
- With `trade_pregame_only`, snapshots taken at or after the game's kickoff never fill
  a pre-game order (the executor cancels what is still open at kickoff). An in-game
  order (`orders.ingame`) fills on the snapshots after kickoff while it is open.

Nothing fills while the kill switch is on.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import psycopg

from fleet.sim import book
from host.settings import get_setting
from host.trading import orders, positions
from host.trading.orders import cents, fill_cost_cents

log = logging.getLogger(__name__)

ACTOR = "paper"
DEFAULT_FEE_MODEL = {"taker_rate": 0.05, "half_spread": 0.01}
EPS = 1e-9


def fee_per_contract(price: float, fee_model: dict[str, Any] | None) -> float:
    """Dollars of fee for one contract at `price`: taker_rate * price * (1 - price)."""
    rate = float((fee_model or DEFAULT_FEE_MODEL).get("taker_rate", DEFAULT_FEE_MODEL["taker_rate"]))
    return rate * price * (1.0 - price)


def fee_cents(price: float, size: int, fee_model: dict[str, Any] | None) -> int:
    return cents(fee_per_contract(price, fee_model) * size)


def fee_increment_cents(price: float, size: int, fee_model: dict[str, Any] | None, before_dollars: float) -> int:
    """The fee of one more fill, rounded on the order's cumulative fee so the per-fill
    roundings never add up to more than the fee of the whole size."""
    return cents(before_dollars + fee_per_contract(price, fee_model) * size) - cents(before_dollars)


def cost_cents(price: float, size: int) -> int:
    return fill_cost_cents(price, size)


def simulate(
    order: dict[str, Any],
    snapshot: dict[str, Any],
    participation: float,
    fee_model: dict[str, Any] | None,
    remaining: int | None = None,
    resting: bool = False,
    taken: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """The fills one snapshot gives an order: [{level, price, size, fee_cents}]. A buy
    walks `ask_depth`, a sell (`order["side"] == "sell"`) walks `bid_depth`. `resting`
    fills at the order's limit; `taken` is what other orders on the same side already
    took from each level of this snapshot. The walk itself is fleet.sim.book.walk,
    shared with the snapshot replay backtests."""
    limit = float(order["price"])
    side = side_of(order)
    left = int(order["size"]) - int(order["filled_size"]) if remaining is None else int(remaining)
    levels = snapshot.get("bid_depth" if side == "sell" else "ask_depth")
    fills = book.walk(levels, limit, left, participation, taken, side, resting)
    return [{**f, "fee_cents": fee_cents(f["price"], f["size"], fee_model)} for f in fills]


def side_of(order: dict[str, Any]) -> str:
    """'sell' for a sell order, 'buy' otherwise (every order before step 6 Part B)."""
    return "sell" if order.get("side") == "sell" else "buy"


def fill_id(order_id: Any, snapshot_id: Any, level: int, side: str) -> str:
    """The paper fill id: ask levels `paper:{order}:{snapshot}:{level}`, bid levels
    `paper:{order}:{snapshot}:b{level}`."""
    return f"paper:{order_id}:{snapshot_id}:{'b' if side == 'sell' else ''}{level}"


def kickoff_bound(conn: psycopg.Connection, order: dict[str, Any]) -> datetime | None:
    """The game's kickoff when trade_pregame_only is on (snapshots from then on are
    not used), else None; always None for an in-game order."""
    if order.get("ingame") or order.get("assignment_id") is None:
        return None
    if get_setting(conn, "trade_pregame_only", True) is False:
        return None
    row = conn.execute(
        "SELECT g.kickoff_at FROM assignments a JOIN games g ON g.game_id = a.game_id WHERE a.id = %s",
        (order["assignment_id"],),
    ).fetchone()
    return None if row is None else row["kickoff_at"]


def pending_snapshots(conn: psycopg.Connection, order: dict[str, Any]) -> list[dict[str, Any]]:
    """Snapshots of the order's market after its submission, after the last one that
    filled it and (pregame only) before kickoff, oldest first."""
    last = conn.execute(
        "SELECT COALESCE(MAX(snapshot_id), 0) AS last FROM fills WHERE order_id = %s", (order["id"],)
    ).fetchone()["last"]
    bound = kickoff_bound(conn, order)
    rows = conn.execute(
        """
        SELECT * FROM price_snapshots
         WHERE market_id = %s AND ts > %s AND id > %s AND (%s::timestamptz IS NULL OR ts < %s)
         ORDER BY ts, id
        """,
        (order["market_id"], order["submitted_at"], int(last or 0), bound, bound),
    ).fetchall()
    return [dict(r) for r in rows]


def is_resting(conn: psycopg.Connection, order: dict[str, Any]) -> bool:
    """True when the first snapshot after submission could not fill the order: a buy's
    ask above the limit, a sell's bid below it (or the touch missing). Such an order
    later fills at its limit."""
    row = conn.execute(
        "SELECT bid, ask FROM price_snapshots WHERE market_id = %s AND ts > %s ORDER BY ts, id LIMIT 1",
        (order["market_id"], order["submitted_at"]),
    ).fetchone()
    if row is None:
        return False
    limit = float(order["price"])
    if side_of(order) == "sell":
        return row["bid"] is None or float(row["bid"]) < limit - EPS
    return row["ask"] is None or float(row["ask"]) > limit + EPS


def taken_from(conn: psycopg.Connection, snapshot_id: int, side: str = "buy") -> dict[int, int]:
    """Contracts other paper orders already took from each level of one side of a
    snapshot: the last field of the paper fill id is the ask level index, or `b` plus
    the bid level index."""
    rows = conn.execute(
        "SELECT exchange_fill_id, size FROM fills WHERE snapshot_id = %s AND mode = 'paper'", (snapshot_id,)
    ).fetchall()
    out: dict[int, int] = {}
    for row in rows:
        last = str(row["exchange_fill_id"]).rsplit(":", 1)[-1]
        is_bid = last.startswith("b")
        if is_bid != (side == "sell"):
            continue
        try:
            level = int(last[1:] if is_bid else last)
        except ValueError:
            continue
        out[level] = out.get(level, 0) + int(row["size"])
    return out


def _fee_so_far(conn: psycopg.Connection, order_id: Any, fee_model: dict[str, Any] | None) -> float:
    rows = conn.execute("SELECT price, size FROM fills WHERE order_id = %s", (order_id,)).fetchall()
    return sum(fee_per_contract(float(r["price"]), fee_model) * int(r["size"]) for r in rows)


def fill_order(conn: psycopg.Connection, order: dict[str, Any], participation: float, fee_model: dict[str, Any] | None) -> int:
    """Apply every pending snapshot to one order; the number of fills recorded. A buy
    is capped by its reservation, a sell by the contracts its assignment holds."""
    recorded = 0
    side = side_of(order)
    resting = is_resting(conn, order)
    for snapshot in pending_snapshots(conn, order):
        current = orders.get_order(conn, order["id"])
        if current["status"] not in ("open", "partial"):
            break
        cap = _sell_cap(conn, current) if side == "sell" else orders.remaining_reservation_cents(conn, current)
        remaining = int(current["size"]) - int(current["filled_size"])
        fee_before = _fee_so_far(conn, order["id"], fee_model)
        taken = taken_from(conn, int(snapshot["id"]), side)
        for fill in simulate(current, snapshot, participation, fee_model, remaining, resting, taken):
            if side == "sell":
                size = min(fill["size"], cap)
            else:
                size = _fit_reservation(fill["price"], fill["size"], cap, fee_model, fee_before)
            if size <= 0:
                continue
            fee = fee_increment_cents(fill["price"], size, fee_model, fee_before)
            orders.record_fill(
                conn, order["id"], fill["price"], size, fee, "paper", ACTOR,
                snapshot_id=int(snapshot["id"]), exchange_fill_id=fill_id(order["id"], snapshot["id"], fill["level"], side),
            )
            cap -= size if side == "sell" else cost_cents(fill["price"], size) + fee
            fee_before += fee_per_contract(fill["price"], fee_model) * size
            recorded += 1
    return recorded


def _sell_cap(conn: psycopg.Connection, order: dict[str, Any]) -> int:
    """Contracts a sell may still fill: what its assignment holds on the market (no
    shorting, whatever the approval saw)."""
    if order.get("assignment_id") is None:
        return 0
    return max(0, positions.held(conn, order["assignment_id"], order["market_id"])[0])


def _fit_reservation(price: float, size: int, reservation: int, fee_model: dict[str, Any] | None, fee_before: float = 0.0) -> int:
    """The largest size up to `size` whose cost plus fee fits the reservation left."""
    while size > 0 and cost_cents(price, size) + fee_increment_cents(price, size, fee_model, fee_before) > reservation:
        size -= 1
    return size


def killed(conn: psycopg.Connection) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key = 'kill_switch'").fetchone()
    return row is not None and row["value"] is True


def process(conn: psycopg.Connection, now: datetime | None = None) -> int:
    """Simulate fills for every open or partial paper order; fills recorded. Nothing
    while the kill switch is on (the kill cancels paper orders in its own
    transaction; this keeps a flag raised any other way from filling anything)."""
    if killed(conn):
        return 0
    participation = float(get_setting(conn, "participation", 0.5) or 0.0)
    fee_model = get_setting(conn, "fee_model", DEFAULT_FEE_MODEL)
    rows = conn.execute(
        "SELECT * FROM orders WHERE mode = 'paper' AND status IN ('open', 'partial') AND submitted_at IS NOT NULL ORDER BY submitted_at, id"
    ).fetchall()
    total = 0
    for row in rows:
        try:
            total += fill_order(conn, dict(row), participation, fee_model if isinstance(fee_model, dict) else None)
        except Exception:  # noqa: BLE001 - one order's trouble must not stop the others
            log.exception("paper fills for order %s failed", row["id"])
            if not conn.autocommit:
                conn.rollback()
    return total
