"""Seed rows for the workloads pages (docs/workloads-design.md section 10), written with
plain SQL on the 0010 tables so it needs none of the host modules: workloads (a jobs-mode
one, a service, a heavy one with email approvals), machines in every state (a flash Pi, a
pinned live-trading box, an SSD mini PC, an offline HDD box, one with the native worker
running), assignments, jobs, logs, secrets (names only; the stored bytes are stand-ins,
never real ciphertext) and outbound actions.

`seed(url)` fills a throwaway database for tests/hw/screenshots.py style captures;
`seed_conn(conn)` is the same on an open connection (tests/test_wl_pages.py uses the
small `add_*` helpers directly). `touch(url)` keeps the online machines' heartbeats fresh.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from host.workloads.manifest import parse_manifest

DIGEST = "sha256:" + "ab12" * 16
SECRET_MARKER = b"stand-in-bytes-not-a-real-ciphertext"


def manifest(name: str, *, mode: str = "jobs", ram: int = 128, disk: int = 300, write_heavy: bool = False,
             container: tuple[str, ...] = (), host_only: tuple[str, ...] = (), actions: tuple[str, ...] = (),
             protocol: str = "workload-v1", description: str = "") -> dict[str, Any]:
    """A validated manifest as stored in `workloads.manifest` (Manifest.to_json())."""
    runtime: dict[str, Any] = {"mode": mode, "job_kinds": ["hello"] if mode == "jobs" else []}
    trading: dict[str, Any] = {}
    if protocol == "fleet-worker":
        runtime.update({"network": "host", "uts_host": True, "state_volume": True})
        trading = {"can_trade": True}
    data = {
        "schema": 1, "name": name, "description": description or f"The {name} workload", "image": f"fleet/{name}",
        "protocol": protocol, "resources": {"min_ram_mb": ram, "min_disk_mb": disk, "write_heavy": write_heavy, "memory_max_mb": 256},
        "runtime": runtime, "secrets": {"container": list(container), "host_only": list(host_only)},
        "outbound": {"actions": list(actions)}, "trading": trading,
    }
    return parse_manifest(data, name).to_json()


def add_workload(conn: psycopg.Connection, name: str, *, published: bool = True, size_mb: int | None = 120,
                 enabled: bool = True, **kw: Any) -> dict[str, Any]:
    data = manifest(name, **kw)
    conn.execute(
        "INSERT INTO workloads (name, manifest, image_repo, image_digest, image_size_mb, enabled) VALUES (%s, %s, %s, %s, %s, %s)",
        (name, Jsonb(data), data["image"], DIGEST if published else None, size_mb if published else None, enabled),
    )
    return data


def add_machine(conn: psycopg.Connection, name: str, *, ram_mb: int | None = 3934, disk_mb: int | None = 16_000,
                free_mb: int | None = 9_000, disk_type: str = "flash", override: str | None = None, online: bool = True,
                docker_ok: bool = True, enabled: bool = True, native: str = "absent", pinned_reason: str | None = None,
                cpu_pct: float = 4.0, worker_id: str | None = None) -> str:
    """One machine with its (empty) assignment row; returns its id."""
    machine_id = "m_" + uuid.uuid4().hex[:6]
    conn.execute(
        """
        INSERT INTO machines (id, name, token_hash, hostname, docker_ok, docker_version, agent_version, arch, cpu_count, cpu_pct,
                              ram_total_mb, ram_used_mb, disk_type_detected, disk_type_override, disk_size_mb, disk_free_mb,
                              native_polymarket, polymarket_worker_id, pinned, pinned_reason, enabled, last_heartbeat_at)
        VALUES (%s, %s, 'not-a-real-hash', %s, %s, '26.1.5', 'a1b2c3d4e5f6', 'aarch64', 4, %s, %s, 900, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, CASE WHEN %s THEN now() - interval '2 seconds' ELSE now() - interval '2 hours' END)
        """,
        (machine_id, name, name + ".lan", docker_ok, cpu_pct, ram_mb, disk_type, override, disk_mb, free_mb, native, worker_id,
         pinned_reason is not None, pinned_reason, enabled, online),
    )
    conn.execute("INSERT INTO workload_assignments (machine_id) VALUES (%s)", (machine_id,))
    return machine_id


def assign_row(conn: psycopg.Connection, machine_id: str, workload: str | None, state: str = "running", **cols: Any) -> None:
    sets = {"workload": workload, "state": state, "epoch": 2, "acked_epoch": 2, "started_at": None, **cols}
    names = ", ".join(f"{k} = %s" for k in sets)
    conn.execute(f"UPDATE workload_assignments SET {names} WHERE machine_id = %s", (*sets.values(), machine_id))
    if state == "running":
        conn.execute("UPDATE workload_assignments SET started_at = now() - interval '25 minutes', cpu_pct = 3.5, mem_mb = 41 WHERE machine_id = %s", (machine_id,))


def add_job(conn: psycopg.Connection, workload: str, status: str, *, kind: str = "hello", machine_id: str | None = None,
            params: dict[str, Any] | None = None, progress: float = 0.0, error: str | None = None,
            result: dict[str, Any] | None = None) -> str:
    leased = status in ("leased", "cancel_requested")
    row = conn.execute(
        """
        INSERT INTO workload_jobs (workload, kind, status, params, progress, error, result, lease_machine_id, lease_epoch,
                                   lease_token, lease_expires_at, finished_at, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN 2 END, CASE WHEN %s THEN gen_random_uuid() END,
                CASE WHEN %s THEN now() + interval '30 seconds' END,
                CASE WHEN %s THEN now() - interval '5 minutes' END, now() - interval '10 minutes')
        RETURNING id
        """,
        (workload, kind, status, Jsonb(params or {}), progress, error, Jsonb(result) if result is not None else None,
         machine_id if leased else None, leased, leased, leased, status in ("succeeded", "failed", "cancelled")),
    ).fetchone()
    return str(row["id"])


def add_outbound(conn: psycopg.Connection, workload: str, kind: str, payload: dict[str, Any], status: str = "pending",
                 machine_id: str | None = None, error: str | None = None) -> str:
    row = conn.execute(
        """
        INSERT INTO outbound_actions (workload, machine_id, kind, payload, dedupe_key, status, error, decided_by, decided_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, CASE WHEN %s <> 'pending' THEN 'owner@example.com' END,
                CASE WHEN %s <> 'pending' THEN now() - interval '3 minutes' END)
        RETURNING id
        """,
        (workload, machine_id, kind, Jsonb(payload), uuid.uuid4().hex, status, error, status, status),
    ).fetchone()
    return str(row["id"])


def add_secret(conn: psycopg.Connection, workload: str, name: str, scope: str = "container") -> None:
    """A stored secret row with stand-in bytes (never a real value)."""
    conn.execute(
        "INSERT INTO workload_secrets (workload, name, scope, nonce, ciphertext, updated_by) VALUES (%s, %s, %s, %s, %s, 'owner@example.com')",
        (workload, name, scope, b"n" * 24, SECRET_MARKER),
    )


def add_logs(conn: psycopg.Connection, machine_id: str, workload: str | None, count: int, stream: str = "stdout", prefix: str = "line") -> None:
    conn.execute(
        """
        INSERT INTO machine_logs (machine_id, workload, ts, stream, line)
        SELECT %s, %s, now() - make_interval(secs => (%s - n)), %s, %s || ' ' || n FROM generate_series(1, %s) AS n
        """,
        (machine_id, workload, count, stream, prefix, count),
    )


def seed_conn(conn: psycopg.Connection) -> dict[str, str]:
    """A small fleet in every state; returns the ids the captures and tests use."""
    add_workload(conn, "polymarket", mode="service", ram=3000, disk=2048, protocol="fleet-worker",
                 container=("FLEET_ENROLL_TOKEN",), description="Polymarket trading worker")
    add_workload(conn, "hello", container=("HELLO_GREETING",), actions=("log",), description="Says hello")
    add_workload(conn, "demo-site", mode="service", ram=8192, disk=4096, write_heavy=True, host_only=("SMTP_URL", "EMAIL_FROM"),
                 actions=("email",), description="Builds and previews demo sites")
    add_workload(conn, "archive", ram=512, disk=20_000, write_heavy=True, published=False, description="Market data archive")
    pi1 = add_machine(conn, "pi-1")
    pi2 = add_machine(conn, "pi-2", ram_mb=7936, disk_mb=120_000, disk_type="ssd", pinned_reason="live trading")
    mini = add_machine(conn, "mini-1", ram_mb=15_900, disk_mb=250_000, free_mb=180_000, disk_type="ssd", cpu_pct=22.0)
    old = add_machine(conn, "old-box", ram_mb=2000, disk_mb=500_000, disk_type="hdd", online=False)
    native = add_machine(conn, "native-box", native="active")
    assign_row(conn, pi1, "hello")
    assign_row(conn, pi2, "polymarket")
    assign_row(conn, mini, "demo-site", state="starting")
    add_secret(conn, "hello", "HELLO_GREETING")
    add_secret(conn, "demo-site", "SMTP_URL", "host_only")
    running = add_job(conn, "hello", "leased", machine_id=pi1, params={"name": "Ada", "steps": 5}, progress=0.4)
    add_job(conn, "hello", "queued", params={"name": "Grace"})
    add_job(conn, "hello", "succeeded", machine_id=pi1, params={"name": "Linus"}, progress=1.0, result={"greeting": "Hello, Linus!"})
    add_job(conn, "hello", "failed", machine_id=pi1, error="scratch full", progress=0.6)
    add_logs(conn, pi1, "hello", 60)
    add_logs(conn, pi1, None, 3, "agent", "agent")
    mail = add_outbound(conn, "demo-site", "email", {"to": "client@example.com", "subject": "Your demo site is ready for review", "body": "Hi,\nThe preview is up."}, machine_id=mini)
    add_outbound(conn, "hello", "log", {"message": "greeted Ada"}, machine_id=pi1)
    add_outbound(conn, "demo-site", "email", {"to": "x@example.com", "subject": "Sent one"}, status="sent")
    add_outbound(conn, "demo-site", "email", {"to": "y@example.com", "subject": "Bounced"}, status="failed", error="550 mailbox full")
    add_outbound(conn, "hello", "log", {"message": "no thanks"}, status="rejected")
    return {"pi1": pi1, "pi2": pi2, "mini": mini, "old": old, "native": native, "job": running, "mail": mail}


def seed(url: str) -> dict[str, str]:
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        return seed_conn(conn)


def touch(url: str) -> None:
    """Keep the online machines online while captures run."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("UPDATE machines SET last_heartbeat_at = now() - interval '2 seconds' WHERE name NOT IN ('old-box')")

