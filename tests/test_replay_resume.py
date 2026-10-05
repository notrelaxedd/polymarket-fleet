"""Snapshot replay checkpoints (docs/ROBUSTNESS.md B1): a resume from every checkpoint
gives exactly the uninterrupted result, the finished seasons are rebuilt from the price
facts stored in the checkpoint (not from the prices file), and a checkpoint of the
other price source restarts the run. Fixture based, no database."""
from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from fleet.sim.backtest import records_of, run_backtest
from fleet.sim.control import JobStopped
from fleet.sim.data import load_games
from fleet.sim.prices import Replay
from tests.test_replay import FIXTURE, LIMITS, PARAMS, PLATFORM, game_markets, played

SEASONS = [2022, 2024]


@pytest.fixture(scope="module")
def games() -> list[dict]:
    return load_games(FIXTURE)


def _markets(games: list[dict], close_shift: float = 0.03, offsets: list[float] | None = None) -> list[dict[str, Any]]:
    """Recorded markets for 40 played games of each of 2022, 2023 and 2024, plus 10
    games per season whose bars are too early to score."""
    out: list[dict[str, Any]] = []
    for season in (2022, 2023, 2024):
        g = played(games, season)
        out += [m for game in g[:40] for m in game_markets(game, offsets or [-10], close_shift=close_shift)]
        out += [m for game in g[40:50] for m in game_markets(game, [-50], close_shift=close_shift)]
    return out


def _run(games: list[dict], replay: Replay, checkpoint: dict | None = None,
         sink: list[dict] | None = None, stop_after: int | None = None) -> dict:
    emitted: list[dict] = [] if sink is None else sink

    def emit(cp: dict, _progress: float) -> None:
        emitted.append(json.loads(json.dumps(cp)))  # what the host stores and hands back

    def should_stop() -> bool:
        return stop_after is not None and len(emitted) >= stop_after

    return run_backtest(games, "elo_blend", PARAMS, SEASONS, LIMITS, emit, should_stop, checkpoint, replay=replay)


def _dump(result: dict) -> str:
    return json.dumps(result, sort_keys=True)


@pytest.fixture(scope="module")
def baseline(games: list[dict]) -> dict[str, Any]:
    replay = Replay(_markets(games), PLATFORM)
    checkpoints: list[dict] = []
    result = _run(games, replay, sink=checkpoints)
    return {"replay": replay, "result": result, "checkpoints": checkpoints}


def test_the_baseline_is_a_snapshot_run_over_three_seasons(baseline: dict) -> None:
    result = baseline["result"]
    assert result["seasons"] == [2022, 2023, 2024] and len(baseline["checkpoints"]) == 3
    assert result["n_games"] == 120 and result["n_bets"] > 0
    assert sum(s["n_unscored_no_prices"] for s in result["per_season"]) == result["n_unscored_no_prices"]
    assert all(s["n_unscored_no_prices"] > 10 for s in result["per_season"])
    for entry in baseline["checkpoints"][-1]["per_season"]:
        assert isinstance(entry["prices"], list) and len(entry["prices"]) == 40
        assert entry["n_unscored_no_prices"] >= 10


def test_resume_from_every_checkpoint_equals_the_uninterrupted_run(games: list[dict], baseline: dict) -> None:
    want = _dump(baseline["result"])
    assert _dump(_run(games, baseline["replay"])) == want, "deterministic"
    for checkpoint in baseline["checkpoints"]:
        assert _dump(_run(games, baseline["replay"], copy.deepcopy(checkpoint))) == want


def test_a_stopped_run_resumes_exactly(games: list[dict], baseline: dict) -> None:
    sink: list[dict] = []
    with pytest.raises(JobStopped):
        _run(games, baseline["replay"], sink=sink, stop_after=2)
    assert len(sink) == 2
    assert _dump(_run(games, baseline["replay"], sink[-1])) == _dump(baseline["result"])


def test_finished_seasons_come_from_the_stored_facts_not_the_prices_file(games: list[dict], baseline: dict) -> None:
    """The prices file changes after the first season was checkpointed (other closes):
    the resumed run keeps the stored first season and replays the rest on the new file."""
    first = baseline["checkpoints"][0]
    moved = Replay(_markets(games, close_shift=-0.04), PLATFORM)
    resumed = _run(games, moved, copy.deepcopy(first))
    fresh = _run(games, moved)
    assert resumed["per_season"][0] == baseline["result"]["per_season"][0]
    assert resumed["per_season"][1:] == fresh["per_season"][1:]
    per_season = baseline["checkpoints"][-1]["per_season"]
    rebuilt = records_of(games, "elo_blend", PARAMS, per_season, LIMITS)
    clvs = [r["bet"]["clv"] for season in rebuilt for r in season if r["bet"]]
    assert clvs and all(c == pytest.approx(0.03 - 0.01) for c in clvs), "rebuilt from the stored closes"


def test_a_checkpoint_of_the_other_price_source_restarts(games: list[dict], baseline: dict) -> None:
    closing: list[dict] = []
    run_backtest(games, "elo_blend", PARAMS, SEASONS, LIMITS, lambda cp, p: closing.append(json.loads(json.dumps(cp))),
                 lambda: False)
    assert _dump(_run(games, baseline["replay"], closing[0])) == _dump(baseline["result"])
    snapshot_cp = baseline["checkpoints"][0]
    replay_free = run_backtest(games, "elo_blend", PARAMS, SEASONS, LIMITS, lambda cp, p: None, lambda: False,
                               copy.deepcopy(snapshot_cp))
    plain = run_backtest(games, "elo_blend", PARAMS, SEASONS, LIMITS, lambda cp, p: None, lambda: False)
    assert _dump(replay_free) == _dump(plain)
