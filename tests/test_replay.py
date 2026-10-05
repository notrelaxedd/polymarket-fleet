"""Snapshot replay backtests (docs/ROBUSTNESS.md B1): which games are scored, the
decision-time prices, the depth walk and the touch fallback, the CLV sign, the sim
platform refusal, the price stress on recorded prices, the job and its context fetch,
and closing-line results left unchanged. Fixture based, no database."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from fleet.sim.backtest import records_of, run_backtest, run_seasons
from fleet.sim.data import load_games, outcome_of
from fleet.sim.fills import BetRule, settle
from fleet.sim.odds import devig
from fleet.sim.prices import Replay, SimPricesRefused, p_market_of, parse_ts, plan_replay_bet
from fleet.sim.stress import price_stress, stressed_rule
from fleet.sim.records import replan
from fleet.worker import config, context
from fleet.worker.jobs import DEFAULT_LIMITS, JOBS
from tests.fake_host import FakeHost

FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "games_sample.csv")
PARAMS = {"k": 24, "hfa": 55, "regress": 0.33, "rest_per_day": 1.0, "mov_scale": 1,
          "min_edge": 0.01, "kelly_fraction": 0.25}
LIMITS = dict(DEFAULT_LIMITS)
PLATFORM = "polymarket_us"
RULE = BetRule(0.05, 0.01, 0.03, 0.25, 100000, 100000, participation=0.5)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def decision_of(game: dict[str, Any], minutes: int = 60) -> float:
    kickoff = parse_ts(game["kickoff_at"])
    assert kickoff is not None
    return kickoff - 60.0 * minutes


def market(game: dict[str, Any], side: str, mid: float, offsets: list[float], *, platform: str = PLATFORM,
           confirmed: bool = True, close: float | None = None, liq: int | None = 500000,
           depth: list[Any] | None = None, mids: list[float] | None = None) -> dict[str, Any]:
    """One recorded market: a bar per offset (minutes from the decision time) with the
    given mid (or per-bar mids), a 0.02 spread and the liquidity; depth rows as given."""
    d = decision_of(game)
    bars = []
    for i, offset in enumerate(offsets):
        m = mids[i] if mids else mid
        bars.append([iso(d + 60.0 * offset), round(m - 0.01, 4), round(m + 0.01, 4), round(m, 4), liq])
    return {"market_id": f"{game['game_id']}-{side}-{platform}", "game_id": game["game_id"], "side": side,
            "platform": platform, "confirmed": confirmed, "closing_price": close, "kickoff_at": game["kickoff_at"],
            "bars": bars, "depth": depth or []}


def game_markets(game: dict[str, Any], offsets: list[float], close_shift: float | None = None,
                 **kw: Any) -> list[dict[str, Any]]:
    """Home and away markets whose mids flip the closing line (a market the model
    disagrees with, so there are bets), an overround of 0.01 and closes at mid +
    close_shift when given."""
    p = 1.0 - (devig(game["home_moneyline"], game["away_moneyline"]) or 0.5)
    home, away = round(p + 0.005, 4), round(1.0 - p + 0.005, 4)
    out = []
    for side, mid in (("home", home), ("away", away)):
        close = None if close_shift is None else round(mid + close_shift, 4)
        out.append(market(game, side, mid, offsets, close=close, **kw))
    return out


@pytest.fixture(scope="module")
def games() -> list[dict]:
    return load_games(FIXTURE)


def played(games: list[dict], season: int) -> list[dict]:
    return [g for g in games if g["season"] == season and outcome_of(g) is not None]


@pytest.fixture(scope="module")
def layout(games: list[dict]) -> dict[str, Any]:
    """Season 2024 markets: 20 games with a bar 10 min before the decision, 5 with one
    exactly 30 min before, 5 only 31 to 45 min before, 5 only after the decision, 5
    unconfirmed and 3 on platform sim."""
    g = played(games, 2024)
    markets: list[dict[str, Any]] = []
    for game in g[0:20]:
        markets += game_markets(game, [-10])
    for game in g[20:25]:
        markets += game_markets(game, [-30])
    for game in g[25:30]:
        markets += game_markets(game, [-45, -31])
    for game in g[30:35]:
        markets += game_markets(game, [1, 5])
    for game in g[35:40]:
        markets += game_markets(game, [-10], confirmed=False)
    for game in g[40:43]:
        markets += game_markets(game, [-10], platform="sim")
    return {"markets": markets, "scored": [x["game_id"] for x in g[0:25]], "sim": [x["game_id"] for x in g[40:43]],
            "played": len(g)}


def _run(games: list[dict], replay: Replay, seasons: list[int] | None = None, limits: dict | None = None) -> dict:
    return run_backtest(games, "elo_blend", PARAMS, seasons or [2023, 2024], limits or LIMITS,
                        lambda cp, p: None, lambda: False, replay=replay)


def _records(games: list[dict], replay: Replay, seasons: list[int], limits: dict | None = None) -> list[dict]:
    per_season = run_seasons(games, "elo_blend", PARAMS, seasons, limits or LIMITS, lambda cp, p: None,
                             lambda: False, replay=replay)
    return [r for season in records_of(games, "elo_blend", PARAMS, per_season, limits or LIMITS) for r in season]


# which games are scored ------------------------------------------------------------


def test_replay_scores_only_games_with_bars_near_the_decision_time(games: list[dict], layout: dict) -> None:
    replay = Replay(layout["markets"], PLATFORM)
    result = _run(games, replay)
    assert result["seasons"] == [2024], "2023 has no recorded market, so it is not replayed"
    assert result["n_games"] == 25
    assert result["n_unscored_no_prices"] == layout["played"] - 25
    assert result["per_season"][0]["n_unscored_no_prices"] == layout["played"] - 25
    assert result["price_source"] == "snapshots" and result["platform"] == PLATFORM
    records = _records(games, replay, [2024, 2024])
    assert [r["game_id"] for r in records] == layout["scored"]
    assert result["n_bets"] > 0 and result["n_bets"] == sum(1 for r in records if r["bet"])


def test_sim_prices_are_excluded_unless_allowed(games: list[dict], layout: dict) -> None:
    with pytest.raises(SimPricesRefused):
        Replay(layout["markets"], "sim")
    with pytest.raises(SimPricesRefused):
        Replay(layout["markets"], "sim", allow_sim=False)
    assert not any(Replay(layout["markets"], PLATFORM).has_game(gid) for gid in layout["sim"])
    allowed = Replay(layout["markets"], "sim", allow_sim=True)
    records = _records(games, allowed, [2024, 2024])
    assert [r["game_id"] for r in records] == layout["sim"]
    assert _run(games, allowed)["platform"] == "sim"


def test_p_market_is_the_devigged_mid_of_the_last_bar_at_or_before_the_decision(games: list[dict]) -> None:
    game = played(games, 2024)[0]
    home = market(game, "home", 0.0, [-20, -5, 5], mids=[0.40, 0.60, 0.90])
    away = market(game, "away", 0.0, [-20, -5, 5], mids=[0.62, 0.44, 0.12])
    facts = Replay([home, away], PLATFORM).facts(game)
    assert facts is not None
    assert facts["sides"]["home"]["mid"] == pytest.approx(0.60) and facts["sides"]["away"]["mid"] == pytest.approx(0.44)
    assert facts["sides"]["home"]["ask"] == pytest.approx(0.61)
    assert p_market_of(facts) == pytest.approx(0.60 / 1.04)
    only_home = Replay([home], PLATFORM).facts(game)
    assert only_home is not None and p_market_of(only_home) == pytest.approx(0.60)
    only_away = Replay([away], PLATFORM).facts(game)
    assert only_away is not None and p_market_of(only_away) == pytest.approx(0.56)
    later = Replay([home, away], PLATFORM, decision_minutes=50).facts(game)
    assert later is not None and later["sides"]["home"]["mid"] == pytest.approx(0.90)


def test_closing_price_falls_back_to_the_last_bar_before_kickoff(games: list[dict]) -> None:
    game = played(games, 2024)[0]
    frozen = market(game, "home", 0.5, [-10, 30], close=0.57)
    loose = market(game, "home", 0.0, [-10, 30], mids=[0.5, 0.66])
    assert Replay([frozen], PLATFORM).facts(game)["sides"]["home"]["close"] == pytest.approx(0.57)
    assert Replay([loose], PLATFORM).facts(game)["sides"]["home"]["close"] == pytest.approx(0.66)


# the fill -----------------------------------------------------------------------------


def _facts(home_levels: Any = None, home_close: float = 0.6, away_close: float = 0.4, liq: int | None = 1000) -> dict:
    return {"game_id": "g", "sides": {
        "home": {"market_id": "h", "mid": 0.49, "ask": 0.50, "liq": liq, "levels": home_levels, "close": home_close},
        "away": {"market_id": "a", "mid": 0.51, "ask": 0.52, "liq": liq, "levels": None, "close": away_close},
    }}


def test_depth_walk_takes_participation_of_each_level_at_or_below_the_ask() -> None:
    bet = plan_replay_bet(0.7, _facts([[0.48, 4], [0.50, 10], [0.52, 100]]), RULE)
    assert bet is not None and bet["side"] == "home" and bet["fill"] == "depth" and bet["market_id"] == "h"
    assert bet["contracts"] == 7  # floor(0.5 * 4) at 0.48, floor(0.5 * 10) at 0.50, 0.52 is above the ask
    assert bet["price"] == pytest.approx((2 * 0.48 + 5 * 0.50) / 7)
    fee = 0.05 * (2 * 0.48 * 0.52 + 5 * 0.5 * 0.5)
    assert bet["fee"] == pytest.approx(fee / 7)
    assert bet["stake_cents"] == 355  # round((3.46 + 0.08746) * 100)
    assert bet["edge"] == pytest.approx(0.7 - (3.46 + fee) / 7)
    assert settle(bet, 1.0) == (700, 345) and settle(bet, 0.0) == (0, -355)


def test_touch_fallback_caps_by_the_bar_liquidity_and_depth_above_the_ask_fills_nothing() -> None:
    bet = plan_replay_bet(0.7, _facts(None, liq=1000), RULE)
    assert bet is not None and bet["fill"] == "touch"
    assert bet["contracts"] == 10  # floor(0.5 * 1000 / (100 * 0.50))
    assert bet["price"] == pytest.approx(0.50)
    assert plan_replay_bet(0.7, _facts(None, liq=None), RULE) is None, "unknown liquidity never fills"
    assert plan_replay_bet(0.7, _facts([[0.53, 100]]), RULE) is None, "a book above the ask fills nothing"
    assert plan_replay_bet(0.7, _facts([]), RULE) is None, "an empty recorded book fills nothing"
    small = plan_replay_bet(0.7, _facts(None, liq=100000), BetRule(0.05, 0.01, 0.03, 0.25, 100000, 2500))
    assert small is not None and small["contracts"] == 48  # Kelly 9615 cents capped by max_bet: floor(2500 / 51.25)


def test_depth_counts_only_within_two_minutes_before_the_decision(games: list[dict]) -> None:
    game = played(games, 2024)[0]
    d = decision_of(game)
    levels = [[0.5, 40]]

    def facts_with(offset_s: float) -> dict:
        row = market(game, "home", 0.49, [-5], depth=[[iso(d + offset_s), [[0.48, 40]], levels]])
        out = Replay([row], PLATFORM).facts(game)
        assert out is not None
        return out["sides"]["home"]

    assert facts_with(-60)["levels"] == levels
    assert facts_with(-120)["levels"] == levels
    assert facts_with(-180)["levels"] is None
    assert facts_with(30)["levels"] is None, "a book after the decision is never used"


def test_clv_is_the_close_minus_the_entry_on_the_bought_side() -> None:
    rising = plan_replay_bet(0.7, _facts(None, home_close=0.60), RULE)
    falling = plan_replay_bet(0.7, _facts(None, home_close=0.42), RULE)
    assert rising is not None and rising["clv"] == pytest.approx(0.10)
    assert falling is not None and falling["clv"] == pytest.approx(-0.08)
    away = plan_replay_bet(0.3, _facts(None, home_close=0.9, away_close=0.60), RULE)
    assert away is not None and away["side"] == "away" and away["market_id"] == "a"
    assert away["clv"] == pytest.approx(0.60 - 0.52), "the bought side's close, not the home market's"


def test_backtest_clv_follows_the_recorded_close(games: list[dict]) -> None:
    g = played(games, 2024)[:100]
    for shift, sign in ((0.05, 1.0), (-0.05, -1.0)):
        markets = [m for game in g for m in game_markets(game, [-10], close_shift=shift)]
        replay = Replay(markets, PLATFORM)
        records = _records(games, replay, [2024, 2024])
        bets = [r["bet"] for r in records if r["bet"]]
        assert len(bets) > 10
        for bet in bets:
            assert bet["clv"] == pytest.approx(shift - 0.01)  # close = mid + shift, entry = ask = mid + 0.01
        result = _run(games, replay)
        lo, hi = result["ci"]["avg_clv"]
        assert sign * lo > 0 and sign * hi > 0
        assert lo == pytest.approx(shift - 0.01) and hi == pytest.approx(shift - 0.01)


# price stress on recorded prices ------------------------------------------------------


def test_price_stress_raises_the_entry_on_recorded_prices(games: list[dict]) -> None:
    replay = Replay([m for game in played(games, 2024) for m in game_markets(game, [-10], close_shift=0.0)], PLATFORM)
    records = _records(games, replay, [2024, 2024])
    rows = price_stress(records, PARAMS, LIMITS)
    base_bets = sum(1 for r in records if r["bet"])
    assert rows[1]["n_bets"] <= rows[0]["n_bets"] <= base_bets
    rule = stressed_rule(PARAMS, LIMITS, {"half_spread": 0.02})
    assert rule.price_bump == pytest.approx(0.02)
    moved = 0
    for r in records:
        stressed = replan(r, rule)
        if r["bet"] and stressed["bet"] and stressed["bet"]["side"] == r["bet"]["side"]:
            assert stressed["bet"]["price"] == pytest.approx(r["bet"]["price"] + 0.02)
            assert stressed["bet"]["clv"] == pytest.approx(r["bet"]["clv"] - 0.02)
            moved += 1
    assert moved > 0
    fee = stressed_rule(PARAMS, LIMITS, {"taker_rate": 1.5})
    for r in records:
        stressed = replan(r, fee)
        if r["bet"] and stressed["bet"] and stressed["bet"]["side"] == r["bet"]["side"]:
            assert stressed["bet"]["fee"] > r["bet"]["fee"] and stressed["bet"]["price"] == pytest.approx(r["bet"]["price"])


# the job and its context ----------------------------------------------------------------


def _write_prices(tmp_path: Path, markets: list[dict]) -> str:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"markets": markets, "count": len(markets)}))
    return str(path)


def test_backtest_job_replays_snapshots_and_refuses_sim(games: list[dict], layout: dict, tmp_path: Path) -> None:
    prices = _write_prices(tmp_path, layout["markets"])
    base = {"family": "elo_blend", "params": PARAMS, "seasons": [2023, 2024], "price_source": "snapshots",
            "price_platform": PLATFORM, "decision_minutes_before_kickoff": 60, "allow_sim_prices": False,
            "participation": 0.5, "_context": {"games_path": FIXTURE, "prices_path": prices}}
    result = JOBS["backtest"](dict(base), None, lambda cp, p: None, lambda: False)
    assert result["n_games"] == 25 and result["price_source"] == "snapshots" and result["platform"] == PLATFORM
    with pytest.raises(SimPricesRefused):
        JOBS["backtest"](dict(base, price_platform="sim"), None, lambda cp, p: None, lambda: False)
    sim = JOBS["backtest"](dict(base, price_platform="sim", allow_sim_prices=True), None, lambda cp, p: None, lambda: False)
    assert sim["n_games"] == 3 and sim["platform"] == "sim"
    with pytest.raises(ValueError):
        JOBS["backtest"](dict(base, price_source="bogus"), None, lambda cp, p: None, lambda: False)
    with pytest.raises(ValueError):
        JOBS["backtest"](dict(base, _context={"games_path": FIXTURE}), None, lambda cp, p: None, lambda: False)


def test_closing_line_results_are_unchanged(games: list[dict]) -> None:
    params = {"family": "elo_blend", "params": PARAMS, "seasons": [2021, 2024], "_context": {"games_path": FIXTURE}}
    plain = JOBS["backtest"](dict(params), None, lambda cp, p: None, lambda: False)
    named = JOBS["backtest"](dict(params, price_source="closing_line"), None, lambda cp, p: None, lambda: False)
    direct = run_backtest(games, "elo_blend", PARAMS, [2021, 2024], dict(DEFAULT_LIMITS), lambda cp, p: None, lambda: False)
    assert json.dumps(plain, sort_keys=True) == json.dumps(named, sort_keys=True) == json.dumps(direct, sort_keys=True)
    assert not {"price_source", "platform", "n_unscored_no_prices"} & set(plain)
    assert all(r["bet"] is None or r["bet"]["clv"] == 0.0
               for season in records_of(games, "elo_blend", PARAMS, run_seasons(
                   games, "elo_blend", PARAMS, [2024, 2024], LIMITS, lambda cp, p: None, lambda: False), LIMITS)
               for r in season)


@pytest.fixture
def fake_host() -> Any:
    host = FakeHost(lease_seconds=30.0, heartbeat_seconds=0.2).start()
    try:
        yield host
    finally:
        host.stop()


def test_context_fetches_and_caches_the_prices_of_a_snapshot_backtest(fake_host: FakeHost, layout: dict,
                                                                     tmp_path: Path) -> None:
    token = fake_host.register({"enroll_token": fake_host.mint_enroll_token(), "name": "t"})["worker_token"]
    fake_host.set_prices(layout["markets"])
    state = str(tmp_path / "state")
    os.makedirs(state)
    job = {"kind": "backtest", "params": {"family": "elo_blend", "price_source": "snapshots", "price_platform": PLATFORM}}

    def build(j: dict) -> dict:
        return context.build_context(fake_host.url, token, state, j, timeout=2.0, data_timeout=5.0)

    fake_host.set_games([])
    ctx = build(job)
    assert ctx["prices_path"] == config.prices_cache_path(state, PLATFORM)
    with open(ctx["prices_path"], encoding="utf-8") as fh:
        cached = json.load(fh)
    platforms = {m["platform"] for m in cached["markets"]}
    assert platforms == {PLATFORM} and all(m["confirmed"] for m in cached["markets"])
    mtime = os.stat(ctx["prices_path"]).st_mtime_ns
    assert build(job)["prices_path"] == ctx["prices_path"] and os.stat(ctx["prices_path"]).st_mtime_ns == mtime
    queries = fake_host.prices_queries()
    assert len(queries) == 2 and all("platform=polymarket_us" in q and "since=" in q for q in queries)
    assert "prices_path" not in build({"kind": "backtest", "params": {"family": "elo_blend"}})
    assert "prices_path" not in build(dict(job, kind="validate"))
    assert "prices_path" not in build({"kind": "backtest", "params": dict(job["params"], price_platform="sim")})
    assert len(fake_host.prices_queries()) == 2
    sim = build({"kind": "backtest", "params": dict(job["params"], price_platform="sim", allow_sim_prices=True)})
    with open(sim["prices_path"], encoding="utf-8") as fh:
        assert {m["platform"] for m in json.load(fh)["markets"]} == {"sim"}
