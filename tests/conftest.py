"""Shared fixtures: per-test databases cloned from a migrated template, app client, fake workers."""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
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


# ------------------------------------------------------------------ step 3 helpers

FIXTURE_GAMES = Path(__file__).resolve().parent / "fixtures" / "games_sample.csv"


def ingest_fixture(conn: psycopg.Connection) -> dict[str, Any]:
    """Load the nflverse fixture (2016-2025) into the games table."""
    from host import nflverse

    return nflverse.ingest(conn, str(FIXTURE_GAMES))


def backtest_metrics(
    n_bets: int = 300, roi: float = 0.03, log_loss: float = 0.660, market_log_loss: float = 0.659,
    max_drawdown: float = 0.12, seasons: list[int] | None = None, **extra: Any,
) -> dict[str, Any]:
    """A metrics object of the docs/MODELS.md shape with chosen headline numbers."""
    seasons = seasons if seasons is not None else [2016, 2017, 2018]
    stake = n_bets * 1200
    metrics = {
        "n_games": max(n_bets, 1) * 3, "n_bets": n_bets, "total_stake_cents": stake,
        "pnl_cents": int(round(stake * roi)), "roi": roi, "hit_rate": 0.52, "avg_edge": 0.034,
        "avg_stake_cents": 1200.0, "log_loss": log_loss, "brier": 0.235, "market_log_loss": market_log_loss,
        "calibration": [{"count": 10, "mean_p": (i + 0.5) / 10, "mean_outcome": (i + 0.5) / 10} for i in range(10)],
        "max_drawdown_cents": int(round(max_drawdown * 60000)), "max_drawdown": max_drawdown, "seasons": seasons,
    }
    metrics.update(extra)
    return metrics


def insert_model(
    conn: psycopg.Connection,
    family: str = "elo_blend",
    params: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    status: str = "candidate",
    parent: dict[str, Any] | None = None,
    summary: str | None = None,
    trained_through: list[int] | None = None,
    artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a model row directly (a root, or a child of `parent` in its lineage)."""
    from psycopg.types.json import Jsonb

    from fleet.models.base import params_hash

    params = params if params is not None else {"k": 24.0, "hfa": 55.0, "mov_scale": 1}
    model_id = uuid.uuid4()
    lineage_id = parent["lineage_id"] if parent else model_id
    if parent:
        status = parent["status"]
        metrics = parent["backtest_metrics"] if metrics is None else metrics
    return conn.execute(
        """
        INSERT INTO models (id, lineage_id, family, params, params_hash, artifact, parent_model_id,
                            trained_through, summary, status, backtest_metrics)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (
            model_id, lineage_id, family, Jsonb(params), params_hash(params),
            Jsonb(artifact) if artifact is not None else None, parent["id"] if parent else None,
            Jsonb(trained_through) if trained_through is not None else None, summary, status,
            Jsonb(metrics) if metrics is not None else None,
        ),
    ).fetchone()


