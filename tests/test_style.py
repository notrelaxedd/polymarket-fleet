"""The stylesheet against the step 7 component contract (docs/UI.md): colour pairs meet
WCAG AA (4.5:1) in the one dark scheme the whole site shares with the 3D page, state chips
and filled rules use text-safe pairs, the fonts are served from /static (no third party),
the JavaScript-off rules win the cascade, and the contract's tokens and components exist.
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


_RGBA = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)")


def _root_block() -> str:
    """The top-level :root declarations (the one scheme)."""
    return " ".join(body for cond, group, body in rules() if group == ":root" and not cond)


def _raw_tokens() -> dict[str, str]:
    return {k: v.strip() for k, v in re.findall(r"--([a-z0-9-]+):\s*([^;]+);", _root_block())}


def _follow(value: str, raw: dict[str, str]) -> str:
    """A token value with var(--x) aliases followed to the value they name."""
    for _ in range(10):
        m = re.fullmatch(r"var\(--([a-z0-9-]+)\)", value.strip())
        if not m or m[1] not in raw:
            break
        value = raw[m[1]]
    return value.strip()


def _tokens() -> dict[str, str]:
    """Every solid colour token as #rrggbb (aliases followed)."""
    raw = _raw_tokens()
    out = {}
    for name in raw:
        value = _follow(raw[name], raw).lower()
        if re.fullmatch(r"#[0-9a-f]{6}", value):
            out[name] = value
    return out


def _tints() -> dict[str, tuple[int, int, int, float]]:
    """Every translucent colour token as (r, g, b, alpha)."""
    raw = _raw_tokens()
    out = {}
    for name in raw:
        m = _RGBA.fullmatch(_follow(raw[name], raw))
        if m:
            out[name] = (int(m[1]), int(m[2]), int(m[3]), float(m[4]))
    return out


