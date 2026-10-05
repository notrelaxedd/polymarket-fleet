"""Lineage eligibility: the step 6 gate on the validation era (every rule at its
boundary), the fallback to the search era, demotion, lineage-wide status, retired,
and the paper CLV bootstrap gate."""
from __future__ import annotations

import uuid

import pytest
from psycopg.types.json import Jsonb

from host import eligibility, stats
from tests.conftest import (
    backtest_metrics, insert_game, insert_model, insert_paper_bet, insert_validated_model, model_row, set_setting,
    stress_metrics, validation_metrics,
)

LEGACY = {"min_bets": 200, "min_roi": 0.02, "max_drawdown": 0.30}
LIMITS = {**eligibility.DEFAULT_THRESHOLDS}


def test_base_threshold_boundaries():
    assert eligibility.meets_thresholds(backtest_metrics(n_bets=200, roi=0.02, max_drawdown=0.30), LEGACY), "inclusive"
    assert not eligibility.meets_thresholds(backtest_metrics(n_bets=199, roi=0.02, max_drawdown=0.30), LEGACY)
    assert not eligibility.meets_thresholds(backtest_metrics(n_bets=200, roi=0.0199, max_drawdown=0.30), LEGACY)
    assert not eligibility.meets_thresholds(backtest_metrics(n_bets=200, roi=0.02, max_drawdown=0.3001), LEGACY)
    assert not eligibility.meets_thresholds(None, LEGACY)
    assert not eligibility.meets_thresholds({}, LEGACY)
    assert not eligibility.meets_thresholds({"n_bets": "many", "roi": 0.5, "max_drawdown": 0.1}, LEGACY)
    assert not eligibility.meets_thresholds({"n_bets": True, "roi": 0.5, "max_drawdown": 0.1}, LEGACY)


def test_validation_rules_at_their_boundaries():
    good = validation_metrics(n_bets=50, roi=0.02, max_drawdown=0.3, ci_roi=(0.0, 0.05), market_p=0.1)
    assert eligibility.meets_thresholds(good, LIMITS), "CI lower bound exactly 0 and market_p exactly the max pass"
    assert not eligibility.meets_thresholds(validation_metrics(n_bets=49), LIMITS), "min_bets applies to the validation era"
    assert not eligibility.meets_thresholds(validation_metrics(ci_roi=(-0.0001, 0.05)), LIMITS), "CI lower bound below 0"
    assert not eligibility.meets_thresholds(validation_metrics(market_p=0.1001), LIMITS), "market p over the max"
    for flag in ("overfit", "fragile"):
        assert not eligibility.meets_thresholds(validation_metrics(flags=[flag]), LIMITS), flag
        assert not eligibility.meets_thresholds(validation_metrics(), LIMITS, stress_metrics(flags=[flag])), f"stress {flag}"
    assert eligibility.meets_thresholds(validation_metrics(), LIMITS, stress_metrics(flags=["regime_dependent"])), "not forbidden by default"
    assert not eligibility.meets_thresholds(validation_metrics(), {**LIMITS, "forbid_flags": ["regime_dependent"]}, stress_metrics(flags=["regime_dependent"]))
    assert eligibility.meets_thresholds(validation_metrics(flags=["overfit"]), {**LIMITS, "forbid_flags": []})
    # A missing or malformed CI or p never passes while the rule is on.
    no_ci = validation_metrics()
    del no_ci["ci"]
    assert not eligibility.meets_thresholds(no_ci, LIMITS)
    assert not eligibility.meets_thresholds(validation_metrics(ci={"roi": [None, 0.1]}), LIMITS)
    assert not eligibility.meets_thresholds(validation_metrics(ci={"roi": "wide"}), LIMITS)
    no_p = validation_metrics()
    del no_p["market_p"]
    assert not eligibility.meets_thresholds(no_p, LIMITS)
    assert eligibility.meets_thresholds(no_p, {**LIMITS, "max_market_p": None}), "a null rule is off"
    assert eligibility.model_flags(validation_metrics(flags=["overfit"]), stress_metrics(flags=["fragile", "overfit"])) == ["overfit", "fragile"]
    assert eligibility.model_flags(None, None) == [] and eligibility.model_flags({"flags": "x"}, {"flags": [1, "fragile"]}) == ["fragile"]


