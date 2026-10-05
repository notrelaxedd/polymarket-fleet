"""In-game trade rules (docs/INGAME.md "In-game trade rules"; contract section 9).

Called from fleet.worker.trade.TradeLoop.tick, every ingame_tick_s, for assignments whose
payload "ingame" block is enabled once the game has kicked off; before kickoff the
pre-game rules run unchanged. Pure helpers over one assignment of GET /api/v1/trade/state:

- ingame_skip(): why nothing is proposed now, or None. In order: not active, not
  enabled, not in progress, no state, status not "in", state older than
  ingame_max_state_age_s, within ingame_quiet_seconds of the last score or possession
  change, game seconds left <= ingame_cutoff_seconds (regulation: (4 - period) * 900 +
  clock; overtime: the clock), no in-game model, no pre-game probability.
- plan_ingame(): p_home = ingame_wp.predict(state, pregame_p_home); a market whose
  |p_side - mid| < ingame_dead_zone gets nothing. Buys follow the pre-game rule with
  ingame_min_edge and the extra cap ingame_max_bet_cents, only while lag.suspended is
  false; sells follow the step 6B rule with ingame_min_edge, also while suspended.
  Every request carries "ingame": true and "gtd_seconds": ingame_gtd_seconds, and its
  client_request_id is "ingame-" + sha256(...|ingame)[:32].
- stale_ingame(): open orders whose edge under the current in-game probability is below 0.

The host checks every one of these rules again (host/trading/ingame.py); the worker only
requests. IngameRunner keeps the per-assignment cadence and the model cache for the loop.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any, Callable

from fleet.models.ingame_wp import FAMILY, IngameWP
from fleet.sim.book import depth_through
from fleet.worker.sell import ORDER_SIDE, held_size, open_sell_size, open_sells, sell_edge
from fleet.worker.trade import (
    _num,
    _open_orders,
    _parse_time,
    book_depth,
    committed_cents,
    equity_cents,
    kicked_off,
    load_model,
    market_p_home,
    side_edge,
    trade_settings,
)

DEFAULT_INGAME: dict[str, float] = {
    "ingame_tick_s": 5, "ingame_max_state_age_s": 30, "ingame_quiet_seconds": 20, "ingame_cutoff_seconds": 120,
    "ingame_dead_zone": 0.03, "ingame_min_edge": 0.05, "ingame_max_bet_cents": 500, "ingame_gtd_seconds": 60,
}
QUARTER_SECONDS = 900
CADENCE_SLACK = 0.9  # a tick this fraction of ingame_tick_s after the last run counts as due


def ingame_settings(settings: Any) -> dict[str, Any]:
    """The pre-game trade settings plus the in-game keys over their defaults."""
    out: dict[str, Any] = dict(trade_settings(settings), **DEFAULT_INGAME)
    if isinstance(settings, dict):
        for key in DEFAULT_INGAME:
            if _num(settings.get(key)) is not None:
                out[key] = float(settings[key])
    return out


def ingame_block(assignment: dict[str, Any]) -> dict[str, Any]:
    block = assignment.get("ingame")
    return block if isinstance(block, dict) else {}


def ingame_active(assignment: dict[str, Any], now: float | None = None) -> bool:
    """True when the in-game rules replace the pre-game ones: enabled and kicked off."""
    return ingame_block(assignment).get("enabled") is True and kicked_off(assignment, now)


def seconds_left(state: dict[str, Any]) -> int | None:
    """Game seconds remaining: (4 - period) * 900 + clock in regulation, the clock in overtime."""
    period, clock = _num(state.get("period")), _num(state.get("clock_seconds"))
    if period is None or clock is None or period < 1 or clock < 0:
        return None
    if period >= 5:
        return int(clock)
    return int((4 - int(period)) * QUARTER_SECONDS + min(clock, QUARTER_SECONDS))


def load_ingame_model(spec: Any, cache: dict[str, Any] | None = None) -> IngameWP | None:
    """The payload's in-game model (family ingame_wp only), cached by id."""
    if not isinstance(spec, dict) or spec.get("family") != FAMILY:
        return None
    model = load_model(spec, cache)
    return model if isinstance(model, IngameWP) else None


