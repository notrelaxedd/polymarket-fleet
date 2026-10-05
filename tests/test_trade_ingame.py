"""The worker's in-game rules (fleet/worker/trade_ingame.py; contract section 9), checked
by hand: freshness, quiet period, cutoff (regulation and overtime clocks), dead zone,
ingame_min_edge, the ingame_max_bet_cents cap, buys blocked while the feed lag is
suspended (sells allowed), the request fields, the client_request_id, the model loaded
from the payload and cached by id, and the TradeLoop hook (cadence, kill, pre-game
unchanged before kickoff). Pure: no database, no network."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from typing import Any

import pytest

from fleet.models.ingame_wp import FEATURE_NAMES
from fleet.sim.odds import logit
from fleet.worker import trade
from fleet.worker.trade import TradeLoop, client_request_id, plan_proposals
from fleet.worker.trade_ingame import (
    IngameRunner,
    ingame_request_id,
    ingame_settings,
    ingame_skip,
    load_ingame_model,
    plan_ingame,
    seconds_left,
    stale_ingame,
)

TAKER = 0.05
SETTINGS: dict[str, Any] = {
    "min_edge": 0.03, "kelly_fraction": 0.25, "participation": 0.5, "trade_pregame_only": True,
    "fee_model": {"taker_rate": TAKER}, "trade_tick_s": 5,
    "ingame_tick_s": 5, "ingame_max_state_age_s": 30, "ingame_quiet_seconds": 20, "ingame_cutoff_seconds": 120,
    "ingame_dead_zone": 0.03, "ingame_min_edge": 0.05, "ingame_max_bet_cents": 500, "ingame_gtd_seconds": 60,
}
KICKOFF = "2025-09-07T17:00:00Z"
NOW = dt.datetime(2025, 9, 7, 18, 0, tzinfo=dt.timezone.utc).timestamp()  # an hour after kickoff
SERVER_TIME = "2025-09-07T18:00:00Z"
BEFORE = dt.datetime(2025, 9, 7, 12, 0, tzinfo=dt.timezone.utc).timestamp()


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _model_spec(p: float, model_id: str | None = None) -> dict[str, Any]:
    """An ingame_wp model whose only non-zero coefficient is the constant: p_home = p.
    One id per probability: models are cached by id (as model ids are in production)."""
    model_id = model_id or f"ig-{p}"
    coef = [logit(p)] + [0.0] * (len(FEATURE_NAMES) - 1)
    return {"id": model_id, "family": "ingame_wp", "params": {"l2": 1.0, "time_scale": 1.0, "fp_scale": 1.0},
            "artifact": {"coef": coef, "features": list(FEATURE_NAMES), "n_train": 1000, "train_seasons": [2020]}}


def _state(**extra: Any) -> dict[str, Any]:
    s = {"status": "in", "period": 3, "clock_seconds": 412, "home_score": 17, "away_score": 14, "possession": "home",
         "down": 1, "distance": 10, "yardline_100": 60, "home_timeouts": 3, "away_timeouts": 2}
    s.update(extra)
    return s


def _ingame(p: float = 0.70, age_s: float = 3.0, last_change: dict[str, Any] | None = None, suspended: bool = False,
            enabled: bool = True, **state: Any) -> dict[str, Any]:
    return {
        "enabled": enabled, "model": _model_spec(p), "pregame_p_home": 0.55,
        "game_state": {"state": _state(**state), "ts": _iso(NOW - age_s), "age_s": age_s, "source": "espn_summary",
                       "last_change": last_change},
        "lag": {"suspended": suspended, "median_lag_s": 6.0, "n": 12},
    }


def _market(side: str, bid: float, ask: float, **extra: Any) -> dict[str, Any]:
    m = {"id": f"m-{side}", "side": side, "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 4), "tick": 0.01,
         "min_size": 1, "snapshot_id": 41 if side == "home" else 42, "liquidity_usd_cents": 500_000,
         "ask_depth": [[ask, 500]], "bid_depth": [[bid, 500]], "status": "open", "below_floor": False}
    m.update(extra)
    return m


def _assignment(ingame: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    a = {
        "id": "a1", "job_id": "j1", "lease_token": "tok", "status": "active", "mode": "paper", "max_bet_cents": None,
        "game": {"game_id": "2025_01_BUF_KC", "season": 2025, "week": 1, "kickoff_at": KICKOFF, "home_team": "KC",
                 "away_team": "BUF", "home_rest": 7, "away_rest": 10, "status": "scheduled"},
        "model": {"id": "pre-1", "family": "elo_blend", "params": {},
                  "artifact": {"ratings": {}, "blend": {"a": 0.0, "b": 0.0, "c": logit(0.7)}, "through": [2025, 1],
                               "season": 2025}},
        "bankroll": {"available_cents": 100_000, "reserved_cents": 0, "open_cost_cents": 0, "realized_pnl_cents": 0},
        "markets": [_market("home", 0.58, 0.60), _market("away", 0.40, 0.42)],
        "open_orders": [], "positions": [],
        "ingame": _ingame() if ingame is None else ingame,
    }
    a.update(extra)
    return a


# ------------------------------------------------------------------ maths by hand


def test_buy_by_hand() -> None:
    """p_home 0.70 vs home ask 0.60: fee 0.05 * 0.6 * 0.4 = 0.012, cost 0.612, edge 0.088
    >= ingame_min_edge 0.05. Kelly target 0.25 * 100000 * 0.088 / 0.388 = 5670 cents, capped by
    ingame_max_bet_cents 500, so size floor(500 / 61.2) = 8. Away: 0.30 vs 0.42, no edge."""
    out = plan_ingame(_assignment(), SETTINGS, now=NOW)
    assert len(out) == 1
    p = out[0]
    assert p["side"] == "home" and p["market_id"] == "m-home" and p["price"] == 0.60 and p["size"] == 8
    assert p["stake_cents"] == 500 and p["edge"] == pytest.approx(0.088) and p["my_p"] == pytest.approx(0.70)
    assert p["market_p"] == pytest.approx(0.59) and "order_side" not in p
    assert p["ingame"] is True and p["gtd_seconds"] == 60
    assert p["rationale"] == "in-game: my 0.70 vs ask 0.60, fee 0.012, edge 0.088"
    assert p["job_id"] == "j1" and p["lease_token"] == "tok" and p["assignment_id"] == "a1" and p["snapshot_id"] == 41


def test_client_request_id_includes_ingame() -> None:
    p = plan_ingame(_assignment(), SETTINGS, now=NOW)[0]
    text = "a1|m-home|41|0.6000|8|buy|ingame"
    assert p["client_request_id"] == "ingame-" + hashlib.sha256(text.encode()).hexdigest()[:32]
    assert p["client_request_id"] == ingame_request_id("a1", "m-home", 41, 0.60, 8)
    assert "ingame" in p["client_request_id"] and len(p["client_request_id"]) <= 64
    assert p["client_request_id"] != client_request_id("a1", "m-home", 41, 0.60, 8), "never collides with a pre-game id"
    assert ingame_request_id("a1", "m-home", 41, 0.60, 8, "sell") != p["client_request_id"]


def test_ingame_min_edge_replaces_min_edge() -> None:
    """p 0.66: edge 0.66 - 0.612 = 0.048 clears min_edge 0.03 but not ingame_min_edge 0.05."""
    a = _assignment(_ingame(p=0.66))
    assert plan_ingame(a, SETTINGS, now=NOW) == []
    out = plan_ingame(a, dict(SETTINGS, ingame_min_edge=0.04), now=NOW)
    assert [p["side"] for p in out] == ["home"] and out[0]["edge"] == pytest.approx(0.048)


def test_max_bet_caps() -> None:
    """Stake = min(Kelly target, available, ingame_max_bet_cents, max_bet_cents); size = stake // 61.2."""
    assert plan_ingame(_assignment(), dict(SETTINGS, ingame_max_bet_cents=300), now=NOW)[0]["size"] == 4
    assert plan_ingame(_assignment(max_bet_cents=200), SETTINGS, now=NOW)[0]["size"] == 3
    assert plan_ingame(_assignment(), dict(SETTINGS, max_bet_cents=130), now=NOW)[0]["size"] == 2
    small = _assignment(bankroll={"available_cents": 2000, "reserved_cents": 0, "open_cost_cents": 0})
    # Kelly 0.25 * 2000 * 0.088 / 0.388 = 113.4 -> 113 cents, under the 500 cap: size 1.
    assert plan_ingame(small, SETTINGS, now=NOW)[0]["stake_cents"] == 113
    held = _assignment(positions=[{"market_id": "m-home", "side": "home", "size": 900, "basis_cents": 5700}])
    assert plan_ingame(held, SETTINGS, now=NOW) == [], "the target position (5670 cents) is already held"