def test_gate_metrics_follow_require_validation():
    row = {"backtest_metrics": backtest_metrics(n_bets=400, roi=0.05), "validation_metrics": None, "stress_metrics": None}
    assert eligibility.gate_metrics(row, LIMITS) is None, "the validation era is judged"
    assert eligibility.root_status(row, "candidate", LIMITS) == "candidate", "not validated: never paper_ok"
    off = {**LIMITS, "require_validation": False}
    assert eligibility.gate_metrics(row, off) is row["backtest_metrics"]
    assert eligibility.root_status(row, "candidate", off) == "paper_ok", "falls back to the three search-era rules"
    assert eligibility.gate_limits(off) == {"min_bets": 50, "min_roi": 0.02, "max_drawdown": 0.3}
    bad_search = {**row, "backtest_metrics": backtest_metrics(n_bets=400, roi=0.05, max_drawdown=0.5), "validation_metrics": validation_metrics()}
    assert eligibility.root_status(bad_search, "candidate", LIMITS) == "paper_ok", "with validation on, the search era is not judged"
    assert eligibility.root_status(bad_search, "candidate", off) == "candidate"
    flagged = {**bad_search, "stress_metrics": stress_metrics(flags=["fragile"])}
    assert eligibility.root_status(flagged, "paper_ok", LIMITS) == "candidate", "a stress flag demotes"


def test_status_for_transitions():
    good, bad = validation_metrics(), validation_metrics(n_bets=10)
    assert eligibility.status_for("candidate", good, LIMITS) == "paper_ok"
    assert eligibility.status_for("candidate", bad, LIMITS) == "candidate"
    assert eligibility.status_for("paper_ok", bad, LIMITS) == "candidate", "demoted when the metrics drop"
    assert eligibility.status_for("live_eligible", good, LIMITS) == "live_eligible", "the paper status is kept"
    assert eligibility.status_for("live_eligible", bad, LIMITS) == "candidate"
    assert eligibility.status_for("retired", good, LIMITS) == "retired"


def test_thresholds_come_from_settings(conn):
    assert eligibility.thresholds(conn) == {
        "min_bets": 50, "min_roi": 0.02, "max_drawdown": 0.3, "require_validation": True, "min_roi_ci_low": 0.0,
        "max_market_p": 0.1, "forbid_flags": ["overfit", "fragile"],
    }, "the migration seeds the ROBUSTNESS.md defaults"
    set_setting(conn, "thresholds_backtest", {"min_bets": 5, "min_roi": 0.0, "max_drawdown": 0.5})
    assert eligibility.thresholds(conn) == {**eligibility.DEFAULT_THRESHOLDS, "min_bets": 5, "min_roi": 0.0, "max_drawdown": 0.5}, "legacy object: new rules default"
    conn.execute("DELETE FROM settings WHERE key = 'thresholds_backtest'")
    assert eligibility.thresholds(conn) == eligibility.DEFAULT_THRESHOLDS
    assert eligibility.paper_thresholds(conn) == {
        "min_games": 10, "min_bets": 40, "min_days": 21, "min_clv": 0.0, "min_pnl_cents": 1, "clv_ci_excludes_zero": True,
    }


def test_recompute_lineage_writes_every_row_and_demotes(conn):
    root = insert_validated_model(conn)
    child = insert_model(conn, parent=root, trained_through=[2024, 10])
    grandchild = insert_model(conn, parent=child, trained_through=[2024, 12])
    assert eligibility.recompute_lineage(conn, root["id"]) == "paper_ok"
    assert {model_row(conn, m["id"])["status"] for m in (root, child, grandchild)} == {"paper_ok"}
    # Validation ROI drops under the threshold: the whole lineage returns to candidate.
    conn.execute("UPDATE models SET validation_metrics = %s WHERE id = %s", (Jsonb(validation_metrics(roi=0.01)), root["id"]))
    assert eligibility.recompute_lineage(conn, root["id"]) == "candidate"
    assert {model_row(conn, m["id"])["status"] for m in (root, child, grandchild)} == {"candidate"}
    # Only the root's metrics count.
    conn.execute("UPDATE models SET validation_metrics = %s WHERE id = %s", (Jsonb(validation_metrics(roi=0.2)), child["id"]))
    assert eligibility.recompute_lineage(conn, root["id"]) == "candidate"
    # The search era alone never promotes while validation is required.
    conn.execute("UPDATE models SET validation_metrics = NULL, backtest_metrics = %s WHERE id = %s", (Jsonb(backtest_metrics(n_bets=900, roi=0.2)), root["id"]))
    assert eligibility.recompute_lineage(conn, root["id"]) == "candidate"
    # Thresholds relaxed to the search era: promoted again; recompute_all covers every lineage.
    set_setting(conn, "thresholds_backtest", {"min_bets": 100, "min_roi": 0.0, "max_drawdown": 0.9, "require_validation": False})
    other = insert_model(conn, params={"k": 30.0}, metrics=backtest_metrics(n_bets=150, roi=0.001))
    assert eligibility.recompute_all(conn) == 2
    assert model_row(conn, grandchild["id"])["status"] == "paper_ok" and model_row(conn, other["id"])["status"] == "paper_ok"
    assert eligibility.recompute_lineage(conn, grandchild["id"]) is None, "not a lineage id"


