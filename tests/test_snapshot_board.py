"""Step 6 Part B (docs/ROBUSTNESS.md B1): a snapshot replay result is stored in
models.snapshot_metrics on the whole lineage, the leaderboard shows the snapshot
column group and ranks paper > snapshot (30 bets, shrunk CLV) > validation."""
from __future__ import annotations

from typing import Any

import pytest
from psycopg.types.json import Jsonb

from host.leaderboard_snapshot import rank_mode, shrunk_snapshot_clv, snapshot_clv, snapshot_summary
from tests.conftest import (
    backtest_metrics, insert_game, insert_model, insert_validated_model, lease_job, model_row, set_setting,
    validation_metrics,
)
from tests.pagecheck import page

PARAMS = {"k": 24.0, "hfa": 55.0, "regress": 0.33, "rest_per_day": 1.0, "mov_scale": 1, "min_edge": 0.03, "kelly_fraction": 0.25}


def snapshot_metrics(n_bets: int = 40, roi: float = 0.03, clv: float | None = 0.02, ci_clv: tuple[float, float] = (0.004, 0.031),
                     n_unscored: int = 7, **extra: Any) -> dict[str, Any]:
    """A snapshot replay result: the backtest shape plus price_source, platform,
    n_unscored_no_prices and a real CLV with its bootstrap range."""
    metrics = backtest_metrics(n_bets=n_bets, roi=roi, seasons=[2025])
    metrics.update({
        "n_games": n_bets * 2, "price_source": "snapshots", "platform": "polymarket_us", "n_unscored_no_prices": n_unscored,
        "ci": {"roi": [roi - 0.05, roi + 0.05], "avg_clv": list(ci_clv), "max_drawdown": [0.05, 0.2], "hit_rate": [0.4, 0.6],
               "avg_edge": [0.02, 0.05]},
        "per_season": [{"season": 2025, "n_games": n_bets * 2, "n_bets": n_bets, "roi": roi, "pnl_cents": 1000, "log_loss": 0.66,
                        "market_log_loss": 0.661, "max_drawdown": 0.1, "n_unscored_no_prices": n_unscored}],
    })
    if clv is not None:
        metrics["avg_clv"] = clv
    metrics.update(extra)
    return metrics


