"""Browser tests for host/static/app.js: Chromium through Playwright against a real
server on the per-test database. Skipped when playwright or its Chromium is missing
(PLAYWRIGHT_BROWSERS_PATH, default /opt/pw-browsers); never downloads a browser."""
from __future__ import annotations

import re
from typing import Any, Iterator

import pytest

from tests.hw.conftest import PHONE
from tests.hw.serve import Server
from tests.hw.ui_checks import menus_on_top


@pytest.fixture
def page(browser: Any, server: Server) -> Iterator[Any]:
    context = browser.new_context(viewport=PHONE)
    try:
        yield context.new_page()
    finally:
        context.close()


def _row(worker_id: str) -> str:
    """The selector of a worker's row (the step 7 data hooks)."""
    return f'[data-row="worker"][data-id="{worker_id}"]'


def _role(worker_id: str) -> str:
    return f'{_row(worker_id)} select[name="role"]'


def _card(page: Any, worker_id: str) -> str:
    return page.text_content(_row(worker_id))


def _fleet(page: Any, server: Server, **kwargs: Any) -> str:
    """Open the Fleet card page: /fleet/list since the 3D page took /fleet, else / (steps 1 to 6)."""
    for path in ("/fleet/list", "/"):
        response = page.goto(server.url + path, **kwargs)
        if response is not None and response.status == 200:
            return server.url + path
    raise AssertionError("no Fleet page")


def test_role_select_autosubmits_and_the_switching_line_clears_on_ack(page, server, conn, make_worker) -> None:
    w = make_worker("box1")
    fleet = _fleet(page, server, wait_until="networkidle")
    with page.expect_navigation():
        page.select_option(_role(w.id), "train")
    assert page.url == fleet, "the redirect carries no flash in the query string"
    assert page.text_content("[data-flash]") == "box1: switching to Training (epoch 2)"
    assert "switching to Training (epoch 2)" in _card(page, w.id)
    assert page.is_disabled(_role(w.id)), "an online worker's select is held while it acks"
    conn.execute(
        "UPDATE workers SET reported_role = 'train', acked_epoch = 2, last_heartbeat_at = now() WHERE id = %s", (w.id,)
    )
    page.wait_for_function(f"!document.querySelector('{_row(w.id)}').textContent.includes('switching to')", timeout=8_000)
    assert page.is_enabled(_role(w.id)) and page.input_value(_role(w.id)) == "train"
    page.wait_for_selector("[data-flash]", state="detached", timeout=10_000)


def test_focused_select_holds_the_grid_briefly_and_the_counter_reports_the_grid_age(page, server, conn, make_worker) -> None:
    """MEDIUM: the 'updated N s ago' text follows the fleet grid, not the topbar fetch,
    and a select that keeps focus after its picker was dismissed stops holding the grid
    after 15 s."""
    w = make_worker("box1")
    _fleet(page, server, wait_until="networkidle")
    page.focus(_role(w.id))
    conn.execute("UPDATE workers SET cpu_pct = 77 WHERE id = %s", (w.id,))
    page.wait_for_timeout(11_500)
    assert "CPU 77%" not in _card(page, w.id), "the grid is held while the select has focus"
    counter = page.text_content("#updated")
    assert re.fullmatch(r"updated 1[0-9] s ago", counter), f"topbar fetch must not reset the counter: {counter!r}"
    page.wait_for_function(
        f"document.querySelector('{_row(w.id)}').textContent.includes('CPU 77%')", timeout=12_000
    )
    page.wait_for_function(
        "/^updated [0-4] s ago$/.test(document.getElementById('updated').textContent)", timeout=3_000
    ), "the grid refresh resets the counter"


def test_connection_lost_shows_in_the_sticky_bar_and_dims_the_grid(page, server, make_worker) -> None:
    """MEDIUM: a failed fetch is visible without scrolling and the stale data is marked."""
    make_worker("box1")
    _fleet(page, server, wait_until="networkidle")
    page.route("**/fragments/fleet", lambda route: route.abort())
    page.wait_for_function("document.getElementById('updated').textContent === 'connection lost'", timeout=8_000)
    assert page.evaluate("document.body.classList.contains('conn-lost')")
    box = page.locator("#updated").bounding_box()
    assert box is not None and box["y"] < 140, f"the indicator sits in the top bar, not the footer: {box}"
    assert page.evaluate("getComputedStyle(document.querySelector('.dot')).backgroundColor") == "rgb(156, 163, 175)"
    assert float(page.evaluate("getComputedStyle(document.querySelector('[data-list=\"workers\"]')).opacity")) < 1
    page.unroute("**/fragments/fleet")
    page.wait_for_function("/^updated [0-2] s ago$/.test(document.getElementById('updated').textContent)", timeout=8_000)
    assert not page.evaluate("document.body.classList.contains('conn-lost')")


