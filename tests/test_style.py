"""The stylesheet's colour pairs meet WCAG AA (4.5:1) in both schemes, and the
JavaScript-off rules win the cascade. Pure file tests, no database."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parent.parent / "host" / "static" / "style.css").read_text()


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


def _schemes() -> tuple[dict[str, str], dict[str, str]]:
    light_block = CSS[CSS.index(":root {"):CSS.index("@media (prefers-color-scheme: dark)")]
    dark_block = CSS[CSS.index("@media (prefers-color-scheme: dark)"):CSS.index("* { box-sizing")]
    light = _tokens(light_block)
    dark = {**light, **_tokens(dark_block)}
    return light, dark


def _luminance(hex_colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(fg: str, bg: str) -> float:
    a, b = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


# (foreground token or literal, background token or literal) pairs used for small text
PAIRS = [
    ("text", "card"), ("text", "bg"), ("muted", "card"), ("muted", "bg"),
    ("amber-text", "card"),          # the "switching to ..." line
    ("red-fg", "card"),              # inline validation errors, "Trading is killed."
    ("chip-text", "badge"),          # disabled / stale / offline chips
    ("#ffffff", "green-fill"),       # succeeded badge, LIVE pill
    ("#ffffff", "amber-fill"),       # waiting / cancel requested badges
    ("#ffffff", "leased-fill"),      # leased badge
    ("#ffffff", "red"),              # failed badge, KILL button, killed top bar
    ("red-text", "red"),
    ("accent-text", "accent-fill"),  # primary buttons
    ("accent", "card"),              # links
]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("fg,bg", PAIRS)
def test_text_colours_meet_aa(scheme: str, fg: str, bg: str) -> None:
    light, dark = _schemes()
    tokens = light if scheme == "light" else dark
    fg_hex = fg if fg.startswith("#") else tokens[fg]
    bg_hex = bg if bg.startswith("#") else tokens[bg]
    ratio = contrast(fg_hex, bg_hex)
    assert ratio >= 4.5, f"{scheme}: {fg} {fg_hex} on {bg} {bg_hex} is {ratio:.2f}:1"


def test_fills_use_the_text_safe_tokens() -> None:
    assert ".btn.primary { background: var(--accent-fill); border-color: var(--accent-fill)" in CSS
    assert ".badge.st-succeeded { background: var(--green-fill)" in CSS
    assert ".badge.st-cancel_requested, .badge.st-waiting { background: var(--amber-fill)" in CSS
    assert ".badge.st-leased { background: var(--leased-fill)" in CSS
    assert ".switching { margin: 0.4rem 0 0; color: var(--amber-text)" in CSS
    assert ".error { color: var(--red-fg); }" in CSS


def test_js_only_rule_outranks_btn_without_javascript() -> None:
    """LOW: `.js-only {display:none}` lost to the later `.btn {display:inline-flex}`."""
    assert re.search(r"html:not\(\.js\) \.js-only \{ display: none; \}", CSS)
    assert not re.search(r"^\.js-only \{", CSS, re.M)


def test_no_em_dashes_in_templates_css_or_js() -> None:
    root = Path(__file__).resolve().parent.parent
    for path in list((root / "host" / "templates").glob("*.html")) + list((root / "host" / "static").iterdir()):
        assert chr(0x2014) not in path.read_text(), path
