"""Walk-forward backtest by season (docs/MODELS.md, "Backtest"; docs/ROBUSTNESS.md, A2).

For each test season S: replay Elo from the earliest game through S - 1, fit the blend on
the moneyline games of seasons < S (at least 3 such seasons), then walk S in kickoff order
predicting, betting and scoring each game before its result updates the ratings. One
test season is one checkpoint unit; the checkpoint holds, per finished season, the
sufficient statistics, the blend and the packed model probabilities of its scored
games (fleet.sim.records), from which rebuild_records recovers every per-game record,
so a resumed run finishes with exactly the metrics of an uninterrupted one, including
the resampling fields (fleet.sim.robust) that need every scored game. An entry without
packed probabilities (an older worker's checkpoint) restarts the run.

Snapshot replay (docs/ROBUSTNESS.md B1, a fleet.sim.prices.Replay passed as `replay`):
the plan keeps the seasons with a recorded market for one of their games; a played
game is scored only when Replay.facts finds a bar near its decision time (the others
count in n_unscored_no_prices), p_market is the devigged decision-time mid and the
bet fills on the recorded book. A season entry then also holds "prices" (the facts of
its scored games, in order) and "n_unscored_no_prices", so a resume rebuilds the
finished seasons without the prices file. The result gains "price_source":
"snapshots", "platform" and "n_unscored_no_prices"; closing-line results are unchanged.
"""

from __future__ import annotations

from typing import Any, Callable

from fleet.models.registry import get_family
from fleet.sim.control import check_stop
from fleet.sim.data import complete_seasons, features_of, has_moneylines, outcome_of
from fleet.sim.prices import Replay, p_market_of
from fleet.sim.fills import BetRule
from fleet.sim.metrics import empty_stats, merge_stats, metrics_from_stats, record_game
from fleet.sim.odds import devig
from fleet.sim.records import bet_rows, build_record, build_snapshot_record, pack_probs, unpack_probs
from fleet.sim.robust import robust_fields

MIN_HISTORY_SEASONS = 3
DEFAULT_SEASONS: list[int | None] = [2010, None]
ERA_SEARCH = "search"
ERA_VALIDATION = "validation"
PRICE_SOURCE_SNAPSHOTS = "snapshots"

Emit = Callable[[dict[str, Any], float], None]
ShouldStop = Callable[[], bool]


def season_plan(games: list[dict[str, Any]], seasons: list[int | None] | tuple[int | None, int | None] | None,
                through_latest: bool = False) -> list[int]:
    """The seasons a backtest over [first, last] evaluates (last None = last complete,
    or with `through_latest` (a snapshot replay) the latest season present, the season
    in progress included)."""
    first, last = (seasons or DEFAULT_SEASONS)[:2] if seasons else DEFAULT_SEASONS
    present = sorted({g["season"] for g in games})
    if last is None and through_latest:
        last = present[-1] if present else None
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


def scored_pair(game: dict[str, Any]) -> tuple[float, float] | None:
    """(p_market, outcome) when the game is scored: both moneylines and a result."""
    outcome = outcome_of(game)
    p_market = devig(game["home_moneyline"], game["away_moneyline"]) if has_moneylines(game) else None
    if outcome is None or p_market is None:
        return None
    return p_market, outcome


def fill_rule(family: str, params: dict[str, Any], limits: dict[str, Any]) -> BetRule:
    """The fill rule of a model (its params with the family defaults) under the limits."""
    return BetRule.build(get_family(family)(params).params, limits)


