"""Browser tests for host/static/app.js: Chromium through Playwright against a real
server on the per-test database. Skipped when playwright or its Chromium is missing
(PLAYWRIGHT_BROWSERS_PATH, default /opt/pw-browsers); never downloads a browser."""
from __future__ import annotations

import os
import re
from typing import Any, Iterator

import pytest

from tests.hw.serve import Server

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright is not installed")
    with sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure means no browser here
            pytest.skip(f"no Chromium for Playwright: {exc}")
        yield chromium
        chromium.close()


@pytest.fixture
def server(test_db_url: str) -> Iterator[Server]:
    with Server(test_db_url) as live:
        yield live


@pytest.fixture
def page(browser: Any, server: Server) -> Iterator[Any]:
    context = browser.new_context(viewport=PHONE)
    try:
        yield context.new_page()
    finally:
        context.close()


def _card(page: Any, worker_id: str) -> str:
    return page.text_content(f'[data-worker="{worker_id}"]')


def test_role_select_autosubmits_and_the_switching_line_clears_on_ack(page, server, conn, make_worker) -> None:
    w = make_worker("box1")
    page.goto(server.url + "/", wait_until="networkidle")
    with page.expect_navigation():
        page.select_option(f"#role-{w.id}", "train")
    assert page.url == server.url + "/", "the redirect carries no flash in the query string"
    assert page.text_content(".flash") == "box1: switching to train (epoch 2)"
    assert "switching to train (epoch 2)" in _card(page, w.id)
    assert page.is_disabled(f"#role-{w.id}"), "an online worker's select is held while it acks"
    conn.execute(
        "UPDATE workers SET reported_role = 'train', acked_epoch = 2, last_heartbeat_at = now() WHERE id = %s", (w.id,)
    )
    page.wait_for_function(f"!document.querySelector('[data-worker=\"{w.id}\"] .switching')", timeout=8_000)
    assert page.is_enabled(f"#role-{w.id}") and page.input_value(f"#role-{w.id}") == "train"
    page.wait_for_selector(".flash", state="detached", timeout=10_000)


def test_focused_select_holds_the_grid_briefly_and_the_counter_reports_the_grid_age(page, server, conn, make_worker) -> None:
    """MEDIUM: the 'updated N s ago' text follows the fleet grid, not the topbar fetch,
    and a select that keeps focus after its picker was dismissed stops holding the grid
    after 15 s."""
    w = make_worker("box1")
    page.goto(server.url + "/", wait_until="networkidle")
    page.focus(f"#role-{w.id}")
    conn.execute("UPDATE workers SET cpu_pct = 77 WHERE id = %s", (w.id,))
    page.wait_for_timeout(11_500)
    assert "CPU 77%" not in _card(page, w.id), "the grid is held while the select has focus"
    counter = page.text_content("#updated")
    assert re.fullmatch(r"updated 1[0-9] s ago", counter), f"topbar fetch must not reset the counter: {counter!r}"
    page.wait_for_function(
        f"document.querySelector('[data-worker=\"{w.id}\"]').textContent.includes('CPU 77%')", timeout=12_000
    )
    page.wait_for_function(
        "/^updated [0-4] s ago$/.test(document.getElementById('updated').textContent)", timeout=3_000
    ), "the grid refresh resets the counter"


def test_connection_lost_shows_in_the_sticky_bar_and_dims_the_grid(page, server, make_worker) -> None:
    """MEDIUM: a failed fetch is visible without scrolling and the stale data is marked."""
    make_worker("box1")
    page.goto(server.url + "/", wait_until="networkidle")
    page.route("**/fragments/fleet", lambda route: route.abort())
    page.wait_for_function("document.getElementById('updated').textContent === 'connection lost'", timeout=8_000)
    assert page.evaluate("document.body.classList.contains('conn-lost')")
    box = page.locator("#updated").bounding_box()
    assert box is not None and box["y"] < 140, f"the indicator sits in the top bar, not the footer: {box}"
    assert page.evaluate("getComputedStyle(document.querySelector('.dot')).backgroundColor") == "rgb(156, 163, 175)"
    assert float(page.evaluate("getComputedStyle(document.getElementById('fleet-grid')).opacity")) < 1
    page.unroute("**/fragments/fleet")
    page.wait_for_function("/^updated [0-2] s ago$/.test(document.getElementById('updated').textContent)", timeout=8_000)
    assert not page.evaluate("document.body.classList.contains('conn-lost')")


def test_without_javascript_copy_buttons_hide_and_set_shows(browser, server, make_worker) -> None:
    """LOW: `.js-only` must win against `.btn` so the Copy buttons are gone with JS off."""
    w = make_worker("box1")
    context = browser.new_context(viewport=PHONE, java_script_enabled=False)
    try:
        page = context.new_page()
        page.goto(server.url + "/")
        assert page.locator(f'[data-worker="{w.id}"] button.js-hide').is_visible(), "the Set button shows"
        assert page.locator("#updated").bounding_box() is None, "no freshness line without the script"
        page.goto(server.url + "/settings")
        with page.expect_navigation():
            page.click("form#enroll button")
        assert page.locator("button.js-only").count() == 3
        assert all(not page.locator("button.js-only").nth(i).is_visible() for i in range(3))
    finally:
        context.close()
