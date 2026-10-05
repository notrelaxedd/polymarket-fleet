"""The epa_blend family recovers known coefficients on synthetic data, and a heavy L2
shrinks the signals but leaves the market and the intercept free (docs/ROBUSTNESS.md
B2; step 6B contract section B)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from fleet.models import newton
from fleet.models.epa_blend import EpaBlend
from fleet.models.epa_features import FEATURE_NAMES
from fleet.sim.data import attach_team_stats, features_of
from fleet.sim.odds import devig, expit

PARAMS = {"window": 6, "shrink": 2.0, "l2": 0.5, "min_edge": 0.03, "kelly_fraction": 0.25}

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
