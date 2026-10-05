"""The epa_blend family: features, coefficient recovery, no leakage, artifacts, search
and summary (docs/ROBUSTNESS.md B2; step 6B contract section B)."""

from __future__ import annotations

import json
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fleet.models import newton
from fleet.models.epa_blend import DEFAULT_PARAMS, PARAM_KEYS, EpaBlend, carried_signals
from fleet.models.epa_features import FEATURE_NAMES, LeagueMean, rolling
from fleet.models.registry import FAMILIES
from fleet.models.search_space import bounds_of, perturb_params
from fleet.sim.backtest import run_fold
from fleet.sim.data import attach_team_stats, features_of, load_games
from fleet.sim.odds import devig, expit
from fleet.sim.search import run_search
from fleet.sim.train import run_train
from fleet.worker.jobs import DEFAULT_LIMITS

FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "games_sample.csv")
PARAMS = {"window": 6, "shrink": 2.0, "l2": 0.5, "min_edge": 0.03, "kelly_fraction": 0.25}


def sentences(text: str) -> int:
    return len(re.findall(r"[.!?](?=\s|$)", text))


# fixture with synthetic team_game_stats ------------------------------------------


def stats_rows(games: list[dict], seed: str = "stats") -> list[dict]:
    """Two rows per played game, EPA loosely tied to the score, seeded per game."""
    rows = []
    for g in games:
        if g["home_score"] is None:
            continue
        rng = random.Random(f"{seed}:{g['game_id']}")
        for team, pf, pa in ((g["home_team"], g["home_score"], g["away_score"]),
                             (g["away_team"], g["away_score"], g["home_score"])):
            rows.append({"game_id": g["game_id"], "season": g["season"], "week": g["week"], "team": team,
                         "kickoff_at": g["kickoff_at"], "off_epa_per_play": (pf - 22) / 60 + rng.gauss(0, 0.05),
                         "def_epa_per_play": (pa - 22) / 60 + rng.gauss(0, 0.05), "pass_rate": 0.58,
                         "plays": 62, "success_rate": 0.44})
    return rows


def write_cache(path: Path, games: list[dict], stats: list[dict]) -> list[dict]:
    rows = [{k: v for k, v in g.items() if k != "team_stats"} for g in games]
    path.write_text(json.dumps({"games": rows, "count": len(rows), "team_game_stats": stats}), encoding="utf-8")
    return load_games(str(path))


@pytest.fixture(scope="module")
def raw() -> list[dict]:
    return load_games(FIXTURE)