def test_dead_zone() -> None:
    """Fee-free: p 0.615 vs ask 0.60 is an edge of 0.015 >= 0.01, but |0.615 - mid 0.59| = 0.025 < 0.03."""
    cfg = dict(SETTINGS, fee_model={"taker_rate": 0.0}, ingame_min_edge=0.01)
    a = _assignment(_ingame(p=0.615))
    assert plan_ingame(a, cfg, now=NOW) == []
    out = plan_ingame(a, dict(cfg, ingame_dead_zone=0.02), now=NOW)
    assert [p["side"] for p in out] == ["home"] and out[0]["edge"] == pytest.approx(0.015)


def test_freshness() -> None:
    assert plan_ingame(_assignment(_ingame(age_s=30)), SETTINGS, now=NOW), "30 s old is still fresh"
    assert ingame_skip(_assignment(_ingame(age_s=30.5)), SETTINGS, NOW) == "stale"
    assert plan_ingame(_assignment(_ingame(age_s=30.5)), SETTINGS, now=NOW) == []
    gone = _ingame()
    gone["game_state"]["age_s"] = None
    assert ingame_skip(_assignment(gone), SETTINGS, NOW) == "stale"
    gone["game_state"] = None
    assert ingame_skip(_assignment(gone), SETTINGS, NOW) == "no_state" and plan_ingame(_assignment(gone), SETTINGS, now=NOW) == []
    for status in ("pre", "half", "end_period", "final"):
        assert ingame_skip(_assignment(_ingame(status=status)), SETTINGS, NOW) == "not_in_play"


