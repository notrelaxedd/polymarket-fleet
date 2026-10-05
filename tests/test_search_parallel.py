"""Multi-core search (fleet.sim.parallel, fleet.sim.search with workers > 1): byte-identical
results and candidate-boundary checkpoints against the single-process run, SIGTERM
through the real runner child mid-search then an exact resume, no stray workers, and
the process group's memory. Fixture based, no database."""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

import pytest

from fleet.common import sysinfo
from fleet.sim.data import load_games
from fleet.sim.parallel import resolve_workers
from fleet.sim.search import run_search
from fleet.worker.jobs import DEFAULT_LIMITS
from fleet.worker.runner import Runner

FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "games_sample.csv")
ZERO_FEES = dict(DEFAULT_LIMITS, fee_model={"taker_rate": 0.0, "half_spread": 0.0})
SEARCH_KW = dict(family="elo_blend", n=12, seed=11, seasons=[2016, 2021], top_k=3, limits=ZERO_FEES, validation_seasons=[2022, 2025])
JOB_PARAMS = {"family": "elo_blend", "n": 12, "seed": 11, "seasons": [2016, 2021], "top_k": 3, "validation_seasons": [2022, 2025],
              "workers": 3, "fee_model": ZERO_FEES["fee_model"]}
RSS_LIMIT_MB = 300


@pytest.fixture(scope="module")
def games() -> list[dict]:
    return load_games(FIXTURE)


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def _boundaries(emitted: list[tuple[dict, float]]) -> list[tuple[str, float]]:
    """The candidate-complete (and validation-complete) checkpoints: current is empty."""
    return [(_dumps(cp), p) for cp, p in emitted if cp["current"] == {}]


def test_resolve_workers() -> None:
    assert resolve_workers(None) == 1 and resolve_workers(0) == 1 and resolve_workers("3") == 3 and resolve_workers(2.0) == 2
    assert resolve_workers("auto") == max(1, (os.cpu_count() or 2) - 1) and resolve_workers("bad") == 1


def test_three_workers_match_one_worker_byte_for_byte(games: list[dict]) -> None:
    runs: dict[int, tuple[str, list[tuple[str, float]]]] = {}
    for workers in (1, 3):
        emitted: list[tuple[dict, float]] = []
        result = run_search(games, emit=lambda cp, p: emitted.append((copy.deepcopy(cp), p)), should_stop=lambda: False,
                            workers=workers, **SEARCH_KW)
        runs[workers] = (_dumps(result["create_models"]), _boundaries(emitted))
        assert len(result["create_models"]) == 3 and all(c["validation_metrics"] for c in result["create_models"])
        if workers == 3:
            assert len(emitted) == 12 + 3, "the pool emits one checkpoint per completed candidate, in order"
            assert [cp["evaluated"] for cp, _ in emitted] == list(range(1, 13)) + [12, 12, 12]
    assert runs[1][0] == runs[3][0], "create_models identical byte for byte"
    assert runs[1][1] == runs[3][1], "the candidate-boundary checkpoint sequence (and progress) identical"
    assert len(runs[3][1]) == 15


def _job(checkpoint: dict | None = None) -> dict:
    return {"id": "search-par", "kind": "model_search", "params": dict(JOB_PARAMS), "checkpoint": checkpoint,
            "context": {"games_path": FIXTURE, "model": None}}


