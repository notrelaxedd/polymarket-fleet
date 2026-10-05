"""Trade role: the per-tick proposal loop (docs/TRADING.md "Trade worker tick").

Pure helpers, unit-testable without HTTP: market_p_home() devigs the two markets'
mids, plan_proposals() turns one assignment of the GET /api/v1/trade/state payload
into order requests (edge, Kelly stake, size, client_request_id, rationale) and
stale_orders() names the open buy orders whose edge at the current ask is below zero.
fleet.worker.sell mirrors both for selling a held position (plan_sells, stale_sells).
Models come from fleet.models.registry, rebuilt from the payload's artifact and
cached by model id.

TradeLoop wraps them for the agent: run() fetches the state, posts the proposals
(POST /api/v1/orders/request), cancels stale orders (POST /api/v1/orders/{id}/cancel)
and keeps a summary for status.json. Nothing is proposed under kill, for a halted
assignment, after kickoff (trade_pregame_only) or on a market below the liquidity
floor. release_jobs() is the POST /api/v1/trade/release handshake before a role change
away from trade (bounded to 4 s, one retry).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import math
import time
from typing import Any

from fleet.common import http
from fleet.models.base import Model
from fleet.models.registry import get_family
from fleet.sim.data import features_of
from fleet.sim.odds import clamp_prob

log = logging.getLogger("fleet.trade")

STATE_PATH = "/api/v1/trade/state"
REQUEST_PATH = "/api/v1/orders/request"
RELEASE_PATH = "/api/v1/trade/release"
RELEASE_TIMEOUT = 4.0
OPEN_STATUSES = ("approved", "submitting", "open", "partial")
DEFAULT_SETTINGS: dict[str, Any] = {
    "min_edge": 0.03, "kelly_fraction": 0.25, "participation": 0.5, "trade_pregame_only": True,
    "trade_tick_s": 5, "trade_max_games": 6, "fee_model": {"taker_rate": 0.05},
}

_MODEL_CACHE: dict[str, Model] = {}


# ------------------------------------------------------------------ helpers


def _num(value: Any) -> float | None:
    """A finite float from a JSON number or numeric string; None otherwise."""
    try:
        out = None if isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None
    return out if out is not None and math.isfinite(out) else None


def _parse_time(value: Any) -> float | None:
    """An ISO 8601 timestamp (Z or offset; naive means UTC) to epoch seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def trade_settings(settings: Any) -> dict[str, Any]:
    """The state payload's settings merged over the documented defaults."""
    out = dict(DEFAULT_SETTINGS)
    fee = dict(DEFAULT_SETTINGS["fee_model"])
    if isinstance(settings, dict):
        for key in ("min_edge", "kelly_fraction", "participation", "trade_tick_s", "trade_max_games", "max_bet_cents"):
            if _num(settings.get(key)) is not None:
                out[key] = settings[key]
        if isinstance(settings.get("trade_pregame_only"), bool):
            out["trade_pregame_only"] = settings["trade_pregame_only"]
        if isinstance(settings.get("fee_model"), dict) and _num(settings["fee_model"].get("taker_rate")) is not None:
            fee["taker_rate"] = float(settings["fee_model"]["taker_rate"])
    out["fee_model"] = fee
    return out


def load_model(spec: Any, cache: dict[str, Model] | None = None) -> Model | None:
    """The Model for a payload's {"id", "family", "params", "artifact"}, cached by id."""
    if not isinstance(spec, dict) or not spec.get("family"):
        return None
    store = _MODEL_CACHE if cache is None else cache
    key = str(spec.get("id") or "")
    if key and key in store:
        return store[key]
    try:
        family = get_family(str(spec["family"]))
        model = family.from_json(dict(spec.get("params") or {}), dict(spec.get("artifact") or {}))
    except (ValueError, TypeError, KeyError) as exc:
        log.warning("cannot load model %s: %s", key or spec.get("family"), exc)
        return None
    if key:
        store[key] = model
    return model


def market_p_home(markets: list[dict[str, Any]]) -> float | None:
    """Devig of the home and away mids; one market: its mid for that side."""
    mids: dict[str, float] = {}
    for m in markets:
        mid = _num(m.get("mid"))
        if mid is None:
            bid, ask = _num(m.get("bid")), _num(m.get("ask"))
            mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        if m.get("side") in ("home", "away") and mid is not None and m["side"] not in mids:
            mids[m["side"]] = mid
    if "home" in mids and "away" in mids:
        total = mids["home"] + mids["away"]
        return mids["home"] / total if total > 0 else None
    if "home" in mids:
        return mids["home"]
    if "away" in mids:
        return 1.0 - mids["away"]
    return None


