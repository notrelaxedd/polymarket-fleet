"""Snapshot replay integration rules (docs/ROBUSTNESS.md B1, step 6B decisions): a null
last season replays through the latest season present, the season in progress
included, on the worker and on the host; the result carries a top-level `avg_clv`
(the plain mean CLV over its bets)."""
from __future__ import annotations

from typing import Any

import pytest

from fleet.sim.backtest import season_plan
from fleet.sim.prices import Replay
from host.jobparams import prepare_params
from tests.conftest import set_setting
from tests.test_replay import PLATFORM, _records, _run, game_markets, games, layout  # noqa: F401 (fixtures)


def _in_progress(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The fixture with the last 40 games of 2025 not played yet."""
    out = [dict(g) for g in games]
    last = [g for g in out if g["season"] == 2025][-40:]
    for g in last:
        g["home_score"] = g["away_score"] = None
    return out


def test_season_plan_runs_through_the_season_in_progress_only_for_a_replay(games: list[dict]) -> None:
    current = _in_progress(games)
    assert season_plan(current, [2023, None])[-1] == 2024, "closing line: the last complete season"
    assert season_plan(current, [2023, None], through_latest=True) == [2023, 2024, 2025]
    assert season_plan(current, [2023, 2024], through_latest=True) == [2023, 2024], "an explicit last stays"


def test_replay_scores_the_played_games_of_the_season_in_progress(games: list[dict]) -> None:
    current = _in_progress(games)
    season = [g for g in current if g["season"] == 2025]
    played = [g for g in season if g["home_score"] is not None]
    markets: list[dict[str, Any]] = []
    for game in played[:30] + [g for g in season if g["home_score"] is None][:5]:
        markets += game_markets(game, [-10])
    result = _run(current, Replay(markets, PLATFORM), [2024, None])
    assert result["seasons"] == [2025], "2024 has no recorded market; 2025 is in progress and still replayed"
    assert result["n_games"] == 30, "unplayed games are not scored"
    assert result["n_unscored_no_prices"] == len(played) - 30


def test_snapshot_result_reports_the_plain_mean_clv(games: list[dict], layout: dict) -> None:
    replay = Replay(layout["markets"], PLATFORM)
    result = _run(games, replay)
    clvs = [r["bet"]["clv"] for r in _records(games, replay, [2024, 2024]) if r["bet"]]
    assert clvs and result["avg_clv"] == pytest.approx(sum(clvs) / len(clvs))
    empty = _run(games, Replay([], PLATFORM))
    assert empty["n_bets"] == 0 and empty["avg_clv"] is None
    closing = _run(games, None, [2024, 2024])
    assert "avg_clv" not in closing and "price_source" not in closing, "closing-line results are unchanged"


def _seasons(conn: Any, last_played: bool) -> None:
    conn.execute("DELETE FROM games")
    for season in range(2019, 2026):
        status = "final" if season < 2025 or last_played else "scheduled"
        conn.execute(
            "INSERT INTO games (game_id, season, game_type, week, gameday, kickoff_at, home_team, away_team, status,"
            " home_moneyline, away_moneyline, raw) VALUES (%s, %s, 'REG', 1, %s, %s, 'KC', 'LV', %s, -150, 130, '{}')",
            (f"{season}_01_LV_KC", season, f"{season}-09-09", f"{season}-09-10T00:20:00Z", status),
        )


def test_host_resolves_a_null_last_season_to_the_latest_for_snapshots(conn) -> None:
    _seasons(conn, last_played=False)
    snap = prepare_params(conn, "backtest", {"family": "elo_blend", "params": {}, "price_source": "snapshots",
                                             "seasons": [2023, None]})
    assert snap["seasons"] == [2023, 2025], "the season in progress is included"
    closing = prepare_params(conn, "backtest", {"family": "elo_blend", "params": {}, "seasons": [2023, None]})
    assert closing["seasons"] == [2023, 2024], "closing line keeps the last complete season"
    set_setting(conn, "backtest_seasons", [2022, None])
    set_setting(conn, "validation_seasons", [2024, None])
    copied = prepare_params(conn, "backtest", {"family": "elo_blend", "params": {}, "price_source": "snapshots"})
    assert copied["backtest_seasons"] == [2022, 2025], "no validation cap: a replay selects nothing"
    plain = prepare_params(conn, "backtest", {"family": "elo_blend", "params": {}})
    assert plain["backtest_seasons"] == [2022, 2023], "closing line: capped before the validation era"
    explicit = prepare_params(conn, "backtest", {"family": "elo_blend", "params": {}, "price_source": "snapshots",
                                                 "seasons": [2023, 2024]})
    assert explicit["seasons"] == [2023, 2024]