def _over(tint: tuple[int, int, int, float], surface: str) -> str:
    """A translucent colour composited over a solid surface, as #rrggbb."""
    r, g, b, a = tint
    under = [int(surface[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(c * a + u * (1 - a)):02x}" for c, u in zip((r, g, b), under))


def _schemes() -> tuple[dict[str, str]]:
    """The schemes the stylesheet defines: one, dark. Kept as a tuple so callers that loop
    over the schemes (tests/test_dashboard.py, tests/test_trading_sells_page.py) still do."""
    return (_tokens(),)


def _luminance(hex_colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(fg: str, bg: str) -> float:
    a, b = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def _resolve(value: str, tokens: dict[str, str], over: str | None = None) -> str | None:
    """A colour value as #rrggbb: a token, a literal hex (3 or 6 digits), white; a
    translucent token only when `over` names the surface under it; else None."""
    value = value.strip().lower()
    m = re.fullmatch(r"var\(--([a-z0-9-]+)\)", value)
    if m:
        if m[1] not in tokens and over and m[1] in _tints():
            return _over(_tints()[m[1]], tokens[over])
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
    ("text", "badge"),               # menu summary hover, .badge text
    ("amber-text", "card"),          # the "switching to ..." line, stale ages
    ("red-fg", "card"),              # inline validation errors, "Trading is killed."
    ("red-fg", "bg"),                # the lost-connection line under the bar, danger buttons
    ("chip-text", "badge"),          # disabled / stale / offline chips, chip-muted, counts
    ("ink", "green-fill"),           # the LIVE pill, the live_eligible badge
    ("ink", "amber-fill"),           # primary buttons (the 3D page's amber .btn.go), the unattended banner
    ("red-text", "red"),             # KILL button, exchange-down banner, the killed top bar and bottom nav
    ("red", "ink"),                  # the KILLED chip and banners on the killed bar
    ("accent", "card"),              # links, the sell and snapshot outline chips
    ("accent", "bg"),
    ("accent", "live-bg"),           # the sell and snapshot outline chips on a live row
    ("text", "live-bg"), ("muted", "live-bg"), ("red-fg", "live-bg"),
    ("green-text", "card"),          # "beats the market" (the Robustness reading)
    ("text", "bad-bg"),              # the killed Kill switch group
    ("chip-text", "bad-bg"),         # its grey text (.group.killed sets --muted to --chip-text)
    ("red-fg", "bad-bg"),            # "Trading is killed." inside it
    ("accent", "bad-bg"),            # its Trading link
]

# state chips and badges: the state colour on its own 10% tint, over each surface a chip sits on
TINTS = [("green-text", "tint-ok"), ("amber-text", "tint-hot"), ("red-fg", "tint-bad"), ("accent", "tint-ok")]
SURFACES = ("card", "bg", "live-bg", "badge")


@pytest.mark.parametrize("fg,bg", PAIRS)
def test_text_colours_meet_aa(fg: str, bg: str) -> None:
    tokens = _tokens()
    fg_hex = fg if fg.startswith("#") else tokens[fg]
    bg_hex = bg if bg.startswith("#") else tokens[bg]
    ratio = contrast(fg_hex, bg_hex)
    assert ratio >= 4.5, f"{fg} {fg_hex} on {bg} {bg_hex} is {ratio:.2f}:1"


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("fg,tint", TINTS)
def test_tinted_chips_meet_aa(fg: str, tint: str, surface: str) -> None:
    tokens = _tokens()
    bg_hex = _over(_tints()[tint], tokens[surface])
    ratio = contrast(tokens[fg], bg_hex)
    assert ratio >= 4.5, f"{fg} on {tint} over {surface} ({bg_hex}) is {ratio:.2f}:1"


def test_one_dark_scheme_for_every_viewer() -> None:
    """The site is dark only, like the 3D page: :root declares color-scheme: dark, there is
    no light scheme to fall back to and no prefers-color-scheme switch; the 3D page's
    palette is defined with its own names and values; every token a pair uses exists."""
    root = _root_block()
    assert "color-scheme: dark" in root and "light" not in _decl(root, "color-scheme")
    assert "prefers-color-scheme" not in _strip_comments(CSS)
    tokens = _tokens()
    palette = {"void": "#04070b", "panel-solid": "#0a1218", "line": "#1c3140", "line-strong": "#2f5468", "fg": "#dfeaf0",
               "dim": "#93a8b4", "ok": "#45c4f5", "hot": "#ffa733", "off": "#73858f", "ink": "#04121a"}
    assert {k: tokens.get(k) for k in palette} == palette
    assert _raw_tokens()["panel"] == "rgba(7, 13, 18, 0.9)"
    for fg, bg in PAIRS:
        for token in (fg, bg):
            if not token.startswith("#"):
                assert token in tokens, token
    for _, tint in TINTS:
        assert tint in _tints(), tint
    assert _decl(declarations("body", media=""), "background") == "var(--bg)", "the body paints the dark surface itself"


def test_every_filled_rule_keeps_its_text_readable() -> None:
    """Any rule that sets both a solid background and a text colour (chips, badges,
    banners, pills, buttons) reaches 4.5:1; a background from a tint token is read over
    the card. Other translucent or gradient backgrounds are skipped; the pairs above cover
    the surfaces they sit on."""
    checked = 0
    tokens = _tokens()
    for cond, group, body in rules():
        if ":root" in group:
            continue
        bg_value = _decl(body, "background-color") or _decl(body, "background")
        fg_value = _decl(body, "color")
        if not bg_value or not fg_value:
            continue
        bg, fg = _resolve(bg_value, tokens, over="card"), _resolve(fg_value, tokens)
        if bg is None or fg is None:
            continue
        checked += 1
        ratio = contrast(fg, bg)
        assert ratio >= 4.5, f"{group} {fg_value} on {bg_value} is {ratio:.2f}:1"
    assert checked >= 5, "the stylesheet sets filled colours somewhere"


@pytest.mark.parametrize("state,fill", [("ok", "green-fill"), ("bad", "red")])
def test_state_chips_use_the_text_safe_fills(state: str, fill: str) -> None:
    """The ok slot (cyan) means good or on, red means stop or loss; both always in their
    text-safe colour (the tinted pairs above)."""
    assert f"var(--{fill})" in declarations(f".chip-{state}"), state


def test_banners_and_mode_fills_use_the_text_safe_tokens() -> None:
    assert "var(--red)" in declarations(".banner-down") and "var(--amber-fill)" in declarations(".banner-warn")
    assert "var(--green-fill)" in declarations(".pill.live")
    assert "var(--amber-fill)" in declarations(".btn.primary") and "var(--ink)" in declarations(".btn.primary")
    assert "var(--red-fg)" in declarations(".error")


def test_step7_review_colours_and_scroll_padding() -> None:
    """The sell and snapshot chips are accent outlines, not a fifth state fill; the green
    line and the killed group use their AA tokens; the focus ring is ink on the red bar;
    an #anchor or a focused field clears the sticky bar and, on a phone, the bottom nav."""
    for chip in (".chip.chip-sell", ".chip.chip-snapshot"):
        body = declarations(chip, media="")
        assert "background: transparent" in body and "color: var(--accent)" in body and "solid var(--accent)" in body, chip
    assert "var(--green-text)" in declarations(".market-line.beats")
    assert "--muted: var(--chip-text)" in declarations(".group.killed")
    assert "outline-color: var(--ink)" in declarations(".topbar.killed :focus-visible")
    assert "var(--topbar-h" in declarations("html", media="")
    assert re.search(r"scroll-padding-bottom:[^;]*--nav-h", declarations("html", media="max-width"))
    assert "var(--tap)" in declarations("details.intro > summary") and "2rem" not in declarations("details.intro[open] > summary")


def test_js_only_rule_outranks_btn_without_javascript() -> None:
    """LOW: `.js-only {display:none}` lost to the later `.btn {display:inline-flex}`."""
    assert "display: none" in declarations("html:not(.js) .js-only")
    assert not any(group == ".js-only" for _, group, _ in rules()), "a bare .js-only rule loses to .btn"


def test_no_em_dashes_in_templates_css_or_js() -> None:
    """Every text file under host/templates and host/static, any depth (the fonts are binary)."""
    static = [p for pattern in ("*.css", "*.js", "*.html") for p in (ROOT / "host" / "static").rglob(pattern)]
    for path in list((ROOT / "host" / "templates").glob("*.html")) + static:
        assert chr(0x2014) not in path.read_text(encoding="utf-8"), path


FONTS = ROOT / "host" / "static" / "fonts"


def test_fonts_are_served_from_static_and_nothing_loads_from_a_third_party() -> None:
    """Chakra Petch 500/600/700 and JetBrains Mono 400/500/700 (the 3D page's fonts) come from
    /static/fonts, the files exist with their OFL licences, the package data ships them, and
    the stylesheet and the page shell load nothing from another origin."""
    css = _strip_comments(CSS)
    faces = re.findall(r"@font-face\s*\{([^}]*)\}", css)
    have = set()
    for face in faces:
        family = re.search(r"font-family:\s*\"([^\"]+)\"", face)[1]
        weight = re.search(r"font-weight:\s*(\d+)", face)[1]
        urls = re.findall(r"url\(([^)]+)\)", face)
        assert urls and all(u.strip("\"'").startswith("/static/fonts/") for u in urls), face
        for u in urls:
            assert (FONTS / u.strip("\"'").removeprefix("/static/fonts/")).is_file(), u
        assert "font-display: swap" in face
        have.add((family, weight))
    assert have == {("Chakra Petch", w) for w in ("500", "600", "700")} | {("JetBrains Mono", w) for w in ("400", "500", "700")}
    assert sorted(p.name for p in FONTS.glob("OFL-*.txt")) == ["OFL-chakra-petch.txt", "OFL-jetbrains-mono.txt"]
    assert "@import" not in css and not re.search(r"url\(\s*[\"']?(https?:)?//", css)
    base = (ROOT / "host" / "templates" / "base.html").read_text()
    assert not re.search(r"(href|src)=\"(https?:)?//", base), "the shell loads nothing from another origin"
    assert re.search(r"Chakra Petch.*JetBrains Mono|JetBrains Mono.*Chakra Petch", _root_block())
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert '"static/fonts/*"' in pyproject, "the wheel ships the fonts"


FLEET_UI = ROOT / "fleet-ui"


@pytest.mark.skipif(not FLEET_UI.is_dir(), reason="no fleet-ui/ folder in this checkout")
def test_no_em_dashes_in_the_fleet_ui_source() -> None:
    """The 3D page's source follows the same rule: fleet-ui/src (TypeScript, TSX, CSS,
    any depth) and its index.html."""
    paths = [p for pattern in ("*.ts", "*.tsx", "*.css") for p in (FLEET_UI / "src").rglob(pattern)]
    paths += [FLEET_UI / "index.html"] if (FLEET_UI / "index.html").is_file() else []
    for path in paths:
        assert chr(0x2014) not in path.read_text(encoding="utf-8"), path


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
    """The spacing and type scale, the tap size, and square corners (the 3D page's look)."""
    want = {"s1": "4px", "s2": "8px", "s3": "12px", "s4": "16px", "s5": "24px", "tap": "44px", "radius": "0",
            "fs-body": "1rem", "fs-meta": "0.85rem", "fs-display": "1.75rem"}
    have = dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", _root_block()))
    assert {k: have.get(k, "").strip() for k in want} == want


@shell
@pytest.mark.parametrize("component", COMPONENTS)
def test_contract_component_is_styled(component: str) -> None:
    assert declarations(component), f"no rule styles {component}"


@shell
def test_contract_chip_states_carry_their_colour() -> None:
    """ok cyan (the green slot), warn amber, bad red, muted grey; each a readable pair, a
    tinted background read over the card."""
    for state, token in (("ok", "green"), ("warn", "amber"), ("bad", "red"), ("muted", "")):
        body = declarations(f".chip-{state}", media="")
        assert "background" in body, state
        if token:
            assert f"var(--{token}" in body, state
    tokens = _tokens()
    for state in ("ok", "warn", "bad", "muted"):
        body = declarations(f".chip-{state}", media="") or ""
        bg = _resolve(_decl(body, "background-color") or _decl(body, "background") or "", tokens, over="card")
        fg = _resolve(_decl(body, "color") or _decl(declarations(".chip", media=""), "color") or "", tokens)
        assert bg and fg, state
        assert contrast(fg, bg) >= 4.5, state


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
def test_contract_dark_only() -> None:
    """One scheme (docs/UI.md): dark surfaces, light text, the shell pages say so too."""
    tokens = _tokens()
    assert _luminance(tokens["bg"]) < 0.01 and _luminance(tokens["card"]) < 0.01 and _luminance(tokens["text"]) > 0.7
    for name in ("base.html", "error.html"):
        assert '<meta name="color-scheme" content="dark">' in (ROOT / "host" / "templates" / name).read_text(), name


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
