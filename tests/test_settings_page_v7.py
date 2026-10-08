"""Step 7 Settings page (docs/UI.md "Settings"): the groups as disclosures with a
one-line summary of the stored values, help as a muted line under each input, the
stats on the first screen, a rejected form opening its group, and the small pages
around it (enroll token, kill confirm, error). Forms post exactly as before; their
behaviour is covered in test_dashboard.py, test_kill_switch.py and test_snapshot_params.py."""
from __future__ import annotations

import pytest

from host import settings_summary
from tests.conftest import enable_live, flash_cookie, set_setting
from tests.pagecheck import Node, page

# group key -> (title, the data-form / data-card names it holds)
GROUPS = {
    "settings-limits": ("Limits", ["trading"]),
    "settings-trading": ("Trading", ["trade"]),
    "settings-ingame": ("In-game", ["ingame"]),
    "settings-stocks": ("Stocks", ["stocks"]),  # step 9
    "settings-robustness": ("Robustness gates", ["thresholds", "seasons", "fees"]),
    "settings-replay": ("Snapshot replay and signals", ["replay", "signals"]),
    "settings-fleet": ("Fleet", ["fleet", "tz", "enroll"]),
    "settings-live": ("Live trading", ["live", "kill"]),
    "settings-data": ("Data", ["nflverse"]),
    "settings-audit": ("Audit log", ["audit"]),
}


def _settings(client) -> Node:  # noqa: ANN001 - a TestClient
    return page(client.get("/settings").text)


def _group(p: Node, key: str) -> Node:
    return p.one(f'details.disclosure[data-key="{key}"]')


def _summary(p: Node, key: str) -> str:
    return _group(p, key).one(".disclosure-summary").text


def test_every_group_is_a_closed_disclosure_with_a_summary(client):
    p = _settings(client)
    assert p.page_name == "settings" and p.one("main > h1").text == "Settings"
    keys = [d.attr("data-key") for d in p.select("details.disclosure")]
    assert keys == list(GROUPS), "the groups in reading order"
    for key, (title, cards) in GROUPS.items():
        group = _group(p, key)
        assert group.one("summary .disclosure-title").text == title, key
        assert group.one(".disclosure-summary").text, f"{key}: the header says what is in force"
        assert not group.is_open, f"{key}: closed on a plain GET, so the page reads at a glance"
        for name in cards:
            assert group.has(f'[data-card="{name}"]'), (key, name)
    assert p.one('[data-card="trading"]').closest("details").attr("data-key") == "settings-limits"
    assert not p.has("[data-errors]") and not p.has(".inline-error")


