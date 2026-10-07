"""Shared helpers for the workloads guardrail tests (docs/workloads-design.md).

Everything here is plain SQL against the 0010 schema plus the existing Polymarket
fixtures, so the helpers work without the workloads implementation. The only imports
of `host.workloads.*` are `manifest` (on the branch) and, lazily inside functions, the
contract functions a test asks for explicitly (`mint_run_token`).

Import what you need explicitly; fixtures (`secrets_key`, `no_secrets_key`) are meant
to be imported into a test module so pytest sees them.
"""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from host import auth
from host.api.app import create_app
from host.workloads.manifest import Manifest, parse_manifest
from tests.conftest import FakeWorker, TradeSetup, insert_worker, trade_setup

OWNER_LOGIN = "owner@example.com"
OWNER = {"Tailscale-User-Login": OWNER_LOGIN}
INTRUDER = {"Tailscale-User-Login": "intruder@example.com"}

# One key for the whole session: the tests that need a key set it through the fixture,
# the tests that need none delete it. Generated here so no real key is ever involved.
SECRETS_KEY = base64.b64encode(os.urandom(32)).decode()


# ------------------------------------------------------------------ manifests


def manifest_data(
    name: str = "hello",
    *,
    ram: int = 128,
    disk: int = 300,
    write_heavy: bool = False,
    memory_max_mb: int | None = None,
    memory_max_pct: int | None = None,
    mode: str = "jobs",
    kinds: list[str] | None = None,
    protocol: str = "workload-v1",
    container: tuple[str, ...] = (),
    host_only: tuple[str, ...] = (),
    actions: tuple[str, ...] = (),
    can_trade: bool = False,
    network: str = "bridge",
    uts_host: bool = False,
    state_volume: bool = False,
    nice: int = 0,
    description: str | None = None,
    image: str | None = None,
) -> dict[str, Any]:
    """A workload.toml as a decoded dict (the input of `parse_manifest`)."""
    resources: dict[str, Any] = {"min_ram_mb": ram, "min_disk_mb": disk, "write_heavy": write_heavy}
    if memory_max_mb is not None:
        resources["memory_max_mb"] = memory_max_mb
    if memory_max_pct is not None:
        resources["memory_max_pct"] = memory_max_pct
    runtime: dict[str, Any] = {
        "mode": mode,
        "network": network,
        "uts_host": uts_host,
        "state_volume": state_volume,
        "nice": nice,
    }
    if mode == "jobs":
        runtime["job_kinds"] = list(kinds) if kinds is not None else [name.replace("-", "_")]
    elif kinds:
        runtime["job_kinds"] = list(kinds)
    return {
        "schema": 1,
        "name": name,
        "description": description if description is not None else f"{name} test workload",
        "image": image or f"fleet/{name}",
        "protocol": protocol,
        "resources": resources,
        "runtime": runtime,
        "secrets": {"container": list(container), "host_only": list(host_only)},
        "outbound": {"actions": list(actions)},
        "trading": {"can_trade": can_trade},
    }


def make_manifest(name: str = "hello", **kw: Any) -> Manifest:
    """A validated Manifest (jobs mode, kind = the name with `-` as `_`, unless overridden)."""
    return parse_manifest(manifest_data(name, **kw), name)


def hello_manifest() -> Manifest:
    """The hello workload as designed: tiny, one container secret."""
    return make_manifest("hello", ram=128, disk=300, memory_max_mb=256, container=("HELLO_GREETING",))


def polymarket_manifest() -> Manifest:
    """The Polymarket workload as designed (section 2): needs 3000 MB RAM and 2048 MB disk."""
    return make_manifest(
        "polymarket", ram=3000, disk=2048, write_heavy=False, mode="service", protocol="fleet-worker",
        network="host", uts_host=True, state_volume=True, memory_max_pct=85, nice=5,
        container=("FLEET_ENROLL_TOKEN",), can_trade=True,
    )


def archive_manifest() -> Manifest:
    """A write-heavy market data archive that needs 2 GB of RAM."""
    return make_manifest("archive", ram=2048, disk=4096, write_heavy=True, mode="service", state_volume=True)


def demo_site_manifest() -> Manifest:
    """A demo-site factory (Node + headless Chrome) that needs 8 GB of RAM."""
    return make_manifest("demo-site", ram=8192, disk=4096, kinds=["demo_site"])


