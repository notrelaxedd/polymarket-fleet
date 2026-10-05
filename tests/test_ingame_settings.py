"""The step 6C settings: every in-game and game-state key has a validator with the
contract's ranges, the seeds pass them, and the Settings page's "In-game" group shows,
saves and rejects them."""
from __future__ import annotations

import pytest

from host.errors import BadRequest
from host.settings import SCHEMA, get_settings, set_settings
from host.settings_forms import GROUPS, parse_group
from host.settings_schema_ingame import INGAME_SCHEMA
from tests.conftest import flash_cookie
from tests.pagecheck import page

INGAME_KEYS = {
    "trade_ingame", "ingame_tick_s", "ingame_max_state_age_s", "ingame_quiet_seconds", "ingame_cutoff_seconds",
    "ingame_dead_zone", "ingame_min_edge", "ingame_max_bet_cents", "ingame_gtd_seconds", "ingame_max_lag_s",
    "ingame_lag_min_events", "gamestate_poll_s", "gamestate_max_rps", "gamestate_sources", "espn_summary_url",
    "yahoo_pbp_url", "yahoo_poll_s",
}
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={event_id}"
GOOD_FORM = {
    "trade_ingame": "true", "ingame_tick_s": "4", "ingame_max_state_age_s": "25", "ingame_quiet_seconds": "30",
    "ingame_cutoff_seconds": "180", "ingame_dead_zone": "0.04", "ingame_min_edge": "0.06", "ingame_max_bet": "7.50",
    "ingame_gtd_seconds": "45", "ingame_max_lag_s": "15.5", "ingame_lag_min_events": "8", "gamestate_poll_s": "3",
    "gamestate_max_rps": "0.5", "gamestate_espn": "true", "gamestate_yahoo": "true", "espn_summary_url": ESPN,
    "yahoo_pbp_url": "https://sports.yahoo.com/nfl/pbp?gameid={event_id}", "yahoo_poll_s": "20",
}


def check(key, value):
    return SCHEMA[key](value)


def test_every_ingame_key_has_a_validator_and_the_seeds_pass(conn):
    stored = get_settings(conn)
    assert INGAME_KEYS <= set(stored), "migration 0009 seeds every key"
    assert set(INGAME_SCHEMA) == INGAME_KEYS and INGAME_KEYS <= set(SCHEMA)
    for key in INGAME_KEYS:
        assert check(key, stored[key]) is None, (key, stored[key])
    assert stored["ingame_lag_min_events"] == 5 and stored["gamestate_sources"] == ["espn"]


@pytest.mark.parametrize("key,good,bad", [
    ("gamestate_poll_s", [3, 4, 5, 3.5], [2, 2.99, 5.01, 6, "4", None, True]),
    ("gamestate_max_rps", [0.01, 1.0, 10], [0, -1, 10.01, "1", None]),
    ("gamestate_sources", [["espn"], ["yahoo"], ["espn", "yahoo"], []], [["nfl"], ["espn", "espn"], "espn", [1], None]),
    ("espn_summary_url", [ESPN, "http://x.test/s/{event_id}"], ["https://x.test/summary", "ftp://x/{event_id}", "", None]),
    ("yahoo_pbp_url", ["", "https://y.test/{event_id}"], ["https://y.test/pbp", "y.test/{event_id}", None, 3]),
    ("yahoo_poll_s", [5, 12, 120], [4, 121, 12.5, "12"]),
    ("ingame_lag_min_events", [1, 5, 100], [0, 101, 2.5, None]),
    ("trade_ingame", [True, False], ["true", 1, None]),
    ("ingame_tick_s", [1, 60], [0, 61, 1.5]),
    ("ingame_max_state_age_s", [5, 300], [4, 301]),
    ("ingame_quiet_seconds", [0, 300], [-1, 301]),
    ("ingame_cutoff_seconds", [0, 900], [-1, 901]),
    ("ingame_dead_zone", [0, 0.03, 0.5], [-0.01, 0.51, "0.03"]),
    ("ingame_min_edge", [0, 0.05, 0.5], [-0.01, 0.51]),
    ("ingame_max_bet_cents", [0, 500], [-1, 5.5]),
    ("ingame_gtd_seconds", [10, 3600], [9, 3601]),
    ("ingame_max_lag_s", [0.5, 20, 600], [0, -5, 600.5]),
])
def test_ingame_validator_ranges(key, good, bad):
    for value in good:
        assert check(key, value) is None, (key, value)
    for value in bad:
        assert check(key, value), (key, value)


def test_bad_ingame_settings_are_refused_with_the_key(conn):
    with pytest.raises(BadRequest, match="gamestate_poll_s must be between 3 and 5"):
        set_settings(conn, {"gamestate_poll_s": 10}, "owner")
    with pytest.raises(BadRequest, match=r"espn_summary_url must contain \{event_id\}"):
        set_settings(conn, {"espn_summary_url": "https://x.test/summary"}, "owner")
    with pytest.raises(BadRequest, match="gamestate_sources holds unknown sources: nfl"):
        set_settings(conn, {"gamestate_sources": ["espn", "nfl"]}, "owner")
    out = set_settings(conn, {"ingame_lag_min_events": 12, "gamestate_max_rps": 2.5}, "owner")
    assert out["ingame_lag_min_events"] == 12 and out["gamestate_max_rps"] == 2.5


