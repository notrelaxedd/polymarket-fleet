"""Odds maths, the Elo engine, the blend fit, params_hash, the summary and no-leakage.
Fixture based, no database."""
from __future__ import annotations

import math
import random
import re
from pathlib import Path

import pytest

from fleet.models.base import params_hash
from fleet.models.blend_fit import fit_blend
from fleet.models.elo import Elo
from fleet.models.elo_blend import EloBlend
from fleet.models.registry import FAMILIES
from fleet.sim.backtest import run_fold
from fleet.sim.data import load_games
from fleet.sim.odds import american_to_implied, devig, expit, logit
from fleet.worker.jobs import DEFAULT_LIMITS

FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "games_sample.csv")
PARAMS = {"k": 24, "hfa": 55, "regress": 0.33, "rest_per_day": 1.0, "mov_scale": 1,
          "min_edge": 0.03, "kelly_fraction": 0.25}


@pytest.fixture(scope="module")
def games() -> list[dict]:
    return load_games(FIXTURE)


def sentences(text: str) -> int:
    return len(re.findall(r"[.!?](?=\s|$)", text))


# odds ---------------------------------------------------------------------


def test_american_to_implied() -> None:
    assert american_to_implied(-150) == pytest.approx(0.6)
    assert american_to_implied(136) == pytest.approx(100 / 236)
    assert american_to_implied(100) == pytest.approx(0.5)
    assert american_to_implied(None) is None


def test_devig() -> None:
    imp_home, imp_away = 0.6, 100 / 236
    assert devig(-150, 136) == pytest.approx(imp_home / (imp_home + imp_away))
    assert devig(-110, -110) == pytest.approx(0.5)
    assert devig(None, 136) is None
    assert devig(-150, None) is None


def test_logit_expit_roundtrip() -> None:
    for p in (0.01, 0.3, 0.5, 0.77, 0.99):
        assert expit(logit(p)) == pytest.approx(p)
    assert 0.0 < expit(-50) < 0.5 < expit(50) <= 1.0
    assert math.isfinite(logit(0.0)) and math.isfinite(logit(1.0))


# elo ----------------------------------------------------------------------


def game(home: str = "A", away: str = "B", hs: int | None = 24, as_: int | None = 17,
         season: int = 2020, home_rest: int = 7, away_rest: int = 7) -> dict:
    return {"game_id": f"{season}_{home}_{away}", "season": season, "week": 1, "home_team": home,
            "away_team": away, "home_score": hs, "away_score": as_, "home_rest": home_rest,
            "away_rest": away_rest, "home_moneyline": -150, "away_moneyline": 130}


def test_elo_one_game_without_margin_scaling() -> None:
    elo = Elo(k=24, hfa=55, regress=0.3, rest_per_day=1.0, mov_scale=0)
    e = 1 / (1 + 10 ** (-55 / 400))
    assert elo.expect(game()) == pytest.approx(e)
    delta = elo.update(game())
    assert delta == pytest.approx(24 * (1 - e))
    assert elo.rating("A") == pytest.approx(1500 + 24 * (1 - e))
    assert elo.rating("B") == pytest.approx(1500 - 24 * (1 - e))
    assert elo.games_seen == 1


def test_elo_one_game_with_margin_scaling() -> None:
    elo = Elo(k=20, hfa=55, regress=0.3, rest_per_day=0.0, mov_scale=1)
    e = 1 / (1 + 10 ** (-55 / 400))
    mult = math.log(7 + 1) * 2.2 / (0.001 * 55 + 2.2)  # winner (home) advantage includes hfa
    assert elo.update(game()) == pytest.approx(20 * mult * (1 - e))
    # an away upset: the winner's advantage is minus the home advantage
    elo2 = Elo(k=20, hfa=55, regress=0.3, rest_per_day=0.0, mov_scale=1)
    mult_away = math.log(10 + 1) * 2.2 / (0.001 * -55 + 2.2)
    assert elo2.update(game(hs=10, as_=20)) == pytest.approx(20 * mult_away * (0 - e))