def plain_manifest(name: str, *kinds: str, **kw: Any) -> Manifest:
    """A jobs-mode workload with the given job kinds and no secrets (the queue tests)."""
    return make_manifest(name, kinds=list(kinds) or [name], **kw)


def load_real_manifest(name: str) -> Manifest:
    """workloads/<name>/workload.toml from the repository; skips the test when the folder is not there yet."""
    from pathlib import Path

    from host.workloads.manifest import load_manifest

    folder = Path(__file__).resolve().parent.parent / "workloads" / name
    if not (folder / "workload.toml").is_file():
        pytest.skip(f"workloads/{name} does not exist in this tree")
    return load_manifest(folder)


def machine_dict(**kw: Any) -> dict[str, Any]:
    """A `machines` row as a plain dict (for the pure placement checks): the realistic
    4 GB flash box with Docker, overridable per column."""
    row: dict[str, Any] = {
        "id": "m_000001", "name": "box", "docker_ok": True, "ram_total_mb": 3800, "disk_free_mb": 9000,
        "disk_size_mb": 15000, "disk_type_detected": "flash", "disk_type_override": None,
        "native_polymarket": "absent", "polymarket_worker_id": None, "pinned": False, "enabled": True,
    }
    row.update(kw)
    return row


def insert_workload(
    conn: psycopg.Connection,
    manifest: Manifest,
    *,
    published: bool = True,
    size_mb: int | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Insert (or replace) a workloads row from a manifest; published = has an image digest."""
    digest = "sha256:" + hashlib.sha256(manifest.name.encode()).hexdigest() if published else None
    if size_mb is None and published:
        size_mb = 100
    return conn.execute(
        """
        INSERT INTO workloads (name, manifest, image_repo, image_digest, image_size_mb, enabled)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (name) DO UPDATE SET manifest = EXCLUDED.manifest, image_repo = EXCLUDED.image_repo,
               image_digest = EXCLUDED.image_digest, image_size_mb = EXCLUDED.image_size_mb,
               enabled = EXCLUDED.enabled, updated_at = now()
        RETURNING *
        """,
        (manifest.name, Jsonb(manifest.to_json()), manifest.image, digest, size_mb, enabled),
    ).fetchone()


def replace_manifest(conn: psycopg.Connection, manifest: Manifest) -> None:
    """Swap the stored manifest of an existing workload (as a re-sync of an edited file does)."""
    conn.execute("UPDATE workloads SET manifest = %s WHERE name = %s", (Jsonb(manifest.to_json()), manifest.name))


# ------------------------------------------------------------------ machines


@dataclass
class FakeMachine:
    """A machine inserted straight into the tables, with its plain token."""

    id: str
    token: str
    name: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def new_machine_id() -> str:
    return "m_" + secrets.token_hex(3)


def insert_machine(
    conn: psycopg.Connection,
    name: str = "box",
    *,
    ram_total_mb: int | None = 3800,
    disk_free_mb: int | None = 9000,
    disk_size_mb: int | None = 15000,
    disk_type: str = "flash",
    disk_override: str | None = None,
    docker_ok: bool = True,
    native: str = "absent",
    remote_ip: str | None = None,
    boot_id: str | None = None,
    worker_id: str | None = None,
    pinned: bool = False,
    pinned_reason: str | None = None,
    enabled: bool = True,
    online: bool = True,
    workload: str | None = None,
    epoch: int = 1,
    state: str | None = None,
    acked_epoch: int = 0,
) -> FakeMachine:
    """A machine row plus its workload_assignments row. Defaults are the realistic fleet
    box: a 4 GB-RAM machine (3800 MB usable) on a 16 GB flash card with 9 GB free."""
    token = auth.mint_token()
    mid = new_machine_id()
    conn.execute(
        """
        INSERT INTO machines (id, name, token_hash, hostname, boot_id, remote_ip, docker_ok, arch, cpu_count,
                              ram_total_mb, ram_used_mb, disk_type_detected, disk_type_override, disk_size_mb,
                              disk_free_mb, docker_root, native_polymarket, polymarket_worker_id, pinned,
                              pinned_reason, pinned_at, enabled, last_heartbeat_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'x86_64', 4, %s, 500, %s, %s, %s, %s, '/var/lib/docker', %s, %s, %s, %s,
                CASE WHEN %s THEN now() END, %s,
                CASE WHEN %s THEN now() ELSE now() - interval '1 hour' END)
        """,
        (mid, name, auth.hash_token(token), name, boot_id, remote_ip, docker_ok, ram_total_mb, disk_type,
         disk_override, disk_size_mb, disk_free_mb, native, worker_id, pinned, pinned_reason, pinned, enabled, online),
    )
    conn.execute(
        """
        INSERT INTO workload_assignments (machine_id, workload, epoch, acked_epoch, state, assigned_by)
        VALUES (%s, %s, %s, %s, %s, 'test')
        """,
        (mid, workload, epoch, acked_epoch, state or ("stopped" if workload is None else "pending")),
    )
    return FakeMachine(mid, token, name)


def machine_row(conn: psycopg.Connection, machine_id: str) -> dict[str, Any]:
    return conn.execute("SELECT * FROM machines WHERE id = %s", (machine_id,)).fetchone()


def assignment_of(conn: psycopg.Connection, machine_id: str) -> dict[str, Any]:
    """The workload_assignments row of a machine."""
    return conn.execute("SELECT * FROM workload_assignments WHERE machine_id = %s", (machine_id,)).fetchone()


def set_assignment(
    conn: psycopg.Connection,
    machine_id: str,
    workload: str | None,
    epoch: int,
    state: str = "running",
    acked: int | None = None,
) -> dict[str, Any]:
    """Put a machine's assignment at an exact (workload, epoch, state), clearing the run token."""
    return conn.execute(
        """
        UPDATE workload_assignments SET workload = %s, epoch = %s, state = %s, acked_epoch = %s,
               run_token_hash = NULL, draining_to = NULL, draining_to_set = false, updated_at = now()
         WHERE machine_id = %s RETURNING *
        """,
        (workload, epoch, state, epoch if acked is None else acked, machine_id),
    ).fetchone()


def set_remote_ip(conn: psycopg.Connection, machine_id: str, ip: str) -> None:
    conn.execute("UPDATE machines SET remote_ip = %s WHERE id = %s", (ip, machine_id))


def link_worker(conn: psycopg.Connection, machine: FakeMachine, worker: FakeWorker) -> None:
    """Link a machine to its Polymarket worker the way `link_polymarket_worker` would."""
    conn.execute("UPDATE machines SET polymarket_worker_id = %s WHERE id = %s", (worker.id, machine.id))


def audit_for(conn: psycopg.Connection, *, action: str | None = None, entity: str | None = None) -> list[dict[str, Any]]:
    """audit_log rows filtered by action and/or entity, oldest first."""
    clauses, params = [], []
    if action is not None:
        clauses.append("action = %s")
        params.append(action)
    if entity is not None:
        clauses.append("entity = %s")
        params.append(entity)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(f"SELECT * FROM audit_log {where} ORDER BY id", params).fetchall()


def audit_count(conn: psycopg.Connection) -> int:
    return int(conn.execute("SELECT count(*) AS n FROM audit_log").fetchone()["n"])


# ------------------------------------------------------------------ Polymarket trading rows

_game_counter = iter(range(1, 10_000))


def unique_game_id() -> str:
    return f"2026_05_T{next(_game_counter)}"


def live_trader(
    conn: psycopg.Connection,
    machine: FakeMachine,
    *,
    mode: str = "live",
    name: str | None = None,
    game_id: str | None = None,
    leased: bool = True,
) -> TradeSetup:
    """A trade worker linked to `machine` holding the leased trade job of a `mode`
    assignment (live by default; `mode="paper"` for a paper-only trader)."""
    worker = insert_worker(conn, name or f"w-{machine.name}-{uuid.uuid4().hex[:4]}", role="trade")
    conn.execute("UPDATE machines SET polymarket_worker_id = %s WHERE id = %s", (worker.id, machine.id))
    setup = trade_setup(
        conn, mode=mode, game_id=game_id or unique_game_id(), worker=worker,
        model_status="live_eligible" if mode == "live" else "paper_ok",
    )
    if not leased:
        release_trade_lease(conn, setup)
    return setup


def release_trade_lease(conn: psycopg.Connection, setup: TradeSetup) -> None:
    """Hand the trade job back to the queue (as a drain release does); the assignment stays."""
    conn.execute(
        """
        UPDATE jobs SET status = 'queued', lease_worker_id = NULL, lease_token = NULL,
               lease_expires_at = NULL, updated_at = now() WHERE id = %s
        """,
        (setup.job["id"],),
    )


def stop_trading(conn: psycopg.Connection, setup: TradeSetup) -> None:
    """Trading is over: the assignment settles, its job completes, its open orders are gone."""
    conn.execute(
        "UPDATE assignments SET status = 'settled', settled_at = now(), updated_at = now() WHERE id = %s",
        (setup.assignment["id"],),
    )
    conn.execute(
        """
        UPDATE jobs SET status = 'succeeded', lease_worker_id = NULL, lease_token = NULL,
               lease_expires_at = NULL, finished_at = now(), updated_at = now() WHERE id = %s
        """,
        (setup.job["id"],),
    )
    conn.execute(
        """
        UPDATE orders SET status = 'cancelled', updated_at = now()
         WHERE assignment_id = %s AND status IN ('approved', 'submitting', 'open', 'partial', 'cancel_requested')
        """,
        (setup.assignment["id"],),
    )


def insert_order(
    conn: psycopg.Connection,
    worker_id: str | None,
    market_id: Any,
    *,
    mode: str = "live",
    status: str = "open",
    assignment_id: Any = None,
) -> dict[str, Any]:
    """An order row of a worker straight into the table (any mode and status)."""
    return conn.execute(
        """
        INSERT INTO orders (client_request_id, assignment_id, worker_id, market_id, mode, price, size, cost_cents, status)
        VALUES (%s, %s, %s, %s, %s, 0.5, 10, 500, %s) RETURNING *
        """,
        (uuid.uuid4().hex, assignment_id, worker_id, market_id, mode, status),
    ).fetchone()


def set_worker_acked_idle(conn: psycopg.Connection, worker_id: str) -> None:
    """What a worker's heartbeat does once it has finished a role change to idle."""
    conn.execute(
        "UPDATE workers SET reported_role = 'idle', acked_epoch = role_epoch, last_heartbeat_at = now() WHERE id = %s",
        (worker_id,),
    )


# ------------------------------------------------------------------ Polymarket jobs snapshot


def polymarket_snapshot(conn: psycopg.Connection) -> dict[str, list[str]]:
    """Every row of the Polymarket queue tables as text, to prove workload code never touches them."""
    out: dict[str, list[str]] = {}
    for table in ("jobs", "job_events", "workers", "assignments", "orders", "bankrolls", "ledger"):
        out[table] = [r["t"] for r in conn.execute(f"SELECT t::text AS t FROM {table} t ORDER BY 1").fetchall()]
    return out


# ------------------------------------------------------------------ workload_jobs rows


def insert_wl_job(
    conn: psycopg.Connection,
    workload: str,
    kind: str,
    *,
    params: dict[str, Any] | None = None,
    target: str | None = None,
    status: str = "queued",
    max_expiries: int | None = 3,
    **cols: Any,
) -> dict[str, Any]:
    """A workload_jobs row straight into the table (for states `create_job` cannot make)."""
    names = ["workload", "kind", "params", "target_machine_id", "status", "max_expiries"] + list(cols)
    values = [workload, kind, Jsonb(params or {}), target, status, max_expiries] + list(cols.values())
    marks = ", ".join(["%s"] * len(values))
    return conn.execute(
        f"INSERT INTO workload_jobs ({', '.join(names)}) VALUES ({marks}) RETURNING *", values
    ).fetchone()


def lease_wl_job(
    conn: psycopg.Connection, job_id: Any, machine: FakeMachine, epoch: int, seconds: int = 60,
    status: str = "leased",
) -> dict[str, Any]:
    """Lease a workload job to a machine directly (as a claim would); the row has its lease_token."""
    return conn.execute(
        """
        UPDATE workload_jobs SET status = %s, lease_machine_id = %s, lease_epoch = %s, lease_token = gen_random_uuid(),
               lease_expires_at = now() + make_interval(secs => %s), started_at = COALESCE(started_at, now())
         WHERE id = %s RETURNING *
        """,
        (status, machine.id, epoch, seconds, job_id),
    ).fetchone()


def wl_job(conn: psycopg.Connection, job_id: Any) -> dict[str, Any]:
    return conn.execute("SELECT * FROM workload_jobs WHERE id = %s", (uuid.UUID(str(job_id)),)).fetchone()


def expire_wl_lease(conn: psycopg.Connection, job_id: Any) -> None:
    conn.execute(
        "UPDATE workload_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
        (uuid.UUID(str(job_id)),),
    )


def wl_events(conn: psycopg.Connection, job_id: Any) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM workload_job_events WHERE job_id = %s ORDER BY id", (uuid.UUID(str(job_id)),)
    ).fetchall()


