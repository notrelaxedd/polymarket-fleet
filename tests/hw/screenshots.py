"""Dashboard screenshots and layout checks with Playwright. A dev tool, not a pytest test.

    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers .venv/bin/python tests/hw/screenshots.py [OUT_DIR]

It seeds a throwaway database: three workers and a few jobs, then the rows of
tests/hw/seed_step3.py (the nflverse fixture, a real search, its models, a trained
child, a backtest), seed_step6.py (validation and stress tables: two lineages ranked,
one flagged overfit, one "not validated", a validate job), seed_step4.py (games with
sim markets, a trade worker, assignments, orders in every state, a fill, a settled
bet), the paper CLV interval, seed_step6b.py (an epa_blend lineage ranked on
snapshot replay CLV, snapshot columns on two more, a snapshot backtest job, a partly
sold position with a filled and an open sell, a second position) and, once
check_step6b has passed, seed_step6c.py (two ingame_wp lineages, a third-quarter game
with a fresh game state, an in-game assignment holding a partly filled in-game buy,
ESPN feed lag rows, yesterday's game settled with in-game bets; check_step6c). It
serves the app with FLEET_DEV=1 on a free port and captures home (/), fleet (/fleet),
jobs and its Done tab, job detail (sleep, search, backtest, validate, snapshot replay),
models (with the In-game models group), model detail (the flagged, the
snapshot-ranked and the ingame_wp ones too), trading (the in-game line, the in-game
chips, the In-game feed group), settings (with the In-game group), the market probe
page (the Exchange group's Probe button) and the game-state probe page (its "Probe
game state" form, answered by a stubbed ESPN), plus the validate form and the New
assignment form opened from a model and from an ingame_wp model, at 390x844 and
1280x800 in light and dark;
then (seed_step5.py) settings, trading, fleet and home with live on, after an auto-kill,
after a hand POST /kill, and trading after the reset.

Every capture runs the docs/UI.md assertions (tests/hw/ui_checks.py): no horizontal
overflow, every .chip has text, every <details> has a <summary> with text; at 390 px
also the h1 and a .stat inside the first 844 px (not on the three captures that open a
form at the top on purpose), no .row taller than 88 px, 44 px tap targets for buttons,
row links, selects, inputs, menu items and summaries, and no chip cut in a row title or
flag line, measured again with every disclosure opened; then every "..." menu is opened
and each item must be the topmost thing under its centre, as loaded and with
body.conn-lost (Fleet holds an offline worker that is not the last card). On the Models
and model pages at 390 no non-stacked table scrolls sideways, no in-game reason line is
cut and no pre-game row has an in-game line (step 6C review). At 1280 the Models rows
keep their title, number and menu inside the row; on Fleet the fragment refresh must
reset "updated N s ago". It exits 1 on any problem. It needs the Chromium build the installed Playwright expects under
PLAYWRIGHT_BROWSERS_PATH; it never downloads a browser.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")

from tests.hw.seed_shots import drop_database, fresh_database, seed, seed_6c, touch  # noqa: E402
from tests.hw.seed_step5 import auto_kill, seed_live  # noqa: E402
from tests.hw.seed_step6b import check_step6b  # noqa: E402
from tests.hw.seed_step6c import check_step6c, probe  # noqa: E402
from tests.hw.serve import Server  # noqa: E402
from tests.hw.ui_checks import check_page, first_screen  # noqa: E402
from tests.pagecheck import mode_pill, topbar  # noqa: E402
from tests.pagecheck import page as parse  # noqa: E402

DEFAULT_OUT = Path(os.environ.get("SCREENSHOT_DIR", "/tmp/screenshots"))
VIEWPORTS = {"390": (390, 844), "1280": (1280, 800)}
SCHEMES = ("light", "dark")
# captures that open a form at the top on purpose: the form, not a stat, is their first screen
FORM_FIRST = {"jobs-validate-form", "trading-assign", "trading-assign-ingame"}


# ---------------------------------------------------------------- captures and checks


PROBE = "probe:/trading"  # a POST result: open Trading, then press Probe markets in the Exchange group
PROBE_GAMESTATE = "probe-gamestate:/trading"  # the same with the "Probe game state" form (step 6C)


def open_page(page: Any, server_url: str, path: str) -> None:
    """Load `path`; a "probe:" path loads the page after it and presses Probe markets,
    a "probe-gamestate:" path submits the Probe game state form there."""
    if path.startswith("probe-gamestate:"):
        page.goto(server_url + path.removeprefix("probe-gamestate:"), wait_until="networkidle")
        probe(page)
        assert page.locator('main[data-page="probe"] dd.c-game').count() == 1, "the game-state probe page"
        return
    if path.startswith("probe:"):
        page.goto(server_url + path.removeprefix("probe:"), wait_until="networkidle")
        page.click('details[data-key="trading-exchange"] > summary')
        with page.expect_navigation(wait_until="networkidle"):
            page.click('[data-action="probe"] button')
        assert page.locator('main[data-page="probe"]').count() == 1, "the probe page"
        return
    page.goto(server_url + path, wait_until="networkidle")


def pages(ids: dict[str, str]) -> list[tuple[str, str]]:
    return [
        ("home", "/"), ("fleet", "/fleet"), ("jobs", "/jobs"), ("jobs-done", "/jobs?tab=done"), ("job-detail", f"/jobs/{ids['running']}"), ("settings", "/settings"),
        ("models", "/models"), ("model-detail", f"/models/{ids['model']}"), ("model-overfit", f"/models/{ids['overfit_model']}"),
        ("job-search", f"/jobs/{ids['search_job']}"), ("job-backtest", f"/jobs/{ids['backtest_job']}"),
        ("job-validate", f"/jobs/{ids['validate_job']}"), ("model-snapshot", f"/models/{ids['epa_model']}"),
        ("job-replay", f"/jobs/{ids['replay_job']}"),
        ("jobs-validate-form", f"/jobs?validate_model={ids['model']}"),
        ("trading", "/trading"), ("trading-assign", f"/trading?model={ids['model']}"), ("probe", PROBE),
        ("model-ingame", f"/models/{ids['ingame_model']}"),
        ("trading-assign-ingame", f"/trading?ingame_model={ids['ingame_model']}"), ("probe-gamestate", PROBE_GAMESTATE),
    ]


def check_models_desktop(page: Any, problems: list[str]) -> None:
    """At 1280 px every Models row keeps its title, headline number and "..." menu inside
    the row box, and the title is not squeezed below 200 px."""
    found = page.evaluate(
        """() => {
             const rows = Array.from(document.querySelectorAll('[data-list="ranked"] .row'));
             const narrow = rows.map(r => r.querySelector('.row-title').getBoundingClientRect().width).filter(w => w < 200);
             const outside = rows.filter(r => {
               const box = r.getBoundingClientRect();
               return Array.from(r.querySelectorAll('.row-value, details.menu > summary'))
                 .some(el => el.getBoundingClientRect().right > box.right + 1);
             }).length;
             return [narrow, outside, rows.length];
           }"""
    )
    narrow, outside, rows = found
    if narrow:
        problems.append(f"models at 1280: {len(narrow)} row titles narrower than 200 px ({[round(w) for w in narrow]})")
    if outside or not rows:
        problems.append(f"models at 1280: {outside} of {rows} rows push their number or menu outside the row")


def check_phone_tables(page: Any, name: str, problems: list[str]) -> None:
    """At 390 px (disclosures open) a non-stacked table on a Models page fits its wrapper
    (the in-game calibration once hid its vegas_wp column), the in-game reason (an
    .ingame-row's second grey line) wraps instead of being cut, and no pre-game row has an
    in-game line: settlement credits in-game bets to the ingame_wp lineage (6C review)."""
    wide, cut, pregame = page.evaluate(
        """() => [Array.from(document.querySelectorAll('.table-wrap'))
                    .filter(el => !el.querySelector('table.stack') && el.scrollWidth > el.clientWidth + 1)
                    .map(el => [el.querySelector('table').className, el.scrollWidth, el.clientWidth]),
                  Array.from(document.querySelectorAll('.ingame-row .row-meta2'))
                    .filter(el => el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1).length,
                  document.querySelectorAll('[data-row="model"] .row-ingame').length]"""
    )
    for cls, scroll_w, client_w in wide:
        problems.append(f"{name}: table.{cls} scrolls sideways ({scroll_w} > {client_w})")
    if cut:
        problems.append(f"{name}: {cut} in-game reason lines cut")
    if pregame:
        problems.append(f"{name}: {pregame} pre-game model rows carry an in-game line")


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

        def shoot(path: str, name: str, width: str, scheme: str) -> None:
            w, h = VIEWPORTS[width]
            context = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme)
            page = context.new_page()
            touch(database_url, ids["box1"])
            open_page(page, server_url, path)
            target = out / f"{name}-{width}-{scheme}.png"
            page.screenshot(path=str(target), full_page=True)
            written.append(str(target))
            phone = width == "390"
            found = check_page(page, f"{name}-{width}-{scheme}", phone=phone)
            if name in FORM_FIRST:
                found = [p for p in found if p not in first_screen(page, f"{name}-{width}-{scheme}")]
            problems.extend(found)
            if phone and name.startswith("model"):
                check_phone_tables(page, f"{name}-{width}-{scheme}", problems)
            if name == "fleet" and phone and scheme == "light":
                page.reload(wait_until="networkidle")
                check_refresh_counter(page, problems)
            if name == "models" and width == "1280" and scheme == "light":
                check_models_desktop(page, problems)
            context.close()

        def shoot_all(captures: list[tuple[str, str]]) -> None:
            for name, path in captures:
                for width in VIEWPORTS:
                    for scheme in SCHEMES:
                        shoot(path, name, width, scheme)

        check_step6b(server_url, ids)  # before the 6C rows add a third position
        ids.update(seed_6c(database_url, ids))
        check_step6c(server_url, ids)
        shoot_all(pages(ids))

        # Step 5: live on (the settings group on, the live assignment, the smoke order, the
        # LIVE pill), then the auto-kill the exchange process pulls, then the reset.
        seed_live(database_url, ids["trader"])
        with httpx.Client(base_url=server_url, trust_env=False) as client:
            settings = parse(client.get("/settings").text)
            assert settings.one("[data-live-state]").attr("data-live-state") == "on" and mode_pill(settings) == "LIVE", "live is on"
            assert parse(client.get("/trading").text).has('[data-chip="smoke"]'), "the smoke order is flagged"
        shoot_all([("settings-live", "/settings"), ("trading-live", "/trading"), ("fleet-live", "/fleet"), ("home-live", "/")])
        auto_kill(database_url)
        with httpx.Client(base_url=server_url, trust_env=False) as client:
            reason = topbar(parse(client.get("/").text)).one("[data-auto-kill]")
            assert reason.attr("data-auto-kill") == "clock_skew", "the bar names the auto-kill reason"
        shoot_all([("fleet-autokill", "/fleet"), ("home-autokill", "/"), ("settings-autokill", "/settings"), ("trading-autokill", "/trading")])
        with httpx.Client(base_url=server_url, trust_env=False) as client:
            resp = client.post("/kill/reset", data={"confirm": "RESUME"}, headers={"Origin": server_url}, follow_redirects=False)
            assert resp.status_code == 303, resp.text
            assert "KILLED" not in topbar(parse(client.get("/").text)).text
            assert parse(client.get("/settings").text).one("[data-live-state]").attr("data-live-state") == "off"

        with httpx.Client(base_url=server_url, trust_env=False) as client:
            resp = client.post("/kill", data={}, headers={"Origin": server_url}, follow_redirects=False)
            assert resp.status_code == 303, resp.text
            assert "TRADING KILLED" in topbar(parse(client.get("/").text)).text
        for scheme in SCHEMES:
            shoot("/fleet", "fleet-killed", "390", scheme)
            shoot("/", "home-killed", "390", scheme)
            shoot("/trading", "trading-killed", "390", scheme)
        with httpx.Client(base_url=server_url, trust_env=False) as client:
            resp = client.post("/kill/reset", data={"confirm": "RESUME"}, headers={"Origin": server_url}, follow_redirects=False)
            assert resp.status_code == 303, resp.text
            assert parse(client.get("/trading").text).has('[data-action="activate-all-paper"]')
        for scheme in SCHEMES:
            shoot("/trading", "trading-reset", "390", scheme)
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
