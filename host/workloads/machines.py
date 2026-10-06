"""Machines: enrollment, token rotation, heartbeat, and the link to a Polymarket worker."""
from __future__ import annotations

import re
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from host.auth import hash_token, mint_token
from host.errors import BadRequest, NotFound, Unauthorized
from host.events import add_audit
from host.heartbeat import server_time
from host.settings import get_int_setting
from host.workloads import agent_bundle, assign, secrets as wl_secrets
from host.workloads.machine_specs import NATIVE_STATES, clip_logs, container_block, spec_columns
from host.workloads.placement import DISK_TYPES
from host.workloads.registry import get_workload

ENROLL_TTL_SECONDS = 3600
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHORT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_SPEC_COLUMNS = (
    "cpu_pct", "ram_used_mb", "ram_total_mb", "cpu_count", "arch", "disk_size_mb", "disk_free_mb",
    "docker_root", "docker_ok", "docker_version", "disk_type_detected",
)
SECRET_COLUMNS = ("token_hash", "prev_token_hash")


def public_machine(row: dict[str, Any]) -> dict[str, Any]:
    """A machine row without its token hashes."""
    return {k: v for k, v in row.items() if k not in SECRET_COLUMNS}


def new_machine_id() -> str:
    """Machine ids look like m_3f9a1c."""
    return "m_" + _secrets.token_hex(3)


def create_enroll_token(conn: psycopg.Connection, ttl_seconds: int = ENROLL_TTL_SECONDS) -> dict[str, Any]:
    """Mint a single-use machine enroll token: {"token", "expires_at"}."""
    token = mint_token()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    conn.execute(
        "INSERT INTO machine_enroll_tokens (token_hash, expires_at) VALUES (%s, %s)", (hash_token(token), expires_at)
    )
    return {"token": token, "expires_at": expires_at}


def _clean(body: dict[str, Any], key: str, pattern: re.Pattern[str]) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.match(value):
        raise BadRequest(f"{key} is not valid")
    return value


def _apply_specs(conn: psycopg.Connection, machine_id: str, specs: Any) -> None:
    cols = spec_columns(specs)
    if not cols:
        return
    sets = ", ".join(f"{c} = %({c})s" for c in cols)
    conn.execute(f"UPDATE machines SET {sets} WHERE id = %(id)s", {**cols, "id": machine_id})


