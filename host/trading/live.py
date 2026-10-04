"""The typed live switch (docs/LIVE.md "Live switch").

`enable_live` needs the exact dated phrase ("ENABLE LIVE TRADING YYYY-MM-DD", today in
the owner's time zone), the kill switch off, credentials loaded by the exchange
process, a fresh successful auth probe (within AUTH_MAX_AGE_S) and a clock skew within
`auto_kill.clock_skew_ms`. `disable_live` is immediate and goes through
`kill.live_off` (live assignments halted, live orders cancelled on the exchange).
`live_state` is what GET /api/live and the Settings page show. The gate helpers at the
bottom are shared with assignments and approvals.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from host import kill
from host.errors import BadRequest, Conflict
from host.events import add_audit
from host.exchange.state import read_state, set_live_enabled
from host.settings import get_setting
from host.trading.positions import owner_tz, server_now

PHRASE_PREFIX = "ENABLE LIVE TRADING "
AUTH_MAX_AGE_S = 600
DEFAULT_SKEW_LIMIT_MS = 30_000


def skew_limit_ms(conn: psycopg.Connection) -> int:
    """settings.auto_kill.clock_skew_ms (30 000 when unset or malformed)."""
    block = get_setting(conn, "auto_kill", None)
    try:
        return int((block or {}).get("clock_skew_ms", DEFAULT_SKEW_LIMIT_MS))
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_SKEW_LIMIT_MS


def expected_phrase(conn: psycopg.Connection, now: datetime | None = None) -> str:
    """The phrase the owner must type today, in the owner's time zone."""
    moment = (now or server_now(conn)).astimezone(owner_tz(conn))
    return PHRASE_PREFIX + moment.date().isoformat()


def is_live_enabled(conn: psycopg.Connection) -> bool:
    return get_setting(conn, "live_enabled", False) is True


def auth_age_s(state: dict[str, Any], now: datetime) -> float | None:
    checked = state.get("auth_checked_at")
    return None if checked is None else max(0.0, (now - checked).total_seconds())


def auth_fresh(state: dict[str, Any], now: datetime, max_age_s: float = AUTH_MAX_AGE_S) -> bool:
    """auth_ok with a probe no older than `max_age_s`."""
    age = auth_age_s(state, now)
    return bool(state.get("auth_ok")) and age is not None and age <= max_age_s


def skew_within_limit(state: dict[str, Any], limit_ms: int) -> bool:
    skew = state.get("clock_skew_ms")
    return skew is None or abs(int(skew)) <= limit_ms


def precondition_problems(conn: psycopg.Connection, state: dict[str, Any], now: datetime) -> list[str]:
    """Why live cannot be enabled right now (empty when it can)."""
    problems = []
    if kill.is_killed(conn):
        problems.append("the kill switch is on; reset it first")
    if not state.get("credentials_present"):
        problems.append("the exchange process has no credentials loaded")
    if not auth_fresh(state, now):
        problems.append(f"the exchange has not confirmed its credentials within the last {AUTH_MAX_AGE_S // 60} minutes")
    if not skew_within_limit(state, skew_limit_ms(conn)):
        problems.append(f"the exchange clock skew {state.get('clock_skew_ms')} ms is over the limit")
    return problems


def auto_kill_reasons(conn: psycopg.Connection) -> list[str]:
    """Reasons of the `auto_kill` audit rows since the last RESUME, oldest first."""
    rows = conn.execute(
        """
        SELECT after FROM audit_log
         WHERE action = 'auto_kill'
           AND id > COALESCE((SELECT max(id) FROM audit_log WHERE action = 'kill_reset'), 0)
         ORDER BY id
        """
    ).fetchall()
    reasons: list[str] = []
    for row in rows:
        reason = (row["after"] or {}).get("reason") if isinstance(row["after"], dict) else None
        if reason and reason not in reasons:
            reasons.append(str(reason))
    return reasons