def test_quiet_period() -> None:
    recent = _ingame(last_change={"kind": "score", "ts": _iso(NOW - 10)})
    assert ingame_skip(_assignment(recent), SETTINGS, NOW) == "quiet" and plan_ingame(_assignment(recent), SETTINGS, now=NOW) == []
    edge = _ingame(last_change={"kind": "possession", "ts": _iso(NOW - 19.5)})
    assert ingame_skip(_assignment(edge), SETTINGS, NOW) == "quiet"
    old = _ingame(last_change={"kind": "possession", "ts": _iso(NOW - 20)})
    assert ingame_skip(_assignment(old), SETTINGS, NOW) is None and plan_ingame(_assignment(old), SETTINGS, now=NOW)
    assert ingame_skip(_assignment(recent), dict(SETTINGS, ingame_quiet_seconds=5), NOW) is None
    bad = _ingame(last_change={"kind": "score", "ts": "garbage"})
    assert ingame_skip(_assignment(bad), SETTINGS, NOW) == "quiet", "an unreadable change time is treated as recent"


def test_cutoff_regulation_and_overtime() -> None:
    assert seconds_left(_state(period=1, clock_seconds=900)) == 3600
    assert seconds_left(_state(period=3, clock_seconds=0)) == 900
    assert seconds_left(_state(period=4, clock_seconds=121)) == 121
    assert seconds_left(_state(period=5, clock_seconds=600)) == 600, "overtime uses its own clock"
    assert seconds_left(_state(period=6, clock_seconds=30)) == 30
    assert seconds_left(_state(period=None)) is None and seconds_left(_state(clock_seconds=None)) is None
    assert ingame_skip(_assignment(_ingame(period=4, clock_seconds=121)), SETTINGS, NOW) is None
    assert ingame_skip(_assignment(_ingame(period=4, clock_seconds=120)), SETTINGS, NOW) == "cutoff"
    assert ingame_skip(_assignment(_ingame(period=3, clock_seconds=0)), SETTINGS, NOW) is None
    assert ingame_skip(_assignment(_ingame(period=5, clock_seconds=600)), SETTINGS, NOW) is None
    assert ingame_skip(_assignment(_ingame(period=5, clock_seconds=121)), SETTINGS, NOW) is None
    assert ingame_skip(_assignment(_ingame(period=5, clock_seconds=120)), SETTINGS, NOW) == "cutoff"
    assert plan_ingame(_assignment(_ingame(period=5, clock_seconds=100)), SETTINGS, now=NOW) == []
    assert ingame_skip(_assignment(_ingame(period=3, clock_seconds=0)), dict(SETTINGS, ingame_cutoff_seconds=900), NOW) == "cutoff"
    assert ingame_skip(_assignment(_ingame(clock_seconds=None)), SETTINGS, NOW) == "cutoff"


