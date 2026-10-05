"""Regression tests for the step 6 Part A review findings on the worker side: the
search pool (a dead worker errors instead of hanging, the in-flight window, the CPU
affinity), the runner's kill reaching the pool workers, the validate job refusing
overlapping eras, the Brier identity on continuous forecasts and the summary verdict.
"""

from __future__ import annotations

import os
import random
import signal
import time
from typing import Any

import pytest

from fleet.common import sysinfo
from fleet.models.elo_blend import build_summary
from fleet.sim.control import JobStopped
from fleet.sim.parallel import ordered_map, resolve_workers
from fleet.sim.stats import brier_decomposition
from fleet.worker.jobs import run_validate_job, search_era_overlap
from fleet.worker.runner import Runner

POOL_JOBS_SPEC = "tests.test_review6a_worker:POOL_JOBS"


def _slow(item: int) -> int:
    time.sleep(0.3)
    return item * 2


def _suicide(item: int) -> int:
    if item == 2:
        os.kill(os.getpid(), signal.SIGKILL)
    time.sleep(0.05)
    return item


def _pool_job(task: Any, params: dict[str, Any], emit: Any, should_stop: Any) -> dict[str, Any]:
    out: list[int] = []

    def accept(value: int) -> None:
        out.append(value)
        emit({"done": len(out)}, len(out) / 40)

    ordered_map(task, list(range(40)), int(params.get("workers", 3)), {}, should_stop, accept)
    return {"values": out}


POOL_JOBS = {
    "pool_slow": lambda params, cp, emit, stop: _pool_job(_slow, params, emit, stop),
    "pool_suicide": lambda params, cp, emit, stop: _pool_job(_suicide, params, emit, stop),
}


# a dead pool worker -------------------------------------------------------------------


def test_a_killed_pool_worker_raises_instead_of_hanging() -> None:
    """Review 6A (medium): a SIGKILLed worker never returned its result and the map
    waited forever; now the map raises within a poll or two."""
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="search worker died"):
        ordered_map(_suicide, list(range(10)), 3, {}, lambda: False, lambda r: None)
    assert time.monotonic() - started < 10


def test_a_killed_pool_worker_is_an_error_line_from_the_runner(monkeypatch) -> None:
    monkeypatch.setenv("FLEET_TEST_JOBS", POOL_JOBS_SPEC)
    runner = Runner({"id": "j-suicide", "kind": "pool_suicide", "params": {"workers": 3}})
    runner.start()
    assert runner.wait(30), "the child ends instead of waiting for the lost result"
    runner.reap_group()
    assert runner.outcome == "error" and "search worker died" in (runner.error or "")
    assert sysinfo.session_pids(runner.pid) == []


def test_task_errors_still_reach_the_parent() -> None:
    def boom(item: int) -> int:
        raise ValueError(f"bad item {item}")

    with pytest.raises(ValueError, match="bad item 0"):
        ordered_map(boom, [0, 1], 2, {}, lambda: False, lambda r: None)


# the in-flight window -----------------------------------------------------------------


def _uneven(item: int) -> int:
    time.sleep(1.2 if item == 0 else 0.05)
    return os.getpid()


def test_at_most_workers_items_run_ahead_of_the_head() -> None:
    """Review 6A (low): Pool.imap let fast workers run far past a slow head, and a stop
    lost every item finished out of order. Only `workers` indices past the first
    unfinished one are handed out, so a stop repeats at most `workers` items."""
    results: list[int] = []
    stop_at = time.monotonic() + 0.8  # while item 0 still runs
    with pytest.raises(JobStopped):
        ordered_map(_uneven, list(range(30)), 3, {}, lambda: time.monotonic() > stop_at, results.append)
    assert results == [], "nothing passes the slow head"
    seen: list[int] = []
    ordered_map(_slow, list(range(7)), 3, {}, lambda: False, seen.append)
    assert seen == [2 * i for i in range(7)], "results arrive in item order"


def test_the_window_bounds_the_items_started(tmp_path) -> None:
    marks = tmp_path / "started"
    marks.mkdir()

    def mark(item: int) -> int:
        (marks / str(item)).write_text("x")
        time.sleep(1.5 if item == 0 else 0.02)
        return item

    stop_at = time.monotonic() + 1.0
    with pytest.raises(JobStopped):
        ordered_map(mark, list(range(30)), 3, {}, lambda: time.monotonic() > stop_at, lambda r: None)
    started = sorted(int(p.name) for p in marks.iterdir())
    assert started == [0, 1, 2], f"only indices 0 to 2 may start while 0 runs, got {started}"


