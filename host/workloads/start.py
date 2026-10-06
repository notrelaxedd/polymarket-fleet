"""POST /api/v1/machines/{id}/start: the one place a container gets its run token and secrets."""
from __future__ import annotations

from typing import Any

import psycopg

from host.errors import Conflict
from host.workloads import assign, secrets as wl_secrets, tokens


def start(conn: psycopg.Connection, machine: dict[str, Any], epoch: int) -> dict[str, Any]:
    """Mint a new run token (the old one stops working) and return it with the container
    secrets: {"run_token", "secrets"}. 409 when `epoch` is not current, nothing is
    assigned, the machine is disabled or draining, or the image is not published.

    The caller must send `Cache-Control: no-store` and must never log the result.
    """
    row = conn.execute("SELECT * FROM workload_assignments WHERE machine_id = %s FOR UPDATE", (machine["id"],)).fetchone()
    if row is None or row["workload"] is None or row["epoch"] != epoch:
        raise Conflict("epoch is not current or nothing is assigned")
    current = conn.execute("SELECT * FROM machines WHERE id = %s", (machine["id"],)).fetchone()
    if assign.desired_run(conn, current, row) is None:
        raise Conflict("nothing should run on this machine now (disabled, draining or image not published)")
    values = wl_secrets.secrets_for_machine(conn, machine["id"], epoch)
    token = tokens.mint_run_token(conn, machine["id"], epoch)
    conn.execute(
        "UPDATE workload_assignments SET state = 'starting', updated_at = now()"
        " WHERE machine_id = %s AND state IN ('pending', 'stopped', 'failed')",
        (machine["id"],),
    )
    return {"run_token": token, "secrets": values}
