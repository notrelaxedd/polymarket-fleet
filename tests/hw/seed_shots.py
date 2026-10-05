"""The throwaway database behind tests/hw/screenshots.py: create and drop it, seed
three workers (running, switching, offline) and a few jobs, then the step 3, 6, 4 and
6B rows (seed_step3/6/4/6b), and keep the workers and leases fresh while the captures
run."""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from host import db
from host.events import add_audit
from tests.conftest import ADMIN_URL, db_url, insert_job, insert_worker, set_heartbeat_age
from tests.hw.seed_step3 import seed_models
from tests.hw.seed_step4 import seed_trading, touch_trading
from tests.hw.seed_step5 import touch_live
from tests.hw.seed_step6 import seed_paper_ci, seed_validation
from tests.hw.seed_step6b import seed_sells, seed_snapshot


def fresh_database() -> str:
    """Create a migrated throwaway database and return its URL."""
    name = f"fleet_shots_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = db_url(name)
    db.migrate(url)
    return url


def drop_database(url: str) -> None:
    name = psycopg.conninfo.conninfo_to_dict(url)["dbname"]
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _machine(conn: psycopg.Connection, worker_id: str, cpu: float, used: int, total: int) -> None:
    conn.execute(
        """
        UPDATE workers SET cpu_pct = %s, ram_used_mb = %s, ram_total_mb = %s, hostname = name || '.lan',
               python_version = '3.11.2', code_version = 'a1b2c3d4e5f6'
         WHERE id = %s
        """,
        (cpu, used, total, worker_id),
    )


def _event(conn: psycopg.Connection, job_id: Any, event: str, worker_id: str | None, detail: dict | None, ago: int) -> None:
    conn.execute(
        "INSERT INTO job_events (job_id, ts, worker_id, event, detail) VALUES (%s, now() - make_interval(secs => %s), %s, %s, %s)",
        (job_id, ago, worker_id, event, Jsonb(detail) if detail is not None else None),
    )


def seed(url: str) -> dict[str, str]:
    """Three workers (running, switching, offline) and a few jobs, then the step 3, 6,
    4 and 6B rows (the module docstring lists them); returns the ids the captures need."""
    ids = _seed_fleet(url)
    ids.update(seed_models(url, ids["box2"], ids["box1"]))
    ids.update(seed_validation(url, ids["box2"]))
    ids.update(seed_trading(url, ids["model"]))
    seed_paper_ci(url)
    ids.update(seed_snapshot(url, ids["box2"], ids["model"]))
    ids.update(seed_sells(url, ids["trader"], ids["assignment"]))
    return ids


def _seed_fleet(url: str) -> dict[str, str]:
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        box1 = insert_worker(conn, "box1", role="backtest")
        _machine(conn, box1.id, 37.5, 2611, 7936)
        running = insert_job(
            conn, "sleep", status="leased", progress=0.42, lease_worker_id=box1.id, target_worker_id=box1.id,
            params=Jsonb({"seconds": 60}), checkpoint=Jsonb({"elapsed": 25}),
        )
        conn.execute(
            """
            UPDATE jobs SET lease_token = gen_random_uuid(), lease_expires_at = now() + interval '30 seconds',
                   started_at = now() - interval '25 seconds', created_at = now() - interval '40 seconds'
             WHERE id = %s
            """,
            (running["id"],),
        )
        _event(conn, running["id"], "created", None, {"target": box1.id}, 40)
        _event(conn, running["id"], "claimed", box1.id, None, 38)
        _event(conn, running["id"], "preempt_requested", None, {"role": "train"}, 31)
        _event(conn, running["id"], "released", box1.id, {"status": "queued", "reason": "drain"}, 30)
        _event(conn, running["id"], "claimed", box1.id, None, 25)

        box2 = insert_worker(conn, "box2", role="idle")
        _machine(conn, box2.id, 3.0, 912, 3934)
        conn.execute("UPDATE workers SET desired_role = 'backtest', role_epoch = 2 WHERE id = %s", (box2.id,))

        box3 = insert_worker(conn, "box3", role="idle")
        _machine(conn, box3.id, 0.0, 702, 3934)
        set_heartbeat_age(conn, box3.id, 600)

        done = insert_job(conn, "sleep", status="succeeded", progress=1.0, target_worker_id=box2.id,
                          params=Jsonb({"seconds": 30}), checkpoint=Jsonb({"elapsed": 30}), result=Jsonb({"slept": 30}))
        conn.execute(
            "UPDATE jobs SET created_at = now() - interval '12 minutes', started_at = now() - interval '11 minutes',"
            " finished_at = now() - interval '10 minutes' WHERE id = %s", (done["id"],),
        )
        _event(conn, done["id"], "claimed", box2.id, None, 660)
        _event(conn, done["id"], "succeeded", box2.id, None, 600)
        failed = insert_job(conn, "sleep", status="failed", progress=0.6, target_worker_id=box3.id, expiries=3,
                            params=Jsonb({"seconds": 600}), checkpoint=Jsonb({"elapsed": 360}),
                            error="failed after 3 expiries (last: out of memory)")
        conn.execute(
            "UPDATE jobs SET created_at = now() - interval '2 hours', started_at = now() - interval '2 hours',"
            " finished_at = now() - interval '90 minutes' WHERE id = %s", (failed["id"],),
        )
        insert_job(conn, "sleep", params=Jsonb({"seconds": 120}))
        add_audit(conn, "set_role", box2.id, "owner@example.com", {"desired_role": "idle"}, {"desired_role": "backtest"})
        add_audit(conn, "settings_changed", "max_bet_cents", "owner@example.com", {"max_bet_cents": 2500}, {"max_bet_cents": 2000})
        return {"box1": box1.id, "box2": box2.id, "box3": box3.id, "running": str(running["id"])}


def touch(url: str, box1: str) -> None:
    """Keep box1 and box2 online and the lease alive while the captures run."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("UPDATE workers SET last_heartbeat_at = now() - interval '3 seconds' WHERE id = %s", (box1,))
        conn.execute("UPDATE workers SET last_heartbeat_at = now() - interval '1 second' WHERE name = 'box2'")
        conn.execute("UPDATE jobs SET lease_expires_at = now() + interval '30 seconds' WHERE status = 'leased'")
        touch_trading(conn)
        touch_live(conn)