def kicked_off(assignment: dict[str, Any], now: float | None = None) -> bool:
    """True once the game's kickoff_at has passed (or its status says it is on or over)."""
    game = assignment.get("game") or {}
    kickoff = _parse_time(game.get("kickoff_at"))
    return game.get("status") in ("final", "in_progress", "live") or (
        kickoff is not None and kickoff <= (time.time() if now is None else now))


def side_edge(p_side: float, ask: float, taker_rate: float) -> tuple[float, float, float]:
    """(fee, cost, edge) for buying one side at the ask."""
    fee = taker_rate * ask * (1.0 - ask)
    cost = ask + fee
    return fee, cost, p_side - cost


def client_request_id(
    assignment_id: Any, market_id: Any, snapshot_id: Any, price: float, size: int, order_side: str = "buy"
) -> str:
    """sha256(assignment|market|snapshot_id|price|size)[:32] for a buy (the step 4
    formula, so in-flight ids never change); a sell appends |order_side to the text."""
    text = f"{assignment_id}|{market_id}|{snapshot_id}|{price:.4f}|{size}"
    if order_side != "buy":
        text += f"|{order_side}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def predict(assignment: dict[str, Any], model: Model, market_p: float) -> float:
    """my_p = model.predict(game, market_p_home, features) on the payload's game row."""
    game = {"home_rest": None, "away_rest": None, **(assignment.get("game") or {})}
    game["season"] = int(game.get("season") or 0)
    return clamp_prob(float(model.predict(game, market_p, features_of(game))))


def _open_orders(assignment: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in assignment.get("open_orders") or [] if isinstance(o, dict) and o.get("status") in OPEN_STATUSES]


def equity_cents(bankroll: Any) -> int:
    """What the assignment has to size against: available plus reserved plus the
    cost of open positions (initial + realized by the ledger identity)."""
    bank = bankroll if isinstance(bankroll, dict) else {}
    return sum(int(_num(bank.get(k)) or 0) for k in ("available_cents", "reserved_cents", "open_cost_cents"))


def committed_cents(assignment: dict[str, Any], market_id: Any) -> int:
    """Cents already at work on one market: the basis of the position held there."""
    total = 0
    for p in assignment.get("positions") or []:
        if isinstance(p, dict) and str(p.get("market_id")) == str(market_id):
            total += int(_num(p.get("basis_cents")) or 0)
    return total


def book_depth(market: dict[str, Any], price: float) -> float:
    """Contracts offered at or below `price` across the market's ask levels."""
    total = 0.0
    for level in market.get("ask_depth") or []:
        try:
            level_price, size = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if level_price <= price + 1e-9 and size > 0:
            total += size
    return total


# ------------------------------------------------------------ pure helpers


def plan_proposals(
    assignment: dict[str, Any], settings: Any, model: Model | None = None, now: float | None = None
) -> list[dict[str, Any]]:
    """Order requests for one assignment, following docs/TRADING.md step by step.

    The Kelly stake is a target position, `floor(kelly * equity * edge / (1 - cost))`
    with equity = available + reserved + open cost, less what the assignment already
    holds on that market; so a filled position stops the worker re-buying the same
    edge tick after tick. The size is also held to `participation` of the book depth
    at or below the ask, the same cap the host enforces.
    """
    if assignment.get("status") != "active":
        return []
    cfg = trade_settings(settings)
    if cfg["trade_pregame_only"] and kicked_off(assignment, now):
        return []
    markets = [m for m in assignment.get("markets") or [] if isinstance(m, dict) and m.get("side") in ("home", "away")]
    market_p = market_p_home(markets)
    model = model if model is not None else load_model(assignment.get("model"))
    if market_p is None or model is None:
        return []
    my_p = predict(assignment, model, market_p)
    available = int(_num((assignment.get("bankroll") or {}).get("available_cents")) or 0)
    if available <= 0:
        return []
    equity = max(available, equity_cents(assignment.get("bankroll")))
    cap = available
    for max_bet in (assignment.get("max_bet_cents"), cfg.get("max_bet_cents")):
        if _num(max_bet) is not None:
            cap = min(cap, int(max_bet))
    taken = {str(o.get("market_id")) for o in _open_orders(assignment)}
    min_edge, kelly, taker = float(cfg["min_edge"]), float(cfg["kelly_fraction"]), cfg["fee_model"]["taker_rate"]
    participation = float(cfg["participation"])
    out: list[dict[str, Any]] = []
    for m in markets:
        ask, snapshot_id = _num(m.get("ask")), m.get("snapshot_id")
        if m.get("below_floor") or m.get("status", "open") != "open" or str(m.get("id")) in taken:
            continue
        if ask is None or snapshot_id is None or not 0.0 < ask < 1.0:
            continue
        p_side = my_p if m["side"] == "home" else 1.0 - my_p
        fee, cost, edge = side_edge(p_side, ask, taker)
        if edge < min_edge or cost >= 1.0:
            continue
        target = math.floor(kelly * equity * edge / (1.0 - cost))
        stake = min(target - committed_cents(assignment, m.get("id")), cap)
        if stake <= 0:
            continue
        size = math.floor(stake / (cost * 100.0))
        size = min(size, math.floor(participation * book_depth(m, ask) + 1e-9))
        if size < max(1, int(_num(m.get("min_size")) or 1)):
            continue
        out.append({
            "client_request_id": client_request_id(assignment.get("id"), m.get("id"), snapshot_id, ask, size),
            "job_id": assignment.get("job_id"), "lease_token": assignment.get("lease_token"),
            "assignment_id": assignment.get("id"), "market_id": m.get("id"), "snapshot_id": snapshot_id,
            "price": round(ask, 4), "size": size, "my_p": round(p_side, 6),
            "market_p": round(market_p if m["side"] == "home" else 1.0 - market_p, 6), "edge": round(edge, 6),
            "rationale": f"my {p_side:.2f} vs ask {ask:.2f}, fee {fee:.3f}, edge {edge:.3f}",
            "side": m["side"], "stake_cents": stake,
        })
    return out


