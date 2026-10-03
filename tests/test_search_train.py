"""Model search (reproducible candidates, resume, ordering, create_models), training
(artifact, child shape, games_seen) and the worker job registry. Fixture based."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from fleet.models.base import params_hash
from fleet.models.elo_blend import EloBlend
from fleet.sim.control import JobStopped
from fleet.sim.data import load_games
from fleet.sim.metrics import shrunk_roi
from fleet.sim.search import candidate_params, run_search
from fleet.sim.train import run_train
from fleet.worker import jobs
from fleet.worker.jobs import DEFAULT_LIMITS, JOBS, limits_from_params

FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "games_sample.csv")
PARAMS = {"k": 24, "hfa": 55, "regress": 0.33, "rest_per_day": 1.0, "mov_scale": 1,
          "min_edge": 0.03, "kelly_fraction": 0.25}
LIMITS = dict(DEFAULT_LIMITS)
SEARCH_KW = dict(family="elo_blend", n=4, seed=11, seasons=[2010, None], top_k=3, limits=LIMITS)


@pytest.fixture(scope="module")
def games() -> list[dict]:
    return load_games(FIXTURE)


def sentences(text: str) -> int:
    return len(re.findall(r"[.!?](?=\s|$)", text))


# search --------------------------------------------------------------------------


def test_candidate_params_reproducible_from_seed() -> None:
    assert candidate_params("elo_blend", 3, 7) == candidate_params("elo_blend", 3, 7)
    assert candidate_params("elo_blend", 3, 7) != candidate_params("elo_blend", 3, 8)
    assert candidate_params("elo_blend", 3, 7) != candidate_params("elo_blend", 4, 7)
    with pytest.raises(ValueError):
        candidate_params("nope", 3, 7)


def test_search_result_shape_and_ordering(games: list[dict]) -> None:
    result = run_search(games, emit=lambda cp, p: None, should_stop=lambda: False, **SEARCH_KW)
    assert result["evaluated"] == 4 and result["seasons"] == list(range(2019, 2026))
    top = result["top"]
    assert len(top) == 3
    keys = [(-e["score"], e["metrics"]["log_loss"], e["index"]) for e in top]
    assert keys == sorted(keys)
    for e in top:
        assert e["params"] == candidate_params("elo_blend", 11, e["index"])
        assert e["params_hash"] == params_hash(e["params"])
        assert e["score"] == pytest.approx(shrunk_roi(e["metrics"]))
        assert "per_season" not in e["metrics"] and e["metrics"]["seasons"] == result["seasons"]
    created = result["create_models"]
    assert [set(c) for c in created] == [{"family", "params", "artifact", "backtest_metrics", "summary", "trained_through"}] * 3
    for c, e in zip(created, top):
        assert c["family"] == "elo_blend" and c["artifact"] is None and c["trained_through"] is None
        assert c["params"] == e["params"] and c["backtest_metrics"] == e["metrics"]
        assert sentences(c["summary"]) == 3 and c["summary"] == EloBlend.summary(e["params"], e["metrics"])
    json.dumps(result)


def test_search_top_ordering_prefers_shrunk_roi_then_log_loss() -> None:
    from fleet.sim.search import insert_top

    def entry(i: int, roi: float, bets: int, ll: float) -> dict:
        m = {"roi": roi, "n_bets": bets, "log_loss": ll}
        return {"index": i, "params": {}, "params_hash": "", "score": shrunk_roi(m), "metrics": m}

    top: list[dict] = []
    for e in (entry(0, 0.10, 50, 0.66), entry(1, 0.05, 400, 0.65), entry(2, 0.05, 400, 0.64), entry(3, -0.2, 10, 0.5)):
        top = insert_top(top, e, 3)
    assert [e["index"] for e in top] == [2, 1, 0]


def test_search_resume_equivalence(games: list[dict]) -> None:
    full = run_search(games, emit=lambda cp, p: None, should_stop=lambda: False, **SEARCH_KW)
    emitted: list[tuple[dict, float]] = []

    def emit(cp: dict, progress: float) -> None:
        emitted.append((copy.deepcopy(cp), progress))

    with pytest.raises(JobStopped):
        run_search(games, emit=emit, should_stop=lambda: len(emitted) >= 10, **SEARCH_KW)
    checkpoint, progress = emitted[-1]
    assert checkpoint["next"] == [1, 3] and checkpoint["evaluated"] == 1 and len(checkpoint["top"]) == 1
    assert checkpoint["current"]["next"] == 3 and progress == pytest.approx(10 / 28)
    assert set(checkpoint) == {"next", "current", "top", "evaluated"}
    resumed = run_search(games, emit=emit, should_stop=lambda: False, checkpoint=checkpoint, **SEARCH_KW)
    assert resumed == full
    progresses = [p for _, p in emitted]
    assert progresses == sorted(progresses) and progresses[-1] == pytest.approx(1.0)
    # resuming from a candidate boundary and from a finished search both work
    boundary = next(cp for cp, _ in emitted if cp["next"] == [2, 0])
    assert run_search(games, emit=lambda cp, p: None, should_stop=lambda: False, checkpoint=boundary, **SEARCH_KW) == full
    assert run_search(games, emit=lambda cp, p: None, should_stop=lambda: False, checkpoint=emitted[-1][0], **SEARCH_KW) == full


# train ------------------------------------------------------------------------------


def test_train_artifact_and_child_shape(games: list[dict]) -> None:
    parent = {"id": "parent-1", "lineage_id": "lin-1", "family": "elo_blend", "params": PARAMS}
    emitted: list[dict] = []
    result = run_train(games, parent, {"season": 2022, "week": 10}, lambda cp, p: emitted.append(cp), lambda: False)
    assert set(result) == {"create_models", "through", "games_seen"}
    assert result["through"] == [2022, 10]
    expected_seen = sum(1 for g in games if (g["season"], g["week"]) <= (2022, 10))
    assert result["games_seen"] == expected_seen
    child = result["create_models"][0]
    assert set(child) == {"family", "params", "artifact", "parent_model_id", "trained_through"}
    assert child["family"] == "elo_blend" and child["parent_model_id"] == "parent-1"
    assert child["trained_through"] == [2022, 10] and child["params"] == EloBlend(PARAMS).params
    artifact = child["artifact"]
    assert set(artifact) == {"ratings", "blend", "through", "season", "games_seen"}
    assert artifact["season"] == 2022
    assert artifact["through"] == [2022, 10] and artifact["games_seen"] == expected_seen
    assert len(artifact["ratings"]) == 32 and set(artifact["blend"]) == {"a", "b", "c"}
    assert [cp["season"] for cp in emitted] == list(range(2016, 2023))
    assert emitted[-1]["next"] == 7
    # the artifact restores a model that predicts the next game identically
    model = EloBlend(PARAMS)
    model.fit(games, (2022, 10), lambda: False)
    assert EloBlend.from_json(PARAMS, artifact).to_json() == model.to_json()
    json.dumps(result)


def test_train_rejects_missing_model_or_bad_through(games: list[dict]) -> None:
    with pytest.raises(ValueError):
        run_train(games, {}, {"season": 2022, "week": 1}, lambda cp, p: None, lambda: False)
    with pytest.raises(ValueError):
        run_train(games, {"id": "x", "family": "elo_blend", "params": {}}, "2022", lambda cp, p: None, lambda: False)


# worker jobs ----------------------------------------------------------------------


def test_registry_has_the_four_kinds() -> None:
    assert set(JOBS) == {"sleep", "backtest", "model_search", "train"}
    assert jobs.JobStopped is JobStopped


def test_limits_from_params_defaults_and_overrides() -> None:
    assert limits_from_params({}) == DEFAULT_LIMITS
    got = limits_from_params({"fee_model": {"taker_rate": 0.02}, "max_bet_cents": 500, "backtest_seasons": [2015, 2024]})
    assert got["fee_model"] == {"taker_rate": 0.02, "half_spread": 0.01}
    assert got["max_bet_cents"] == 500 and got["backtest_seasons"] == [2015, 2024]
    assert got["default_bankroll_cents"] == 10000 and got["trade_max_games"] == 6


def test_backtest_job_reads_context_and_limits(games: list[dict]) -> None:
    emitted: list[float] = []
    params = {"family": "elo_blend", "params": PARAMS, "seasons": [2022, 2023],
              "_context": {"games_path": FIXTURE, "model": None}}
    result = JOBS["backtest"](params, None, lambda cp, p: emitted.append(p), lambda: False)
    assert result["seasons"] == [2022, 2023] and emitted == [0.5, 1.0]
    model = {"id": "m1", "family": "elo_blend", "params": PARAMS, "artifact": None}
    by_id = {"model_id": "m1", "seasons": [2022, 2023], "_context": {"games_path": FIXTURE, "model": model}}
    assert JOBS["backtest"](by_id, None, lambda cp, p: None, lambda: False) == result
    default_span = JOBS["backtest"]({"family": "elo_blend", "params": PARAMS, "_context": {"games_path": FIXTURE}},
                                    None, lambda cp, p: None, lambda: False)
    assert default_span["seasons"] == list(range(2019, 2026))
    with pytest.raises(ValueError):
        JOBS["backtest"]({"family": "elo_blend", "params": PARAMS}, None, lambda cp, p: None, lambda: False)
    with pytest.raises(ValueError):
        JOBS["backtest"]({"model_id": "m1", "_context": {"games_path": FIXTURE, "model": None}}, None,
                         lambda cp, p: None, lambda: False)


def test_model_search_and_train_jobs(games: list[dict]) -> None:
    params = {"family": "elo_blend", "n": 2, "seed": 5, "seasons": [2021, 2022], "top_k": 2,
              "_context": {"games_path": FIXTURE, "model": None}}
    result = JOBS["model_search"](params, None, lambda cp, p: None, lambda: False)
    assert result["evaluated"] == 2 and len(result["create_models"]) == 2 and result["seasons"] == [2021, 2022]
    model = {"id": "m1", "family": "elo_blend", "params": PARAMS}
    trained = JOBS["train"]({"model_id": "m1", "through": {"season": 2021, "week": 3},
                             "_context": {"games_path": FIXTURE, "model": model}}, None, lambda cp, p: None, lambda: False)
    assert trained["through"] == [2021, 3] and trained["create_models"][0]["parent_model_id"] == "m1"
    with pytest.raises(ValueError):
        JOBS["train"]({"model_id": "m1", "through": {"season": 2021, "week": 3},
                       "_context": {"games_path": FIXTURE, "model": None}}, None, lambda cp, p: None, lambda: False)


def test_backtest_job_via_json_cache(tmp_path: Path, games: list[dict]) -> None:
    """The agent's cache is JSON in the MODELS.md field set; it loads like the csv."""
    cache = tmp_path / "games.json"
    cache.write_text(json.dumps(games))
    assert load_games(str(cache)) == games
    params = {"family": "elo_blend", "params": PARAMS, "seasons": [2024, 2024],
              "_context": {"games_path": str(cache), "model": None}}
    from_json = JOBS["backtest"](params, None, lambda cp, p: None, lambda: False)
    params["_context"]["games_path"] = FIXTURE
    assert from_json == JOBS["backtest"](params, None, lambda cp, p: None, lambda: False)
