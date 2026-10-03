"""Settings table access, per-key validation and the job kind -> role mapping."""
from __future__ import annotations

import re
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from host.errors import BadRequest
from host.events import add_audit

ROLES = ("idle", "backtest", "model_search", "train", "trade")
BATCH_ROLES = ("backtest", "model_search", "train")
JOB_ROLES = ("backtest", "model_search", "train", "trade")
KIND_TO_ROLE = {
    "sleep": "backtest",
    "backtest": "backtest",
    "model_search": "model_search",
    "train": "train",
    "trade": "trade",
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
TZ_RE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+)*$")

Validator = Callable[[Any], str | None]


def _is_int(value: Any) -> bool:
    """A JSON integer (bools are not numbers here)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return _is_int(value) or (isinstance(value, float) and value == value and value not in (float("inf"), float("-inf")))


def _bool(value: Any) -> str | None:
    return None if isinstance(value, bool) else "must be true or false"


def _int_range(low: int, high: int | None = None, nullable: bool = False) -> Validator:
    def check(value: Any) -> str | None:
        if value is None and nullable:
            return None
        if not _is_int(value):
            return "must be an integer"
        if value < low or (high is not None and value > high):
            return f"must be between {low} and {high}" if high is not None else f"must be at least {low}"
        return None

    return check


def _fraction(value: Any) -> str | None:
    if not _is_number(value):
        return "must be a number"
    if value < 0 or value > 1:
        return "must be between 0 and 1"
    return None


def _tz(value: Any) -> str | None:
    if not isinstance(value, str) or not TZ_RE.match(value) or len(value) > 64:
        return "must be an IANA time zone name such as America/New_York"
    return None


def _cents_by_mode(value: Any) -> str | None:
    if not isinstance(value, dict) or set(value) != {"live", "paper"}:
        return "must be an object with integer cents for live and paper"
    for mode in ("live", "paper"):
        if not _is_int(value[mode]) or value[mode] < 0:
            return f"{mode} must be an integer of at least 0"
    return None


SCHEMA: dict[str, Validator] = {
    "live_enabled": _bool,
    "kill_switch": _bool,
    "tz": _tz,
    "lease_seconds": _int_range(10, 3600),
    "heartbeat_seconds": _int_range(1, 60),
    "online_after_seconds": _int_range(5, 3600),
    "max_expiries": _int_range(1, 100, nullable=True),
    "liquidity_floor_cents": _int_range(0),
    "max_bet_cents": _int_range(0),
    "max_daily_loss_cents": _cents_by_mode,
    "default_bankroll_cents": _int_range(0),
    "min_edge": _fraction,
    "kelly_fraction": _fraction,
    "trade_max_games": _int_range(0, 100),
}


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


def validate_settings(updates: dict[str, Any], known: set[str]) -> None:
    """Reject unknown keys and values of the wrong type or range (no coercion)."""
    unknown = sorted(set(updates) - known)
    if unknown:
        raise BadRequest(f"unknown setting keys: {', '.join(unknown)}")
    problems = []
    for key, value in updates.items():
        check = SCHEMA.get(key)
        error = check(value) if check is not None else None
        if error:
            problems.append(f"{key} {error}")
    if problems:
        raise BadRequest("invalid settings: " + "; ".join(problems))


def set_settings(conn: psycopg.Connection, updates: dict[str, Any], actor: str | None = None) -> dict[str, Any]:
    """Validate and store the given keys; one audit row per changed key."""
    before = get_settings(conn)
    validate_settings(updates, set(before))
    for key, value in updates.items():
        if before[key] == value:
            continue
        conn.execute(
            "UPDATE settings SET value = %s, updated_at = now() WHERE key = %s",
            (Jsonb(value), key),
        )
        add_audit(conn, "settings_changed", key, actor, {key: before[key]}, {key: value})
    return get_settings(conn)