# ------------------------------------------------------------------ outbound rows


def insert_outbound(
    conn: psycopg.Connection,
    workload: str,
    kind: str = "log",
    *,
    payload: dict[str, Any] | None = None,
    status: str = "pending",
    dedupe_key: str | None = None,
    machine_id: str | None = None,
    job_id: Any = None,
    age_days: float = 0,
) -> dict[str, Any]:
    """An outbound_actions row in any status, created `age_days` ago."""
    return conn.execute(
        """
        INSERT INTO outbound_actions (workload, machine_id, job_id, kind, payload, dedupe_key, status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now() - make_interval(secs => %s)) RETURNING *
        """,
        (workload, machine_id, job_id, kind, Jsonb(payload if payload is not None else {"note": "x"}),
         dedupe_key or uuid.uuid4().hex, status, age_days * 86400),
    ).fetchone()


def outbound_row(conn: psycopg.Connection, action_id: Any) -> dict[str, Any]:
    return conn.execute("SELECT * FROM outbound_actions WHERE id = %s", (uuid.UUID(str(action_id)),)).fetchone()


@dataclass
class FakeSender:
    """A sender that records every call and sends nothing. `fail` makes it raise."""

    result: dict[str, Any] = field(default_factory=lambda: {"ok": True})
    fail: Exception | None = None
    delay: float = 0.0
    calls: list[tuple[dict[str, Any], dict[str, str]]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, action: dict[str, Any], host_secrets: dict[str, str]) -> dict[str, Any]:
        with self._lock:
            self.calls.append((dict(action), dict(host_secrets)))
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return dict(self.result)

    @property
    def ids(self) -> list[str]:
        return [str(a["id"]) for a, _ in self.calls]


