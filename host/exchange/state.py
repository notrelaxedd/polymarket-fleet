"""The single `exchange_state` row: heartbeat, market source and last error."""
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
    """Step 5 hook: the gateway's auth and balance check."""
    conn.execute(
        """
        UPDATE exchange_state SET auth_ok = %s, auth_checked_at = now(), balance_cents = %s,
               buying_power_cents = %s, balance_checked_at = now(), updated_at = now()
         WHERE id = true
        """,
        (ok, balance_cents, buying_power_cents),
    )
