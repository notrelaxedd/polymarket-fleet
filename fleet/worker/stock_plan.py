"""The order batch of one stock decision (contract section 1), used by fleet.worker.stock_trade.

The model decides from the bars dated before the session (fleet.stocks.data.history_before,
the backtest's own cut). Equity E = cash + reserved + sum(qty * ref price), target =
floor(w * E / ref price), order = target - held (held counts the open orders' remaining
quantity), sells first, then buys.

Like fleet.stocks.backtest.run, the book is only rebalanced when the weights differ from
the previous session's decision (the model asked again with the bars before that
session): with unchanged weights a name is left to drift, and only traded when it is
missing (wanted but not held), extra (held but not wanted) or unfinished (the caller
says an earlier order for it was cut or rejected).

The host's limits are kept, so an order is never refused for its size: a buy is at most
stock_max_order_cents and keeps held + open buys + qty within stock_max_position_cents,
a sell at most stock_max_order_cents ("capped by max_order/max_position"). A buy is then
cut to the cash left after the earlier buys of the batch, at the host's reservation
ceil(qty * ref * (1 + stock_price_band)) ("cut to cash"); sells free no cash until they
fill. What max_order or cash leaves undone is reported as unfinished and bought at the
next sessions; max_position is a permanent cap, so that part stays in cash.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from fleet.stocks.backtest import clean_weights
from fleet.stocks.data import BENCHMARK, Bars, history_before, last_date
from fleet.stocks.families import StockModel, make_model

PENDING_STATUSES = ("approved", "submitting", "open", "partial")
DEFAULT_BAND = "0.05"
CAPPED = "capped by max_order/max_position"


@dataclass
class Plan:
    """orders: the batch; unfinished: names a later session should still trade;
    changed: the weights differ from the previous session's decision."""

    orders: list[dict[str, Any]]
    unfinished: set[str] = field(default_factory=set)
    changed: bool = True


def client_request_id(job_id: Any, session_date: str, symbol: str, side: str) -> str:
    return hashlib.sha256(f"{job_id}|{session_date}|{symbol}|{side}".encode("utf-8")).hexdigest()[:24]


def _int(value: Any) -> int:
    try:
        return int(value) if not isinstance(value, bool) else 0
    except (TypeError, ValueError):
        return 0


def open_quantities(orders: Any) -> tuple[dict[str, int], dict[str, int]]:
    """({symbol: remaining open buy qty}, {symbol: remaining open sell qty})."""
    buys: dict[str, int] = {}
    sells: dict[str, int] = {}
    for o in orders if isinstance(orders, list) else []:
        if not isinstance(o, dict) or o.get("status") not in PENDING_STATUSES:
            continue
        left = max(0, _int(o.get("qty")) - _int(o.get("filled_qty")))
        book = buys if o.get("side") == "buy" else sells
        book[str(o.get("symbol"))] = book.get(str(o.get("symbol")), 0) + left
    return buys, sells


def reservation_cents(qty: int, ref_cents: int, band: Decimal) -> int:
    """The host's buy reservation, ceil(qty * ref * (1 + band)), in exact decimals."""
    return int(math.ceil(Decimal(qty * ref_cents) * (Decimal(1) + band)))


def price_band(state: dict[str, Any]) -> Decimal:
    value = (state.get("settings") or {}).get("stock_price_band", DEFAULT_BAND)
    try:
        band = Decimal(str(value))
    except InvalidOperation:
        band = Decimal(DEFAULT_BAND)
    return band if band.is_finite() and band >= 0 else Decimal(DEFAULT_BAND)


def limit_cents(state: dict[str, Any], key: str) -> int | None:
    """A host limit from state.settings: None (no cap) when missing or not an int."""
    value = (state.get("settings") or {}).get(key)
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else None


def fit_buys_to_cash(buys: list[dict[str, Any]], cash_cents: int, band: Decimal) -> list[dict[str, Any]]:
    """Each buy cut to the cash the earlier buys left (dropped when not one share fits)."""
    left, out = cash_cents, []
    for order in buys:
        price, qty = order["ref_price_cents"], order["qty"]
        fit = min(qty, int(Decimal(max(left, 0)) / (Decimal(price) * (1 + band))))
        while fit > 0 and reservation_cents(fit, price, band) > left:
            fit -= 1
        if fit <= 0:
            continue
        if fit < qty:
            order = dict(order, qty=fit, rationale=f"{order['rationale']}, cut to cash {fit}")
        left -= reservation_cents(fit, price, band)
        out.append(order)
    return out


