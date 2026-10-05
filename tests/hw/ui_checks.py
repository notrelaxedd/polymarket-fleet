"""The docs/UI.md page assertions, run in a real browser (Playwright) on a loaded page.
Used by tests/hw/screenshots.py on every capture and by tests/hw/test_row_audit.py.

Every check returns a list of problems in words (empty when the page passes):
- `overflow`: no horizontal scroll at any width;
- `first_screen`: at 390x844 the first screen holds the h1 and at least one .stat;
- `row_heights`: no visible .row taller than 88 px (phone width);
- `chips_have_text`: every .chip has a word;
- `details_have_summaries`: every <details> starts with a <summary> that has text (or an aria-label);
- `tap_targets`: visible buttons, row links, selects, inputs, textareas, menu items and
  menu summaries are at least 44 px tall (phone width).
`open_disclosures` opens every closed disclosure (not the "..." menus) so the rows and
forms inside them are measured too.
"""
from __future__ import annotations

from typing import Any

ROW_MAX_PX = 88
TAP_PX = 44
TAP_TARGETS = (
    "button, a.row-main, select, input:not([type=hidden]):not([type=checkbox]), textarea, label.check, "
    ".menu-item, details.menu > summary"
)


def overflow(page: Any, name: str) -> list[str]:
    scroll_w, inner_w = page.evaluate("[document.scrollingElement.scrollWidth, window.innerWidth]")
    return [f"{name}: horizontal scroll, scrollWidth {scroll_w} > innerWidth {inner_w}"] if scroll_w > inner_w else []


def first_screen(page: Any, name: str, height: int = 844) -> list[str]:
    """The h1 and one .stat lie inside the first `height` px of the page (scrolled to the top)."""
    found = page.evaluate(
        """(h) => {
             window.scrollTo(0, 0);
             const inside = el => { const r = el.getBoundingClientRect(); return r.height > 0 && r.top >= 0 && r.bottom <= h; };
             const title = document.querySelector('main h1');
             const stats = Array.from(document.querySelectorAll('main .stat')).filter(inside);
             return [!!title && inside(title), stats.length];
           }""",
        height,
    )
    problems = []
    if not found[0]:
        problems.append(f"{name}: the h1 is not inside the first {height} px")
    if not found[1]:
        problems.append(f"{name}: no .stat inside the first {height} px")
    return problems


def row_heights(page: Any, name: str, limit: int = ROW_MAX_PX) -> list[str]:
    tall = page.evaluate(
        """(limit) => Array.from(document.querySelectorAll('.row'))
             .filter(r => r.getClientRects().length > 0)
             .map(r => [r.getAttribute('data-row') || '?', r.getAttribute('data-id') || '?', r.getBoundingClientRect().height])
             .filter(([, , h]) => h > limit)""",
        limit,
    )
    return [f"{name}: {kind} row {ident} is {h:.0f} px tall (> {limit})" for kind, ident, h in tall]


def chips_have_text(page: Any, name: str) -> list[str]:
    empty = page.evaluate(
        "() => Array.from(document.querySelectorAll('.chip')).filter(c => !c.textContent.trim()).map(c => c.outerHTML.slice(0, 80))"
    )
    return [f"{name}: empty chip {html}" for html in empty]


def details_have_summaries(page: Any, name: str) -> list[str]:
    bad = page.evaluate(
        """() => Array.from(document.querySelectorAll('details')).filter(d => {
             const s = d.firstElementChild;
             return !s || s.tagName !== 'SUMMARY' || !(s.textContent.trim() || s.getAttribute('aria-label'));
           }).map(d => d.getAttribute('data-key') || d.className || d.outerHTML.slice(0, 60))"""
    )
    return [f"{name}: <details> {what} has no summary with text" for what in bad]


def tap_targets(page: Any, name: str, minimum: int = TAP_PX) -> list[str]:
    short = page.evaluate(
        """([sel, min]) => Array.from(document.querySelectorAll(sel))
             .filter(el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden')
             .map(el => [el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).split(' ')[0] : ''),
                         (el.getAttribute('name') || el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 30),
                         el.getBoundingClientRect().height])
             .filter(([, , h]) => h < min - 0.5)""",
        [TAP_TARGETS, minimum],
    )
    return [f"{name}: {tag} '{text}' is {h:.0f} px tall (< {minimum})" for tag, text, h in short]


def open_disclosures(page: Any) -> int:
    """Open every closed <details> except the action menus; the count opened."""
    return page.evaluate(
        """() => { let n = 0;
             document.querySelectorAll('details:not(.menu):not([open])').forEach(d => { d.open = true; n++; });
             return n; }"""
    )


def check_page(page: Any, name: str, phone: bool) -> list[str]:
    """Every docs/UI.md assertion for one capture: the width-independent ones always,
    the first screen, row heights and tap targets at phone width (with the disclosures
    opened afterwards, so folded rows and forms are measured too)."""
    problems = overflow(page, name) + chips_have_text(page, name) + details_have_summaries(page, name)
    if phone:
        problems += first_screen(page, name)
        problems += row_heights(page, name) + tap_targets(page, name)
        if open_disclosures(page):
            opened = f"{name} (opened)"
            problems += overflow(page, opened) + row_heights(page, opened) + tap_targets(page, opened)
    return problems
