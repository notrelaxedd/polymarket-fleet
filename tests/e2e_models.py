"""Step 3 phase of the end-to-end test (tests/test_e2e.py): the nflverse fixture goes in
through the CLI, a model search runs on the real runner and creates models, one of them
is trained and backtested, and a longer search is preempted by a role change and resumed
on the next model_search worker without repeating more than one candidate-season.
"""
from __future__ import annotations

import contextlib
import io
import time
from typing import Any, Callable

from fleet.worker import config as worker_config
from fleet.worker import runner as runner_module
from host import cli
from tests.conftest import FIXTURE_GAMES

FIXTURE_ROWS = 2761
SHORT_SEARCH = {"family": "elo_blend", "n": 3, "seasons": [2019, 2021], "top_k": 2}
LONG_SEARCH = {"family": "elo_blend", "n": 40, "seed": 3, "seasons": [2019, 2025], "top_k": 3}
LONG_PLAN = [2019, 2020, 2021, 2022, 2023, 2024, 2025]


class CountingRunner(runner_module.Runner):
    """A Runner that counts the checkpoints each child printed, per job id.

    The search emits exactly one checkpoint per candidate-season, so the counts of the
    runs before and after a preemption say how many units were repeated.
    """

    counts: dict[str, list[int]] = {}

    def __init__(self, job: dict[str, Any], **kw: Any) -> None:
        super().__init__(job, **kw)
        self.counts.setdefault(self.job_id, []).append(0)
        self._slot = len(self.counts[self.job_id]) - 1

    def _apply(self, msg: dict[str, Any]) -> None:
        super()._apply(msg)
        if "checkpoint" in msg and isinstance(msg["checkpoint"], dict) and not msg.get("stopped"):
            self.counts[self.job_id][self._slot] += 1  # the final "stopped" line repeats the last checkpoint


def unit_index(checkpoint: dict[str, Any] | None, plan_len: int) -> int:
    """A search checkpoint's position as candidate * seasons + season index."""
    nxt = (checkpoint or {}).get("next") or [0, 0]
    return int(nxt[0]) * plan_len + int(nxt[1])