def live_state(conn: psycopg.Connection, now: datetime | None = None) -> dict[str, Any]:
    """Everything the live switch UI shows (docs/LIVE.md "Dashboard additions")."""
    now = now or server_now(conn)
    state = read_state(conn, now)
    return {
        "live_enabled": is_live_enabled(conn),
        "live_enabled_at": state.get("live_enabled_at"),
        "live_enabled_by": state.get("live_enabled_by"),
        "credentials_present": bool(state.get("credentials_present")),
        "auth_ok": bool(state.get("auth_ok")),
        "auth_checked_at": state.get("auth_checked_at"),
        "auth_age_s": auth_age_s(state, now),
        "auth_failures": int(state.get("auth_failures") or 0),
        "balance_cents": state.get("balance_cents"),
        "buying_power_cents": state.get("buying_power_cents"),
        "balance_checked_at": state.get("balance_checked_at"),
        "clock_skew_ms": state.get("clock_skew_ms"),
        "last_auth_error": state.get("last_auth_error"),
        "open_orders_checked_at": state.get("open_orders_checked_at"),
        "killed": kill.is_killed(conn),
        "auto_kill_reasons": auto_kill_reasons(conn),
        "expected_phrase": expected_phrase(conn, now),
        "problems": precondition_problems(conn, state, now),
    }


def enable_live(conn: psycopg.Connection, actor: str, confirm: str, now: datetime | None = None) -> dict[str, Any]:
    """Turn live on: 400 unless `confirm` is exactly today's phrase, 409 when a
    precondition fails. Idempotent when already on (a second audit row records the
    repeated confirmation). Serialised with the kill through the live approval lock."""
    now = now or server_now(conn)
    phrase = expected_phrase(conn, now)
    if not isinstance(confirm, str) or confirm != phrase:
        raise BadRequest(f'confirmation must be exactly "{phrase}"')
    kill.approval_lock(conn, "live")
    state = read_state(conn, now)
    problems = precondition_problems(conn, state, now)
    if problems:
        raise Conflict("live trading cannot be enabled: " + "; ".join(problems))
    was_on = is_live_enabled(conn)
    if not was_on:
        from psycopg.types.json import Jsonb

        conn.execute("UPDATE settings SET value = %s, updated_at = now() WHERE key = 'live_enabled'", (Jsonb(True),))
        set_live_enabled(conn, now, actor)
    add_audit(
        conn, "live_on", "live_enabled", actor, {"live_enabled": was_on},
        {"live_enabled": True, "balance_cents": state.get("balance_cents"), "clock_skew_ms": state.get("clock_skew_ms")},
        confirmation_text=confirm,
    )
    return live_state(conn, now)


def disable_live(conn: psycopg.Connection, actor: str, reason: str = "owner") -> dict[str, Any]:
    """Live off now, through kill.live_off (halts, cancels, audit `live_off`)."""
    result = kill.live_off(conn, actor, reason)
    out = live_state(conn)
    out["live_off"] = result
    return out


def live_platform(conn: psycopg.Connection) -> str | None:
    """The market platform live orders may be placed on: the current `market_source`,
    never `polymarket_clob` (a price-only source). Markets left over from another
    source (their platform differs) are never traded live."""
    source = str(get_setting(conn, "market_source", "sim") or "sim")
    return None if source == "polymarket_clob" else source


def market_platform_problem(conn: psycopg.Connection, platform: str | None) -> str | None:
    """Why a market of `platform` cannot take a live order right now (None when it can)."""
    wanted = live_platform(conn)
    if wanted is None:
        return "market_source polymarket_clob is a price source only; set it to polymarket_us for live orders"
    if platform != wanted:
        return f"the market is on {platform!r}, not on the current market source {wanted!r}"
    return None


def live_gate(conn: psycopg.Connection, model: dict[str, Any], game_id: str | None = None) -> None:
    """The three live preconditions of a live assignment; 409 naming the first that
    fails. With `game_id`, the game must also have a confirmed market on the live
    platform (the current market source), so stale markets of another source and
    the price-only CLOB source never receive live orders."""
    if not is_live_enabled(conn):
        raise Conflict("live trading is disabled (live_enabled is false)")
    if model["status"] != "live_eligible":
        raise Conflict("the model's lineage is not live_eligible")
    state = conn.execute("SELECT auth_ok FROM exchange_state WHERE id").fetchone()
    if state is None or not state["auth_ok"]:
        raise Conflict("the exchange has not confirmed its credentials (auth_ok is false)")
    if game_id is not None:
        wanted = live_platform(conn)
        if wanted is None:
            raise Conflict(market_platform_problem(conn, None) or "no live platform")
        row = conn.execute(
            "SELECT 1 FROM markets WHERE game_id = %s AND platform = %s AND mapping_confirmed AND status = 'open' LIMIT 1",
            (game_id, wanted),
        ).fetchone()
        if row is None:
            raise Conflict(f"game {game_id} has no confirmed open market on the live platform {wanted!r}")