def test_without_javascript_copy_buttons_hide_and_set_shows(browser, server, make_worker) -> None:
    """LOW: `.js-only` must win against `.btn` so the Copy buttons are gone with JS off."""
    w = make_worker("box1")
    context = browser.new_context(viewport=PHONE, java_script_enabled=False)
    try:
        page = context.new_page()
        _fleet(page, server)
        assert page.locator(f"{_row(w.id)} button.js-hide").is_visible(), "the Set button shows"
        assert page.locator("#updated").bounding_box() is None, "no freshness line without the script"
        page.goto(server.url + "/settings")
        # the enroll form sits in the closed Fleet group; with JS off the summary still opens it
        page.click('details[data-key="settings-fleet"] > summary')
        with page.expect_navigation():
            page.click('[data-action="enroll"] button')
        assert page.locator("button.js-only").count() == 3
        assert all(not page.locator("button.js-only").nth(i).is_visible() for i in range(3))
    finally:
        context.close()


def test_menus_of_dimmed_workers_open_above_the_next_card(page, server, make_worker) -> None:
    """HIGH (step 7 review): an offline or disabled worker's card is dimmed with opacity,
    which makes a stacking context, so the next card's row painted over its open menu and
    a tap on Disable or Details landed on the next worker's role select. The card holding
    an open menu is lifted, also while the connection is lost (the grid is dimmed)."""
    offline = make_worker("box1", online=False)
    make_worker("box2", enabled=False)
    make_worker("box3")
    _fleet(page, server, wait_until="networkidle")
    assert menus_on_top(page, "fleet") == [] and menus_on_top(page, "fleet", dimmed=True) == []
    page.route("**/fragments/fleet", lambda route: route.abort())
    page.wait_for_function("document.body.classList.contains('conn-lost')", timeout=8_000)
    assert menus_on_top(page, "fleet") == []
    page.click(f"{_row(offline.id)} details.menu > summary")
    details = page.locator(f'{_row(offline.id)} [data-action="details"] > summary')
    details.click()
    assert details.evaluate("s => s.parentElement.open"), "a real tap on Details opens it"


def test_anchor_links_open_their_group_pick_the_job_kind_and_clear_the_bar(page, server) -> None:
    """MEDIUM (step 7 review): /jobs#model_search showed the Backtest form (the kind
    switch hid the target); a stored "closed" or the section id outside its <details> left
    an anchored group folded; the target landed under the sticky top bar."""
    page.goto(server.url + "/jobs#model_search", wait_until="networkidle")
    assert page.input_value('select[data-switch="job-kind"]') == "model_search"
    assert page.locator('[data-form="model_search"]').is_visible() and not page.locator('[data-form="backtest"]').is_visible()
    top, bar = page.evaluate("[document.getElementById('model_search').getBoundingClientRect().top, "
                             "document.getElementById('topbar').getBoundingClientRect().bottom]")
    assert top >= bar, f"the target lands below the bar ({top} < {bar})"
    page.evaluate("() => localStorage.setItem('fleet.details.settings-live', '0')")
    page.goto(server.url + "/settings#live", wait_until="networkidle")
    assert page.evaluate("document.querySelector('details[data-key=\"settings-live\"]').open"), "the URL target wins over a stored close"
    top, bar = page.evaluate("[document.getElementById('live').getBoundingClientRect().top, "
                             "document.getElementById('topbar').getBoundingClientRect().bottom]")
    assert top >= bar, f"#live lands below the bar ({top} < {bar})"
    page.evaluate("() => localStorage.setItem('fleet.details.trading-exchange', '0')")
    page.goto(server.url + "/trading#exchange", wait_until="networkidle")
    assert page.evaluate("document.querySelector('details[data-key=\"trading-exchange\"]').open"), "a section id opens the group it wraps"