# auto workers and affinity ------------------------------------------------------------


def test_auto_workers_follow_the_affinity_mask(monkeypatch) -> None:
    """Review 6A (low): os.cpu_count() ignores taskset and cpusets."""
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0}, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert resolve_workers("auto") == 1
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1, 2}, raising=False)
    assert resolve_workers("auto") == 2
    assert resolve_workers(5) == 5 and resolve_workers(None) == 1


# the runner's kill reaches the pool workers -------------------------------------------


def test_runner_kill_leaves_no_pool_worker_behind(monkeypatch) -> None:
    """Review 6A (medium): the pool workers sit in process groups of their own, so a
    group SIGKILL missed them and they computed on as orphans. kill() now SIGKILLs the
    whole session (and the workers die with their parent anyway)."""
    monkeypatch.setenv("FLEET_TEST_JOBS", POOL_JOBS_SPEC)
    runner = Runner({"id": "j-kill", "kind": "pool_slow", "params": {"workers": 3}})
    runner.start()
    deadline = time.monotonic() + 15
    while len(sysinfo.session_pids(runner.pid)) < 4 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(sysinfo.session_pids(runner.pid)) >= 4, "the child and its three workers"
    runner.kill()
    assert runner.wait(5)
    runner.reap_group()
    deadline = time.monotonic() + 3
    while sysinfo.session_pids(runner.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sysinfo.session_pids(runner.pid) == [], "no orphan worker survives the kill"


def test_runner_sigterm_stops_the_pool_cleanly(monkeypatch) -> None:
    monkeypatch.setenv("FLEET_TEST_JOBS", POOL_JOBS_SPEC)
    runner = Runner({"id": "j-term", "kind": "pool_slow", "params": {"workers": 3}})
    runner.start()
    deadline = time.monotonic() + 15
    while runner.snapshot()[2] < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    runner.stop(grace=5.0)
    assert runner.outcome == "stopped" and not runner.kill_sent
    assert sysinfo.session_pids(runner.pid) == []


# the validate job and overlapping eras ------------------------------------------------


def test_search_era_overlap() -> None:
    assert search_era_overlap({"seasons": [2013, 2021]}, [2022, None]) == []
    assert search_era_overlap({"seasons": list(range(2013, 2026))}, [2022, 2025]) == [2022, 2023, 2024, 2025]
    assert search_era_overlap(None, [2022, None]) == [] and search_era_overlap({"seasons": [2022]}, []) == []


def test_validate_job_refuses_search_metrics_from_the_validation_era() -> None:
    """Review 6A (high): a backtest stored on validation-era seasons used to stand in for
    the search era, so the overfit flag compared an era with itself and vanished."""
    model = {"family": "elo_blend", "params": {}, "backtest_metrics": {"seasons": [2022, 2023], "roi": 0.0, "n_bets": 0}}
    params = {"model_id": "m", "validation_seasons": [2022, 2025], "_context": {"model": model, "games_path": "/nonexistent"}}
    with pytest.raises(ValueError, match="inside the validation era"):
        run_validate_job(params, None, lambda cp, p: None, lambda: False)


# the Brier identity and the summary verdict -------------------------------------------


def test_brier_identity_holds_for_continuous_forecasts() -> None:
    """Review 6A (low): without the within-bucket terms rel - res + unc missed the Brier
    score by about as much as the reliability itself on NFL-range forecasts."""
    rng = random.Random(8)
    p = [0.4 + 0.35 * rng.random() for _ in range(800)]
    y = [1.0 if rng.random() < q else 0.0 for q in p]
    y[3] = 0.5  # a tie
    parts = brier_decomposition(p, y)
    brier = sum((a - b) ** 2 for a, b in zip(p, y)) / len(p)
    total = parts["reliability"] - parts["resolution"] + parts["uncertainty"] + parts["within_variance"] - parts["within_covariance"]
    assert total == pytest.approx(brier, abs=1e-12)
    assert parts["within_variance"] > 0


def test_summary_says_beats_the_closing_line_only_with_a_significant_market_test() -> None:
    """Review 6A (low): a 0.002 log-loss margin on a short era is not a market test."""
    metrics = {"seasons": [2019], "n_games": 270, "n_bets": 0, "log_loss": 0.650, "market_log_loss": 0.653}
    assert "beats the closing line" in build_summary({}, metrics), "no market test stored: the old rule"
    assert "beats the closing line" in build_summary({}, {**metrics, "market_p": 0.03})
    weak = build_summary({}, {**metrics, "market_p": 0.165})
    assert "beats the closing line" not in weak and "not significantly (market test p 0.17)" in weak