def stale_orders(assignment: dict[str, Any], settings: Any, model: Model | None = None) -> list[str]:
    """Ids of the assignment's open buy orders whose edge at the market's current ask is
    below 0 (open sells are fleet.worker.sell.stale_sells' business)."""
    orders = [o for o in _open_orders(assignment) if o.get("side", "buy") != "sell"]
    if not orders:
        return []
    markets = {str(m.get("id")): m for m in assignment.get("markets") or [] if isinstance(m, dict)}
    market_p = market_p_home([m for m in markets.values() if m.get("side") in ("home", "away")])
    model = model if model is not None else load_model(assignment.get("model"))
    if market_p is None or model is None:
        return []
    my_p = predict(assignment, model, market_p)
    taker = trade_settings(settings)["fee_model"]["taker_rate"]
    stale: list[str] = []
    for order in orders:
        market = markets.get(str(order.get("market_id")))
        ask = _num(market.get("ask")) if market else None
        if market is None or ask is None or market.get("side") not in ("home", "away"):
            continue
        p_side = my_p if market["side"] == "home" else 1.0 - my_p
        if side_edge(p_side, ask, taker)[2] < 0.0:
            stale.append(str(order.get("id")))
    return stale


# ---------------------------------------------------------------- the loop


class TradeLoop:
    """One trade worker's tick, driven by the agent's main loop."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.settings: dict[str, Any] = trade_settings(None)
        self.last_tick: dict[str, Any] | None = None
        self.assignments: list[dict[str, Any]] = []
        self.ticks = 0
        self._models: dict[str, Model] = {}

    # knobs

    def trade_max_games(self) -> int:
        override = getattr(self.agent.options, "trade_max_games", None)
        value = override if override is not None else self.settings.get("trade_max_games")
        return max(0, min(100, int(_num(value) or 0)))

    def tick_seconds(self) -> float:
        override = getattr(self.agent.options, "trade_tick_s", None)
        value = override if override is not None else self.settings.get("trade_tick_s")
        return max(0.05, float(_num(value) or 5.0))

    def _post(self, path: str, body: dict[str, Any], timeout: float | None = None) -> Any:
        conf = self.agent.conf
        return http.post_json(conf["host_url"] + path, body, token=conf["worker_token"],
                              timeout=timeout if timeout is not None else self.agent.options.http_timeout)

    # state

    def fetch_state(self) -> dict[str, Any] | None:
        """GET /api/v1/trade/state; None (logged) when the host did not answer."""
        conf = self.agent.conf
        try:
            state = http.get_json(conf["host_url"] + STATE_PATH, token=conf["worker_token"], timeout=self.agent.options.http_timeout)
            if not isinstance(state, dict):
                raise http.HttpConnectionError(f"bad trade state: {state!r}")
        except (http.HttpError, http.HttpConnectionError) as exc:
            log.warning("trade state unavailable: %s", exc)
            self.last_tick = {"at": time.time(), "error": str(exc)}
            return None
        self.settings = trade_settings(state.get("settings"))
        return state

    def run(self) -> list[dict[str, Any]]:
        """Fetch the state and run one tick."""
        state = self.fetch_state()
        return self.tick(state) if state is not None else []

    def tick(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """Propose (buys, then sells), then cancel stale orders, per assignment; returns
        the posted requests with the host's answer under "result"."""
        from fleet.worker.sell import plan_sells, stale_sells  # sell builds on this module

        self.ticks += 1
        self.settings = trade_settings(state.get("settings"))
        killed = bool(state.get("kill")) or bool(getattr(self.agent, "kill", False))
        now = _parse_time(state.get("server_time"))
        posted: list[dict[str, Any]] = []
        cancelled: list[str] = []
        summaries: list[dict[str, Any]] = []
        for a in state.get("assignments") or []:
            if not isinstance(a, dict):
                continue
            summaries.append(self._summary(a))
            if killed:
                continue
            model = load_model(a.get("model"), self._models)
            for proposal in plan_proposals(a, self.settings, model, now) + plan_sells(a, self.settings, model, now):
                posted.append(self._request(proposal))
            for order_id in stale_orders(a, self.settings, model) + stale_sells(a, self.settings, model):
                if self._cancel(order_id):
                    cancelled.append(order_id)
        approved = sum(1 for p in posted if (p.get("result") or {}).get("status") == "approved")
        self.assignments = summaries
        self.last_tick = {
            "at": time.time(), "kill": killed, "assignments": len(summaries), "proposed": len(posted),
            "approved": approved, "rejected": len(posted) - approved, "cancelled": len(cancelled),
        }
        return posted

    @staticmethod
    def _summary(a: dict[str, Any]) -> dict[str, Any]:
        game, bank = a.get("game") or {}, a.get("bankroll") or {}
        return {
            "id": a.get("id"), "job_id": a.get("job_id"), "status": a.get("status"), "mode": a.get("mode"),
            "game_id": game.get("game_id"), "kickoff_at": game.get("kickoff_at"),
            "available_cents": bank.get("available_cents"), "open_orders": len(a.get("open_orders") or []),
        }

    def _request(self, proposal: dict[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in proposal.items() if k not in ("side", "stake_cents")}
        try:
            result = self._post(REQUEST_PATH, body)
            if not isinstance(result, dict):
                raise http.HttpConnectionError(f"bad answer {result!r}")
        except (http.HttpError, http.HttpConnectionError) as exc:
            log.warning("order request %s failed: %s", proposal["client_request_id"], exc)
            result = {"status": "error", "order_id": None, "reason": str(exc)}
        log.info("proposal %s %s %s x%d: %s (%s)", proposal.get("order_side", "buy"), proposal.get("side"),
                 proposal["price"], proposal["size"],
                 result.get("status"), result.get("reason") or proposal["rationale"])
        return dict(proposal, result=result)

    def _cancel(self, order_id: str) -> bool:
        try:
            self._post(f"/api/v1/orders/{order_id}/cancel", {})
        except (http.HttpError, http.HttpConnectionError) as exc:
            log.warning("cancel of order %s failed: %s", order_id, exc)
            return False
        log.info("cancelled stale order %s", order_id)
        return True

    # handshake

    def release_jobs(self, jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
        """POST /api/v1/trade/release for the held trade jobs, bounded to 4 s; one retry.
        None when the host never answered for good (the agent then carries the releases)."""
        body = {"jobs": [{"id": str(j["id"]), "lease_token": str(j["lease_token"])} for j in jobs]}
        timeout = min(self.agent.options.http_timeout, RELEASE_TIMEOUT)
        for attempt in (1, 2):
            try:
                answer = self._post(RELEASE_PATH, body, timeout=timeout)
                if isinstance(answer, dict):
                    log.info("trade release: %s order(s) cancelled, %s pending, %d job(s) released",
                             answer.get("cancelled"), answer.get("pending"), len(answer.get("released") or []))
                    return answer
                raise http.HttpConnectionError(f"bad answer {answer!r}")
            except http.HttpError as exc:
                if exc.status < 500:
                    log.warning("trade release refused (%s); proceeding", exc)
                    return {"cancelled": 0, "pending": 0, "released": []}
                log.warning("trade release failed (%s)%s", exc, "; retrying once" if attempt == 1 else "; proceeding")
            except http.HttpConnectionError as exc:
                log.warning("trade release not answered (%s)%s", exc, "; retrying once" if attempt == 1 else "; proceeding")
        return None

    def status(self) -> dict[str, Any]:
        """The status.json "trade" block."""
        return {
            "jobs": sorted(getattr(self.agent, "trade_jobs", {})), "max_games": self.trade_max_games(),
            "tick_seconds": self.tick_seconds(), "ticks": self.ticks, "assignments": list(self.assignments),
            "last_tick": self.last_tick,
        }
