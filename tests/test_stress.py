"""Stress tests (fleet.sim.stress, fleet.sim.records, fleet.models.search_space): the
price stress keeps or reduces bets, the flag rules on constructed cases, the
neighbourhood perturbation's determinism and clipping, and the regime partition.
Fixture based where records are needed, no database."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from fleet.models.search_space import bounds_of, clip_params, perturb_params
from fleet.sim.backtest import fill_rule, rebuild_records, records_of, run_seasons
from fleet.sim.data import load_games
from fleet.sim.fills import BetRule
from fleet.sim.records import REGIME_DIMENSIONS, kickoff_hour_et, pack_probs, regimes_of, unpack_probs
from fleet.sim.stress import (fragile_flag, neighbourhood_summary, price_stress, regime_dependent_flag, regime_table,
                              stress_flags, stressed_rule)
from fleet.worker.jobs import DEFAULT_LIMITS

FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "games_sample.csv")
PARAMS = {"k": 24, "hfa": 55, "regress": 0.33, "rest_per_day": 1.0, "mov_scale": 1, "min_edge": 0.01, "kelly_fraction": 0.25}
ZERO_FEES = dict(DEFAULT_LIMITS, fee_model={"taker_rate": 0.0, "half_spread": 0.0})
VALIDATION = [2022, 2025]


@pytest.fixture(scope="module")
def games() -> list[dict]:
    return load_games(FIXTURE)


@pytest.fixture(scope="module")
def records(games: list[dict]) -> list[dict]:
    per_season = run_seasons(games, "elo_blend", PARAMS, VALIDATION, ZERO_FEES, lambda cp, p: None, lambda: False)
    return [r for season in records_of(games, "elo_blend", PARAMS, per_season, ZERO_FEES) for r in season]


# price stress ---------------------------------------------------------------------


def test_stressed_rule_changes_only_the_price_terms() -> None:
    base = BetRule.build(PARAMS, DEFAULT_LIMITS)
    spread = stressed_rule(PARAMS, DEFAULT_LIMITS, {"half_spread": 0.02})
    assert spread.half_spread == pytest.approx(0.03) and spread.taker_rate == base.taker_rate
    fee = stressed_rule(PARAMS, DEFAULT_LIMITS, {"taker_rate": 1.5})
    assert fee.taker_rate == pytest.approx(0.075) and fee.half_spread == base.half_spread
    assert (fee.min_edge, fee.kelly_fraction, fee.bankroll_cents, fee.max_bet_cents) == (0.01, 0.25, 10000, 2500)


def test_price_stress_keeps_or_reduces_bets_and_never_touches_the_log_loss(records: list[dict]) -> None:
    base_bets = sum(1 for r in records if r["bet"])
    assert base_bets > 100
    rows = price_stress(records, PARAMS, ZERO_FEES)
    assert [r["name"] for r in rows] == ["spread+0.01", "spread+0.02", "fee x1.5"]
    assert rows[0]["n_bets"] <= base_bets and rows[1]["n_bets"] <= rows[0]["n_bets"] < base_bets
    assert rows[2]["n_bets"] == base_bets, "a taker rate of 0 x 1.5 is still 0"
    base_ll = sum(r["ll_model"] for r in records) / len(records)
    for row in rows:
        assert set(row) == {"name", "n_bets", "roi", "log_loss", "mean_ll_gain"}
        assert row["log_loss"] == pytest.approx(base_ll)
        assert row["mean_ll_gain"] == pytest.approx(rows[0]["mean_ll_gain"])


# flags -----------------------------------------------------------------------------


def _prices(n2: int, roi2: float) -> list[dict]:
    return [{"name": "spread+0.01", "n_bets": n2 + 10, "roi": roi2, "log_loss": 0.6, "mean_ll_gain": 0.0},
            {"name": "spread+0.02", "n_bets": n2, "roi": roi2, "log_loss": 0.6, "mean_ll_gain": 0.0},
            {"name": "fee x1.5", "n_bets": n2 + 5, "roi": roi2, "log_loss": 0.6, "mean_ll_gain": 0.0}]


def _nbhd(median: float) -> dict:
    return {"n": 10, "shrunk_roi_median": median, "shrunk_roi_p10": median - 0.01, "ll_gain_median": 0.0, "ll_gain_p10": 0.0}


def test_fragile_flag_rules() -> None:
    base = {"n_bets": 100, "roi": 0.05}
    assert not fragile_flag(base, _prices(60, 0.02), _nbhd(0.01))
    assert fragile_flag(base, _prices(49, 0.02), _nbhd(0.01)), "fewer than half the bets survive"
    assert not fragile_flag(base, _prices(50, 0.02), _nbhd(0.01)), "exactly half is kept"
    assert fragile_flag(base, _prices(90, -0.001), _nbhd(0.01)), "a positive ROI turns negative"
    assert not fragile_flag({"n_bets": 100, "roi": -0.02}, _prices(90, -0.05), _nbhd(0.01)), "a negative base cannot turn"
    assert fragile_flag(base, _prices(90, 0.03), _nbhd(-0.001)), "the neighbourhood median is below zero"
    assert not fragile_flag({"n_bets": 100, "roi": -0.01}, _prices(90, -0.02), _nbhd(-0.01)), "base below zero too"
    assert not fragile_flag({"n_bets": 0, "roi": 0.0}, _prices(0, 0.0), _nbhd(0.0)), "no bets, nothing to break"
    assert not fragile_flag(base, _prices(90, 0.03), {"n": 0})


def _regimes(bets: dict[str, int] | None = None, **pnls: int) -> dict:
    out = {name: {"n_games": 10, "n_bets": 5, "roi": 0.0, "pnl_cents": 0, "mean_ll_gain": 0.0} for pair in REGIME_DIMENSIONS for name in pair}
    for name, pnl in pnls.items():
        out[name]["pnl_cents"] = pnl
    for name, n in (bets or {}).items():
        out[name]["n_bets"] = n
    return out


def test_regime_dependent_flag_rules() -> None:
    assert not regime_dependent_flag(_regimes(home=600, away=400))
    assert regime_dependent_flag(_regimes(home=1200, away=-700)), "the profit at home, more than half of it lost away"
    assert not regime_dependent_flag(_regimes(home=1200, away=-600)), "giving back exactly half does not flag"
    assert not regime_dependent_flag(_regimes(home=1200, away=-200)), "a small loss on the other side does not flag"
    assert regime_dependent_flag(_regimes(underdog=500, favourite=-300))
    assert not regime_dependent_flag(_regimes(home=1200, away=0)), "the other side must lose money"
    assert not regime_dependent_flag(_regimes(home=-300, away=-500)), "a losing lineage is not regime dependent"
    assert not regime_dependent_flag(_regimes(primetime=100, day=-500)), "a negative total is not judged"
    assert stress_flags({"n_bets": 100, "roi": 0.05}, _prices(10, 0.01), _nbhd(0.02), _regimes(home=1200, away=-700)) == ["fragile", "regime_dependent"]
    assert stress_flags({"n_bets": 100, "roi": 0.05}, _prices(90, 0.03), _nbhd(0.02), _regimes()) == []


# neighbourhood ------------------------------------------------------------------------


def test_perturbation_is_deterministic_bounded_and_keeps_fixed_params() -> None:
    base = {"k": 39.0, "hfa": 21.0, "regress": 0.59, "rest_per_day": 0.0, "mov_scale": 1, "min_edge": 0.01, "kelly_fraction": 0.3}
    first = perturb_params("elo_blend", base, random.Random("7:nbhd:0"))
    assert first == perturb_params("elo_blend", base, random.Random("7:nbhd:0"))
    assert first != perturb_params("elo_blend", base, random.Random("7:nbhd:1"))
    assert first["mov_scale"] == 1 and first["rest_per_day"] == 0.0
    bounds = bounds_of("elo_blend")
    for i in range(50):
        p = perturb_params("elo_blend", base, random.Random(f"7:nbhd:{i}"))
        for name, (low, high) in bounds.items():
            if name not in base:  # a bound for a param this model does not carry (step 6B penalties)
                assert name not in p
                continue
            assert low <= p[name] <= high, name
            assert 0.9 * base[name] - 1e-9 <= p[name] or p[name] == low
            assert p[name] <= 1.1 * base[name] + 1e-9 or p[name] == high
    assert clip_params("elo_blend", {"k": 100.0, "hfa": 0.0, "mov_scale": 1, "unknown": 5.0}) == {"k": 40.0, "hfa": 20.0, "mov_scale": 1, "unknown": 5.0}
    penalised = perturb_params("elo_blend", {**base, "qb_change_penalty": 80.0, "out_penalty_per_player": 15.0}, random.Random("7:nbhd:3"))
    assert penalised["qb_change_penalty"] <= 80.0 and penalised["out_penalty_per_player"] <= 15.0


def test_neighbourhood_summary_percentiles() -> None:
    runs = [{"shrunk_roi": 0.01 * i, "mean_ll_gain": -0.001 * i} for i in range(10)]
    summary = neighbourhood_summary(runs)
    assert summary["n"] == 10
    assert summary["shrunk_roi_median"] == pytest.approx(0.045) and summary["shrunk_roi_p10"] == pytest.approx(0.009)
    assert summary["ll_gain_median"] == pytest.approx(-0.0045) and summary["ll_gain_p10"] == pytest.approx(-0.0081)
    assert neighbourhood_summary([]) == {"n": 0, "shrunk_roi_median": 0.0, "shrunk_roi_p10": 0.0, "ll_gain_median": 0.0, "ll_gain_p10": 0.0}


# regimes ---------------------------------------------------------------------------------


def test_regime_partition_covers_every_scored_game_once_per_dimension(records: list[dict]) -> None:
    table = regime_table(records, ZERO_FEES)
    assert set(table) == {name for pair in REGIME_DIMENSIONS for name in pair}
    n_bets = sum(1 for r in records if r["bet"])
    pnl = sum(r["pnl_cents"] for r in records)
    for first, second in REGIME_DIMENSIONS:
        a, b = table[first], table[second]
        assert a["n_games"] + b["n_games"] == len(records), (first, second)
        assert a["n_bets"] + b["n_bets"] == n_bets
        assert a["pnl_cents"] + b["pnl_cents"] == pnl
        assert a["n_games"] > 0 and b["n_games"] > 0, "the fixture has both sides of every dimension"
        for part in (a, b):
            assert set(part) == {"n_games", "n_bets", "roi", "pnl_cents", "mean_ll_gain"}
    for r in records:
        names = regimes_of(r)
        assert len(names) == 5 and all(name in pair for name, pair in zip(names, REGIME_DIMENSIONS))


def test_regime_classification_of_a_record() -> None:
    base = {"p_market": 0.6, "lean": "home", "div_game": 1, "hour_et": 20, "temp": 30, "wind": 5, "outdoor": True}
    assert regimes_of(base) == ["favourite", "home", "divisional", "primetime", "cold_or_windy"]
    assert regimes_of(dict(base, lean="away")) == ["underdog", "away", "divisional", "primetime", "cold_or_windy"]
    assert regimes_of(dict(base, p_market=0.5)) == ["favourite", "home", "divisional", "primetime", "cold_or_windy"]
    assert regimes_of(dict(base, div_game=0, hour_et=19, temp=60)) == ["favourite", "home", "non_divisional", "day", "other_weather"]
    assert regimes_of(dict(base, temp=60, wind=15))[-1] == "cold_or_windy"
    assert regimes_of(dict(base, outdoor=False))[-1] == "other_weather", "a dome is never cold or windy"
    assert regimes_of(dict(base, temp=None, wind=None))[-1] == "other_weather"
    assert kickoff_hour_et("2016-09-09T00:30:00+00:00") == 20  # 8:30 pm Eastern the evening before (daylight time)
    assert kickoff_hour_et("2016-12-04T18:00:00+00:00") == 13


def test_records_pack_and_rebuild_round_trip(games: list[dict], records: list[dict]) -> None:
    """A season's packed probabilities rebuild its records exactly under the same rule;
    the packing is compact and refuses a malformed or mismatched string."""
    season = records[0]["season"]
    mine = [r for r in records if r["season"] == season]
    packed = pack_probs(mine)
    assert unpack_probs(packed) == [r["p"] for r in mine] and len(packed) <= 11 * len(mine) + 4
    rebuilt = rebuild_records(games, season, packed, fill_rule("elo_blend", PARAMS, ZERO_FEES))
    assert len(rebuilt) == len(mine)
    for back, r in zip(rebuilt, mine):
        assert {k: v for k, v in back.items()} == r
        assert regimes_of(back) == regimes_of(r)
    with pytest.raises(ValueError):
        rebuild_records(games, season, pack_probs(mine[:-1]), fill_rule("elo_blend", PARAMS, ZERO_FEES))
    with pytest.raises(ValueError):
        unpack_probs(packed[:-3])
    with pytest.raises(ValueError):
        unpack_probs("not base64!")


def test_regime_dependent_needs_a_material_loss_on_a_material_regime() -> None:
    """Review 6A (medium): the old "more than 80% of the profit" test was met by any
    losing side, so every profitable model was flagged. A loss counts only when the
    losing side holds at least 20% of the pair's bets and gives back more than half of
    the winning side's profit."""
    assert not regime_dependent_flag(_regimes(home=10000, away=-1)), "home +$100.00, away -$0.01"
    big_loss_few_bets = _regimes(bets={"home": 90, "away": 10}, home=1200, away=-900)
    assert not regime_dependent_flag(big_loss_few_bets), "the losing side holds 10% of the bets"
    at_share = _regimes(bets={"home": 80, "away": 20}, home=1200, away=-900)
    assert regime_dependent_flag(at_share), "20% of the bets is enough"
    assert not regime_dependent_flag(_regimes(bets={"home": 0, "away": 0}, home=0, away=0)), "no bets, nothing to judge"
