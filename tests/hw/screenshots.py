"""Dashboard screenshots and layout checks with Playwright. A dev tool, not a pytest test.

    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers .venv/bin/python tests/hw/screenshots.py [OUT_DIR]

It creates a throwaway database, seeds three workers and a few jobs straight through
SQL plus the step 3 rows (the nflverse fixture, a real six-candidate search and its
models, a trained child, a backtest; tests/hw/seed_step3.py) and the step 4 rows (two
upcoming games with sim markets and books, an unmatched market, assignments held by a
fourth worker in the trade role, open, resting, rejected and cancelled orders, a fill,
a settled game with its bet and paper score, an exchange heartbeat;
tests/hw/seed_step4.py) and the step 6 rows (tests/hw/seed_step6.py: validation-era
metrics and stress tables on the search lineages, two ranked, one flagged overfit, one
left "not validated", a finished validate job, the paper CLV interval), serves the app
with FLEET_DEV=1 on a free port, and captures
the fleet (with per-worker P&L), jobs, job detail, settings (with the Trading group),
models (with the validation columns and flag chips), model detail (with the Robustness
section), the flagged model, search result, validate result, the Jobs page with the
validate form open, and trading pages at
phone (390x844) and laptop (1280x800) widths in the light and dark colour schemes,
plus the trading page with the create form open, then (step 5, tests/hw/seed_step5.py)
the settings, trading and fleet pages with live on (the Live trading group on, a live
assignment and its exchange order, a resting smoke order, the LIVE pill), the fleet,
settings and trading pages after an auto-kill (the red bar naming the reason), then
the fleet and trading pages after a hand POST /kill and the trading page after the
reset (the "Activate all paper" button). At phone width it also asserts: no horizontal scroll on any page, every visible button,
select and link inside a worker card and every visible form control (a checkbox counts
through its label) is at least 40 px tall, and the fragment refresh resets the
"updated N s ago" counter. Needs the playwright package in the venv and the Chromium
build it expects under PLAYWRIGHT_BROWSERS_PATH; it never downloads a browser.
"""
from __future__ import annotations

import os
import re
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
from tests.hw.seed_step3 import seed_models  # noqa: E402
from tests.hw.seed_step4 import seed_trading, touch_trading  # noqa: E402
from tests.hw.seed_step5 import auto_kill, seed_live, touch_live  # noqa: E402
from tests.hw.seed_step6 import seed_paper_ci, seed_validation  # noqa: E402
from tests.hw.serve import Server  # noqa: E402

DEFAULT_OUT = Path(os.environ.get("SCREENSHOT_DIR", "/tmp/screenshots"))
VIEWPORTS = {"390": (390, 844), "1280": (1280, 800)}
SCHEMES = ("light", "dark")
MIN_TAP_PX = 40
CARD_TARGETS = ".card.worker button, .card.worker select, .card.worker a"
FORM_TARGETS = "form button, form select, form input:not([type=hidden]):not([type=checkbox]), form textarea, form label.check"


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
    """Three workers (running, switching, offline), two finished jobs, one queued job,
    then the step 3 rows (a finished search with models, a running search on box1),
    then the step 4 rows (a trade worker, games, markets, assignments, orders, a bet)."""
    ids = _seed_fleet(url)
    ids.update(seed_models(url, ids["box2"], ids["box1"]))
    ids.update(seed_validation(url, ids["box2"]))
    ids.update(seed_trading(url, ids["model"]))
    seed_paper_ci(url)
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


# ---------------------------------------------------------------- captures and checks


def pages(ids: dict[str, str]) -> list[tuple[str, str]]:
    return [
        ("fleet", "/"), ("jobs", "/jobs"), ("job-detail", f"/jobs/{ids['running']}"), ("settings", "/settings"),
        ("models", "/models"), ("model-detail", f"/models/{ids['model']}"), ("model-overfit", f"/models/{ids['overfit_model']}"),
        ("job-search", f"/jobs/{ids['search_job']}"), ("job-backtest", f"/jobs/{ids['backtest_job']}"),
        ("job-validate", f"/jobs/{ids['validate_job']}"),
        ("jobs-validate-form", f"/jobs?validate_model={ids['model']}"),
        ("trading", "/trading"), ("trading-assign", f"/trading?model={ids['model']}"),
    ]


