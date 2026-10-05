"""The stylesheet against the step 7 component contract (docs/UI.md): colour pairs meet
WCAG AA (4.5:1) in both schemes, state chips and filled rules use text-safe pairs, the
JavaScript-off rules win the cascade, and the contract's tokens and components exist.
Pure file tests, no database. Rules are looked up by selector, never by exact text, so a
restyle that keeps the contract keeps these green."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "host" / "static" / "style.css").read_text()


# ------------------------------------------------------------------ a small CSS reader


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def rules(css: str = CSS) -> list[tuple[str, str, str]]:
    """Every style rule as (media condition or "", selector group, declarations), with
    @media blocks unfolded one level deep (nested @supports and the like are read too)."""
    out: list[tuple[str, str, str]] = []

    def walk(text: str, media: str) -> None:
        pos = 0
        while True:
            open_at = text.find("{", pos)
            if open_at < 0:
                return
            head = text[pos:open_at].strip()
            depth, i = 1, open_at + 1
            while depth and i < len(text):
                depth += {"{": 1, "}": -1}.get(text[i], 0)
                i += 1
            body = text[open_at + 1:i - 1]
            if head.startswith("@media") or head.startswith("@supports"):
                walk(body, (media + " " if media else "") + head)
            elif not head.startswith("@"):
                out.append((media, " ".join(head.split()), " ".join(body.split())))
            pos = i

    walk(_strip_comments(css), "")
    return out


_COMPOUND = re.compile(r"[.#]?[\w-]+|\[[^\]]*\]|:[\w-]+(?:\([^)]*\))?|\*")


def _parts(compound: str) -> set[str]:
    return set(_COMPOUND.findall(compound))


def _compounds(selector: str) -> list[str]:
    return [c for c in re.split(r"\s*[>+~]\s*|\s+", selector.strip()) if c]


def _selector_matches(selector: str, query: str) -> bool:
    """query's compounds match the tail of selector, each a subset: ".chip-bad" matches
    ".chip.chip-bad", ".live-form .btn" matches "form.live-form .btn"."""
    have, want = _compounds(selector), _compounds(query)
    if len(want) > len(have):
        return False
    tail = have[len(have) - len(want):]
    if not _parts(want[-1]) <= _parts(tail[-1]):
        return False
    # the earlier compounds may sit anywhere above, in order
    k = len(have) - 1
    for compound in reversed(want[:-1]):
        k -= 1
        while k >= 0 and not _parts(compound) <= _parts(have[k]):
            k -= 1
        if k < 0:
            return False
    return True


def declarations(query: str, media: str | None = None, css: str = CSS) -> str:
    """The declarations of every rule whose selector group holds a selector matching
    query, joined in source order. media=None reads every rule; a string reads only the
    rules inside an @media whose condition contains it ("" for top-level rules only)."""
    found = []
    for cond, group, body in rules(css):
        if media is not None and (media not in cond if media else cond):
            continue
        if any(_selector_matches(sel, query) for sel in group.split(",")):
            found.append(body)
    return " ".join(found)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


def _root_blocks() -> tuple[str, str]:
    """The light :root declarations and the dark-scheme :root declarations."""
    light = " ".join(body for cond, group, body in rules() if group == ":root" and not cond)
    dark = " ".join(body for cond, group, body in rules() if ":root" in group and "prefers-color-scheme: dark" in cond)
    return light, dark


def _schemes() -> tuple[dict[str, str], dict[str, str]]:
    light_block, dark_block = _root_blocks()
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


def _resolve(value: str, tokens: dict[str, str]) -> str | None:
    """A colour value as #rrggbb: a token, a literal hex (3 or 6 digits), white; else None."""
    value = value.strip().lower()
    m = re.fullmatch(r"var\(--([a-z0-9-]+)\)", value)
    if m:
        return tokens.get(m[1])
    if value in ("#fff", "white"):
        return "#ffffff"
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        return "#" + "".join(ch * 2 for ch in value[1:])
    if re.fullmatch(r"#[0-9a-f]{6}", value):
        return value
    return None


def _decl(body: str, prop: str) -> str | None:
    found = re.findall(rf"(?:^|;)\s*{prop}\s*:\s*([^;]+)", body)
    return found[-1].strip() if found else None


# ------------------------------------------------------------------ WCAG AA pairs


