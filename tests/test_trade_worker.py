"""Pure tests of the trade worker maths (fleet/worker/trade.py) against hand-computed
numbers, plus TradeLoop.tick against a scripted transport. No database, no network."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import pytest

from fleet.models.base import Model
from fleet.sim.odds import logit
from fleet.worker import trade
from fleet.worker.trade import (
    TradeLoop,
    client_request_id,
    kicked_off,
    load_model,
    market_p_home,
    plan_proposals,
    stale_orders,
    trade_settings,
)

TAKER = 0.05
SETTINGS = {"min_edge": 0.03, "kelly_fraction": 0.25, "trade_pregame_only": True, "fee_model": {"taker_rate": TAKER, "half_spread": 0.01}, "trade_tick_s": 5}


class Fixed(Model):
    """A model whose home-win probability is a constant."""

    family = "fixed"

    def __init__(self, p: float) -> None:
        super().__init__({})
        self.p = p

    def predict(self, game: dict[str, Any], market_p: float | None, features: dict[str, Any]) -> float:
        self.seen = (game, market_p, features)
        return self.p


def _market(side: str, ask: float, mid: float | None = None, **extra: Any) -> dict[str, Any]:
    m = {
        "id": f"m-{side}", "side": side, "bid": round(ask - 0.02, 4), "ask": ask, "mid": mid if mid is not None else round(ask - 0.01, 4),
        "tick": 0.01, "min_size": 1, "snapshot_id": 41 if side == "home" else 42, "snapshot_at": "2025-09-06T12:00:00Z",
        "liquidity_usd_cents": 500_000, "ask_depth": [[ask, 500]], "status": "open", "below_floor": False,
    }
    m.update(extra)
    return m


def _assignment(available: int = 10000, max_bet: int | None = None, **extra: Any) -> dict[str, Any]:
    a = {
        "id": "a1", "job_id": "j1", "lease_token": "tok", "status": "active", "mode": "paper", "max_bet_cents": max_bet,
        "game": {"game_id": "2025_01_BUF_KC", "season": 2025, "week": 1, "game_type": "REG", "kickoff_at": "2025-09-07T17:00:00Z",
                 "home_team": "KC", "away_team": "BUF", "home_rest": 7, "away_rest": 10, "div_game": False, "roof": "outdoors",
                 "surface": "grass", "temp": 70, "wind": 3, "status": "scheduled"},
        "model": {"id": "model-1", "family": "elo_blend", "params": {}, "artifact": {"ratings": {}, "blend": {"a": 0.0, "b": 0.0, "c": logit(0.6)}, "through": [2025, 1], "season": 2025}},
        "bankroll": {"available_cents": available, "reserved_cents": 0, "open_cost_cents": 0, "realized_pnl_cents": 0},
        "markets": [_market("home", 0.52), _market("away", 0.50)],
        "open_orders": [],
        "positions": [],
    }
    a.update(extra)
    return a


NOW = 1_757_000_000.0  # 2025-09-04, three days before the kickoff above


def _cost(ask: float) -> tuple[float, float]:
    fee = TAKER * ask * (1 - ask)
    return fee, ask + fee


# ---------------------------------------------------------------- devig


def test_market_p_home_devigs_the_two_mids() -> None:
    assert market_p_home([_market("home", 0.57, mid=0.56), _market("away", 0.47, mid=0.46)]) == pytest.approx(0.56 / 1.02)
    assert market_p_home([_market("home", 0.57, mid=0.56)]) == pytest.approx(0.56)
    assert market_p_home([_market("away", 0.47, mid=0.46)]) == pytest.approx(0.54)
    assert market_p_home([]) is None
    no_mid = _market("home", 0.57, mid=None)
    no_mid["mid"] = None
    assert market_p_home([no_mid]) == pytest.approx(0.56), "mid falls back to (bid + ask) / 2"


# ------------------------------------------------------------ proposals


def test_proposal_maths_fee_edge_kelly_stake_and_size() -> None:
    """my 0.57, home ask 0.52, away ask 0.50, bankroll $100, Kelly 0.25."""
    a = _assignment()
    out = plan_proposals(a, SETTINGS, model=Fixed(0.57), now=NOW)
    fee, cost = _cost(0.52)
    edge = 0.57 - cost
    assert fee == pytest.approx(0.01248) and cost == pytest.approx(0.53248) and edge == pytest.approx(0.03752)
    stake = math.floor(0.25 * 10000 * edge / (1 - cost))
    assert stake == 200
    size = math.floor(stake / (cost * 100))
    assert size == 3
    assert len(out) == 1, "the away side (0.43 vs cost 0.5125) has no edge"
    p = out[0]
    assert p["market_id"] == "m-home" and p["side"] == "home" and p["price"] == 0.52 and p["size"] == 3
    assert p["stake_cents"] == 200 and p["snapshot_id"] == 41
    assert p["my_p"] == pytest.approx(0.57) and p["edge"] == pytest.approx(edge, abs=1e-6)
    assert p["market_p"] == pytest.approx(0.51 / 1.0, abs=1e-6)  # mids 0.51 and 0.49 devig to 0.51
    assert p["rationale"] == "my 0.57 vs ask 0.52, fee 0.012, edge 0.038"
    assert p["job_id"] == "j1" and p["lease_token"] == "tok" and p["assignment_id"] == "a1"
    assert set(p) == {"client_request_id", "job_id", "lease_token", "assignment_id", "market_id", "snapshot_id", "price", "size",
                      "my_p", "market_p", "edge", "rationale", "side", "stake_cents"}


def test_model_gets_market_p_home_and_the_features_dict() -> None:
    model = Fixed(0.57)
    plan_proposals(_assignment(), SETTINGS, model=model, now=NOW)
    game, market_p, features = model.seen
    assert market_p == pytest.approx(0.51)
    assert game["home_team"] == "KC"
    assert features["home_rest"] == 7 and features["away_rest"] == 10 and features["week"] == 1 and features["season"] == 2025


def test_away_side_is_proposed_from_one_minus_my_p() -> None:
    a = _assignment()
    out = plan_proposals(a, SETTINGS, model=Fixed(0.40), now=NOW)
    fee, cost = _cost(0.50)
    assert [p["side"] for p in out] == ["away"]
    assert out[0]["my_p"] == pytest.approx(0.60) and out[0]["edge"] == pytest.approx(0.60 - cost, abs=1e-6)
    assert out[0]["market_p"] == pytest.approx(0.49)


def test_no_proposal_below_min_edge() -> None:
    fee, cost = _cost(0.52)
    just_below = cost + 0.03 - 1e-9
    assert plan_proposals(_assignment(), SETTINGS, model=Fixed(just_below), now=NOW) == []
    assert len(plan_proposals(_assignment(), SETTINGS, model=Fixed(cost + 0.03 + 1e-9), now=NOW)) == 1


def test_stake_is_capped_by_max_bet_and_available() -> None:
    fee, cost = _cost(0.52)
    big = plan_proposals(_assignment(available=1_000_000), SETTINGS, model=Fixed(0.70), now=NOW)[0]
    edge = 0.70 - cost
    assert big["stake_cents"] == math.floor(0.25 * 1_000_000 * edge / (1 - cost))
    capped = plan_proposals(_assignment(available=1_000_000, max_bet=2500), SETTINGS, model=Fixed(0.70), now=NOW)[0]
    assert capped["stake_cents"] == 2500 and capped["size"] == math.floor(2500 / (cost * 100)) == 46
    # With kelly_fraction <= 1 the Kelly stake is always below available (edge < 1 - cost);
    # a fraction above 1 shows the cap at available.
    poor = plan_proposals(_assignment(available=300), dict(SETTINGS, kelly_fraction=3.0), model=Fixed(0.99), now=NOW)[0]
    assert math.floor(3.0 * 300 * (0.99 - cost) / (1 - cost)) == 880
    assert poor["stake_cents"] == 300, "capped at available when Kelly asks for more"
    assert poor["size"] == math.floor(300 / (cost * 100)) == 5
    global_cap = plan_proposals(_assignment(available=1_000_000), dict(SETTINGS, max_bet_cents=1000), model=Fixed(0.70), now=NOW)[0]
    assert global_cap["stake_cents"] == 1000


def test_size_floor_and_min_size() -> None:
    full_kelly = dict(SETTINGS, kelly_fraction=1.0)
    a = _assignment(available=160)
    fee, cost = _cost(0.52)
    out = plan_proposals(a, full_kelly, model=Fixed(0.99), now=NOW)
    stake = math.floor(160 * (0.99 - cost) / (1 - cost))
    assert stake == 156
    assert out[0]["stake_cents"] == 156 and out[0]["size"] == math.floor(156 / (cost * 100)) == 2
    a["markets"][0]["min_size"] = 3
    assert plan_proposals(a, full_kelly, model=Fixed(0.99), now=NOW) == [], "size 2 is below min_size 3"
    assert plan_proposals(_assignment(available=50), full_kelly, model=Fixed(0.99), now=NOW) == [], "stake 48 buys no whole contract"
    assert plan_proposals(_assignment(available=0), full_kelly, model=Fixed(0.99), now=NOW) == []


def test_one_open_order_per_market() -> None:
    a = _assignment(open_orders=[{"id": "o1", "market_id": "m-home", "price": 0.52, "size": 3, "filled_size": 0, "status": "open"}])
    assert plan_proposals(a, SETTINGS, model=Fixed(0.70), now=NOW) == []
    a["open_orders"][0]["status"] = "cancelled"
    assert len(plan_proposals(a, SETTINGS, model=Fixed(0.70), now=NOW)) == 1, "a terminal order frees the market"


def test_below_floor_market_and_closed_market_are_skipped() -> None:
    a = _assignment()
    a["markets"][0]["below_floor"] = True
    assert plan_proposals(a, SETTINGS, model=Fixed(0.70), now=NOW) == []
    a["markets"][0]["below_floor"] = False
    a["markets"][0]["status"] = "resolved"
    assert plan_proposals(a, SETTINGS, model=Fixed(0.70), now=NOW) == []
    a["markets"][0]["status"] = "open"
    a["markets"][0]["snapshot_id"] = None
    assert plan_proposals(a, SETTINGS, model=Fixed(0.70), now=NOW) == [], "no snapshot to cite, no proposal"


def test_halted_assignment_and_kickoff_rules() -> None:
    assert plan_proposals(_assignment(status="halted"), SETTINGS, model=Fixed(0.70), now=NOW) == []
    kicked = _assignment()
    kicked["game"]["kickoff_at"] = "2025-09-01T17:00:00Z"
    assert kicked_off(kicked, NOW) and plan_proposals(kicked, SETTINGS, model=Fixed(0.70), now=NOW) == []
    assert len(plan_proposals(kicked, dict(SETTINGS, trade_pregame_only=False), model=Fixed(0.70), now=NOW)) == 1
    final = _assignment()
    final["game"]["status"] = "final"
    assert kicked_off(final, NOW)
    assert not kicked_off(_assignment(), NOW)


def test_client_request_id_is_stable_and_changes_with_its_inputs() -> None:
    a = _assignment()
    first = plan_proposals(a, SETTINGS, model=Fixed(0.57), now=NOW)[0]
    second = plan_proposals(a, SETTINGS, model=Fixed(0.57), now=NOW)[0]
    assert first["client_request_id"] == second["client_request_id"]
    expected = hashlib.sha256(b"a1|m-home|41|0.5200|3").hexdigest()[:32]
    assert first["client_request_id"] == expected == client_request_id("a1", "m-home", 41, 0.52, 3)
    assert len(expected) == 32
    a["markets"][0]["snapshot_id"] = 43
    assert plan_proposals(a, SETTINGS, model=Fixed(0.57), now=NOW)[0]["client_request_id"] != expected
    assert client_request_id("a1", "m-home", 41, 0.52, 4) != expected


def test_model_is_loaded_from_the_payload_artifact_and_cached_by_id() -> None:
    cache: dict[str, Any] = {}
    a = _assignment()
    model = load_model(a["model"], cache)
    assert model is not None and model.family == "elo_blend"
    assert model.predict(a["game"], 0.51, {}) == pytest.approx(0.6), "blend a=b=0 pins the prediction at expit(c)"
    assert load_model(a["model"], cache) is model and list(cache) == ["model-1"]
    out = plan_proposals(a, SETTINGS, now=NOW)  # default cache, no model given
    fee, cost = _cost(0.52)
    assert len(out) == 1 and out[0]["my_p"] == pytest.approx(0.6) and out[0]["edge"] == pytest.approx(0.6 - cost, abs=1e-6)
    assert load_model({"id": "x", "family": "no_such_family"}, {}) is None
    assert load_model(None) is None


def test_trade_settings_defaults_and_overrides() -> None:
    cfg = trade_settings(None)
    assert cfg["min_edge"] == 0.03 and cfg["kelly_fraction"] == 0.25 and cfg["trade_pregame_only"] is True
    assert cfg["fee_model"]["taker_rate"] == 0.05 and cfg["trade_tick_s"] == 5 and cfg["trade_max_games"] == 6
    cfg = trade_settings({"min_edge": 0.01, "fee_model": {"taker_rate": 0.02}, "trade_pregame_only": False, "trade_max_games": 2, "trade_tick_s": "x"})
    assert cfg["min_edge"] == 0.01 and cfg["fee_model"]["taker_rate"] == 0.02 and cfg["trade_pregame_only"] is False
    assert cfg["trade_max_games"] == 2 and cfg["trade_tick_s"] == 5


# --------------------------------------------------------------- cancels


def test_stale_orders_are_those_with_negative_edge_at_the_current_ask() -> None:
    orders = [
        {"id": "o-home", "market_id": "m-home", "price": 0.52, "size": 3, "filled_size": 0, "status": "open"},
        {"id": "o-away", "market_id": "m-away", "price": 0.50, "size": 2, "filled_size": 0, "status": "partial"},
        {"id": "o-done", "market_id": "m-home", "price": 0.52, "size": 3, "filled_size": 3, "status": "filled"},
    ]
    a = _assignment(open_orders=orders)
    assert stale_orders(a, SETTINGS, model=Fixed(0.57)) == ["o-away"], "0.43 against cost 0.5125 is negative; home still positive"
    a["markets"][0]["ask"] = 0.60  # home cost 0.612 > 0.57
    assert stale_orders(a, SETTINGS, model=Fixed(0.57)) == ["o-home", "o-away"]
    a["markets"][0]["ask"] = 0.52
    assert stale_orders(a, SETTINGS, model=Fixed(0.57)) == ["o-away"]
    orders[1]["status"] = "cancel_requested"
    assert stale_orders(a, SETTINGS, model=Fixed(0.57)) == [], "an order already being cancelled is left alone"
    assert stale_orders(_assignment(), SETTINGS, model=Fixed(0.57)) == []


# ------------------------------------------------------------ the loop


class _Options:
    http_timeout = 2.0
    trade_tick_s = None
    trade_max_games = None


class _Agent:
    conf = {"host_url": "http://fake", "worker_token": "t"}
    options = _Options()
    kill = False
    trade_jobs: dict[str, Any] = {}


def _scripted(monkeypatch, answers: dict[str, Any] | None = None) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []

    def post_json(url: str, body: Any, token: str | None = None, timeout: float = 4.0) -> Any:
        calls.append((url.replace("http://fake", ""), body))
        if url.endswith("/orders/request"):
            return (answers or {}).get("request", {"status": "approved", "order_id": "o-new", "reason": None})
        if url.endswith("/cancel"):
            return {"status": "cancelled"}
        if url.endswith("/trade/release"):
            answer = (answers or {}).get("release", {"cancelled": 1, "pending": 0, "released": [j["id"] for j in body["jobs"]]})
            if isinstance(answer, Exception):
                raise answer
            return answer
        raise AssertionError(url)

    monkeypatch.setattr(trade.http, "post_json", post_json)
    return calls


def _state(**extra: Any) -> dict[str, Any]:
    state = {"kill": False, "server_time": "2025-09-04T12:00:00Z", "settings": dict(SETTINGS, trade_max_games=3, trade_tick_s=2), "assignments": [_assignment()]}
    state.update(extra)
    return state


def test_tick_posts_proposals_and_cancels_through_the_worker_api(monkeypatch) -> None:
    calls = _scripted(monkeypatch)
    loop = TradeLoop(_Agent())
    posted = loop.tick(_state())
    assert len(posted) == 1 and posted[0]["result"] == {"status": "approved", "order_id": "o-new", "reason": None}
    assert calls[0][0] == "/api/v1/orders/request"
    body = calls[0][1]
    assert set(body) == {"client_request_id", "job_id", "lease_token", "assignment_id", "market_id", "snapshot_id", "price", "size", "my_p", "market_p", "edge", "rationale"}
    assert loop.last_tick["proposed"] == 1 and loop.last_tick["approved"] == 1 and loop.last_tick["cancelled"] == 0
    assert loop.settings["trade_max_games"] == 3 and loop.trade_max_games() == 3 and loop.tick_seconds() == 2.0
    assert loop.assignments[0]["game_id"] == "2025_01_BUF_KC" and loop.assignments[0]["status"] == "active"

    stale = _assignment(open_orders=[{"id": "o-away", "market_id": "m-away", "price": 0.50, "size": 2, "filled_size": 0, "status": "open"}])
    calls.clear()
    posted = loop.tick(_state(assignments=[stale]))
    assert [c[0] for c in calls] == ["/api/v1/orders/request", "/api/v1/orders/o-away/cancel"], "proposals first, then the stale cancel"
    assert loop.last_tick["cancelled"] == 1 and loop.ticks == 2


def test_tick_under_kill_proposes_and_cancels_nothing(monkeypatch) -> None:
    calls = _scripted(monkeypatch)
    loop = TradeLoop(_Agent())
    stale = _assignment(open_orders=[{"id": "o-away", "market_id": "m-away", "price": 0.50, "size": 2, "filled_size": 0, "status": "open"}])
    assert loop.tick(_state(kill=True, assignments=[stale])) == []
    assert calls == [] and loop.last_tick["kill"] is True and loop.last_tick["assignments"] == 1
    agent = _Agent()
    agent.kill = True
    assert TradeLoop(agent).tick(_state()) == [] and calls == [], "the agent's own kill flag counts too"


def test_tick_records_rejections_and_transport_errors(monkeypatch) -> None:
    _scripted(monkeypatch, {"request": {"status": "rejected", "order_id": "o-r", "reason": "bankroll"}})
    loop = TradeLoop(_Agent())
    posted = loop.tick(_state())
    assert posted[0]["result"]["reason"] == "bankroll" and loop.last_tick["rejected"] == 1

    def boom(url: str, body: Any, token: str | None = None, timeout: float = 4.0) -> Any:
        raise trade.http.HttpConnectionError("down")

    monkeypatch.setattr(trade.http, "post_json", boom)
    posted = loop.tick(_state())
    assert posted[0]["result"]["status"] == "error" and "down" in posted[0]["result"]["reason"]


def test_release_jobs_retries_once_then_gives_up(monkeypatch) -> None:
    calls = _scripted(monkeypatch)
    loop = TradeLoop(_Agent())
    jobs = [{"id": "j1", "lease_token": "t1"}, {"id": "j2", "lease_token": "t2"}]
    assert loop.release_jobs(jobs) == {"cancelled": 1, "pending": 0, "released": ["j1", "j2"]}
    assert calls == [("/api/v1/trade/release", {"jobs": jobs})]

    attempts: list[float] = []

    def failing(url: str, body: Any, token: str | None = None, timeout: float = 4.0) -> Any:
        attempts.append(timeout)
        raise trade.http.HttpConnectionError("no answer")

    monkeypatch.setattr(trade.http, "post_json", failing)
    assert loop.release_jobs(jobs) is None
    assert attempts == [2.0, 2.0], "bounded by http_timeout (never above 4 s), one retry"

    def refused(url: str, body: Any, token: str | None = None, timeout: float = 4.0) -> Any:
        raise trade.http.HttpError(401, "bad token", url)

    monkeypatch.setattr(trade.http, "post_json", refused)
    assert loop.release_jobs(jobs) == {"cancelled": 0, "pending": 0, "released": []}, "a 4xx is final"


# ------------------------------------------------- review fixes: positions, participation


def test_existing_position_at_target_means_no_proposal() -> None:
    """The Kelly stake is a target position: what is already held on the market comes off it."""
    fee, cost = _cost(0.52)
    edge = 0.57 - cost
    target = math.floor(0.25 * 10000 * edge / (1 - cost))
    assert target == 200
    held = _assignment(positions=[{"market_id": "m-home", "side": "home", "size": 3, "avg_price": 0.52, "basis_cents": 200}])
    assert plan_proposals(held, SETTINGS, model=Fixed(0.57), now=NOW) == [], "at target: nothing to add"
    part = _assignment(positions=[{"market_id": "m-home", "side": "home", "size": 1, "avg_price": 0.52, "basis_cents": 52}])
    out = plan_proposals(part, SETTINGS, model=Fixed(0.57), now=NOW)
    assert len(out) == 1 and out[0]["stake_cents"] == 200 - 52 and out[0]["size"] == math.floor(148 / (cost * 100)) == 2
    other = _assignment(positions=[{"market_id": "m-away", "side": "away", "size": 9, "avg_price": 0.5, "basis_cents": 450}])
    assert plan_proposals(other, SETTINGS, model=Fixed(0.57), now=NOW)[0]["stake_cents"] == 200, "a position elsewhere does not count"


def test_equity_includes_reserved_and_open_cost_so_fills_do_not_shrink_the_target() -> None:
    fee, cost = _cost(0.52)
    edge = 0.57 - cost
    a = _assignment(available=9800)
    a["bankroll"].update({"reserved_cents": 0, "open_cost_cents": 200})
    a["positions"] = [{"market_id": "m-home", "side": "home", "size": 3, "avg_price": 0.52, "basis_cents": 200}]
    assert plan_proposals(a, SETTINGS, model=Fixed(0.57), now=NOW) == [], "equity is still 10000: the position is the target"
    # the loop of the finding: feeding each fill back no longer re-buys the same edge
    a = _assignment(available=10000)
    held = 0
    for _ in range(60):
        props = plan_proposals(a, SETTINGS, model=Fixed(0.60), now=NOW)
        if not props:
            break
        p = props[0]
        held += p["size"]
        spent = math.floor(p["size"] * 0.52 * 100)
        a["bankroll"]["available_cents"] -= spent
        a["bankroll"]["open_cost_cents"] += spent
        a["positions"] = [{"market_id": "m-home", "side": "home", "size": held, "avg_price": 0.52, "basis_cents": held * 52}]
    target = math.floor(0.25 * 10000 * (0.60 - cost) / (1 - cost))
    assert held * 52 <= target < 10000 * 0.1, "about a quarter-Kelly target, not most of the bankroll"
    assert a["bankroll"]["available_cents"] > 9000


def test_size_is_held_to_participation_of_the_book_depth() -> None:
    a = _assignment(available=1_000_000)
    a["markets"][0]["ask_depth"] = [[0.51, 60], [0.52, 200], [0.53, 1000]]
    a["markets"][0]["ask"] = 0.52
    out = plan_proposals(a, dict(SETTINGS, participation=0.5), model=Fixed(0.70), now=NOW)
    assert out[0]["size"] == 130, "half of the 260 offered at or below the ask"
    out = plan_proposals(a, dict(SETTINGS, participation=0.1), model=Fixed(0.70), now=NOW)
    assert out[0]["size"] == 26
    a["markets"][0]["ask_depth"] = [[0.52, 5]]
    assert plan_proposals(a, dict(SETTINGS, participation=0.1), model=Fixed(0.70), now=NOW) == [], "0.5 contracts round to nothing"
    assert trade_settings({"participation": 0.3, "trade_max_games": 2})["participation"] == 0.3
    assert trade_settings({"participation": 0.3, "trade_max_games": 2})["trade_max_games"] == 2
    assert trade_settings(None)["participation"] == 0.5
