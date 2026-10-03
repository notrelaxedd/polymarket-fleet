"""Shared fixtures: per-test databases cloned from a migrated template, app client, fake workers."""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterator
from urllib.parse import unquote

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from host import auth, db
from host.api.app import create_app
from host.config import Config

ADMIN_URL = os.environ.get(
    "FLEET_TEST_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
)
TEMPLATE_PREFIX = "fleet_test_template_"
TEMPLATE_LOCK_KEY = 7_401_002


def _migrations_digest() -> str:
    """Short hash over every migration's name and content: a changed migration file
    (even under the same version name) gets its own template database."""
    h = hashlib.sha256()
    for path in db.migration_files():
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


TEMPLATE_DB = TEMPLATE_PREFIX + _migrations_digest()


def db_url(name: str) -> str:
    """The admin connection string pointed at another database."""
    return make_conninfo(ADMIN_URL, dbname=name)


def _admin() -> psycopg.Connection:
    return psycopg.connect(ADMIN_URL, autocommit=True, row_factory=dict_row)


def _template_is_current(admin: psycopg.Connection) -> bool:
    """True when the template exists and has every migration applied."""
    exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEMPLATE_DB,)).fetchone()
    if not exists:
        return False
    wanted = {p.stem for p in db.migration_files()}
    try:
        with psycopg.connect(db_url(TEMPLATE_DB), row_factory=dict_row) as conn:
            rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    except psycopg.Error:
        return False
    return wanted <= {r["version"] for r in rows}


def _drop(admin: psycopg.Connection, name: str) -> None:
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def template_db() -> str:
    """Create the migrated template database once per session."""
    with _admin() as admin:
        admin.execute("SELECT pg_advisory_lock(%s)", (TEMPLATE_LOCK_KEY,))
        try:
            if not _template_is_current(admin):
                _drop(admin, TEMPLATE_DB)
                admin.execute(f'CREATE DATABASE "{TEMPLATE_DB}"')
                db.migrate(db_url(TEMPLATE_DB))
        finally:
            admin.execute("SELECT pg_advisory_unlock(%s)", (TEMPLATE_LOCK_KEY,))
    return TEMPLATE_DB


@pytest.fixture
def test_db_url(template_db: str) -> Iterator[str]:
    """A fresh database cloned from the template, dropped afterwards."""
    name = f"fleet_test_{uuid.uuid4().hex[:12]}"
    with _admin() as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
    try:
        yield db_url(name)
    finally:
        with _admin() as admin:
            _drop(admin, name)


@pytest.fixture
def pool(test_db_url: str) -> Iterator[ConnectionPool]:
    """A pool on the per-test database, large enough for the concurrency tests."""
    p = db.make_pool(test_db_url, min_size=1, max_size=40)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def conn(pool: ConnectionPool) -> Iterator[psycopg.Connection]:
    """One autocommitting-per-statement style connection for assertions.

    Each statement is committed right away so it never blocks the app's own
    transactions running in other threads.
    """
    with pool.connection() as c:
        c.autocommit = True
        yield c


@pytest.fixture
def config(test_db_url: str, tmp_path) -> Config:
    """Dev-mode config pointed at the per-test database."""
    return Config(
        database_url=test_db_url,
        public_url="http://127.0.0.1:8080",
        owner_login="owner@example.com",
        dev=True,
        allowed_origins=("http://127.0.0.1:8080",),
        deploy_dir=tmp_path / "deploy",
    )


@pytest.fixture
def app(config: Config):
    return create_app(config)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    """TestClient with the lifespan (pool) running."""
    with TestClient(app) as c:
        yield c