def test_retired_lineage_stays_retired(conn):
    root = insert_validated_model(conn, status="retired")
    child = insert_model(conn, parent=root, trained_through=[2024, 10])
    assert eligibility.recompute_lineage(conn, root["id"]) == "retired"
    assert model_row(conn, child["id"])["status"] == "retired"
    conn.execute("UPDATE models SET status = 'candidate' WHERE id = %s", (root["id"],))
    assert eligibility.recompute_lineage(conn, root["id"]) == "retired"
    assert model_row(conn, root["id"])["status"] == "retired"


def test_recompute_touches_updated_at_only_on_change(conn):
    root = insert_validated_model(conn)
    conn.execute("UPDATE models SET updated_at = now() - interval '1 hour'")
    assert eligibility.recompute_lineage(conn, root["id"]) == "paper_ok"
    first = model_row(conn, root["id"])["updated_at"]
    assert eligibility.recompute_lineage(conn, root["id"]) == "paper_ok"
    assert model_row(conn, root["id"])["updated_at"] == first


# ------------------------------------------------------------ the bootstrap and the paper CI gate


def test_bootstrap_is_deterministic_and_sane():
    values = [0.02, 0.03, -0.01, 0.05, 0.04, 0.01, 0.03, 0.02, 0.06, 0.0]
    first = stats.bootstrap_ci(values, "paper:x")
    assert first == stats.bootstrap_ci(values, "paper:x") and first != stats.bootstrap_ci(values, "paper:y")
    assert first is not None and first[0] < sum(values) / len(values) < first[1]
    assert stats.bootstrap_ci([], "paper:x") is None
    assert stats.bootstrap_ci([0.05] * 20, "paper:x") == pytest.approx([0.05, 0.05]), "no spread, no width"
    assert stats.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5 and stats.percentile([7.0], 5) == 7.0
    assert stats.weighted_mean([1.0, 3.0], [3.0, 1.0]) == 1.5 and stats.weighted_mean([1.0, 3.0]) == 2.0
    assert stats.shrunk_roi({"roi": 0.05, "n_bets": 400}) == 0.05 * 400 / 500 and stats.shrunk_roi({"shrunk_roi": 0.01, "roi": 0.5, "n_bets": 1}) == 0.01
    assert stats.shrunk_roi(None) == 0.0 and stats.shrunk_roi({"roi": "x"}) == 0.0
    weighted = stats.bootstrap_ci([0.1] * 5 + [-0.1] * 5, "paper:z", weights=[1000.0] * 5 + [1.0] * 5)
    plain = stats.bootstrap_ci([0.1] * 5 + [-0.1] * 5, "paper:z")
    assert weighted is not None and plain is not None and weighted[0] > 0.0 > plain[0], "heavy positive bets dominate the resample"


def _paper_lineage(conn, clvs, status="paper_ok"):
    tag = uuid.uuid4().hex[:6]
    model = insert_validated_model(conn, status=status, params={"k": 20.0 + len(clvs) % 11, "seed": tag})
    for i, clv in enumerate(clvs):
        game = f"2026_0{(i % 9) + 1}_A_B{i}{tag}"
        insert_game(conn, game, kickoff_in_s=-(i + 1) * 3600)
        insert_paper_bet(conn, model, game, clv, pnl_cents=100)
        conn.execute(
            "INSERT INTO model_scores (model_id, game_id, mode, lineage_id, n_bets, stake_cents, pnl_cents, avg_clv) VALUES (%s, %s, 'paper', %s, 1, 1000, 100, %s)",
            (model["id"], game, model["lineage_id"], clv),
        )
    return model


