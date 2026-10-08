"""Per-key validators for the settings table and the cross-field rules
(host.settings applies them; docs/PROTOCOL.md "Owner routes", settings schema)."""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from host.money import MAX_CENTS

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


def _object_of(fields: dict[str, Validator], label: str, optional: dict[str, Validator] | None = None) -> Validator:
    """A JSON object with exactly `fields` (plus any of `optional`, which readers
    default when absent), each checked by its validator."""
    optional = optional or {}

    def check(value: Any) -> str | None:
        if not isinstance(value, dict) or not set(fields) <= set(value) or not set(value) <= set(fields) | set(optional):
            return f"must be an object with {label}"
        for key, inner in {**fields, **optional}.items():
            if key not in value:
                continue
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


def _workers(value: Any) -> str | None:
    """`search_workers`: "auto" (cpu_count - 1, at least 1) or an integer 1..64."""
    if value == "auto":
        return None
    return _int_range(1, 64)(value) and "must be \"auto\" or an integer between 1 and 64"


FLAG_NAMES = ("overfit", "fragile", "regime_dependent")


def _flag_list(value: Any) -> str | None:
    """A list of distinct flag names out of FLAG_NAMES (may be empty)."""
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        return "must be a list of flag names"
    unknown = sorted(set(value) - set(FLAG_NAMES))
    if unknown:
        return "holds unknown flags: " + ", ".join(unknown) + " (known: " + ", ".join(FLAG_NAMES) + ")"
    if len(set(value)) != len(value):
        return "must not repeat a flag"
    return None


def _url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 512 or not re.match(r"^https?://[^\s]+$", value):
        return "must be an http(s) URL"
    return None


def _season_url(value: Any) -> str | None:
    """An http(s) URL template holding {season} (the nflverse per-season files)."""
    return _url(value) or (None if "{season}" in value else "must contain {season}, such as .../injuries_{season}.csv")


def _one_of(*choices: str) -> Validator:
    def check(value: Any) -> str | None:
        return None if value in choices else "must be one of " + ", ".join(choices)

    return check


def _json_object(value: Any) -> str | None:
    """Any JSON object (free-form config), at most 16 KiB when serialised."""
    if not isinstance(value, dict):
        return "must be an object"
    if len(json.dumps(value)) > 16 * 1024:
        return "must be at most 16 KiB"
    return None


def _market_source_config(value: Any) -> str | None:
    """The free-form source config, except that the live gateway's destination and
    signing template are pinned (host/exchange/adapters/live_policy.py): a settings
    write can never redirect signed requests or choose the signed bytes."""
    error = _json_object(value)
    if error:
        return error
    from host.exchange.adapters.live_policy import polymarket_us_problems

    problems = polymarket_us_problems(value.get("polymarket_us"))
    return "; ".join(problems) if problems else None


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


def season_problems(settings: dict[str, Any]) -> list[str]:
    """Cross-field rule over the two eras: the validation era must start after the
    search era ends. With a set last search season, after that season; with a null
    one (capped below the validation era when a job is created, host/eras.py), after
    the first search season, so the capped search era is never empty."""
    search, validation = settings.get("backtest_seasons"), settings.get("validation_seasons")
    if _seasons(search) or _seasons(validation):
        return []
    if search[1] is not None and validation[0] <= search[1]:
        return [f"validation_seasons must start after the search era ends ({search[1]}): backtest_seasons is {search}"]
    if search[1] is None and validation[0] <= search[0]:
        return [f"validation_seasons must start after the first search season ({search[0]}): backtest_seasons is {search}"]
    return []


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
        "min_bets, min_roi, max_drawdown and optionally require_validation, min_roi_ci_low, max_market_p, forbid_flags",
        optional={
            "require_validation": _bool, "min_roi_ci_low": _number_range(-1, 1), "max_market_p": _number_range(0, 1),
            "forbid_flags": _flag_list,
        },
    ),
    "backtest_seasons": _seasons,
    # step 6: the held-out era and the search pool
    "validation_seasons": _seasons,
    "search_workers": _workers,
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
    "market_source_config": _market_source_config,
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
        "min_games, min_bets, min_days, min_clv, min_pnl_cents and optionally clv_ci_excludes_zero",
        optional={"clv_ci_excludes_zero": _bool},
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
    # step 5: live
    "auth_probe_interval_s": _int_range(10, 86400),
    "buying_power_max_age_s": _int_range(10, 86400),
    "submitting_grace_s": _int_range(5, 3600),
    "auto_kill": _object_of({"auth_failures": _int_range(1, 100), "clock_skew_ms": _int_range(1000, 600000)}, "auth_failures and clock_skew_ms"),
    "smoke_hold_seconds": _int_range(1, 600),
    "live_fills_poll_s": _int_range(1, 60),
    "open_orders_audit_s": _int_range(5, 3600),
    # step 6 Part B: snapshot replay and the nflverse signals
    "allow_sim_prices": _bool,
    "decision_minutes_before_kickoff": _int_range(0, 300),
    "nflverse_injuries_url": _season_url,
    "nflverse_pbp_url": _season_url,
    "signals_refresh_hours": _int_range(1, 168),
}

# step 6 Part C: the in-game trade rules and the game-state feed (host/settings_schema_ingame.py)
from host.settings_schema_ingame import INGAME_SCHEMA  # noqa: E402

SCHEMA.update(INGAME_SCHEMA)

# step 9: stocks on Alpaca, the daily bar feed (host/settings_schema_stocks.py)
from host.settings_schema_stocks import STOCKS_SCHEMA  # noqa: E402

SCHEMA.update(STOCKS_SCHEMA)
