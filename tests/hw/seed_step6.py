"""Step 6 rows for the screenshot database: validation-era metrics and stress tables
on the search's lineages (two ranked, one flagged overfit, one left unvalidated), a
finished validate job with its result, and the paper CLV interval of the lineage
that paper trades. Used by tests/hw/screenshots.py right after the step 3 rows.

The numbers are constructed (the step 3 seed runs a real search; the validation
numbers here are hand-picked to look like a real validation of those models), so
the captures show every element of the Robustness section.
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from host import eligibility
from tests.conftest import insert_job, stress_metrics, validation_metrics

VALIDATION_SEASONS = [2022, 2025]


def _per_season(base_roi: float) -> list[dict[str, Any]]:
    rows = []
    for i, season in enumerate(range(VALIDATION_SEASONS[0], VALIDATION_SEASONS[1] + 1)):
        roi = round(base_roi + (0.012 if i % 2 == 0 else -0.009), 4)
        bets = 28 + 3 * i
        rows.append({"season": season, "n_games": 272 + i, "n_bets": bets, "roi": roi, "pnl_cents": int(round(bets * 1200 * roi)),
                     "log_loss": round(0.654 + 0.002 * i, 4), "market_log_loss": round(0.658 + 0.001 * i, 4), "max_drawdown": round(0.05 + 0.01 * i, 3)})
    return rows


def _validation(roi: float, n_bets: int, ci: tuple[float, float], market_p: float, gain: float, flags: list[str]) -> dict[str, Any]:
    return validation_metrics(n_bets=n_bets, roi=roi, ci_roi=ci, market_p=market_p, mean_ll_gain=gain, flags=flags,
                              seasons=list(range(VALIDATION_SEASONS[0], VALIDATION_SEASONS[1] + 1)), per_season=_per_season(roi))


PROFILES = [
    # (validation metrics, stress metrics): ranked #1, ranked #2, flagged overfit; the fourth root stays unvalidated.
    (_validation(0.041, 126, (0.004, 0.093), 0.012, 0.0021, []), stress_metrics(seed=1, base_bets=126)),
    (_validation(0.028, 118, (-0.024, 0.079), 0.044, 0.0014, []), stress_metrics(seed=1, base_bets=118, flags=["regime_dependent"])),
    (_validation(-0.006, 104, (-0.058, 0.047), 0.37, -0.0003, ["overfit"]), stress_metrics(seed=1, base_bets=104, flags=["fragile"])),
]


def seed_validation(url: str, worker_id: str) -> dict[str, str]:
    """Validate the first three search lineages (the fourth stays "not validated"),
    store a finished validate job for the first one, recompute eligibility, and
    return the ids the captures need."""
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        roots = conn.execute("SELECT * FROM models WHERE id = lineage_id ORDER BY created_at, id").fetchall()
        for root, (validation, stress) in zip(roots, PROFILES):
            conn.execute(
                "UPDATE models SET validation_metrics = %s, stress_metrics = %s, updated_at = now() WHERE lineage_id = %s",
                (Jsonb(validation), Jsonb(stress), root["lineage_id"]),
            )
            eligibility.recompute_lineage(conn, root["lineage_id"])
        first = roots[0]
        job = insert_job(
            conn, "validate", status="succeeded", progress=1.0, target_worker_id=worker_id,
            params=Jsonb({"model_id": str(first["id"]), "seed": 1, "validation_seasons": VALIDATION_SEASONS, "workers": "auto",
                          "backtest_seasons": [2010, 2021], "fee_model": {"taker_rate": 0.05, "half_spread": 0.01},
                          "default_bankroll_cents": 10000, "max_bet_cents": 2500, "trade_max_games": 6}),
            checkpoint=Jsonb({"stage": "regimes"}),
            result=Jsonb({"validation_metrics": PROFILES[0][0], "stress_metrics": PROFILES[0][1]}),
        )
        conn.execute(
            "UPDATE jobs SET created_at = now() - interval '14 minutes', started_at = now() - interval '13 minutes',"
            " finished_at = now() - interval '11 minutes' WHERE id = %s", (job["id"],),
        )
        for event, ago, detail in (("created", 840, {"target": worker_id}), ("claimed", 780, None),
                                   ("model_validation", 662, {"model_id": str(first["id"]), "status": "paper_ok"}), ("succeeded", 660, None)):
            conn.execute(
                "INSERT INTO job_events (job_id, ts, worker_id, event, detail) VALUES (%s, now() - make_interval(secs => %s), %s, %s, %s)",
                (job["id"], ago, None if event == "created" else worker_id, event, Jsonb(detail) if detail is not None else None),
            )
        return {"validate_job": str(job["id"]), "overfit_model": str(roots[2]["id"]) if len(roots) > 2 else str(first["id"])}


def seed_paper_ci(url: str) -> None:
    """Cache the paper CLV interval of every lineage with settled paper bets (what
    the settlement does), so the leaderboard shows the 90% range."""
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        for row in conn.execute("SELECT DISTINCT lineage_id FROM bets WHERE mode = 'paper'").fetchall():
            eligibility.paper_ci(conn, row["lineage_id"])