# (foreground token or literal, background token or literal) pairs used for small text
PAIRS = [
    ("text", "card"), ("text", "bg"), ("muted", "card"), ("muted", "bg"),
    ("amber-text", "card"),          # the "switching to ..." line, stale ages
    ("red-fg", "card"),              # inline validation errors, "Trading is killed."
    ("chip-text", "badge"),          # disabled / stale / offline chips, chip-muted
    ("#ffffff", "green-fill"),       # chip-ok, succeeded badge, LIVE pill
    ("#ffffff", "amber-fill"),       # chip-warn, waiting / cancel requested badges
    ("#ffffff", "leased-fill"),      # leased badge
    ("#ffffff", "red"),              # chip-bad, failed badge, KILL button, killed top bar
    ("red-text", "red"),
    ("accent-text", "accent-fill"),  # primary buttons
    ("accent", "card"),              # links
    ("accent", "live-bg"),           # the sell and snapshot outline chips on a live row
    ("green-text", "card"),          # "beats the market" (the Robustness reading)
    ("text", "bad-bg"),              # the killed Kill switch group
    ("chip-text", "bad-bg"),         # its grey text (.group.killed sets --muted to --chip-text)
    ("red-fg", "bad-bg"),            # "Trading is killed." inside it
    ("accent", "bad-bg"),            # its Trading link
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


def test_both_schemes_define_their_tokens() -> None:
    light_block, dark_block = _root_blocks()
    assert _tokens(light_block) and _tokens(dark_block), "light :root and a prefers-color-scheme: dark :root"
    light, dark = _schemes()
    for _, token in PAIRS:
        if not token.startswith("#"):
            assert token in light and token in dark, token


def test_every_filled_rule_keeps_its_text_readable() -> None:
    """Any rule that sets both a solid background and a text colour (chips, badges,
    banners, pills, buttons) reaches 4.5:1 in both schemes. Translucent or gradient
    backgrounds are skipped; the fills above cover the solid ones."""
    checked = 0
    for scheme, tokens in zip(("light", "dark"), _schemes()):
        for cond, group, body in rules():
            if ":root" in group or (scheme == "light" and "prefers-color-scheme: dark" in cond):
                continue
            bg_value = _decl(body, "background-color") or _decl(body, "background")
            fg_value = _decl(body, "color")
            if not bg_value or not fg_value:
                continue
            bg, fg = _resolve(bg_value, tokens), _resolve(fg_value, tokens)
            if bg is None or fg is None:
                continue
            checked += 1
            ratio = contrast(fg, bg)
            assert ratio >= 4.5, f"{scheme}: {group} {fg_value} on {bg_value} is {ratio:.2f}:1"
    assert checked >= 5, "the stylesheet sets filled colours somewhere"


@pytest.mark.parametrize("state,fill", [("ok", "green-fill"), ("bad", "red")])
def test_state_chips_use_the_text_safe_fills(state: str, fill: str) -> None:
    """Green means good or on, red means stop or loss; both always on a text-safe fill."""
    assert f"var(--{fill})" in declarations(f".chip-{state}"), state


def test_banners_and_mode_fills_use_the_text_safe_tokens() -> None:
    assert "var(--red)" in declarations(".banner-down") and "var(--amber-fill)" in declarations(".banner-warn")
    assert "var(--green-fill)" in declarations(".pill.live")
    assert "var(--accent-fill)" in declarations(".btn.primary")
    assert "var(--red-fg)" in declarations(".error")


def test_step7_review_colours_and_scroll_padding() -> None:
    """The sell and snapshot chips are accent outlines, not a fifth state fill; the green
    line and the killed group use their AA tokens; the focus ring is white on the red bar;
    an #anchor or a focused field clears the sticky bar and, on a phone, the bottom nav."""
    for chip in (".chip.chip-sell", ".chip.chip-snapshot"):
        body = declarations(chip, media="")
        assert "background: transparent" in body and "color: var(--accent)" in body and "solid var(--accent)" in body, chip
    assert "var(--green-text)" in declarations(".market-line.beats")
    assert "--muted: var(--chip-text)" in declarations(".group.killed")
    assert "outline-color: #fff" in declarations(".topbar.killed :focus-visible")
    assert "var(--topbar-h" in declarations("html", media="")
    assert re.search(r"scroll-padding-bottom:[^;]*--nav-h", declarations("html", media="max-width"))
    assert "var(--tap)" in declarations("details.intro > summary") and "2rem" not in declarations("details.intro[open] > summary")


def test_js_only_rule_outranks_btn_without_javascript() -> None:
    """LOW: `.js-only {display:none}` lost to the later `.btn {display:inline-flex}`."""
    assert "display: none" in declarations("html:not(.js) .js-only")
    assert not any(group == ".js-only" for _, group, _ in rules()), "a bare .js-only rule loses to .btn"


def test_no_em_dashes_in_templates_css_or_js() -> None:
    for path in list((ROOT / "host" / "templates").glob("*.html")) + list((ROOT / "host" / "static").iterdir()):
        assert chr(0x2014) not in path.read_text(), path


# ------------------------------------------------------------------ the step 7 component contract

# The contract's components land with the step 7 shell stylesheet; until its spacing
# tokens exist these checks wait (they switch on by themselves when --s1 is defined).
SHELL = "--s1:" in CSS
shell = pytest.mark.skipif(not SHELL, reason="the step 7 shell stylesheet (spacing tokens) has not landed yet")

COMPONENTS = (
    ".page", ".intro", ".stats", ".stat", ".stat-value", ".stat-label", ".stat-note", ".rows", ".row", ".row-main",
    ".row-title", ".row-meta", ".row-value", ".chip", ".chip-ok", ".chip-warn", ".chip-bad", ".chip-muted", ".card",
    ".card-head", ".count", ".disclosure", ".disclosure-title", ".disclosure-summary", ".disclosure-body", ".menu",
    ".menu-list", ".menu-item", ".bar", ".bar-fill", ".caption",
)


@shell
def test_contract_tokens() -> None:
    light_block, _ = _root_blocks()
    want = {"s1": "4px", "s2": "8px", "s3": "12px", "s4": "16px", "s5": "24px", "tap": "44px", "radius": "10px",
            "fs-body": "1rem", "fs-meta": "0.85rem", "fs-display": "1.75rem"}
    have = dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", light_block))
    assert {k: have.get(k, "").strip() for k in want} == want


@shell
@pytest.mark.parametrize("component", COMPONENTS)
def test_contract_component_is_styled(component: str) -> None:
    assert declarations(component), f"no rule styles {component}"


@shell
def test_contract_chip_states_carry_their_colour() -> None:
    """ok green, warn amber, bad red, muted grey; each a readable pair in both schemes."""
    for state, token in (("ok", "green"), ("warn", "amber"), ("bad", "red"), ("muted", "")):
        body = declarations(f".chip-{state}", media="")
        assert "background" in body, state
        if token:
            assert f"var(--{token}" in body, state
    for scheme, tokens in zip(("light", "dark"), _schemes()):
        for state in ("ok", "warn", "bad", "muted"):
            body = declarations(f".chip-{state}", media="") or ""
            bg = _resolve(_decl(body, "background-color") or _decl(body, "background") or "", tokens)
            fg = _resolve(_decl(body, "color") or _decl(declarations(".chip", media=""), "color") or "", tokens)
            if bg and fg:
                assert contrast(fg, bg) >= 4.5, (scheme, state)


@shell
def test_contract_type_scale() -> None:
    """Two body sizes plus one display size, nothing else, on every contract component;
    the display size is for stats (and the page title) only."""
    allowed = {"var(--fs-body)", "var(--fs-meta)", "var(--fs-display)", "inherit", "1em", "100%"}
    for component in COMPONENTS:
        for size in re.findall(r"(?:^|;)\s*font-size\s*:\s*([^;]+)", declarations(component)):
            assert size.strip() in allowed, f"{component}: font-size {size.strip()}"
    for cond, group, body in rules():
        if "var(--fs-display)" in body:
            assert all("stat" in sel or sel.strip() == "h1" for sel in group.split(",")), f"{group}: the display size is for stats only"


@shell
def test_contract_tap_targets() -> None:
    for component in (".menu-item", ".btn", ".menu summary"):
        body = declarations(component)
        assert "var(--tap)" in body or "44px" in body, component


@shell
def test_contract_stats_grid_and_bottom_nav() -> None:
    """Stats are 2 columns at 390 px and 4 from 700 px; under 700 px the nav is a fixed
    bottom bar and the page keeps clear of it."""
    assert "repeat(2" in declarations(".stats")
    assert "repeat(4" in declarations(".stats", media="min-width")
    phone_nav = declarations(".nav", media="max-width")
    assert "position: fixed" in phone_nav and "bottom" in phone_nav
    assert "safe-area-inset-bottom" in CSS
    clearance = declarations(".page", media="max-width") + declarations("body", media="max-width")
    assert re.search(r"padding(-bottom)?:[^;]*(--nav-h|safe-area-inset-bottom|--safe-bottom)", clearance), "the page clears the bottom nav"


@shell
def test_contract_light_and_dark() -> None:
    assert "prefers-color-scheme: dark" in CSS
    light, dark = _schemes()
    assert light["bg"] != dark["bg"] and light["text"] != dark["text"]


def test_shell_markup_feeds_the_bottom_nav() -> None:
    """The safe-area padding needs viewport-fit=cover; each of the five nav links carries
    an icon (hidden from screen readers) above its label; the wordmark is Home; the status
    shows the current mode's P&L only."""
    from host.web import ENV

    child = ENV.from_string('{% extends "base.html" %}{% block page %}models{% endblock %}')
    cents = {"today_cents": -120, "all_time_cents": 5}
    bar = {"live": False, "killed": False, "pnl": {"paper": cents, "live": {"today_cents": 999, "all_time_cents": 0}}}
    html = child.render(topbar=bar, path="/models/abc", flash=None)
    assert "viewport-fit=cover" in html and 'href="/" data-nav="home"' in html
    links = re.findall(r'<a href="(/\w+)" data-nav="(\w+)"( class="active" aria-current="page")?><svg class="nav-icon" '
                       r'aria-hidden="true"[^>]*>.*?</svg><span class="nav-label">(\w+)</span></a>', html)
    assert [(h, n, lab) for h, n, _, lab in links] == [
        ("/fleet", "fleet", "Fleet"), ("/jobs", "jobs", "Jobs"), ("/models", "models", "Models"),
        ("/trading", "trading", "Trading"), ("/settings", "settings", "Settings")]
    assert [n for _, n, cur, _ in links if cur] == ["models"]
    assert re.search(r'data-pnl="paper">paper <span class="num">-\$1\.20</span> today', html) and "$9.99" not in html
