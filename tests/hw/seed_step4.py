"""Step 4 rows for the screenshot database: two upcoming games with sim markets and
snapshots, an unmatched market, three assignments through the real create path, a
trade worker holding their jobs, orders in every interesting state (open with a
partial fill, resting, rejected, cancelled), a settled game with its bet and score,
extra paper scores so one lineage ranks on paper (each with the settled bets behind
it, so the paper CLV range seed_paper_ci bootstraps is over the same bets as the CLV
stat), and a live exchange heartbeat. Used by tests/hw/screenshots.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from host.exchange.settle import settle_game
from host.trading import orders
from host.trading.limits import approve_order
from tests.conftest import (
    insert_game, insert_market, insert_snapshot, insert_worker, lease_trade_job, make_assignment, order_body,
    worker_row,
)

TRADER = "box4"


def _order(conn: psycopg.Connection, worker_id: str, assignment: dict[str, Any], job: dict[str, Any], market: dict[str, Any],
           snapshot: dict[str, Any], price: float, size: int, **extra: Any) -> dict[str, Any]:
    """Approve one order request through the real limits and return the order row."""
    body = order_body(assignment, job, market, snapshot, price=price, size=size, **extra)
    decision = approve_order(conn, worker_row(conn, worker_id), body)
    return conn.execute("SELECT * FROM orders WHERE id = %s", (decision["order_id"],)).fetchone()


def _open(conn: psycopg.Connection, order: dict[str, Any]) -> dict[str, Any]:
    return orders.set_status(conn, order["id"], "open", "exchange", expected=("approved",), submitted_at=datetime.now(timezone.utc))


def _age(conn: psycopg.Connection, order_id: Any, seconds: int) -> None:
    conn.execute("UPDATE orders SET created_at = now() - make_interval(secs => %s) WHERE id = %s", (seconds, order_id))


def _past_bets(conn: psycopg.Connection, worker_id: str, assignment: dict[str, Any], model_id: Any, lineage: Any,
               game: dict[str, Any], week: int, bets: int, pnl: int, clv: float, days_ago: int) -> None:
    """`bets` settled paper buys on a past game (a resolved market, a filled order each)
    whose stake-weighted CLV is `clv` (spread evenly around it) and whose P&L sums to
    `pnl`, the same numbers as the game's model_scores row."""
    market = insert_market(conn, game["game_id"], side="home", status="resolved")
    title = f"Raiders beat Chiefs (Week {week})"
    conn.execute("UPDATE markets SET title = %s, resolved_yes = true WHERE id = %s", (title, market["id"]))
    shares = [pnl // bets] * bets
    shares[0] += pnl - sum(shares)
    for k, share in enumerate(shares):
        order = conn.execute(
            """
            INSERT INTO orders (client_request_id, assignment_id, worker_id, market_id, mode, price, size, cost_cents,
                                status, filled_size, avg_fill_price, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'paper', 0.6, 25, 1500, 'filled', 25, 0.6,
                    now() - make_interval(days => %s), now() - make_interval(days => %s)) RETURNING id
            """,
            (uuid.uuid4().hex, assignment["id"], worker_id, market["id"], days_ago, days_ago),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO bets (order_id, assignment_id, model_id, lineage_id, game_id, worker_id, mode, date, event, platform,
                              contract, side, entry_price, cost_cents, stake_cents, clv, result, pnl_cents, settled_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'paper', (now() - make_interval(days => %s))::date, %s, 'sim', %s, 'home', 0.6,
                    1500, 1500, %s, %s, %s, now() - make_interval(days => %s, mins => %s))
            """,
            (order["id"], assignment["id"], model_id, lineage, game["game_id"], worker_id, days_ago,
             f"{game['away_team']} @ {game['home_team']}", title, round(clv + (k - (bets - 1) / 2) * 0.002, 4),
             "win" if share > 0 else "loss" if share < 0 else "push", share, days_ago, k),
        )


def seed_trading(url: str, model_id: str) -> dict[str, str]:
    """Seed everything the /trading captures show; returns the ids the captures need."""
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        second_model_id = conn.execute(
            "SELECT id FROM models WHERE id <> %s ORDER BY (lineage_id = id) DESC, created_at LIMIT 1", (model_id,)
        ).fetchone()["id"]
        trader = insert_worker(conn, TRADER, role="trade")
        conn.execute(
            "UPDATE workers SET cpu_pct = 11.0, ram_used_mb = 1402, ram_total_mb = 7936, hostname = name || '.lan',"
            " python_version = '3.11.2', code_version = 'a1b2c3d4e5f6' WHERE id = %s", (trader.id,),
        )
        # Two upcoming games, both moneylines each, fresh books.
        kc_lv = insert_game(conn, "2026_05_KC_LV", home="LV", away="KC", kickoff_in_s=2 * 86400 + 3600)
        dal_phi = insert_game(conn, "2026_05_DAL_PHI", home="PHI", away="DAL", kickoff_in_s=3 * 86400)
        home = insert_market(conn, kc_lv["game_id"], side="home")
        away = insert_market(conn, kc_lv["game_id"], side="away")
        conn.execute("UPDATE markets SET title = 'Raiders beat Chiefs (Week 5)' WHERE id = %s", (home["id"],))
        conn.execute("UPDATE markets SET title = 'Chiefs beat Raiders (Week 5)' WHERE id = %s", (away["id"],))
        phi = insert_market(conn, dal_phi["game_id"], side="home")
        conn.execute("UPDATE markets SET title = 'Eagles beat Cowboys (Week 5)' WHERE id = %s", (phi["id"],))
        snap_home = insert_snapshot(conn, home["id"], bid=0.56, ask=0.58, liquidity_usd_cents=312_000)
        insert_snapshot(conn, away["id"], bid=0.41, ask=0.43, liquidity_usd_cents=280_000, age_s=4)
        insert_snapshot(conn, phi["id"], bid=0.62, ask=0.64, liquidity_usd_cents=190_000, age_s=25)
        loose = insert_market(conn, kc_lv["game_id"], side="home", confirmed=False)
        conn.execute("UPDATE markets SET title = 'LV Raiders vs. Kansas City: Raiders win?' WHERE id = %s", (loose["id"],))
        insert_snapshot(conn, loose["id"], bid=0.55, ask=0.59, liquidity_usd_cents=40_000, age_s=31)
        # Assignments through the real path (bankroll + trade job), the jobs leased by box4.
        first = make_assignment(conn, kc_lv["game_id"], model_id, bankroll_cents=20_000, actor="owner@example.com")
        second = make_assignment(conn, kc_lv["game_id"], second_model_id, bankroll_cents=10_000, max_bet_cents=1_500, actor="owner@example.com")
        third = make_assignment(conn, dal_phi["game_id"], model_id, bankroll_cents=10_000, actor="owner@example.com")
        job1 = lease_trade_job(conn, trader, first)
        job2 = lease_trade_job(conn, trader, second)
        lease_trade_job(conn, trader, third)
        # Orders: open with a partial fill, a resting bid, a rejected request, a cancelled one.
        filled = _open(conn, _order(conn, trader.id, first, job1, home, snap_home, 0.58, 30,
                                    my_p=0.63, market_p=0.57, edge=0.044, rationale="my 0.63 vs ask 0.58, fee 0.012, edge 0.044"))
        orders.record_fill(conn, filled["id"], 0.58, 18, 10, "paper", "exchange", snapshot_id=snap_home["id"])
        _age(conn, filled["id"], 95)
        resting = _open(conn, _order(conn, trader.id, second, job2, home, snap_home, 0.58, 20,
                                     my_p=0.61, market_p=0.57, edge=0.028, rationale="my 0.61 vs ask 0.58, fee 0.012, edge 0.028"))
        _age(conn, resting["id"], 40)
        rejected = _order(conn, trader.id, second, job2, home, snap_home, 0.58, 120,
                          my_p=0.61, market_p=0.57, edge=0.028, rationale="my 0.61 vs ask 0.58, fee 0.012, edge 0.028")
        _age(conn, rejected["id"], 20)
        cancelled = _open(conn, _order(conn, trader.id, first, job1, away, insert_snapshot(conn, away["id"], bid=0.41, ask=0.43, age_s=2), 0.43, 10,
                                       my_p=0.40, market_p=0.42, edge=-0.03, rationale="my 0.40 vs ask 0.43, fee 0.012, edge -0.030"))
        orders.cancel_order(conn, cancelled["id"], trader.id, "edge below zero")
        _age(conn, cancelled["id"], 300)
        # A settled game from earlier today: its assignment, order and fill, then the real settlement.
        done = insert_game(conn, "2026_04_NYG_BUF", home="BUF", away="NYG", kickoff_in_s=3600, week=4)
        buf = insert_market(conn, done["game_id"], side="home")
        conn.execute("UPDATE markets SET title = 'Bills beat Giants (Week 4)' WHERE id = %s", (buf["id"],))
        # The book is fresh while the order is approved (a stale book, even the latest,
        # is rejected) and aged afterwards to sit before the kickoff.
        snap_buf = insert_snapshot(conn, buf["id"], bid=0.70, ask=0.72, liquidity_usd_cents=400_000)
        settled = make_assignment(conn, done["game_id"], model_id, bankroll_cents=10_000, actor="owner@example.com")
        job4 = lease_trade_job(conn, trader, settled)
        won = _open(conn, _order(conn, trader.id, settled, job4, buf, snap_buf, 0.72, 25,
                                 my_p=0.78, market_p=0.71, edge=0.052, rationale="my 0.78 vs ask 0.72, fee 0.010, edge 0.052"))
        conn.execute("UPDATE price_snapshots SET ts = now() - interval '2 hours' WHERE id = %s", (snap_buf["id"],))
        conn.execute("UPDATE markets SET last_snapshot_at = now() - interval '2 hours' WHERE id = %s", (buf["id"],))
        orders.record_fill(conn, won["id"], 0.72, 25, 25, "paper", "exchange", snapshot_id=snap_buf["id"])
        _age(conn, won["id"], 7000)
        conn.execute("UPDATE fills SET ts = now() - interval '115 minutes' WHERE order_id = %s", (won["id"],))
        conn.execute("UPDATE games SET status = 'final', home_score = 27, away_score = 17, kickoff_at = now() - interval '4 hours'"
                     " WHERE game_id = %s", (done["game_id"],))
        settle_game(conn, done["game_id"], "scores")
        # Four more past paper scores so the first lineage ranks on paper (5 games, 30 bets),
        # each with its settled bets, so the CLV stat and its 90% range describe the same bets.
        lineage = conn.execute("SELECT lineage_id FROM models WHERE id = %s", (model_id,)).fetchone()["lineage_id"]
        for i, (bets, pnl, clv) in enumerate([(8, 1450, 0.021), (7, -620, 0.004), (9, 980, 0.017), (5, 310, 0.012)]):
            past = insert_game(conn, f"2026_0{i + 1}_PAST_{i}", kickoff_in_s=-(i + 2) * 86400, week=i + 1)
            conn.execute("UPDATE games SET status = 'final', home_score = 20, away_score = 17 WHERE game_id = %s", (past["game_id"],))
            _past_bets(conn, trader.id, settled, model_id, lineage, past, i + 1, bets, pnl, clv, i + 2)
            conn.execute(
                "INSERT INTO model_scores (model_id, game_id, mode, lineage_id, n_bets, stake_cents, pnl_cents, avg_clv)"
                " VALUES (%s, %s, 'paper', %s, %s, %s, %s, %s)",
                (model_id, past["game_id"], lineage, bets, bets * 1500, pnl, clv),
            )
        conn.execute("UPDATE exchange_state SET heartbeat_at = now() - interval '2 seconds', market_source = 'sim', last_error = NULL")
        return {"trader": trader.id, "assignment": str(first["id"]), "settled_game": done["game_id"], "unmatched": str(loose["id"])}


def touch_trading(conn: psycopg.Connection) -> None:
    """Keep the trade worker online and the exchange heartbeat fresh while captures run."""
    conn.execute("UPDATE workers SET last_heartbeat_at = now() - interval '2 seconds' WHERE name = %s", (TRADER,))
    conn.execute("UPDATE exchange_state SET heartbeat_at = now() - interval '2 seconds'")