def test_lag_suspension_blocks_buys_not_sells() -> None:
    """p_home 0.40 with 10 home contracts held. Sell home at bid 0.58: fee 0.01218, edge
    0.58 - 0.01218 - 0.40 = 0.1678. Buy away at 0.42: cost 0.43218, edge 0.1678."""
    held = [{"market_id": "m-home", "side": "home", "size": 10, "basis_cents": 450}]
    out = plan_ingame(_assignment(_ingame(p=0.40), positions=held), SETTINGS, now=NOW)
    assert [(p["side"], p.get("order_side", "buy")) for p in out] == [("away", "buy"), ("home", "sell")]
    sell = out[1]
    assert sell["price"] == 0.58 and sell["size"] == 10 and sell["edge"] == pytest.approx(0.1678, abs=1e-4)
    assert sell["ingame"] is True and sell["gtd_seconds"] == 60
    assert sell["client_request_id"] == ingame_request_id("a1", "m-home", 41, 0.58, 10, "sell")
    out = plan_ingame(_assignment(_ingame(p=0.40, suspended=True), positions=held), SETTINGS, now=NOW)
    assert [(p["side"], p.get("order_side", "buy")) for p in out] == [("home", "sell")]
    assert plan_ingame(_assignment(_ingame(suspended=True)), SETTINGS, now=NOW) == [], "no buys while suspended"


