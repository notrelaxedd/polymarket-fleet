"""Model routes: worker creation and metrics, the games feed, owner leaderboard and edits."""
from __future__ import annotations

import json
import uuid

from fleet.models.base import params_hash
from tests.conftest import (
    backtest_metrics, ingest_fixture, insert_model, insert_validated_model, lease_job, model_row, set_setting, stress_metrics,
    validation_metrics,
)

PARAMS = {"k": 24.0, "hfa": 55.0, "regress": 0.33, "rest_per_day": 1.0, "mov_scale": 1, "min_edge": 0.03, "kelly_fraction": 0.25}


def _body(job, **extra):
    body = {"job_id": str(job["id"]), "family": "elo_blend", "params": PARAMS, "artifact": None,
            "backtest_metrics": None, "summary": None, "parent_model_id": None, "trained_through": None}
    body.update(extra)
    return body


def test_create_root_is_its_own_lineage_and_candidate(client, conn, make_worker):
    w = make_worker("box1", role="model_search")
    job = lease_job(conn, w, "model_search", {"family": "elo_blend", "n": 3})
    r = client.post("/api/v1/models", json=_body(job, summary="Three sentences."), headers=w.headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True and body["lineage_id"] == body["id"] and body["status"] == "candidate"
    row = model_row(conn, body["id"])
    assert row["params_hash"] == params_hash(PARAMS) and len(row["params_hash"]) == 16
    assert row["family"] == "elo_blend" and row["summary"] == "Three sentences." and row["created_by_job_id"] == job["id"]
    assert row["parent_model_id"] is None and row["trained_through"] is None and row["backtest_metrics"] is None
    events = [e["event"] for e in conn.execute("SELECT event FROM job_events WHERE job_id = %s ORDER BY id", (job["id"],)).fetchall()]
    assert events == ["model_created"]
    # Idempotent: the same identity returns the existing row with created false (200).
    r = client.post("/api/v1/models", json=_body(job, summary="other text"), headers=w.headers)
    assert r.status_code == 200 and r.json() == {**body, "created": False}
    assert model_row(conn, body["id"])["summary"] == "Three sentences.", "the existing row is untouched"
    # Float formatting does not change the identity (6 decimals), a real change does.
    same = {**PARAMS, "k": 24.0000001}
    assert client.post("/api/v1/models", json=_body(job, params=same), headers=w.headers).json()["created"] is False
    other = client.post("/api/v1/models", json=_body(job, params={**PARAMS, "k": 25.0}), headers=w.headers).json()
    assert other["created"] is True and other["id"] != body["id"]
    assert conn.execute("SELECT count(*) AS n FROM models").fetchone()["n"] == 2
    # The worker view of a model.
    r = client.get(f"/api/v1/models/{body['id']}", headers=w.headers)
    assert r.status_code == 200
    assert set(r.json()) == {"id", "lineage_id", "family", "params", "artifact", "parent_model_id", "trained_through", "status", "backtest_metrics", "validation_metrics", "stress_metrics"}
    assert r.json()["params"] == PARAMS
    assert client.get(f"/api/v1/models/{uuid.uuid4()}", headers=w.headers).status_code == 404
    assert client.get("/api/v1/models/garbage", headers=w.headers).status_code == 404
    assert client.get(f"/api/v1/models/{body['id']}").status_code == 401


def test_root_with_good_metrics_is_paper_ok_at_creation(client, conn, make_worker):
    """A search posts its kept candidates with the validation-era metrics and the
    stress table (docs/ROBUSTNESS.md A1); the gate judges the validation era."""
    w = make_worker("box1", role="model_search")
    job = lease_job(conn, w, "model_search")
    validation, stress = validation_metrics(n_bets=90, roi=0.05), stress_metrics(seed=7)
    body = _body(job, backtest_metrics=backtest_metrics(n_bets=400, roi=0.04, max_drawdown=0.1), validation_metrics=validation, stress_metrics=stress)
    r = client.post("/api/v1/models", json=body, headers=w.headers)
    assert r.status_code == 201 and r.json()["status"] == "paper_ok"
    row = model_row(conn, r.json()["id"])
    assert row["status"] == "paper_ok" and row["validation_metrics"] == validation and row["stress_metrics"] == stress
    # Search-era numbers alone never promote while validation is required.
    unvalidated = _body(job, params={**PARAMS, "k": 30.0}, backtest_metrics=backtest_metrics(n_bets=400, roi=0.04, max_drawdown=0.1))
    r = client.post("/api/v1/models", json=unvalidated, headers=w.headers)
    assert r.status_code == 201 and r.json()["status"] == "candidate"
    assert model_row(conn, r.json()["id"])["validation_metrics"] is None
    flagged = _body(job, params={**PARAMS, "k": 31.0}, validation_metrics=validation_metrics(flags=["overfit"]), stress_metrics=stress)
    assert client.post("/api/v1/models", json=flagged, headers=w.headers).json()["status"] == "candidate"
    assert client.post("/api/v1/models", json={**flagged, "params": {**PARAMS, "k": 32.0}, "stress_metrics": []}, headers=w.headers).status_code == 400


def test_child_inherits_lineage_status_and_metrics(client, conn, make_worker):
    w = make_worker("box1", role="train")
    root = insert_validated_model(conn, params=PARAMS, metrics=backtest_metrics(n_bets=400, roi=0.04), status="paper_ok")
    job = lease_job(conn, w, "train", {"model_id": str(root["id"]), "through": {"season": 2024, "week": 10}})
    body = _body(job, parent_model_id=str(root["id"]), trained_through=[2024, 10], artifact={"ratings": {"KC": 1600.0}},
                 validation_metrics=validation_metrics(roi=-0.5), stress_metrics=stress_metrics(flags=["fragile"]))
    r = client.post("/api/v1/models", json=body, headers=w.headers)
    assert r.status_code == 201, r.text
    child = model_row(conn, r.json()["id"])
    assert r.json()["lineage_id"] == str(root["id"]) and r.json()["status"] == "paper_ok"
    assert child["lineage_id"] == root["id"] and child["parent_model_id"] == root["id"]
    assert child["status"] == "paper_ok" and child["backtest_metrics"] == root["backtest_metrics"]
    assert child["validation_metrics"] == root["validation_metrics"] and child["stress_metrics"] == root["stress_metrics"], "a child's own metrics are ignored"
    assert child["trained_through"] == [2024, 10] and child["artifact"] == {"ratings": {"KC": 1600.0}}
    # Same params trained to a different point is a new row; the same point is the same row.
    later = client.post("/api/v1/models", json={**body, "trained_through": [2024, 12]}, headers=w.headers)
    assert later.status_code == 201 and later.json()["created"] is True
    again = client.post("/api/v1/models", json=body, headers=w.headers)
    assert again.status_code == 200 and again.json()["id"] == str(child["id"])
    # trained_through may arrive as {"season", "week"}; a bad shape is 400, an unknown parent 400.
    as_dict = client.post("/api/v1/models", json={**body, "trained_through": {"season": 2024, "week": 10}}, headers=w.headers)
    assert as_dict.status_code == 200 and as_dict.json()["id"] == str(child["id"])
    assert client.post("/api/v1/models", json={**body, "trained_through": "2024"}, headers=w.headers).status_code == 400
    unknown_parent = {**body, "parent_model_id": str(uuid.uuid4()), "trained_through": [2025, 1]}
    other_job = lease_job(conn, w, "train", {"model_id": unknown_parent["parent_model_id"], "through": {"season": 2025, "week": 1}})
    assert client.post("/api/v1/models", json={**unknown_parent, "job_id": str(other_job["id"])}, headers=w.headers).status_code == 400
    assert client.post("/api/v1/models", json={**body, "family": "nope"}, headers=w.headers).status_code == 400
    assert client.post("/api/v1/models", json={**body, "params": []}, headers=w.headers).status_code == 400


def test_job_id_must_be_leased_by_the_caller(client, conn, make_worker):
    w = make_worker("box1", role="model_search")
    other = make_worker("box2", role="model_search")
    job = lease_job(conn, other, "model_search")
    r = client.post("/api/v1/models", json=_body(job), headers=w.headers)
    assert r.status_code == 409 and "leased" in r.json()["detail"]
    done = conn.execute("INSERT INTO jobs (kind, role, status) VALUES ('model_search', 'model_search', 'succeeded') RETURNING *").fetchone()
    assert client.post("/api/v1/models", json=_body(done), headers=w.headers).status_code == 409
    assert client.post("/api/v1/models", json=_body({"id": "garbage"}), headers=w.headers).status_code == 409
    assert client.post("/api/v1/models", json=_body(job), headers={"Authorization": "Bearer nope"}).status_code == 401
    assert conn.execute("SELECT count(*) AS n FROM models").fetchone()["n"] == 0
    mine = lease_job(conn, w, "model_search")
    assert client.post("/api/v1/models", json=_body(mine), headers=w.headers).status_code == 201
    model_id = conn.execute("SELECT id FROM models").fetchone()["id"]
    r = client.post(f"/api/v1/models/{model_id}/backtest", json={"job_id": str(job["id"]), "backtest_metrics": backtest_metrics()}, headers=w.headers)
    assert r.status_code == 409


def test_backtest_metrics_update_runs_eligibility_lineage_wide(client, conn, make_worker):
    """With require_validation off the search-era backtest is the gate, as in step 3."""
    set_setting(conn, "thresholds_backtest", {"min_bets": 200, "min_roi": 0.02, "max_drawdown": 0.3, "require_validation": False})
    w = make_worker("box1", role="backtest")
    root = insert_model(conn)
    child = insert_model(conn, parent=root, trained_through=[2024, 10])
    job = lease_job(conn, w, "backtest", {"model_id": str(child["id"])})
    good = backtest_metrics(n_bets=300, roi=0.05, max_drawdown=0.2)
    r = client.post(f"/api/v1/models/{child['id']}/backtest", json={"job_id": str(job["id"]), "backtest_metrics": good}, headers=w.headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": str(child["id"]), "lineage_id": str(root["id"]), "status": "paper_ok"}
    for m in (root, child):
        row = model_row(conn, m["id"])
        assert row["backtest_metrics"] == good and row["status"] == "paper_ok"
    bad = backtest_metrics(n_bets=300, roi=-0.01)
    assert client.post(f"/api/v1/models/{root['id']}/backtest", json={"job_id": str(job["id"]), "backtest_metrics": bad}, headers=w.headers).status_code == 409, "the job backtests the child, not the root"
    r = client.post(f"/api/v1/models/{child['id']}/backtest", json={"job_id": str(job["id"]), "backtest_metrics": bad}, headers=w.headers)
    assert r.json()["status"] == "candidate"
    assert model_row(conn, root["id"])["status"] == "candidate" and model_row(conn, root["id"])["backtest_metrics"] == bad
    assert client.post(f"/api/v1/models/{uuid.uuid4()}/backtest", json={"job_id": str(job["id"]), "backtest_metrics": good}, headers=w.headers).status_code == 404
    assert client.post(f"/api/v1/models/{child['id']}/backtest", json={"job_id": str(job["id"]), "backtest_metrics": []}, headers=w.headers).status_code == 400
    events = conn.execute("SELECT event, detail FROM job_events WHERE job_id = %s ORDER BY id", (job["id"],)).fetchall()
    assert [e["event"] for e in events] == ["model_backtest", "model_backtest"]
    assert events[0]["detail"] == {"model_id": str(child["id"]), "status": "paper_ok"}


def test_games_feed_with_etag(client, conn, make_worker):
    w = make_worker("box1")
    r = client.get("/api/v1/data/games", headers=w.headers)
    assert r.status_code == 200 and r.json() == {"games": [], "count": 0} and r.headers["etag"] == '"0-0"'
    ingest_fixture(conn)
    r = client.get("/api/v1/data/games", headers=w.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2761 and len(body["games"]) == 2761
    assert body["games"][0]["game_id"] == "2016_01_CAR_DEN" and body["games"][0]["kickoff_at"] == "2016-09-09T00:30:00Z"
    etag = r.headers["etag"]
    assert etag.startswith('"2761-') and etag.endswith('"')
    for header in (etag, etag.strip('"'), f"W/{etag}", f'"other", {etag}', "*"):
        again = client.get("/api/v1/data/games", headers={**w.headers, "If-None-Match": header})
        assert again.status_code == 304 and again.headers["etag"] == etag and again.content == b"", header
    assert client.get("/api/v1/data/games", headers={**w.headers, "If-None-Match": '"stale"'}).status_code == 200
    conn.execute("UPDATE games SET home_score = 99, updated_at = now() + interval '1 second' WHERE game_id = '2016_01_CAR_DEN'")
    r = client.get("/api/v1/data/games", headers={**w.headers, "If-None-Match": etag})
    assert r.status_code == 200 and r.headers["etag"] != etag
    assert client.get("/api/v1/data/games").status_code == 401
    assert client.get("/api/v1/data/games", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_owner_leaderboard_ranking_and_unranked(client, conn):
    """Ranked by the validation era: shrunk ROI, then the log-loss gain over the
    market; lineages without validation numbers are unranked as "not validated"."""
    best = insert_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, metrics=backtest_metrics(n_bets=400, roi=0.02, log_loss=0.65),
                        validation=validation_metrics(n_bets=400, roi=0.05, ci_roi=(0.02, 0.08), market_p=0.01, flags=["overfit"]),
                        stress=stress_metrics(flags=["regime_dependent"]), status="paper_ok", summary="Best.")
    insert_model(conn, parent=best, trained_through=[2024, 10])
    tie_a = insert_model(conn, params={"k": 30.0}, metrics=backtest_metrics(n_bets=900, roi=0.9), validation=validation_metrics(n_bets=100, roi=0.04, mean_ll_gain=0.001))
    tie_b = insert_model(conn, params={"k": 31.0}, metrics=backtest_metrics(n_bets=10, roi=-0.5), validation=validation_metrics(n_bets=100, roi=0.04, mean_ll_gain=0.003))
    search_only = insert_model(conn, params={"k": 32.0}, metrics=backtest_metrics(n_bets=490, roi=0.5))
    none = insert_model(conn, params={"k": 33.0})
    retired = insert_model(conn, params={"k": 34.0}, metrics=backtest_metrics(n_bets=900, roi=0.9), validation=validation_metrics(n_bets=900, roi=0.9), status="retired")
    r = client.get("/api/models")
    assert r.status_code == 200
    board = r.json()
    ranked = [m["id"] for m in board["ranked"]]
    assert ranked == [str(best["id"]), str(tie_b["id"]), str(tie_a["id"])], "validation shrunk ROI, then the larger log-loss gain"
    assert [m["rank"] for m in board["ranked"]] == [1, 2, 3] and all(m["rank_mode"] == "validation" for m in board["ranked"])
    unranked = {m["id"]: m for m in board["unranked"]}
    assert set(unranked) == {str(search_only["id"]), str(none["id"]), str(retired["id"])}
    assert unranked[str(search_only["id"])]["unranked_reason"] == "not validated" and unranked[str(none["id"])]["unranked_reason"] == "not validated"
    assert unranked[str(retired["id"])]["unranked_reason"] == "retired" and unranked[str(search_only["id"])]["validated"] is False
    assert unranked[str(search_only["id"])]["validation"] is None and unranked[str(search_only["id"])]["score"] == 0.0
    assert unranked[str(search_only["id"])]["search_score"] == 0.5 * 490 / 590
    top = board["ranked"][0]
    assert top["short_params"] == "K 20 · HFA 50 · MOV on" and top["members"] == 2 and top["status"] == "paper_ok"
    assert top["summary"] == "Best." and top["lineage_id"] == str(best["id"]) and top["family"] == "elo_blend"
    assert top["score"] == 0.05 * 400 / 500 and top["search_score"] == 0.02 * 400 / 500 and top["validated"] is True
    assert set(top["metrics"]) >= {"roi", "n_bets", "log_loss", "market_log_loss", "max_drawdown", "seasons"}
    assert top["validation"]["ci"]["roi"] == [0.02, 0.08] and top["validation"]["market_p"] == 0.01 and top["validation"]["beats_market"] is True
    assert top["validation"]["shrunk_roi"] == 0.05 * 400 / 500 and top["validation"]["n_bets"] == 400 and top["ll_gain"] == 0.002
    assert top["flags"] == ["overfit", "regime_dependent"] and top["stress_flags"] == ["regime_dependent"] and top["paper_ci"] is None
    assert "rank" not in board["unranked"][0]
    detail = client.get(f"/api/models/{best['id']}").json()
    assert detail["id"] == str(best["id"]) and len(detail["lineage"]) == 2 and detail["lineage"][0]["is_root"] is True
    assert detail["jobs"] == [] and detail["short_params"] == "K 20 · HFA 50 · MOV on" and detail["params"]["k"] == 20.0
    assert detail["flags"] == ["overfit", "regime_dependent"] and set(detail["flag_meanings"]) == {"overfit", "regime_dependent"}
    assert detail["validation_metrics"]["market_p"] == 0.01 and detail["stress_metrics"]["seed"] == 1 and detail["paper_ci"] is None
    assert detail["validated"] is True and detail["score"] == top["score"] and detail["rank_mode"] == "validation"
    assert client.get(f"/api/models/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/models/garbage").status_code == 404


def test_owner_summary_limit_and_retire(client, conn):
    root = insert_model(conn, metrics=backtest_metrics(), status="paper_ok", summary="old")
    child = insert_model(conn, parent=root, trained_through=[2024, 10])
    r = client.post(f"/api/models/{root['id']}/summary", json={"summary": "x" * 600})
    assert r.status_code == 200 and r.json()["summary"] == "x" * 600
    r = client.post(f"/api/models/{root['id']}/summary", json={"summary": "x" * 601})
    assert r.status_code == 400 and "600" in r.json()["detail"]
    assert model_row(conn, root["id"])["summary"] == "x" * 600
    assert client.post(f"/api/models/{root['id']}/summary", json={"summary": "  trimmed  "}).json()["summary"] == "trimmed"
    assert client.post(f"/api/models/{root['id']}/summary", json={"summary": ""}).json()["summary"] is None
    assert client.post(f"/api/models/{root['id']}/summary", json={}).status_code == 400
    assert client.post(f"/api/models/{uuid.uuid4()}/summary", json={"summary": "x"}).status_code == 404
    # Status: only retired, applied to the lineage, excluded from ranking, audited.
    assert client.post(f"/api/models/{root['id']}/status", json={"status": "live_eligible"}).status_code == 400
    assert client.post(f"/api/models/{root['id']}/status", json={"status": "candidate"}).status_code == 400
    assert model_row(conn, root["id"])["status"] == "paper_ok"
    r = client.post(f"/api/models/{child['id']}/status", json={"status": "retired"})
    assert r.status_code == 200 and r.json()["status"] == "retired"
    assert model_row(conn, root["id"])["status"] == "retired" and model_row(conn, child["id"])["status"] == "retired"
    board = client.get("/api/models").json()
    assert board["ranked"] == [] and [m["id"] for m in board["unranked"]] == [str(root["id"])]
    # Fresh metrics never un-retire a lineage.
    w = client.post("/api/enroll-token").json()["token"]
    reg = client.post("/api/v1/workers/register", json={"enroll_token": w, "hostname": "box1"}).json()
    from tests.conftest import FakeWorker

    worker = FakeWorker(reg["worker_id"], reg["worker_token"])
    job = lease_job(conn, worker, "backtest", {"model_id": str(root["id"])})
    r = client.post(f"/api/v1/models/{root['id']}/backtest", json={"job_id": str(job["id"]), "backtest_metrics": backtest_metrics(n_bets=999, roi=0.3)}, headers=worker.headers)
    assert r.json()["status"] == "retired"
    audit = conn.execute("SELECT action, entity, before, after FROM audit_log WHERE action LIKE 'model_%%' ORDER BY id").fetchall()
    assert [a["action"] for a in audit] == ["model_summary", "model_summary", "model_summary", "model_retired"]
    assert audit[0]["entity"] == str(root["id"]) and audit[0]["before"] == {"summary": "old"} and audit[0]["after"] == {"summary": "x" * 600}
    assert audit[-1] == {"action": "model_retired", "entity": str(child["id"]), "before": {"status": "paper_ok"}, "after": {"status": "retired", "assignments_halted": []}}


def test_data_refresh_route_reads_the_configured_url(client, conn, monkeypatch):
    from host import nflverse
    from tests.conftest import FIXTURE_GAMES

    seen = {}

    def fake_fetch(url, timeout=60, max_bytes=0):
        seen["url"] = url
        return FIXTURE_GAMES.read_text(encoding="utf-8")

    monkeypatch.setattr(nflverse, "fetch", fake_fetch)
    client.post("/api/settings", json={"nflverse_url": "https://example.invalid/games.csv"})
    r = client.post("/api/data/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"] == 2761 and body["updated"] == 2761 and body["fetched_at"].endswith("Z")
    assert seen["url"] == "https://example.invalid/games.csv"
    assert conn.execute("SELECT count(*) AS n FROM games").fetchone()["n"] == 2761

    def failing(url, timeout=60, max_bytes=0):
        raise nflverse.Upstream("nflverse fetch failed: boom")

    monkeypatch.setattr(nflverse, "fetch", failing)
    r = client.post("/api/data/refresh")
    assert r.status_code == 502 and "boom" in r.json()["detail"]
    assert client.post("/api/settings", json={"nflverse_url": "ftp://x"}).status_code == 400
    assert client.post("/api/settings", json={"nflverse_refresh_hours": 0}).status_code == 400
    assert client.post("/api/settings", json={"fee_model": {"taker_rate": 0.05}}).status_code == 400
    assert client.post("/api/settings", json={"fee_model": {"taker_rate": 2, "half_spread": 0.01}}).status_code == 400
    assert client.post("/api/settings", json={"thresholds_backtest": {"min_bets": -1, "min_roi": 0.0, "max_drawdown": 0.3}}).status_code == 400
    assert client.post("/api/settings", json={"backtest_seasons": [2020, 2010]}).status_code == 400
    assert client.post("/api/settings", json={"backtest_seasons": [2010]}).status_code == 400
    assert client.post("/api/settings", json={"backtest_seasons": [2012, None], "nflverse_refresh_hours": 12,
                                              "fee_model": {"taker_rate": 0.02, "half_spread": 0.005},
                                              "thresholds_backtest": {"min_bets": 100, "min_roi": 0.01, "max_drawdown": 0.25}}).status_code == 200


def test_thresholds_change_recomputes_lineages(client, conn):
    root = insert_validated_model(conn, validation=validation_metrics(n_bets=120, roi=0.03, ci_roi=(0.005, 0.06), market_p=0.08))
    set_setting(conn, "thresholds_backtest", {"min_bets": 200, "min_roi": 0.02, "max_drawdown": 0.3})
    assert model_row(conn, root["id"])["status"] == "candidate"
    r = client.post("/api/settings", json={"thresholds_backtest": {"min_bets": 100, "min_roi": 0.02, "max_drawdown": 0.3}})
    assert r.status_code == 200, "a legacy three-key object is accepted; the new rules keep their defaults"
    assert model_row(conn, root["id"])["status"] == "paper_ok"
    client.post("/api/settings", json={"thresholds_backtest": {"min_bets": 200, "min_roi": 0.02, "max_drawdown": 0.3}})
    assert model_row(conn, root["id"])["status"] == "candidate"
    full = {"min_bets": 100, "min_roi": 0.02, "max_drawdown": 0.3, "require_validation": True, "min_roi_ci_low": 0.005, "max_market_p": 0.05, "forbid_flags": []}
    assert client.post("/api/settings", json={"thresholds_backtest": full}).status_code == 200
    assert model_row(conn, root["id"])["status"] == "candidate", "market_p 0.08 is over 0.05"
    assert client.post("/api/settings", json={"thresholds_backtest": {**full, "max_market_p": 0.1}}).status_code == 200
    assert model_row(conn, root["id"])["status"] == "paper_ok", "CI lower bound exactly at the floor passes"
    assert client.post("/api/settings", json={"thresholds_backtest": {**full, "max_market_p": 0.1, "min_roi_ci_low": 0.0051}}).status_code == 200
    assert model_row(conn, root["id"])["status"] == "candidate"


def test_job_must_match_the_write_it_reports(client, conn, make_worker):
    """MEDIUM: a lease on some other job (a sleep, a backtest of another model, a
    search) cannot create models in a lineage or overwrite and promote its metrics."""
    w = make_worker("box1", role="backtest")
    a = insert_model(conn, params={"k": 20.0})
    b = insert_model(conn, params={"k": 21.0}, metrics=backtest_metrics(n_bets=10))
    good = backtest_metrics(n_bets=400, roi=0.5)
    sleep = lease_job(conn, w, "sleep", {"seconds": 5})
    r = client.post(f"/api/v1/models/{b['id']}/backtest", json={"job_id": str(sleep["id"]), "backtest_metrics": good}, headers=w.headers)
    assert r.status_code == 409 and "sleep job" in r.json()["detail"]
    r = client.post("/api/v1/models", json=_body(sleep), headers=w.headers)
    assert r.status_code == 409 and "sleep job" in r.json()["detail"]
    other = lease_job(conn, w, "backtest", {"model_id": str(a["id"])})
    r = client.post(f"/api/v1/models/{b['id']}/backtest", json={"job_id": str(other["id"]), "backtest_metrics": good}, headers=w.headers)
    assert r.status_code == 409 and "another model" in r.json()["detail"]
    assert model_row(conn, b["id"])["backtest_metrics"]["n_bets"] == 10 and model_row(conn, b["id"])["status"] == "candidate"
    search = lease_job(conn, w, "model_search", {"family": "elo_blend"})
    child = _body(search, params={"k": 20.0}, parent_model_id=str(a["id"]), trained_through=[2024, 10])
    assert client.post("/api/v1/models", json=child, headers=w.headers).status_code == 409, "a child needs a train job"
    train_other = lease_job(conn, w, "train", {"model_id": str(b["id"]), "through": {"season": 2024, "week": 10}})
    assert client.post("/api/v1/models", json={**child, "job_id": str(train_other["id"])}, headers=w.headers).status_code == 409
    train = lease_job(conn, w, "train", {"model_id": str(a["id"]), "through": {"season": 2024, "week": 10}})
    grafted = {**child, "job_id": str(train["id"]), "params": {"k": 99.0}}
    r = client.post("/api/v1/models", json=grafted, headers=w.headers)
    assert r.status_code == 400 and "parent" in r.json()["detail"]
    assert client.post("/api/v1/models", json={**child, "job_id": str(train["id"])}, headers=w.headers).status_code == 201
    assert client.post("/api/v1/models", json=_body(train, params={"k": 50.0}), headers=w.headers).status_code == 409, "a root needs a search job"
    assert conn.execute("SELECT count(*) AS n FROM models").fetchone()["n"] == 3


def test_identity_hit_takes_the_latest_metrics(client, conn, make_worker):
    """MEDIUM: a second search that evaluates the same params replaces the stored
    metrics (latest evaluation wins) instead of keeping the first, maybe empty, run."""
    w = make_worker("box1", role="model_search")
    first = lease_job(conn, w, "model_search", {"family": "elo_blend", "seasons": [2030, 2030]})
    empty = backtest_metrics(n_bets=0, roi=0.0, seasons=[])
    empty["n_games"] = 0
    r = client.post("/api/v1/models", json=_body(first, backtest_metrics=empty), headers=w.headers)
    assert r.status_code == 201 and r.json()["status"] == "candidate"
    model_id = r.json()["id"]
    second = lease_job(conn, w, "model_search", {"family": "elo_blend", "seasons": [2010, 2021]})
    real = backtest_metrics(n_bets=400, roi=0.04, max_drawdown=0.1, seasons=list(range(2010, 2022)))
    validation, stress = validation_metrics(), stress_metrics(seed=3)
    r = client.post("/api/v1/models", json=_body(second, backtest_metrics=real, validation_metrics=validation, stress_metrics=stress, summary="newer"), headers=w.headers)
    assert r.status_code == 200 and r.json() == {"id": model_id, "lineage_id": model_id, "created": False, "status": "paper_ok"}
    row = model_row(conn, model_id)
    assert row["backtest_metrics"] == real and row["status"] == "paper_ok" and row["summary"] is None
    assert row["validation_metrics"] == validation and row["stress_metrics"] == stress
    events = conn.execute("SELECT event, detail FROM job_events WHERE job_id = %s", (second["id"],)).fetchall()
    assert events[0]["event"] == "model_exists" and events[0]["detail"]["status"] == "paper_ok"
    # A post without metrics keeps what is stored; a child's identity hit never touches the lineage.
    assert client.post("/api/v1/models", json=_body(second), headers=w.headers).json()["status"] == "paper_ok"
    assert model_row(conn, model_id)["backtest_metrics"] == real and model_row(conn, model_id)["validation_metrics"] == validation
    # Newer validation numbers alone replace the stored ones and re-run the gate.
    worse = validation_metrics(roi=-0.02)
    assert client.post("/api/v1/models", json=_body(second, validation_metrics=worse), headers=w.headers).json()["status"] == "candidate"
    assert model_row(conn, model_id)["validation_metrics"] == worse and model_row(conn, model_id)["stress_metrics"] == stress


def test_non_finite_numbers_are_a_400_not_a_500(client, conn, make_worker):
    """LOW: NaN or Infinity anywhere in the body is refused (Postgres jsonb rejects it),
    so the agent fails the job instead of retrying a 500 forever."""
    w = make_worker("box1", role="model_search")
    job = lease_job(conn, w, "model_search")
    headers = {**w.headers, "Content-Type": "application/json"}
    base = {"job_id": str(job["id"]), "family": "elo_blend", "params": {"k": 24.0}}
    for field, value in (("params", '{"k": NaN}'), ("artifact", '{"ratings": {"KC": Infinity}}'), ("backtest_metrics", '{"roi": -Infinity}')):
        body = ", ".join(f'"{k}": {json.dumps(v)}' for k, v in base.items() if k != field) + f', "{field}": {value}'
        r = client.post("/api/v1/models", content="{" + body + "}", headers=headers)
        assert r.status_code == 400 and "NaN or Infinity" in r.json()["detail"], (field, r.text)
    assert client.post("/api/v1/models", json={**base, "params": {"k": "fast"}}, headers=w.headers).status_code == 400
    assert client.post("/api/v1/models", json=base, headers=w.headers).status_code == 201
    model_id = conn.execute("SELECT id FROM models").fetchone()["id"]
    bt = lease_job(conn, w, "backtest", {"model_id": str(model_id)})
    content = '{"job_id": "%s", "backtest_metrics": {"roi": NaN}}' % bt["id"]
    assert client.post(f"/api/v1/models/{model_id}/backtest", content=content, headers=headers).status_code == 400


def _run_in_thread(target):
    import threading

    box = {}

    def run():
        try:
            box["value"] = target()
        except Exception as exc:  # noqa: BLE001 - surfaced by the test
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


def test_thresholds_change_and_model_write_do_not_race(pool, conn, make_worker):
    """MEDIUM: a root posted while min_bets is being raised ends up judged by the new
    thresholds, because the model write waits for the thresholds transaction."""
    import time

    from host import eligibility, models
    from host.settings import set_settings

    w = make_worker("box1", role="model_search")
    job = lease_job(conn, w, "model_search")
    body = _body(job, backtest_metrics=backtest_metrics(n_bets=400, roi=0.04, max_drawdown=0.1))
    with pool.connection() as settings_conn:
        set_settings(settings_conn, {"thresholds_backtest": {"min_bets": 1000, "min_roi": 0.02, "max_drawdown": 0.3}}, "owner")

        def post():
            with pool.connection() as c:
                return models.create_model(c, body, w.id)[0]["status"]

        thread, box = _run_in_thread(post)
        time.sleep(0.5)
        assert thread.is_alive(), "the model write waits for the in-flight thresholds change"
        assert eligibility.recompute_all(settings_conn) == 0, "the uncommitted row is not visible yet"
        settings_conn.commit()
    thread.join(timeout=10)
    assert box.get("value") == "candidate", box
    row = conn.execute("SELECT status FROM models").fetchone()
    assert row["status"] == "candidate" and eligibility.thresholds(conn)["min_bets"] == 1000


def test_child_creation_waits_for_a_committing_backtest(pool, conn, make_worker):
    """LOW: a child posted while the lineage's metrics are being replaced inherits the
    new metrics (the parent row is locked), not the old ones."""
    import time

    from host import models

    w = make_worker("box1", role="train")
    root = insert_validated_model(conn, params=PARAMS, metrics=backtest_metrics(n_bets=10))
    bt = lease_job(conn, w, "backtest", {"model_id": str(root["id"])})
    train = lease_job(conn, w, "train", {"model_id": str(root["id"]), "through": {"season": 2024, "week": 10}})
    child_body = _body(train, parent_model_id=str(root["id"]), trained_through=[2024, 10])
    with pool.connection() as bt_conn:
        models.set_backtest_metrics(bt_conn, root["id"], backtest_metrics(n_bets=999, roi=0.1), bt["id"], w.id)

        def post():
            with pool.connection() as c:
                return models.create_model(c, child_body, w.id)[0]

        thread, box = _run_in_thread(post)
        time.sleep(0.5)
        assert thread.is_alive(), "the child waits for the backtest to commit"
        bt_conn.commit()
    thread.join(timeout=10)
    assert "error" not in box, box
    assert box["value"]["backtest_metrics"]["n_bets"] == 999 and box["value"]["status"] == "paper_ok"
    assert model_row(conn, box["value"]["id"])["backtest_metrics"]["n_bets"] == 999


def test_validation_endpoint_attribution_and_lineage_propagation(client, conn, make_worker):
    """POST /api/v1/models/{id}/validation: the job must be the validate job of that
    model or the search job that created it; the metrics land on every row of the
    lineage and the gate runs on them."""
    w = make_worker("box1", role="backtest")
    root = insert_model(conn, params=PARAMS, metrics=backtest_metrics(n_bets=400, roi=0.05))
    child = insert_model(conn, parent=root, trained_through=[2024, 10])
    job = lease_job(conn, w, "validate", {"model_id": str(child["id"]), "seed": 1})
    good = {"job_id": str(job["id"]), "validation_metrics": validation_metrics(n_bets=90, roi=0.05), "stress_metrics": stress_metrics(seed=1)}
    assert client.post(f"/api/v1/models/{root['id']}/validation", json=good, headers=w.headers).status_code == 409, "the job validates the child"
    r = client.post(f"/api/v1/models/{child['id']}/validation", json=good, headers=w.headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": str(child["id"]), "lineage_id": str(root["id"]), "status": "paper_ok"}
    for m in (root, child):
        row = model_row(conn, m["id"])
        assert row["validation_metrics"] == good["validation_metrics"] and row["stress_metrics"] == good["stress_metrics"]
        assert row["status"] == "paper_ok" and row["backtest_metrics"]["n_bets"] == 400, "the search-era metrics stay"
    fragile = {**good, "stress_metrics": stress_metrics(flags=["fragile"])}
    assert client.post(f"/api/v1/models/{child['id']}/validation", json=fragile, headers=w.headers).json()["status"] == "candidate"
    assert model_row(conn, root["id"])["status"] == "candidate" and model_row(conn, root["id"])["stress_metrics"]["flags"] == ["fragile"]
    events = conn.execute("SELECT event, detail FROM job_events WHERE job_id = %s ORDER BY id", (job["id"],)).fetchall()
    assert [e["event"] for e in events] == ["model_validation", "model_validation"]
    assert events[0]["detail"] == {"model_id": str(child["id"]), "status": "paper_ok"}
    # Fencing: another worker's lease, a sleep job, a backtest job, a search job that did not create the model.
    other = make_worker("box2", role="backtest")
    assert client.post(f"/api/v1/models/{child['id']}/validation", json=good, headers=other.headers).status_code == 409
    for kind, params in (("sleep", {"seconds": 5}), ("backtest", {"model_id": str(child["id"])}), ("model_search", {"family": "elo_blend"})):
        wrong = lease_job(conn, w, kind, params)
        r = client.post(f"/api/v1/models/{child['id']}/validation", json={**good, "job_id": str(wrong["id"])}, headers=w.headers)
        assert r.status_code == 409, (kind, r.text)
    # The search job that created (or found) the model may post its validation.
    search = lease_job(conn, w, "model_search", {"family": "elo_blend"})
    created = client.post("/api/v1/models", json=_body(search, params={**PARAMS, "k": 40.0}), headers=w.headers).json()
    r = client.post(f"/api/v1/models/{created['id']}/validation", json={**good, "job_id": str(search["id"])}, headers=w.headers)
    assert r.status_code == 200 and r.json()["status"] == "paper_ok"
    assert client.post(f"/api/v1/models/{child['id']}/validation", json={**good, "job_id": str(search["id"])}, headers=w.headers).status_code == 409
    # Bad bodies and unknown models.
    assert client.post(f"/api/v1/models/{child['id']}/validation", json={**good, "validation_metrics": []}, headers=w.headers).status_code == 400
    assert client.post(f"/api/v1/models/{child['id']}/validation", json={"job_id": str(job["id"]), "validation_metrics": {}}, headers=w.headers).status_code == 400, "stress_metrics is required"
    nan = '{"job_id": "%s", "validation_metrics": {"roi": NaN}, "stress_metrics": {}}' % job["id"]
    assert client.post(f"/api/v1/models/{child['id']}/validation", content=nan, headers={**w.headers, "Content-Type": "application/json"}).status_code == 400
    assert client.post(f"/api/v1/models/{uuid.uuid4()}/validation", json=good, headers=w.headers).status_code == 404
    assert client.post(f"/api/v1/models/{child['id']}/validation", json=good).status_code == 401
    assert model_row(conn, root["id"])["stress_metrics"]["flags"] == ["fragile"], "nothing above changed the lineage"
