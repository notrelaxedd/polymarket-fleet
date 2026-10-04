"""Settings table access, per-key validation and the job kind -> role mapping."""
from __future__ import annotations

import re
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from host.errors import BadRequest
from host.events import add_audit
from host.money import MAX_CENTS

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
        if not _is_int(value[mode]) or value[mode] < 0 or value[mode] > MAX_CENTS:
            return f"{mode} must be an integer between 0 and {MAX_CENTS}"
    return None


def _object_of(fields: dict[str, Validator], label: str) -> Validator:
    """A JSON object with exactly `fields`, each checked by its validator."""

    def check(value: Any) -> str | None:
        if not isinstance(value, dict) or set(value) != set(fields):
            return f"must be an object with {label}"
        for key, inner in fields.items():
            error = inner(value[key])
            if error:
                return f"{key} {error}"
        return None

    return check


def _number_range(low: float, high: float) -> Validator:
    def check(value: Any) -> str | None:
        if not _is_number(value):
            return "must be a number"
        if value < low or value > high:
            return f"must be between {low} and {high}"
        return None

    return check


FIRST_SEASON, LAST_SEASON = 1999, 2100


def _seasons(value: Any) -> str | None:
    """[first, last]: ints in 1999..2100, last may be null (= last complete season)."""
    if not isinstance(value, list) or len(value) != 2:
        return "must be [first, last]"
    first, last = value
    if not _is_int(first) or first < FIRST_SEASON or first > LAST_SEASON:
        return f"first season must be an integer between {FIRST_SEASON} and {LAST_SEASON}"
    if last is None:
        return None
    if not _is_int(last) or last < FIRST_SEASON or last > LAST_SEASON:
        return f"last season must be null or an integer between {FIRST_SEASON} and {LAST_SEASON}"
    if last < first:
        return "last season must not be before the first"
    return None


def _url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 512 or not re.match(r"^https?://[^\s]+$", value):
        return "must be an http(s) URL"
    return None


def _one_of(*choices: str) -> Validator:
    def check(value: Any) -> str | None:
        return None if value in choices else "must be one of " + ", ".join(choices)

    return check


def _json_object(value: Any) -> str | None:
    """Any JSON object (free-form config), at most 16 KiB when serialised."""
    if not isinstance(value, dict):
        return "must be an object"
    import json

    if len(json.dumps(value)) > 16 * 1024:
        return "must be at most 16 KiB"
    return None


def timing_problems(settings: dict[str, Any]) -> list[str]:
    """Cross-field rules over the merged fleet timing settings.

    A lease must outlive two heartbeats plus the agent's HTTP timeout, otherwise every
    running lease expires between renewals; and a worker must count as online for
    longer than one heartbeat interval, otherwise it flickers offline between beats.
    """
    lease, beat, online = (settings.get(k) for k in ("lease_seconds", "heartbeat_seconds", "online_after_seconds"))
    if not (_is_int(lease) and _is_int(beat) and _is_int(online)):
        return []
    problems = []
    if lease < 2 * beat + 5:
        problems.append(f"lease_seconds must be at least {2 * beat + 5} (2 x heartbeat_seconds + 5)")
    if online <= beat:
        problems.append(f"online_after_seconds must be greater than heartbeat_seconds ({beat})")
    return problems


SCHEMA: dict[str, Validator] = {
    "live_enabled": _bool,
    "kill_switch": _bool,
    "tz": _tz,
    "lease_seconds": _int_range(10, 3600),
    "heartbeat_seconds": _int_range(1, 60),
    "online_after_seconds": _int_range(5, 3600),
    "max_expiries": _int_range(1, 100, nullable=True),
    "liquidity_floor_cents": _int_range(0, MAX_CENTS),
    "max_bet_cents": _int_range(0, MAX_CENTS),
    "max_daily_loss_cents": _cents_by_mode,
    "default_bankroll_cents": _int_range(0, MAX_CENTS),
    "min_edge": _fraction,
    "kelly_fraction": _fraction,
    "trade_max_games": _int_range(0, 100),
    "fee_model": _object_of({"taker_rate": _fraction, "half_spread": _fraction}, "taker_rate and half_spread"),
    "thresholds_backtest": _object_of(
        {"min_bets": _int_range(0, 1_000_000), "min_roi": _number_range(-1, 1), "max_drawdown": _number_range(0, 1)},
        "min_bets, min_roi and max_drawdown",
    ),
    "backtest_seasons": _seasons,
    "nflverse_refresh_hours": _int_range(1, 168),
    "nflverse_url": _url,
    # step 4: trading
    "participation": _fraction,
    "book_max_age_s": _int_range(1, 3600),
    "orphan_cancel_after_s": _int_range(5, 3600),
    "gtd_seconds": _int_range(10, 86400),
    "snapshot_retention_days": _int_range(1, 365),
    "snapshot_active_s": _int_range(1, 300),
    "snapshot_idle_s": _int_range(1, 3600),
    "market_source": _one_of("sim", "polymarket_us", "polymarket_clob"),
    "market_source_config": _json_object,
    "market_lookahead_days": _int_range(1, 60),
    "max_paper_models_per_game": _int_range(1, 20),
    "thresholds_paper": _object_of(
        {
            "min_games": _int_range(0, 10_000),
            "min_bets": _int_range(0, 1_000_000),
            "min_days": _int_range(0, 3650),
            "min_clv": _number_range(-1, 1),
            "min_pnl_cents": _int_range(-MAX_CENTS, MAX_CENTS),
        },
        "min_games, min_bets, min_days, min_clv and min_pnl_cents",
    ),
    "trade_pregame_only": _bool,
    "trade_tick_s": _int_range(1, 60),
    "rate_limits": _object_of(
        {
            "orders_per_s": _number_range(0.1, 1000),
            "cancels_per_s": _number_range(0.1, 1000),
            "market_data_per_s": _number_range(0.1, 1000),
            "account_per_s": _number_range(0.1, 1000),
        },
        "orders_per_s, cancels_per_s, market_data_per_s and account_per_s",
    ),
    "max_exposure_cents": _cents_by_mode,
    "scores_url": _url,
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


def validate_settings(updates: dict[str, Any], current: dict[str, Any]) -> None:
    """Reject unknown keys, values of the wrong type or range (no coercion) and fleet
    timing combinations that cannot work, judged over the stored settings merged with
    the update."""
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
        problems = timing_problems({**current, **updates})
    if problems:
        raise BadRequest("invalid settings: " + "; ".join(problems))


def guarded_problems(updates: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Keys the generic settings write must not touch. `kill_switch` only moves through
    the kill transaction (cancel-all, halts, audit) and the RESUME reset; and live
    trading cannot be switched on while the fleet is killed."""
    problems = []
    if "kill_switch" in updates:
        problems.append("kill_switch is read-only here: use /api/kill or /api/kill/reset")
    if updates.get("live_enabled") is True and current.get("kill_switch") is True:
        problems.append("live_enabled cannot be turned on while kill_switch is on")
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