def test_the_ingame_group_parses_every_key():
    assert "ingame" in GROUPS
    updates = parse_group("ingame", GOOD_FORM)
    assert set(updates) == INGAME_KEYS
    assert updates["ingame_max_bet_cents"] == 750 and updates["gamestate_sources"] == ["espn", "yahoo"]
    assert updates["gamestate_poll_s"] == 3 and isinstance(updates["gamestate_poll_s"], int)
    assert updates["ingame_max_lag_s"] == 15.5 and updates["trade_ingame"] is True
    unticked = {k: v for k, v in GOOD_FORM.items() if k not in ("trade_ingame", "gamestate_yahoo")}
    out = parse_group("ingame", unticked)
    assert out["trade_ingame"] is False and out["gamestate_sources"] == ["espn"]
    for name, message in (("ingame_tick_s", "In-game tick (s) must be a whole number"),
                          ("gamestate_max_rps", "ESPN max requests per second must be a number"),
                          ("ingame_max_bet", "In-game max bet")):
        with pytest.raises(BadRequest, match=message.replace("(", r"\(").replace(")", r"\)")):
            parse_group("ingame", {**GOOD_FORM, name: "lots"})


def test_settings_page_ingame_group_saves_and_rejects(client, conn):
    p = page(client.get("/settings").text)
    form = p.form("ingame")
    assert form.target == "/settings/ingame" and form.attr("id") == "ingame" and form.attr("data-card") == "ingame"
    group = form.closest("details")
    assert group.attr("data-key") == "settings-ingame" and not group.is_open, "a closed group like the others"
    assert "paper only" in group.one(".disclosure-summary").text
    assert p.field("ingame_max_bet").one("input").attr("value") == "5.00"
    assert p.field("gamestate_poll_s").one("input").attr("value") == "4"
    assert p.field("ingame_lag_min_events").one("input").attr("value") == "5"
    assert p.field("yahoo_pbp_url").one("input").attr("value") == ""
    assert p.input("gamestate_espn").has_attr("checked") and not p.input("gamestate_yahoo").has_attr("checked")
    r = client.post("/settings/ingame", data=GOOD_FORM, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "ingame settings saved"
    s = client.get("/api/settings").json()
    assert s["trade_ingame"] is True and s["ingame_max_bet_cents"] == 750 and s["gamestate_sources"] == ["espn", "yahoo"]
    assert s["gamestate_max_rps"] == 0.5 and s["ingame_lag_min_events"] == 8 and s["yahoo_poll_s"] == 20
    assert s["yahoo_pbp_url"] == GOOD_FORM["yahoo_pbp_url"]
    audited = {r["entity"] for r in conn.execute("SELECT entity FROM audit_log WHERE action = 'settings_changed'").fetchall()}
    assert {"trade_ingame", "ingame_max_bet_cents", "gamestate_sources"} <= audited
    for bad, message in [
        ({**GOOD_FORM, "gamestate_poll_s": "6"}, "gamestate_poll_s must be between 3 and 5"),
        ({**GOOD_FORM, "gamestate_max_rps": "0"}, "gamestate_max_rps must be above 0 and at most 10"),
        ({**GOOD_FORM, "espn_summary_url": "https://x.test/summary"}, "espn_summary_url must contain {event_id}"),
        ({**GOOD_FORM, "yahoo_pbp_url": "https://y.test/pbp"}, "yahoo_pbp_url must be empty or contain {event_id}"),
        ({**GOOD_FORM, "yahoo_poll_s": "4"}, "yahoo_poll_s must be between 5 and 120"),
        ({**GOOD_FORM, "ingame_lag_min_events": "0"}, "ingame_lag_min_events must be between 1 and 100"),
        ({**GOOD_FORM, "ingame_dead_zone": "0.9"}, "ingame_dead_zone must be between 0 and 0.5"),
    ]:
        r = client.post("/settings/ingame", data=bad, follow_redirects=False)
        assert r.status_code == 400, bad
        assert message in r.text, message
        rejected = page(r.text)
        assert rejected.form("ingame").closest("details").is_open, "the rejected group opens"
        assert message in rejected.form("ingame").one(".inline-error").text
        notice = rejected.one("[data-errors]")
        assert notice.one("a").target == "#ingame" and "In-game" in notice.text
    assert client.get("/api/settings").json()["gamestate_poll_s"] == 3, "nothing saved on a rejected form"
    off = {k: v for k, v in GOOD_FORM.items() if k not in ("trade_ingame", "gamestate_yahoo")}
    assert client.post("/settings/ingame", data={**off, "yahoo_pbp_url": ""}, follow_redirects=False).status_code == 303
    s = client.get("/api/settings").json()
    assert s["trade_ingame"] is False and s["gamestate_sources"] == ["espn"] and s["yahoo_pbp_url"] == ""