def _enroll(conn: psycopg.Connection, body: dict[str, Any], peer_ip: str | None) -> tuple[dict[str, Any], str]:
    token = str(body.get("enroll_token", ""))
    row = conn.execute(
        "SELECT token_hash FROM machine_enroll_tokens"
        " WHERE token_hash = %s AND used_at IS NULL AND expires_at > now() FOR UPDATE",
        (hash_token(token),),
    ).fetchone()
    if row is None:
        raise Unauthorized("invalid, used or expired enroll token")
    machine_id = new_machine_id()
    while conn.execute("SELECT 1 FROM machines WHERE id = %s", (machine_id,)).fetchone():
        machine_id = new_machine_id()
    plain = mint_token()
    hostname, name = _clean(body, "hostname", NAME_RE), _clean(body, "name", NAME_RE)
    machine = conn.execute(
        """
        INSERT INTO machines (id, name, token_hash, hostname, boot_id, remote_ip, agent_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (machine_id, name or hostname or machine_id, hash_token(plain), hostname, _clean(body, "boot_id", SHORT_RE),
         peer_ip, _clean(body, "agent_version", SHORT_RE)),
    ).fetchone()
    conn.execute("INSERT INTO workload_assignments (machine_id) VALUES (%s)", (machine_id,))
    conn.execute(
        "UPDATE machine_enroll_tokens SET used_at = now(), used_by_machine_id = %s WHERE token_hash = %s",
        (machine_id, row["token_hash"]),
    )
    add_audit(conn, "machine_enrolled", machine_id, machine_id, None, {"name": machine["name"], "hostname": hostname}, peer_ip)
    return machine, plain


def _reregister(conn: psycopg.Connection, body: dict[str, Any], peer_ip: str | None) -> tuple[dict[str, Any], str]:
    machine_id, token = str(body.get("machine_id", "")), str(body.get("machine_token", ""))
    row = conn.execute("SELECT * FROM machines WHERE id = %s FOR UPDATE", (machine_id,)).fetchone()
    presented = hash_token(token)
    if row is None or not (_same(row["token_hash"], presented) or _same(row["prev_token_hash"], presented)):
        raise Unauthorized("invalid machine token")
    plain = mint_token()
    machine = conn.execute(
        """
        UPDATE machines SET token_hash = %s, prev_token_hash = %s, hostname = COALESCE(%s, hostname),
               boot_id = COALESCE(%s, boot_id), remote_ip = %s, agent_version = COALESCE(%s, agent_version)
         WHERE id = %s RETURNING *
        """,
        (hash_token(plain), presented, _clean(body, "hostname", NAME_RE), _clean(body, "boot_id", SHORT_RE),
         peer_ip, _clean(body, "agent_version", SHORT_RE), machine_id),
    ).fetchone()
    return machine, plain


def _same(stored: str | None, presented: str) -> bool:
    return stored is not None and _secrets.compare_digest(stored, presented)


def register(conn: psycopg.Connection, body: dict[str, Any], peer_ip: str | None) -> dict[str, Any]:
    """Enroll (enroll_token) or re-register (machine_id + machine_token); always returns a new token.

    Re-register accepts the current or the previous token, so a supervisor that never saw
    the reply can retry; the first heartbeat with the current token clears the previous.
    """
    if body.get("enroll_token"):
        machine, plain = _enroll(conn, body, peer_ip)
    elif body.get("machine_id") and body.get("machine_token"):
        machine, plain = _reregister(conn, body, peer_ip)
    else:
        raise Unauthorized("enroll_token or machine_id + machine_token required")
    _apply_specs(conn, machine["id"], body.get("specs"))
    if body.get("native_polymarket") in NATIVE_STATES:
        conn.execute("UPDATE machines SET native_polymarket = %s WHERE id = %s", (body["native_polymarket"], machine["id"]))
    machine = conn.execute("SELECT * FROM machines WHERE id = %s", (machine["id"],)).fetchone()
    link_polymarket_worker(conn, machine)
    return {
        "machine_id": machine["id"], "machine_token": plain,
        "heartbeat_seconds": get_int_setting(conn, "heartbeat_seconds", 5),
        "server_time": server_time(conn), "agent_version": agent_bundle.current_version(),
    }


def verify_machine(conn: psycopg.Connection, machine_id: str, token: str) -> dict[str, Any]:
    """The machine row when `token` is its current token, else 401."""
    row = conn.execute("SELECT * FROM machines WHERE id = %s", (machine_id,)).fetchone()
    if row is None or not _same(row["token_hash"], hash_token(token)):
        raise Unauthorized("invalid machine token")
    return row


def link_polymarket_worker(conn: psycopg.Connection, machine: dict[str, Any]) -> str | None:
    """Link the machine to the one Polymarket worker sharing its boot_id (the container
    shares the kernel). An ambiguous or missing match leaves the link as it is."""
    current = machine.get("polymarket_worker_id")
    if not machine.get("boot_id"):
        return current
    rows = conn.execute("SELECT id FROM workers WHERE boot_id = %s LIMIT 2", (machine["boot_id"],)).fetchall()
    if len(rows) != 1:
        return current
    if rows[0]["id"] != current:
        conn.execute("UPDATE machines SET polymarket_worker_id = %s WHERE id = %s", (rows[0]["id"], machine["id"]))
    return rows[0]["id"]


def refresh_links(conn: psycopg.Connection) -> int:
    """Re-run the boot_id link for every machine; returns how many links changed."""
    changed = 0
    for machine in conn.execute("SELECT * FROM machines WHERE boot_id IS NOT NULL").fetchall():
        if link_polymarket_worker(conn, machine) != machine["polymarket_worker_id"]:
            changed += 1
    return changed


def get_machine(conn: psycopg.Connection, machine_id: str, for_update: bool = False) -> dict[str, Any]:
    """One machines row; 404 when unknown."""
    row = conn.execute("SELECT * FROM machines WHERE id = %s" + (" FOR UPDATE" if for_update else ""), (machine_id,)).fetchone()
    if row is None:
        raise NotFound(f"unknown machine: {machine_id!r}")
    return row


def set_disk_type(conn: psycopg.Connection, machine_id: str, disk_type: str | None, actor: str | None,
                  ip: str | None) -> dict[str, Any]:
    """Set (or clear with None) the owner's disk type override; audited."""
    if disk_type is not None and disk_type not in DISK_TYPES:
        raise BadRequest(f"disk_type must be one of {DISK_TYPES} or null")
    before = get_machine(conn, machine_id, for_update=True)
    row = conn.execute(
        "UPDATE machines SET disk_type_override = %s WHERE id = %s RETURNING *", (disk_type, machine_id)
    ).fetchone()
    add_audit(conn, "machine_disk_type", machine_id, actor, {"disk_type_override": before["disk_type_override"]},
              {"disk_type_override": disk_type}, ip)
    return row


def set_enabled(conn: psycopg.Connection, machine_id: str, enabled: bool, actor: str | None, ip: str | None) -> dict[str, Any]:
    """Enable or disable a machine (disabled: its run block is null); audited."""
    before = get_machine(conn, machine_id, for_update=True)
    row = conn.execute("UPDATE machines SET enabled = %s WHERE id = %s RETURNING *", (bool(enabled), machine_id)).fetchone()
    add_audit(conn, "machine_enabled", machine_id, actor, {"enabled": before["enabled"]}, {"enabled": row["enabled"]}, ip)
    return row


def _derive_state(a: dict[str, Any], c: dict[str, Any] | None) -> str:
    """The assignment state implied by what the supervisor reports at the current epoch."""
    if a["state"] == "draining":
        return "draining"
    if a["workload"] is None:
        return "stopped"
    if c is None or c["workload"] != a["workload"] or c["epoch"] != a["epoch"]:
        return "pending"
    return {"running": "running", "starting": "starting", "failed": "failed", "exited": "starting"}.get(c["state"] or "", "pending")


def _store_container(conn: psycopg.Connection, a: dict[str, Any], c: dict[str, Any] | None, acked: Any) -> dict[str, Any]:
    """Step 2: acked_epoch, container block and derived state on the assignment row."""
    mine = c is not None and c["workload"] == a["workload"] and c["epoch"] == a["epoch"]
    state = _derive_state(a, c)
    ack = acked if isinstance(acked, int) and not isinstance(acked, bool) else None
    return conn.execute(
        """
        UPDATE workload_assignments SET
               acked_epoch = GREATEST(acked_epoch, LEAST(epoch, COALESCE(%(ack)s, acked_epoch))),
               state = %(state)s,
               container_id = %(cid)s, cpu_pct = %(cpu)s, mem_mb = %(mem)s,
               image_digest_running = COALESCE(%(digest)s, image_digest_running),
               started_at = CASE WHEN %(mine)s THEN COALESCE(%(started)s, started_at) ELSE started_at END,
               last_exit_code = CASE WHEN %(mine)s THEN %(exit)s ELSE last_exit_code END,
               restarts = CASE WHEN %(mine)s THEN %(restarts)s ELSE restarts END,
               last_error = CASE WHEN %(mine)s THEN %(err)s ELSE last_error END, updated_at = now()
         WHERE machine_id = %(mid)s RETURNING *
        """,
        {
            "ack": ack, "state": state, "mine": mine, "mid": a["machine_id"],
            "cid": c["container_id"] if mine else None, "cpu": c["cpu_pct"] if mine else None,
            "mem": c["mem_mb"] if mine else None, "digest": c["image_digest"] if mine else None,
            "started": c["started_at"] if mine else None, "exit": c["exit_code"] if mine else None,
            "restarts": c["restarts"] if mine else 0, "err": c["error"] if mine else None,
        },
    ).fetchone()


def _store_logs(conn: psycopg.Connection, machine_id: str, workload: str | None, logs: Any) -> None:
    rows = clip_logs(logs)
    if rows:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO machine_logs (machine_id, workload, ts, stream, line) VALUES (%s, %s, COALESCE(%s, now()), %s, %s)",
                [(machine_id, workload, r["ts"], r["stream"], r["line"]) for r in rows],
            )


