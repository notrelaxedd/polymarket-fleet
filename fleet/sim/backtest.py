"""Walk-forward backtest by season (docs/MODELS.md, "Backtest"; docs/ROBUSTNESS.md, A2).

For each test season S: replay Elo from the earliest game through S - 1, fit the blend on
the moneyline games of seasons < S (at least 3 such seasons), then walk S in kickoff order
predicting, betting and scoring each game before its result updates the ratings. One
test season is one checkpoint unit; the checkpoint holds, per finished season, the
sufficient statistics, the blend and the packed per-game records (fleet.sim.records),
so a resumed run finishes with exactly the metrics of an uninterrupted one, including
the resampling fields (fleet.sim.robust) that need every scored game.
"""

from __future__ import annotations

from typing import Any, Callable

from fleet.models.registry import get_family
from fleet.sim.control import check_stop
from fleet.sim.data import complete_seasons, features_of, has_moneylines, outcome_of
from fleet.sim.fills import BetRule
from fleet.sim.metrics import empty_stats, merge_stats, metrics_from_stats, record_game
from fleet.sim.odds import devig
from fleet.sim.records import build_record, pack_all, unpack_all
from fleet.sim.robust import robust_fields

MIN_HISTORY_SEASONS = 3
DEFAULT_SEASONS: list[int | None] = [2010, None]
ERA_SEARCH = "search"
ERA_VALIDATION = "validation"

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
    """One test season: (per-game records, blend coefficients). Records (fleet.sim.records)
    cover every scored (moneyline) game of the season in kickoff order."""
    model = get_family(family)(params)
    model.fit([g for g in games if g["season"] < season], None, should_stop)
    rule = BetRule.build(model.params, limits)
    records: list[dict[str, Any]] = []
    for game in (g for g in games if g["season"] == season):
        outcome = outcome_of(game)
        p_market = devig(game["home_moneyline"], game["away_moneyline"]) if has_moneylines(game) else None
        if outcome is not None and p_market is not None:
            p = model.predict(game, p_market, features_of(game))
            records.append(build_record(game, p, p_market, outcome, rule))
        model.observe(game)
    blend = dict(getattr(model, "blend", {}))
    return records, blend


def stats_of(records: list[dict[str, Any]]) -> dict[str, Any]:
    stats = empty_stats()
    for r in records:
        record_game(stats, r["p"], r["p_market"], r["outcome"], r["bet"], r["pnl_cents"])
    return stats


def season_entry(season: int, records: list[dict[str, Any]], blend: dict[str, Any]) -> dict[str, Any]:
    """The checkpoint entry of a finished test season."""
    return {"season": season, "stats": stats_of(records), "blend": blend, "records": pack_all(records)}


def _resume(checkpoint: dict[str, Any] | None, seasons: list[int]) -> list[dict[str, Any]]:
    """The per-season entries of a checkpoint when they are a prefix of this run (an
    entry without records, from an older worker, restarts the run)."""
    if not checkpoint:
        return []
    done = checkpoint.get("per_season") or []
    if [e.get("season") for e in done] != seasons[:len(done)]:
        return []
    if any(not isinstance(e.get("records"), list) for e in done):
        return []
    return list(done)


def assemble(per_season: list[dict[str, Any]], limits: dict[str, Any], era: str = ERA_SEARCH,
             seed: int | str = 1) -> dict[str, Any]:
    """The result: whole-backtest metrics (plus the last blend and the robustness
    fields) and per_season metrics."""
    seasons = [e["season"] for e in per_season]
    result = metrics_from_stats(merge_stats([e["stats"] for e in per_season]), limits, seasons)
    result["blend"] = dict(per_season[-1]["blend"]) if per_season else {}
    result["per_season"] = [
        {"season": e["season"], **metrics_from_stats(e["stats"], limits, [e["season"]]), "blend": dict(e["blend"])}
        for e in per_season
    ]
    records = [unpack_all(e["records"], e["season"]) for e in per_season]
    result.update(robust_fields(records, limits, seed, era, result))
    return result


def records_of(per_season: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The unpacked records per season of a finished run's checkpoint entries."""
    return [unpack_all(e["records"], e["season"]) for e in per_season]


def run_backtest(games: list[dict[str, Any]], family: str, params: dict[str, Any],
                 seasons: list[int | None] | None, limits: dict[str, Any], emit: Emit,
                 should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None,
                 era: str = ERA_SEARCH, seed: int | str = 1) -> dict[str, Any]:
    """Walk-forward backtest; emits {"next": i, "per_season": [...]} after every season.
    The result is the metrics object with per_season, labelled with the era."""
    per_season = run_seasons(games, family, params, seasons, limits, emit, should_stop, checkpoint)
    return assemble(per_season, limits, era, seed)


def run_seasons(games: list[dict[str, Any]], family: str, params: dict[str, Any],
                seasons: list[int | None] | None, limits: dict[str, Any], emit: Emit,
                should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The per-season checkpoint entries of a backtest (run or resumed), the raw form
    the validation needs for its base run."""
    get_family(family)
    plan = season_plan(games, seasons)
    per_season = _resume(checkpoint, plan)
    for index in range(len(per_season), len(plan)):
        check_stop(should_stop)
        season = plan[index]
        records, blend = run_fold(games, family, params, season, limits, should_stop)
        per_season.append(season_entry(season, records, blend))
        emit({"next": index + 1, "per_season": per_season}, (index + 1) / len(plan))
    return per_season
