"""Step 6B review fixes on the host data side: a sim replay never ranks, the model-page
replay runs through the latest season and an empty replay never wipes a good one, the
games feed serves a snapshot backtest its own injury cutoff, the trade state's team
stats skip rows without EPA like the worker's cache, and the Models rows wrap at
desktop width."""
from __future__ import annotations

import re
from datetime import timedelta

from host import games_feed, signals
from host.jobparams import prepare_params
from host.snapshot_store import latest_season
from tests.conftest import (
    ingest_fixture, insert_model, insert_validated_model, lease_job, model_row, set_setting,
)
from tests.test_ingest_signals import load_injuries, load_pbp
from tests.test_snapshot_board import snapshot_metrics, with_snapshot

LEAKY_GAME = "2023_03_CAR_SEA"


# ------------------------------------------------------------------ sim replays never rank

def test_a_sim_replay_is_shown_but_never_ranks(client, conn):
    set_setting(conn, "allow_sim_prices", False)
    validated = insert_validated_model(conn, params={"k": 1.0})
    sim = with_snapshot(conn, insert_model(conn, params={"k": 2.0}), snapshot_metrics(n_bets=30, platform="sim"))
    sim_validated = with_snapshot(conn, insert_validated_model(conn, params={"k": 3.0}),
                                  snapshot_metrics(n_bets=300, clv=0.5, platform="sim"))
    board = client.get("/api/models").json()
    ranked = {m["id"]: m for m in board["ranked"]}
    unranked = {m["id"]: m for m in board["unranked"]}
    assert str(sim["id"]) in unranked and unranked[str(sim["id"])]["unranked_reason"] == "not validated"
    assert unranked[str(sim["id"])]["rank_mode"] == "validation"
    assert unranked[str(sim["id"])]["snapshot"]["platform"] == "sim", "the sim result stays visible"
    assert ranked[str(sim_validated["id"])]["rank_mode"] == "validation", "even while sim prices are allowed or not"
    assert {m["rank_mode"] for m in board["ranked"]} == {"validation"} and str(validated["id"]) in ranked
    set_setting(conn, "allow_sim_prices", True)
    again = {m["id"]: m for m in client.get("/api/models").json()["ranked"]}
    assert again[str(sim_validated["id"])]["rank_mode"] == "validation"


# ------------------------------------------------------------------ replay seasons and empty results

def test_replay_without_seasons_runs_through_the_latest_season_under_the_migrated_default(conn):
    ingest_fixture(conn)
    set_setting(conn, "backtest_seasons", [2010, 2021])
    model = insert_model(conn)
    out = prepare_params(conn, "backtest", {"model_id": str(model["id"]), "price_source": "snapshots"})
    assert "seasons" not in out
    assert out["backtest_seasons"] == [2010, latest_season(conn)], "the model page's button replays the recorded seasons"
    closing = prepare_params(conn, "backtest", {"model_id": str(model["id"])})
    assert closing["backtest_seasons"] == [2010, 2021], "a closing-line backtest keeps the search era"
    explicit = prepare_params(conn, "backtest", {"model_id": str(model["id"]), "price_source": "snapshots", "seasons": [2023, 2024]})
    assert explicit["seasons"] == [2023, 2024]


def test_an_empty_replay_keeps_the_earlier_snapshot_metrics(client, conn, make_worker):
    w = make_worker("box1", role="backtest")
    root = with_snapshot(conn, insert_model(conn), snapshot_metrics(n_bets=64))
    good = root["snapshot_metrics"]
    job = lease_job(conn, w, "backtest", {"model_id": str(root["id"]), "price_source": "snapshots"})
    empty = snapshot_metrics(n_bets=0, clv=None, n_games=0, seasons=[], per_season=[])
    r = client.post(f"/api/v1/models/{root['id']}/backtest", json={"job_id": str(job["id"]), "backtest_metrics": empty},
                    headers=w.headers)
    assert r.status_code == 200, r.text
    assert model_row(conn, root["id"])["snapshot_metrics"] == good, "a replay that scored nothing never wipes a good one"
    event = conn.execute("SELECT detail FROM job_events WHERE job_id = %s AND event = 'model_snapshot_backtest'",
                         (job["id"],)).fetchone()
    assert event["detail"]["stored"] is False and "kept" in event["detail"]["note"]


