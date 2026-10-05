"""Step 6 Part A paper gate phase of the end-to-end test (tests/test_e2e.py): the
paper CLV interval rule (`thresholds_paper.clv_ci_excludes_zero`, docs/ROBUSTNESS.md
A4) on the live host. Two validated search lineages that pass the backtest gate get
40 settled paper bets each (as the settlement writes them: a resolved market, a
settled assignment, a filled order, a bets row and a model_scores row per game), one
with CLVs that all sit above zero, one whose CLVs straddle zero with a positive mean.
A thresholds_paper change through POST /api/settings recomputes the paper gate: the
first lineage reaches live_eligible, the second stays paper_ok until the interval rule
is switched off and drops back when it is switched on again; a min_bets above the
record demotes both.

It runs last, after the crash phase, because these rows add paper P&L that the
trading and live phases total exactly.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

from host.eligibility import DEFAULT_PAPER_THRESHOLDS, DEFAULT_THRESHOLDS
from tests.conftest import stress_metrics, validation_metrics
from tests.e2e_validation import model_row, set_lineage_metrics

N_BETS = 40
STAKE_CENTS = 1000
EXCLUDES_ZERO = [round(0.01 + 0.004 * ((7 * i) % 10), 4) for i in range(N_BETS)]  # 0.010 to 0.046
STRADDLES = [0.05 if i % 2 == 0 else -0.04 for i in range(N_BETS)]  # mean +0.005, interval across zero


def _settled_paper_bets(conn: psycopg.Connection, model_id: str, clvs: list[float], games: list[str],
                        markets: dict[str, Any]) -> None:
    """One settled winning paper bet per game with the given CLV, settled 30+ days ago."""
    for index, (game_id, clv) in enumerate(zip(games, clvs)):
        assignment = conn.execute(
            """
            INSERT INTO assignments (game_id, model_id, lineage_id, mode, status, created_by, created_at, settled_at)
            VALUES (%s, %s, %s, 'paper', 'settled', 'e2e', now() - make_interval(days => %s), now() - make_interval(days => %s))
            RETURNING id
            """,
            (game_id, model_id, model_id, 31 + index, 30 + index),
        ).fetchone()["id"]
        order = conn.execute(
            """
            INSERT INTO orders (client_request_id, assignment_id, market_id, mode, price, size, cost_cents, status,
                                filled_size, avg_fill_price)
            VALUES (%s, %s, %s, 'paper', 0.5, 20, %s, 'filled', 20, 0.5) RETURNING id
            """,
            (uuid.uuid4().hex, assignment, markets[game_id], STAKE_CENTS),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO bets (order_id, assignment_id, model_id, lineage_id, game_id, mode, date, event, platform, contract,
                              side, entry_price, cost_cents, stake_cents, closing_price, clv, result, pnl_cents, settled_at)
            VALUES (%s, %s, %s, %s, %s, 'paper', current_date - %s, %s, 'sim', 'home', 'home', 0.5, %s, %s, %s, %s,
                    'win', %s, now() - make_interval(days => %s))
            """,
            (order, assignment, model_id, model_id, game_id, 30 + index, game_id, STAKE_CENTS, STAKE_CENTS,
             0.5 + clv, clv, STAKE_CENTS, 30 + index),
        )
        conn.execute(
            """
            INSERT INTO model_scores (model_id, game_id, mode, lineage_id, n_bets, stake_cents, pnl_cents, avg_clv)
            VALUES (%s, %s, 'paper', %s, 1, %s, %s, %s)
            """,
            (model_id, game_id, model_id, STAKE_CENTS, STAKE_CENTS, clv),
        )


