"""Step 6 Part B (docs/ROBUSTNESS.md B1): backtest params for snapshot replay, the
settings they copy, the Jobs form's price source choice and the new Settings keys."""
from __future__ import annotations

import re

import pytest

from host.errors import BadRequest
from host.jobparams import prepare_params
from host.settings import get_settings, validate_settings
from host.settings_forms import GROUPS, form_values, parse_group
from tests.conftest import flash_cookie, insert_model, set_setting

REPLAY_KEYS = {"decision_minutes_before_kickoff", "allow_sim_prices", "price_platform", "participation"}


def test_backtest_price_source_defaults_to_closing_line_without_replay_settings(conn):
    out = prepare_params(conn, "backtest", {"family": "elo_blend", "params": {"k": 20}})
    assert "price_source" not in out, "absent means closing_line; older params stay byte-identical"
    assert not REPLAY_KEYS & set(out), "a closing-line backtest carries no replay settings"
    named = prepare_params(conn, "backtest", {"family": "elo_blend", "params": {}, "price_source": "closing_line"})
    assert named["price_source"] == "closing_line" and not REPLAY_KEYS & set(named)


def test_snapshot_backtest_copies_the_replay_settings_in_force(conn):
    model = insert_model(conn)
    out = prepare_params(conn, "backtest", {"model_id": str(model["id"]), "price_source": "snapshots"})
    assert out["price_source"] == "snapshots" and out["model_id"] == str(model["id"])
    assert out["decision_minutes_before_kickoff"] == 60 and out["allow_sim_prices"] is False
    assert out["price_platform"] == "sim" and out["participation"] == 0.5, "the seeded market_source and participation"
    assert out["fee_model"] and out["backtest_seasons"], "the closing-line copies still apply"
    set_setting(conn, "decision_minutes_before_kickoff", 90)
    set_setting(conn, "allow_sim_prices", True)
    set_setting(conn, "market_source", "polymarket_us")
    set_setting(conn, "participation", 0.25)
    out = prepare_params(conn, "backtest", {"family": "elo_blend", "params": {}, "price_source": "snapshots"})
    assert {k: out[k] for k in REPLAY_KEYS} == {
        "decision_minutes_before_kickoff": 90, "allow_sim_prices": True, "price_platform": "polymarket_us", "participation": 0.25,
    }


def test_malformed_replay_settings_fall_back_to_defaults(conn):
    set_setting(conn, "decision_minutes_before_kickoff", 301)
    set_setting(conn, "allow_sim_prices", "yes")
    set_setting(conn, "participation", 2)
    out = prepare_params(conn, "backtest", {"family": "elo_blend", "params": {}, "price_source": "snapshots"})
    assert out["decision_minutes_before_kickoff"] == 60 and out["allow_sim_prices"] is False and out["participation"] == 0.5


@pytest.mark.parametrize("value", ["bogus", "", None, 1, "SNAPSHOTS"])
def test_unknown_price_source_is_refused(conn, value):
    with pytest.raises(BadRequest, match="price_source must be one of closing_line, snapshots"):
        prepare_params(conn, "backtest", {"family": "elo_blend", "params": {}, "price_source": value})


def test_price_source_belongs_to_backtests_only(conn):
    with pytest.raises(BadRequest, match="unknown model_search params: price_source"):
        prepare_params(conn, "model_search", {"family": "elo_blend", "price_source": "snapshots"})
    model = insert_model(conn)
    with pytest.raises(BadRequest, match="unknown validate params: price_source"):
        prepare_params(conn, "validate", {"model_id": str(model["id"]), "price_source": "snapshots"})
    with pytest.raises(BadRequest, match="unknown backtest params: allow_sim_prices"):
        prepare_params(conn, "backtest", {"family": "elo_blend", "params": {}, "allow_sim_prices": True})


def test_job_api_stores_the_snapshot_params(client, conn):
    model = insert_model(conn)
    r = client.post("/api/jobs", json={"kind": "backtest", "params": {"model_id": str(model["id"]), "price_source": "snapshots"}})
    assert r.status_code in (200, 201), r.text
    params = conn.execute("SELECT params FROM jobs WHERE id = %s", (r.json()["id"],)).fetchone()["params"]
    assert params["price_source"] == "snapshots" and REPLAY_KEYS <= set(params)
    bad = client.post("/api/jobs", json={"kind": "backtest", "params": {"model_id": str(model["id"]), "price_source": "live"}})
    assert bad.status_code == 400 and "price_source" in bad.text


def test_jobs_page_offers_the_price_source_and_explains_it(client, conn):
    html = client.get("/jobs").text
    form = re.search(r'<details class="card send" id="backtest".*?</details>', html, re.S).group(0)
    assert '<select name="price_source">' in form
    assert '<option value="closing_line" selected>closing line (every game, CLV 0)</option>' in form
    assert '<option value="snapshots">snapshots (recorded prices, real CLV)</option>' in form
    assert "60 minutes before kickoff" in form and "<strong>sim</strong>" in form
    assert 'class="error small replay-sim"' in form, "sim platform with sim prices off: the form warns"
    set_setting(conn, "allow_sim_prices", True)
    assert "replay-sim" not in client.get("/jobs").text


