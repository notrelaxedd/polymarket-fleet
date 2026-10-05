"""The Jobs page list (docs/UI.md "Jobs"): two tabs, Running (queued, running and
held) and Done (the last 50 finished, failed or cancelled), each job one row with a
short target ("elo_blend n 50", "8f173b7b", "KC @ LV"), and the counts the stats show.

Read-only display shaping; the JSON API keeps host.views.list_jobs.
"""
from __future__ import annotations

from typing import Any

import psycopg

TABS: dict[str, tuple[str, ...]] = {
    "running": ("queued", "leased", "cancel_requested"),
    "done": ("succeeded", "failed", "cancelled"),
}
DONE_LIMIT = 50
RUNNING_LIMIT = 200
CHIP_STATE = {
    "queued": "muted", "leased": "ok", "cancel_requested": "warn",
    "succeeded": "ok", "failed": "bad", "cancelled": "muted",
}

JOBS_SQL = """
    SELECT j.*, g.away_team || ' @ ' || g.home_team AS game
      FROM jobs j
      LEFT JOIN assignments a ON j.kind = 'trade' AND a.id::text = j.params ->> 'assignment_id'
      LEFT JOIN games g ON g.game_id = a.game_id
     WHERE j.status = ANY(%s)
"""


def tab_name(value: str | None) -> str:
    """The tab a ?tab= value names; anything unknown is Running."""
    return value if value in TABS else "running"


def list_tab(conn: psycopg.Connection, tab: str) -> list[dict[str, Any]]:
    """Running: newest first. Done: the most recently finished first, at most DONE_LIMIT."""
    if tab == "done":
        order, limit = "ORDER BY j.finished_at DESC NULLS LAST, j.created_at DESC, j.id", DONE_LIMIT
    else:
        order, limit = "ORDER BY j.created_at DESC, j.id", RUNNING_LIMIT
    rows = conn.execute(f"{JOBS_SQL} {order} LIMIT %s", (list(TABS[tab]), limit)).fetchall()
    return [{**row, "target": job_target(row), "state": CHIP_STATE.get(row["status"], "muted")} for row in rows]


def job_target(job: dict[str, Any]) -> str:
    """What a job works on, in a few words: the model, the family, the game."""
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    model = str(params.get("model_id") or "")[:8]
    kind = job.get("kind")
    if kind == "model_search":
        return f"{params.get('family', '?')} n {params.get('n', '?')}"
    if kind == "backtest":
        return model or str(params.get("family") or "")
    if kind == "train":
        through = params.get("through") if isinstance(params.get("through"), dict) else {}
        point = f" thru {through.get('season')} w{through.get('week')}" if through else ""
        return f"{model}{point}"
    if kind == "validate":
        return model
    if kind == "trade":
        return str(job.get("game") or "")
    if kind == "sleep":
        return f"{params.get('seconds', '?')} s"
    return model


def job_counts(conn: psycopg.Connection) -> dict[str, int]:
    """Running (leased, not trade), held (leased trade jobs), queued, and finished or
    failed in the last 24 hours, plus the size of each tab."""
    row = conn.execute(
        """
        SELECT count(*) FILTER (WHERE status IN ('leased', 'cancel_requested') AND kind <> 'trade') AS running,
               count(*) FILTER (WHERE status IN ('leased', 'cancel_requested') AND kind = 'trade') AS held,
               count(*) FILTER (WHERE status = 'queued') AS queued,
               count(*) FILTER (WHERE status = ANY(%s) AND finished_at > now() - interval '24 hours') AS done_24h,
               count(*) FILTER (WHERE status = 'failed' AND finished_at > now() - interval '24 hours') AS failed_24h,
               count(*) FILTER (WHERE status = ANY(%s)) AS done
          FROM jobs
        """,
        (list(TABS["done"]), list(TABS["done"])),
    ).fetchone()
    counts = {key: int(row[key]) for key in ("running", "held", "queued", "done_24h", "failed_24h", "done")}
    counts["tab_running"] = counts["running"] + counts["held"] + counts["queued"]
    counts["tab_done"] = min(counts["done"], DONE_LIMIT)
    return counts


def jobs_list_context(conn: psycopg.Connection, tab: str | None) -> dict[str, Any]:
    """The tab, its rows and the counts for jobs.html."""
    name = tab_name(tab)
    return {"tab": name, "jobs": list_tab(conn, name), "counts": job_counts(conn)}


def duration_text(seconds: float | None) -> str:
    """A run time in words: 42 s, 4 min, 2 h 5 min; "-" when the job never started."""
    if seconds is None:
        return "-"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60} min"


def run_seconds(job: dict[str, Any], now: Any) -> float | None:
    """Seconds from the start to the finish (or to now while it runs); None before it starts."""
    start = job.get("started_at")
    if start is None:
        return None
    return ((job.get("finished_at") or now) - start).total_seconds()