def seed_paper_records(host: Any, sure: str, mixed: str) -> None:
    """40 settled paper bets on 2018 fixture games for each lineage (resolved markets)."""
    with psycopg.connect(host.database_url, autocommit=True, row_factory=dict_row) as conn:
        games = [r["game_id"] for r in conn.execute(
            "SELECT game_id FROM games WHERE season = 2018 AND game_type = 'REG' ORDER BY kickoff_at, game_id LIMIT %s",
            (N_BETS,),
        ).fetchall()]
        assert len(games) == N_BETS, games
        markets = {}
        for game_id in games:
            markets[game_id] = conn.execute(
                """
                INSERT INTO markets (platform, market_ref, title, game_id, side, mapping_confirmed, mapping_confidence,
                                     status, resolved_yes, closing_price)
                VALUES ('sim', %s, %s, %s, 'home', true, 1.0, 'resolved', true, 0.55) RETURNING id
                """,
                (f"e2e-{uuid.uuid4().hex}", f"{game_id} home wins", game_id),
            ).fetchone()["id"]
        _settled_paper_bets(conn, sure, EXCLUDES_ZERO, games, markets)
        _settled_paper_bets(conn, mixed, STRADDLES, games, markets)


def phase_paper_gate(host: Any, lineage_ids: list[str]) -> None:
    """The paper CLV interval gate on two lineages (see the module docstring)."""
    sure, mixed = lineage_ids[:2]
    before = host.get("/api/settings")["thresholds_paper"]
    assert before == DEFAULT_PAPER_THRESHOLDS and before["clv_ci_excludes_zero"] is True

    def status(model_id: str) -> str:
        return host.get(f"/api/models/{model_id}")["status"]

    def entry(model_id: str) -> dict[str, Any]:
        board = host.get("/api/models")
        return next(m for m in board["ranked"] + board["unranked"] if m["id"] == model_id)

    for model_id in (sure, mixed):
        set_lineage_metrics(host, model_id, validation_metrics(), stress_metrics())
    host.post("/api/settings", {"thresholds_backtest": DEFAULT_THRESHOLDS})
    assert status(sure) == status(mixed) == "paper_ok", "both pass the backtest gate"
    seed_paper_records(host, sure, mixed)

    host.post("/api/settings", {"thresholds_paper": DEFAULT_PAPER_THRESHOLDS})
    assert status(sure) == "live_eligible", "a CLV interval above zero over 40 bets passes every paper rule"
    assert status(mixed) == "paper_ok", "an interval that straddles zero fails the gate"
    first, second = entry(sure), entry(mixed)
    assert first["paper"]["games"] == N_BETS and first["paper"]["bets"] == N_BETS and first["paper"]["avg_clv"] > 0
    assert first["paper_ci"]["n_bets"] == N_BETS and first["paper_ci"]["ci"][0] > 0, first["paper_ci"]
    assert second["paper"]["avg_clv"] > 0 and second["paper_ci"]["ci"][0] < 0 < second["paper_ci"]["ci"][1], second["paper_ci"]
    lo, hi = first["paper_ci"]["ci"]
    row = model_row(host, sure).text
    assert f"{lo:.3f} to {hi:.3f}" in row or f"{lo * 100:+.1f}% to {hi * 100:+.1f}%" in row, "the 90% range is printed on the row"

    host.post("/api/settings", {"thresholds_paper": dict(DEFAULT_PAPER_THRESHOLDS, clv_ci_excludes_zero=False)})
    assert status(mixed) == "live_eligible", "only the interval rule held it back"
    host.post("/api/settings", {"thresholds_paper": DEFAULT_PAPER_THRESHOLDS})
    assert status(mixed) == "paper_ok", "switched back on, the interval rule demotes the straddling lineage"
    assert status(sure) == "live_eligible"
    host.post("/api/settings", {"thresholds_paper": dict(DEFAULT_PAPER_THRESHOLDS, min_bets=N_BETS + 1)})
    assert status(sure) == status(mixed) == "paper_ok", "fewer than min_bets bets fail the count and the interval"
    host.post("/api/settings", {"thresholds_paper": DEFAULT_PAPER_THRESHOLDS})
    assert status(sure) == "live_eligible" and status(mixed) == "paper_ok"
    assert host.get("/api/settings")["thresholds_paper"] == DEFAULT_PAPER_THRESHOLDS
