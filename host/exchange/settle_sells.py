"""Bets rows of an assignment at settlement, sells included (docs/TRADING.md "Selling
(step 6 Part B)" and "Settlement, bets, scoring, eligibility").

Per market the assignment traded:
- every filled sell order gets its own row: `order_side` "sell", result "sold",
  entry_price = its average sell price, cost_cents = the basis it removed (the sum of
  its fills' `basis_cents`), pnl = proceeds - fee - basis, stake 0, clv null;
- every filled buy order gets a row that covers only the contracts still held: the
  remaining position (bought minus sold contracts, and its remaining basis) is split
  pro rata over the buy orders, the basis by each order's bought basis and the payout
  by its bought contracts, with the rounding residual on the last row so the rows add
  up to exactly what the ledger `settle` row moves; pnl = payout - basis - buy fee,
  stake = the order's whole bought basis plus fee, CLV against the entry VWAP
  (bought basis / (contracts * 100), as before).

Without sells every buy row is the whole order, exactly as before step 6 Part B.

Step 6 Part C: the row of an in-game order (orders.ingame) carries ingame true, a null
CLV and `state_at_entry` (period, clock, score and possession at approval: the one the
approval event recorded, else the newest game_state at or before the order's
created_at, null without one), and is attributed to the
assignment's in-game model (its model_id and lineage_id) instead of the pre-game one.
The money (basis, payout, pnl) is split exactly as for any other order.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg

from host.trading import orders

FILLS_SQL = "SELECT price, size, fee_cents, basis_cents FROM fills WHERE order_id = %s ORDER BY id"
STATE_KEYS = ("period", "clock_seconds", "home_score", "away_score", "possession")


def split(total: int, weights: list[int]) -> list[int]:
    """`total` cents split by `weights`: each row but the last gets floor(total * w /
    sum), the last gets the residual, so the parts add up to `total` exactly. Equal
    shares when every weight is zero."""
    if not weights:
        return []
    if sum(weights) <= 0:
        weights = [1] * len(weights)
    whole = sum(weights)
    parts = [int(total) * int(w) // whole for w in weights[:-1]]
    return parts + [int(total) - sum(parts)]


def outcome(market: dict[str, Any], winner: str | None) -> str:
    """'win', 'loss' or 'push' for the YES side of `market`."""
    side = market.get("side")
    if winner is None or side is None:
        return "push"
    return "win" if side == winner else "loss"


def _fill_totals(conn: psycopg.Connection, order_id: Any) -> dict[str, Any]:
    """Size, notional (price * size * 100), basis and fee over an order's fills, plus
    the exact VWAP."""
    rows = conn.execute(FILLS_SQL, (order_id,)).fetchall()
    size = sum(int(r["size"]) for r in rows)
    notional = sum(orders.fill_cost_cents(r["price"], int(r["size"])) for r in rows)
    basis = sum(int(r["basis_cents"]) if r["basis_cents"] is not None else orders.fill_cost_cents(r["price"], int(r["size"])) for r in rows)
    fee = sum(int(r["fee_cents"]) for r in rows)
    vwap = None
    if size:
        vwap = float(sum(Decimal(str(r["price"])) * int(r["size"]) for r in rows) / size)
    return {"size": size, "notional": notional, "basis": basis, "fee": fee, "vwap": vwap}


def _common(order: dict[str, Any], market: dict[str, Any], assignment: dict[str, Any], game: dict[str, Any]) -> dict[str, Any]:
    closing = None if market.get("closing_price") is None else float(market["closing_price"])
    return {
        "order_id": order["id"], "assignment_id": assignment["id"], "model_id": assignment["model_id"],
        "lineage_id": assignment["lineage_id"], "game_id": game["game_id"], "worker_id": order.get("worker_id"),
        "mode": assignment["mode"], "date": game["gameday"], "event": f"{game['away_team']} @ {game['home_team']} {game['gameday']}",
        "platform": market["platform"], "contract": market["title"], "side": market.get("side") or "none",
        "my_p": order.get("my_p"), "market_p": order.get("market_p"), "edge": order.get("edge"), "closing_price": closing,
    }


def sell_bet(order: dict[str, Any], totals: dict[str, Any], market: dict[str, Any], assignment: dict[str, Any], game: dict[str, Any]) -> dict[str, Any]:
    """The bets row of one filled sell order."""
    entry = totals["vwap"] if totals["vwap"] is not None else float(order["price"])
    pnl = totals["notional"] - totals["fee"] - totals["basis"]
    return {
        **_common(order, market, assignment, game), "order_side": "sell",
        "entry_price": round(entry, 6), "fee_cents": totals["fee"], "cost_cents": totals["basis"],
        "stake_cents": 0, "clv": None, "result": "sold", "pnl_cents": pnl,
        "payout_cents": 0, "size": totals["size"], "proceeds_cents": totals["notional"],
    }


def market_bets(
    conn: psycopg.Connection, market_orders: list[dict[str, Any]], market: dict[str, Any],
    assignment: dict[str, Any], game: dict[str, Any], winner: str | None,
) -> list[dict[str, Any]]:
    """Bets rows of one market's filled orders (in the given order, buys and sells)."""
    totals = {str(o["id"]): _fill_totals(conn, o["id"]) for o in market_orders}
    buys = [o for o in market_orders if o.get("side") != "sell"]
    sells = [o for o in market_orders if o.get("side") == "sell"]
    bought = sum(totals[str(o["id"])]["size"] for o in buys)
    sold = sum(totals[str(o["id"])]["size"] for o in sells)
    held = max(0, bought - sold)
    basis_left = sum(totals[str(o["id"])]["basis"] for o in buys) - sum(totals[str(o["id"])]["basis"] for o in sells)
    result = outcome(market, winner)
    basis_parts = split(basis_left, [totals[str(o["id"])]["basis"] for o in buys])
    if result == "win":
        payout_parts = split(held * 100, [totals[str(o["id"])]["size"] for o in buys])
    elif result == "push":
        payout_parts = list(basis_parts)
    else:
        payout_parts = [0] * len(buys)
    rows: dict[str, dict[str, Any]] = {}
    for order, basis, payout in zip(buys, basis_parts, payout_parts):
        t = totals[str(order["id"])]
        entry = t["basis"] / (t["size"] * 100.0) if t["size"] else float(order["price"])
        closing = None if market.get("closing_price") is None else float(market["closing_price"])
        rows[str(order["id"])] = {
            **_common(order, market, assignment, game), "order_side": "buy",
            "entry_price": round(entry, 6), "fee_cents": t["fee"], "cost_cents": basis,
            "stake_cents": t["basis"] + t["fee"], "clv": None if closing is None else round(closing - entry, 6),
            "result": result, "pnl_cents": payout - basis - t["fee"], "payout_cents": payout, "size": t["size"],
        }
    for order in sells:
        rows[str(order["id"])] = sell_bet(order, totals[str(order["id"])], market, assignment, game)
    return [rows[str(o["id"])] for o in market_orders]


