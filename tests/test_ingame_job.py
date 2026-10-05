"""The in-game search as a worker job (contract section 6): a model_search job with
family ingame_wp gets the pbp feed in its context (fleet.worker.context), runs the
in-game search (fleet.worker.jobs -> fleet.worker.pbp_cache -> fleet.sim.ingame) and its
create_models are posted unchanged by the agent. End to end against tests/fake_host.py
with the real runner child, on the real nflverse slice tests/fixtures/pbp_rows_sample.csv.gz
mapped by host/pbp_rows.py. No database."""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from fleet.sim.ingame import search_from_params
from fleet.worker import config, context, jobs
from fleet.worker.__main__ import main as cli_main
from fleet.worker.agent import Agent, AgentOptions
from host.pbp_rows import map_records
from tests.fake_host import FakeHost

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pbp_rows_sample.csv.gz"
TRAIN_SEASONS = (2019, 2020, 2021, 2022)
HB = 0.2
JOB_PARAMS: dict[str, Any] = {"family": "ingame_wp", "n": 3, "seed": 1, "top_k": 2, "train_seasons": [2019, 2022],
                              "validation_seasons": [2023, None], "train_fraction": 0.5}


def fixture_rows() -> list[dict[str, Any]]:
    """The fixture's two 2023 games as pbp_rows rows (the validation era) plus copies of
    them as eight earlier games (the train era), each copy with its own game id."""
    with gzip.open(FIXTURE, "rt", encoding="utf-8", newline="") as fh:
        real = list(map_records(csv.DictReader(fh), {}))
    copies = [dict(r, season=season, game_id=f"{season}_copy_{r['game_id']}") for season in TRAIN_SEASONS for r in real]
    return json.loads(json.dumps(copies + real))


@pytest.fixture(scope="module")
def rows() -> list[dict[str, Any]]:
    return fixture_rows()


@pytest.fixture
def host() -> Iterator[FakeHost]:
    fake = FakeHost(lease_seconds=30.0, heartbeat_seconds=HB).start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    directory = str(tmp_path / "state")
    os.makedirs(directory)
    monkeypatch.setenv("FLEET_STATE_DIR", directory)
    monkeypatch.delenv("FLEET_TEST_JOBS", raising=False)  # the real job registry
    return directory


@pytest.fixture
def enrolled(host: FakeHost, state_dir: str) -> str:
    assert cli_main(["enroll", f"--host={host.url}", f"--token={host.mint_enroll_token()}", "--name=box1"]) == 0
    return config.load_conf(state_dir)["worker_id"]


@pytest.fixture
def agent(host: FakeHost, state_dir: str, enrolled: str) -> Iterator[Agent]:
    options = AgentOptions(heartbeat_seconds=HB, http_timeout=2.0, shutdown_flush_delay=0.1)
    running = Agent(state_dir=state_dir, options=options)
    thread = threading.Thread(target=running.run_forever, daemon=True)
    thread.start()
    try:
        yield running
    finally:
        running.stop.set()
        thread.join(15.0)


def _run(host: FakeHost, worker_id: str, params: dict[str, Any], timeout: float = 90.0) -> dict[str, Any]:
    job_id = host.enqueue_job("model_search", params, target=worker_id)
    host.wait_for(lambda: host.job(job_id)["status"] in ("succeeded", "failed"), timeout=timeout)
    return host.job(job_id)


def _pbp_requests(host: FakeHost) -> list[tuple[str, str, int]]:
    return [r for r in host.requests if r[1].startswith("/api/v1/data/pbp")]


def test_model_search_ingame_wp_end_to_end(host: FakeHost, enrolled: str, agent: Agent, rows: list[dict[str, Any]]) -> None:
    host.set_pbp(rows)
    job = _run(host, enrolled, JOB_PARAMS)
    assert job["status"] == "succeeded", job["error"]
    year = time.gmtime().tm_year
    assert host.pbp_queries() == [f"seasons=2019-{year}"], "train start to the open validation end"
    assert not any(r[1].startswith("/api/v1/data/games") for r in host.requests), "the in-game search needs no games feed"

    expected = search_from_params(JOB_PARAMS, lambda: iter(rows), lambda c, p: None, lambda: False)
    entries = expected["create_models"]
    assert len(entries) == 2
    posts = [c for c in host.model_posts() if c["path"] == "/api/v1/models"]
    assert len(posts) == 2
    for post, entry in zip(posts, entries):
        body = {k: v for k, v in post["body"].items() if k != "job_id"}
        assert body == json.loads(json.dumps(entry)), "the agent posts the create_models entries unchanged"
        assert post["body"]["job_id"] == job["id"]

    models = sorted(host.models(), key=lambda m: [p["response"]["id"] for p in posts].index(m["id"]))
    n_validation = sum(1 for r in rows if r["season"] == 2023 and r["home_win"] is not None and r["vegas_wp"] is not None)
    for model in models:
        assert model["family"] == "ingame_wp" and model["status"] == "candidate"
        assert len(model["artifact"]["coef"]) == 10 and set(model["artifact"]["train_seasons"]) <= set(TRAIN_SEASONS)
        assert model["backtest_metrics"]["era"] == "search" and model["backtest_metrics"]["seasons"] == list(TRAIN_SEASONS)
        assert model["backtest_metrics"]["n_plays"] > 0 and model["backtest_metrics"]["n_fit_plays"] > 0
        val = model["validation_metrics"]
        assert val["era"] == "validation" and val["seasons"] == [2023] and val["n_plays"] == n_validation > 100
        assert isinstance(val["beats_baseline"], bool) and isinstance(val["log_loss"], float)
        assert val["vegas_log_loss"] > 0 and len(val["calibration"]) == 10
        assert model["summary"].count(". ") == 2

    result = job["result"]
    assert "create_models" not in result
    assert [c["id"] for c in result["created_models"]] == [p["response"]["id"] for p in posts]
    assert all(c["created"] for c in result["created_models"])
    assert result["evaluated"] == 3 and len(result["top"]) == 2 and result["train_seasons"] == [2019, 2022]

    again = _run(host, enrolled, JOB_PARAMS)
    assert again["status"] == "succeeded", again["error"]
    assert _pbp_requests(host)[-1][2] == 304, "an unchanged feed is not downloaded again"
    assert [c["created"] for c in again["result"]["created_models"]] == [False, False]
    assert [c["id"] for c in again["result"]["created_models"]] == [c["id"] for c in result["created_models"]]