def test_the_summaries_read_the_stored_values(client, conn):
    p = _settings(client)
    assert "max bet $25.00" in _summary(p, "settings-limits")
    assert "daily loss $1,000.00 paper" in _summary(p, "settings-limits")
    assert _summary(p, "settings-trading").startswith("Sim · participation 50.0%")
    assert "pregame only" in _summary(p, "settings-trading")
    assert _summary(p, "settings-ingame") == "off by default · max bet $5.00 · min edge 5.0% · lag limit 20 s · feed ESPN · paper only"
    robust = _summary(p, "settings-robustness")
    assert "50 bets" in robust and "validation on" in robust and "forbids Overfit, Fragile" in robust
    assert "search 2010-2021" in robust and "held out from 2022" in robust
    assert _summary(p, "settings-replay") == "decides 60 min before kickoff · sim prices off · signals every 24 h"
    assert "lease 30 s" in _summary(p, "settings-fleet") and "America/New_York" in _summary(p, "settings-fleet")
    assert _summary(p, "settings-live").startswith("off · kill switch off")
    assert "refresh every" in _summary(p, "settings-data") and "games" in _summary(p, "settings-data")
    good = {"max_bet": "12.50", "max_daily_loss_paper": "1500", "max_daily_loss_live": "300", "default_bankroll": "100",
            "liquidity_floor": "500", "min_edge": "0.05", "kelly_fraction": "0.25", "trade_max_games": "4"}
    r = client.post("/settings/trading", data=good, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "trading settings saved"
    p = _settings(client)
    assert "max bet $12.50" in _summary(p, "settings-limits") and "min edge 5.0%" in _summary(p, "settings-limits")
    assert p.stat("max-bet").one(".stat-value").text == "$12.50"


def test_a_rejected_form_opens_its_group_and_says_where(client):
    r = client.post("/settings/seasons", data={"seasons_first": "x", "validation_first": "2022", "search_workers": "auto"},
                    follow_redirects=False)
    assert r.status_code == 400
    p = page(r.text)
    assert _group(p, "settings-robustness").is_open, "the error is in view"
    assert not _group(p, "settings-limits").is_open and not _group(p, "settings-live").is_open
    assert p.card("seasons").texts(".inline-error"), "the message sits in its form"
    notice = p.one("[data-errors]")
    assert notice.one("a").target == "#seasons" and "Robustness gates" in notice.text
    assert p.input("seasons_first").attr("value") == "x", "the typed value is kept"
    assert "search 2010-2021" in _summary(p, "settings-robustness"), "the header keeps the stored values"


def test_a_rejected_post_keeps_the_stats_on_the_stored_values(client, conn):
    """The stats say what is in force: a rejected Trading post re-renders its form with
    the typed market source, but the Market source stat keeps the stored one."""
    form = _settings(client).form("trade")
    data = {i.attr("name"): i.attr("value") or "" for i in form.select("input")
            if i.attr("name") and i.attr("type") not in ("checkbox", "submit")}
    data.update({t.attr("name"): t.text for t in form.select("textarea")})
    data.update(market_source="polymarket_us", participation="7")
    r = client.post("/settings/trade", data=data, follow_redirects=False)
    assert r.status_code == 400
    p = page(r.text)
    assert p.stat("market-source").one(".stat-value").text == "Sim", "the stored source, not the rejected one"
    assert p.input("market_source").one("option[selected]").attr("value") == "polymarket_us", "the form keeps what was typed"
    assert _summary(p, "settings-trading").startswith("Sim")
    stored = conn.execute("SELECT value FROM settings WHERE key = 'market_source'").fetchone()["value"]
    assert stored == "sim"


def test_pregame_only_hint_names_the_in_game_rules(client):
    hint = _settings(client).field("trade_pregame_only").one(".help").text
    assert hint == "no orders after kickoff; unticked, orders after kickoff go only through the in-game rules (paper only)"


def test_help_is_a_muted_line_under_the_input(client):
    p = _settings(client)
    wrappers = p.select("[data-field]")
    assert len(wrappers) > 50
    helped = 0
    for box in wrappers:
        for help_line in box.select(".help"):
            control = box.first("input, select, textarea")
            assert control is not None and control.start < help_line.start, box.attr("data-field")
            helped += 1
    assert helped >= 40, "most fields explain themselves"
    assert "dollars" in p.field("max_bet").one(".help").text
    assert not p.has(".fields .muted.small"), "no hint beside the label any more"


def test_stats_on_the_first_screen(client, conn):
    p = _settings(client)
    stats = p.one(".stats")
    assert stats.start < p.first("details.disclosure").start, "the stats come before the groups"
    assert p.stat("live").one(".stat-value").text == "Off" and p.stat("live").target == "#live"
    assert p.stat("kill").one(".stat-value").text == "Off" and p.stat("kill").target == "#kill"
    assert p.stat("market-source").one(".stat-value").text == "Sim"
    assert p.count("[data-live-state]") == 1, "the live state hook stays unique"
    enable_live(conn)
    client.post("/kill", follow_redirects=False)
    p = _settings(client)
    assert p.stat("kill").one(".stat-value").text == "Killed"
    assert _group(p, "settings-live").is_open, "a killed fleet opens the group with the reset"
    assert "trading killed" in _summary(p, "settings-live")
    assert p.form("kill-reset").closest("details").attr("data-key") == "settings-live"


def test_audit_rows_are_one_line_each(client, conn):
    set_setting(conn, "max_bet_cents", 2500)
    client.post("/settings/tz", data={"tz": "Europe/Berlin"}, follow_redirects=False)
    p = _settings(client)
    rows = p.rows("audit")
    assert rows and rows[0].one(".row-title").text.startswith("Settings Changed")
    assert "CET" in rows[0].one(".row-meta").text or "CEST" in rows[0].one(".row-meta").text
    assert _group(p, "settings-audit").one(".count").text == str(len(rows))


def test_the_small_pages_lead_with_their_title(client):
    for html, name, title in [
        (client.get("/kill/confirm").text, "kill", "Kill trading?"),
        (client.post("/enroll-token").text, "enroll", "New enroll token"),
        (client.get("/nope").text, "error", "404 Not found"),
    ]:
        p = page(html)
        main = p.one("main.page")
        assert main.attr("data-page") == name
        first = next(n for n in main.children if isinstance(n, Node) and not n.has_attr("data-flash"))
        assert first.tag == "h1" and first.text == title, name
    p = page(client.get("/kill/confirm").text)
    assert p.one('form[data-action="kill"]').target == "/kill" and "keep running" in p.text


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"max_bet_cents": "lots", "max_daily_loss_cents": [1], "min_edge": True, "thresholds_backtest": "x",
         "backtest_seasons": None, "validation_seasons": [None], "max_exposure_cents": {"paper": "x"}},
    ],
)
def test_summaries_survive_missing_or_malformed_values(settings):
    out = settings_summary.summaries(settings)
    assert set(out) == {f"summary_{g}" for g in settings_summary.GROUPS}
    assert all(text and chr(0x2014) not in text for text in out.values())
    assert "max bet -" in out["summary_limits"] and "retry forever" in out["summary_fleet"]