def _capped(delta: int, price: int, max_order: int | None, room: int | None) -> tuple[int, bool]:
    """(qty within the host's limits, whether a later session should do the rest).
    delta > 0 is a buy with room = the shares max_position still allows (None: no cap)."""
    qty = abs(delta)
    if max_order is not None:
        qty = min(qty, max_order // price)
    if delta > 0 and room is not None:
        qty = min(qty, room)
    # max_order leaves a shortfall the next sessions fill; max_position is permanent
    retry = 0 < qty < abs(delta) and (delta < 0 or room is None or qty < room)
    return qty, retry


def plan(state: dict[str, Any], bars: Bars, job_id: Any, model: StockModel | None = None,
         unfinished: set[str] | None = None) -> Plan:
    """The order batch for one due decision (sells first, then buys); see the module docstring."""
    a = state.get("assignment") or {}
    decision = state.get("decision") or {}
    session = str(decision.get("session_date") or "")
    symbols = [s for s in a.get("symbols") or [] if isinstance(s, str)]
    spec = state.get("model") or {}
    if model is None:
        model = make_model(str(spec.get("family")), dict(spec.get("params") or {}), symbols)
    ref = {s: _int(c) for s, c in (decision.get("ref_prices_cents") or {}).items() if _int(c) > 0}
    seen = sorted(set(symbols) | {BENCHMARK} | set(model.extra_symbols()))
    hist = history_before(bars, session, seen)
    picked = model.decide(hist, session)
    weights = clean_weights(picked.weights, set(symbols))
    prev_day = last_date(hist, seen)  # the previous session: what the backtest decided last
    prev = clean_weights(model.decide(history_before(hist, prev_day, seen), prev_day).weights,
                         set(symbols)) if prev_day else None
    out = Plan([], changed=prev != weights)
    carry = set(unfinished or ())
    positions = {str(s): _int(q) for s, q in (state.get("positions") or {}).items() if _int(q) > 0}
    open_buys, open_sells = open_quantities(state.get("open_orders"))
    max_order, max_position = limit_cents(state, "stock_max_order_cents"), limit_cents(state, "stock_max_position_cents")
    equity = _int(a.get("cash_cents")) + _int(a.get("reserved_cents"))
    for s, q in positions.items():
        price = ref.get(s) or (round(hist[s][-1].close * 100) if hist.get(s) else 0)
        equity += q * price
    sells: list[dict[str, Any]] = []
    buys: list[dict[str, Any]] = []
    for s in sorted(set(symbols)):
        price = ref.get(s)
        w = weights.get(s, 0.0)
        held = positions.get(s, 0) + open_buys.get(s, 0) - open_sells.get(s, 0)
        if not price:
            if w > 0 or held > 0:
                out.unfinished.add(s)  # like the backtest, a name without a price is retried
            continue
        target = math.floor(w * equity / price + 1e-9) if w > 0 else 0
        room = None if max_position is None else max(0, max_position // price - positions.get(s, 0) - open_buys.get(s, 0))
        wanted = target > 0 and (max_position is None or max_position // price > 0)
        if not out.changed and s not in carry and (held > 0) == wanted:
            continue  # unchanged weights: the position drifts, as in the backtest
        delta = target - held
        if delta < 0:
            delta = -min(-delta, positions.get(s, 0) - open_sells.get(s, 0))
        if delta == 0:
            continue
        qty, retry = _capped(delta, price, max_order, room)
        if retry:
            out.unfinished.add(s)
        if qty <= 0:
            continue
        side = "buy" if delta > 0 else "sell"
        note = picked.notes.get(s) or f"{model.family} not ranked"
        rationale = f"{note}, w {w:.2f}, target {target} held {held}" + (f", {CAPPED}" if qty < abs(delta) else "")
        order = {"client_request_id": client_request_id(job_id, session, s, side), "symbol": s, "side": side,
                 "qty": qty, "ref_price_cents": price, "rationale": rationale}
        (buys if side == "buy" else sells).append(order)
    fitted = fit_buys_to_cash(buys, _int(a.get("cash_cents")), price_band(state))
    kept = {o["symbol"]: o["qty"] for o in fitted}
    out.unfinished |= {o["symbol"] for o in buys if kept.get(o["symbol"], 0) < o["qty"]}
    out.orders = sells + fitted
    return out


def plan_orders(state: dict[str, Any], bars: Bars, job_id: Any, model: StockModel | None = None,
                unfinished: set[str] | None = None) -> list[dict[str, Any]]:
    """plan(...).orders."""
    return plan(state, bars, job_id, model, unfinished).orders