def test_ingame_search_fails_cleanly_without_the_feed(host: FakeHost, enrolled: str, agent: Agent) -> None:
    host.fail_next("/api/v1/data/pbp", status=503)
    job = _run(host, enrolled, JOB_PARAMS, timeout=30.0)
    assert job["status"] == "failed" and "context unavailable" in job["error"] and "pbp" in job["error"]


# context and job routing, in process ---------------------------------------------------


def test_context_of_an_ingame_search(host: FakeHost, state_dir: str, enrolled: str, rows: list[dict[str, Any]]) -> None:
    conf = config.load_conf(state_dir)
    job = {"kind": "model_search", "params": {"family": "ingame_wp", "train_seasons": [2019, 2022],
                                               "validation_seasons": [2023, 2023]}}
    with pytest.raises(context.ContextError):  # nothing served, nothing cached
        host.fail_next("/api/v1/data/pbp", status=503)
        context.build_context(host.url, conf["worker_token"], state_dir, job, 2.0, 5.0)
    host.set_pbp(rows)
    ctx = context.build_context(host.url, conf["worker_token"], state_dir, job, 2.0, 5.0)
    assert set(ctx) == {"games_path", "model", "pbp_path"} and ctx["games_path"] is None and ctx["model"] is None
    assert host.pbp_queries()[-1] == "seasons=2019-2023"
    with gzip.open(ctx["pbp_path"], "rt", encoding="utf-8") as fh:
        assert [json.loads(line) for line in fh] == rows
    host.fail_next("/api/v1/data/pbp", status=503)  # a failed refresh keeps the cached copy
    assert context.build_context(host.url, conf["worker_token"], state_dir, job, 2.0, 5.0) == ctx
    assert context.pbp_seasons({"seasons": [2015, None], "validation_seasons": [2022, None]}) == (2015, time.gmtime().tm_year)
    assert not context.is_ingame_search({"kind": "backtest", "params": {"family": "ingame_wp"}})
    assert not context.is_ingame_search({"kind": "model_search", "params": {"family": "elo_blend"}})


def test_jobs_route_the_ingame_family(tmp_path: Path, rows: list[dict[str, Any]]) -> None:
    path = tmp_path / "pbp.jsonl.gz"
    path.write_bytes(gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode("utf-8")))
    params = dict(JOB_PARAMS, n=2, _context={"games_path": None, "model": None, "pbp_path": str(path)})
    emitted: list[float] = []
    result = jobs.JOBS["model_search"](params, None, lambda c, p: emitted.append(p), lambda: False)
    assert emitted == [0.5, 1.0] and len(result["create_models"]) == 2
    assert result == search_from_params(dict(JOB_PARAMS, n=2), lambda: iter(rows), lambda c, p: None, lambda: False)
    model = {"id": "m1", "family": "ingame_wp", "params": {}, "artifact": None}
    for kind, job_params in (("backtest", {"family": "ingame_wp"}), ("backtest", {"model_id": "m1", "_context": {"model": model}}),
                             ("validate", {"model_id": "m1", "_context": {"model": model}}),
                             ("train", {"model_id": "m1", "_context": {"model": model}})):
        with pytest.raises(ValueError, match="in-game search"):
            jobs.JOBS[kind](job_params, None, lambda c, p: None, lambda: False)


def test_fake_host_pbp_feed_matches_the_documented_shape(host: FakeHost, state_dir: str, enrolled: str,
                                                         rows: list[dict[str, Any]]) -> None:
    import urllib.request

    host.set_pbp(rows)
    token = config.load_conf(state_dir)["worker_token"]
    req = urllib.request.Request(f"{host.url}/api/v1/data/pbp?seasons=2023-2023", headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=5) as resp:
        body, ctype, etag, encoding = resp.read(), resp.headers["Content-Type"], resp.headers["ETag"], resp.headers["Content-Encoding"]
    assert ctype == "application/x-ndjson+gzip" and encoding is None and body[:2] == b"\x1f\x8b"
    lines = [json.loads(line) for line in io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(body)), encoding="utf-8")]
    assert lines == [r for r in rows if r["season"] == 2023] and etag.startswith(f'"{len(lines)}-2023-')