def assignment_bets(
    conn: psycopg.Connection, assignment: dict[str, Any], game: dict[str, Any], winner: str | None,
    markets: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bets rows of every filled order of an assignment, in order creation order."""
    filled = conn.execute(
        "SELECT * FROM orders WHERE assignment_id = %s AND filled_size > 0 ORDER BY created_at, id", (assignment["id"],)
    ).fetchall()
    by_market: dict[Any, list[dict[str, Any]]] = {}
    for order in filled:
        by_market.setdefault(order["market_id"], []).append(dict(order))
    rows: dict[str, dict[str, Any]] = {}
    for market_id, market_orders in by_market.items():
        market = markets.get(market_id) or dict(conn.execute("SELECT * FROM markets WHERE id = %s", (market_id,)).fetchone())
        for bet in market_bets(conn, market_orders, market, assignment, game, winner):
            rows[str(bet["order_id"])] = bet
    owner = None
    for order in filled:
        bet = rows[str(order["id"])]
        bet["ingame"], bet["state_at_entry"] = bool(order.get("ingame")), None
        if bet["ingame"]:
            owner = owner or ingame_owner(conn, assignment)
            bet.update(model_id=owner["id"], lineage_id=owner["lineage_id"], clv=None,
                       state_at_entry=state_at(conn, game["game_id"], order["created_at"], order["id"]))
    return [rows[str(o["id"])] for o in filled]


def ingame_owner(conn: psycopg.Connection, assignment: dict[str, Any]) -> dict[str, Any]:
    """{id, lineage_id} the in-game rows are attributed to: the assignment's in-game
    model (the pre-game model only if none is set, which set_ingame prevents once
    in-game orders exist)."""
    model_id = assignment.get("ingame_model_id")
    if model_id is not None:
        row = conn.execute("SELECT id, lineage_id FROM models WHERE id = %s", (model_id,)).fetchone()
        if row is not None:
            return {"id": row["id"], "lineage_id": row["lineage_id"]}
    return {"id": assignment["model_id"], "lineage_id": assignment["lineage_id"]}


def state_at(conn: psycopg.Connection, game_id: str, ts: Any, order_id: Any = None) -> dict[str, Any] | None:
    """The game situation at approval: the `state_at_entry` the order's approval event
    recorded (host.trading.limits), else the newest game_state row at or before `ts`
    (None without one)."""
    if order_id is not None:
        event = conn.execute(
            "SELECT detail->'state_at_entry' AS s FROM order_events WHERE order_id = %s AND detail ? 'state_at_entry'"
            " ORDER BY id LIMIT 1",
            (order_id,),
        ).fetchone()
        if event is not None and isinstance(event["s"], dict):
            return {key: event["s"].get(key) for key in STATE_KEYS}
    row = conn.execute(
        f"SELECT {', '.join(STATE_KEYS)} FROM game_state WHERE game_id = %s AND ts <= %s ORDER BY ts DESC, id DESC LIMIT 1",
        (game_id, ts),
    ).fetchone()
    return None if row is None else {key: row[key] for key in STATE_KEYS}


def settle_totals(bets: list[dict[str, Any]]) -> tuple[int, int]:
    """(remaining basis, payout) the ledger `settle` row moves: the buy rows' sums."""
    buys = [b for b in bets if b.get("order_side", "buy") == "buy"]
    return sum(b["cost_cents"] for b in buys), sum(b["payout_cents"] for b in buys)
