"""Walk-forward backtest by season (docs/MODELS.md, "Backtest").

For each test season S: replay Elo from the earliest game through S - 1, fit the blend on
the moneyline games of seasons < S (at least 3 such seasons), then walk S in kickoff order
predicting, betting and scoring each game before its result updates the ratings. One
test season is one checkpoint unit; the checkpoint is the per-season stats so far.
"""

from __future__ import annotations

from typing import Any, Callable

from fleet.models.registry import get_family
from fleet.sim.control import check_stop
from fleet.sim.data import complete_seasons, features_of, has_moneylines, outcome_of
from fleet.sim.fills import BetRule, plan_bet, settle
from fleet.sim.metrics import empty_stats, merge_stats, metrics_from_stats
from fleet.sim.odds import devig

MIN_HISTORY_SEASONS = 3
DEFAULT_SEASONS: list[int | None] = [2010, None]

Emit = Callable[[dict[str, Any], float], None]
ShouldStop = Callable[[], bool]


def season_plan(games: list[dict[str, Any]], seasons: list[int | None] | tuple[int | None, int | None] | None) -> list[int]:
    """The seasons a backtest over [first, last] evaluates (last None = last complete)."""
    first, last = (seasons or DEFAULT_SEASONS)[:2] if seasons else DEFAULT_SEASONS
    present = sorted({g["season"] for g in games})
    if last is None:
        complete = complete_seasons(games)
        if not complete:
            return []
        last = complete[-1]
    if first is None:
        first = present[0] if present else 0
    with_lines = sorted({g["season"] for g in games if has_moneylines(g)})
    out = []
    for season in present:
        if season < int(first) or season > int(last):
            continue
        if len([s for s in with_lines if s < season]) < MIN_HISTORY_SEASONS:
            continue
        out.append(season)
    return out


def run_fold(games: list[dict[str, Any]], family: str, params: dict[str, Any], season: int,
             limits: dict[str, Any], should_stop: ShouldStop) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One test season: (per-game records, blend coefficients). Records hold game_id, p,
    p_market, outcome, bet and pnl_cents for every scored (moneyline) game of the season."""
    model = get_family(family)(params)
    model.fit([g for g in games if g["season"] < season], None, should_stop)
    rule = BetRule.build(model.params, limits)
    records: list[dict[str, Any]] = []
    for game in (g for g in games if g["season"] == season):
        outcome = outcome_of(game)
        p_market = devig(game["home_moneyline"], game["away_moneyline"]) if has_moneylines(game) else None
        if outcome is not None and p_market is not None:
            p = model.predict(game, p_market, features_of(game))
            bet = plan_bet(p, p_market, rule)
            pnl = settle(bet, outcome)[1] if bet else 0
            records.append({"game_id": game["game_id"], "p": p, "p_market": p_market,
                            "outcome": outcome, "bet": bet, "pnl_cents": pnl})
        model.observe(game)
    blend = dict(getattr(model, "blend", {}))
    return records, blend


def stats_of(records: list[dict[str, Any]]) -> dict[str, Any]:
    from fleet.sim.metrics import record_game

    stats = empty_stats()
    for r in records:
        record_game(stats, r["p"], r["p_market"], r["outcome"], r["bet"], r["pnl_cents"])
    return stats


def _resume(checkpoint: dict[str, Any] | None, seasons: list[int]) -> list[dict[str, Any]]:
    """The per-season entries of a checkpoint when they are a prefix of this run."""
    if not checkpoint:
        return []
    done = checkpoint.get("per_season") or []
    if [e.get("season") for e in done] != seasons[:len(done)]:
        return []
    return list(done)


def assemble(per_season: list[dict[str, Any]], limits: dict[str, Any]) -> dict[str, Any]:
    """The result: whole-backtest metrics (plus the last blend) and per_season metrics."""
    seasons = [e["season"] for e in per_season]
    result = metrics_from_stats(merge_stats([e["stats"] for e in per_season]), limits, seasons)
    result["blend"] = dict(per_season[-1]["blend"]) if per_season else {}
    result["per_season"] = [
        {"season": e["season"], **metrics_from_stats(e["stats"], limits, [e["season"]]), "blend": dict(e["blend"])}
        for e in per_season
    ]
    return result


def run_backtest(games: list[dict[str, Any]], family: str, params: dict[str, Any],
                 seasons: list[int | None] | None, limits: dict[str, Any], emit: Emit,
                 should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Walk-forward backtest; emits {"next": i, "per_season": [...]} after every season."""
    get_family(family)
    plan = season_plan(games, seasons)
    per_season = _resume(checkpoint, plan)
    for index in range(len(per_season), len(plan)):
        check_stop(should_stop)
        season = plan[index]
        records, blend = run_fold(games, family, params, season, limits, should_stop)
        per_season.append({"season": season, "stats": stats_of(records), "blend": blend})
        emit({"next": index + 1, "per_season": per_season}, (index + 1) / len(plan))
    return assemble(per_season, limits)
