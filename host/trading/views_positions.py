"""Positions for the /trading page (docs/TRADING.md "Selling"): per assignment, every
open position with its size, average cost, the current bid and the unrealized P&L
at that bid, in cents.

The rows come from host.trading.positions.positions (the signed sum of fills: buys
add, sells subtract their removed basis). The current bid is the bid of the market's
latest price snapshot, falling back to the best bid mirrored on the market row; a
position without any bid shows no unrealized figure rather than a made-up one.
Unrealized P&L at the bid is `round(bid * size * 100) - fee - basis_cents`, where fee
is the taker fee a sale of the whole position at that bid would pay (the paper
simulator's fee rule on settings fee_model), so the figure is what selling now would
realize.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg

from host.exchange.paper import fee_cents
from host.settings import get_setting
from host.trading import positions as positions_mod
from host.trading.orders import fill_cost_cents

LATEST_BIDS_SQL = """
    SELECT m.id AS market_id, m.title, m.platform, m.best_bid, s.bid AS snapshot_bid, s.ts AS snapshot_ts
      FROM markets m
      LEFT JOIN LATERAL (
        SELECT ps.bid, ps.ts FROM price_snapshots ps
         WHERE ps.market_id = m.id
         ORDER BY ps.ts DESC, ps.id DESC LIMIT 1
      ) s ON true
     WHERE m.id = ANY(%s)
"""


def latest_bids(conn: psycopg.Connection, market_ids: list[Any]) -> dict[Any, dict[str, Any]]:
    """market_id -> {title, platform, bid, bid_ts}: the latest snapshot's bid, else the
    market row's best bid (bid None when neither exists)."""
    if not market_ids:
        return {}
    out: dict[Any, dict[str, Any]] = {}
    for row in conn.execute(LATEST_BIDS_SQL, (list(market_ids),)).fetchall():
        bid = row["snapshot_bid"] if row["snapshot_bid"] is not None else row["best_bid"]
        out[row["market_id"]] = {
            "title": row["title"],
            "platform": row["platform"],
            "bid": None if bid is None else Decimal(str(bid)),
            "bid_ts": row["snapshot_ts"],
        }
    return out


def value_at_bid(position: dict[str, Any], bid: Decimal | None, fee_model: dict[str, Any] | None = None) -> dict[str, Any]:
    """A position row plus `bid`, `value_cents` (gross, at the bid), `sell_fee_cents`
    and `unrealized_cents` (value - fee - basis); all None without a bid."""
    row = dict(position)
    row["bid"] = None if bid is None else float(bid)
    if bid is None:
        row["value_cents"] = None
        row["sell_fee_cents"] = None
        row["unrealized_cents"] = None
    else:
        size = int(row["size"])
        value = fill_cost_cents(bid, size)
        fee = fee_cents(float(bid), size, fee_model)
        row["value_cents"] = value
        row["sell_fee_cents"] = fee
        row["unrealized_cents"] = value - fee - int(row["basis_cents"])
    return row


def assignment_positions(conn: psycopg.Connection, assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per assignment that holds contracts, in the assignments' order:
    {assignment_id, game_id, label, mode, family, model_id, rows, basis_cents,
    unrealized_cents}. `rows` are the positions valued at the current bid, sorted by
    market title; the totals sum the rows (unrealized over rows that have a bid)."""
    held = [(a, positions_mod.positions(conn, a["id"])) for a in assignments]
    market_ids = sorted({p["market_id"] for _, rows in held for p in rows}, key=str)
    bids = latest_bids(conn, market_ids)
    fee_model = get_setting(conn, "fee_model")
    fee_model = fee_model if isinstance(fee_model, dict) else None
    out = []
    for a, rows in held:
        if not rows:
            continue
        valued = []
        for p in rows:
            info = bids.get(p["market_id"], {})
            row = value_at_bid(p, info.get("bid"), fee_model)
            row["market_title"] = info.get("title") or str(p["market_id"])
            row["platform"] = info.get("platform")
            row["bid_ts"] = info.get("bid_ts")
            valued.append(row)
        valued.sort(key=lambda r: (str(r["market_title"]), str(r["market_id"])))
        game = a.get("game") or {}
        model = a.get("model") or {}
        out.append(
            {
                "assignment_id": a["id"],
                "game_id": a.get("game_id"),
                "label": f"{game.get('away_team', '?')} @ {game.get('home_team', '?')}",
                "mode": a.get("mode"),
                "family": model.get("family"),
                "model_id": a.get("model_id"),
                "rows": valued,
                "size": sum(int(r["size"]) for r in valued),
                "basis_cents": sum(int(r["basis_cents"]) for r in valued),
                "unrealized_cents": sum(int(r["unrealized_cents"]) for r in valued if r["unrealized_cents"] is not None),
                "unpriced": sum(1 for r in valued if r["unrealized_cents"] is None),
            }
        )
    return out