def test_elo_tie() -> None:
    """LOW: a tie moves the ratings with margin scaling on too (margin floored at 1, ln 2)."""
    e = 1 / (1 + 10 ** (-55 / 400))
    with_mov = Elo(k=24, hfa=55, regress=0.3, rest_per_day=0.0, mov_scale=1)
    assert with_mov.update(game(hs=20, as_=20)) == pytest.approx(24 * math.log(2) * (0.5 - e))
    assert with_mov.update(game(hs=20, as_=20)) != 0.0
    no_mov = Elo(k=24, hfa=55, regress=0.3, rest_per_day=0.0, mov_scale=0)
    assert no_mov.update(game(hs=20, as_=20)) == pytest.approx(24 * (0.5 - e))


def test_elo_season_regression() -> None:
    elo = Elo(k=24, hfa=0, regress=0.25, rest_per_day=0.0, mov_scale=0)
    elo.begin_season(2019)
    elo.ratings = {"A": 1600.0, "B": 1400.0}
    elo.begin_season(2019)  # same season: nothing happens
    assert elo.ratings == {"A": 1600.0, "B": 1400.0}
    elo.expect(game(season=2020))  # the first look at a new season regresses
    assert elo.rating("A") == pytest.approx(1575.0)
    assert elo.rating("B") == pytest.approx(1425.0)
    assert elo.rating("C") == 1500.0


def test_elo_rest_clamp() -> None:
    elo = Elo(k=24, hfa=0, regress=0.3, rest_per_day=2.0, mov_scale=0)
    e = elo.expect(game(home_rest=14, away_rest=4))  # diff 10 clamps to 3
    assert e == pytest.approx(1 / (1 + 10 ** (-(2.0 * 3) / 400)))
    e2 = elo.expect(game(home_rest=4, away_rest=14))
    assert e2 == pytest.approx(1 / (1 + 10 ** (-(2.0 * -3) / 400)))
    assert elo.expect(game(home_rest=8, away_rest=7)) == pytest.approx(1 / (1 + 10 ** (-2.0 / 400)))


def test_elo_unplayed_game_is_neither_counted_nor_rated() -> None:
    """LOW: games_seen counts played games only, so a schedule does not inflate it."""
    elo = Elo(k=24, hfa=55, regress=0.3, rest_per_day=0.0, mov_scale=1)
    assert elo.update(game(hs=None, as_=None)) == 0.0
    assert elo.ratings == {} and elo.games_seen == 0
    elo.update(game())
    assert elo.games_seen == 1


# blend --------------------------------------------------------------------


def synthetic_rows(n: int, seed: int, exact: bool) -> list[tuple[float, float, float]]:
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        x1, x2 = rng.gauss(0, 1), rng.gauss(0, 1)
        p = expit(0.8 * x1 + 0.5 * x2 - 0.1)
        rows.append((x1, x2, p if exact else float(rng.random() < p)))
    return rows


def test_blend_fit_recovers_known_coefficients() -> None:
    a, b, c = fit_blend(synthetic_rows(3000, 1, exact=True))  # fractional outcomes: exact optimum
    assert (a, b, c) == pytest.approx((0.8, 0.5, -0.1), abs=2e-3)
    a, b, c = fit_blend(synthetic_rows(6000, 2, exact=False))  # Bernoulli outcomes: noisy
    assert (a, b, c) == pytest.approx((0.8, 0.5, -0.1), abs=0.12)


def test_blend_fit_is_deterministic_and_handles_empty() -> None:
    rows = synthetic_rows(500, 3, exact=False)
    assert fit_blend(rows) == fit_blend(list(rows))
    assert fit_blend([]) == (0.0, 1.0, 0.0)


# params, search space, artifacts, summary ---------------------------------------


def test_params_hash_canonical() -> None:
    h = params_hash({"k": 24.0, "hfa": 55.0000001})
    assert re.fullmatch(r"[0-9a-f]{16}", h)
    assert h == params_hash({"hfa": 55.0, "k": 24.0})
    assert h != params_hash({"hfa": 55.1, "k": 24.0})


def test_search_space_ranges_and_reproducibility() -> None:
    for i in range(50):
        p = EloBlend.search_space(random.Random(f"1:{i}"))
        assert 10 <= p["k"] <= 40 and 20 <= p["hfa"] <= 90 and 0.1 <= p["regress"] <= 0.6
        assert 0 <= p["rest_per_day"] <= 4 and p["mov_scale"] in (0, 1)
        assert 0.01 <= p["min_edge"] <= 0.08 and 0.1 <= p["kelly_fraction"] <= 0.5
        assert p == EloBlend.search_space(random.Random(f"1:{i}"))
    assert FAMILIES["elo_blend"] is EloBlend


