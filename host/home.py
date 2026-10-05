"""The Home page (docs/UI.md "Home"): four headline stats, the "Needs attention" list
and the last settled bets.

Read-only. The attention list is built from the same signals as the top bar banners
(exchange down, assignments unattended, the kill switch with its auto-kill reason)
plus: an enabled worker offline for more than five minutes, an active assignment
whose model is no longer eligible for its mode, a validate job that failed (the
model's latest validate job), and exchange credentials that were never checked or
failed their last check. Each item is a dict the template renders as one row.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from host import pnl, views
from host.leaderboard import leaderboard
from host.settings import get_settings

OFFLINE_AFTER_S = 300
RECENT_BETS = 5
MAX_FAILED_VALIDATIONS = 5


def _age_s(now: datetime, then: datetime | None) -> int | None:
    return None if then is None else max(0, int((now - then).total_seconds()))


def _item(key: str, state: str, word: str, title: str, meta: str, href: str) -> dict[str, str]:
    """One attention row: state ok|warn|bad|muted, the chip word, a title, a meta line and where to fix it."""
    return {"key": key, "state": state, "word": word, "title": title, "meta": meta, "href": href}


def best_model(board: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    """The top ranked lineage with its headline number chosen by its rank basis:
    CLV on paper or snapshot replay, the held-out ROI otherwise."""
    if not board["ranked"]:
        return None
    top = board["ranked"][0]
    if top["rank_mode"] == "paper":
        metric, value = "CLV", top["paper"].get("avg_clv")
    elif top["rank_mode"] == "snapshot":
        metric, value = "CLV", (top.get("snapshot") or {}).get("avg_clv")
    else:
        metric, value = "ROI", (top.get("validation") or {}).get("roi")
    return {"id": top["id"], "name": f"{top['family']} {top['short_params']}", "metric": metric, "value": value,
            "basis": top["rank_mode"]}


def worker_items(workers: list[dict[str, Any]], now: datetime) -> list[dict[str, str]]:
    """Enabled workers whose last heartbeat is more than OFFLINE_AFTER_S old."""
    out = []
    for w in workers:
        age = _age_s(now, w["last_heartbeat_at"])
        if w["enabled"] and (age is None or age > OFFLINE_AFTER_S):
            seen = "never seen" if age is None else f"last seen {age // 60} min ago"
            out.append(_item(f"worker-{w['id']}", "warn", "offline", f"{w['name']} is offline", seen, "/fleet"))
    return out


def assignment_items(conn: psycopg.Connection) -> list[dict[str, str]]:
    """Active assignments whose lineage is retired, or live ones whose lineage is not live_eligible."""
    rows = conn.execute(
        """
        SELECT a.id, a.mode, r.status, g.away_team || ' @ ' || g.home_team AS game
          FROM assignments a
          JOIN models r ON r.id = a.lineage_id
          LEFT JOIN games g ON g.game_id = a.game_id
         WHERE a.status = 'active' AND (r.status = 'retired' OR (a.mode = 'live' AND r.status <> 'live_eligible'))
         ORDER BY a.created_at
        """
    ).fetchall()
    return [
        _item(f"assignment-{r['id']}", "bad", "no model", f"{r['game'] or 'A game'}: no eligible model",
              f"{r['mode']} assignment, model {r['status'].replace('_', ' ')}", "/trading#assignments")
        for r in rows
    ]


def validation_items(conn: psycopg.Connection) -> list[dict[str, str]]:
    """Models whose latest validate job failed (a later success clears the item)."""
    rows = conn.execute(
        """
        SELECT * FROM (
          SELECT DISTINCT ON (params ->> 'model_id') id, params ->> 'model_id' AS model_id, status, error, finished_at
            FROM jobs WHERE kind = 'validate' ORDER BY params ->> 'model_id', created_at DESC, id
        ) latest WHERE status = 'failed' ORDER BY finished_at DESC NULLS LAST LIMIT %s
        """,
        (MAX_FAILED_VALIDATIONS,),
    ).fetchall()
    return [
        _item(f"validate-{r['id']}", "bad", "failed", f"Validate failed for model {(r['model_id'] or '?')[:8]}",
              (r["error"] or "no error recorded").splitlines()[0][:120], f"/jobs/{r['id']}")
        for r in rows
    ]


def exchange_items(conn: psycopg.Connection) -> list[dict[str, str]]:
    """Exchange credentials never checked, or failing their last check."""
    row = conn.execute("SELECT credentials_present, auth_ok, auth_checked_at FROM exchange_state WHERE id").fetchone()
    if row is None or row["auth_checked_at"] is None:
        return [_item("exchange-unchecked", "warn", "unchecked", "Exchange credentials not checked",
                      "Run the auth probe on Trading", "/trading#exchange")]
    if not row["auth_ok"]:
        word = "missing" if not row["credentials_present"] else "failed"
        return [_item("exchange-auth", "bad", word, "Exchange credentials failed their last check",
                      "See the exchange box on Trading", "/trading#exchange")]
    return []


def banner_items(conn: psycopg.Connection, settings: dict[str, Any]) -> list[dict[str, str]]:
    """The top bar's signals: the kill switch (with its auto-kill reason), EXCHANGE DOWN, unattended assignments."""
    out = []
    if settings.get("kill_switch") is True:
        auto = views.latest_auto_kill(conn)
        meta = f"pulled automatically: {auto['reason']}" if auto else "Reset in Settings when ready"
        out.append(_item("kill", "bad", "killed", "Trading is killed", meta, "/settings#kill"))
    if views.exchange_down(conn):
        out.append(_item("exchange-down", "bad", "down", "Exchange process down", "No heartbeat from the exchange process",
                         "/trading#exchange"))
    unattended = views.unattended_assignments(conn)
    if unattended:
        plural = "" if unattended == 1 else "s"
        out.append(_item("unattended", "warn", "unattended", f"{unattended} assignment{plural} unattended",
                         "No trade worker has picked them up", "/trading#assignments"))
    return out


def recent_bets(conn: psycopg.Connection, limit: int = RECENT_BETS) -> list[dict[str, Any]]:
    """The last settled bets, newest first, with the game label."""
    return conn.execute(
        """
        SELECT b.id, b.mode, b.result, b.pnl_cents, b.settled_at, b.contract, b.event, b.model_id,
               g.away_team || ' @ ' || g.home_team AS game
          FROM bets b LEFT JOIN games g ON g.game_id = b.game_id
         ORDER BY b.settled_at DESC, b.id DESC LIMIT %s
        """,
        (limit,),
    ).fetchall()


def open_order_count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) AS n FROM orders WHERE status = ANY(%s)", (list(views.ACTIVE_ORDER_STATUSES),)).fetchone()
    return int(row["n"])


def home_context(conn: psycopg.Connection) -> dict[str, Any]:
    """Everything home.html shows."""
    now = conn.execute("SELECT now() AS t").fetchone()["t"]
    settings = get_settings(conn)
    workers = views.fleet_workers(conn)
    mode = "live" if settings.get("live_enabled") is True else "paper"
    totals = pnl.pnl(conn)["by_mode"][mode]
    attention = (
        banner_items(conn, settings) + worker_items(workers, now) + assignment_items(conn)
        + validation_items(conn) + exchange_items(conn)
    )
    return {
        "online": sum(1 for w in workers if w["online"]),
        "worker_total": len(workers),
        "mode": mode,
        "today_cents": totals["today_cents"],
        "all_time_cents": totals["all_time_cents"],
        "open_orders": open_order_count(conn),
        "best": best_model(leaderboard(conn)),
        "attention": attention,
        "recent": recent_bets(conn),
    }