def ingame_skip(assignment: dict[str, Any], settings: Any, now: float | None = None,
                model: IngameWP | None = None) -> str | None:
    """Why the in-game rules propose nothing for this assignment now (None: go on)."""
    cfg = ingame_settings(settings)
    block = ingame_block(assignment)
    if assignment.get("status") != "active":
        return "inactive"
    if block.get("enabled") is not True:
        return "disabled"
    now = time.time() if now is None else now
    if not kicked_off(assignment, now):
        return "pregame"
    gs = block.get("game_state")
    state = gs.get("state") if isinstance(gs, dict) else None
    if not isinstance(state, dict):
        return "no_state"
    if state.get("status") != "in":
        return "not_in_play"
    age = _num(gs.get("age_s"))
    if age is None or age > cfg["ingame_max_state_age_s"]:
        return "stale"
    change = gs.get("last_change")
    if isinstance(change, dict):
        changed = _parse_time(change.get("ts"))
        if changed is None or now - changed < cfg["ingame_quiet_seconds"]:
            return "quiet"
    left = seconds_left(state)
    if left is None or left <= cfg["ingame_cutoff_seconds"]:
        return "cutoff"
    if model is None and load_ingame_model(block.get("model")) is None:
        return "no_model"
    p0 = _num(block.get("pregame_p_home"))
    if p0 is None or not 0.0 < p0 < 1.0:
        return "no_pregame_p"
    return None


def ingame_p_home(assignment: dict[str, Any], model: IngameWP) -> float:
    block = ingame_block(assignment)
    return float(model.predict(block["game_state"]["state"], _num(block.get("pregame_p_home"))))


def ingame_request_id(assignment_id: Any, market_id: Any, snapshot_id: Any, price: float, size: int,
                      order_side: str = "buy") -> str:
    """"ingame-" + sha256(assignment|market|snapshot_id|price|size|order_side|ingame)[:32]."""
    text = f"{assignment_id}|{market_id}|{snapshot_id}|{price:.4f}|{size}|{order_side}|ingame"
    return "ingame-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _mid(m: dict[str, Any]) -> float | None:
    mid = _num(m.get("mid"))
    if mid is None:
        bid, ask = _num(m.get("bid")), _num(m.get("ask"))
        mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    return mid


def _request(a: dict[str, Any], m: dict[str, Any], snapshot_id: Any, price: float, size: int, p_side: float,
             market_p: float, edge: float, rationale: str, gtd: float, order_side: str = "buy") -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_request_id": ingame_request_id(a.get("id"), m.get("id"), snapshot_id, price, size, order_side),
        "job_id": a.get("job_id"), "lease_token": a.get("lease_token"), "assignment_id": a.get("id"),
        "market_id": m.get("id"), "snapshot_id": snapshot_id, "price": price, "size": size,
        "my_p": round(p_side, 6), "market_p": round(market_p if m["side"] == "home" else 1.0 - market_p, 6),
        "edge": round(edge, 6), "rationale": rationale, "ingame": True, "gtd_seconds": int(gtd),
    }
    if order_side != "buy":
        body["order_side"] = order_side
    body["side"] = m["side"]
    return body


def plan_ingame(assignment: dict[str, Any], settings: Any, model: IngameWP | None = None,
                now: float | None = None) -> list[dict[str, Any]]:
    """In-game buy then sell requests for one assignment (plus "side" / "stake_cents",
    which the tick strips before posting)."""
    block = ingame_block(assignment)
    model = model if model is not None else load_ingame_model(block.get("model"))
    if ingame_skip(assignment, settings, now, model) is not None or model is None:
        return []
    cfg = ingame_settings(settings)
    markets = [m for m in assignment.get("markets") or [] if isinstance(m, dict) and m.get("side") in ("home", "away")]
    market_p = market_p_home(markets)
    if market_p is None:
        return []
    p_home = ingame_p_home(assignment, model)
    suspended = bool((block.get("lag") or {}).get("suspended"))
    taker, participation = cfg["fee_model"]["taker_rate"], float(cfg["participation"])
    min_edge, dead, gtd = cfg["ingame_min_edge"], cfg["ingame_dead_zone"], cfg["ingame_gtd_seconds"]
    available = int(_num((assignment.get("bankroll") or {}).get("available_cents")) or 0)
    equity = max(available, equity_cents(assignment.get("bankroll")))
    cap = min(available, int(cfg["ingame_max_bet_cents"]))
    for max_bet in (assignment.get("max_bet_cents"), cfg.get("max_bet_cents")):
        if _num(max_bet) is not None:
            cap = min(cap, int(max_bet))
    taken = {str(o.get("market_id")) for o in _open_orders(assignment)}
    buys: list[dict[str, Any]] = []
    sells: list[dict[str, Any]] = []
    for m in markets:
        p_side = p_home if m["side"] == "home" else 1.0 - p_home
        mid = _mid(m)
        mid = mid if mid is not None else (market_p if m["side"] == "home" else 1.0 - market_p)
        snapshot_id = m.get("snapshot_id")
        if abs(p_side - mid) < dead or m.get("status", "open") != "open" or snapshot_id is None:
            continue
        min_size = max(1, int(_num(m.get("min_size")) or 1))
        ask, bid = _num(m.get("ask")), _num(m.get("bid"))
        if (not suspended and available > 0 and not m.get("below_floor") and str(m.get("id")) not in taken
                and ask is not None and 0.0 < ask < 1.0):
            fee, cost, edge = side_edge(p_side, ask, taker)
            if edge >= min_edge and cost < 1.0:
                target = math.floor(float(cfg["kelly_fraction"]) * equity * edge / (1.0 - cost))
                stake = min(target - committed_cents(assignment, m.get("id")), cap)
                size = math.floor(stake / (cost * 100.0)) if stake > 0 else 0
                size = min(size, math.floor(participation * book_depth(m, ask) + 1e-9))
                if size >= min_size:
                    req = _request(assignment, m, snapshot_id, round(ask, 4), size, p_side, market_p, edge,
                                   f"in-game: my {p_side:.2f} vs ask {ask:.2f}, fee {fee:.3f}, edge {edge:.3f}", gtd)
                    buys.append(dict(req, stake_cents=stake))
        held = held_size(assignment, m.get("id"))
        if held <= 0 or open_sells(assignment, m.get("id")) or bid is None or not 0.0 < bid < 1.0:
            continue
        fee, edge = sell_edge(p_side, bid, taker)
        if edge < min_edge:
            continue
        room = held - open_sell_size(assignment, m.get("id"))
        size = min(room, math.floor(participation * depth_through(m.get("bid_depth") or [], bid, side="sell") + 1e-9))
        if size >= min_size:
            sells.append(_request(assignment, m, snapshot_id, round(bid, 4), size, p_side, market_p, edge,
                                  f"in-game sell: my {p_side:.2f} vs bid {bid:.2f}, fee {fee:.3f}, edge {edge:.3f}",
                                  gtd, ORDER_SIDE))
    return buys + sells


