"""Step 6 Part B rows for the screenshot database (docs/ROBUSTNESS.md Part B,
docs/TRADING.md "Selling"): an epa_blend lineage ranked on snapshot replay CLV (with
validation and stress tables too, so its page shows both sections), snapshot metrics
on the paper-ranked lineage and a 22-bet one on the unvalidated lineage (shown, not
ranked), a finished snapshot backtest job, and on the trading side a partly sold
position (a filled sell through the real approval and fill path), an open sell on the
rest, and a second assignment holding a losing position. Used by
tests/hw/screenshots.py right after the step 4 rows; `check_step6b` asserts the
captures will show all of it.

The metrics are constructed (hand-picked to look like a real replay), the orders and
fills go through host.trading.limits.approve_order and orders.record_fill.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from host import eligibility
from host.settings import get_setting
from host.trading import orders
from host.trading.limits import approve_order
from host.trading.sells import sell_fee_cents
from tests.conftest import (
    backtest_metrics, insert_job, insert_model, insert_snapshot, stress_metrics, validation_metrics, worker_row,
)
from tests.pagecheck import page

EPA_PARAMS = {"window": 8, "shrink": 3.0, "l2": 1.0, "min_edge": 0.03, "kelly_fraction": 0.25}
REPLAY_SEASONS = [2024, 2026]


def snapshot_metrics(n_bets: int, roi: float, clv: float, ci_clv: tuple[float, float], n_unscored: int) -> dict[str, Any]:
    """A snapshot replay result: the backtest shape plus price_source, platform,
    n_unscored_no_prices, the top-level avg_clv and its bootstrap range, per season."""
    seasons = list(range(REPLAY_SEASONS[0], REPLAY_SEASONS[1] + 1))
    metrics = backtest_metrics(n_bets=n_bets, roi=roi, seasons=REPLAY_SEASONS, max_drawdown=0.09, log_loss=0.654, market_log_loss=0.656)
    split = [n_bets // 2, n_bets - n_bets // 2 - n_bets // 6, n_bets // 6]
    per_season = [{"season": s, "n_games": 3 * b + 7, "n_bets": b, "roi": round(roi + d, 4), "pnl_cents": int(round(b * 1200 * (roi + d))),
                   "log_loss": 0.652 + 0.003 * i, "market_log_loss": 0.655 + 0.002 * i, "max_drawdown": 0.06 + 0.01 * i,
                   "n_unscored_no_prices": n_unscored // 3 + (n_unscored % 3 if i == 0 else 0)}
                  for i, (s, b, d) in enumerate(zip(seasons, split, (0.011, -0.008, 0.004)))]
    pnl = sum(r["pnl_cents"] for r in per_season)
    metrics.update({
        "pnl_cents": pnl, "roi": round(pnl / metrics["total_stake_cents"], 4),
        "n_games": sum(r["n_games"] for r in per_season), "price_source": "snapshots", "platform": "polymarket_us",
        "n_unscored_no_prices": n_unscored, "avg_clv": clv, "per_season": per_season,
        "ci": {"roi": [round(roi - 0.046, 4), round(roi + 0.052, 4)], "avg_clv": list(ci_clv), "max_drawdown": [0.05, 0.16],
               "hit_rate": [0.47, 0.58], "avg_edge": [0.027, 0.044]},
    })
    return metrics


EPA_SNAPSHOT = snapshot_metrics(64, 0.036, 0.0184, (0.0061, 0.0312), 19)


def _connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def seed_snapshot(url: str, worker_id: str, paper_model_id: str) -> dict[str, str]:
    """The epa_blend lineage ranked on snapshot CLV, snapshot metrics on two existing
    lineages, and a finished snapshot backtest job; returns the ids the captures need."""
    with _connect(url) as conn:
        epa = insert_model(conn, "epa_blend", EPA_PARAMS, backtest_metrics(n_bets=212, roi=0.024, seasons=[2019, 2021]),
                           summary="epa_blend window 8, shrink 3.0: rolling EPA diffs shrunk to the league mean, plus Elo and market",
                           validation=validation_metrics(n_bets=97, roi=0.019, ci_roi=(-0.031, 0.072), market_p=0.081),
                           stress=stress_metrics(seed=1, base_bets=97))
        conn.execute("UPDATE models SET snapshot_metrics = %s WHERE lineage_id = %s", (Jsonb(EPA_SNAPSHOT), epa["lineage_id"]))
        eligibility.recompute_lineage(conn, epa["lineage_id"])
        paper = conn.execute("SELECT lineage_id FROM models WHERE id = %s", (paper_model_id,)).fetchone()["lineage_id"]
        unvalidated = conn.execute(
            "SELECT lineage_id FROM models WHERE id = lineage_id AND validation_metrics IS NULL AND family = 'elo_blend'"
            " ORDER BY created_at, id LIMIT 1").fetchone()
        for lineage, metrics in ((paper, snapshot_metrics(88, 0.041, 0.0127, (0.0019, 0.0236), 23)),
                                 (unvalidated and unvalidated["lineage_id"], snapshot_metrics(22, -0.012, 0.0043, (-0.0091, 0.0172), 31))):
            if lineage is not None:
                conn.execute("UPDATE models SET snapshot_metrics = %s WHERE lineage_id = %s", (Jsonb(metrics), lineage))
        params = {"model_id": str(epa["id"]), "seasons": REPLAY_SEASONS, "price_source": "snapshots", "price_platform": "polymarket_us",
                  "decision_minutes_before_kickoff": 60, "allow_sim_prices": False, "participation": 0.5,
                  "fee_model": {"taker_rate": 0.05, "half_spread": 0.01}, "default_bankroll_cents": 10000, "max_bet_cents": 2500}
        job = insert_job(conn, "backtest", status="succeeded", progress=1.0, target_worker_id=worker_id, params=Jsonb(params),
                         checkpoint=Jsonb({"next": 3, "per_season": EPA_SNAPSHOT["per_season"]}), result=Jsonb(EPA_SNAPSHOT))
        conn.execute("UPDATE jobs SET created_at = now() - interval '8 minutes', started_at = now() - interval '7 minutes',"
                     " finished_at = now() - interval '5 minutes' WHERE id = %s", (job["id"],))
        for event, ago, detail in (("created", 480, {"target": worker_id}), ("claimed", 420, None),
                                   ("model_snapshot_backtest", 301, {"model_id": str(epa["id"]), "n_bets": 64}), ("succeeded", 300, None)):
            conn.execute(
                "INSERT INTO job_events (job_id, ts, worker_id, event, detail) VALUES (%s, now() - make_interval(secs => %s), %s, %s, %s)",
                (job["id"], ago, None if event == "created" else worker_id, event, Jsonb(detail) if detail is not None else None),
            )
        return {"epa_model": str(epa["id"]), "replay_job": str(job["id"])}


def _request(conn: psycopg.Connection, trader_id: str, assignment_id: str, market_id: Any, snapshot: dict[str, Any],
             price: float, size: int, order_side: str, my_p: float, edge: float, rationale: str) -> dict[str, Any]:
    """Approve one order through the real limits (a sell goes to approve_sell), then open it."""
    job = conn.execute("SELECT j.* FROM jobs j JOIN assignments a ON a.job_id = j.id WHERE a.id = %s", (assignment_id,)).fetchone()
    body = {"client_request_id": uuid.uuid4().hex, "job_id": str(job["id"]),
            "lease_token": str(job["lease_token"]), "assignment_id": str(assignment_id), "market_id": str(market_id),
            "snapshot_id": int(snapshot["id"]), "price": price, "size": size, "my_p": my_p, "market_p": float(snapshot["mid"]),
            "edge": edge, "rationale": rationale, "order_side": order_side}
    decision = approve_order(conn, worker_row(conn, trader_id), body)
    assert decision["status"] == "approved", f"the {order_side} request was rejected: {decision}"
    return orders.set_status(conn, decision["order_id"], "open", "exchange", expected=("approved",), submitted_at=datetime.now(timezone.utc))


def seed_sells(url: str, trader_id: str, assignment_id: str) -> dict[str, str]:
    """On the first assignment's home market (18 bought at 0.58): the bid rises to 0.62,
    8 are sold there (filled), 5 more rest as an open sell; the Eagles assignment buys
    12 at 0.64 and the bid sits at 0.61, so the page shows a loss beside the gain."""
    with _connect(url) as conn:
        fee_model = get_setting(conn, "fee_model", {"taker_rate": 0.05})
        home = conn.execute("SELECT market_id FROM orders WHERE assignment_id = %s AND filled_size > 0 ORDER BY created_at LIMIT 1",
                            (assignment_id,)).fetchone()["market_id"]
        up = insert_snapshot(conn, home, bid=0.62, ask=0.64, liquidity_usd_cents=318_000)
        sold = _request(conn, trader_id, assignment_id, home, up, 0.62, 8, "sell", 0.57, 0.038, "bid 0.62 - fee 0.012 vs my 0.57: sell edge 0.038")
        orders.record_fill(conn, sold["id"], 0.62, 8, sell_fee_cents(0.62, 8, fee_model), "paper", "exchange", snapshot_id=up["id"])
        conn.execute("UPDATE orders SET created_at = now() - interval '70 seconds' WHERE id = %s", (sold["id"],))
        conn.execute("UPDATE fills SET ts = now() - interval '68 seconds' WHERE order_id = %s", (sold["id"],))
        resting = _request(conn, trader_id, assignment_id, home, insert_snapshot(conn, home, bid=0.62, ask=0.64, age_s=1),
                           0.62, 5, "sell", 0.57, 0.038, "bid 0.62 - fee 0.012 vs my 0.57: sell edge 0.038")
        conn.execute("UPDATE orders SET created_at = now() - interval '15 seconds' WHERE id = %s", (resting["id"],))
        eagles = conn.execute(
            "SELECT a.id, m.id AS market_id FROM assignments a JOIN markets m ON m.game_id = a.game_id AND m.side = 'home' AND m.mapping_confirmed"
            " WHERE a.game_id = '2026_05_DAL_PHI' AND a.mode = 'paper' ORDER BY a.created_at LIMIT 1").fetchone()
        snap = insert_snapshot(conn, eagles["market_id"], bid=0.62, ask=0.64, liquidity_usd_cents=190_000)
        bought = _request(conn, trader_id, eagles["id"], eagles["market_id"], snap, 0.64, 12, "buy", 0.69, 0.038, "my 0.69 vs ask 0.64, fee 0.012, edge 0.038")
        orders.record_fill(conn, bought["id"], 0.64, 12, sell_fee_cents(0.64, 12, fee_model), "paper", "exchange", snapshot_id=snap["id"])
        conn.execute("UPDATE orders SET created_at = now() - interval '200 seconds' WHERE id = %s", (bought["id"],))
        conn.execute("UPDATE fills SET ts = now() - interval '198 seconds' WHERE order_id = %s", (bought["id"],))
        insert_snapshot(conn, eagles["market_id"], bid=0.61, ask=0.63, liquidity_usd_cents=185_000, age_s=3)
        return {"sell_order": str(sold["id"]), "open_sell": str(resting["id"])}


def check_step6b(server_url: str, ids: dict[str, str]) -> None:
    """The pages show what the step 6B captures are for (fails loudly when a seed drifts)."""
    with httpx.Client(base_url=server_url, trust_env=False) as client:
        board = client.get("/api/models").json()
        modes = {m["id"]: m["rank_mode"] for m in board["ranked"]}
        assert modes.get(ids["epa_model"]) == "snapshot", f"the epa_blend lineage ranks on snapshot CLV: {modes}"
        models = page(client.get("/models").text)
        epa = models.row("model", ids["epa_model"])
        assert epa.chip("rank-snapshot").text == "snapshot" and "window 8" in epa.text
        assert epa.one(".row-value").text.startswith("CLV "), "ranked on snapshot CLV: the CLV is the headline"
        model = page(client.get(f"/models/{ids['epa_model']}").text)
        assert "ranked on snapshot CLV" in model.text and "snapshot per season" in model.card("snapshot").text
        jobs = page(client.get("/jobs").text)
        assert jobs.form("backtest").input("price_source") and jobs.has('[data-chip="snapshots"]')
        assert "Snapshots replay the prices the host recorded" in jobs.form("backtest").text
        settings = page(client.get("/settings").text)
        for key in ("decision_minutes_before_kickoff", "allow_sim_prices", "signals_refresh_hours", "nflverse_injuries_url", "nflverse_pbp_url"):
            assert settings.field(key), key
        trading = page(client.get("/trading").text)
        held = {n.attr("data-assignment") for n in trading.card("positions").select("[data-assignment]")}
        assert len(held) == 2, "two assignments hold contracts"
        assert trading.has('[data-chip="sell"]') and "sell" in trading.card("open-orders").row("order", ids["open_sell"]).chips()