def test_summary_is_three_sentences() -> None:
    metrics = {"n_games": 1000, "n_bets": 312, "avg_edge": 0.034, "roi": 0.021, "max_drawdown": 0.14,
               "log_loss": 0.662, "market_log_loss": 0.659, "seasons": [2010, 2025],
               "blend": {"a": 0.4, "b": 1.0, "c": 0.0}}
    text = EloBlend.summary(PARAMS, metrics)
    assert sentences(text) == 3
    assert text.startswith("Elo blend (K 24, home edge 55, 71% weight on the closing line, margin scaling on).")
    assert "Across 2010-2025 it placed 312 bets at an average edge of 3.4% and returned +2.1% on stake with a 14% max drawdown." in text
    assert "Log-loss 0.662 against the market's 0.659; it leans on the market" in text
    assert sentences(EloBlend.summary({"k": 12, "hfa": 30, "mov_scale": 0}, {})) == 3
    assert chr(0x2014) not in text


def test_summary_without_bets_and_with_few_bets() -> None:
    """MEDIUM: 0 bets is said plainly (no "+0.0% on stake"), under 50 bets is flagged."""
    base = {"n_games": 1000, "log_loss": 0.662, "market_log_loss": 0.659, "seasons": [2010, 2025],
            "blend": {"a": 0.4, "b": 1.0, "c": 0.0}}
    none = EloBlend.summary({**PARAMS, "min_edge": 0.062}, {**base, "n_bets": 0, "roi": 0.0, "avg_edge": 0.0, "max_drawdown": 0.0})
    assert sentences(none) == 3
    assert "Across 2010-2025 it never found an edge above its 6.2% minimum after fees, so it placed no bets." in none
    assert "+0.0%" not in none and "0 bets" not in none
    few = EloBlend.summary(PARAMS, {**base, "n_bets": 3, "roi": 1.04, "avg_edge": 0.03, "max_drawdown": 0.0})
    assert sentences(few) == 3
    assert "placed 3 bets at an average edge of 3.0% and returned +104.0% on stake with a 0% max drawdown (too few bets to judge)." in few
    enough = EloBlend.summary(PARAMS, {**base, "n_bets": 50, "roi": 0.02, "avg_edge": 0.03, "max_drawdown": 0.1})
    assert "too few" not in enough
    # A missing drawdown (capital 0) is not reported as 0%.
    unknown = EloBlend.summary(PARAMS, {**base, "n_bets": 60, "roi": 0.02, "avg_edge": 0.03, "max_drawdown": None})
    assert "with an unknown max drawdown." in unknown and sentences(unknown) == 3


def test_summary_weight_and_single_season() -> None:
    """LOW: a negative Elo coefficient is not reported as a positive share; one season is not a span."""
    metrics = {"n_games": 10, "n_bets": 0, "seasons": [2019], "blend": {"a": -0.3, "b": 1.0, "c": 0.0},
               "log_loss": 0.6, "market_log_loss": 0.6}
    text = EloBlend.summary(PARAMS, metrics)
    assert text.startswith("Elo blend (K 24, home edge 55, all weight on the closing line, Elo adds nothing, margin scaling on).")
    assert "77%" not in text and "Across 2019 it" in text and "2019-2019" not in text
    assert sentences(text) == 3


def test_artifact_roundtrip_predicts_identically(games: list[dict]) -> None:
    model = EloBlend(PARAMS)
    model.fit(games, (2020, 5), lambda: False)
    artifact = model.to_json()
    assert artifact["through"] == [2020, 5] and artifact["season"] == 2020
    assert artifact["games_seen"] == sum(1 for g in games if (g["season"], g["week"]) <= (2020, 5))
    assert set(artifact["blend"]) == {"a", "b", "c"} and len(artifact["ratings"]) == 32
    clone = EloBlend.from_json(PARAMS, artifact)
    later = [g for g in games if (g["season"], g["week"]) > (2020, 5)][:40]
    for g in later:
        pm = devig(g["home_moneyline"], g["away_moneyline"])
        assert clone.predict(g, pm, {}) == model.predict(g, pm, {})
        clone.observe(g)
        model.observe(g)


# no leakage ----------------------------------------------------------------