def stale_ingame(assignment: dict[str, Any], settings: Any, model: IngameWP | None = None) -> list[str]:
    """Open orders whose edge under the current in-game probability is below 0 (buys at
    the ask, sells at the bid). Nothing without a usable state (status "in", fresh)."""
    orders = _open_orders(assignment)
    block = ingame_block(assignment)
    model = model if model is not None else load_ingame_model(block.get("model"))
    gs = block.get("game_state") if isinstance(block.get("game_state"), dict) else {}
    state, age = gs.get("state"), _num(gs.get("age_s"))
    cfg = ingame_settings(settings)
    if (not orders or model is None or not isinstance(state, dict) or state.get("status") != "in"
            or age is None or age > cfg["ingame_max_state_age_s"] or _num(block.get("pregame_p_home")) is None):
        return []
    p_home = ingame_p_home(assignment, model)
    taker = cfg["fee_model"]["taker_rate"]
    by_id = {str(m.get("id")): m for m in assignment.get("markets") or [] if isinstance(m, dict)}
    stale: list[str] = []
    for order in orders:
        m = by_id.get(str(order.get("market_id")))
        if m is None or m.get("side") not in ("home", "away"):
            continue
        p_side = p_home if m["side"] == "home" else 1.0 - p_home
        if order.get("side") == ORDER_SIDE:
            bid = _num(m.get("bid"))
            bad = bid is not None and sell_edge(p_side, bid, taker)[1] < 0.0
        else:
            ask = _num(m.get("ask"))
            bad = ask is not None and side_edge(p_side, ask, taker)[2] < 0.0
        if bad:
            stale.append(str(order.get("id")))
    return stale


class IngameRunner:
    """The trade loop's in-game side: per-assignment cadence and the model cache."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.settings: dict[str, Any] = ingame_settings(None)
        self.live = False
        self.models: dict[str, Any] = {}
        self._last_run: dict[str, float] = {}
        self.last: dict[str, Any] = {}

    def configure(self, settings: Any) -> None:
        self.settings = ingame_settings(settings)

    def tick_seconds(self) -> float:
        return max(0.05, float(self.settings["ingame_tick_s"]))

    def due(self, assignment_id: Any) -> bool:
        """True when ingame_tick_s has passed since this assignment's last in-game run."""
        key, now = str(assignment_id), self.clock()
        last = self._last_run.get(key)
        if last is not None and now - last < CADENCE_SLACK * self.tick_seconds():
            return False
        self._last_run[key] = now
        return True

    def run(self, assignment: dict[str, Any], raw_settings: Any, now: float | None,
            request: Callable[[dict[str, Any]], dict[str, Any]],
            cancel: Callable[[str], bool]) -> tuple[list[dict[str, Any]], list[str]]:
        """One in-game pass for one assignment when due: (posted requests, cancelled ids)."""
        if not self.due(assignment.get("id")):
            return [], []
        model = load_ingame_model(ingame_block(assignment).get("model"), self.models)
        self.last[str(assignment.get("id"))] = {"skip": ingame_skip(assignment, raw_settings, now, model)}
        posted = [request(p) for p in plan_ingame(assignment, raw_settings, model, now)]
        cancelled = [oid for oid in stale_ingame(assignment, raw_settings, model) if cancel(oid)]
        return posted, cancelled