@pytest.fixture(scope="module")
def games(raw: list[dict], tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    return write_cache(tmp_path_factory.mktemp("epa") / "games.json", raw, stats_rows(raw))


# features --------------------------------------------------------------------------


def test_rolling_shrinks_toward_the_league_mean() -> None:
    rows = [{"off_epa_per_play": v, "def_epa_per_play": -v} for v in (0.5, 0.1, 0.2, 0.3)]
    league = (0.0, 0.1)
    assert rolling([], 8, 3.0, league) == league
    off, dfn = rolling(rows, 3, 3.0, league)  # last 3 rows, weight 3 / (3 + 3)
    assert off == pytest.approx(0.5 * 0.2) and dfn == pytest.approx(0.1 + 0.5 * (-0.2 - 0.1))
    assert rolling(rows, 3, 0.0, league)[0] == pytest.approx(0.2), "no shrinkage at 0"


def test_league_mean_counts_each_row_once_and_only_recent_seasons() -> None:
    lm = LeagueMean()
    a1 = {"team": "A", "season": 2020, "game_id": "g1", "kickoff_at": "2020-09-10", "off_epa_per_play": 0.2,
          "def_epa_per_play": 0.0}
    a2 = {**a1, "game_id": "g2", "kickoff_at": "2020-09-17", "off_epa_per_play": 0.4}
    lm.absorb({"home": [a1], "away": []})
    lm.absorb({"home": [a1, a2], "away": []})
    assert lm.seasons[2020][0] == 2 and lm.mean(2020) == pytest.approx((0.3, 0.0))
    assert lm.mean(2021) == pytest.approx((0.3, 0.0)) and lm.mean(2022) == (0.0, 0.0)
    assert LeagueMean.from_json(json.loads(json.dumps(lm.to_json()))).to_json() == lm.to_json()


def test_feature_vector_reads_signals_rest_and_market(games: list[dict]) -> None:
    model = EpaBlend(PARAMS)
    g = dict(next(x for x in games if x["season"] == 2018 and x["team_stats"]["home"]))
    g.update(home_rest=13, away_rest=6, div_game=1,
             signals={"home_qb_changed": 1, "away_out_count": 4, "home_out_count": 1})
    x = dict(zip(FEATURE_NAMES, model._vector(g, features_of(g), 0.5)))
    assert x["intercept"] == 1.0 and x["rest"] == 1.0 and x["divisional"] == 1.0
    assert x["qb_change"] == 1.0 and x["outs"] == -3.0 and x["market"] == pytest.approx(0.0)
    assert x["off_epa"] != 0.0 and x["elo"] == 0.0, "untrained Elo: every team at 1500"


# coefficient recovery on synthetic data -----------------------------------------------

TRUE = {"intercept": 0.15, "elo": 0.6, "off_epa": 3.0, "def_epa": -3.0, "rest": 0.2, "qb_change": -0.4,
        "outs": -0.08, "divisional": -0.2, "market": 0.8}


def _american(p: float) -> int:
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def synthetic_games(seasons: int = 14) -> list[dict]:
    """32 teams with drifting latent offence and defence; outcomes drawn in kickoff
    order from TRUE applied to the model's own feature rows."""
    rng = random.Random("epa-synth")
    teams = [f"T{i:02d}" for i in range(32)]
    off = {t: rng.gauss(0, 0.08) for t in teams}
    dfn = {t: rng.gauss(0, 0.08) for t in teams}
    start = datetime(2000, 9, 1, 17, tzinfo=timezone.utc)
    games, stats = [], []
    for s in range(seasons):
        for week in range(1, 18):
            order = teams[:]
            rng.shuffle(order)
            for j in range(16):
                home, away = order[2 * j], order[2 * j + 1]
                kick = (start + timedelta(days=365 * s + 7 * week, minutes=j)).isoformat()
                gid = f"{2000 + s}_{week:02d}_{away}_{home}"
                pm = expit(rng.gauss(0, 0.7))
                games.append({
                    "game_id": gid, "season": 2000 + s, "game_type": "REG", "week": week, "kickoff_at": kick,
                    "home_team": home, "away_team": away, "home_score": None, "away_score": None,
                    "home_moneyline": _american(pm), "away_moneyline": _american(1 - pm),
                    "home_rest": rng.randint(4, 14), "away_rest": rng.randint(4, 14),
                    "div_game": 1 if rng.random() < 0.3 else 0,
                    "signals": {"home_qb_changed": int(rng.random() < 0.15), "away_qb_changed": int(rng.random() < 0.15),
                                "home_out_count": rng.randint(0, 6), "away_out_count": rng.randint(0, 6)}})
                for team, opp in ((home, away), (away, home)):
                    stats.append({"game_id": gid, "season": 2000 + s, "week": week, "team": team, "kickoff_at": kick,
                                  "off_epa_per_play": off[team] - dfn[opp] + rng.gauss(0, 0.15),
                                  "def_epa_per_play": off[opp] + dfn[team] + rng.gauss(0, 0.15),
                                  "pass_rate": 0.6, "plays": 60, "success_rate": 0.45})
        for t in teams:
            off[t] = 0.6 * off[t] + rng.gauss(0, 0.06)
            dfn[t] = 0.6 * dfn[t] + rng.gauss(0, 0.06)
    attach_team_stats(games, stats)
    gen = EpaBlend(PARAMS)
    w = [TRUE[n] for n in FEATURE_NAMES]
    for g in games:
        f = features_of(g)
        x = gen._vector(g, f, devig(g["home_moneyline"], g["away_moneyline"]))
        home_won = rng.random() < expit(sum(a * b for a, b in zip(w, x)))
        g["home_score"], g["away_score"] = (24, 17) if home_won else (17, 24)
        gen._learn(g, f)
    return games


def test_fit_recovers_known_coefficients() -> None:
    games = synthetic_games()
    model = EpaBlend({**PARAMS, "l2": 0.01})
    model.fit(games, None, lambda: False)
    # Standard errors from the Hessian at the fit, on the very rows the fit saw.
    probe = EpaBlend(PARAMS)
    xs, ys = [], []
    for g in games:
        f = features_of(g)
        xs.append(probe._vector(g, f, devig(g["home_moneyline"], g["away_moneyline"])))
        ys.append(1.0 if g["home_score"] > g["away_score"] else 0.0)
        probe._learn(g, f)
    assert model.n_train == len(xs)
    _, h = newton.gradient_hessian(xs, ys, model.coef, 0.0)
    for i, name in enumerate(FEATURE_NAMES):
        unit = [1.0 if j == i else 0.0 for j in range(len(FEATURE_NAMES))]
        se = newton.solve(h, unit)[i] ** 0.5
        assert abs(model.blend[name] - TRUE[name]) < 4 * se, (name, model.blend[name], TRUE[name], se)
    assert model.blend["off_epa"] > 1.5 and model.blend["def_epa"] < -1.5
    # A heavy L2 pulls the signals toward 0 but leaves the market and intercept free.
    heavy = EpaBlend({**PARAMS, "l2": 1e4})
    heavy.fit(games, None, lambda: False)
    assert abs(heavy.blend["off_epa"]) < 0.1 and heavy.blend["market"] > 0.5


# no leakage --------------------------------------------------------------------------


def _alter(g: dict, rng: random.Random) -> dict:
    g = dict(g)
    g["home_score"], g["away_score"] = g["away_score"], g["home_score"]
    g["home_moneyline"], g["away_moneyline"] = g["away_moneyline"], g["home_moneyline"]
    g["home_rest"] = rng.randint(3, 14)
    g["signals"] = {**g["signals"], "home_qb_changed": 1, "away_out_count": 9}
    return g


def _key(records: list[dict]) -> list[tuple]:
    return [(r["game_id"], r["p"], r["pnl_cents"]) for r in records]


@pytest.mark.parametrize("season", [2020, 2023])
def test_no_leakage_from_future_seasons(raw: list[dict], games: list[dict], season: int, tmp_path: Path) -> None:
    limits = dict(DEFAULT_LIMITS)
    base, blend = run_fold(games, "epa_blend", PARAMS, season, limits, lambda: False)
    assert len(base) > 200
    rng = random.Random(season)
    altered_games = [g if g["season"] <= season else _alter(g, rng) for g in raw]
    altered_stats = stats_rows(altered_games, seed="other")  # every future stat changes
    altered_stats = [r for r in altered_stats if r["season"] > season] + [
        r for r in stats_rows(raw) if r["season"] <= season]
    variants = [
        write_cache(tmp_path / "cut.json", [g for g in raw if g["season"] <= season], stats_rows(raw)),
        write_cache(tmp_path / "alt.json", altered_games, altered_stats),
    ]
    for variant in variants:
        records, blend2 = run_fold(variant, "epa_blend", PARAMS, season, limits, lambda: False)
        assert blend2 == blend
        assert _key(records) == _key(base)


def test_no_leakage_inside_the_season(raw: list[dict], games: list[dict], tmp_path: Path) -> None:
    """Altering the results and stats of the second half of the test season changes
    no prediction before it."""
    limits = dict(DEFAULT_LIMITS)
    season = 2022
    base, _ = run_fold(games, "epa_blend", PARAMS, season, limits, lambda: False)
    in_season = [g for g in raw if g["season"] == season]
    cut = in_season[len(in_season) // 2]["kickoff_at"]
    rng = random.Random(1)
    later = [g if not (g["season"] >= season and g["kickoff_at"] >= cut) else _alter(g, rng) for g in raw]
    stats = [r for r in stats_rows(raw) if r["kickoff_at"] < cut] + [
        r for r in stats_rows(later, seed="later") if r["kickoff_at"] >= cut]
    records, _ = run_fold(write_cache(tmp_path / "half.json", later, stats), "epa_blend", PARAMS, season, limits,
                          lambda: False)
    early = {g["game_id"] for g in in_season if g["kickoff_at"] < cut}
    first_half = [r for r in base if r["game_id"] in early]
    assert len(first_half) > 100
    assert [(r["game_id"], r["p"]) for r in records[:len(first_half)]] == [(r["game_id"], r["p"]) for r in first_half]
    assert [r["p"] for r in records] != [r["p"] for r in base], "the altered half does change later predictions"


# artifacts, training, registry -------------------------------------------------------


def test_artifact_roundtrip_predicts_identically(games: list[dict]) -> None:
    model = EpaBlend(PARAMS)
    model.fit(games, (2021, 9), lambda: False)
    artifact = json.loads(json.dumps(model.to_json()))
    assert artifact["features"] == list(FEATURE_NAMES) and len(artifact["coef"]) == len(FEATURE_NAMES)
    assert artifact["through"] == [2021, 9] and artifact["season"] == 2021 and artifact["n_train"] > 1000
    clone = EpaBlend.from_json(PARAMS, artifact)
    later = [g for g in games if (g["season"], g["week"]) > (2021, 9) and g["home_moneyline"] is not None][:60]
    for g in later:
        pm = devig(g["home_moneyline"], g["away_moneyline"])
        assert clone.predict(g, pm, features_of(g)) == model.predict(g, pm, features_of(g))
        assert clone.predict(g, None, {}) == model.predict(g, None, {})
        clone.observe(g)
        model.observe(g)


def test_train_job_returns_a_child_artifact(games: list[dict]) -> None:
    out = run_train(games, {"id": 7, "family": "epa_blend", "params": PARAMS}, {"season": 2024, "week": 22},
                    lambda cp, p: None, lambda: False)
    child = out["create_models"][0]
    assert child["family"] == "epa_blend" and child["params"] == PARAMS and out["games_seen"] > 2000
    fold_blend = run_fold(games, "epa_blend", PARAMS, 2025, dict(DEFAULT_LIMITS), lambda: False)[1]
    assert dict(zip(child["artifact"]["features"], child["artifact"]["coef"])) == pytest.approx(fold_blend)


def test_registry_params_and_search_space() -> None:
    assert FAMILIES["epa_blend"] is EpaBlend
    assert set(PARAM_KEYS) == set(DEFAULT_PARAMS)
    bounds = bounds_of("epa_blend")
    for i in range(60):
        p = EpaBlend.search_space(random.Random(f"3:{i}"))
        assert set(p) == set(PARAM_KEYS)
        assert isinstance(p["window"], int) and 4 <= p["window"] <= 12
        assert 0 <= p["shrink"] <= 8 and 0.01 <= p["l2"] <= 10
        assert 0.01 <= p["min_edge"] <= 0.08 and 0.1 <= p["kelly_fraction"] <= 0.5
        assert p == EpaBlend.search_space(random.Random(f"3:{i}"))
        q = perturb_params("epa_blend", p, random.Random(f"n:{i}"))
        assert all(bounds[k][0] <= q[k] <= bounds[k][1] for k in bounds)
        assert 4 <= EpaBlend(q).window <= 12


# summary -----------------------------------------------------------------------------


def test_summary_names_the_signals_that_carried_it() -> None:
    metrics = {"n_games": 900, "n_bets": 120, "avg_edge": 0.04, "roi": 0.03, "max_drawdown": 0.12,
               "log_loss": 0.655, "market_log_loss": 0.661, "seasons": [2018, 2025],
               "blend": {"intercept": 0.02, "elo": 0.1, "off_epa": 1.6, "def_epa": -0.9, "rest": 0.01,
                         "qb_change": -0.2, "outs": -0.001, "divisional": 0.0, "market": 0.91}}
    text = EpaBlend.summary(PARAMS, metrics)
    assert sentences(text) == 3
    assert text.startswith("EPA blend (6-game window, shrink 2.0, L2 0.50, market weight 0.91); the signals that "
                           "carried it were quarterback change (0.20), offensive EPA (0.16) and defensive EPA (0.09)")
    assert "Across 2018-2025 it placed 120 bets" in text and "beats the closing line" in text
    assert [n for n, _ in carried_signals(metrics["blend"])] == ["qb_change", "off_epa", "def_epa"]
    quiet = EpaBlend.summary(PARAMS, {**metrics, "blend": {n: 0.0 for n in FEATURE_NAMES}})
    assert "no signal moved the price" in quiet and sentences(quiet) == 3
    empty = EpaBlend.summary({}, {})
    assert "no coefficients were fitted" in empty and sentences(empty) == 3
    assert chr(0x2014) not in text + quiet + empty


# search end to end -------------------------------------------------------------------


def test_small_search_runs_end_to_end(games: list[dict]) -> None:
    kw = {"family": "epa_blend", "n": 3, "seed": 5, "seasons": [2023, 2024], "top_k": 1,
          "limits": dict(DEFAULT_LIMITS)}
    progress: list[float] = []
    result = run_search(games, emit=lambda cp, p: progress.append(p), should_stop=lambda: False,
                        validation_seasons=[2025, 2025], **kw)
    assert result["evaluated"] == 3 and result["seasons"] == [2023, 2024] and len(result["top"]) == 1
    assert progress and progress[-1] == pytest.approx(1.0)
    for entry in result["create_models"]:
        assert entry["family"] == "epa_blend" and set(entry["params"]) == set(PARAM_KEYS)
        assert set(entry["backtest_metrics"]["blend"]) == set(FEATURE_NAMES)
        assert sentences(entry["summary"]) == 3 and entry["summary"].startswith("EPA blend (")
        assert entry["validation_metrics"]["seasons"] == [2025]
        assert entry["stress_metrics"] is not None
    again = run_search(games, emit=lambda cp, p: None, should_stop=lambda: False, **kw)
    assert again["top"] == result["top"], "deterministic for a seed"
