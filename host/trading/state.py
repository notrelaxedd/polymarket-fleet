"""The GET /api/v1/trade/state body: one entry per trade job a worker holds, with
the game, the model artifact, the bankroll, the markets with their latest snapshot,
the open orders and the positions (docs/PROTOCOL.md "Step 4 additions").

Step 6 Part B: markets carry `bid_depth` and open orders their `side` ('buy' | 'sell')
so the worker can propose and cancel sells; positions are host.trading.positions rows
({"market_id", "side", "size", "basis_cents", "avg_cost"}, signed: buys minus sells);
the game carries "signals" and "team_stats" from host.signals.game_signals so the
worker builds features with fleet.sim.data.features_of exactly as a backtest does.

Step 6 Part C: each entry carries "ingame" (`ingame_block`: enabled, the in-game
model, the latest game state, pregame_p_home and the feed-lag summary) and open
orders their `ingame` flag; the settings gain the in-game trade rules
(docs/INGAME.md)."""
from __future__ import annotations

import uuid
from typing import Any

import psycopg

from fleet.sim.odds import devig
from host.exchange.feedlag import lag_status
from host.exchange.gamestate import latest_state
from host.kill import is_killed
from host.settings import get_int_setting, get_settings
from host.signals import game_signals
from host.trading.orders import ACTIVE_STATUSES
from host.trading.positions import positions, server_now

STATE_SETTINGS = (
    "min_edge", "kelly_fraction", "participation", "trade_pregame_only", "fee_model", "trade_tick_s",
    "max_bet_cents", "trade_max_games", "ingame_tick_s", "ingame_max_state_age_s", "ingame_quiet_seconds",
    "ingame_cutoff_seconds", "ingame_dead_zone", "ingame_min_edge", "ingame_max_bet_cents", "ingame_gtd_seconds",
)
ACTIVE_LIST = "', '".join(ACTIVE_STATUSES)


def _state_markets(conn: psycopg.Connection, game_id: str, floor: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT m.id, m.side, m.tick, m.min_size, m.status, m.title,
               s.id AS snapshot_id, s.ts AS snapshot_at, s.bid, s.ask, s.mid, s.ask_depth, s.bid_depth,
               s.liquidity_usd_cents
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
                "bid_depth": r["bid_depth"],
                "status": r["status"], "below_floor": liquidity is None or int(liquidity) < floor,
            }
        )
    return out


def pregame_p_home(conn: psycopg.Connection, game: dict[str, Any] | None) -> float | None:
    """The in-game model's prior: the devigged closing moneyline of the game, else the
    frozen closing mid of the home market (1 - the away market's when only that one is
    frozen), else None."""
    if game is None:
        return None
    p = devig(game.get("home_moneyline"), game.get("away_moneyline"))
    if p is not None:
        return float(p)
    rows = conn.execute(
        """
        SELECT side, closing_price FROM markets
         WHERE game_id = %s AND mapping_confirmed AND closing_price IS NOT NULL ORDER BY side DESC, id
        """,
        (game["game_id"],),
    ).fetchall()
    for r in rows:  # 'home' sorts before 'away' descending
        price = float(r["closing_price"])
        return price if r["side"] == "home" else 1.0 - price
    return None


def ingame_block(conn: psycopg.Connection, a: dict[str, Any], game: dict[str, Any] | None,
                 lag: dict[str, Any], now: Any) -> dict[str, Any]:
    """The per-assignment "ingame" entry: {"enabled", "model", "game_state",
    "pregame_p_home", "lag": {"suspended", "median_lag_s", "n"}}. "enabled" is false
    when the in-game model or its lineage is retired (approval rejects it anyway)."""
    from host.trading.ingame import model_retired

    model = None
    if a.get("ingame_model_id") is not None:
        model = conn.execute(
            "SELECT id, family, params, artifact FROM models WHERE id = %s", (a["ingame_model_id"],)
        ).fetchone()
    return {
        "enabled": bool(a.get("trade_ingame")) and model is not None and not model_retired(conn, model["id"]),
        "model": dict(model) if model is not None else None,
        "game_state": latest_state(conn, a["game_id"], now) if game is not None else None,
        "pregame_p_home": pregame_p_home(conn, game),
        "lag": {k: lag.get(k) for k in ("suspended", "median_lag_s", "n")},
    }


def _state_entry(conn: psycopg.Connection, job: dict[str, Any], floor: int, lag: dict[str, Any],
                 now: Any) -> dict[str, Any] | None:
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
        SELECT id, market_id, side, price, size, filled_size, status, snapshot_id, created_at, ingame FROM orders
         WHERE assignment_id = %s AND status IN ('{ACTIVE_LIST}') ORDER BY created_at
        """,
        (a["id"],),
    ).fetchall()
    game_fields = {k: v for k, v in dict(game or {}).items() if k != "raw"}
    if game is not None:
        game_fields.update(game_signals(conn, [a["game_id"]]).get(a["game_id"], {}))
    return {
        "id": a["id"], "job_id": job["id"], "lease_token": job["lease_token"], "status": a["status"], "mode": a["mode"],
        "max_bet_cents": a["max_bet_cents"], "game": game_fields, "model": dict(model) if model else None,
        "bankroll": {k: (bank or {}).get(k)
                     for k in ("available_cents", "reserved_cents", "open_cost_cents", "realized_pnl_cents")},
        "markets": _state_markets(conn, a["game_id"], floor),
        "open_orders": [dict(o) for o in open_orders],
        "positions": positions(conn, a["id"]),
        "ingame": ingame_block(conn, dict(a), dict(game) if game is not None else None, lag, now),
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
    now = server_now(conn)
    lag = lag_status(conn) if jobs else {}
    entries = [e for e in (_state_entry(conn, dict(j), floor, lag, now) for j in jobs) if e is not None]
    return {
        "kill": is_killed(conn),
        "server_time": now,
        "settings": {k: settings.get(k) for k in STATE_SETTINGS},
        "assignments": entries,
    }