# ------------------------------------------------------------------ the feed's injury cutoff

def _leaky_report(conn) -> None:
    """Bryce Young (CAR, week 3) listed Out 120 minutes before kickoff."""
    kickoff = conn.execute("SELECT kickoff_at FROM games WHERE game_id = %s", (LEAKY_GAME,)).fetchone()["kickoff_at"]
    conn.execute("UPDATE injuries SET date_modified = %s WHERE full_name = 'Bryce Young' AND week = 3",
                 (kickoff - timedelta(minutes=120),))


def _game(body: dict, game_id: str) -> dict:
    return next(g for g in body["games"] if g["game_id"] == game_id)


def test_a_snapshot_backtest_gets_its_own_injury_cutoff_after_the_setting_changes(client, conn, make_worker):
    ingest_fixture(conn)
    load_injuries(conn)
    _leaky_report(conn)
    worker = make_worker()
    set_setting(conn, "decision_minutes_before_kickoff", 300)
    model = insert_model(conn)
    params = prepare_params(conn, "backtest", {"model_id": str(model["id"]), "price_source": "snapshots"})
    assert params["decision_minutes_before_kickoff"] == 300
    set_setting(conn, "decision_minutes_before_kickoff", 30)  # the owner queues a second replay
    url = f"/api/v1/data/games?decision_minutes={params['decision_minutes_before_kickoff']}"
    own = client.get(url, headers=worker.headers)
    assert own.status_code == 200 and own.json()["decision_minutes_before_kickoff"] == 300
    signals_300 = _game(own.json(), LEAKY_GAME)["signals"]
    assert signals_300["away_out_qb"] == 0 and signals_300["away_out_count"] == 0, "a report after the bet is never counted"
    assert own.headers["etag"].strip('"').endswith(".d300")
    plain = client.get("/api/v1/data/games", headers=worker.headers)
    assert plain.json()["decision_minutes_before_kickoff"] == 30 and _game(plain.json(), LEAKY_GAME)["signals"]["away_out_qb"] == 1
    assert plain.headers["etag"] != own.headers["etag"], "the two cutoffs never share a cached body"
    assert client.get(url, headers={**worker.headers, "If-None-Match": own.headers["etag"]}).status_code == 304
    for bad in ("301", "-1", "abc", "1.5", ""):
        r = client.get(f"/api/v1/data/games?decision_minutes={bad}", headers=worker.headers)
        assert r.status_code == 400 and "decision_minutes" in r.text, bad
    assert games_feed.feed(conn, 300)["decision_minutes_before_kickoff"] == 300


# ------------------------------------------------------------------ team stats paths agree

def test_team_stats_skip_rows_without_epa_like_the_worker_cache(conn):
    ingest_fixture(conn)
    load_pbp(conn)
    conn.execute("UPDATE team_game_stats SET def_epa_per_play = NULL WHERE game_id = '2023_02_KC_JAX' AND team = 'KC'")
    kc = signals.game_signals(conn, ["2023_03_CHI_KC"])["2023_03_CHI_KC"]["team_stats"]["home"]
    assert [r["game_id"] for r in kc] == ["2023_01_DET_KC"], "the worker's normalise_stat_row drops the null-EPA row too"


# ------------------------------------------------------------------ Models rows wrap at desktop width

def test_models_record_cells_can_wrap(client, conn):
    model = with_snapshot(conn, insert_model(conn, params={"k": 1.0}), snapshot_metrics(n_bets=30))
    html = client.get("/models").text
    row = re.search(rf'<tr class="model-row" data-model="{model["id"]}">.*?</tr>', html, re.S).group(0)
    assert '<td class="c-paper">' in row and '<td class="c-snapshot">' in row, "no nowrap on the two long record cells"
    css = client.get("/static/style.css").text
    assert "table.models td.c-paper, table.models td.c-snapshot { min-width: 9rem; }" in css
    assert ".chip.chip-snapshot { background: transparent; color: var(--accent); border: 1px solid var(--accent); }" in css