def test_jobs_form_sends_a_snapshot_backtest(client, conn):
    model = insert_model(conn)
    r = client.post("/jobs", data={"kind": "backtest", "model_id": str(model["id"]), "family": "elo_blend", "params": "{}",
                                   "seasons_first": "", "seasons_last": "", "price_source": "snapshots", "target": "any_idle"},
                    follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("backtest job ")
    job = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert job["params"]["price_source"] == "snapshots" and job["params"]["price_platform"] == "sim"
    listing = client.get("/jobs").text
    assert '<span class="chip chip-snapshot">snapshots</span>' in listing
    page = client.get(f"/jobs/{job['id']}").text
    assert "price source <strong>snapshots</strong>: recorded sim prices 60 minutes before kickoff, participation 0.5" in page
    r = client.post("/jobs", data={"kind": "backtest", "model_id": str(model["id"]), "price_source": "bogus", "target": "any_idle"})
    assert r.status_code == 400 and "price_source must be one of" in r.text
    assert 'name="price_source"' in r.text and '<details class="card send" id="backtest" open>' in r.text


@pytest.mark.parametrize(
    "key, good, bad",
    [
        ("allow_sim_prices", [True, False], ["true", 1, None]),
        ("decision_minutes_before_kickoff", [0, 60, 300], [-1, 301, 60.5, "60", True]),
        ("signals_refresh_hours", [1, 24, 168], [0, 169, 1.5, "24"]),
        ("nflverse_injuries_url", ["https://x.test/injuries_{season}.csv"], ["https://x.test/injuries.csv", "ftp://x/{season}", 3]),
        ("nflverse_pbp_url", ["http://x.test/pbp/play_by_play_{season}.csv.gz"], ["play_by_play_{season}.csv.gz", ""]),
    ],
)
def test_settings_validators_for_the_new_keys(conn, key, good, bad):
    current = get_settings(conn)
    for value in good:
        validate_settings({key: value}, current)
    for value in bad:
        with pytest.raises(BadRequest):
            validate_settings({key: value}, current)


def test_seeded_values_pass_their_validators(conn):
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key IN ('allow_sim_prices', 'decision_minutes_before_kickoff',"
        " 'nflverse_injuries_url', 'nflverse_pbp_url', 'signals_refresh_hours')"
    ).fetchall()
    assert len(rows) == 5
    validate_settings({r["key"]: r["value"] for r in rows}, get_settings(conn))


def test_settings_forms_parse_the_two_new_groups():
    assert {"replay", "signals"} <= set(GROUPS)
    assert parse_group("replay", {"decision_minutes_before_kickoff": " 45 ", "allow_sim_prices": "true"}) == {
        "decision_minutes_before_kickoff": 45, "allow_sim_prices": True,
    }
    assert parse_group("replay", {"decision_minutes_before_kickoff": "60"})["allow_sim_prices"] is False, "unticked = false"
    with pytest.raises(BadRequest, match="Decision minutes before kickoff must be a whole number"):
        parse_group("replay", {"decision_minutes_before_kickoff": "an hour"})
    signals = parse_group("signals", {"nflverse_injuries_url": " https://a/{season}.csv ", "nflverse_pbp_url": "https://b/{season}.gz",
                                      "signals_refresh_hours": "12"})
    assert signals == {"nflverse_injuries_url": "https://a/{season}.csv", "nflverse_pbp_url": "https://b/{season}.gz",
                       "signals_refresh_hours": 12}
    values = form_values({"decision_minutes_before_kickoff": 60, "allow_sim_prices": True, "signals_refresh_hours": 24,
                          "nflverse_injuries_url": "https://a/{season}.csv"})
    assert values["decision_minutes_before_kickoff"] == "60" and values["allow_sim_prices"] == "true"
    assert values["signals_refresh_hours"] == "24" and values["nflverse_pbp_url"] == ""


def test_settings_page_saves_and_rejects_the_new_groups(client, conn):
    html = client.get("/settings").text
    replay = re.search(r'<form class="card group" method="post" action="/settings/replay" id="replay">.*?</form>', html, re.S).group(0)
    assert 'name="decision_minutes_before_kickoff" value="60" inputmode="numeric"' in replay
    assert 'name="allow_sim_prices" value="true">' in replay and "testing only" in replay
    signals = re.search(r'<form class="card group" method="post" action="/settings/signals" id="signals">.*?</form>', html, re.S).group(0)
    assert 'name="signals_refresh_hours" value="24"' in signals and "must contain {season}" in signals
    assert "injuries_{season}.csv" in signals and "play_by_play_{season}.csv.gz" in signals
    r = client.post("/settings/replay", data={"decision_minutes_before_kickoff": "30", "allow_sim_prices": "true"}, follow_redirects=False)
    assert r.status_code == 303
    stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings").fetchall()}
    assert stored["decision_minutes_before_kickoff"] == 30 and stored["allow_sim_prices"] is True
    assert 'name="allow_sim_prices" value="true" checked>' in client.get("/settings").text
    r = client.post("/settings/replay", data={"decision_minutes_before_kickoff": "301"})
    assert r.status_code == 400 and "must be between 0 and 300" in r.text
    r = client.post("/settings/signals", data={"nflverse_injuries_url": "https://a/injuries.csv", "nflverse_pbp_url": "https://b/{season}.gz",
                                               "signals_refresh_hours": "24"})
    assert r.status_code == 400 and "must contain {season}" in r.text
    r = client.post("/settings/signals", data={"nflverse_injuries_url": "https://a/{season}.csv", "nflverse_pbp_url": "https://b/{season}.gz",
                                               "signals_refresh_hours": "6"}, follow_redirects=False)
    assert r.status_code == 303
    assert conn.execute("SELECT value FROM settings WHERE key = 'signals_refresh_hours'").fetchone()["value"] == 6
