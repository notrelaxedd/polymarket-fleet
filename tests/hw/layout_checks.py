"""The layout checks of tests/hw/screenshots.py (a dev tool, not a pytest test): no
horizontal scroll and tap targets at least MIN_TAP_PX tall at phone width, tables that
fit their wrappers on the Models pages at 390 and 1280, and the footer's refresh
counter. Each check appends what it finds to `problems`; screenshots.py exits 1 on any.
"""
from __future__ import annotations

import re
from typing import Any

MIN_TAP_PX = 40
CARD_TARGETS = ".card.worker button, .card.worker select, .card.worker a"
FORM_TARGETS = "form button, form select, form input:not([type=hidden]):not([type=checkbox]), form textarea, form label.check"


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


def check_desktop_tables(page: Any, name: str, problems: list[str]) -> None:
    """At desktop width every table fits its wrapper: no table-wrap scrolls sideways
    (the document check alone misses a wrapper that scrolls inside itself)."""
    wide = page.evaluate(
        """() => Array.from(document.querySelectorAll('.table-wrap'))
             .filter(el => el.scrollWidth > el.clientWidth + 1)
             .map(el => [el.scrollWidth, el.clientWidth])"""
    )
    for scroll_w, client_w in wide:
        problems.append(f"{name}: a table-wrap scrolls sideways at desktop width ({scroll_w} > {client_w})")


def check_phone_tables(page: Any, name: str, problems: list[str]) -> None:
    """At 390 px a non-stacked table on the Models pages fits its wrapper (the in-game
    calibration once hid its vegas_wp column) and the in-game reason is not clipped."""
    wide, clipped = page.evaluate(
        """() => [Array.from(document.querySelectorAll('.table-wrap'))
                    .filter(el => !el.querySelector('table.stack') && el.scrollWidth > el.clientWidth + 1)
                    .map(el => [el.querySelector('table').className, el.scrollWidth, el.clientWidth]),
                  Array.from(document.querySelectorAll('.ingame-reason'))
                    .filter(el => el.scrollWidth > el.clientWidth + 1).length]"""
    )
    for cls, scroll_w, client_w in wide:
        problems.append(f"{name} at 390: table.{cls} scrolls sideways ({scroll_w} > {client_w})")
    if clipped:
        problems.append(f"{name} at 390: {clipped} in-game reason lines clipped")


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
