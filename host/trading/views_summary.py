"""Display shaping for the /trading page (docs/UI.md "Trading"): the first-screen stats
and the state colour of every status chip. Read-only; the numbers come from the same
functions the top bar and the trading rules use (host.pnl, host.trading.positions).

Stats: today's P&L per mode (the top bar shows only the current mode, so the other
mode's figure lives here; the live one shows while live is on or real money is still in
play), the open order count, the exposure (open order reservations plus the cost of
open positions, the figure the exposure limit checks) and the exchange in words.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host import pnl, views
from host.settings import get_setting
from host.trading.positions import exposure_cents
from host.web import ago

# Colour means state (docs/UI.md principle 4): green = good or on, amber = attention,
# red = stop or loss, grey = idle or finished.
ASSIGNMENT_TONE = {"active": "ok", "halted": "warn", "settled": "muted", "cancelled": "muted"}
ORDER_TONE = {
    "rejected": "bad", "rejected_by_exchange": "bad", "approved": "muted", "submitting": "muted", "open": "ok",
    "partial": "ok", "filled": "ok", "cancel_requested": "warn", "cancelled": "muted", "expired": "muted",
}
MARKET_TONE = {"open": "ok", "closed": "muted", "resolved": "muted"}
TONES = {"assignment": ASSIGNMENT_TONE, "order": ORDER_TONE, "market": MARKET_TONE}
STALE_AFTER_S = 60


def tone(kind: str, status: Any) -> str:
    """The chip state (ok | warn | bad | muted) of a status of an assignment, order or market."""
    return TONES.get(kind, {}).get(str(status), "muted")


def exchange_words(exchange: dict[str, Any]) -> dict[str, str]:
    """The exchange stat: "ok" or "DOWN", with the source and the heartbeat age."""
    source = exchange.get("market_source") or "no source"
    return {
        "value": "DOWN" if exchange.get("down") else "ok",
        "note": f"{source} · {ago(exchange.get('heartbeat_age_s'))}",
    }


def trading_stats(conn: psycopg.Connection, exchange: dict[str, Any], open_orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything the stats grid shows, in cents and counts (the template formats them)."""
    by_mode = pnl.pnl(conn)["by_mode"]
    live_on = get_setting(conn, "live_enabled") is True
    exposure = {mode: exposure_cents(conn, mode) for mode in ("paper", "live")}
    return {
        "paper_today_cents": int(by_mode["paper"]["today_cents"]),
        "paper_all_cents": int(by_mode["paper"]["all_time_cents"]),
        "live_today_cents": int(by_mode["live"]["today_cents"]),
        "live_all_cents": int(by_mode["live"]["all_time_cents"]),
        "show_live": live_on or views.live_activity(conn),
        "open_orders": len(open_orders),
        "open_live": sum(1 for o in open_orders if o.get("mode") == "live"),
        "exposure_cents": exposure["paper"] + exposure["live"],
        "exposure_paper_cents": exposure["paper"],
        "exposure_live_cents": exposure["live"],
        "exchange": exchange_words(exchange),
    }


def stale_markets(markets: list[dict[str, Any]]) -> int:
    """How many mapped markets have no snapshot in the last minute (or none at all)."""
    return sum(1 for m in markets if m.get("snapshot_age_s") is None or m["snapshot_age_s"] > STALE_AFTER_S)