def lease_job(conn: psycopg.Connection, worker: FakeWorker, kind: str = "backtest", params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Insert a job already leased by `worker` (the row carries its lease_token)."""
    from psycopg.types.json import Jsonb

    role = {"sleep": "backtest"}.get(kind, kind)
    return conn.execute(
        """
        INSERT INTO jobs (kind, role, status, params, lease_worker_id, lease_token, lease_expires_at, started_at)
        VALUES (%s, %s, 'leased', %s, %s, gen_random_uuid(), now() + interval '30 seconds', now()) RETURNING *
        """,
        (kind, role, Jsonb(params or {}), worker.id),
    ).fetchone()


def model_row(conn: psycopg.Connection, model_id: Any) -> dict[str, Any]:
    """Fetch a model row by id."""
    return conn.execute("SELECT * FROM models WHERE id = %s", (uuid.UUID(str(model_id)),)).fetchone()


# ------------------------------------------------------------------ step 4 helpers

GAME_ID = "2026_05_KC_LV"


def insert_game(
    conn: psycopg.Connection,
    game_id: str = GAME_ID,
    home: str = "LV",
    away: str = "KC",
    kickoff_in_s: int = 48 * 3600,
    season: int = 2026,
    week: int = 5,
    status: str = "scheduled",
) -> dict[str, Any]:
    """Insert (or refresh) a scheduled game kicking off `kickoff_in_s` from now."""
    return conn.execute(
        """
        INSERT INTO games (game_id, season, game_type, week, gameday, kickoff_at, home_team, away_team,
                           home_moneyline, away_moneyline, status, raw)
        VALUES (%s, %s, 'REG', %s, (now() + make_interval(secs => %s))::date, now() + make_interval(secs => %s),
                %s, %s, -150, 130, %s, '{}')
        ON CONFLICT (game_id) DO UPDATE SET kickoff_at = EXCLUDED.kickoff_at, status = EXCLUDED.status
        RETURNING *
        """,
        (game_id, season, week, kickoff_in_s, kickoff_in_s, home, away, status),
    ).fetchone()


def insert_market(
    conn: psycopg.Connection,
    game_id: str = GAME_ID,
    side: str = "home",
    confirmed: bool = True,
    status: str = "open",
    tick: float = 0.01,
    min_size: int = 1,
    platform: str = "sim",
) -> dict[str, Any]:
    """Insert a market mapped to a game (confirmed by default)."""
    return conn.execute(
        """
        INSERT INTO markets (platform, market_ref, title, game_id, side, mapping_confirmed, mapping_confidence,
                             status, tick, min_size)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (platform, uuid.uuid4().hex, f"{game_id} {side} wins", game_id, side, confirmed, 1.0 if confirmed else 0.5,
         status, tick, min_size),
    ).fetchone()


