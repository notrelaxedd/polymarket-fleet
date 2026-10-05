"""Selling a held position: the mirror image of the buy rule (docs/TRADING.md
"Selling (step 6 Part B)").

Pure helpers over one assignment of the GET /api/v1/trade/state payload, called from
fleet.worker.trade.TradeLoop.tick:

- plan_sells(): for each market where the assignment holds contracts, p_side from the
  model, fee = taker_rate * bid * (1 - bid), sell_edge = bid - fee - p_side; when
  sell_edge >= min_edge, a limit SELL at the bid for min(position size - open sell
  size, floor(participation * bid depth at or above the bid)) contracts. Never more
  than the position (no shorting) and at most one open sell per market.
- stale_sells(): ids of open sells whose sell_edge at the current bid is below zero.

Nothing is proposed for a halted assignment or after kickoff (trade_pregame_only); the
tick itself proposes nothing under kill. Positions are signed (buys minus sells), so a
sale that filled has already left the position size.
"""

from __future__ import annotations

import math
from typing import Any

from fleet.models.base import Model
from fleet.sim.book import depth_through
from fleet.worker.trade import (
    OPEN_STATUSES,
    _num,
    client_request_id,
    kicked_off,
    load_model,
    market_p_home,
    predict,
    trade_settings,
)

ORDER_SIDE = "sell"
# A sell being cancelled can still fill, and the host counts it as open (one open sell
# per market), so it blocks a new sell here too.
SELL_OPEN_STATUSES = (*OPEN_STATUSES, "cancel_requested")


def sell_edge(p_side: float, bid: float, taker_rate: float) -> tuple[float, float]:
    """(fee, sell_edge) for selling one contract of a side at the bid."""
    fee = taker_rate * bid * (1.0 - bid)
    return fee, bid - fee - p_side


def held_size(assignment: dict[str, Any], market_id: Any) -> int:
    """The signed position size (buys minus sells) the assignment holds on one market."""
    total = 0
    for p in assignment.get("positions") or []:
        if isinstance(p, dict) and str(p.get("market_id")) == str(market_id):
            total += int(_num(p.get("size")) or 0)
    return total


def _sell_orders(assignment: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in assignment.get("open_orders") or []
            if isinstance(o, dict) and o.get("side") == ORDER_SIDE and o.get("status") in SELL_OPEN_STATUSES]


def open_sells(assignment: dict[str, Any], market_id: Any) -> list[dict[str, Any]]:
    """The assignment's open sell orders on one market (cancel_requested included)."""
    return [o for o in _sell_orders(assignment) if str(o.get("market_id")) == str(market_id)]


def open_sell_size(assignment: dict[str, Any], market_id: Any) -> int:
    """Contracts still offered by the open sells on one market (size minus filled)."""
    return sum(max(0, int(_num(o.get("size")) or 0) - int(_num(o.get("filled_size")) or 0))
               for o in open_sells(assignment, market_id))


def _model_view(
    assignment: dict[str, Any], model: Model | None
) -> tuple[list[dict[str, Any]], float, float] | None:
    """(markets, market_p_home, my_p) or None when there is no price or no model."""
    markets = [m for m in assignment.get("markets") or [] if isinstance(m, dict) and m.get("side") in ("home", "away")]
    market_p = market_p_home(markets)
    model = model if model is not None else load_model(assignment.get("model"))
    if market_p is None or model is None:
        return None
    return markets, market_p, predict(assignment, model, market_p)


def plan_sells(
    assignment: dict[str, Any], settings: Any, model: Model | None = None, now: float | None = None
) -> list[dict[str, Any]]:
    """Sell requests for one assignment (the body of POST /api/v1/orders/request plus
    the team "side", which the tick strips before posting)."""
    if assignment.get("status") != "active":
        return []
    cfg = trade_settings(settings)
    if cfg["trade_pregame_only"] and kicked_off(assignment, now):
        return []
    if not any(held_size(assignment, m.get("id")) > 0 for m in assignment.get("markets") or [] if isinstance(m, dict)):
        return []
    view = _model_view(assignment, model)
    if view is None:
        return []
    markets, market_p, my_p = view
    min_edge, taker = float(cfg["min_edge"]), cfg["fee_model"]["taker_rate"]
    participation = float(cfg["participation"])
    out: list[dict[str, Any]] = []
    for m in markets:
        held = held_size(assignment, m.get("id"))
        bid, snapshot_id = _num(m.get("bid")), m.get("snapshot_id")
        if held <= 0 or m.get("status", "open") != "open" or open_sells(assignment, m.get("id")):
            continue
        if bid is None or snapshot_id is None or not 0.0 < bid < 1.0:
            continue
        p_side = my_p if m["side"] == "home" else 1.0 - my_p
        fee, edge = sell_edge(p_side, bid, taker)
        if edge < min_edge:
            continue
        room = held - open_sell_size(assignment, m.get("id"))
        size = min(room, math.floor(participation * depth_through(m.get("bid_depth") or [], bid, side="sell") + 1e-9))
        if size < max(1, int(_num(m.get("min_size")) or 1)):
            continue
        price = round(bid, 4)
        out.append({
            "client_request_id": client_request_id(assignment.get("id"), m.get("id"), snapshot_id, price, size, ORDER_SIDE),
            "job_id": assignment.get("job_id"), "lease_token": assignment.get("lease_token"),
            "assignment_id": assignment.get("id"), "market_id": m.get("id"), "snapshot_id": snapshot_id,
            "price": price, "size": size, "my_p": round(p_side, 6),
            "market_p": round(market_p if m["side"] == "home" else 1.0 - market_p, 6), "edge": round(edge, 6),
            "rationale": f"sell: my {p_side:.2f} vs bid {bid:.2f}, fee {fee:.3f}, edge {edge:.3f}",
            "order_side": ORDER_SIDE, "side": m["side"],
        })
    return out


def stale_sells(assignment: dict[str, Any], settings: Any, model: Model | None = None) -> list[str]:
    """Ids of the assignment's open sells whose sell_edge at the market's current bid is below 0."""
    orders = [o for o in _sell_orders(assignment) if o.get("status") in OPEN_STATUSES]
    if not orders:
        return []
    view = _model_view(assignment, model)
    if view is None:
        return []
    markets, _, my_p = view
    by_id = {str(m.get("id")): m for m in markets}
    taker = trade_settings(settings)["fee_model"]["taker_rate"]
    stale: list[str] = []
    for order in orders:
        market = by_id.get(str(order.get("market_id")))
        bid = _num(market.get("bid")) if market else None
        if market is None or bid is None:
            continue
        p_side = my_p if market["side"] == "home" else 1.0 - my_p
        if sell_edge(p_side, bid, taker)[1] < 0.0:
            stale.append(str(order.get("id")))
    return stale
