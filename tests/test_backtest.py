"""Fill rule and stake maths, metrics, drawdown, and the walk-forward backtest's
determinism, resume equivalence and per-unit timing. Fixture based, no database."""
from __future__ import annotations

import copy
import json
import math
import random
import time
from pathlib import Path

import pytest

from fleet.sim.backtest import run_backtest, run_fold, season_plan
from fleet.sim.records import unpack_probs
from fleet.sim.control import JobStopped
from fleet.sim.data import load_games
from fleet.sim.fills import BetRule, plan_bet, settle, side_cost, stake_cents
from fleet.sim.metrics import empty_stats, merge_stats, metrics_from_stats, record_game
from fleet.worker.jobs import DEFAULT_LIMITS

FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "games_sample.csv")
PARAMS = {"k": 24, "hfa": 55, "regress": 0.33, "rest_per_day": 1.0, "mov_scale": 1,
          "min_edge": 0.03, "kelly_fraction": 0.25}
LIMITS = dict(DEFAULT_LIMITS)
RULE = BetRule(taker_rate=0.05, half_spread=0.01, min_edge=0.03, kelly_fraction=0.25,
               bankroll_cents=10000, max_bet_cents=2500)


@pytest.fixture(scope="module")
def games() -> list[dict]:
    return load_games(FIXTURE)


# fills ----------------------------------------------------------------------


def test_fill_rule_hand_computed() -> None:
    price, fee, cost = side_cost(0.5, RULE)
    assert (price, fee, cost) == pytest.approx((0.51, 0.012495, 0.522495))
    bet = plan_bet(0.6, 0.5, RULE)
    assert bet is not None and bet["side"] == "home"
    assert bet["edge"] == pytest.approx(0.6 - 0.522495)
    assert bet["stake_cents"] == 405  # floor(0.25 * 10000 * 0.077505 / 0.477505)
    assert bet["contracts"] == pytest.approx(405 / 52.2495)
    assert settle(bet, 1.0) == (775, 370)  # round(7.75127 contracts * 100) - 405
    assert settle(bet, 0.0) == (0, -405)
    assert settle(bet, 0.5) == (405, 0)  # a tie returns the stake


def test_fill_rule_picks_the_away_side_and_respects_min_edge() -> None:
    bet = plan_bet(0.3, 0.5, RULE)
    assert bet is not None and bet["side"] == "away" and bet["p_model"] == pytest.approx(0.7)
    assert settle(bet, 0.0)[1] > 0 and settle(bet, 1.0)[1] == -bet["stake_cents"]
    assert plan_bet(0.55, 0.5, RULE) is None  # edge 0.0275 < min_edge 0.03
    assert plan_bet(0.5525, 0.5, RULE) is not None  # edge 0.030005


def test_stake_caps() -> None:
    big = BetRule(0.05, 0.01, 0.03, 0.5, 100000, 2500)
    bet = plan_bet(0.9, 0.5, big)
    assert bet is not None and bet["stake_cents"] == 2500  # raw 39527 capped at max_bet
    assert stake_cents(1.5, 0.2, BetRule(0.05, 0.01, 0.03, 1.0, 1000, 10 ** 9)) == 1000  # bankroll cap
    assert stake_cents(0.0, 0.5, RULE) == 0 and stake_cents(0.2, 1.0, RULE) == 0
    assert plan_bet(0.9, 0.5, BetRule(0.05, 0.01, 0.03, 0.25, 10000, 0)) is None  # max_bet 0: no bets


def test_bet_rule_from_params_and_limits() -> None:
    rule = BetRule.build({"min_edge": 0.05, "kelly_fraction": 0.4}, {"fee_model": {"taker_rate": 0.02}})
    assert rule == BetRule(0.02, 0.01, 0.05, 0.4, 10000, 2500)


# metrics ----------------------------------------------------------------------


