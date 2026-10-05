"""The docs/UI.md page assertions, run in a real browser (Playwright) on a loaded page.
Used by tests/hw/screenshots.py on every capture and by tests/hw/test_row_audit.py.

Every check returns a list of problems in words (empty when the page passes):
- `overflow`: no horizontal scroll at any width;
- `first_screen`: at 390x844 the first screen holds the h1 and at least one .stat;
- `row_heights`: no visible .row taller than 88 px (phone width);
- `chips_have_text`: every .chip has a word;
- `details_have_summaries`: every <details> starts with a <summary> that has text (or an aria-label);
- `tap_targets`: visible buttons, row links (and a row number or card header that is a
  link), selects, inputs, textareas, menu items and every summary (menus, disclosures,
  the intro) are at least 44 px tall (phone width);
- `chips_whole`: no chip in a row title, a row's flag line or the row itself is cut by
  an ancestor that clips (the state word is always readable);
- `menus_on_top`: each "..." menu, opened, is the topmost thing under the centre of every
  item (a dimmed card or region makes a stacking context; the next card must not paint
  over the list); `dimmed=True` runs it with body.conn-lost as when the connection is lost.
`open_disclosures` opens every closed disclosure (not the "..." menus) so the rows and
forms inside them are measured too.
"""
from __future__ import annotations

from typing import Any

ROW_MAX_PX = 88
TAP_PX = 44
TAP_TARGETS = (
    "button, a.row-main, a.row-value, .card-head > a, select, input:not([type=hidden]):not([type=checkbox]), textarea, label.check, "
    ".menu-item, details > summary"
)
# chips that must never be cut: in a title (it flexes so the text gives way), on the flag
# line under it (it wraps) and directly in the row; a chip at the end of a one-line
# .row-meta is not covered (that line ellipsises)
WHOLE_CHIPS = ".row-title .chip, .row-flags .chip, .row > .chip"


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


def chips_whole(page: Any, name: str) -> list[str]:
    """No chip in WHOLE_CHIPS sticks out of its nearest ancestor that clips (overflow not
    visible) by more than half a pixel on any side."""
    cut = page.evaluate(
        """(sel) => Array.from(document.querySelectorAll(sel)).filter(c => c.getClientRects().length > 0).map(c => {
             const r = c.getBoundingClientRect();
             for (let a = c.parentElement; a && a !== document.body; a = a.parentElement) {
               const cs = getComputedStyle(a);
               if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
               const b = a.getBoundingClientRect();
               const out = r.left < b.left - 0.5 || r.right > b.right + 0.5 || r.top < b.top - 0.5 || r.bottom > b.bottom + 0.5;
               const row = c.closest('[data-row]');
               return out ? [c.textContent.trim(), row ? row.getAttribute('data-row') + ' ' + row.getAttribute('data-id') : '?'] : null;
             }
             return null;
           }).filter(Boolean)""",
        WHOLE_CHIPS,
    )
    return [f"{name}: chip '{word}' is cut ({row})" for word, row in cut]


def menus_on_top(page: Any, name: str, dimmed: bool = False) -> list[str]:
    """Open each visible "..." menu in turn; at the centre of each of its items (scrolled
    to the middle of the screen) the element on top is that item or inside it."""
    hidden = page.evaluate(
        """(dimmed) => {
             const bad = [], body = document.body, was = body.classList.contains('conn-lost');
             if (dimmed) body.classList.add('conn-lost');
             // a menu inside a folded disclosure has a box but is not painted: only the ones a reader can open
             const shown = d => d.getClientRects().length > 0 && !(d.parentElement && d.parentElement.closest('details:not([open])'));
             for (const m of Array.from(document.querySelectorAll('details.menu')).filter(shown)) {
               const wasOpen = m.open;
               m.open = true;
               for (const it of Array.from(m.querySelectorAll('.menu-list .menu-item')).filter(e => e.getClientRects().length > 0)) {
                 it.scrollIntoView({block: 'center'});
                 const r = it.getBoundingClientRect();
                 const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                 if (hit && (hit === it || it.contains(hit))) continue;
                 const row = m.closest('[data-row]');
                 bad.push([it.textContent.trim().slice(0, 20), row ? row.getAttribute('data-row') + ' ' + row.getAttribute('data-id') : '?',
                           hit ? hit.tagName.toLowerCase() + '.' + (hit.getAttribute('class') || '').split(' ')[0] : 'nothing']);
               }
               m.open = wasOpen;
             }
             if (dimmed && !was) body.classList.remove('conn-lost');
             window.scrollTo(0, 0);
             return bad;
           }""",
        dimmed,
    )
    state = " (connection lost)" if dimmed else ""
    return [f"{name}{state}: menu item '{item}' of {row} is under {hit}" for item, row, hit in hidden]


def open_disclosures(page: Any) -> int:
    """Open every closed <details> except the action menus; the count opened."""
    return page.evaluate(
        """() => { let n = 0;
             document.querySelectorAll('details:not(.menu):not([open])').forEach(d => { d.open = true; n++; });
             return n; }"""
    )


def check_page(page: Any, name: str, phone: bool) -> list[str]:
    """Every docs/UI.md assertion for one capture: the width-independent ones always,
    the first screen, row heights, tap targets and whole chips at phone width (with the
    disclosures opened afterwards, so folded rows and forms are measured too), then every
    menu opened, as loaded and with the connection lost."""
    problems = overflow(page, name) + chips_have_text(page, name) + details_have_summaries(page, name)
    if phone:
        problems += first_screen(page, name)
        problems += row_heights(page, name) + tap_targets(page, name) + chips_whole(page, name)
        if open_disclosures(page):
            opened = f"{name} (opened)"
            problems += overflow(page, opened) + row_heights(page, opened) + tap_targets(page, opened) + chips_whole(page, opened)
        problems += menus_on_top(page, name) + menus_on_top(page, name, dimmed=True)
    return problems
