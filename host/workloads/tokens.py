"""Run tokens: the bearer a workload container uses on /api/v1/wl/* (one per assignment epoch)."""
from __future__ import annotations

from typing import Any

import psycopg

from host.auth import hash_token, mint_token
from host.errors import Conflict, Unauthorized


def mint_run_token(conn: psycopg.Connection, machine_id: str, epoch: int) -> str:
    """A new run token for the machine's current epoch; the previous one stops working.

    Only the sha256 is stored. 409 when `epoch` is not the assignment's current epoch.
    """
    token = mint_token()
    row = conn.execute(
        """
        UPDATE workload_assignments SET run_token_hash = %s, updated_at = now()
         WHERE machine_id = %s AND epoch = %s AND workload IS NOT NULL
        RETURNING machine_id
        """,
        (hash_token(token), machine_id, epoch),
    ).fetchone()
    if row is None:
        raise Conflict("epoch is not current or nothing is assigned")
    return token


def run_scope(conn: psycopg.Connection, token: str) -> dict[str, Any]:
    """{machine_id, workload, epoch} for a live run token, else 401.

    401 when the hash is unknown (also any worker or machine token), the machine is
    disabled, or nothing is assigned any more. An assignment change clears the stored
    hash, so a token from an older epoch is simply unknown.
    """
    row = conn.execute(
        """
        SELECT a.machine_id, a.workload, a.epoch FROM workload_assignments a
          JOIN machines m ON m.id = a.machine_id
         WHERE a.run_token_hash = %s AND m.enabled AND a.workload IS NOT NULL
        """,
        (hash_token(token),),
    ).fetchone()
    if row is None:
        raise Unauthorized("invalid run token")
    return {"machine_id": row["machine_id"], "workload": row["workload"], "epoch": row["epoch"]}
