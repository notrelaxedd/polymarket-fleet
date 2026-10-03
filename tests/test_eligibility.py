"""Lineage eligibility: threshold boundaries, demotion, lineage-wide status, retired."""
from __future__ import annotations

from psycopg.types.json import Jsonb

from host import eligibility
from tests.conftest import backtest_metrics, insert_model, model_row

LIMITS = {"min_bets": 200, "min_roi": 0.02, "max_drawdown": 0.30}


def test_threshold_boundaries():
    assert eligibility.meets_thresholds(backtest_metrics(n_bets=200, roi=0.02, max_drawdown=0.30), LIMITS), "inclusive"
    assert not eligibility.meets_thresholds(backtest_metrics(n_bets=199, roi=0.02, max_drawdown=0.30), LIMITS)
    assert not eligibility.meets_thresholds(backtest_metrics(n_bets=200, roi=0.0199, max_drawdown=0.30), LIMITS)
    assert not eligibility.meets_thresholds(backtest_metrics(n_bets=200, roi=0.02, max_drawdown=0.3001), LIMITS)
    assert not eligibility.meets_thresholds(None, LIMITS)
    assert not eligibility.meets_thresholds({}, LIMITS)
    assert not eligibility.meets_thresholds({"n_bets": "many", "roi": 0.5, "max_drawdown": 0.1}, LIMITS)
    assert not eligibility.meets_thresholds({"n_bets": True, "roi": 0.5, "max_drawdown": 0.1}, LIMITS)


def test_status_for_transitions():
    good = backtest_metrics()
    bad = backtest_metrics(n_bets=10)
    assert eligibility.status_for("candidate", good, LIMITS) == "paper_ok"
    assert eligibility.status_for("candidate", bad, LIMITS) == "candidate"
    assert eligibility.status_for("paper_ok", bad, LIMITS) == "candidate", "demoted when the metrics drop"
    assert eligibility.status_for("live_eligible", good, LIMITS) == "live_eligible", "step 4 status is kept"
    assert eligibility.status_for("live_eligible", bad, LIMITS) == "candidate"
    assert eligibility.status_for("retired", good, LIMITS) == "retired"


def test_thresholds_come_from_settings(conn):
    assert eligibility.thresholds(conn) == {"min_bets": 200, "min_roi": 0.02, "max_drawdown": 0.3}
    conn.execute("UPDATE settings SET value = %s WHERE key = 'thresholds_backtest'", (Jsonb({"min_bets": 5, "min_roi": 0.0, "max_drawdown": 0.5}),))
    assert eligibility.thresholds(conn) == {"min_bets": 5, "min_roi": 0.0, "max_drawdown": 0.5}
    conn.execute("DELETE FROM settings WHERE key = 'thresholds_backtest'")
    assert eligibility.thresholds(conn) == eligibility.DEFAULT_THRESHOLDS


def test_recompute_lineage_writes_every_row_and_demotes(conn):
    root = insert_model(conn, metrics=backtest_metrics(n_bets=250, roi=0.05, max_drawdown=0.1))
    child = insert_model(conn, parent=root, trained_through=[2024, 10])
    grandchild = insert_model(conn, parent=child, trained_through=[2024, 12])
    assert eligibility.recompute_lineage(conn, root["id"]) == "paper_ok"
    assert {model_row(conn, m["id"])["status"] for m in (root, child, grandchild)} == {"paper_ok"}
    # Metrics drop under the threshold: the whole lineage returns to candidate.
    conn.execute("UPDATE models SET backtest_metrics = %s WHERE id = %s", (Jsonb(backtest_metrics(n_bets=250, roi=0.01)), root["id"]))
    assert eligibility.recompute_lineage(conn, root["id"]) == "candidate"
    assert {model_row(conn, m["id"])["status"] for m in (root, child, grandchild)} == {"candidate"}
    # Only the root's metrics count.
    conn.execute("UPDATE models SET backtest_metrics = %s WHERE id = %s", (Jsonb(backtest_metrics(n_bets=900, roi=0.2)), child["id"]))
    assert eligibility.recompute_lineage(conn, root["id"]) == "candidate"
    # Thresholds relaxed: promoted again; recompute_all covers every lineage.
    conn.execute("UPDATE settings SET value = %s WHERE key = 'thresholds_backtest'", (Jsonb({"min_bets": 100, "min_roi": 0.0, "max_drawdown": 0.9}),))
    other = insert_model(conn, params={"k": 30.0}, metrics=backtest_metrics(n_bets=150, roi=0.001))
    assert eligibility.recompute_all(conn) == 2
    assert model_row(conn, grandchild["id"])["status"] == "paper_ok" and model_row(conn, other["id"])["status"] == "paper_ok"
    assert eligibility.recompute_lineage(conn, grandchild["id"]) is None, "not a lineage id"


def test_retired_lineage_stays_retired(conn):
    root = insert_model(conn, metrics=backtest_metrics(), status="retired")
    child = insert_model(conn, parent=root, trained_through=[2024, 10])
    assert eligibility.recompute_lineage(conn, root["id"]) == "retired"
    assert model_row(conn, child["id"])["status"] == "retired"
    # A child marked retired by the owner keeps the root retired too (the status is lineage-wide).
    conn.execute("UPDATE models SET status = 'candidate' WHERE id = %s", (root["id"],))
    assert eligibility.recompute_lineage(conn, root["id"]) == "retired"
    assert model_row(conn, root["id"])["status"] == "retired"


def test_recompute_touches_updated_at_only_on_change(conn):
    root = insert_model(conn, metrics=backtest_metrics())
    conn.execute("UPDATE models SET updated_at = now() - interval '1 hour'")
    assert eligibility.recompute_lineage(conn, root["id"]) == "paper_ok"
    first = model_row(conn, root["id"])["updated_at"]
    assert eligibility.recompute_lineage(conn, root["id"]) == "paper_ok"
    assert model_row(conn, root["id"])["updated_at"] == first
