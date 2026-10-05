"""host/templates/_ui.html renders the step 7 component contract exactly: the classes,
the markup patterns and the data hooks the page tests read. Pure template tests."""
from __future__ import annotations

from host.web import make_env
from tests.pagecheck import Node, page


def render(source: str, **context: object) -> Node:
    template = make_env().from_string('{% import "_ui.html" as ui %}' + source)
    return page(template.render(**context))


def test_stat_is_a_link_or_a_block() -> None:
    linked = render('{{ ui.stat("paper-today", "+$6.57", "Paper today", href="/trading", note="2 games") }}').one(".stat")
    assert linked.tag == "a" and linked.target == "/trading" and linked.attr("data-stat") == "paper-today"
    assert [linked.one(c).text for c in (".stat-value", ".stat-label", ".stat-note")] == ["+$6.57", "Paper today", "2 games"]
    plain = render('{{ ui.stat("exposure", "$34.12", "Exposure") }}').one('[data-stat="exposure"]')
    assert plain.tag == "div" and plain.has_class("stat") and not plain.has_attr("href") and not plain.has(".stat-note")
    assert plain.prop("Exposure") == "$34.12"


def test_chip_carries_state_key_word_and_extra_classes() -> None:
    doc = render('{{ ui.chip("ok", "paper") }}{{ ui.chip("bad", "LIVE", key="live", extra="chip-live is-live") }}')
    ok, live = doc.select(".chip")
    assert ok.classes == ["chip", "chip-ok"] and ok.attr("data-chip") == "ok" and ok.text == "paper"
    assert live.classes == ["chip", "chip-bad", "chip-live", "is-live"] and live.attr("data-chip") == "live" and live.text == "LIVE"
    assert render('{{ ui.chip("warn", "<b>x</b>") }}').one(".chip").text == "<b>x</b>", "the word is escaped"


def test_intro_starts_open_with_its_key() -> None:
    intro = render('{{ ui.intro("models", "One row per lineage.") }}').one("details.intro")
    assert intro.is_open and intro.attr("data-key") == "intro-models"
    assert intro.one("summary").text == "What this page is" and intro.one("p.muted").text == "One row per lineage."


def test_bar_is_a_clamped_progressbar() -> None:
    for given, want in ((42, "42"), (41.6, "42"), (None, "0"), (-5, "0"), (250, "100")):
        bar = render("{{ ui.bar(pct) }}", pct=given).one(".bar")
        assert bar.attr("role") == "progressbar" and (bar.attr("aria-valuemin"), bar.attr("aria-valuemax")) == ("0", "100")
        assert bar.attr("aria-valuenow") == want and bar.one(".bar-fill").attr("style") == f"width: {want}%", given


def test_disclosure_wraps_its_body() -> None:
    src = '{% call ui.disclosure("trading-open-orders", "Open orders", count=2, summary="2 resting, $1.20") %}<p id="in">x</p>{% endcall %}'
    box = render(src).one("details.disclosure")
    assert box.attr("data-key") == "trading-open-orders" and not box.is_open
    summary = box.one("summary")
    assert [summary.one(c).text for c in (".disclosure-title", ".count", ".disclosure-summary")] == ["Open orders", "2", "2 resting, $1.20"]
    assert box.one(".disclosure-body").has("#in")
    bare = render('{% call ui.disclosure("settings-limits", "Limits", count=0, open=True) %}y{% endcall %}').one("details")
    assert bare.is_open and bare.one(".count").text == "0" and not bare.has(".disclosure-summary")
    assert not render('{% call ui.disclosure("k", "T") %}z{% endcall %}').has(".count")


def test_menu_holds_the_actions() -> None:
    src = ('{% call ui.menu() %}<form class="menu-item" data-action="halt" method="post" action="/assignments/1/halt">'
           '<button type="submit">Halt</button></form><a class="menu-item" data-action="detail" href="/x">Detail</a>{% endcall %}')
    menu = render(src).one("details.menu")
    assert not menu.is_open and menu.one("summary").attr("aria-label") == "More actions" and menu.one("summary").text == "..."
    assert menu.one(".menu-list").actions() == ["halt", "detail"] and menu.action("halt").target == "/assignments/1/halt"
    assert render('{% call ui.menu("Model actions") %}a{% endcall %}').one("summary").attr("aria-label") == "Model actions"


def test_row_is_one_line_item_with_its_hooks() -> None:
    src = ('<ul class="rows" data-list="models">{% call ui.row("model", "m1", "/models/m1", "elo_blend K 40 · HFA 70", meta="paper 5 games") %}'
           '{{ ui.chip("ok", "paper", key="paper_ok") }}<span class="row-value">CLV +0.6%</span>{% endcall %}</ul>')
    doc = render(src)
    row = doc.listing("models").row("model", "m1")
    assert row.tag == "li" and row.has_class("row")
    main = row.one("a.row-main")
    assert main.target == "/models/m1" and main.one(".row-title").text == "elo_blend K 40 · HFA 70" and main.one(".row-meta").text == "paper 5 games"
    assert row.chips() == ["paper_ok"] and row.one(".row-value").text == "CLV +0.6%"
    assert not render('{% call ui.row("job", 7, "/jobs/7", "sleep") %}{% endcall %}').row("job", 7).has(".row-meta")


def test_chip_takes_a_title() -> None:
    chip = render('{{ ui.chip("warn", "fragile", key="fragile", title="worse prices remove the edge") }}').one(".chip")
    assert chip.attr("title") == "worse prices remove the edge" and chip.text == "fragile"
    assert not render('{{ ui.chip("ok", "paper") }}').one(".chip").has_attr("title")


def test_row_without_a_page_with_extra_lines_classes_and_hooks() -> None:
    src = ('<ul class="rows">{% call ui.row("order", 3, None, "5 @ 52c", meta="KC @ LV", meta2="edge 3.1%", ingame="Q3 7:12 KC 17-14",'
           ' cls="is-live", attrs={"data-order": 3, "data-kind": "smoke"}) %}{% endcall %}</ul>')
    row = render(src).row("order", 3)
    assert row.has_class("is-live") and row.attr("data-order") == "3" and row.attr("data-kind") == "smoke"
    main = row.one(".row-main")
    assert main.tag == "div" and not main.has_attr("href")
    assert [m.text for m in main.select(".row-meta")] == ["KC @ LV", "edge 3.1%", "Q3 7:12 KC 17-14"]
    assert main.one(".row-ingame").text == "Q3 7:12 KC 17-14"