@dataclass
class FakeWorker:
    """A worker inserted straight into the table, with its plain token."""

    id: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def insert_worker(
    conn: psycopg.Connection,
    name: str = "box",
    role: str = "idle",
    enabled: bool = True,
    online: bool = True,
    acked: bool = True,
    auto_role: bool = False,
) -> FakeWorker:
    """Insert a worker row in a settled state and return its id and token."""
    token = auth.mint_token()
    wid = auth.new_worker_id()
    conn.execute(
        """
        INSERT INTO workers (id, name, token_hash, desired_role, reported_role, role_epoch,
                             acked_epoch, enabled, auto_role, last_heartbeat_at, hostname,
                             python_version, code_version)
        VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s,
                CASE WHEN %s THEN now() ELSE now() - interval '1 hour' END, %s, '3.11', 'test')
        """,
        (wid, name, auth.hash_token(token), role, role, 1 if acked else 0, enabled, auto_role, online, name),
    )
    return FakeWorker(wid, token)


@pytest.fixture
def make_worker(conn: psycopg.Connection) -> Callable[..., FakeWorker]:
    """Factory fixture around insert_worker."""

    def _make(name: str = "box", **kw: Any) -> FakeWorker:
        return insert_worker(conn, name, **kw)

    return _make


def heartbeat_body(
    reported_role: str = "idle",
    acked_epoch: int = 1,
    want_job: bool = True,
    jobs: list[dict[str, Any]] | None = None,
    released: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A complete heartbeat payload with sensible defaults."""
    body = {
        "cpu_pct": 1.5,
        "ram_used_mb": 512,
        "ram_total_mb": 4096,
        "reported_role": reported_role,
        "acked_epoch": acked_epoch,
        "jobs": jobs or [],
        "released": released or [],
        "want_job": want_job,
        "code_version": "test",
        "skew_ms": 0,
    }
    body.update(extra)
    return body


@pytest.fixture
def heartbeat(client: TestClient, conn: psycopg.Connection) -> Callable[..., Any]:
    """POST a heartbeat for a worker; defaults echo its current desired role/epoch."""

    def _post(worker: FakeWorker, token: str | None = None, **kw: Any):
        row = conn.execute(
            "SELECT desired_role, role_epoch FROM workers WHERE id = %s", (worker.id,)
        ).fetchone()
        kw.setdefault("reported_role", row["desired_role"] if row else "idle")
        kw.setdefault("acked_epoch", row["role_epoch"] if row else 1)
        headers = {"Authorization": f"Bearer {token or worker.token}"}
        return client.post(f"/api/v1/workers/{worker.id}/heartbeat", json=heartbeat_body(**kw), headers=headers)

    return _post


def flash_cookie(response: Any) -> str:
    """The flash message a form redirect left in its cookie ("" when none)."""
    value = response.cookies.get("flash")
    return unquote(value) if value else ""


def job_row(conn: psycopg.Connection, job_id: Any) -> dict[str, Any]:
    """Fetch a job row by id."""
    return conn.execute("SELECT * FROM jobs WHERE id = %s", (uuid.UUID(str(job_id)),)).fetchone()


def worker_row(conn: psycopg.Connection, worker_id: str) -> dict[str, Any]:
    """Fetch a worker row by id."""
    return conn.execute("SELECT * FROM workers WHERE id = %s", (worker_id,)).fetchone()


def expire_lease(conn: psycopg.Connection, job_id: Any) -> None:
    """Move a lease into the past so the reaper picks it up."""
    conn.execute(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
        (uuid.UUID(str(job_id)),),
    )


def set_heartbeat_age(conn: psycopg.Connection, worker_id: str, seconds: int) -> None:
    """Make a worker's last heartbeat `seconds` old (dashboard status dots)."""
    conn.execute(
        "UPDATE workers SET last_heartbeat_at = now() - make_interval(secs => %s) WHERE id = %s",
        (seconds, worker_id),
    )


def insert_job(conn: psycopg.Connection, kind: str = "sleep", role: str | None = None, **cols: Any) -> dict[str, Any]:
    """Insert a job row directly (e.g. a trade job before step 4 can create one)."""
    role = role or {"sleep": "backtest"}.get(kind, kind)
    names = ["kind", "role"] + list(cols)
    values = [kind, role] + list(cols.values())
    placeholders = ", ".join(["%s"] * len(values))
    return conn.execute(
        f"INSERT INTO jobs ({', '.join(names)}) VALUES ({placeholders}) RETURNING *", values
    ).fetchone()