def check_phone_layout(page: Any, name: str, problems: list[str]) -> None:
    """No horizontal scroll; tap targets inside worker cards and forms are at least MIN_TAP_PX tall."""
    scroll_w, inner_w = page.evaluate("[document.scrollingElement.scrollWidth, window.innerWidth]")
    if scroll_w > inner_w:
        problems.append(f"{name}: horizontal scroll, scrollWidth {scroll_w} > innerWidth {inner_w}")
    short = page.evaluate(
        """(sel) => Array.from(document.querySelectorAll(sel))
             .filter(el => el.getClientRects().length > 0)
             .map(el => [el.tagName, (el.getAttribute('name') || el.textContent || '').trim().slice(0, 30), el.getBoundingClientRect().height])
             .filter(([, , h]) => h < %d)""" % MIN_TAP_PX,
        f"{CARD_TARGETS}, {FORM_TARGETS}",
    )
    for tag, text, height in short:
        problems.append(f"{name}: {tag} '{text}' is {height:.0f} px tall (< {MIN_TAP_PX})")


def check_models_desktop(page: Any, problems: list[str]) -> None:
    """At 1280 px the Models table keeps every summary at least 200 px wide and every
    action button inside its table's visible box (the table scrolls inside .table-wrap,
    so the page-level scroll check would not see a squeezed column)."""
    found = page.evaluate(
        """() => {
             const narrow = Array.from(document.querySelectorAll('table.models td.c-summary'))
               .map(td => td.getBoundingClientRect().width).filter(w => w < 200);
             const hidden = Array.from(document.querySelectorAll('table.models td.c-actions .btn')).filter(btn => {
               const wrap = btn.closest('.table-wrap');
               return wrap && btn.getBoundingClientRect().right > wrap.getBoundingClientRect().right + 1;
             }).length;
             const buttons = document.querySelectorAll('table.models td.c-actions .btn').length;
             return [narrow, hidden, buttons];
           }"""
    )
    narrow, hidden, buttons = found
    if narrow:
        problems.append(f"models at 1280: {len(narrow)} summary cells narrower than 200 px ({[round(w) for w in narrow]})")
    if hidden or not buttons:
        problems.append(f"models at 1280: {hidden} of {buttons} action buttons outside the visible table")


def check_refresh_counter(page: Any, problems: list[str]) -> None:
    """The footer counts up, a fragment refresh resets it."""
    page.wait_for_function("/^updated [3-9] s ago$/.test(document.getElementById('updated').textContent)", timeout=12_000)
    with page.expect_response(re.compile(r"/fragments/fleet$"), timeout=12_000):
        pass
    try:
        page.wait_for_function("/^updated [01] s ago$/.test(document.getElementById('updated').textContent)", timeout=3_000)
    except Exception:  # noqa: BLE001  (the playwright TimeoutError is what we expect here)
        problems.append("fleet: the fragment refresh did not reset the 'updated N s ago' text")


def capture_all(server_url: str, database_url: str, ids: dict[str, str], out: Path) -> list[str]:
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    written: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        def shoot(path: str, name: str, width: str, scheme: str, check: bool) -> None:
            w, h = VIEWPORTS[width]
            context = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme)
            page = context.new_page()
            touch(database_url, ids["box1"])
            page.goto(server_url + path, wait_until="networkidle")
            target = out / f"{name}-{width}-{scheme}.png"
            page.screenshot(path=str(target), full_page=True)
            written.append(str(target))
            if check:
                check_phone_layout(page, name, problems)
                if name == "fleet":
                    check_refresh_counter(page, problems)
            if name == "models" and width == "1280" and scheme == "light":
                check_models_desktop(page, problems)
            context.close()

        def shoot_all(captures: list[tuple[str, str]]) -> None:
            for name, path in captures:
                for width in VIEWPORTS:
                    for scheme in SCHEMES:
                        shoot(path, name, width, scheme, check=(width == "390" and scheme == "light"))

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
