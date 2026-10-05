"""Settlement on a final (docs/TRADING.md, "Settlement, bets, scoring, eligibility").

For every assignment of a final game not yet settled: resolve the markets (winner YES,
loser NO, tie push), cancel its open orders with release, settle the positions through
`ledger.settle`, write one `bets` row per filled order (entry VWAP, fee, cost, my_p,
market_p, edge, stake, closing price, CLV, result, pnl), upsert `model_scores`, mark
the assignment settled, complete the trade job with the score summary, then recompute
the lineage's paper eligibility.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host import eligibility, kill, leases
from host.config import _as_bool
from host.errors import BadRequest, Conflict, Forbidden, NotFound
from host.events import add_audit, add_job_event
from host.exchange import settle_sells, snapshots
from host.exchange.adapters.base import utcnow
from host.settings import get_setting
from host.trading import ledger, orders

log = logging.getLogger(__name__)


def winner_of(game: dict[str, Any]) -> str | None:
    """'home', 'away' or None for a tie."""
    home, away = int(game["home_score"]), int(game["away_score"])
    if home == away:
        return None
    return "home" if home > away else "away"


def settle_game(conn: psycopg.Connection, game_id: str, actor: str = "settle") -> dict[str, Any]:
    """Settle every unsettled assignment of a final game. Conflict when not final.

    Runs under both approval locks, the same ones the kill switch and approvals take
    first, so a kill pressed while a game settles waits for the settlement (or the
    other way round) instead of meeting it half way through the row locks."""
    kill.approval_locks(conn)
    game = conn.execute("SELECT * FROM games WHERE game_id = %s FOR UPDATE", (game_id,)).fetchone()
    if game is None:
        raise NotFound(f"unknown game {game_id}")
    if game["status"] != "final" or game["home_score"] is None or game["away_score"] is None:
        raise Conflict(f"game {game_id} is not final")
    now = utcnow()
    winner = winner_of(game)
    snapshots.freeze_closing_prices(conn, now)
    snapshots.freeze_game_closing_prices(conn, game_id)
    markets = resolve_markets(conn, game_id, winner)
    rows = conn.execute(
        "SELECT * FROM assignments WHERE game_id = %s AND status IN ('active', 'halted') ORDER BY created_at FOR UPDATE",
        (game_id,),
    ).fetchall()
    summary: dict[str, Any] = {"game_id": game_id, "winner": winner, "assignments": 0, "bets": 0, "pnl_cents": 0, "lineages": []}
    lineages: list[Any] = []
    for assignment in rows:
        result = settle_assignment(conn, dict(assignment), dict(game), winner, markets, actor)
        summary["assignments"] += 1
        summary["bets"] += result["n_bets"]
        summary["pnl_cents"] += result["pnl_cents"]
        if assignment["lineage_id"] not in lineages:
            lineages.append(assignment["lineage_id"])
    for lineage_id in lineages:
        status = eligibility.recompute_paper(conn, lineage_id, actor=actor)
        summary["lineages"].append({"lineage_id": str(lineage_id), "status": status})
    return summary


def resolve_markets(conn: psycopg.Connection, game_id: str, winner: str | None) -> dict[Any, dict[str, Any]]:
    rows = conn.execute("SELECT * FROM markets WHERE game_id = %s FOR UPDATE", (game_id,)).fetchall()
    out = {}
    for market in rows:
        resolved_yes = None if winner is None or market["side"] is None else (market["side"] == winner)
        row = conn.execute(
            "UPDATE markets SET status = 'resolved', resolved_yes = %s, updated_at = now() WHERE id = %s RETURNING *",
            (resolved_yes, market["id"]),
        ).fetchone()
        out[row["id"]] = dict(row)
    return out


def settle_assignment(
    conn: psycopg.Connection,
    assignment: dict[str, Any],
    game: dict[str, Any],
    winner: str | None,
    markets: dict[Any, dict[str, Any]],
    actor: str,
) -> dict[str, Any]:
    """One assignment: cancel, settle, bets, score, job, status. The open orders are
    locked (and cancelled) before the bankroll row, the order paper fills and the
    kill use, so the two never wait on each other in opposite directions."""
    aid = assignment["id"]
    open_rows = conn.execute(
        "SELECT id FROM orders WHERE assignment_id = %s AND status IN ('approved', 'submitting', 'open', 'partial')", (aid,)
    ).fetchall()
    for row in open_rows:
        orders.cancel_order(conn, row["id"], actor, "settlement")
    bank = ledger.bankroll_for_assignment(conn, aid, for_update=True)
    bets = settle_sells.assignment_bets(conn, assignment, game, winner, markets)
    total_basis, total_payout = settle_sells.settle_totals(bets)
    if total_basis or total_payout:
        ledger.settle(conn, bank["id"], total_basis, total_payout, aid)
    for bet in bets:
        insert_bet(conn, bet)
    score = upsert_score(conn, assignment, bets)
    conn.execute("UPDATE assignments SET status = 'settled', settled_at = now(), updated_at = now() WHERE id = %s", (aid,))
    complete_trade_job(conn, assignment, score)
    add_audit(conn, "assignment_settled", str(aid), actor, {"status": assignment["status"]}, {"status": "settled", **score})
    return score


def insert_bet(conn: psycopg.Connection, bet: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO bets (order_id, assignment_id, model_id, lineage_id, game_id, worker_id, mode, date, event,
                          platform, contract, side, entry_price, fee_cents, cost_cents, my_p, market_p, edge,
                          stake_cents, closing_price, clv, result, pnl_cents, order_side)
        VALUES (%(order_id)s, %(assignment_id)s, %(model_id)s, %(lineage_id)s, %(game_id)s, %(worker_id)s, %(mode)s,
                %(date)s, %(event)s, %(platform)s, %(contract)s, %(side)s, %(entry_price)s, %(fee_cents)s,
                %(cost_cents)s, %(my_p)s, %(market_p)s, %(edge)s, %(stake_cents)s, %(closing_price)s, %(clv)s,
                %(result)s, %(pnl_cents)s, %(order_side)s)
        ON CONFLICT (order_id) DO NOTHING
        """,
        {"order_side": "buy", **bet},
    )


