"""Row-height audit (docs/UI.md "Tests"): Models with 20 lineages and Trading with 20
assignments holding orders, rendered by Chromium at 390 px. Each page stays under six
phone screens (6 x 844 px) and no row is taller than 88 px, with the disclosures closed
as a visit finds them; opened, the rows inside keep the same limit. Skipped when
playwright or its Chromium is missing, like tests/hw/test_ui.py."""
from __future__ import annotations

from typing import Any

from tests.conftest import approved_order, insert_validated_model, stress_metrics, trade_setup
from tests.hw.conftest import PHONE
from tests.hw.ui_checks import open_disclosures, row_heights

ROWS = 20
SCREENS = 6
FLAGS = ([], ["overfit"], ["fragile"], ["regime_dependent"], ["fragile", "regime_dependent"])
STATUSES = ("candidate", "paper_ok", "live_eligible", "candidate", "paper_ok")


def _measure(browser: Any, url: str, kind: str) -> tuple[float, int, list[str]]:
    """(page height, rows of `kind`, problems) at 390x844 for one page."""
    context = browser.new_context(viewport=PHONE)
    try:
        page = context.new_page()
        page.goto(url, wait_until="networkidle")
        height = page.evaluate("document.documentElement.scrollHeight")
        count = page.locator(f'.row[data-row="{kind}"]').count()
        problems = row_heights(page, url)
        open_disclosures(page)
        problems += row_heights(page, f"{url} (opened)")
        return height, count, problems
    finally:
        context.close()


def test_models_with_twenty_lineages_fit_six_screens(browser, server, conn) -> None:
    for i in range(ROWS):
        insert_validated_model(
            conn, status=STATUSES[i % len(STATUSES)], params={"k": 20.0 + i, "hfa": 50.0 + i, "mov_scale": i % 2},
            stress=stress_metrics(flags=FLAGS[i % len(FLAGS)], seed=i + 1),
        )
    height, count, problems = _measure(browser, server.url + "/models", "model")
    assert count == ROWS, f"{count} model rows"
    assert not problems, problems
    assert height < SCREENS * PHONE["height"], f"Models is {height} px tall at 390 px (> {SCREENS} screens)"


def test_trading_with_twenty_assignments_and_their_orders_fits_six_screens(browser, server, conn) -> None:
    for i in range(ROWS):
        setup = trade_setup(conn, game_id=f"2026_05_A{i:02d}_B{i:02d}", kickoff_in_s=(24 + i) * 3600)
        approved_order(conn, setup, size=5 + i)
    height, count, problems = _measure(browser, server.url + "/trading", "assignment")
    assert count == ROWS, f"{count} assignment rows"
    assert conn.execute("SELECT count(*) AS n FROM orders").fetchone()["n"] == ROWS
    assert not problems, problems
    assert height < SCREENS * PHONE["height"], f"Trading is {height} px tall at 390 px (> {SCREENS} screens)"
