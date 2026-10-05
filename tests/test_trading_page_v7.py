"""The step 7 Trading page (docs/UI.md "Trading"): the first-screen stats, the New
assignment disclosure folded at the top and outside the refreshed region, assignment
rows (row link, status chip, "..." menu with Halt and Detail, the LIVE chip leading a
live row), the closed disclosures with their counts, and the 6C in-game slot. Pages are
read through tests/pagecheck.py hooks only."""
from __future__ import annotations

from typing import Any

from tests.conftest import GAME_ID, approved_order, enable_live, insert_snapshot, trade_setup
from tests.pagecheck import page

DISCLOSURES = ("open-orders", "positions", "orders", "fills", "markets", "unmatched", "exchange", "ledger")


def test_first_screen_stats_in_words(client: Any, conn: Any) -> None:
    setup = trade_setup(conn)
    approved_order(conn, setup, size=10)
    conn.execute("UPDATE exchange_state SET heartbeat_at = now() - interval '2 seconds', market_source = 'sim'")
    p = page(client.get("/trading").text)
    stats = p.one(".stats")
    names = [n.attr("data-stat") for n in stats.select("[data-stat]")]
    assert names == ["paper-today", "open-orders", "exposure", "exchange"], "no live stat without live activity"
    assert p.stat("paper-today").one(".stat-value").text == "$0.00" and p.prop("Paper today") == "$0.00"
    assert p.stat("open-orders").one(".stat-value").text == "1" and p.stat("open-orders").target == "#open-orders"
    reserved = conn.execute("SELECT reserved_cents FROM bankrolls").fetchone()["reserved_cents"]
    assert reserved > 0 and p.prop("Exposure") == f"${reserved // 100}.{reserved % 100:02d}"
    exchange = p.stat("exchange")
    assert exchange.one(".stat-value").text == "ok" and exchange.one(".stat-note").text == "sim · 2 s ago"
    assert p.one("h1").text == "Trading" and p.one('details[data-key="intro-trading"]').is_open
    # the stats live inside the refreshed region, the fragment carries them too
    assert page(client.get("/fragments/trading").text).has('[data-stat="exchange"]')
    conn.execute("UPDATE exchange_state SET heartbeat_at = NULL")
    assert page(client.get("/trading").text).prop("Exchange") == "DOWN"


def test_second_mode_pnl_lives_on_trading_while_live_is_in_play(client: Any, conn: Any) -> None:
    enable_live(conn)
    trade_setup(conn, mode="live", model_status="live_eligible")
    p = page(client.get("/trading").text)
    assert p.stat("live-today").one(".stat-value").text == "$0.00" and p.has('[data-stat="paper-today"]')
    assert "paper" in p.stat("exposure").one(".stat-note").text and "live" in p.stat("exposure").one(".stat-note").text


def test_new_assignment_disclosure_sits_at_the_top_outside_the_refresh(client: Any, conn: Any) -> None:
    p = page(client.get("/trading").text)
    form = p.form("assign")
    box = form.closest("details")
    assert box.has_class("disclosure") and not box.is_open and box.one(".disclosure-title").text == "New assignment"
    assert box.start < p.one("#trading-live").start and form.closest("#trading-live") is None
    assert all(n.text for n in form.select(".help")), "help is a muted line under each input"


def test_assignment_row_link_chips_and_menu(client: Any, conn: Any) -> None:
    setup = trade_setup(conn)
    aid = setup.assignment["id"]
    p = page(client.get("/trading").text)
    assignments = p.card("assignments")
    assert assignments.one(".card-head .count").text == "1"
    row = p.row("assignment", aid)
    main = row.one(".row-main")
    assert main.tag == "a" and main.target == f"/models/{setup.model['id']}" and "KC @ LV" in main.one(".row-title").text
    assert len(row.select(".row-meta")) == 1 and GAME_ID in row.one(".row-meta").text
    assert row.chip("active").has_class("chip-ok") and row.chip("paper").text == "paper"
    assert row.one(".row-value").text == "$100.00", "bankroll available"
    menu = row.one("details.menu")
    assert menu.one("summary").attr("aria-label") and menu.action("halt").one("button").has_class("menu-item")
    assert f"/api/assignments/{aid}" in menu.hrefs and "avail $100.00" in menu.text and "open orders 0" in menu.text
    client.post(f"/assignments/{aid}/halt", data={}, follow_redirects=False)
    assert page(client.get("/trading").text).row("assignment", aid).chip("halted").has_class("chip-warn")