def test_metrics_on_a_tiny_season() -> None:
    stats = empty_stats()
    bets = [{"stake_cents": 100, "edge": 0.05}, None, {"stake_cents": 50, "edge": 0.04}, {"stake_cents": 200, "edge": 0.06}]
    rows = [(0.7, 0.6, 1.0, 80), (0.3, 0.4, 0.0, 0), (0.5, 0.5, 0.5, 0), (0.8, 0.7, 0.0, -200)]
    for (p, pm, y, pnl), bet in zip(rows, bets):
        record_game(stats, p, pm, y, bet, pnl)
    m = metrics_from_stats(stats, LIMITS, [2020])
    assert m["n_games"] == 4 and m["n_bets"] == 3 and m["total_stake_cents"] == 350 and m["pnl_cents"] == -120
    assert m["roi"] == pytest.approx(-120 / 350) and m["hit_rate"] == pytest.approx(1 / 3)
    assert m["avg_edge"] == pytest.approx(0.05) and m["avg_stake_cents"] == pytest.approx(350 / 3)
    ll = (-math.log(0.7) - math.log(0.7) - math.log(0.5) - math.log(0.2)) / 4
    assert m["log_loss"] == pytest.approx(ll)
    mll = (-math.log(0.6) - math.log(0.6) - math.log(0.5) - math.log(0.3)) / 4
    assert m["market_log_loss"] == pytest.approx(mll)
    assert m["brier"] == pytest.approx((0.09 + 0.09 + 0.0 + 0.64) / 4)
    assert m["max_drawdown_cents"] == 200 and m["max_drawdown"] == pytest.approx(200 / (10000 * 6))
    assert m["seasons"] == [2020]
    cal = m["calibration"]
    assert len(cal) == 10 and [b["count"] for b in cal] == [0, 0, 0, 1, 0, 1, 0, 1, 1, 0]
    assert cal[7] == {"count": 1, "mean_p": 0.7, "mean_outcome": 1.0}
    empty = metrics_from_stats(empty_stats(), LIMITS, [])
    assert empty["roi"] == 0.0 and empty["log_loss"] == 0.0 and empty["n_bets"] == 0


def test_drawdown_definition() -> None:
    stats = empty_stats()
    for pnl in (100, -50, -80, 200, -300):
        record_game(stats, 0.5, 0.5, 1.0, {"stake_cents": 100, "edge": 0.05}, pnl)
    assert stats["max_drawdown_cents"] == 300  # peak 170 to trough -130
    first = empty_stats()
    record_game(first, 0.5, 0.5, 1.0, {"stake_cents": 100, "edge": 0.05}, -100)
    assert first["max_drawdown_cents"] == 100  # the start counts as a peak of 0


def test_merged_stats_equal_one_run_over_the_concatenation() -> None:
    rng = random.Random(5)
    for _ in range(30):
        pnls = [rng.randint(-300, 300) for _ in range(rng.randint(1, 40))]
        cuts = sorted(rng.sample(range(1, len(pnls)), min(3, len(pnls) - 1))) if len(pnls) > 1 else []
        parts, start = [], 0
        for cut in cuts + [len(pnls)]:
            s = empty_stats()
            for pnl in pnls[start:cut]:
                record_game(s, 0.6, 0.5, 1.0, {"stake_cents": 100, "edge": 0.05}, pnl)
            parts.append(s)
            start = cut
        whole = empty_stats()
        for pnl in pnls:
            record_game(whole, 0.6, 0.5, 1.0, {"stake_cents": 100, "edge": 0.05}, pnl)
        merged = merge_stats(parts)
        for key in ("n_games", "n_bets", "hits", "total_stake_cents", "pnl_cents", "max_prefix", "min_prefix",
                    "max_drawdown_cents"):
            assert merged[key] == whole[key], key
        assert [b[0] for b in merged["calibration"]] == [b[0] for b in whole["calibration"]]
        for mb, wb in zip(merged["calibration"], whole["calibration"]):
            assert mb[1:] == pytest.approx(wb[1:])
        for key in ("sum_edge", "sum_brier", "sum_log_loss", "sum_market_log_loss"):
            assert merged[key] == pytest.approx(whole[key])


# backtest ----------------------------------------------------------------------


def test_test_seasons_need_three_seasons_of_history(games: list[dict]) -> None:
    assert season_plan(games, [2010, None]) == [2019, 2020, 2021, 2022, 2023, 2024, 2025]
    assert season_plan(games, [2020, 2021]) == [2020, 2021]
    assert season_plan(games, [2026, None]) == []
    unfinished = copy.deepcopy(games)
    for g in unfinished:
        if g["season"] == 2025 and g["week"] == 22:
            g["home_score"] = g["away_score"] = None
    assert season_plan(unfinished, [2010, None])[-1] == 2024