def insert_snapshot(
    conn: psycopg.Connection,
    market_id: Any,
    bid: float = 0.50,
    ask: float = 0.52,
    liquidity_usd_cents: int = 200_000,
    ask_depth: list[list[float]] | None = None,
    bid_depth: list[list[float]] | None = None,
    age_s: float = 0.0,
) -> dict[str, Any]:
    """Insert a price snapshot `age_s` seconds old and mirror bid/ask on the market."""
    from psycopg.types.json import Jsonb

    if ask_depth is None:
        ask_depth = [[ask, 500], [round(ask + 0.01, 4), 500], [round(ask + 0.02, 4), 500]]
    if bid_depth is None:
        bid_depth = [[bid, 500], [round(bid - 0.01, 4), 500]]
    row = conn.execute(
        """
        INSERT INTO price_snapshots (market_id, ts, bid, ask, mid, bid_depth, ask_depth, liquidity_usd_cents)
        VALUES (%s, now() - make_interval(secs => %s), %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (market_id, age_s, bid, ask, round((bid + ask) / 2, 4), Jsonb(bid_depth), Jsonb(ask_depth), liquidity_usd_cents),
    ).fetchone()
    conn.execute(
        "UPDATE markets SET best_bid = %s, best_ask = %s, liquidity_usd_cents = %s, last_snapshot_at = %s WHERE id = %s",
        (bid, ask, liquidity_usd_cents, row["ts"], market_id),
    )
    return row


def make_assignment(
    conn: psycopg.Connection,
    game_id: str = GAME_ID,
    model_id: Any = None,
    mode: str = "paper",
    bankroll_cents: int = 10_000,
    max_bet_cents: int | None = None,
    actor: str = "test",
) -> dict[str, Any]:
    """Create an assignment through host.trading.assignments (bankroll + trade job)."""
    from host.trading.assignments import create_assignment

    if model_id is None:
        model_id = insert_model(conn, status="paper_ok", artifact={"ratings": {}, "blend": {"a": 1, "b": 0, "c": 0}},
                                params={"k": 24.0, "hfa": 55.0, "mov_scale": 1, "seed": uuid.uuid4().hex[:8]})["id"]
    return create_assignment(conn, game_id, model_id, mode, bankroll_cents, actor, max_bet_cents)


def lease_trade_job(conn: psycopg.Connection, worker: FakeWorker, assignment: dict[str, Any]) -> dict[str, Any]:
    """Lease the assignment's trade job to `worker` directly (as a claim would)."""
    return conn.execute(
        """
        UPDATE jobs SET status = 'leased', lease_worker_id = %s, lease_token = gen_random_uuid(),
               lease_expires_at = now() + interval '60 seconds', started_at = COALESCE(started_at, now()),
               preempt_requested = false, updated_at = now()
         WHERE id = %s RETURNING *
        """,
        (worker.id, assignment["job_id"]),
    ).fetchone()


def order_body(
    assignment: dict[str, Any],
    job: dict[str, Any],
    market: dict[str, Any],
    snapshot: dict[str, Any] | None,
    price: float = 0.52,
    size: int = 10,
    **extra: Any,
) -> dict[str, Any]:
    """A complete POST /api/v1/orders/request body with a fresh client_request_id."""
    body = {
        "client_request_id": uuid.uuid4().hex,
        "job_id": str(job["id"]),
        "lease_token": str(job["lease_token"]),
        "assignment_id": str(assignment["id"]),
        "market_id": str(market["id"]),
        "snapshot_id": None if snapshot is None else int(snapshot["id"]),
        "price": price,
        "size": size,
        "my_p": 0.58,
        "market_p": 0.51,
        "edge": 0.04,
        "rationale": "my 0.58 vs ask 0.52, fee 0.012, edge 0.04",
    }
    body.update(extra)
    return body


@dataclass
class TradeSetup:
    """A game, a confirmed market with a snapshot, a funded assignment and a trade
    worker holding its leased trade job."""

    game: dict[str, Any]
    model: dict[str, Any]
    market: dict[str, Any]
    snapshot: dict[str, Any]
    assignment: dict[str, Any]
    worker: FakeWorker
    worker_row: dict[str, Any]
    job: dict[str, Any]

    def body(self, **kw: Any) -> dict[str, Any]:
        return order_body(self.assignment, self.job, self.market, self.snapshot, **kw)


def trade_setup(
    conn: psycopg.Connection,
    mode: str = "paper",
    bankroll_cents: int = 10_000,
    max_bet_cents: int | None = None,
    game_id: str = GAME_ID,
    worker: FakeWorker | None = None,
    model_status: str = "paper_ok",
    kickoff_in_s: int = 48 * 3600,
    liquidity_usd_cents: int = 200_000,
    side: str = "home",
) -> TradeSetup:
    """Everything an approval needs, in one call (live mode also flips the gates on)."""
    game = insert_game(conn, game_id, kickoff_in_s=kickoff_in_s)
    model = insert_model(conn, status=model_status, artifact={"ratings": {}, "blend": {"a": 1, "b": 0, "c": 0}},
                         params={"k": 24.0 + len(game_id) % 7, "hfa": 55.0, "mov_scale": 1, "seed": uuid.uuid4().hex[:6]})
    market = insert_market(conn, game_id, side=side)
    snapshot = insert_snapshot(conn, market["id"], liquidity_usd_cents=liquidity_usd_cents)
    if mode == "live":
        enable_live(conn)
    assignment = make_assignment(conn, game_id, model["id"], mode, bankroll_cents, max_bet_cents)
    worker = worker or insert_worker(conn, f"trader-{uuid.uuid4().hex[:4]}", role="trade")
    job = lease_trade_job(conn, worker, assignment)
    return TradeSetup(game, model, market, snapshot, assignment, worker, worker_row(conn, worker.id), job)


def enable_live(conn: psycopg.Connection, buying_power_cents: int = 10_000_000) -> None:
    """Flip every live gate on: live_enabled, the exchange auth row (credentials present,
    auth_ok, fresh balance and buying power, no skew)."""
    conn.execute("UPDATE settings SET value = 'true' WHERE key = 'live_enabled'")
    conn.execute(
        """
        UPDATE exchange_state SET auth_ok = true, auth_checked_at = now(), heartbeat_at = now(), credentials_present = true,
               balance_cents = %s, buying_power_cents = %s, balance_checked_at = now(), clock_skew_ms = 0,
               auth_failures = 0, last_auth_error = NULL, live_enabled_at = now(), live_enabled_by = 'test'
        """,
        (buying_power_cents, buying_power_cents),
    )


def auth_state(conn: psycopg.Connection, **cols: Any) -> None:
    """Set exchange_state columns directly (step 5 live tests)."""
    sets = ", ".join(f"{name} = %s" for name in cols)
    conn.execute(f"UPDATE exchange_state SET {sets}, updated_at = now() WHERE id = true", list(cols.values()))


def audit_rows(conn: psycopg.Connection, action: str) -> list[dict[str, Any]]:
    """Audit rows of one action, oldest first."""
    return conn.execute(
        "SELECT actor, entity, before, after, confirmation_text FROM audit_log WHERE action = %s ORDER BY id", (action,)
    ).fetchall()


def set_setting(conn: psycopg.Connection, key: str, value: Any) -> None:
    """Write one setting as JSON without validation."""
    from psycopg.types.json import Jsonb

    conn.execute("UPDATE settings SET value = %s WHERE key = %s", (Jsonb(value), key))


def post_loss(conn: psycopg.Connection, bankroll_id: Any, cents: int, ts: Any = None) -> None:
    """Record a realized loss of `cents` on a bankroll (an `adjust` ledger row at `ts`,
    default now) and keep the cached columns in step."""
    bank = conn.execute("SELECT mode FROM bankrolls WHERE id = %s", (bankroll_id,)).fetchone()
    conn.execute(
        """
        INSERT INTO ledger (bankroll_id, ts, mode, kind, d_available, d_realized, ref_type, note)
        VALUES (%s, COALESCE(%s, clock_timestamp()), %s, 'adjust', %s, %s, 'owner', 'test loss')
        """,
        (bankroll_id, ts, bank["mode"], -cents, -cents),
    )
    conn.execute(
        "UPDATE bankrolls SET available_cents = available_cents - %s, realized_pnl_cents = realized_pnl_cents - %s WHERE id = %s",
        (cents, cents, bankroll_id),
    )


def approve(conn: psycopg.Connection, setup: TradeSetup, **kw: Any) -> dict[str, Any]:
    """Run approve_order for a setup; the decision dict."""
    from host.trading.limits import approve_order

    return approve_order(conn, worker_row(conn, setup.worker.id), setup.body(**kw))


def approved_order(conn: psycopg.Connection, setup: TradeSetup, **kw: Any) -> dict[str, Any]:
    """An approved order row for the setup (asserts the approval)."""
    decision = approve(conn, setup, **kw)
    assert decision["status"] == "approved", decision
    return order_row(conn, decision["order_id"])


def order_row(conn: psycopg.Connection, order_id: Any) -> dict[str, Any]:
    return conn.execute("SELECT * FROM orders WHERE id = %s", (uuid.UUID(str(order_id)),)).fetchone()


def order_events(conn: psycopg.Connection, order_id: Any) -> list[str]:
    """The to_status column of an order's events in order."""
    rows = conn.execute("SELECT to_status FROM order_events WHERE order_id = %s ORDER BY id", (uuid.UUID(str(order_id)),)).fetchall()
    return [r["to_status"] for r in rows]


def bankroll_of(conn: psycopg.Connection, assignment: dict[str, Any]) -> dict[str, Any]:
    return conn.execute("SELECT * FROM bankrolls WHERE assignment_id = %s", (assignment["id"],)).fetchone()


def assignment_row(conn: psycopg.Connection, assignment_id: Any) -> dict[str, Any]:
    return conn.execute("SELECT * FROM assignments WHERE id = %s", (uuid.UUID(str(assignment_id)),)).fetchone()
