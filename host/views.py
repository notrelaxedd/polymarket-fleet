"""Read-only queries behind the owner API (/api/fleet, job listings)."""
from __future__ import annotations

from typing import Any

import psycopg

from host.leases import get_job
from host.scheduling import online_after
from host.settings import get_setting, public_settings

JOB_STATUSES = ("queued", "leased", "cancel_requested", "succeeded", "failed", "cancelled")


def _current_jobs(conn: psycopg.Connection) -> dict[str, list[dict[str, Any]]]:
    """Active jobs grouped by lease worker; a trade job carries its game ("KC @ LV")."""
    rows = conn.execute(
        """
        SELECT j.id, j.kind, j.status, j.progress, j.lease_worker_id,
               g.away_team || ' @ ' || g.home_team AS game
          FROM jobs j
          LEFT JOIN assignments a ON j.kind = 'trade' AND a.id::text = j.params ->> 'assignment_id'
          LEFT JOIN games g ON g.game_id = a.game_id
         WHERE j.status IN ('leased', 'cancel_requested') ORDER BY j.started_at
        """
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        entry = {"id": str(row["id"]), "kind": row["kind"], "status": row["status"], "progress": row["progress"]}
        if row["kind"] == "trade":
            entry["game"] = row["game"]
        grouped.setdefault(row["lease_worker_id"], []).append(entry)
    return grouped


def fleet_workers(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Worker summaries as /api/fleet presents them."""
    rows = conn.execute(
        """
        SELECT w.*, (last_heartbeat_at > now() - make_interval(secs => %s)) AS online
          FROM workers w ORDER BY name, id
        """,
        (online_after(conn),),
    ).fetchall()
    jobs = _current_jobs(conn)
    out = []
    for w in rows:
        out.append(
            {
                "id": w["id"],
                "name": w["name"],
                "online": bool(w["online"]),
                "desired_role": w["desired_role"],
                "reported_role": w["reported_role"],
                "role_epoch": w["role_epoch"],
                "acked_epoch": w["acked_epoch"],
                "switching": w["acked_epoch"] != w["role_epoch"] or w["reported_role"] != w["desired_role"],
                "auto_role": w["auto_role"],
                "enabled": w["enabled"],
                "cpu_pct": w["cpu_pct"],
                "ram_used_mb": w["ram_used_mb"],
                "ram_total_mb": w["ram_total_mb"],
                "code_version": w["code_version"],
                "python_version": w["python_version"],
                "hostname": w["hostname"],
                "last_heartbeat_at": w["last_heartbeat_at"],
                "current_jobs": jobs.get(w["id"], []),
            }
        )
    return out


def fleet(conn: psycopg.Connection) -> dict[str, Any]:
    """The /api/fleet document."""
    now = conn.execute("SELECT now() AS t").fetchone()["t"]
    return {"workers": fleet_workers(conn), "settings": public_settings(conn), "server_time": now}


def list_jobs(conn: psycopg.Connection, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Newest jobs first, optionally filtered by status."""
    limit = max(1, min(int(limit), 500))
    if status:
        return conn.execute(
            "SELECT * FROM jobs WHERE status = %s ORDER BY created_at DESC, id LIMIT %s",
            (status, limit),
        ).fetchall()
    return conn.execute("SELECT * FROM jobs ORDER BY created_at DESC, id LIMIT %s", (limit,)).fetchall()


def job_with_events(conn: psycopg.Connection, job_id: Any, limit: int = 50) -> dict[str, Any]:
    """One job plus its last `limit` events in chronological order."""
    job = dict(get_job(conn, job_id))
    events = conn.execute(
        "SELECT * FROM job_events WHERE job_id = %s ORDER BY id DESC LIMIT %s", (job["id"], limit)
    ).fetchall()
    job["events"] = list(reversed(events))
    return job


def audit_rows(conn: psycopg.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Newest audit_log rows first."""
    limit = max(1, min(int(limit), 500))
    return conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT %s", (limit,)).fetchall()


def worker_names(conn: psycopg.Connection) -> dict[str, str]:
    """worker id -> name for every worker, sorted by name."""
    rows = conn.execute("SELECT id, name FROM workers ORDER BY name, id").fetchall()
    return {row["id"]: row["name"] for row in rows}


# ------------------------------------------------------------------ step 4: the /trading page

UNATTENDED_AFTER_S = 60
EXCHANGE_DOWN_AFTER_S = 15
ACTIVE_ORDER_STATUSES = ("approved", "submitting", "open", "partial", "cancel_requested")


def upcoming_games(conn: psycopg.Connection, with_markets: bool = True, limit: int = 200) -> list[dict[str, Any]]:
    """Games that have not kicked off, soonest first; `with_markets` keeps only those
    with a confirmed market (the ones an assignment can trade)."""
    clause = "AND EXISTS (SELECT 1 FROM markets m WHERE m.game_id = g.game_id AND m.mapping_confirmed)" if with_markets else ""
    return conn.execute(
        f"""
        SELECT g.game_id, g.season, g.week, g.home_team, g.away_team, g.kickoff_at, g.status FROM games g
         WHERE g.status <> 'final' AND (g.kickoff_at IS NULL OR g.kickoff_at > now()) {clause}
         ORDER BY g.kickoff_at NULLS LAST, g.game_id LIMIT %s
        """,
        (max(1, min(int(limit), 1000)),),
    ).fetchall()


def assignable_models(conn: psycopg.Connection, limit: int = 200) -> list[dict[str, Any]]:
    """Models whose lineage is not retired for the Assign select: trained models
    first (an untrained search root mirrors the market and never trades), newest
    first within each group."""
    return conn.execute(
        """
        SELECT m.id, m.lineage_id, m.family, m.params, m.status, m.trained_through FROM models m
         WHERE NOT EXISTS (SELECT 1 FROM models r WHERE r.lineage_id = m.lineage_id AND r.status = 'retired')
         ORDER BY (m.trained_through IS NOT NULL) DESC, m.created_at DESC, m.id LIMIT %s
        """,
        (max(1, min(int(limit), 1000)),),
    ).fetchall()


def unattended_assignments(conn: psycopg.Connection, after_s: int = UNATTENDED_AFTER_S) -> int:
    """Active assignments whose trade job has sat queued for more than `after_s`."""
    row = conn.execute(
        """
        SELECT count(*) AS n FROM assignments a JOIN jobs j ON j.id = a.job_id
         WHERE a.status = 'active' AND j.status = 'queued'
           AND GREATEST(j.created_at, j.updated_at) < now() - make_interval(secs => %s)
        """,
        (after_s,),
    ).fetchone()
    return int(row["n"])


def exchange_down(conn: psycopg.Connection, after_s: int = EXCHANGE_DOWN_AFTER_S) -> bool:
    """The EXCHANGE DOWN banner: no heartbeat within `after_s` while an order is active
    or the kill switch is on."""
    row = conn.execute(
        """
        SELECT (heartbeat_at IS NULL OR heartbeat_at < now() - make_interval(secs => %s)) AS stale
          FROM exchange_state WHERE id
        """,
        (after_s,),
    ).fetchone()
    if row is None or not row["stale"]:
        return False
    killed = conn.execute("SELECT value FROM settings WHERE key = 'kill_switch'").fetchone()
    if killed is not None and killed["value"] is True:
        return True
    active = conn.execute(
        "SELECT 1 FROM orders WHERE status = ANY(%s) LIMIT 1", (list(ACTIVE_ORDER_STATUSES),)
    ).fetchone()
    return active is not None


def halted_paper_count(conn: psycopg.Connection) -> int:
    """Halted paper assignments whose game is not final (what "Activate all paper" would touch)."""
    row = conn.execute(
        """
        SELECT count(*) AS n FROM assignments a JOIN games g ON g.game_id = a.game_id
         WHERE a.mode = 'paper' AND a.status = 'halted' AND g.status <> 'final'
        """
    ).fetchone()
    return int(row["n"])


# ------------------------------------------------------------------ step 5: the live switch

LIVE_STATE_KEYS = (
    "live_enabled_at", "live_enabled_by", "credentials_present", "auth_ok", "auth_checked_at", "auth_failures",
    "balance_cents", "buying_power_cents", "balance_checked_at", "clock_skew_ms", "last_auth_error",
    "open_orders_checked_at",
)


def latest_auto_kill(conn: psycopg.Connection) -> dict[str, Any] | None:
    """The newest `auto_kill` audit row since the last `kill_reset`: what the killed
    top bar names as the reason. None when the kill was pressed by hand (or reset)."""
    row = conn.execute(
        """
        SELECT ts, actor, after FROM audit_log
         WHERE action = 'auto_kill'
           AND id > COALESCE((SELECT max(id) FROM audit_log WHERE action = 'kill_reset'), 0)
         ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    after = row["after"] if isinstance(row["after"], dict) else {}
    reason = after.get("reason") or (row["actor"] or "").removeprefix("auto:") or "unknown"
    return {"reason": str(reason), "ts": row["ts"], "actor": row["actor"], "detail": {k: v for k, v in after.items() if k != "reason"},
            "remedy": auto_kill_remedy(str(reason))}


def live_order_counts(conn: psycopg.Connection) -> dict[str, int]:
    """Active live orders for the /trading exchange box: all of them (`live`), the
    ones still open or on their way (`open`), the ones awaiting the exchange's
    cancel (`cancel_pending`) and, among all, the smoke orders."""
    row = conn.execute(
        """
        SELECT count(*) FILTER (WHERE mode = 'live') AS live,
               count(*) FILTER (WHERE mode = 'live' AND status = 'cancel_requested') AS cancel_pending,
               count(*) FILTER (WHERE mode = 'live' AND kind = 'smoke') AS smoke
          FROM orders WHERE status = ANY(%s)
        """,
        (list(ACTIVE_ORDER_STATUSES),),
    ).fetchone()
    live, pending = int(row["live"]), int(row["cancel_pending"])
    return {"live": live, "open": live - pending, "cancel_pending": pending, "smoke": int(row["smoke"])}


def live_activity(conn: psycopg.Connection) -> bool:
    """Real money still in play while live is off: an active live order, a live
    assignment not yet settled, or a live bankroll with cash reserved or in open
    positions. The top bar keeps the live P&L segment while this holds."""
    row = conn.execute(
        """
        SELECT EXISTS (SELECT 1 FROM orders WHERE mode = 'live' AND status = ANY(%s))
            OR EXISTS (SELECT 1 FROM assignments WHERE mode = 'live' AND status IN ('active', 'halted'))
            OR EXISTS (SELECT 1 FROM bankrolls WHERE mode = 'live' AND (reserved_cents > 0 OR open_cost_cents > 0)) AS active
        """,
        (list(ACTIVE_ORDER_STATUSES),),
    ).fetchone()
    return bool(row and row["active"])


AUTO_KILL_REMEDIES: dict[str, str] = {
    "auth_failures": "fix exchange.env or the auth config, then docker compose up -d --force-recreate exchange, "
                     "probe-account until auth is ok, then RESUME and re-enable live",
    "clock_skew": "live orders rest until the skew clears or their GTD; fix the host clock (Docker Desktop: wsl --shutdown "
                  "or restart Docker Desktop), then docker compose restart exchange, confirm the skew here, RESUME, re-enable; "
                  "cancel-all --direct pulls them now",
    "unknown_order": "this system needs exclusive use of the account: cancel any hand-placed order in the app, "
                     "check the open orders there, then RESUME and re-enable",
    "unknown_fill": "a fill for an order this system did not place: check the app for hand-placed orders, "
                    "reconcile positions by hand, then RESUME and re-enable",
    "ambiguous_reconciliation": "two exchange orders share one client id: cancel the duplicate in the app, "
                                "then RESUME and re-enable",
    "late_fill": "a fill arrived for an order already closed here: book the position by hand (ledger adjust), "
                 "check the app, then RESUME and re-enable",
}


def auto_kill_remedy(reason: str | None) -> str | None:
    """The one-line recovery for an auto-kill reason (README "Auto-kill reasons")."""
    return AUTO_KILL_REMEDIES.get(str(reason or ""))


def live_state_fallback(conn: psycopg.Connection) -> dict[str, Any]:
    """The Settings live group's state straight from the tables, for a host without
    host.trading.live (no phrase, no precondition list); the page never 500s."""
    row = conn.execute("SELECT * FROM exchange_state WHERE id").fetchone() or {}
    now = conn.execute("SELECT now() AS t").fetchone()["t"]
    state: dict[str, Any] = {key: row.get(key) for key in LIVE_STATE_KEYS}
    checked = state.get("auth_checked_at")
    state.update(
        {
            "live_enabled": get_setting(conn, "live_enabled", False) is True,
            "credentials_present": bool(state.get("credentials_present")),
            "auth_ok": bool(state.get("auth_ok")),
            "auth_failures": int(state.get("auth_failures") or 0),
            "auth_age_s": None if checked is None else max(0.0, (now - checked).total_seconds()),
            "killed": get_setting(conn, "kill_switch", False) is True,
            "auto_kill_reasons": [r["reason"] for r in [latest_auto_kill(conn)] if r],
            "expected_phrase": "", "problems": ["the live switch module is not installed on this host"],
        }
    )
    return state