def test_summary_formats():
    s = {"max_bet_cents": 123456, "max_daily_loss_cents": {"paper": 100000, "live": 30000}, "min_edge": 0.03,
         "kelly_fraction": 0.25, "default_bankroll_cents": 10000, "trade_max_games": 6,
         "max_exposure_cents": {"paper": 0, "live": 5000}, "market_source": "polymarket_us", "participation": 0.5,
         "trade_pregame_only": False, "thresholds_paper": {"min_games": 5, "min_bets": 30},
         "thresholds_backtest": {"min_bets": 50, "min_roi": 0.02, "require_validation": False, "forbid_flags": ["regime_dependent"]},
         "backtest_seasons": [2010, None], "validation_seasons": [2022, 2024], "max_expiries": 3}
    out = settings_summary.summaries(s)
    assert out["summary_limits"].startswith("max bet $1,234.56 · daily loss $1,000.00 paper, $300.00 live · min edge 3.0% · Kelly 0.25")
    assert "exposure $50.00 live" in out["summary_trading"] and "orders after kickoff allowed" in out["summary_trading"]
    assert "validation off" in out["summary_robustness"] and "forbids Regime Dependent" in out["summary_robustness"]
    assert "search from 2010" in out["summary_robustness"] and "held out 2022-2024" in out["summary_robustness"]
    assert "3 expiries" in out["summary_fleet"]
    assert out["summary_ingame"] == "off by default · max bet - · min edge - · lag limit - · feed off · paper only"
    ingame = {"trade_ingame": True, "ingame_max_bet_cents": 500, "ingame_min_edge": 0.05, "ingame_max_lag_s": 20,
              "gamestate_sources": ["espn", "yahoo"]}
    assert settings_summary.ingame(ingame) == "on for new assignments · max bet $5.00 · min edge 5.0% · lag limit 20 s · feed ESPN, Yahoo · paper only"