def test_paper_ci_gate_on_constructed_bets(conn):
    set_setting(conn, "thresholds_paper", {"min_games": 2, "min_bets": 4, "min_days": 0, "min_clv": 0.0, "min_pnl_cents": 1, "clv_ci_excludes_zero": True})
    positive = _paper_lineage(conn, [0.03, 0.04, 0.02, 0.05, 0.03, 0.04])
    assert eligibility.recompute_paper(conn, positive["lineage_id"]) == "live_eligible"
    ci = conn.execute("SELECT * FROM lineage_paper_ci WHERE lineage_id = %s", (positive["lineage_id"],)).fetchone()
    assert ci["n_bets"] == 6 and ci["clv_low"] > 0 and ci["clv_low"] <= ci["avg_clv"] <= ci["clv_high"]
    assert ci["clv_low"] == pytest.approx(stats.bootstrap_ci([0.03, 0.04, 0.02, 0.05, 0.03, 0.04], f"paper:{positive['lineage_id']}", [1000.0] * 6)[0], abs=1e-6)
    mixed = _paper_lineage(conn, [0.05, -0.04, 0.03, -0.03, 0.04, -0.05])
    assert eligibility.recompute_paper(conn, mixed["lineage_id"]) == "paper_ok", "a CLV interval straddling zero fails the gate"
    assert conn.execute("SELECT clv_low FROM lineage_paper_ci WHERE lineage_id = %s", (mixed["lineage_id"],)).fetchone()["clv_low"] < 0
    few = _paper_lineage(conn, [0.03, 0.04, 0.05])
    set_setting(conn, "thresholds_paper", {"min_games": 2, "min_bets": 3, "min_days": 0, "min_clv": 0.0, "min_pnl_cents": 1, "clv_ci_excludes_zero": True})
    assert eligibility.recompute_paper(conn, few["lineage_id"]) == "live_eligible"
    set_setting(conn, "thresholds_paper", {"min_games": 2, "min_bets": 4, "min_days": 0, "min_clv": 0.0, "min_pnl_cents": 1, "clv_ci_excludes_zero": True})
    assert eligibility.recompute_paper(conn, few["lineage_id"]) == "paper_ok", "the CI needs at least min_bets bets with a CLV"
    # The rule switched off: the mixed lineage passes the base thresholds (its pooled CLV is positive).
    set_setting(conn, "thresholds_paper", {"min_games": 2, "min_bets": 4, "min_days": 0, "min_clv": 0.0, "min_pnl_cents": 1, "clv_ci_excludes_zero": False})
    assert eligibility.recompute_paper(conn, mixed["lineage_id"]) == "live_eligible"
    # Bets without a CLV are not resampled; none at all gives no interval and never passes the rule.
    set_setting(conn, "thresholds_paper", {"min_games": 1, "min_bets": 1, "min_days": 0, "min_clv": -1.0, "min_pnl_cents": -1, "clv_ci_excludes_zero": True})
    blank = _paper_lineage(conn, [None, None])
    assert eligibility.recompute_paper(conn, blank["lineage_id"]) == "paper_ok"
    assert conn.execute("SELECT n_bets, clv_low FROM lineage_paper_ci WHERE lineage_id = %s", (blank["lineage_id"],)).fetchone() == {"n_bets": 0, "clv_low": None}
    assert eligibility.paper_ci(conn, uuid.uuid4()) == {"n_bets": 0, "avg_clv": None, "ci": None}
    assert eligibility.recompute_paper(conn, uuid.uuid4()) is None


def test_a_paper_thresholds_change_through_the_api_recomputes_the_paper_gate(client, conn):
    """POST /api/settings with thresholds_paper reruns the paper gate of every lineage
    with a paper record, as the Settings form does."""
    sure = _paper_lineage(conn, [0.03, 0.04, 0.02, 0.05, 0.03, 0.04])
    mixed = _paper_lineage(conn, [0.05, -0.04, 0.03, -0.03, 0.04, -0.05])
    loose = {"min_games": 2, "min_bets": 4, "min_days": 0, "min_clv": 0.0, "min_pnl_cents": 1, "clv_ci_excludes_zero": True}
    assert client.post("/api/settings", json={"thresholds_paper": loose}).status_code == 200
    assert model_row(conn, sure["id"])["status"] == "live_eligible" and model_row(conn, mixed["id"])["status"] == "paper_ok"
    assert client.post("/api/settings", json={"thresholds_paper": {**loose, "clv_ci_excludes_zero": False}}).status_code == 200
    assert model_row(conn, mixed["id"])["status"] == "live_eligible"
    assert client.post("/api/settings", json={"thresholds_paper": loose}).status_code == 200
    assert model_row(conn, mixed["id"])["status"] == "paper_ok", "the interval rule switched back on demotes"
