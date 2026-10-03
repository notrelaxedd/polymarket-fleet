"""Read-only queries behind the owner API (/api/fleet, job listings)."""
from __future__ import annotations

from typing import Any

import psycopg

from host.leases import get_job
from host.scheduling import online_after
from host.settings import public_settings

JOB_STATUSES = ("queued", "leased", "cancel_requested", "succeeded", "failed", "cancelled")


def _current_jobs(conn: psycopg.Connection) -> dict[str, list[dict[str, Any]]]:
    """Active jobs grouped by lease worker."""
    rows = conn.execute(
        """
        SELECT id, kind, status, progress, lease_worker_id FROM jobs
         WHERE status IN ('leased', 'cancel_requested') ORDER BY started_at
        """
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["lease_worker_id"], []).append(
            {"id": str(row["id"]), "kind": row["kind"], "status": row["status"], "progress": row["progress"]}
        )
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