# ------------------------------------------------------------------ calling the contract functions


def call(pool: Any, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    """Run a contract function on its own pooled connection, committed on success and
    rolled back when it raises (the shape the API gives it)."""
    with pool.connection() as c:
        return fn(c, *args, **kw)


def mint_run(pool: Any, machine: FakeMachine, epoch: int) -> str:
    """A run token through the contract function `host.workloads.tokens.mint_run_token`."""
    from host.workloads.tokens import mint_run_token

    return call(pool, mint_run_token, machine.id, epoch)


# ------------------------------------------------------------------ HTTP helpers


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextlib.contextmanager
def strict_client(config: Any, **overrides: Any) -> Iterator[TestClient]:
    """A client against an app that is not in dev mode: the owner login and the
    worker-IP refusal are enforced. `overrides` replace Config fields."""
    cfg = dataclasses.replace(config, dev=False, owner_login=OWNER_LOGIN, **overrides)
    with TestClient(create_app(cfg)) as c:
        yield c


@dataclass
class RunCreds:
    token: str
    secrets: dict[str, str]

    @property
    def headers(self) -> dict[str, str]:
        return bearer(self.token)


def start_run(client: TestClient, machine: FakeMachine, epoch: int) -> Any:
    """POST /api/v1/machines/{id}/start (the supervisor's call): the raw response."""
    return client.post(f"/api/v1/machines/{machine.id}/start", json={"epoch": epoch}, headers=machine.headers)


def get_run(client: TestClient, machine: FakeMachine, epoch: int | None = None, conn: Any = None) -> RunCreds:
    """Start the machine's container at its current (or the given) epoch; asserts 200."""
    if epoch is None:
        epoch = int(conn.execute("SELECT epoch FROM workload_assignments WHERE machine_id = %s", (machine.id,)).fetchone()["epoch"])
    r = start_run(client, machine, epoch)
    assert r.status_code == 200, r.text
    body = r.json()
    return RunCreds(body["run_token"], body.get("secrets", {}))


def machine_heartbeat_body(epoch: int, **extra: Any) -> dict[str, Any]:
    """A section 5.1 heartbeat body for a healthy 4 GB flash machine."""
    body: dict[str, Any] = {
        "specs": {"cpu_pct": 4.0, "ram_used_mb": 900, "ram_total_mb": 3800, "cpu_count": 4, "arch": "x86_64",
                  "disk_type": "flash", "disk_size_mb": 15000, "disk_free_mb": 9000,
                  "docker_root": "/var/lib/docker", "docker_ok": True, "docker_version": "26.1.5"},
        "native_polymarket": "absent",
        "acked_epoch": epoch,
        "container": None,
        "logs": [],
        "cleanup": {"images_removed": 0, "bytes_freed": 0, "low_disk": False},
    }
    body.update(extra)
    return body


def put_secret(client: TestClient, workload: str, name: str, value: str, headers: dict[str, str] | None = None) -> Any:
    return client.put(f"/api/workloads/{workload}/secrets/{name}", json={"value": value}, headers=headers or {})


# ------------------------------------------------------------------ whole-database scan


def database_text(conn: psycopg.Connection) -> str:
    """Every row of every table as text (bytea shows as hex), for 'this value is nowhere' checks."""
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    ).fetchall()
    parts: list[str] = []
    for t in tables:
        name = t["table_name"]
        parts.extend(r["t"] for r in conn.execute(f'SELECT x::text AS t FROM "{name}" x').fetchall())
    return "\n".join(parts)