def _wait_for(predicate, timeout: float, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def test_sigterm_mid_search_then_resume_gives_the_same_result(games: list[dict]) -> None:
    expected = run_search(games, emit=lambda cp, p: None, should_stop=lambda: False, workers=1, **SEARCH_KW)
    runner = Runner(_job())
    runner.start()
    peak_kb = 0
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            peak_kb = max(peak_kb, sysinfo.session_rss_kb(runner.pid or 0))
            _, _, seq = runner.snapshot()
            if seq >= 2:
                break
            time.sleep(0.005)
        else:
            pytest.fail("no checkpoint from the pooled search within 30 s")
        runner.terminate()
        assert runner.wait(15.0), "the runner must stop promptly after SIGTERM"
        peak_kb = max(peak_kb, sysinfo.session_rss_kb(runner.pid or 0))
    finally:
        runner.stop(1.0)
    assert runner.outcome == "stopped", (runner.error, runner.outcome)
    checkpoint, progress, seq = runner.snapshot()
    assert checkpoint is not None and 1 <= checkpoint["evaluated"] < 12 and checkpoint["current"] == {}
    assert 0 < progress < 1 and not runner.group_killed, "SIGTERM alone ends the pool and its workers"
    assert _wait_for(lambda: sysinfo.session_pids(runner.pid or 0) == [], 5.0), "no pool worker survives the stop"

    resumed = Runner(_job(json.loads(json.dumps(checkpoint))))
    resumed.start()
    try:
        assert resumed.wait(60.0), "the resumed search did not finish"
        while True:
            peak_kb = max(peak_kb, sysinfo.session_rss_kb(resumed.pid or 0))
            if resumed.outcome is not None:
                break
    finally:
        resumed.stop(1.0)
    assert resumed.outcome == "done", resumed.error
    result = resumed.result
    assert _dumps(result) == _dumps(json.loads(_dumps(expected))), "resume after SIGTERM reproduces the single-process result"
    first_after = resumed.snapshot()[0]
    assert first_after is not None and first_after["evaluated"] == 12
    assert _wait_for(lambda: sysinfo.session_pids(resumed.pid or 0) == [], 5.0)
    assert peak_kb / 1024.0 < RSS_LIMIT_MB, f"process group peaked at {peak_kb / 1024.0:.0f} MB"


def test_process_group_memory_stays_small_during_a_pooled_search() -> None:
    runner = Runner(_job())
    runner.start()
    peak_kb = 0
    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and runner.outcome is None:
            peak_kb = max(peak_kb, sysinfo.session_rss_kb(runner.pid or 0))
            time.sleep(0.02)
    finally:
        runner.stop(1.0)
    assert runner.outcome == "done", runner.error
    assert peak_kb > 0 and peak_kb / 1024.0 < RSS_LIMIT_MB, f"process group peaked at {peak_kb / 1024.0:.0f} MB"
    assert len(runner.result["create_models"]) == 3


def test_pool_resume_from_a_mid_search_checkpoint_matches(games: list[dict]) -> None:
    """A single-process checkpoint (mid candidate) resumes exactly on the pool path,
    which repeats the unfinished candidate, and a pool checkpoint resumes exactly on
    the single-process path."""
    full = run_search(games, emit=lambda cp, p: None, should_stop=lambda: False, workers=1, **SEARCH_KW)
    emitted: list[dict] = []
    with pytest.raises(Exception):
        run_search(games, emit=lambda cp, p: emitted.append(copy.deepcopy(cp)), should_stop=lambda: len(emitted) >= 8, workers=1, **SEARCH_KW)
    mid = emitted[-1]
    assert mid["current"] != {}, "a mid-candidate checkpoint"
    assert run_search(games, emit=lambda cp, p: None, should_stop=lambda: False, workers=3, checkpoint=mid, **SEARCH_KW) == full
    pooled: list[dict] = []
    with pytest.raises(Exception):
        run_search(games, emit=lambda cp, p: pooled.append(copy.deepcopy(cp)), should_stop=lambda: len(pooled) >= 5, workers=3, **SEARCH_KW)
    assert pooled[-1]["evaluated"] == 5 and pooled[-1]["current"] == {}
    assert run_search(games, emit=lambda cp, p: None, should_stop=lambda: False, workers=1, checkpoint=pooled[-1], **SEARCH_KW) == full
    # a pool stop during the validation phase keeps the validated prefix
    late: list[dict] = []
    with pytest.raises(Exception):
        run_search(games, emit=lambda cp, p: late.append(copy.deepcopy(cp)), should_stop=lambda: len(late) >= 13, workers=3, **SEARCH_KW)
    assert late[-1]["evaluated"] == 12 and len(late[-1]["validated"]) == 1
    assert run_search(games, emit=lambda cp, p: None, should_stop=lambda: False, workers=3, checkpoint=late[-1], **SEARCH_KW) == full
