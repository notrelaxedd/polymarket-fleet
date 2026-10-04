"""The single `exchange_state` row: heartbeat, market source, last error and (step 5)
the authenticated session: auth result, balance and buying power, clock skew,
credentials presence, the open-order audit stamp and the live switch stamp."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from host.exchange.adapters.base import utcnow

STALE_AFTER_S = 15


def heartbeat(conn: psycopg.Connection, source: str, error: str | None = None, now: datetime | None = None) -> None:
    """Stamp the heartbeat (every 5 s in the loop); `error` replaces last_error,
    None clears it."""
    conn.execute(
        """
        UPDATE exchange_state SET heartbeat_at = %s, market_source = %s, last_error = %s, updated_at = now()
         WHERE id = true
        """,
        (now or utcnow(), source, None if error is None else str(error)[:2000]),
    )


def read_state(conn: psycopg.Connection, now: datetime | None = None) -> dict[str, Any]:
    """The row plus `heartbeat_age_s` and `alive` (heartbeat within 15 s)."""
    row = conn.execute("SELECT * FROM exchange_state WHERE id = true").fetchone()
    now = now or utcnow()
    out: dict[str, Any] = dict(row) if row else {"heartbeat_at": None, "market_source": None, "last_error": None}
    beat = out.get("heartbeat_at")
    age = None if beat is None else max(0.0, (now - beat).total_seconds())
    out["heartbeat_age_s"] = age
    out["alive"] = age is not None and age <= STALE_AFTER_S
    return out


def set_auth(conn: psycopg.Connection, ok: bool, balance_cents: int | None = None, buying_power_cents: int | None = None) -> None:
    """The gateway's auth and balance check (database clock)."""
    conn.execute(
        """
        UPDATE exchange_state SET auth_ok = %s, auth_checked_at = now(), balance_cents = %s,
               buying_power_cents = %s, balance_checked_at = now(), updated_at = now()
         WHERE id = true
        """,
        (ok, balance_cents, buying_power_cents),
    )


def record_auth_ok(
    conn: psycopg.Connection, now: datetime, balance_cents: int | None, buying_power_cents: int | None,
    skew_ms: int | None,
) -> None:
    """A successful auth probe: auth_ok, balances, skew, failures back to zero."""
    conn.execute(
        """
        UPDATE exchange_state SET auth_ok = true, auth_checked_at = %s, balance_cents = %s, buying_power_cents = %s,
               balance_checked_at = %s, clock_skew_ms = %s, auth_failures = 0, last_auth_error = NULL,
               credentials_present = true, updated_at = now()
         WHERE id = true
        """,
        (now, balance_cents, buying_power_cents, now, skew_ms),
    )


def record_auth_failure(conn: psycopg.Connection, now: datetime, error: str, creds_present: bool = True) -> int:
    """A failed auth probe: auth_ok false, failures + 1, the error text. Returns the
    new failure count."""
    row = conn.execute(
        """
        UPDATE exchange_state SET auth_ok = false, auth_checked_at = %s, auth_failures = auth_failures + 1,
               last_auth_error = %s, credentials_present = %s, updated_at = now()
         WHERE id = true RETURNING auth_failures
        """,
        (now, str(error)[:500], creds_present),
    ).fetchone()
    return int(row["auth_failures"]) if row else 0


def set_credentials_present(conn: psycopg.Connection, present: bool, error: str | None = None) -> None:
    """Without credentials there is no session: auth_ok false and failures cleared;
    `error` is the loader's secret-free message for a malformed secret (shown as
    the last auth error so the owner can tell it from a missing file)."""
    if present:
        conn.execute("UPDATE exchange_state SET credentials_present = true, updated_at = now() WHERE id = true")
        return
    conn.execute(
        """
        UPDATE exchange_state SET credentials_present = false, auth_ok = false, auth_failures = 0,
               last_auth_error = %s, updated_at = now()
         WHERE id = true
        """,
        (str(error)[:500] if error else "no credentials loaded",),
    )


def set_clock_skew(conn: psycopg.Connection, skew_ms: int | None) -> None:
    conn.execute("UPDATE exchange_state SET clock_skew_ms = %s, updated_at = now() WHERE id = true", (skew_ms,))


def set_open_orders_checked(conn: psycopg.Connection, now: datetime) -> None:
    conn.execute("UPDATE exchange_state SET open_orders_checked_at = %s, updated_at = now() WHERE id = true", (now,))


def set_live_enabled(conn: psycopg.Connection, at: datetime | None, by: str | None) -> None:
    """The live switch stamp (both NULL when live is off)."""
    conn.execute(
        "UPDATE exchange_state SET live_enabled_at = %s, live_enabled_by = %s, updated_at = now() WHERE id = true",
        (at, by),
    )