def with_snapshot(conn, model: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    conn.execute("UPDATE models SET snapshot_metrics = %s WHERE lineage_id = %s", (Jsonb(metrics), model["lineage_id"]))
    return model_row(conn, model["id"])


def paper_score(conn, model: dict[str, Any], games: int, bets_each: int, clv: float) -> None:
    for i in range(games):
        game = insert_game(conn, f"2026_1{i}_SN_AP", kickoff_in_s=-(i + 1) * 86400)
        conn.execute(
            "INSERT INTO model_scores (model_id, game_id, mode, lineage_id, n_bets, stake_cents, pnl_cents, avg_clv)"
            " VALUES (%s, %s, 'paper', %s, %s, 6000, 60, %s)",
            (model["id"], game["game_id"], model["lineage_id"], bets_each, clv),
        )


# ------------------------------------------------------------------ storage routing

def test_snapshot_result_goes_to_snapshot_metrics_on_the_whole_lineage(client, conn, make_worker):
    set_setting(conn, "thresholds_backtest", {"min_bets": 10, "min_roi": 0.0, "max_drawdown": 0.9, "require_validation": False})
    w = make_worker("box1", role="backtest")
    search = backtest_metrics(n_bets=300, roi=-0.02)
    root = insert_model(conn, params=PARAMS, metrics=search)
    child = insert_model(conn, parent=root, trained_through=[2025, 10])
    job = lease_job(conn, w, "backtest", {"model_id": str(child["id"]), "price_source": "snapshots"})
    good = snapshot_metrics(n_bets=300, roi=0.2)
    r = client.post(f"/api/v1/models/{child['id']}/backtest", json={"job_id": str(job["id"]), "backtest_metrics": good}, headers=w.headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": str(child["id"]), "lineage_id": str(root["id"]), "status": "candidate"}
    for m in (root, child):
        row = model_row(conn, m["id"])
        assert row["snapshot_metrics"] == good, "stored on every row of the lineage"
        assert row["backtest_metrics"] == search, "the closing-line numbers are never overwritten"
        assert row["status"] == "candidate", "snapshot metrics do not move eligibility in this step"
    event = conn.execute("SELECT event, detail FROM job_events WHERE job_id = %s", (job["id"],)).fetchone()
    assert event["event"] == "model_snapshot_backtest" and event["detail"]["n_bets"] == 300
    # The metrics must name the job's price source, both ways.
    plain = backtest_metrics(n_bets=300, roi=0.2)
    r = client.post(f"/api/v1/models/{child['id']}/backtest", json={"job_id": str(job["id"]), "backtest_metrics": plain}, headers=w.headers)
    assert r.status_code == 400 and "price_source" in r.text
    closing = lease_job(conn, w, "backtest", {"model_id": str(child["id"]), "price_source": "closing_line"})
    r = client.post(f"/api/v1/models/{child['id']}/backtest", json={"job_id": str(closing["id"]), "backtest_metrics": good}, headers=w.headers)
    assert r.status_code == 400
    assert model_row(conn, root["id"])["backtest_metrics"] == search
    # A closing-line result still lands in backtest_metrics (and runs eligibility); snapshot numbers stay.
    r = client.post(f"/api/v1/models/{child['id']}/backtest", json={"job_id": str(closing["id"]), "backtest_metrics": plain}, headers=w.headers)
    assert r.status_code == 200 and r.json()["status"] == "paper_ok"
    row = model_row(conn, root["id"])
    assert row["backtest_metrics"] == plain and row["snapshot_metrics"] == good


def test_a_later_child_inherits_the_snapshot_metrics(client, conn, make_worker):
    w = make_worker("box1", role="train")
    root = with_snapshot(conn, insert_validated_model(conn, params=PARAMS), snapshot_metrics())
    job = lease_job(conn, w, "train", {"model_id": str(root["id"]), "through": {"season": 2025, "week": 3}})
    body = {"job_id": str(job["id"]), "family": "elo_blend", "params": PARAMS, "artifact": None, "backtest_metrics": None,
            "summary": None, "parent_model_id": str(root["id"]), "trained_through": [2025, 3]}
    r = client.post("/api/v1/models", json=body, headers=w.headers)
    assert r.status_code == 201, r.text
    assert model_row(conn, r.json()["id"])["snapshot_metrics"] == root["snapshot_metrics"]


# ------------------------------------------------------------------ summary and rank basis

def test_snapshot_summary_and_clv_fallback():
    assert snapshot_summary({"snapshot_metrics": None}) is None
    s = snapshot_summary({"snapshot_metrics": snapshot_metrics(n_bets=30, clv=0.02)})
    assert s["n_bets"] == 30 and s["avg_clv"] == 0.02 and s["clv_ci"] == [0.004, 0.031] and s["clv_estimated"] is False
    assert s["score"] == pytest.approx(0.02 * 30 / 55) and s["platform"] == "polymarket_us" and s["n_unscored_no_prices"] == 7
    estimated = snapshot_summary({"snapshot_metrics": snapshot_metrics(clv=None, ci_clv=(0.01, 0.03))})
    assert estimated["avg_clv"] == pytest.approx(0.02) and estimated["clv_estimated"] is True
    assert snapshot_clv({"ci": {}}) == (None, False) and shrunk_snapshot_clv(0, 0.5) == 0.0
    none_bet = snapshot_summary({"snapshot_metrics": snapshot_metrics(n_bets=0)})
    assert none_bet["roi"] is None and none_bet["score"] == 0.0


def test_rank_mode_precedence_and_the_30_bet_boundary():
    at29 = snapshot_summary({"snapshot_metrics": snapshot_metrics(n_bets=29)})
    at30 = snapshot_summary({"snapshot_metrics": snapshot_metrics(n_bets=30)})
    assert rank_mode(True, at30) == "paper"
    assert rank_mode(False, at30) == "snapshot"
    assert rank_mode(False, at29) == "validation"
    assert rank_mode(False, None) == "validation"
    no_clv = snapshot_summary({"snapshot_metrics": snapshot_metrics(n_bets=50, clv=None, ci=None)})
    assert rank_mode(False, no_clv) == "validation", "a snapshot result without any CLV cannot rank on it"


def test_leaderboard_order_with_three_rank_bases(client, conn):
    papered = with_snapshot(conn, insert_validated_model(conn, params={"k": 1.0}), snapshot_metrics(n_bets=200, clv=0.09))
    paper_score(conn, papered, games=5, bets_each=6, clv=0.001)
    snap_low = with_snapshot(conn, insert_validated_model(conn, params={"k": 2.0}), snapshot_metrics(n_bets=30, clv=0.02, roi=0.01))
    snap_high = with_snapshot(conn, insert_validated_model(conn, params={"k": 3.0}), snapshot_metrics(n_bets=40, clv=0.03))
    at29 = with_snapshot(conn, insert_validated_model(conn, params={"k": 4.0}, validation=validation_metrics(n_bets=200, roi=0.01)),
                         snapshot_metrics(n_bets=29, clv=0.5))
    validated = insert_validated_model(conn, params={"k": 5.0}, validation=validation_metrics(n_bets=200, roi=0.05))
    unvalidated29 = with_snapshot(conn, insert_model(conn, params={"k": 6.0}), snapshot_metrics(n_bets=29, clv=0.5))
    retired = with_snapshot(conn, insert_model(conn, params={"k": 7.0}, status="retired"), snapshot_metrics(n_bets=500, clv=0.5))
    unvalidated_snap = with_snapshot(conn, insert_model(conn, params={"k": 8.0}), snapshot_metrics(n_bets=400, clv=0.5))
    board = client.get("/api/models").json()
    ranked = board["ranked"]
    assert [m["id"] for m in ranked] == [str(m["id"]) for m in (papered, snap_high, snap_low, validated, at29)]
    assert [m["rank_mode"] for m in ranked] == ["paper", "snapshot", "snapshot", "validation", "validation"]
    assert [m["rank"] for m in ranked] == [1, 2, 3, 4, 5]
    assert ranked[1]["snapshot_score"] == pytest.approx(0.03 * 40 / 65) and ranked[2]["snapshot_score"] == pytest.approx(0.02 * 30 / 55)
    assert all(m["validated"] for m in ranked), "only lineages the held-out era has judged rank (step 6A review)"
    assert ranked[4]["snapshot"]["n_bets"] == 29, "29 snapshot bets: still ranked on the validation era"
    unranked = {m["id"]: m for m in board["unranked"]}
    assert unranked[str(unvalidated29["id"])]["unranked_reason"] == "not validated"
    assert unranked[str(retired["id"])]["unranked_reason"] == "retired"
    assert unranked[str(unvalidated29["id"])]["rank_mode"] == "validation"
    assert unranked[str(unvalidated_snap["id"])]["unranked_reason"] == "not validated", \
        "400 snapshot bets never rank a lineage that is not validated"
    # One more snapshot bet promotes the 29-bet lineage onto the snapshot basis.
    with_snapshot(conn, at29, snapshot_metrics(n_bets=30, clv=0.5))
    ranked = client.get("/api/models").json()["ranked"]
    assert ranked[1]["id"] == str(at29["id"]) and ranked[1]["rank_mode"] == "snapshot"


def test_snapshot_ties_break_on_roi(client, conn):
    worse = with_snapshot(conn, insert_validated_model(conn, params={"k": 1.0}), snapshot_metrics(n_bets=30, clv=0.02, roi=0.01))
    better = with_snapshot(conn, insert_validated_model(conn, params={"k": 2.0}), snapshot_metrics(n_bets=30, clv=0.02, roi=0.04))
    ranked = client.get("/api/models").json()["ranked"]
    assert [m["id"] for m in ranked] == [str(better["id"]), str(worse["id"])]


# ------------------------------------------------------------------ pages

def test_models_page_shows_the_snapshot_group(client, conn):
    ranked = with_snapshot(conn, insert_validated_model(conn, params={"k": 1.0}), snapshot_metrics(n_bets=30, clv=0.02, roi=0.031))
    plain = insert_validated_model(conn, params={"k": 2.0})
    p = page(client.get("/models").text)
    assert "CLV 90% range" in p.card("ranked").text, "the snapshot column group names its range"
    assert "30 bets replayed on recorded prices" in p.text
    row = p.row("model", ranked["id"])
    chip = row.chip("rank-snapshot")
    assert row.text.startswith("#1") and chip.text == "snapshot" and chip.attr("title") == "ranked on snapshot replay CLV"
    assert "60 games · 30 bets · ROI +3.1%" in row.text and "CLV 0.020" in row.text and "0.004 to 0.031" in row.text
    other = p.row("model", plain["id"])
    assert "snapshot -" in other.text and "rank-snapshot" not in other.chips()


def test_model_page_shows_the_snapshot_section(client, conn):
    model = with_snapshot(conn, insert_validated_model(conn, params={"k": 1.0}), snapshot_metrics(n_bets=29, clv=0.02))
    p = page(client.get(f"/models/{model['id']}").text)
    assert p.prop("snapshot replay").startswith("58 games · 29 bets · ROI +3.0% · CLV 0.020")
    assert "ranks on it from 30 bets" in p.prop("snapshot replay")
    section = p.card("snapshot")
    assert "Replayed on recorded polymarket_us prices: 58 games scored, 29 bets, ROI +3.0%." in section.text
    assert "0.020 per contract (90% range 0.004 to 0.031)" in section.text
    assert "7 games skipped for lack of recorded prices" in section.text and "snapshot per season" in section.text
    replay = section.action("replay-snapshots")
    assert replay.target == "/jobs" and replay.one('input[name="price_source"]').attr("value") == "snapshots"
    bare = insert_model(conn, params={"k": 2.0})
    p = page(client.get(f"/models/{bare['id']}").text)
    assert "no snapshot replay yet" in p.prop("snapshot replay") and "No snapshot replay yet." in p.card("snapshot").text


def test_job_page_shows_a_snapshot_result(client, conn, make_worker):
    w = make_worker("box1", role="backtest")
    model = insert_model(conn)
    job = lease_job(conn, w, "backtest", {"model_id": str(model["id"]), "price_source": "snapshots", "price_platform": "polymarket_us",
                                           "decision_minutes_before_kickoff": 60, "participation": 0.5, "allow_sim_prices": False})
    conn.execute("UPDATE jobs SET status = 'succeeded', result = %s WHERE id = %s", (Jsonb(snapshot_metrics(clv=None)), job["id"]))
    text = page(client.get(f"/jobs/{job['id']}").text).text
    assert "Replayed on recorded polymarket_us prices: 80 games scored, 40 bets" in text
    assert "0.018 per contract" in text, "no avg_clv reported: the middle of the range"


def test_short_params_label_for_epa_blend() -> None:
    from host.leaderboard import short_params

    assert short_params("epa_blend", {"window": 8, "shrink": 3.0, "l2": 1.0, "min_edge": 0.03}) == "window 8 · shrink 3.0 · L2 1.00"
    assert short_params("epa_blend", {}) == "window ? · shrink ? · L2 ?"
    assert short_params("elo_blend", PARAMS) == "K 24 · HFA 55 · MOV on", "elo_blend keeps its label"