def run_fold(games: list[dict[str, Any]], family: str, params: dict[str, Any], season: int,
             limits: dict[str, Any], should_stop: ShouldStop) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One test season: (per-game records, blend coefficients). Records (fleet.sim.records)
    cover every scored (moneyline) game of the season in kickoff order."""
    model = get_family(family)(params)
    model.fit([g for g in games if g["season"] < season], None, should_stop)
    rule = BetRule.build(model.params, limits)
    records: list[dict[str, Any]] = []
    for game in (g for g in games if g["season"] == season):
        scored = scored_pair(game)
        if scored is not None:
            p_market, outcome = scored
            p = model.predict(game, p_market, features_of(game))
            records.append(build_record(game, p, p_market, outcome, rule))
        model.observe(game)
    blend = dict(getattr(model, "blend", {}))
    return records, blend


def run_replay_fold(games: list[dict[str, Any]], family: str, params: dict[str, Any], season: int,
                    limits: dict[str, Any], should_stop: ShouldStop,
                    replay: Replay) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    """One test season on recorded prices: (records, blend, n_unscored_no_prices).
    Records cover the played games with decision-time prices, in kickoff order."""
    model = get_family(family)(params)
    model.fit([g for g in games if g["season"] < season], None, should_stop)
    rule = BetRule.build(model.params, limits)
    records: list[dict[str, Any]] = []
    unscored = 0
    for game in (g for g in games if g["season"] == season):
        outcome = outcome_of(game)
        if outcome is not None:
            facts = replay.facts(game)
            if facts is None:
                unscored += 1
            else:
                p = model.predict(game, p_market_of(facts), features_of(game))
                records.append(build_snapshot_record(game, p, facts, outcome, rule))
        model.observe(game)
    return records, dict(getattr(model, "blend", {})), unscored


def _rebuild_snapshot(games: list[dict[str, Any]], season: int, probs: list[float], prices: list[Any],
                      rule: BetRule) -> list[dict[str, Any]]:
    if len(prices) != len(probs):
        raise ValueError(f"season {season}: {len(probs)} stored probabilities for {len(prices)} stored price facts")
    by_id = {g["game_id"]: g for g in games if g["season"] == season}
    out = []
    for facts, p in zip(prices, probs):
        game = by_id.get(facts.get("game_id")) if isinstance(facts, dict) else None
        outcome = outcome_of(game) if game is not None else None
        if game is None or outcome is None:
            raise ValueError(f"season {season}: stored price facts name an unknown or unplayed game")
        out.append(build_snapshot_record(game, p, facts, outcome, rule))
    return out


def rebuild_records(games: list[dict[str, Any]], season: int, packed: str, rule: BetRule,
                    prices: list[Any] | None = None) -> list[dict[str, Any]]:
    """The records of a finished season from its packed probabilities: the same
    scored games in the same order under the same fill rule give the same records.
    With `prices` (a snapshot replay entry's facts) the scored games are those facts."""
    probs = unpack_probs(packed)
    if prices is not None:
        return _rebuild_snapshot(games, season, probs, prices, rule)
    scored = [(g, pair) for g in games if g["season"] == season for pair in (scored_pair(g),) if pair is not None]
    if len(scored) != len(probs):
        raise ValueError(f"season {season}: {len(probs)} stored probabilities for {len(scored)} scored games")
    return [build_record(g, p, p_market, outcome, rule) for (g, (p_market, outcome)), p in zip(scored, probs)]


def stats_of(records: list[dict[str, Any]]) -> dict[str, Any]:
    stats = empty_stats()
    for r in records:
        record_game(stats, r["p"], r["p_market"], r["outcome"], r["bet"], r["pnl_cents"])
    return stats


def season_entry(season: int, records: list[dict[str, Any]], blend: dict[str, Any],
                 n_unscored: int | None = None) -> dict[str, Any]:
    """The checkpoint entry of a finished test season (n_unscored given: a snapshot
    replay season, which also keeps the price facts of its scored games)."""
    entry = {"season": season, "stats": stats_of(records), "blend": blend, "records": pack_probs(records)}
    if n_unscored is not None:
        entry["prices"] = [r["prices"] for r in records]
        entry["n_unscored_no_prices"] = int(n_unscored)
    return entry


def _resume(checkpoint: dict[str, Any] | None, seasons: list[int], snapshot: bool = False) -> list[dict[str, Any]]:
    """The per-season entries of a checkpoint when they are a prefix of this run (an
    entry without packed probabilities, from an older worker, or from the other price
    source restarts the run)."""
    if not checkpoint:
        return []
    done = checkpoint.get("per_season") or []
    if [e.get("season") for e in done] != seasons[:len(done)]:
        return []
    if any(not isinstance(e.get("records"), str) for e in done):
        return []
    if any(isinstance(e.get("prices"), list) != snapshot for e in done):
        return []
    return list(done)


def assemble(games: list[dict[str, Any]], family: str, params: dict[str, Any], per_season: list[dict[str, Any]],
             limits: dict[str, Any], era: str = ERA_SEARCH, seed: int | str = 1,
             replay: Replay | None = None) -> dict[str, Any]:
    """The result: whole-backtest metrics (plus the last blend and the robustness
    fields, computed from the records rebuilt for (family, params)) and per_season
    metrics; a snapshot replay adds price_source, platform and n_unscored_no_prices."""
    seasons = [e["season"] for e in per_season]
    result = metrics_from_stats(merge_stats([e["stats"] for e in per_season]), limits, seasons)
    result["blend"] = dict(per_season[-1]["blend"]) if per_season else {}
    result["per_season"] = [
        {"season": e["season"], **metrics_from_stats(e["stats"], limits, [e["season"]]), "blend": dict(e["blend"])}
        for e in per_season
    ]
    records = records_of(games, family, params, per_season, limits)
    result.update(robust_fields(records, limits, seed, era, result))
    if replay is not None:
        for entry, row in zip(per_season, result["per_season"]):
            row["n_unscored_no_prices"] = int(entry.get("n_unscored_no_prices", 0))
        result["price_source"] = PRICE_SOURCE_SNAPSHOTS
        result["platform"] = replay.platform
        result["n_unscored_no_prices"] = sum(int(e.get("n_unscored_no_prices", 0)) for e in per_season)
        clvs = [row[4] for row in bet_rows([r for season in records for r in season])]
        result["avg_clv"] = sum(clvs) / len(clvs) if clvs else None  # plain mean over bets
    return result


def records_of(games: list[dict[str, Any]], family: str, params: dict[str, Any], per_season: list[dict[str, Any]],
               limits: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """The records per season of a finished run's checkpoint entries, rebuilt under
    the model's fill rule."""
    rule = fill_rule(family, params, limits)
    return [rebuild_records(games, e["season"], e["records"], rule, e.get("prices")) for e in per_season]


def run_backtest(games: list[dict[str, Any]], family: str, params: dict[str, Any],
                 seasons: list[int | None] | None, limits: dict[str, Any], emit: Emit,
                 should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None,
                 era: str = ERA_SEARCH, seed: int | str = 1, replay: Replay | None = None) -> dict[str, Any]:
    """Walk-forward backtest; emits {"next": i, "per_season": [...]} after every season.
    The result is the metrics object with per_season, labelled with the era; with a
    replay, on the recorded prices."""
    per_season = run_seasons(games, family, params, seasons, limits, emit, should_stop, checkpoint, replay)
    return assemble(games, family, params, per_season, limits, era, seed, replay)


def run_seasons(games: list[dict[str, Any]], family: str, params: dict[str, Any],
                seasons: list[int | None] | None, limits: dict[str, Any], emit: Emit,
                should_stop: ShouldStop, checkpoint: dict[str, Any] | None = None,
                replay: Replay | None = None) -> list[dict[str, Any]]:
    """The per-season checkpoint entries of a backtest (run or resumed), the raw form
    the validation needs for its base run."""
    get_family(family)
    plan = season_plan(games, seasons, through_latest=replay is not None)
    if replay is not None:
        recorded = {g["season"] for g in games if replay.has_game(g["game_id"])}
        plan = [season for season in plan if season in recorded]
    per_season = _resume(checkpoint, plan, replay is not None)
    for index in range(len(per_season), len(plan)):
        check_stop(should_stop)
        season = plan[index]
        if replay is None:
            records, blend = run_fold(games, family, params, season, limits, should_stop)
            per_season.append(season_entry(season, records, blend))
        else:
            records, blend, unscored = run_replay_fold(games, family, params, season, limits, should_stop, replay)
            per_season.append(season_entry(season, records, blend, unscored))
        emit({"next": index + 1, "per_season": per_season}, (index + 1) / len(plan))
    return per_season
