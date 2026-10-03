"""The fleet-wide kill switch: set, reset (with confirmation) and read.

The flag lives in settings.kill_switch and is mirrored to workers in every register
and heartbeat reply. It stops trade claims (and, from step 4, order approvals); batch
roles keep working.
"""
from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from host.errors import BadRequest
from host.events import add_audit
from host.settings import get_setting

RESET_CONFIRMATION = "RESUME"


def is_killed(conn: psycopg.Connection) -> bool:
    """True when settings.kill_switch is the JSON boolean true."""
    return get_setting(conn, "kill_switch", False) is True


def _lock(conn: psycopg.Connection) -> bool:
    """Lock the kill_switch row for this transaction and return its current value.

    A press that races a reset (or the other way round) waits for the other
    transaction to commit and then sees what it wrote, so the last press wins and
    the audit order matches the final flag.
    """
    row = conn.execute("SELECT value FROM settings WHERE key = 'kill_switch' FOR UPDATE").fetchone()
    return row is not None and row["value"] is True


def _write(conn: psycopg.Connection, value: bool) -> None:
    conn.execute(
        "UPDATE settings SET value = %s, updated_at = now() WHERE key = 'kill_switch'", (Jsonb(value),)
    )


def set_kill(conn: psycopg.Connection, actor: str | None) -> bool:
    """Raise the kill flag. Idempotent; every press writes an audit row `kill`.

    Returns True when the flag was off before this call.
    """
    before = _lock(conn)
    if not before:
        _write(conn, True)
    add_audit(conn, "kill", "kill_switch", actor, {"kill_switch": before}, {"kill_switch": True})
    return not before


def reset_kill(conn: psycopg.Connection, actor: str | None, confirm: str | None) -> None:
    """Clear the kill flag; `confirm` must be exactly RESUME, otherwise 400 and no change."""
    if confirm != RESET_CONFIRMATION:
        raise BadRequest(f'confirmation must be exactly "{RESET_CONFIRMATION}"')
    before = _lock(conn)
    if before:
        _write(conn, False)
    add_audit(
        conn, "kill_reset", "kill_switch", actor, {"kill_switch": before}, {"kill_switch": False},
        confirmation_text=confirm,
    )