def forms_of(value: str) -> list[str]:
    """The ways a plaintext could show up: as is, base64, hex."""
    raw = value.encode()
    return [value, base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode(), raw.hex()]


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def secrets_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """FLEET_SECRETS_KEY set to a generated key."""
    monkeypatch.setenv("FLEET_SECRETS_KEY", SECRETS_KEY)
    return SECRETS_KEY


@pytest.fixture
def no_secrets_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """FLEET_SECRETS_KEY absent."""
    monkeypatch.delenv("FLEET_SECRETS_KEY", raising=False)


# ------------------------------------------------------------------ the two-workload secrets world

ALPHA_KEY_VALUE = "alpha-container-secret-4f81c2"
ALPHA_SMTP_VALUE = "smtps://alphauser:alpha-smtp-pw-77ab@smtp.alpha.example:465"
ALPHA_FROM_VALUE = "alpha-sender@alpha.example"
BRAVO_KEY_VALUE = "bravo-container-secret-91e0d7"
BRAVO_SMTP_VALUE = "smtps://bravouser:bravo-smtp-pw-23cd@smtp.bravo.example:465"
ALPHA_VALUES = {"ALPHA_KEY": ALPHA_KEY_VALUE, "SMTP_URL": ALPHA_SMTP_VALUE, "EMAIL_FROM": ALPHA_FROM_VALUE}
BRAVO_VALUES = {"BRAVO_KEY": BRAVO_KEY_VALUE, "SMTP_URL": BRAVO_SMTP_VALUE}
ALL_SECRET_VALUES = (ALPHA_KEY_VALUE, ALPHA_SMTP_VALUE, ALPHA_FROM_VALUE, BRAVO_KEY_VALUE, BRAVO_SMTP_VALUE)


