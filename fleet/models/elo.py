"""The Elo rating engine of the elo_blend family (docs/MODELS.md, "First family")."""

from __future__ import annotations

import math
from typing import Any

from fleet.sim.data import outcome_of

START_RATING = 1500.0
REST_CLAMP = 3


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def rest_adjustment(rest_per_day: float, home_rest: int | None, away_rest: int | None) -> float:
    if home_rest is None or away_rest is None:
        return 0.0
    return rest_per_day * clamp(home_rest - away_rest, -REST_CLAMP, REST_CLAMP)


def expected_home(r_home: float, r_away: float, hfa: float, rest_adj: float) -> float:
    return 1.0 / (1.0 + 10.0 ** (-(r_home + hfa + rest_adj - r_away) / 400.0))


def mov_multiplier(mov_scale: float, margin: int, winner_advantage: float) -> float:
    """1 without margin scaling, else the 538-style log margin damped by the favourite's
    edge; the margin is floored at 1 so a tie still moves the ratings (ln 2)."""
    if mov_scale == 0:
        return 1.0
    return mov_scale * math.log(max(margin, 1) + 1) * 2.2 / (0.001 * winner_advantage + 2.2)


class Elo:
    """Mutable ratings; call begin_season before the first game of each season."""

    def __init__(self, k: float, hfa: float, regress: float, rest_per_day: float, mov_scale: float) -> None:
        self.k = float(k)
        self.hfa = float(hfa)
        self.regress = float(regress)
        self.rest_per_day = float(rest_per_day)
        self.mov_scale = float(mov_scale)
        self.ratings: dict[str, float] = {}
        self.season: int | None = None
        self.games_seen = 0

    def rating(self, team: str) -> float:
        return self.ratings.get(team, START_RATING)

    def begin_season(self, season: int) -> None:
        """Regress every rating toward 1500 when the season changes."""
        if self.season is None:
            self.season = season
            return
        if season == self.season:
            return
        keep = 1.0 - self.regress
        self.ratings = {t: START_RATING + (r - START_RATING) * keep for t, r in self.ratings.items()}
        self.season = season

    def expect(self, game: dict[str, Any]) -> float:
        """P(home win) before the game, with hfa and the clamped rest edge."""
        self.begin_season(game["season"])
        rest_adj = rest_adjustment(self.rest_per_day, game["home_rest"], game["away_rest"])
        return expected_home(self.rating(game["home_team"]), self.rating(game["away_team"]), self.hfa, rest_adj)

    def update(self, game: dict[str, Any]) -> float:
        """Apply a finished game's result; returns the home delta (0 for unplayed games,
        which do not count towards games_seen)."""
        self.begin_season(game["season"])
        outcome = outcome_of(game)
        if outcome is None:
            return 0.0
        self.games_seen += 1
        home, away = game["home_team"], game["away_team"]
        r_home, r_away = self.rating(home), self.rating(away)
        rest_adj = rest_adjustment(self.rest_per_day, game["home_rest"], game["away_rest"])
        e = expected_home(r_home, r_away, self.hfa, rest_adj)
        margin = abs(game["home_score"] - game["away_score"])
        home_adv = r_home + self.hfa + rest_adj - r_away
        if outcome == 1.0:
            winner_adv = home_adv
        elif outcome == 0.0:
            winner_adv = -home_adv
        else:
            winner_adv = 0.0
        delta = self.k * mov_multiplier(self.mov_scale, margin, winner_adv) * (outcome - e)
        self.ratings[home] = r_home + delta
        self.ratings[away] = r_away - delta
        return delta

    def state(self) -> dict[str, Any]:
        return {"ratings": dict(self.ratings), "season": self.season, "games_seen": self.games_seen}

    def load_state(self, ratings: dict[str, float], season: int | None, games_seen: int) -> None:
        self.ratings = {str(t): float(r) for t, r in ratings.items()}
        self.season = season
        self.games_seen = int(games_seen)