def _alter(g: dict, rng: random.Random) -> dict:
    g = dict(g)
    g["home_score"], g["away_score"] = g["away_score"], g["home_score"]
    g["home_moneyline"], g["away_moneyline"] = g["away_moneyline"], g["home_moneyline"]
    g["home_rest"] = rng.randint(3, 14)
    return g


@pytest.mark.parametrize("season", [2020, 2023])
def test_no_leakage_from_future_seasons(games: list[dict], season: int) -> None:
    limits = dict(DEFAULT_LIMITS)
    base, blend = run_fold(games, "elo_blend", PARAMS, season, limits, lambda: False)
    rng = random.Random(season)
    without_future = [g for g in games if g["season"] <= season]
    altered_future = [g if g["season"] <= season else _alter(g, rng) for g in games]
    for variant in (without_future, altered_future):
        records, blend2 = run_fold(variant, "elo_blend", PARAMS, season, limits, lambda: False)
        assert blend2 == blend
        assert [(r["game_id"], r["p"], r["pnl_cents"]) for r in records] == [
            (r["game_id"], r["p"], r["pnl_cents"]) for r in base]


def test_no_leakage_inside_the_season(games: list[dict]) -> None:
    """Altering the second half of the test season changes nothing before it."""
    limits = dict(DEFAULT_LIMITS)
    season = 2022
    base, _ = run_fold(games, "elo_blend", PARAMS, season, limits, lambda: False)
    in_season = [g for g in games if g["season"] == season]
    cut = in_season[len(in_season) // 2]["kickoff_at"]
    rng = random.Random(1)
    variant = [g if not (g["season"] >= season and g["kickoff_at"] >= cut) else _alter(g, rng) for g in games]
    records, _ = run_fold(variant, "elo_blend", PARAMS, season, limits, lambda: False)
    first_half = [r for r in base if r["game_id"] in {g["game_id"] for g in in_season if g["kickoff_at"] < cut}]
    assert [(r["game_id"], r["p"]) for r in records[:len(first_half)]] == [(r["game_id"], r["p"]) for r in first_half]
    assert len(first_half) > 100


def test_artifact_keeps_the_elo_season_so_a_reload_regresses_once(games: list[dict]) -> None:
    """MEDIUM: training through a season with no games yet (the off-season) must not lose
    the between-season regression when the child is reloaded from its artifact."""
    last = games[-1]["season"]
    future = (last + 1, 1)
    model = EloBlend(PARAMS)
    model.fit(games, future, lambda: False)
    assert model.elo.season == last, "the ratings sit in the last season actually replayed"
    artifact = model.to_json()
    assert artifact["through"] == list(future) and artifact["season"] == last
    clone = EloBlend.from_json(PARAMS, artifact)
    assert clone.elo.season == last
    probe = {"season": last + 1, "week": 1, "home_team": "KC", "away_team": "LV", "home_rest": 7, "away_rest": 7,
             "home_moneyline": -150, "away_moneyline": 130}
    assert clone.predict(probe, 0.6, {}) == model.predict(probe, 0.6, {})
    assert clone.elo.rating("KC") == pytest.approx(model.elo.rating("KC"))
    regressed = 1500 + (artifact["ratings"]["KC"] - 1500) * (1 - PARAMS["regress"])
    assert clone.elo.rating("KC") == pytest.approx(regressed), "the first look at the new season regressed once"
    # An old artifact without the season falls back to through[0].
    legacy = {k: v for k, v in artifact.items() if k != "season"}
    assert EloBlend.from_json(PARAMS, legacy).elo.season == future[0]


def test_blend_window_is_inclusive_like_the_backtest_fold(games: list[dict]) -> None:
    """LOW: training through (S, 22) fits the blend the fold testing S + 1 uses."""
    model = EloBlend(PARAMS)
    model.fit(games, (2024, 22), lambda: False)
    _, fold_blend = run_fold(games, "elo_blend", PARAMS, 2025, dict(DEFAULT_LIMITS), lambda: False)
    assert model.blend == pytest.approx(fold_blend)
    # A mid-season point keeps the played games of that week in the fit.
    mid = EloBlend(PARAMS)
    mid.fit(games, (2021, 10), lambda: False)
    before = EloBlend(PARAMS)
    before.fit(games, (2021, 9), lambda: False)
    assert mid.blend != before.blend, "week 10's played games are in the fit"
