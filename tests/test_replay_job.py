"""Snapshot replay backtests, the worker side (docs/ROBUSTNESS.md B1): the backtest job
on recorded prices with the sim refusal, closing-line results left unchanged, and the
job context's prices fetch and cache. Fixture based, no database."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from fleet.sim.backtest import records_of, run_backtest, run_seasons
from fleet.sim.prices import SimPricesRefused
from fleet.worker import config, context
from fleet.worker.jobs import DEFAULT_LIMITS, JOBS
from tests.fake_host import FakeHost
from tests.test_replay import FIXTURE, LIMITS, PARAMS, PLATFORM, games, layout  # noqa: F401 (fixtures)

# the job and its context ----------------------------------------------------------------


def _write_prices(tmp_path: Path, markets: list[dict]) -> str:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"markets": markets, "count": len(markets)}))
    return str(path)


def test_backtest_job_replays_snapshots_and_refuses_sim(games: list[dict], layout: dict, tmp_path: Path) -> None:
    prices = _write_prices(tmp_path, layout["markets"])
    base = {"family": "elo_blend", "params": PARAMS, "seasons": [2023, 2024], "price_source": "snapshots",
            "price_platform": PLATFORM, "decision_minutes_before_kickoff": 60, "allow_sim_prices": False,
            "participation": 0.5, "_context": {"games_path": FIXTURE, "prices_path": prices}}
    result = JOBS["backtest"](dict(base), None, lambda cp, p: None, lambda: False)
    assert result["n_games"] == 25 and result["price_source"] == "snapshots" and result["platform"] == PLATFORM
    with pytest.raises(SimPricesRefused):
        JOBS["backtest"](dict(base, price_platform="sim"), None, lambda cp, p: None, lambda: False)
    sim = JOBS["backtest"](dict(base, price_platform="sim", allow_sim_prices=True), None, lambda cp, p: None, lambda: False)
    assert sim["n_games"] == 3 and sim["platform"] == "sim"
    with pytest.raises(ValueError):
        JOBS["backtest"](dict(base, price_source="bogus"), None, lambda cp, p: None, lambda: False)
    with pytest.raises(ValueError):
        JOBS["backtest"](dict(base, _context={"games_path": FIXTURE}), None, lambda cp, p: None, lambda: False)


def test_closing_line_results_are_unchanged(games: list[dict]) -> None:
    params = {"family": "elo_blend", "params": PARAMS, "seasons": [2021, 2024], "_context": {"games_path": FIXTURE}}
    plain = JOBS["backtest"](dict(params), None, lambda cp, p: None, lambda: False)
    named = JOBS["backtest"](dict(params, price_source="closing_line"), None, lambda cp, p: None, lambda: False)
    direct = run_backtest(games, "elo_blend", PARAMS, [2021, 2024], dict(DEFAULT_LIMITS), lambda cp, p: None, lambda: False)
    assert json.dumps(plain, sort_keys=True) == json.dumps(named, sort_keys=True) == json.dumps(direct, sort_keys=True)
    assert not {"price_source", "platform", "n_unscored_no_prices"} & set(plain)
    assert all(r["bet"] is None or r["bet"]["clv"] == 0.0
               for season in records_of(games, "elo_blend", PARAMS, run_seasons(
                   games, "elo_blend", PARAMS, [2024, 2024], LIMITS, lambda cp, p: None, lambda: False), LIMITS)
               for r in season)


@pytest.fixture
def fake_host() -> Any:
    host = FakeHost(lease_seconds=30.0, heartbeat_seconds=0.2).start()
    try:
        yield host
    finally:
        host.stop()


def test_context_fetches_and_caches_the_prices_of_a_snapshot_backtest(fake_host: FakeHost, layout: dict,
                                                                     tmp_path: Path) -> None:
    token = fake_host.register({"enroll_token": fake_host.mint_enroll_token(), "name": "t"})["worker_token"]
    fake_host.set_prices(layout["markets"])
    state = str(tmp_path / "state")
    os.makedirs(state)
    job = {"kind": "backtest", "params": {"family": "elo_blend", "price_source": "snapshots", "price_platform": PLATFORM}}

    def build(j: dict) -> dict:
        return context.build_context(fake_host.url, token, state, j, timeout=2.0, data_timeout=5.0)

    fake_host.set_games([])
    ctx = build(job)
    assert ctx["prices_path"] == config.prices_cache_path(state, PLATFORM)
    with open(ctx["prices_path"], encoding="utf-8") as fh:
        cached = json.load(fh)
    platforms = {m["platform"] for m in cached["markets"]}
    assert platforms == {PLATFORM} and all(m["confirmed"] for m in cached["markets"])
    mtime = os.stat(ctx["prices_path"]).st_mtime_ns
    assert build(job)["prices_path"] == ctx["prices_path"] and os.stat(ctx["prices_path"]).st_mtime_ns == mtime
    queries = fake_host.prices_queries()
    assert len(queries) == 2 and all("platform=polymarket_us" in q and "since=" in q for q in queries)
    assert "prices_path" not in build({"kind": "backtest", "params": {"family": "elo_blend"}})
    assert "prices_path" not in build(dict(job, kind="validate"))
    assert "prices_path" not in build({"kind": "backtest", "params": dict(job["params"], price_platform="sim")})
    assert len(fake_host.prices_queries()) == 2
    sim = build({"kind": "backtest", "params": dict(job["params"], price_platform="sim", allow_sim_prices=True)})
    with open(sim["prices_path"], encoding="utf-8") as fh:
        assert {m["platform"] for m in json.load(fh)["markets"]} == {"sim"}