def _sells(out: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return [(p["side"], p["size"]) for p in out if p.get("order_side") == "sell"]


def test_sell_uses_ingame_min_edge_and_no_shorting() -> None:
    """p_home 0.50: home sell edge 0.58 - 0.01218 - 0.50 = 0.0678 (and the away buy at 0.42
    has edge 0.50 - 0.43218 = 0.0678); p 0.52: both 0.0478 < 0.05, so nothing at all."""
    held = [{"market_id": "m-home", "side": "home", "size": 3, "basis_cents": 150}]
    out = plan_ingame(_assignment(_ingame(p=0.50), positions=held), SETTINGS, now=NOW)
    assert _sells(out) == [("home", 3)] and out[-1]["edge"] == pytest.approx(0.0678, abs=1e-4)
    assert plan_ingame(_assignment(_ingame(p=0.52), positions=held), SETTINGS, now=NOW) == []
    assert _sells(plan_ingame(_assignment(_ingame(p=0.50)), SETTINGS, now=NOW)) == [], "nothing held, nothing sold"
    open_sell = [{"id": "s1", "market_id": "m-home", "side": "sell", "price": 0.58, "size": 3, "filled_size": 0, "status": "open"}]
    assert _sells(plan_ingame(_assignment(_ingame(p=0.50), positions=held, open_orders=open_sell), SETTINGS, now=NOW)) == []


def test_gates_before_the_rules() -> None:
    assert ingame_skip(_assignment(), SETTINGS, NOW) is None
    assert ingame_skip(_assignment(_ingame(enabled=False)), SETTINGS, NOW) == "disabled"
    assert ingame_skip(_assignment(status="halted"), SETTINGS, NOW) == "inactive"
    assert ingame_skip(_assignment(), SETTINGS, BEFORE) == "pregame"
    assert plan_ingame(_assignment(), SETTINGS, now=BEFORE) == []
    no_p = _ingame()
    no_p["pregame_p_home"] = None
    assert ingame_skip(_assignment(no_p), SETTINGS, NOW) == "no_pregame_p"
    wrong = _ingame()
    wrong["model"] = {"id": "pre-1", "family": "elo_blend", "params": {}, "artifact": {}}
    assert ingame_skip(_assignment(wrong), SETTINGS, NOW) == "no_model" and plan_ingame(_assignment(wrong), SETTINGS, now=NOW) == []
    gtd = plan_ingame(_assignment(), dict(SETTINGS, ingame_gtd_seconds=45), now=NOW)[0]
    assert gtd["gtd_seconds"] == 45


def test_settings_defaults() -> None:
    cfg = ingame_settings({"min_edge": 0.02, "ingame_min_edge": "0.07", "ingame_dead_zone": "x"})
    assert cfg["min_edge"] == 0.02 and cfg["ingame_min_edge"] == 0.07 and cfg["ingame_dead_zone"] == 0.03
    assert cfg["ingame_max_state_age_s"] == 30 and cfg["ingame_cutoff_seconds"] == 120 and cfg["ingame_gtd_seconds"] == 60
    assert ingame_settings(None)["ingame_max_bet_cents"] == 500


def test_model_loaded_from_payload_and_cached_by_id() -> None:
    cache: dict[str, Any] = {}
    model = load_ingame_model(_model_spec(0.7, "ig-9"), cache)
    assert model is not None and model.predict(_state(), 0.5) == pytest.approx(0.7)
    assert cache["ig-9"] is model
    broken = {"id": "ig-9", "family": "ingame_wp", "params": {}, "artifact": {"coef": [1.0]}}
    assert load_ingame_model(broken, cache) is model, "cached by id: the artifact is not rebuilt"
    assert load_ingame_model(broken, {}) is None, "a bad artifact does not load"
    assert load_ingame_model(_model_spec(0.7) | {"family": "elo_blend"}, {}) is None
    assert load_ingame_model(None, cache) is None


def test_stale_ingame_orders() -> None:
    """p_home 0.55: an open buy on home at ask 0.60 has edge 0.55 - 0.612 < 0; a sell on
    home at bid 0.58 has 0.58 - 0.01218 - 0.55 = 0.0178 >= 0 and stays."""
    orders = [{"id": "b1", "market_id": "m-home", "side": "buy", "price": 0.6, "size": 2, "filled_size": 0, "status": "open"},
              {"id": "s1", "market_id": "m-home", "side": "sell", "price": 0.58, "size": 2, "filled_size": 0, "status": "open"}]
    a = _assignment(_ingame(p=0.55), open_orders=orders)
    assert stale_ingame(a, SETTINGS) == ["b1"]
    assert stale_ingame(_assignment(_ingame(p=0.70), open_orders=orders), SETTINGS) == ["s1"]
    assert stale_ingame(_assignment(_ingame(p=0.55, age_s=40), open_orders=orders), SETTINGS) == [], "no fresh state, no verdict"


# ------------------------------------------------------------------ the loop hook


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


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _tick_state(assignment: dict[str, Any], server_time: str = SERVER_TIME, **extra: Any) -> dict[str, Any]:
    return {"kill": False, "server_time": server_time, "settings": dict(SETTINGS, ingame_tick_s=3),
            "assignments": [assignment], **extra}


def test_tick_posts_ingame_requests_on_their_cadence(monkeypatch) -> None:
    calls = _scripted(monkeypatch)
    loop = TradeLoop(_Agent())
    clock = _Clock()
    loop.ingame.clock = clock
    posted = loop.tick(_tick_state(_assignment()))
    assert [c[0] for c in calls] == ["/api/v1/orders/request"]
    body = calls[0][1]
    assert body["ingame"] is True and body["gtd_seconds"] == 60 and body["client_request_id"].startswith("ingame-")
    assert "side" not in body and "stake_cents" not in body and body["size"] == 8
    assert posted[0]["result"]["status"] == "approved"
    assert loop.last_tick["ingame"] == {"assignments": 1, "proposed": 1}
    assert loop.tick_seconds() == 3.0, "the loop ticks at ingame_tick_s while a game is live"
    assert "ig-0.7" in loop.ingame.models, "the in-game model is cached by id"

    calls.clear()
    clock.t += 1.0
    assert loop.tick(_tick_state(_assignment())) == [] and calls == [], "not due before ingame_tick_s"
    clock.t += 2.0
    assert len(loop.tick(_tick_state(_assignment()))) == 1 and len(calls) == 1


def test_tick_cancels_stale_ingame_orders(monkeypatch) -> None:
    calls = _scripted(monkeypatch)
    loop = TradeLoop(_Agent())
    orders = [{"id": "b1", "market_id": "m-home", "side": "buy", "price": 0.6, "size": 2, "filled_size": 0, "status": "open"}]
    loop.tick(_tick_state(_assignment(_ingame(p=0.55), open_orders=orders)))
    assert [c[0] for c in calls] == ["/api/v1/orders/b1/cancel"] and loop.last_tick["cancelled"] == 1


def test_tick_under_kill_posts_nothing_ingame(monkeypatch) -> None:
    calls = _scripted(monkeypatch)
    loop = TradeLoop(_Agent())
    orders = [{"id": "b1", "market_id": "m-home", "side": "buy", "price": 0.6, "size": 2, "filled_size": 0, "status": "open"}]
    assert loop.tick(_tick_state(_assignment(_ingame(p=0.55), open_orders=orders), kill=True)) == []
    assert calls == [] and loop.last_tick["kill"] is True
    agent = _Agent()
    agent.kill = True
    assert TradeLoop(agent).tick(_tick_state(_assignment())) == [] and calls == []


def test_tick_disabled_ingame_keeps_pregame_rules_after_kickoff(monkeypatch) -> None:
    """Not enabled: nothing in-game, and the pre-game rule (trade_pregame_only) proposes nothing."""
    calls = _scripted(monkeypatch)
    loop = TradeLoop(_Agent())
    assert loop.tick(_tick_state(_assignment(_ingame(enabled=False)))) == [] and calls == []
    assert "ingame" not in loop.last_tick and loop.tick_seconds() == 5.0


def test_pregame_behaviour_unchanged_before_kickoff(monkeypatch) -> None:
    """Before kickoff an enabled in-game block changes nothing: the same pre-game requests."""
    calls = _scripted(monkeypatch)
    before = _iso(BEFORE)
    with_block = TradeLoop(_Agent()).tick(_tick_state(_assignment(), server_time=before))
    first = list(calls)
    calls.clear()
    plain = _assignment()
    del plain["ingame"]
    loop = TradeLoop(_Agent())
    without = loop.tick(_tick_state(plain, server_time=before))
    assert with_block == without and first == calls and len(first) == 1
    assert first[0][1]["client_request_id"] == plan_proposals(plain, SETTINGS, now=BEFORE)[0]["client_request_id"]
    assert "ingame" not in first[0][1] and "ingame" not in loop.last_tick and loop.tick_seconds() == 5.0


def test_runner_cadence_slack() -> None:
    clock = _Clock()
    runner = IngameRunner(clock)
    runner.configure({"ingame_tick_s": 5})
    assert runner.due("a1") and not runner.due("a1")
    clock.t += 4.4
    assert not runner.due("a1")
    clock.t += 0.1
    assert runner.due("a1"), "a tick 4.5 s (90 %) later counts, so agent jitter does not skip a beat"
    assert runner.due("a2"), "each assignment has its own cadence"
    assert math.isclose(runner.tick_seconds(), 5.0)
