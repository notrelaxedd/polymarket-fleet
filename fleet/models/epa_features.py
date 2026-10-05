"""Feature construction of the epa_blend family (docs/MODELS.md, "epa_blend").

The feature vector of a game, in FEATURE_NAMES order:
intercept 1, Elo diff / 400 (no home edge: the intercept carries it), shrunk rolling
offensive EPA per play diff, shrunk rolling defensive EPA allowed per play diff, rest
diff (clamped to +-3 days, divided by 3), qb_changed diff, out_count diff, divisional
(0/1), market logit. Every diff is home minus away.

Rolling EPA: the mean of a team's last `window` team_stats rows (all strictly before
the game), shrunk toward the league mean by n / (n + shrink). The league mean comes
from LeagueMean, which only ever sees rows attached to games already processed, so a
prediction never uses anything from the predicted game or later.
"""

from __future__ import annotations

from typing import Any

from fleet.models.elo import clamp
from fleet.sim.odds import logit
from fleet.sim.signals import normalise_signals

FEATURE_NAMES = ("intercept", "elo", "off_epa", "def_epa", "rest", "qb_change", "outs", "divisional", "market")
MARKET_INDEX = FEATURE_NAMES.index("market")
UNPENALISED = (0, MARKET_INDEX)  # L2 shrinks the signals toward "the closing line alone"
REST_CLAMP = 3


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


class LeagueMean:
    """Running league means of offensive and defensive EPA per play over the rows seen
    so far, from the current and the previous season (the scoring environment drifts)."""

    def __init__(self) -> None:
        self.seasons: dict[int, list[float]] = {}  # season -> [rows, off sum, def sum]
        self.last: dict[str, list[str]] = {}  # team -> [kickoff_at, game_id] of its newest row counted

    def absorb(self, team_stats: dict[str, list[dict[str, Any]]]) -> None:
        """Count every row of a game's team_stats not counted before (rows are oldest
        first and strictly before that game)."""
        for side in ("home", "away"):
            for row in team_stats.get(side) or []:
                team = str(row.get("team") or "")
                key = [str(row.get("kickoff_at") or ""), str(row.get("game_id") or "")]
                off, dfn = _float(row.get("off_epa_per_play")), _float(row.get("def_epa_per_play"))
                if not team or off is None or dfn is None:
                    continue
                last = self.last.get(team)
                if last is not None and key <= last:
                    continue
                self.last[team] = key
                try:
                    season = int(row.get("season"))
                except (TypeError, ValueError):
                    continue
                sums = self.seasons.setdefault(season, [0.0, 0.0, 0.0])
                sums[0] += 1.0
                sums[1] += off
                sums[2] += dfn

    def mean(self, season: int) -> tuple[float, float]:
        n = off = dfn = 0.0
        for s in (season - 1, season):
            sums = self.seasons.get(s)
            if sums:
                n += sums[0]
                off += sums[1]
                dfn += sums[2]
        return (off / n, dfn / n) if n else (0.0, 0.0)

    def to_json(self) -> dict[str, Any]:
        return {"seasons": {str(s): list(v) for s, v in sorted(self.seasons.items())},
                "last": {t: list(v) for t, v in sorted(self.last.items())}}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "LeagueMean":
        out = cls()
        data = data if isinstance(data, dict) else {}
        out.seasons = {int(s): [float(x) for x in v] for s, v in (data.get("seasons") or {}).items()}
        out.last = {str(t): [str(x) for x in v] for t, v in (data.get("last") or {}).items()}
        return out


def rolling(rows: list[dict[str, Any]], window: int, shrink: float, league: tuple[float, float]) -> tuple[float, float]:
    """(offensive, defensive) EPA per play over the last `window` rows, shrunk toward
    the league mean by n / (n + shrink); the league mean itself with no rows."""
    offs: list[float] = []
    defs: list[float] = []
    for row in rows[-window:] if window > 0 else []:
        off, dfn = _float(row.get("off_epa_per_play")), _float(row.get("def_epa_per_play"))
        if off is not None and dfn is not None:
            offs.append(off)
            defs.append(dfn)
    n = len(offs)
    if n == 0:
        return league
    weight = n / (n + max(0.0, shrink))
    raw_off, raw_def = sum(offs) / n, sum(defs) / n
    return (league[0] + weight * (raw_off - league[0]), league[1] + weight * (raw_def - league[1]))


def rest_diff(game: dict[str, Any]) -> float:
    home, away = game.get("home_rest"), game.get("away_rest")
    if home is None or away is None:
        return 0.0
    return clamp(float(home) - float(away), -REST_CLAMP, REST_CLAMP) / REST_CLAMP


def feature_vector(game: dict[str, Any], features: dict[str, Any], elo_diff: float, window: int, shrink: float,
                   league: tuple[float, float], p_market: float) -> list[float]:
    """The FEATURE_NAMES row of one game (see the module docstring)."""
    stats = features.get("team_stats") or {}
    home_off, home_def = rolling(stats.get("home") or [], window, shrink, league)
    away_off, away_def = rolling(stats.get("away") or [], window, shrink, league)
    s = normalise_signals(features.get("signals"))
    div = features.get("div_game", game.get("div_game"))
    return [
        1.0,
        elo_diff / 400.0,
        home_off - away_off,
        home_def - away_def,
        rest_diff({"home_rest": features.get("home_rest", game.get("home_rest")),
                   "away_rest": features.get("away_rest", game.get("away_rest"))}),
        float(s["home_qb_changed"] - s["away_qb_changed"]),
        float(s["home_out_count"] - s["away_out_count"]),
        1.0 if div else 0.0,
        logit(p_market),
    ]
