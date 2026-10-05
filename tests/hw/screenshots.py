"""Dashboard screenshots and layout checks with Playwright. A dev tool, not a pytest test.

    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers .venv/bin/python tests/hw/screenshots.py [OUT_DIR]

It seeds a throwaway database: three workers and a few jobs, then the rows of
tests/hw/seed_step3.py (the nflverse fixture, a real search, its models, a trained
child, a backtest), seed_step6.py (validation and stress tables: two lineages ranked,
one flagged overfit, one "not validated", a validate job), seed_step4.py (games with
sim markets, a trade worker, assignments, orders in every state, a fill, a settled
bet), the paper CLV interval, seed_step6b.py (an epa_blend lineage ranked on snapshot
replay CLV, snapshot columns on two more, a snapshot backtest job, a partly sold
position with a filled and an open sell, a second position) and, once check_step6b
has passed, seed_step6c.py (two ingame_wp lineages, a third-quarter game with a fresh
game state, an in-game assignment holding a partly filled in-game buy, ESPN feed lag
rows, yesterday's game settled with in-game bets). It serves the app with FLEET_DEV=1
on a free port and captures fleet, jobs (backtest form with its price source), job
detail, settings (Trading, Snapshot replay, nflverse signals, In-game), models
(validation, paper and snapshot columns, In-game models), model detail, the flagged
model, the snapshot-ranked model, the ingame_wp model, the search, backtest, validate
and snapshot backtest results, the validate form, trading (positions, sell chips, live
score, in-game chips, In-game feed), its create form (also with an ingame_wp model
preselected) and the game-state probe page (the "Probe game state" form submitted
against a stubbed ESPN payload), at 390x844 and 1280x800 in light and dark; then
(seed_step5.py) settings, trading and fleet with live on, after an auto-kill, after a
hand POST /kill, and trading after the reset. At phone width it fails on horizontal
scroll, on a visible button, select or link in a worker card or a form control (a
checkbox through its label) under 40 px tall, and when the fragment refresh does not
reset "updated N s ago". It needs the Chromium build the installed Playwright expects
under PLAYWRIGHT_BROWSERS_PATH; it never downloads a browser.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")

from host import db  # noqa: E402
from host.events import add_audit  # noqa: E402
from tests.conftest import ADMIN_URL, db_url, insert_job, insert_worker, set_heartbeat_age  # noqa: E402
from tests.hw.layout_checks import (  # noqa: E402
    check_desktop_tables,
    check_models_desktop,
    check_phone_layout,
    check_phone_tables,
    check_refresh_counter,
)
from tests.hw.seed_step3 import seed_models  # noqa: E402
from tests.hw.seed_step4 import seed_trading, touch_trading  # noqa: E402
from tests.hw.seed_step5 import auto_kill, seed_live, touch_live  # noqa: E402
from tests.hw.seed_step6 import seed_paper_ci, seed_validation  # noqa: E402
from tests.hw.seed_step6b import check_step6b, seed_sells, seed_snapshot  # noqa: E402
from tests.hw.seed_step6c import check_step6c, probe, seed_ingame, stub_espn, touch_ingame  # noqa: E402
from tests.hw.serve import Server  # noqa: E402

DEFAULT_OUT = Path(os.environ.get("SCREENSHOT_DIR", "/tmp/screenshots"))
VIEWPORTS = {"390": (390, 844), "1280": (1280, 800)}
SCHEMES = ("light", "dark")


# ---------------------------------------------------------------- database and seed


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
    4 and 6B rows (the module docstring lists them; capture_all adds the 6C rows after
    check_step6b); returns the ids the captures need."""
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
        touch_ingame(conn)


# ---------------------------------------------------------------- captures and checks


def pages(ids: dict[str, str]) -> list[tuple[Any, ...]]:
    """(name, path) or (name, path, action): the action runs on the loaded page before the capture."""
    return [
        ("fleet", "/"), ("jobs", "/jobs"), ("job-detail", f"/jobs/{ids['running']}"), ("settings", "/settings"),
        ("models", "/models"), ("model-detail", f"/models/{ids['model']}"), ("model-overfit", f"/models/{ids['overfit_model']}"),
        ("job-search", f"/jobs/{ids['search_job']}"), ("job-backtest", f"/jobs/{ids['backtest_job']}"),
        ("job-validate", f"/jobs/{ids['validate_job']}"), ("model-snapshot", f"/models/{ids['epa_model']}"),
        ("job-replay", f"/jobs/{ids['replay_job']}"),
        ("jobs-validate-form", f"/jobs?validate_model={ids['model']}"),
        ("trading", "/trading"), ("trading-assign", f"/trading?model={ids['model']}"),
        ("model-ingame", f"/models/{ids['ingame_model']}"), ("trading-assign-ingame", f"/trading?model={ids['ingame_model']}"),
        ("probe-gamestate", "/trading", probe),
    ]