def test_backtest_is_deterministic(games: list[dict]) -> None:
    runs = [run_backtest(games, "elo_blend", PARAMS, [2010, None], LIMITS, lambda cp, p: None, lambda: False)
            for _ in range(2)]
    assert runs[0] == runs[1]
    result = runs[0]
    assert result["seasons"] == [s["season"] for s in result["per_season"]] == list(range(2019, 2026))
    assert result["n_games"] == sum(s["n_games"] for s in result["per_season"]) == 1960
    assert result["pnl_cents"] == sum(s["pnl_cents"] for s in result["per_season"])
    assert 0.5 < result["log_loss"] < 0.75 and 0.5 < result["market_log_loss"] < 0.75
    assert set(result["blend"]) == {"a", "b", "c"}


def test_backtest_resumes_from_a_checkpoint(games: list[dict]) -> None:
    full = run_backtest(games, "elo_blend", PARAMS, [2010, None], LIMITS, lambda cp, p: None, lambda: False)
    emitted: list[tuple[dict, float]] = []

    def emit(cp: dict, progress: float) -> None:
        emitted.append((copy.deepcopy(cp), progress))

    with pytest.raises(JobStopped):
        run_backtest(games, "elo_blend", PARAMS, [2010, None], LIMITS, emit, lambda: len(emitted) >= 3)
    checkpoint, progress = emitted[-1]
    assert checkpoint["next"] == 3 and len(checkpoint["per_season"]) == 3 and progress == pytest.approx(3 / 7)
    resumed = run_backtest(games, "elo_blend", PARAMS, [2010, None], LIMITS, emit, lambda: False, checkpoint)
    assert resumed == full
    assert [p for _, p in emitted[3:]] == pytest.approx([4 / 7, 5 / 7, 6 / 7, 1.0])
    # a checkpoint from another season plan is ignored, not trusted
    foreign = {"next": 1, "per_season": [{"season": 2017, "stats": empty_stats(), "blend": {}}]}
    assert run_backtest(games, "elo_blend", PARAMS, [2010, None], LIMITS, lambda cp, p: None, lambda: False, foreign) == full


def test_backtest_stops_before_the_first_unit(games: list[dict]) -> None:
    with pytest.raises(JobStopped):
        run_backtest(games, "elo_blend", PARAMS, [2010, None], LIMITS, lambda cp, p: None, lambda: True)
    with pytest.raises(ValueError):
        run_backtest(games, "nope", PARAMS, [2010, None], LIMITS, lambda cp, p: None, lambda: False)


def test_games_without_moneylines_update_elo_but_are_not_scored(games: list[dict]) -> None:
    variant = copy.deepcopy(games)
    stripped = [g for g in variant if g["season"] == 2021][:20]
    for g in stripped:
        g["home_moneyline"] = g["away_moneyline"] = None
    records, _ = run_fold(variant, "elo_blend", PARAMS, 2021, LIMITS, lambda: False)
    ids = {r["game_id"] for r in records}
    assert not ids & {g["game_id"] for g in stripped}
    base, _ = run_fold(games, "elo_blend", PARAMS, 2021, LIMITS, lambda: False)
    assert len(base) == len(records) + 20


def test_one_candidate_one_season_is_fast(games: list[dict]) -> None:
    start = time.perf_counter()
    run_fold(games, "elo_blend", PARAMS, 2025, LIMITS, lambda: False)
    assert time.perf_counter() - start < 1.5


def test_max_drawdown_is_null_without_capital() -> None:
    """MEDIUM: trade_max_games 0 (trading paused) or a 0 bankroll must not report a 0%
    drawdown that passes the eligibility gate; the metric is null instead."""
    stats = empty_stats()
    record_game(stats, 0.7, 0.6, 0.0, {"stake_cents": 300, "edge": 0.05}, -300)
    assert metrics_from_stats(stats, LIMITS, [2020])["max_drawdown"] == pytest.approx(300 / 60000)
    for limits in (dict(LIMITS, trade_max_games=0), dict(LIMITS, default_bankroll_cents=0)):
        m = metrics_from_stats(stats, limits, [2020])
        assert m["max_drawdown_cents"] == 300 and m["max_drawdown"] is None
    from host.eligibility import meets_thresholds

    assert not meets_thresholds({"n_bets": 500, "roi": 0.5, "max_drawdown": None}, {"min_bets": 1, "min_roi": 0.0, "max_drawdown": 0.3})


