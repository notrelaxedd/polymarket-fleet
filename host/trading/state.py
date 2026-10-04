"""The GET /api/v1/trade/state body: one entry per trade job a worker holds, with
the game, the model artifact, the bankroll, the markets with their latest snapshot,
the open orders and the positions (docs/PROTOCOL.md "Step 4 additions")."""
from __future__ import annotations

import uuid
from typing import Any

import psycopg

from host.kill import is_killed
from host.settings import get_int_setting, get_settings
from host.trading.orders import ACTIVE_STATUSES
from host.trading.positions import positions, server_now

STATE_SETTINGS = (
    "min_edge", "kelly_fraction", "participation", "trade_pregame_only", "fee_model", "trade_tick_s",
    "max_bet_cents", "trade_max_games",
)
ACTIVE_LIST = "', '".join(ACTIVE_STATUSES)


def _state_markets(conn: psycopg.Connection, game_id: str, floor: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT m.id, m.side, m.tick, m.min_size, m.status, m.title,
               s.id AS snapshot_id, s.ts AS snapshot_at, s.bid, s.ask, s.mid, s.ask_depth, s.liquidity_usd_cents
          FROM markets m
          LEFT JOIN LATERAL (SELECT * FROM price_snapshots p WHERE p.market_id = m.id
                             ORDER BY p.ts DESC, p.id DESC LIMIT 1) s ON true
         WHERE m.game_id = %s AND m.mapping_confirmed
         ORDER BY m.side, m.id
        """,
        (game_id,),
    ).fetchall()
    out = []
    for r in rows:
        liquidity = r["liquidity_usd_cents"]
        out.append(
            {
                "id": r["id"], "side": r["side"], "title": r["title"], "bid": r["bid"], "ask": r["ask"], "mid": r["mid"],
                "tick": r["tick"], "min_size": r["min_size"], "snapshot_id": r["snapshot_id"],
                "snapshot_at": r["snapshot_at"], "liquidity_usd_cents": liquidity, "ask_depth": r["ask_depth"],
                "status": r["status"], "below_floor": liquidity is None or int(liquidity) < floor,
            }
        )
    return out


def _state_entry(conn: psycopg.Connection, job: dict[str, Any], floor: int) -> dict[str, Any] | None:
    params = job["params"] if isinstance(job["params"], dict) else {}
    try:
        aid = uuid.UUID(str(params.get("assignment_id")))
    except (ValueError, TypeError):
        return None
    a = conn.execute("SELECT * FROM assignments WHERE id = %s", (aid,)).fetchone()
    if a is None:
        return None
    game = conn.execute("SELECT * FROM games WHERE game_id = %s", (a["game_id"],)).fetchone()
    model = conn.execute("SELECT id, family, params, artifact FROM models WHERE id = %s", (a["model_id"],)).fetchone()
    bank = conn.execute("SELECT * FROM bankrolls WHERE assignment_id = %s", (a["id"],)).fetchone()
    open_orders = conn.execute(
        f"""
        SELECT id, market_id, price, size, filled_size, status, snapshot_id, created_at FROM orders
         WHERE assignment_id = %s AND status IN ('{ACTIVE_LIST}') ORDER BY created_at
        """,
        (a["id"],),
    ).fetchall()
    game_fields = {k: v for k, v in dict(game or {}).items() if k != "raw"}
    return {
        "id": a["id"], "job_id": job["id"], "lease_token": job["lease_token"], "status": a["status"], "mode": a["mode"],
        "max_bet_cents": a["max_bet_cents"], "game": game_fields, "model": dict(model) if model else None,
        "bankroll": {k: (bank or {}).get(k)
                     for k in ("available_cents", "reserved_cents", "open_cost_cents", "realized_pnl_cents")},
        "markets": _state_markets(conn, a["game_id"], floor),
        "open_orders": [dict(o) for o in open_orders],
        "positions": positions(conn, a["id"]),
    }


def trade_state(conn: psycopg.Connection, worker_id: str) -> dict[str, Any]:
    """The GET /api/v1/trade/state body: one entry per trade job the worker holds."""
    settings = get_settings(conn)
    floor = get_int_setting(conn, "liquidity_floor_cents", 0)
    jobs = conn.execute(
        """
        SELECT * FROM jobs WHERE lease_worker_id = %s AND kind = 'trade'
           AND status IN ('leased', 'cancel_requested') ORDER BY created_at
        """,
        (worker_id,),
    ).fetchall()
    entries = [e for e in (_state_entry(conn, dict(j), floor) for j in jobs) if e is not None]
    return {
        "kill": is_killed(conn),
        "server_time": server_now(conn),
        "settings": {k: settings.get(k) for k in STATE_SETTINGS},
        "assignments": entries,
    }