def test_live_row_leads_with_the_live_chip(client: Any, conn: Any) -> None:
    enable_live(conn)
    setup = trade_setup(conn, mode="live", model_status="live_eligible")
    row = page(client.get("/trading").text).row("assignment", setup.assignment["id"])
    title = row.one(".row-title")
    assert row.has_class("is-live") and title.select(".chip")[0].attr("data-chip") == "live"
    assert row.chip("live").has_class("chip-bad") and not row.has('[data-chip="paper"]')


def test_groups_are_closed_disclosures_with_counts(client: Any, conn: Any) -> None:
    setup = trade_setup(conn)
    approved_order(conn, setup, size=10)
    insert_snapshot(conn, setup.market["id"])
    p = page(client.get("/trading").text)
    for name in DISCLOSURES:
        box = p.card(name).one("details.disclosure")
        assert not box.is_open and box.attr("data-key") == f"trading-{name}", name
        assert box.one("summary .disclosure-title").text, name
    assert p.card("open-orders").one("summary .count").text == "1" and p.card("orders").one("summary .count").text == "1"
    assert p.card("markets").one("summary .count").text == "1" and "every book fresh" in p.card("markets").one("summary").text
    assert p.card("open-orders").one("details.disclosure").first(".disclosure-body").has('[data-action="cancel-all"]')
    assert p.card("exchange").one("summary").has('[data-chip="exchange-up"]') or p.card("exchange").one("summary").has('[data-chip="exchange-down"]')
    order = p.card("open-orders").rows("order")[0]
    assert order.one(".row-main").tag == "div" and order.one("details.menu").has('[data-action="cancel"]')


def test_ledger_problems_open_their_disclosure(client: Any, conn: Any) -> None:
    trade_setup(conn)
    assert not page(client.get("/trading").text).card("ledger").one("details").is_open
    conn.execute("UPDATE bankrolls SET available_cents = available_cents + 1")
    ledger = page(client.get("/trading").text).card("ledger")
    assert ledger.one("details").is_open and ledger.first("summary").has('[data-chip="ledger-problems"]')


def test_every_chip_has_a_word_and_every_details_a_summary(client: Any, conn: Any) -> None:
    setup = trade_setup(conn)
    approved_order(conn, setup, size=10)
    p = page(client.get("/trading").text)
    assert all(c.text for c in p.select(".chip"))
    for d in p.select("details"):
        summary = next(c for c in d.children if not isinstance(c, str) and c.tag == "summary")
        assert summary.text or summary.attr("aria-label"), d
    assert all(len(r.select(".row-meta")) <= 2 for r in p.select(".row")), "two lines at most"
    assert chr(0x2014) not in client.get("/trading").text


def test_ingame_slot_shows_a_second_meta_line(client: Any, conn: Any, monkeypatch: Any) -> None:
    """Step 6C sets assignment["ingame_line"]; the row shows it as its second .row-meta."""
    from host.api import dashboard_trading
    from host.trading import assignments

    setup = trade_setup(conn)
    real = assignments.list_assignments

    def with_line(c: Any, *a: Any, **kw: Any) -> list[dict[str, Any]]:
        rows = real(c, *a, **kw)
        for r in rows:
            r["ingame_line"] = "Q3 7:12 · KC 17-10 · 4 s ago"
        return rows

    monkeypatch.setattr(dashboard_trading.assignments, "list_assignments", with_line)
    row = page(client.get("/fragments/trading").text).row("assignment", setup.assignment["id"])
    metas = row.select(".row-meta")
    assert len(metas) == 2 and metas[1].text == "Q3 7:12 · KC 17-10 · 4 s ago"


def test_view_shaping_helpers() -> None:
    from host.trading.views_summary import exchange_words, stale_markets, tone

    assert tone("assignment", "active") == "ok" and tone("assignment", "halted") == "warn"
    assert tone("order", "rejected") == "bad" and tone("order", "cancelled") == "muted" and tone("market", "nope") == "muted"
    assert exchange_words({"down": True, "market_source": None, "heartbeat_age_s": None}) == {"value": "DOWN", "note": "no source · never"}
    assert stale_markets([{"snapshot_age_s": None}, {"snapshot_age_s": 61.0}, {"snapshot_age_s": 3.0}]) == 2


def test_probe_page_leads_with_its_title_and_stats(client: Any, conn: Any) -> None:
    r = client.post("/exchange/probe", data={}, follow_redirects=False)
    p = page(r.text)
    assert r.status_code == 200 and p.page_name == "probe" and p.one("main").select("*")[0].tag == "h1"
    assert p.stat("probe-source").one(".stat-value").text == "sim" and p.has('[data-copy="payload"]')
    assert "/trading#exchange" in p.one("main").hrefs