def upsert_score(conn: psycopg.Connection, assignment: dict[str, Any], bets: list[dict[str, Any]]) -> dict[str, Any]:
    """model_scores for one assignment: pnl over every row (sells included), n_bets and
    the stake-weighted CLV over the buy rows (a sell row has stake 0 and no CLV)."""
    buys = [b for b in bets if b.get("order_side", "buy") == "buy"]
    stake = sum(b["stake_cents"] for b in bets)
    pnl = sum(b["pnl_cents"] for b in bets)
    weighted = [(b["clv"], b["stake_cents"]) for b in buys if b["clv"] is not None and b["stake_cents"] > 0]
    avg_clv = None
    if weighted:
        avg_clv = sum(c * s for c, s in weighted) / sum(s for _, s in weighted)
    conn.execute(
        """
        INSERT INTO model_scores (model_id, game_id, mode, lineage_id, n_bets, stake_cents, pnl_cents, avg_clv, computed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (model_id, game_id, mode) DO UPDATE SET
            n_bets = EXCLUDED.n_bets, stake_cents = EXCLUDED.stake_cents, pnl_cents = EXCLUDED.pnl_cents,
            avg_clv = EXCLUDED.avg_clv, computed_at = now()
        """,
        (assignment["model_id"], assignment["game_id"], assignment["mode"], assignment["lineage_id"], len(buys), stake, pnl, avg_clv),
    )
    return {"n_bets": len(buys), "stake_cents": stake, "pnl_cents": pnl, "avg_clv": avg_clv}


def complete_trade_job(conn: psycopg.Connection, assignment: dict[str, Any], score: dict[str, Any]) -> None:
    """Succeed the trade job with the score summary: through the lease fence when it
    is leased, directly when it sits queued (no lease token exists then)."""
    if assignment.get("job_id") is None:
        return
    job = conn.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (assignment["job_id"],)).fetchone()
    if job is None or job["status"] in ("succeeded", "failed", "cancelled"):
        return
    result = {"assignment_id": str(assignment["id"]), "game_id": assignment["game_id"], **score}
    if job["status"] in leases.ACTIVE and job["lease_token"] is not None:
        leases.complete(conn, job["id"], job["lease_token"], result)
        return
    conn.execute(
        """
        UPDATE jobs SET status = 'succeeded', result = %s, progress = 1, finished_at = now(),
               lease_worker_id = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = now()
         WHERE id = %s
        """,
        (Jsonb(result), job["id"]),
    )
    add_job_event(conn, job["id"], "succeeded", None, {"by": "settlement"})


def settle_due(conn: psycopg.Connection, actor: str = "settle", errors: list[str] | None = None) -> list[dict[str, Any]]:
    """Settle every final game that still has unsettled assignments. A game whose
    settlement fails is logged, rolled back and retried on the next pass; its error
    text is appended to `errors` (when given) so the exchange loop can surface it."""
    rows = conn.execute(
        """
        SELECT DISTINCT g.game_id FROM games g JOIN assignments a ON a.game_id = g.game_id
         WHERE g.status = 'final' AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
           AND a.status IN ('active', 'halted')
         ORDER BY g.game_id
        """
    ).fetchall()
    out = []
    for row in rows:
        try:
            out.append(settle_game(conn, row["game_id"], actor))
            if not conn.autocommit:
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - one game must not block the others
            log.exception("settlement of %s failed", row["game_id"])
            if errors is not None:
                errors.append(f"settlement of {row['game_id']} failed: {exc}")
            if not conn.autocommit:
                conn.rollback()
    return out


def simulate_final(conn: psycopg.Connection, game_id: str, home_score: int, away_score: int, actor: str | None) -> dict[str, Any]:
    """Set a final for testing (sim source or FLEET_DEV only), then settle."""
    source = get_setting(conn, "market_source", "sim")
    if source != "sim" and not _as_bool(os.environ.get("FLEET_DEV", "")):
        raise Forbidden("simulate-final is only allowed with market_source sim (or FLEET_DEV)")
    if not isinstance(home_score, int) or not isinstance(away_score, int) or home_score < 0 or away_score < 0:
        raise BadRequest("scores must be non-negative integers")
    game = conn.execute("SELECT * FROM games WHERE game_id = %s FOR UPDATE", (game_id,)).fetchone()
    if game is None:
        raise NotFound(f"unknown game {game_id}")
    raw = dict(game["raw"] or {})
    raw["score_source"] = "sim"
    conn.execute(
        "UPDATE games SET home_score = %s, away_score = %s, status = 'final', raw = %s, updated_at = clock_timestamp() WHERE game_id = %s",
        (home_score, away_score, Jsonb(raw), game_id),
    )
    add_audit(conn, "simulate_final", game_id, actor, {"status": game["status"]}, {"home_score": home_score, "away_score": away_score})
    return settle_game(conn, game_id, actor or "simulate-final")

