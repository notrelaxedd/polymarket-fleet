"""The worker's sell rule (fleet/worker/sell.py, docs/TRADING.md "Selling"): sell maths by
hand, no shorting, one open sell per market, stale sells, the sell client_request_id and
the tick posting sells. Pure: no database, no network."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import pytest

from fleet.models.base import Model
from fleet.sim.odds import logit
from fleet.worker import trade
from fleet.worker.sell import held_size, open_sell_size, plan_sells, sell_edge, stale_sells
from fleet.worker.trade import TradeLoop, client_request_id, plan_proposals, stale_orders

TAKER = 0.05
SETTINGS = {"min_edge": 0.03, "kelly_fraction": 0.25, "participation": 0.5, "trade_pregame_only": True,
            "fee_model": {"taker_rate": TAKER}, "trade_tick_s": 5}
NOW = 1_757_000_000.0  # before the kickoff below


class Fixed(Model):
    """A model whose home-win probability is a constant."""

    family = "fixed"

    def __init__(self, p: float) -> None:
        super().__init__({})
        self.p = p

    def predict(self, game: dict[str, Any], market_p: float | None, features: dict[str, Any]) -> float:
        return self.p


def _market(side: str, bid: float, ask: float, **extra: Any) -> dict[str, Any]:
    m = {
        "id": f"m-{side}", "side": side, "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 4), "tick": 0.01,
        "min_size": 1, "snapshot_id": 41 if side == "home" else 42, "snapshot_at": "2025-09-06T12:00:00Z",
        "liquidity_usd_cents": 500_000, "ask_depth": [[ask, 500]], "bid_depth": [[bid, 500], [round(bid - 0.01, 4), 500]],
        "status": "open", "below_floor": False,
    }
    m.update(extra)
    return m


def _position(side: str, size: int, avg_cost: float = 0.45) -> dict[str, Any]:
    return {"market_id": f"m-{side}", "side": side, "size": size, "basis_cents": round(size * avg_cost * 100),
            "avg_cost": avg_cost}


def _assignment(positions: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    a = {
        "id": "a1", "job_id": "j1", "lease_token": "tok", "status": "active", "mode": "paper", "max_bet_cents": None,
        "game": {"game_id": "2025_01_BUF_KC", "season": 2025, "week": 1, "kickoff_at": "2025-09-07T17:00:00Z",
                 "home_team": "KC", "away_team": "BUF", "home_rest": 7, "away_rest": 10, "status": "scheduled"},
        "model": {"id": "model-sell-1", "family": "elo_blend", "params": {},
                  "artifact": {"ratings": {}, "blend": {"a": 0.0, "b": 0.0, "c": logit(0.4)}, "through": [2025, 1], "season": 2025}},
        "bankroll": {"available_cents": 10_000, "reserved_cents": 0, "open_cost_cents": 450, "realized_pnl_cents": 0},
        "markets": [_market("home", 0.50, 0.52), _market("away", 0.48, 0.50)],
        "open_orders": [],
        "positions": [_position("home", 10)] if positions is None else positions,
    }
    a.update(extra)
    return a


def _sell_order(oid: str, side: str = "home", price: float = 0.50, size: int = 4, filled: int = 0,
                status: str = "open") -> dict[str, Any]:
    return {"id": oid, "market_id": f"m-{side}", "side": "sell", "price": price, "size": size, "filled_size": filled,
            "status": status}


# ------------------------------------------------------------------ maths


def test_sell_maths_by_hand() -> None:
    """my 0.40 for home, bid 0.50: fee 0.05 * 0.5 * 0.5 = 0.0125, sell edge 0.0875."""
    fee, edge = sell_edge(0.40, 0.50, TAKER)
    assert fee == pytest.approx(0.0125) and edge == pytest.approx(0.0875)
    out = plan_sells(_assignment(), SETTINGS, model=Fixed(0.40), now=NOW)
    assert len(out) == 1, "the away side (my 0.60 vs bid 0.48) is not held and has no sell edge anyway"
    p = out[0]
    assert p["order_side"] == "sell" and p["side"] == "home" and p["market_id"] == "m-home"
    assert p["price"] == 0.50 and p["size"] == 10 and p["snapshot_id"] == 41
    assert p["edge"] == pytest.approx(0.0875) and p["my_p"] == pytest.approx(0.40)
    assert p["rationale"] == "sell: my 0.40 vs bid 0.50, fee 0.013, edge 0.087"
    assert p["job_id"] == "j1" and p["lease_token"] == "tok" and p["assignment_id"] == "a1"
    assert p["client_request_id"] == client_request_id("a1", "m-home", 41, 0.50, 10, "sell")
    assert set(p) == {"client_request_id", "job_id", "lease_token", "assignment_id", "market_id", "snapshot_id", "price",
                      "size", "my_p", "market_p", "edge", "rationale", "order_side", "side"}


def test_no_sell_unless_the_bid_overshoots_the_model_by_min_edge() -> None:
    a = _assignment()
    # my 0.46: edge 0.50 - 0.0125 - 0.46 = 0.0275 < 0.03
    assert plan_sells(a, SETTINGS, model=Fixed(0.46), now=NOW) == []
    # my 0.45: edge 0.0375 >= 0.03
    assert len(plan_sells(a, SETTINGS, model=Fixed(0.45), now=NOW)) == 1
    # the model above the bid: never a sell (that is a hold or a buy)
    assert plan_sells(a, SETTINGS, model=Fixed(0.60), now=NOW) == []
    assert plan_sells(a, dict(SETTINGS, min_edge=0.09), model=Fixed(0.40), now=NOW) == [], "min_edge comes from Settings"


def test_away_position_uses_one_minus_my_p() -> None:
    a = _assignment(positions=[_position("away", 6)])
    # my home 0.60 -> away 0.40; away bid 0.48: fee 0.01248, edge 0.06752
    out = plan_sells(a, SETTINGS, model=Fixed(0.60), now=NOW)
    assert [(p["market_id"], p["size"], p["price"]) for p in out] == [("m-away", 6, 0.48)]
    assert out[0]["edge"] == pytest.approx(0.48 - TAKER * 0.48 * 0.52 - 0.40, abs=1e-6)


def test_no_position_no_sell_and_no_shorting() -> None:
    assert plan_sells(_assignment(positions=[]), SETTINGS, model=Fixed(0.10), now=NOW) == []
    assert plan_sells(_assignment(positions=[_position("home", 0)]), SETTINGS, model=Fixed(0.10), now=NOW) == []
    assert plan_sells(_assignment(positions=[_position("away", 5)]), SETTINGS, model=Fixed(0.10), now=NOW) == [], \
        "a held away contract with no away sell edge sells nothing; the home side is not held"
    out = plan_sells(_assignment(positions=[_position("home", 3)]), SETTINGS, model=Fixed(0.10), now=NOW)
    assert out[0]["size"] == 3, "never more than the position, however deep the book"


def test_size_is_held_to_participation_of_bid_depth_at_or_above_the_bid() -> None:
    a = _assignment(positions=[_position("home", 100)])
    a["markets"][0]["bid_depth"] = [[0.50, 30], [0.49, 1000]]
    assert plan_sells(a, SETTINGS, model=Fixed(0.40), now=NOW)[0]["size"] == 15, "half of the 30 bid at 0.50"
    assert plan_sells(a, dict(SETTINGS, participation=0.1), model=Fixed(0.40), now=NOW)[0]["size"] == 3
    a["markets"][0]["bid_depth"] = [[0.50, 1]]
    assert plan_sells(a, SETTINGS, model=Fixed(0.40), now=NOW) == [], "floor(0.5 * 1) = 0 contracts"
    a["markets"][0]["bid_depth"] = [[0.50, 500]]
    a["markets"][0]["min_size"] = 200
    assert plan_sells(a, SETTINGS, model=Fixed(0.40), now=NOW) == [], "below the market's min_size"


def test_signed_positions_are_summed_per_market() -> None:
    a = _assignment(positions=[_position("home", 10), {"market_id": "m-home", "side": "home", "size": -4}])
    assert held_size(a, "m-home") == 6 and held_size(a, "m-away") == 0
    assert plan_sells(a, SETTINGS, model=Fixed(0.40), now=NOW)[0]["size"] == 6


def test_one_open_sell_per_market() -> None:
    a = _assignment(positions=[_position("home", 10), _position("away", 5)], open_orders=[_sell_order("s1", size=4)])
    out = plan_sells(a, SETTINGS, model=Fixed(0.20), now=NOW)
    assert [p["market_id"] for p in out] == [], "home has an open sell; away (my 0.80) has no sell edge"
    out = plan_sells(a, SETTINGS, model=Fixed(0.40), now=NOW)
    assert out == []
    a["positions"] = [_position("home", 10), _position("away", 5)]
    a["markets"][1]["bid"] = 0.70
    a["markets"][1]["bid_depth"] = [[0.70, 500]]
    out = plan_sells(a, SETTINGS, model=Fixed(0.50), now=NOW)
    assert [(p["market_id"], p["size"]) for p in out] == [("m-away", 5)], "an open sell elsewhere does not block"
    for status in ("approved", "submitting", "partial", "cancel_requested"):
        a["open_orders"] = [_sell_order("s1", status=status)]
        assert all(p["market_id"] != "m-home" for p in plan_sells(a, SETTINGS, model=Fixed(0.20), now=NOW)), status
    a["open_orders"] = [_sell_order("s1", status="filled")]
    assert any(p["market_id"] == "m-home" for p in plan_sells(a, SETTINGS, model=Fixed(0.20), now=NOW))
    a["open_orders"] = [{"id": "b1", "market_id": "m-home", "side": "buy", "price": 0.52, "size": 2, "filled_size": 0,
                         "status": "open"}]
    assert any(p["market_id"] == "m-home" for p in plan_sells(a, SETTINGS, model=Fixed(0.20), now=NOW)), \
        "an open buy is not an open sell"


def test_open_sell_size_counts_the_unfilled_part() -> None:
    a = _assignment(open_orders=[_sell_order("s1", size=6, filled=2, status="partial"), _sell_order("s2", side="away")])
    assert open_sell_size(a, "m-home") == 4 and open_sell_size(a, "m-away") == 4


def test_halted_kicked_off_or_unpriced_assignments_sell_nothing() -> None:
    assert plan_sells(_assignment(status="halted"), SETTINGS, model=Fixed(0.10), now=NOW) == []
    after = 1_757_300_000.0  # after the 2025-09-07 17:00Z kickoff
    assert plan_sells(_assignment(), SETTINGS, model=Fixed(0.10), now=after) == []
    assert len(plan_sells(_assignment(), dict(SETTINGS, trade_pregame_only=False), model=Fixed(0.10), now=after)) == 1
    a = _assignment()
    a["markets"][0]["bid"] = None
    assert plan_sells(a, SETTINGS, model=Fixed(0.10), now=NOW) == []
    a = _assignment()
    a["markets"][0]["status"] = "resolved"
    assert plan_sells(a, SETTINGS, model=Fixed(0.10), now=NOW) == []


def test_model_loaded_from_the_payload_when_not_given() -> None:
    out = plan_sells(_assignment(), SETTINGS, now=NOW)  # the artifact predicts about 0.40 for home
    assert len(out) == 1 and out[0]["my_p"] == pytest.approx(0.40, abs=0.01)


# ------------------------------------------------------------------ stale


def test_stale_sells_are_those_with_negative_sell_edge_at_the_current_bid() -> None:
    a = _assignment(open_orders=[_sell_order("s1"), _sell_order("s2", side="away", status="partial", filled=1),
                                 _sell_order("s3", status="filled")])
    # my 0.40: home at bid 0.50 edge +0.0875 keeps; away my 0.60 at bid 0.48 edge -0.13 is stale
    assert stale_sells(a, SETTINGS, model=Fixed(0.40)) == ["s2"]
    a["markets"][0]["bid"] = 0.41  # 0.41 - 0.0121 - 0.40 < 0
    assert stale_sells(a, SETTINGS, model=Fixed(0.40)) == ["s1", "s2"]
    a["open_orders"][0]["status"] = "cancel_requested"
    assert stale_sells(a, SETTINGS, model=Fixed(0.40)) == ["s2"], "an order already being cancelled is left alone"


def test_buy_staleness_ignores_sells_and_sell_staleness_ignores_buys() -> None:
    buy = {"id": "b1", "market_id": "m-away", "price": 0.50, "size": 2, "filled_size": 0, "status": "open"}
    a = _assignment(open_orders=[_sell_order("s1"), buy])
    # my 0.40: the home sell is fine; a buy on home would be stale (0.40 vs cost 0.5325) but s1 is a sell
    assert stale_orders(a, SETTINGS, model=Fixed(0.40)) == [], "away buy at my 0.60 vs cost 0.5125 still has edge"
    a["open_orders"][1]["market_id"] = "m-home"
    assert stale_orders(a, SETTINGS, model=Fixed(0.40)) == ["b1"]
    assert stale_sells(a, SETTINGS, model=Fixed(0.40)) == []


def test_an_open_sell_blocks_buys_on_its_market() -> None:
    a = _assignment(positions=[], open_orders=[_sell_order("s1")])
    assert plan_proposals(a, SETTINGS, model=Fixed(0.70), now=NOW) == []
    a["open_orders"] = []
    assert len(plan_proposals(a, SETTINGS, model=Fixed(0.70), now=NOW)) == 1


# ------------------------------------------------------------------ ids


def test_client_request_id_buys_unchanged_sells_suffixed() -> None:
    old = hashlib.sha256(b"a1|m-home|41|0.5000|10").hexdigest()[:32]
    assert client_request_id("a1", "m-home", 41, 0.5, 10) == old
    assert client_request_id("a1", "m-home", 41, 0.5, 10, "buy") == old
    sell = client_request_id("a1", "m-home", 41, 0.5, 10, "sell")
    assert sell == hashlib.sha256(b"a1|m-home|41|0.5000|10|sell").hexdigest()[:32] and sell != old and len(sell) == 32


# ------------------------------------------------------------------ the tick


class _Options:
    http_timeout = 2.0
    trade_tick_s = None
    trade_max_games = None


class _Agent:
    conf = {"host_url": "http://fake", "worker_token": "t"}
    options = _Options()
    kill = False
    trade_jobs: dict[str, Any] = {}


def _scripted(monkeypatch) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []

    def post_json(url: str, body: Any, token: str | None = None, timeout: float = 4.0) -> Any:
        calls.append((url.replace("http://fake", ""), body))
        if url.endswith("/orders/request"):
            return {"status": "approved", "order_id": "o-new", "reason": None}
        if url.endswith("/cancel"):
            return {"status": "cancelled"}
        raise AssertionError(url)

    monkeypatch.setattr(trade.http, "post_json", post_json)
    return calls


def _state(**extra: Any) -> dict[str, Any]:
    state = {"kill": False, "server_time": "2025-09-04T12:00:00Z", "settings": dict(SETTINGS), "assignments": [_assignment()]}
    state.update(extra)
    return state


def test_tick_posts_a_sell_and_cancels_a_stale_sell(monkeypatch) -> None:
    calls = _scripted(monkeypatch)
    loop = TradeLoop(_Agent())
    posted = loop.tick(_state())
    sells = [p for p in posted if p.get("order_side") == "sell"]
    assert len(sells) == 1 and sells[0]["result"]["status"] == "approved"
    body = next(b for path, b in calls if path == "/api/v1/orders/request" and b.get("order_side") == "sell")
    assert set(body) == {"client_request_id", "job_id", "lease_token", "assignment_id", "market_id", "snapshot_id", "price",
                         "size", "my_p", "market_p", "edge", "rationale", "order_side"}, "the team side is not sent"
    assert body["price"] == 0.50 and body["market_id"] == "m-home"
    stale = _assignment(open_orders=[_sell_order("s-away", side="away")])
    calls.clear()
    loop.tick(_state(assignments=[stale]))
    assert "/api/v1/orders/s-away/cancel" in [c[0] for c in calls]
    assert loop.last_tick["cancelled"] == 1


def test_tick_under_kill_sells_nothing(monkeypatch) -> None:
    calls = _scripted(monkeypatch)
    loop = TradeLoop(_Agent())
    stale = _assignment(open_orders=[_sell_order("s-away", side="away")])
    assert loop.tick(_state(kill=True, assignments=[stale])) == [] and calls == []


def test_sell_size_never_exceeds_position_minus_open_sells_over_ticks() -> None:
    """Feeding sells back as fills: the position shrinks and is never oversold."""
    a = _assignment(positions=[_position("home", 25)])
    a["markets"][0]["bid_depth"] = [[0.50, 12]]
    sold = 0
    for _ in range(20):
        out = plan_sells(a, SETTINGS, model=Fixed(0.30), now=NOW)
        if not out:
            break
        size = out[0]["size"]
        assert size <= held_size(a, "m-home") - open_sell_size(a, "m-home")
        sold += size
        a["positions"] = [_position("home", 25 - sold)] if sold < 25 else []
    assert sold == 25 and math.isclose(held_size(a, "m-home"), 0)