def alpha_manifest() -> Manifest:
    """Container secret ALPHA_KEY, host-only SMTP_URL and EMAIL_FROM, outbound email and log."""
    return make_manifest("alpha", kinds=["alpha"], container=("ALPHA_KEY",), host_only=("SMTP_URL", "EMAIL_FROM"),
                         actions=("email", "log"))


def bravo_manifest() -> Manifest:
    """Container secret BRAVO_KEY, host-only SMTP_URL (its own), outbound email."""
    return make_manifest("bravo", kinds=["bravo"], container=("BRAVO_KEY",), host_only=("SMTP_URL",), actions=("email",))


def quiet_manifest() -> Manifest:
    """No secrets and no outbound actions."""
    return make_manifest("quiet", kinds=["quiet"])


@dataclass
class SecretWorld:
    """alpha, bravo and quiet published; one machine each (alpha at epoch 5, bravo at 2,
    quiet at 1); every secret of alpha and bravo written through the owner API."""

    client: TestClient
    conn: Any
    alpha: FakeMachine
    bravo: FakeMachine
    quiet: FakeMachine
    alpha_epoch: int = 5
    bravo_epoch: int = 2
    quiet_epoch: int = 1


@pytest.fixture
def secret_world(client: TestClient, conn: Any, secrets_key: str) -> SecretWorld:
    for m in (alpha_manifest(), bravo_manifest(), quiet_manifest()):
        insert_workload(conn, m, size_mb=50)
    world = SecretWorld(
        client, conn,
        insert_machine(conn, "alphabox", workload="alpha", epoch=5, state="running", acked_epoch=5),
        insert_machine(conn, "bravobox", workload="bravo", epoch=2, state="running", acked_epoch=2),
        insert_machine(conn, "quietbox", workload="quiet", epoch=1, state="running", acked_epoch=1),
    )
    for workload, values in (("alpha", ALPHA_VALUES), ("bravo", BRAVO_VALUES)):
        for name, value in values.items():
            r = put_secret(client, workload, name, value)
            assert r.status_code < 300, (workload, name, r.text)
    return world