# step 6: records, era labels and the resampling fields ------------------------------


def test_records_carry_the_regime_features_and_log_losses(games: list[dict]) -> None:
    records, _ = run_fold(games, "elo_blend", PARAMS, 2022, LIMITS, lambda: False)
    keys = {"game_id", "season", "p", "p_market", "outcome", "bet", "pnl_cents", "ll_model", "ll_market", "lean",
            "div_game", "hour_et", "temp", "wind", "outdoor"}
    assert all(set(r) == keys for r in records) and all(r["season"] == 2022 for r in records)
    for r in records:
        assert r["ll_model"] == pytest.approx(-(r["outcome"] * math.log(r["p"]) + (1 - r["outcome"]) * math.log(1 - r["p"])))
        assert r["lean"] in ("home", "away") and 0 <= r["hour_et"] <= 23 and r["div_game"] in (0, 1)
        if r["bet"] is not None:
            assert r["bet"]["side"] == r["lean"] and r["bet"]["clv"] == 0.0
    assert {r["lean"] for r in records} == {"home", "away"}
    by_id = {g["game_id"]: g for g in games}
    assert all(r["outdoor"] == ((by_id[r["game_id"]]["roof"] or "").lower() not in ("dome", "closed")) for r in records)


def test_backtest_result_has_the_robustness_fields(games: list[dict]) -> None:
    result = run_backtest(games, "elo_blend", PARAMS, [2022, 2023], LIMITS, lambda cp, p: None, lambda: False)
    assert result["era"] == "search"
    assert set(result["ci"]) == {"roi", "avg_clv", "max_drawdown", "hit_rate", "avg_edge"}
    assert result["shrunk_roi"] == pytest.approx(result["roi"] * result["n_bets"] / (result["n_bets"] + 100))
    assert result["mean_ll_gain"] == pytest.approx(result["market_log_loss"] - result["log_loss"])
    assert 0 < result["market_p"] <= 1 and set(result["brier_decomposition"]) == {"reliability", "resolution", "uncertainty"}
    assert isinstance(result["calib_slope"], float) and isinstance(result["calib_intercept"], float)
    validation = run_backtest(games, "elo_blend", PARAMS, [2022, 2023], LIMITS, lambda cp, p: None, lambda: False, era="validation", seed=9)
    assert validation["era"] == "validation"
    for key in ("n_bets", "roi", "log_loss", "shrunk_roi", "mean_ll_gain", "per_season"):
        assert validation[key] == result[key]
    other_seed = run_backtest(games, "elo_blend", PARAMS, [2022, 2023], LIMITS, lambda cp, p: None, lambda: False, seed=9)
    assert other_seed["ci"] == validation["ci"] and other_seed["market_p"] == validation["market_p"]
    assert "per_season" in result and "ci" not in result["per_season"][0]


def test_checkpoint_entries_carry_packed_records_and_old_ones_restart(games: list[dict]) -> None:
    emitted: list[dict] = []
    full = run_backtest(games, "elo_blend", PARAMS, [2022, 2023], LIMITS, lambda cp, p: emitted.append(copy.deepcopy(cp)), lambda: False)
    first = emitted[0]["per_season"][0]
    assert set(first) == {"season", "stats", "blend", "records"}
    assert isinstance(first["records"], str) and len(unpack_probs(first["records"])) == first["stats"]["n_games"]
    assert len(first["records"]) < 12 * first["stats"]["n_games"], "about 11 bytes a scored game"
    json.dumps(emitted[0])
    # a resumed run computes the same resampling fields from the packed records
    resumed = run_backtest(games, "elo_blend", PARAMS, [2022, 2023], LIMITS, lambda cp, p: None, lambda: False, json.loads(json.dumps(emitted[0])))
    assert resumed == full
    # a checkpoint written by an older worker (no records) is not trusted: the run restarts
    old = {"next": 1, "per_season": [{"season": 2022, "stats": first["stats"], "blend": first["blend"]}]}
    progress: list[float] = []
    assert run_backtest(games, "elo_blend", PARAMS, [2022, 2023], LIMITS, lambda cp, p: progress.append(p), lambda: False, old) == full
    assert progress == [0.5, 1.0]
