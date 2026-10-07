"""The fleet page's event feed (GET /api/fleet/events): audit_log and job_events rows
about workers and the kill switch, as short plain-English lines.

Keys are `a:<audit_log.id>` and `j:<job_events.id>`; `who` is the worker's name or
`fleet` for fleet-wide rows; `tone` is ok (normal), hot (warning), off (machine going
down) or fg (neutral). See docs/PROTOCOL.md "Fleet UI additions".
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg

from host.errors import BadRequest

# The roles in display order with the names the fleet page shows.
ROLE_NAMES = (
    {"id": "idle", "name": "Idle", "short": "Idle"},
    {"id": "backtest", "name": "Backtest", "short": "Backtest"},
    {"id": "model_search", "name": "Model search", "short": "Search"},
    {"id": "train", "name": "Training", "short": "Train"},
    {"id": "trade", "name": "Trading", "short": "Trade"},
)
ROLE_LABEL = {role["id"]: role["name"] for role in ROLE_NAMES}

# Job kinds as they read in "Took a <kind> job".
KIND_LABEL = {
    "sleep": "test",
    "backtest": "backtest",
    "validate": "validation",
    "model_search": "model search",
    "train": "training",
    "trade": "trading",
}

AUDIT_ACTIONS = (
    "set_role", "auto_role", "auto_idle", "set_enabled", "worker_enrolled",
    "reboot_requested", "reboot_done", "kill", "auto_kill", "kill_reset",
)
JOB_EVENTS = ("claimed", "succeeded", "failed", "released", "lease_expired", "cancelled")

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
FLEET = "fleet"


def parse_since(value: str | None) -> datetime | None:
    """An ISO-8601 timestamp from the query string; naive means UTC. A `+` that arrived
    unencoded (as a space) is put back. 400 when it does not parse."""
    if value is None or value == "":
        return None
    text = value.strip().replace(" ", "+")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise BadRequest(f"since: not an ISO-8601 timestamp: {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _by(text: str, actor: str | None) -> str:
    return f"{text} by {actor}" if actor else text


def _role(detail: dict[str, Any]) -> str:
    role = detail.get("desired_role")
    return ROLE_LABEL.get(str(role), str(role or "another role"))


def audit_text(action: str, actor: str | None, after: dict[str, Any]) -> tuple[str, str]:
    """(text, tone) for one audit row."""
    if action == "set_role":
        return _by(f"Moved to {_role(after)}", actor), "ok"
    if action == "auto_role":
        return f"Moved to {_role(after)} for a job", "ok"
    if action == "auto_idle":
        return "Back to Idle, no work left", "ok"
    if action == "set_enabled":
        return (_by("Enabled", actor), "ok") if after.get("enabled") else (_by("Disabled", actor), "fg")
    if action == "worker_enrolled":
        return "Enrolled", "ok"
    if action == "reboot_requested":
        return _by("Reboot requested", actor), "off"
    if action == "reboot_done":
        return "Back up after a reboot", "ok"
    if action == "kill":
        return _by("Kill switch on", actor), "hot"
    if action == "auto_kill":
        reason = str(after.get("reason") or "unknown").replace("_", " ")
        return f"Kill switch on automatically: {reason}", "hot"
    if action == "kill_reset":
        return _by("Kill switch off", actor), "ok"
    return action.replace("_", " ").capitalize(), "fg"


def job_text(event: str, kind: str | None, detail: dict[str, Any]) -> tuple[str, str]:
    """(text, tone) for one job event."""
    label = KIND_LABEL.get(str(kind), str(kind or "a"))
    if event == "claimed":
        return f"Took a {label} job", "ok"
    if event == "succeeded":
        return f"Finished a {label} job", "ok"
    if event == "failed":
        return f"A {label} job failed", "hot"
    if event == "lease_expired":
        return f"Lost a {label} job (no heartbeat)", "hot"
    if event == "cancelled":
        return f"Cancelled a {label} job", "fg"
    if event == "released":
        reason = detail.get("reason")
        if reason == "oom":
            return f"Ran out of memory on a {label} job", "hot"
        if reason == "cancel" or detail.get("status") == "cancelled":
            return f"Stopped a cancelled {label} job", "fg"
        return f"Handed back a {label} job", "fg"
    return f"{event.replace('_', ' ').capitalize()} ({label} job)", "fg"


EVENTS_SQL = """
SELECT * FROM (
  (SELECT 'a:' || a.id AS key, a.ts, a.id AS seq, 0 AS src, a.action AS name, a.actor,
          a.after AS detail, w.id AS worker_id, w.name AS who, NULL::text AS kind
     FROM audit_log a LEFT JOIN workers w ON w.id = a.entity
    WHERE a.action = ANY(%(actions)s)
      AND NOT (a.action = 'kill' AND COALESCE(a.actor, '') LIKE 'auto:%%')
      AND (%(since)s::timestamptz IS NULL OR a.ts >= %(since)s::timestamptz)
    ORDER BY a.id DESC LIMIT %(limit)s)
  UNION ALL
  (SELECT 'j:' || e.id, e.ts, e.id, 1, e.event, NULL, e.detail, e.worker_id,
          COALESCE(w.name, e.worker_id), j.kind
     FROM job_events e JOIN jobs j ON j.id = e.job_id LEFT JOIN workers w ON w.id = e.worker_id
    WHERE e.worker_id IS NOT NULL AND e.event = ANY(%(events)s)
      AND (%(since)s::timestamptz IS NULL OR e.ts >= %(since)s::timestamptz)
    ORDER BY e.id DESC LIMIT %(limit)s)
) x ORDER BY ts DESC, src DESC, seq DESC LIMIT %(limit)s
"""


def fleet_events(conn: psycopg.Connection, since: datetime | None = None, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Newest first: the newest `limit` events, or with `since` every event at or after
    it (inclusive, the client dedupes by key) capped at `limit`."""
    if not 1 <= int(limit) <= MAX_LIMIT:
        raise BadRequest(f"limit must be 1..{MAX_LIMIT}")
    rows = conn.execute(
        EVENTS_SQL,
        {"actions": list(AUDIT_ACTIONS), "events": list(JOB_EVENTS), "since": since, "limit": int(limit)},
    ).fetchall()
    events = []
    for row in rows:
        detail = row["detail"] if isinstance(row["detail"], dict) else {}
        if row["src"] == 0:
            text, tone = audit_text(row["name"], row["actor"], detail)
        else:
            text, tone = job_text(row["name"], row["kind"], detail)
        events.append(
            {"key": row["key"], "ts": row["ts"], "worker_id": row["worker_id"],
             "who": row["who"] or FLEET, "tone": tone, "text": text}
        )
    now = conn.execute("SELECT now() AS t").fetchone()["t"]
    return {"events": events, "server_time": now}
