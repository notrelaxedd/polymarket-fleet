"""Profit and loss for the top bar, the fleet cards and GET /api/pnl.

Today = `bets.pnl_cents` of bets settled today in the owner's time zone plus the
mark-to-mid change of every open position since the later of the day's start and the
fill (a fill made today counts from its own price; an older one from the mid of the
last snapshot before the day started). All-time = every settled bet plus the whole
unrealized gain of the open positions. Per worker the same sums are restricted to the
orders that worker requested; per mode likewise.

Sells (step 6 Part B) before settlement: a sell fill on an unresolved market counts
`proceeds - fee - mark` of its contracts all-time (its realized `proceeds - fee -
basis` minus the unrealized `mark - basis` the buys still carry for the contracts it
took away), and today either the same when it filled today or `-(mark - start mark)`
when it filled earlier. Summed with the buy fills this is exactly the realized gain of
the sales plus the mark-to-mid of the contracts still held. Once the market settles
the sell's `bets` row (result `sold`) carries the realized part.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

import psycopg

from host.settings import get_setting
from host.trading.orders import fill_cost_cents
from host.web import zone

MODES = ("paper", "live")

OPEN_FILLS_SQL = """
    SELECT f.price, f.size, f.ts, f.fee_cents, f.basis_cents, o.side, o.worker_id, o.mode,
           cur.mid AS mid_now, cur.bid AS bid_now, cur.ask AS ask_now,
           prev.mid AS mid_start, prev.bid AS bid_start, prev.ask AS ask_start,
           m.best_bid, m.best_ask
      FROM fills f
      JOIN orders o ON o.id = f.order_id
      JOIN markets m ON m.id = o.market_id
      LEFT JOIN LATERAL (SELECT mid, bid, ask FROM price_snapshots p WHERE p.market_id = m.id
                         ORDER BY p.ts DESC, p.id DESC LIMIT 1) cur ON true
      LEFT JOIN LATERAL (SELECT mid, bid, ask FROM price_snapshots p WHERE p.market_id = m.id AND p.ts < %(start)s
                         ORDER BY p.ts DESC, p.id DESC LIMIT 1) prev ON true
     WHERE m.status <> 'resolved'
"""

BETS_SQL = """
    SELECT mode, worker_id,
           COALESCE(SUM(pnl_cents) FILTER (WHERE settled_at >= %(start)s AND settled_at < %(end)s), 0) AS today,
           COALESCE(SUM(pnl_cents), 0) AS all_time
      FROM bets GROUP BY mode, worker_id
"""


def owner_day(conn: psycopg.Connection, now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) of the owner's calendar day (settings.tz) containing `now`."""
    tz = zone(get_setting(conn, "tz"))
    if now is None:
        now = conn.execute("SELECT now() AS t").fetchone()["t"]
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(tz)
    start = datetime.combine(local.date(), time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def _mid(mid: Any, bid: Any, ask: Any) -> float | None:
    if mid is not None:
        return float(mid)
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2
    return None


def _cents(price: float, size: int) -> int:
    return int(round(price * size * 100))


class _Sums:
    """Running today / all-time totals, split by worker and by mode."""

    def __init__(self) -> None:
        self.today = 0
        self.all_time = 0
        self.by_worker: dict[str, int] = {}
        self.by_mode: dict[str, dict[str, int]] = {m: {"today_cents": 0, "all_time_cents": 0} for m in MODES}

    def add(self, worker_id: str | None, mode: str, today: int, all_time: int) -> None:
        self.today += today
        self.all_time += all_time
        if worker_id is not None:
            self.by_worker[worker_id] = self.by_worker.get(worker_id, 0) + today
        bucket = self.by_mode.setdefault(mode, {"today_cents": 0, "all_time_cents": 0})
        bucket["today_cents"] += today
        bucket["all_time_cents"] += all_time


def _add_open_positions(conn: psycopg.Connection, sums: _Sums, start: datetime) -> None:
    for row in conn.execute(OPEN_FILLS_SQL, {"start": start}).fetchall():
        size = int(row["size"])
        is_sell = row.get("side") == "sell"
        basis = _cents(float(row["price"]), size)
        if is_sell and row.get("basis_cents") is not None:
            basis = int(row["basis_cents"])
        now_mid = _mid(row["mid_now"], row["bid_now"], row["ask_now"])
        if now_mid is None:
            now_mid = _mid(None, row["best_bid"], row["best_ask"])
        mark = basis if now_mid is None else _cents(now_mid, size)
        filled_at = row["ts"] if row["ts"].tzinfo else row["ts"].replace(tzinfo=timezone.utc)
        start_mid = _mid(row["mid_start"], row["bid_start"], row["ask_start"])
        reference = basis if filled_at >= start or start_mid is None else _cents(start_mid, size)
        if is_sell:
            sold = fill_cost_cents(row["price"], size) - int(row["fee_cents"] or 0) - mark
            sums.add(row["worker_id"], row["mode"], sold if filled_at >= start else reference - mark, sold)
            continue
        sums.add(row["worker_id"], row["mode"], mark - reference, mark - basis)


def pnl(conn: psycopg.Connection, now: datetime | None = None) -> dict[str, Any]:
    """{"today_cents", "all_time_cents", "by_worker": {id: today cents}, "by_mode": {...}}.

    `by_worker` carries every worker row (zero when it never traded).
    """
    start, end = owner_day(conn, now)
    sums = _Sums()
    for row in conn.execute(BETS_SQL, {"start": start, "end": end}).fetchall():
        sums.add(row["worker_id"], row["mode"], int(row["today"]), int(row["all_time"]))
    _add_open_positions(conn, sums, start)
    by_worker = {row["id"]: 0 for row in conn.execute("SELECT id FROM workers ORDER BY id").fetchall()}
    by_worker.update(sums.by_worker)
    return {
        "today_cents": sums.today,
        "all_time_cents": sums.all_time,
        "by_worker": by_worker,
        "by_mode": {mode: dict(sums.by_mode[mode]) for mode in MODES},
    }