def capture_all(server_url: str, database_url: str, ids: dict[str, str], out: Path) -> list[str]:
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    written: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        def shoot(path: str, name: str, width: str, scheme: str, check: bool, act: Any = None) -> None:
            w, h = VIEWPORTS[width]
            context = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme)
            page = context.new_page()
            touch(database_url, ids["box1"])
            page.goto(server_url + path, wait_until="networkidle")
            if act is not None:
                act(page)
            target = out / f"{name}-{width}-{scheme}.png"
            page.screenshot(path=str(target), full_page=True)
            written.append(str(target))
            if check:
                check_phone_layout(page, name, problems)
                if name in ("models", "model-ingame"):
                    check_phone_tables(page, name, problems)
                if name == "fleet":
                    check_refresh_counter(page, problems)
            if width == "1280" and scheme == "light" and name.startswith("models"):
                check_desktop_tables(page, name, problems)
            if name == "models" and width == "1280" and scheme == "light":
                check_models_desktop(page, problems)
            context.close()

        def shoot_all(captures: list[tuple[Any, ...]]) -> None:
            for name, path, *act in captures:
                for width in VIEWPORTS:
                    for scheme in SCHEMES:
                        shoot(path, name, width, scheme, check=(width == "390" and scheme == "light"), act=act[0] if act else None)

        check_step6b(server_url, ids)  # before the 6C rows add a third position
        ids.update(seed_ingame(database_url, ids["trader"], ids["model"]))
        stub_espn()
        check_step6c(server_url, ids)
        shoot_all(pages(ids))

        # Step 5: live on (the settings group on, the live assignment, the smoke order, the
        # LIVE pill), then the auto-kill the exchange process pulls, then the reset.
        seed_live(database_url, ids["trader"])
        with httpx.Client(base_url=server_url, trust_env=False) as client:
            page_html = client.get("/settings").text
            assert 'data-live-state="on"' in page_html and 'class="pill live">LIVE</span>' in page_html, "live is on"
            assert 'chip-smoke' in client.get("/trading").text, "the smoke order is flagged"
        shoot_all([("settings-live", "/settings"), ("trading-live", "/trading"), ("fleet-live", "/")])
        auto_kill(database_url)
        with httpx.Client(base_url=server_url, trust_env=False) as client:
            assert 'data-auto-kill="clock_skew"' in client.get("/").text, "the bar names the auto-kill reason"
        shoot_all([("fleet-autokill", "/"), ("settings-autokill", "/settings"), ("trading-autokill", "/trading")])
        with httpx.Client(base_url=server_url, trust_env=False) as client:
            resp = client.post("/kill/reset", data={"confirm": "RESUME"}, headers={"Origin": server_url}, follow_redirects=False)
            assert resp.status_code == 303, resp.text
            assert "KILLED" not in client.get("/").text and 'data-live-state="off"' in client.get("/settings").text

        with httpx.Client(base_url=server_url, trust_env=False) as client:
            resp = client.post("/kill", data={}, headers={"Origin": server_url}, follow_redirects=False)
            assert resp.status_code == 303, resp.text
            assert "TRADING KILLED" in client.get("/").text
        for scheme in SCHEMES:
            shoot("/", "fleet-killed", "390", scheme, check=(scheme == "light"))
            shoot("/trading", "trading-killed", "390", scheme, check=(scheme == "light"))
        with httpx.Client(base_url=server_url, trust_env=False) as client:
            resp = client.post("/kill/reset", data={"confirm": "RESUME"}, headers={"Origin": server_url}, follow_redirects=False)
            assert resp.status_code == 303, resp.text
            assert "Activate all paper" in client.get("/trading").text
        for scheme in SCHEMES:
            shoot("/trading", "trading-reset", "390", scheme, check=(scheme == "light"))
        browser.close()
    for line in problems:
        print("PROBLEM", line)
    if problems:
        raise SystemExit(1)
    return written


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    database_url = fresh_database()
    try:
        ids = seed(database_url)
        with Server(database_url) as server:
            for path in capture_all(server.url, database_url, ids, out):
                print(path)
    finally:
        drop_database(database_url)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
