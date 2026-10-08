"""Settings table access, validation (the per-key checks live in host.settings_schema)
and the job kind -> role mapping."""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host.errors import BadRequest
from host.events import add_audit
from host.settings_schema import (  # noqa: F401  (re-exported for the modules that read them here)
    FIRST_SEASON,
    FLAG_NAMES,
    LAST_SEASON,
    SCHEMA,
    season_problems,
    timing_problems,
)

ROLES = ("idle", "backtest", "model_search", "train", "trade")
BATCH_ROLES = ("backtest", "model_search", "train")
JOB_ROLES = ("backtest", "model_search", "train", "trade")
KIND_TO_ROLE = {
    "sleep": "backtest",
    "backtest": "backtest",
    "validate": "backtest",
    "model_search": "model_search",
    "train": "train",
    "trade": "trade",
    # step 9: stocks on Alpaca (docs/ALPACA.md)
    "stock_search": "model_search",
    "stock_backtest": "backtest",
    "stock_validate": "backtest",
    "stock_trade": "trade",
}
PUBLIC_SETTINGS = (
    "live_enabled",
    "kill_switch",
    "tz",
    "lease_seconds",
    "heartbeat_seconds",
    "online_after_seconds",
    "max_expiries",
)

def role_for_kind(kind: str) -> str:
    """Map a job kind to the worker role that may run it; 400 on unknown kind."""
    try:
        return KIND_TO_ROLE[kind]
    except KeyError:
        raise BadRequest(f"unknown job kind: {kind!r}") from None


def get_settings(conn: psycopg.Connection) -> dict[str, Any]:
    """All settings as a plain dict."""
    rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return {row["key"]: row["value"] for row in rows}


def public_settings(conn: psycopg.Connection) -> dict[str, Any]:
    """The subset of settings exposed on /api/fleet."""
    all_settings = get_settings(conn)
    return {key: all_settings[key] for key in PUBLIC_SETTINGS if key in all_settings}


def get_setting(conn: psycopg.Connection, key: str, default: Any = None) -> Any:
    """One setting value, or `default` when the key is missing."""
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return default if row is None else row["value"]


def get_int_setting(conn: psycopg.Connection, key: str, default: int) -> int:
    """One numeric setting coerced to int."""
    value = get_setting(conn, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def validate_settings(updates: dict[str, Any], current: dict[str, Any]) -> None:
    """Reject unknown keys, values of the wrong type or range (no coercion), fleet
    timing combinations that cannot work and a validation era that overlaps the
    search era, judged over the stored settings merged with the update."""
    unknown = sorted(set(updates) - set(current))
    if unknown:
        raise BadRequest(f"unknown setting keys: {', '.join(unknown)}")
    problems = []
    for key, value in updates.items():
        check = SCHEMA.get(key)
        error = check(value) if check is not None else None
        if error:
            problems.append(f"{key} {error}")
    if not problems:
        merged = {**current, **updates}
        problems = timing_problems(merged) + season_problems(merged)
    if problems:
        raise BadRequest("invalid settings: " + "; ".join(problems))


def guarded_problems(updates: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Keys the generic settings write must not touch. `kill_switch` only moves through
    the kill transaction (cancel-all, halts, audit) and the RESUME reset; `live_enabled`
    only through the typed live switch (`POST /live`, `POST /live/off`, step 5)."""
    problems = []
    if "kill_switch" in updates:
        problems.append("kill_switch is read-only here: use /api/kill or /api/kill/reset")
    if "live_enabled" in updates:
        problems.append("live_enabled is read-only here: use /live or /live/off")
    return problems


def set_settings(conn: psycopg.Connection, updates: dict[str, Any], actor: str | None = None) -> dict[str, Any]:
    """Validate and store the given keys; one audit row per changed key."""
    before = get_settings(conn)
    validate_settings(updates, before)
    guarded = guarded_problems(updates, before)
    if guarded:
        raise BadRequest("invalid settings: " + "; ".join(guarded))
    for key, value in updates.items():
        if before[key] == value:
            continue
        conn.execute(
            "UPDATE settings SET value = %s, updated_at = now() WHERE key = %s",
            (Jsonb(value), key),
        )
        add_audit(conn, "settings_changed", key, actor, {key: before[key]}, {key: value})
    return get_settings(conn)