def ingest_through_cli(host: Any, state_dir: str, monkeypatch: Any) -> None:
    """python -m host.cli ingest-games --file <fixture>; the worker feed then serves it."""
    monkeypatch.setenv("DATABASE_URL", host.database_url)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["ingest-games", "--file", str(FIXTURE_GAMES)]) == 0
    assert out.getvalue().startswith(f"ingested {FIXTURE_ROWS} rows from "), out.getvalue()
    assert f"{FIXTURE_ROWS} inserted, 0 changed; last complete season 2025" in out.getvalue()
    token = worker_config.load_conf(state_dir)["worker_token"]
    resp = host.client.get("/api/v1/data/games", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200 and resp.json()["count"] == FIXTURE_ROWS
    etag = resp.headers["etag"]
    again = host.client.get("/api/v1/data/games", headers={"Authorization": f"Bearer {token}", "If-None-Match": etag})
    assert again.status_code == 304, "an unchanged games table answers 304"


def send(host: Any, kind: str, params: dict[str, Any], target: str) -> dict[str, Any]:
    return host.post("/api/jobs", {"kind": kind, "params": params, "target": target}, expect=201)


def finished(host: Any, job_id: str) -> Callable[[], Any]:
    def check() -> Any:
        job = host.job(job_id)
        assert job["status"] not in ("failed", "cancelled"), f"{job['status']}: {job.get('error')}"
        return job if job["status"] == "succeeded" else False

    return check


def search_phase(host: Any, worker_id: str, wait_for: Callable[..., Any], settled: Callable[..., Any]) -> list[str]:
    """A three-candidate search on any_idle: the worker flips, the job succeeds, two
    models exist, the leaderboard and the Models page show them."""
    job = send(host, "model_search", SHORT_SEARCH, "any_idle")
    assert job["target_worker_id"] == worker_id and job["role"] == "model_search", (job, host.worker(worker_id))
    assert job["params"]["seed"] == 0 and job["params"]["fee_model"]["taker_rate"] == 0.05
    flipped = host.worker(worker_id)
    assert flipped["desired_role"] == "model_search" and flipped["auto_role"] is True
    wait_for(settled(host, worker_id, "model_search"), "agent to ack model_search")
    done = wait_for(finished(host, job["id"]), "short search done", timeout=30.0)
    result = done["result"]
    assert done["progress"] == 1, "a 0.2 s job may finish before a heartbeat carries a checkpoint"
    assert result["evaluated"] == 3 and result["seasons"] == [2019, 2020, 2021] and len(result["top"]) == 2
    created = result["created_models"]
    assert len(created) == 2 and all(m["created"] is True and m["lineage_id"] == m["id"] for m in created)
    assert "create_models" not in result
    events = host.events(job["id"])
    assert events.count("model_created") == 2 and events[-1] == "succeeded", events
    ids = [m["id"] for m in created]

    board = host.get("/api/models")
    listed = {m["id"]: m for m in board["ranked"] + board["unranked"]}
    assert set(ids) <= set(listed), (ids, list(listed))
    for mid, top in zip(ids, result["top"]):
        entry = listed[mid]
        assert entry["family"] == "elo_blend" and entry["status"] == "candidate" and entry["members"] == 1
        assert entry["metrics"]["n_bets"] == top["metrics"]["n_bets"]
        assert entry["summary"].count(". ") + entry["summary"].count(".\n") >= 2 and entry["summary"].endswith("CLV.")
        assert entry["summary"].startswith(f"Elo blend (K {round(top['params']['k'])}, home edge")
    page = host.client.get("/models").text
    for mid in ids:
        assert f'href="/models/{mid}"' in page
        assert listed[mid]["summary"].split(". ")[0] in page, "the Models page renders the summary (first sentence)"
    detail = host.client.get(f"/jobs/{job['id']}").text
    assert 'class="metrics top' in detail and f'href="/models/{ids[0]}"' in detail
    wait_for(settled(host, worker_id, "idle"), "worker idle after the search")
    return ids


def train_phase(host: Any, worker_id: str, root_id: str, wait_for: Callable[..., Any]) -> str:
    """Train the first model through 2021 week 10: a child in the root's lineage."""
    job = send(host, "train", {"model_id": root_id, "through": {"season": 2021, "week": 10}}, worker_id)
    done = wait_for(finished(host, job["id"]), "train done", timeout=30.0)
    result = done["result"]
    assert result["through"] == [2021, 10] and result["games_seen"] > 1000
    child_id = result["created_models"][0]["id"]
    assert result["created_models"][0]["created"] is True
    child = host.get(f"/api/models/{child_id}")
    root = host.get(f"/api/models/{root_id}")
    assert child["lineage_id"] == root["lineage_id"] == root_id and child["parent_model_id"] == root_id
    assert child["trained_through"] == [2021, 10] and child["status"] == root["status"]
    assert child["params"] == root["params"]
    artifact = child["artifact"]
    assert artifact["through"] == [2021, 10] and set(artifact["blend"]) == {"a", "b", "c"}
    assert len(artifact["ratings"]) >= 32 and "LV" in artifact["ratings"] and "OAK" not in artifact["ratings"]
    assert [r["id"] for r in child["lineage"]] == [root_id, child_id]
    assert child["backtest_metrics"]["n_bets"] == root["backtest_metrics"]["n_bets"], "a child inherits the lineage metrics"
    page = host.client.get(f"/models/{child_id}").text
    assert "2021 week 10" in page and f'href="/models/{root_id}"' in page
    return child_id


def backtest_phase(host: Any, worker_id: str, model_id: str, root_id: str, wait_for: Callable[..., Any]) -> None:
    """Backtest the trained child over its lineage's seasons: metrics on the job and the model."""
    job = send(host, "backtest", {"model_id": model_id, "seasons": [2019, 2021]}, worker_id)
    done = wait_for(finished(host, job["id"]), "backtest done", timeout=30.0)
    result = done["result"]
    assert result["seasons"] == [2019, 2020, 2021] and len(result["per_season"]) == 3
    assert 0.55 < result["log_loss"] < 0.75 and abs(result["log_loss"] - result["market_log_loss"]) < 0.02
    assert result["n_games"] > 700 and 0 <= result["max_drawdown"] <= 1
    assert "created_models" not in result
    for mid in (model_id, root_id):
        stored = host.get(f"/api/models/{mid}")["backtest_metrics"]
        assert stored["n_games"] == result["n_games"] and stored["log_loss"] == result["log_loss"]
        assert stored["per_season"][0]["season"] == 2019
    assert "model_backtest" in host.events(job["id"])
    detail = host.client.get(f"/jobs/{job['id']}").text
    assert 'class="kv metrics"' in detail and 'class="metrics per-season' in detail
    assert f">{result['n_games']}<" in detail
    assert 'class="kv metrics"' in host.client.get(f"/models/{model_id}").text


def preempt_phase(host: Any, worker_id: str, wait_for: Callable[..., Any], settled: Callable[..., Any]) -> None:
    """A 40-candidate search is moved off the worker mid-job (role train) and resumes on
    the next model_search worker; the two runs together repeat at most one unit."""
    plan_len = len(LONG_PLAN)
    units = LONG_SEARCH["n"] * plan_len
    job = send(host, "model_search", LONG_SEARCH, worker_id)
    job_id = job["id"]
    wait_for(settled(host, worker_id, "model_search"), "worker in model_search for the long search")
    seen: list[float] = []

    def running() -> Any:
        current = host.job(job_id)
        assert current["status"] in ("leased", "queued"), current["status"]
        seen.append(current["progress"])
        return current if (current["checkpoint"] or {}).get("evaluated", 0) >= 2 else False

    wait_for(running, "two candidates evaluated", timeout=30.0)
    assert seen[-1] > 0 and seen == sorted(seen), f"progress climbs: {seen}"

    host.set_role(worker_id, "train")
    wait_for(settled(host, worker_id, "train"), "worker moved to train")
    requeued = wait_for(lambda: (j := host.job(job_id))["status"] == "queued" and j, "search back in the queue")
    released_unit = unit_index(requeued["checkpoint"], plan_len)
    first_run = CountingRunner.counts[job_id][0]
    assert 2 * plan_len <= released_unit < units and first_run == released_unit, (released_unit, first_run)
    assert requeued["checkpoint"]["top"] and requeued["checkpoint"]["evaluated"] >= 2

    host.set_role(worker_id, "model_search")
    wait_for(lambda: host.job(job_id)["status"] == "leased", "search re-leased")
    deadline = time.monotonic() + 60.0
    while True:
        current = host.job(job_id)
        assert unit_index(current["checkpoint"], plan_len) >= released_unit, "checkpoint went backwards"
        if current["status"] == "succeeded":
            break
        assert current["status"] == "leased" and time.monotonic() < deadline, current["status"]
        time.sleep(0.05)
    assert current["result"]["evaluated"] == LONG_SEARCH["n"] and current["result"]["seasons"] == LONG_PLAN
    assert len(current["result"]["created_models"]) == 3 and current["progress"] == 1
    runs = CountingRunner.counts[job_id]
    assert len(runs) == 2 and 0 <= sum(runs) - units <= 1, f"units per run {runs}, total {units}"
    assert host.events(job_id).count("claimed") == 2
    host.set_role(worker_id, "idle")  # the manual role sets above cleared auto_role, so no auto-return
    wait_for(settled(host, worker_id, "idle"), "worker idle after the resumed search")


def phase_models(host: Any, state_dir: str, worker_id: str, monkeypatch: Any,
                 wait_for: Callable[..., Any], settled: Callable[..., Any]) -> dict[str, Any]:
    """Returns {"roots": [search model ids], "child": the trained model id} for step 4."""
    ingest_through_cli(host, state_dir, monkeypatch)
    ids = search_phase(host, worker_id, wait_for, settled)
    child_id = train_phase(host, worker_id, ids[0], wait_for)
    backtest_phase(host, worker_id, child_id, ids[0], wait_for)
    preempt_phase(host, worker_id, wait_for, settled)
    return {"roots": ids, "child": child_id}
