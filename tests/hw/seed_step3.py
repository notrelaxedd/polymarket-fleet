"""Step 3 rows for the screenshot database: the nflverse fixture, a finished model search
with real metrics (a short search run right here on the fixture), the models it created,
one trained child and a finished backtest of it. Used by tests/hw/screenshots.py.
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fleet.models.registry import get_family
from fleet.sim.backtest import run_backtest
from fleet.sim.data import load_games
from fleet.sim.search import run_search
from fleet.worker.jobs import DEFAULT_LIMITS
from host import nflverse
from tests.conftest import FIXTURE_GAMES, insert_job, insert_model

SEARCH = {"family": "elo_blend", "n": 6, "seed": 7, "seasons": [2019, 2025], "top_k": 5}
LIMITS = dict(DEFAULT_LIMITS, backtest_seasons=[2010, 2025])


def _noop(_checkpoint: dict[str, Any], _progress: float) -> None:
    return None


def _never() -> bool:
    return False


def _event(conn: psycopg.Connection, job_id: Any, event: str, worker_id: str | None, detail: dict | None, ago: int) -> None:
    conn.execute(
        "INSERT INTO job_events (job_id, ts, worker_id, event, detail) VALUES (%s, now() - make_interval(secs => %s), %s, %s, %s)",
        (job_id, ago, worker_id, event, Jsonb(detail) if detail is not None else None),
    )


def _finished_job(conn: psycopg.Connection, kind: str, worker_id: str, params: dict[str, Any], result: dict[str, Any],
                  checkpoint: dict[str, Any], started_ago: int, took: int) -> dict[str, Any]:
    job = insert_job(conn, kind, status="succeeded", progress=1.0, target_worker_id=worker_id, lease_worker_id=None,
                     params=Jsonb(dict(params, **LIMITS)), checkpoint=Jsonb(checkpoint), result=Jsonb(result))
    conn.execute(
        "UPDATE jobs SET created_at = now() - make_interval(secs => %s), started_at = now() - make_interval(secs => %s),"
        " finished_at = now() - make_interval(secs => %s) WHERE id = %s",
        (started_ago + 5, started_ago, started_ago - took, job["id"]),
    )
    _event(conn, job["id"], "created", None, {"target": worker_id}, started_ago + 5)
    _event(conn, job["id"], "claimed", worker_id, None, started_ago)
    return job


def seed_models(url: str, worker_id: str, running_worker_id: str) -> dict[str, str]:
    """Ingest the fixture, run a six-candidate search, store it as a finished job with
    its models, train one child and backtest it; also park a running search on
    `running_worker_id`. Returns the ids the captures need."""
    games = load_games(str(FIXTURE_GAMES))
    search = run_search(games, SEARCH["family"], SEARCH["n"], SEARCH["seed"], SEARCH["seasons"], SEARCH["top_k"],
                        LIMITS, _noop, _never)
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        nflverse.ingest(conn, str(FIXTURE_GAMES))
        plan = search["seasons"]
        checkpoint = {"next": [SEARCH["n"], 0], "current": {}, "top": search["top"], "evaluated": SEARCH["n"]}
        job = _finished_job(conn, "model_search", worker_id, SEARCH, {}, checkpoint, 1800, 40)
        created, roots = [], []
        for entry in search["create_models"]:
            row = insert_model(conn, entry["family"], entry["params"], entry["backtest_metrics"], summary=entry["summary"])
            conn.execute("UPDATE models SET created_by_job_id = %s WHERE id = %s", (job["id"], row["id"]))
            created.append({"id": str(row["id"]), "lineage_id": str(row["lineage_id"]), "created": True})
            roots.append(row)
            _event(conn, job["id"], "model_created", worker_id, {"model_id": str(row["id"]), "status": "candidate"}, 1761)
        result = {k: v for k, v in search.items() if k != "create_models"}
        result["created_models"] = created
        conn.execute("UPDATE jobs SET result = %s WHERE id = %s", (Jsonb(result), job["id"]))
        _event(conn, job["id"], "succeeded", worker_id, None, 1760)

        root = roots[0]
        family = get_family(root["family"])(dict(root["params"]))
        family.fit([g for g in games if (g["season"], g["week"]) <= (2024, 18)], (2024, 18), _never)
        artifact = family.to_json()
        child = insert_model(conn, root["family"], dict(root["params"]), parent=root, trained_through=[2024, 18],
                             artifact=artifact, summary=root["summary"])
        train_job = _finished_job(conn, "train", worker_id, {"model_id": str(root["id"]), "through": {"season": 2024, "week": 18}},
                                  {"created_models": [{"id": str(child["id"]), "lineage_id": str(root["lineage_id"]), "created": True}],
                                   "through": [2024, 18], "games_seen": artifact["games_seen"]},
                                  {"next": 9, "seasons": list(range(2016, 2025)), "season": 2024}, 900, 3)
        conn.execute("UPDATE models SET created_by_job_id = %s WHERE id = %s", (train_job["id"], child["id"]))
        _event(conn, train_job["id"], "succeeded", worker_id, None, 897)

        backtest = run_backtest(games, root["family"], dict(root["params"]), SEARCH["seasons"], LIMITS, _noop, _never)
        bt_job = _finished_job(conn, "backtest", worker_id, {"model_id": str(child["id"]), "seasons": SEARCH["seasons"]},
                               backtest, {"next": len(plan), "per_season": []}, 600, 2)
        _event(conn, bt_job["id"], "model_backtest", worker_id, {"model_id": str(child["id"])}, 598)
        _event(conn, bt_job["id"], "succeeded", worker_id, None, 598)
        conn.execute("UPDATE models SET backtest_metrics = %s WHERE lineage_id = %s", (Jsonb(backtest), root["lineage_id"]))

        half = {"next": [3, 2], "current": {"next": 2, "per_season": []}, "top": search["top"][:3], "evaluated": 3}
        running = insert_job(conn, "model_search", status="leased", progress=0.52, lease_worker_id=running_worker_id,
                             target_worker_id=running_worker_id, params=Jsonb(dict(SEARCH, n=200, **LIMITS)), checkpoint=Jsonb(half))
        conn.execute(
            "UPDATE jobs SET lease_token = gen_random_uuid(), lease_expires_at = now() + interval '30 seconds',"
            " started_at = now() - interval '70 seconds', created_at = now() - interval '80 seconds' WHERE id = %s",
            (running["id"],),
        )
        _event(conn, running["id"], "claimed", running_worker_id, None, 70)
        return {"search_job": str(job["id"]), "model": str(root["id"]), "child": str(child["id"]),
                "backtest_job": str(bt_job["id"]), "running_search": str(running["id"])}