def _keep_images(conn: psycopg.Connection, a: dict[str, Any]) -> list[str]:
    keep: list[str] = []
    if a["workload"]:
        current = get_workload(conn, a["workload"])["image_digest"]
        if current:
            keep.append(current)
    if a["image_digest_running"] and a["image_digest_running"] not in keep:
        keep.append(a["image_digest_running"])
    return keep


def heartbeat(conn: psycopg.Connection, machine: dict[str, Any], body: dict[str, Any], peer_ip: str | None) -> dict[str, Any]:
    """One supervisor heartbeat, one transaction (section 5.1): store, derive, answer."""
    row = conn.execute("SELECT * FROM machines WHERE id = %s FOR UPDATE", (machine["id"],)).fetchone()
    if row is None or row["token_hash"] != machine["token_hash"]:
        raise Unauthorized("invalid machine token")
    cleanup = body.get("cleanup") if isinstance(body.get("cleanup"), dict) else {}
    native = body.get("native_polymarket")
    conn.execute(
        """
        UPDATE machines SET last_heartbeat_at = now(), prev_token_hash = NULL, remote_ip = COALESCE(%s, remote_ip),
               native_polymarket = COALESCE(%s, native_polymarket),
               low_disk = COALESCE(%s, low_disk), agent_version = COALESCE(%s, agent_version)
         WHERE id = %s
        """,
        (peer_ip, native if native in NATIVE_STATES else None,
         cleanup.get("low_disk") if isinstance(cleanup.get("low_disk"), bool) else None,
         body.get("agent_version") if isinstance(body.get("agent_version"), str) and SHORT_RE.match(body["agent_version"]) else None,
         machine["id"]),
    )
    _apply_specs(conn, machine["id"], body.get("specs"))
    assignment = conn.execute("SELECT * FROM workload_assignments WHERE machine_id = %s FOR UPDATE", (machine["id"],)).fetchone()
    if assignment is None:
        assignment = conn.execute("INSERT INTO workload_assignments (machine_id) VALUES (%s) RETURNING *", (machine["id"],)).fetchone()
    assignment = _store_container(conn, assignment, container_block(body.get("container")), body.get("acked_epoch"))
    container = body.get("container") if isinstance(body.get("container"), dict) else None
    _store_logs(conn, machine["id"], (container or {}).get("workload") or assignment["workload"], body.get("logs"))
    current = conn.execute("SELECT * FROM machines WHERE id = %s", (machine["id"],)).fetchone()
    if current["polymarket_worker_id"] is None:
        link_polymarket_worker(conn, current)
    workload = assignment["workload"]
    return {
        "epoch": assignment["epoch"], "workload": workload,
        "run": assign.desired_run(conn, current, assignment),
        "secrets_version": wl_secrets.secrets_version(conn, workload) if workload else None,
        "keep_images": _keep_images(conn, assignment),
        "agent_version": agent_bundle.current_version(), "server_time": server_time(conn),
        "heartbeat_seconds": get_int_setting(conn, "heartbeat_seconds", 5),
    }
