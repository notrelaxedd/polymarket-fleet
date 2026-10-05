"""Injuries and play-by-play ingest, per-game signals and the games feed (docs/ROBUSTNESS.md B2).

Fixtures (cut from nflverse-data release files, CC BY 4.0):
- tests/fixtures/injuries_sample.csv: injuries/injuries_2023.csv, regular season weeks
  1-3 of DET, KC, NYJ, CAR, IND, SEA and BAL, columns season, game_type, team, week,
  gsis_id, position, full_name, report_status, date_modified.
- tests/fixtures/pbp_sample.csv.gz: pbp/play_by_play_2023.csv.gz, every play of
  2023_01_DET_KC, 2023_01_BUF_NYJ, 2023_02_KC_JAX, 2023_03_CHI_KC and 2023_03_CAR_SEA,
  columns play_id, game_id, season, week, season_type, game_date, home_team, away_team,
  posteam, defteam, play_type, epa, success, pass.
"""
from __future__ import annotations

import csv
import gzip
import io
from datetime import timedelta
from pathlib import Path

import pytest

from host import data_refresh, games_feed, ingest_injuries, ingest_pbp, signals
from host.errors import BadRequest, Upstream
from tests.conftest import ingest_fixture

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INJURIES = FIXTURES / "injuries_sample.csv"
PBP = FIXTURES / "pbp_sample.csv.gz"
INJURY_ROWS = 153


def load_injuries(conn):
    return ingest_injuries.ingest(conn, None, str(INJURIES))


def load_pbp(conn):
    return ingest_pbp.ingest(conn, None, str(PBP))


def fixture_outs(team: str, week: int) -> list[dict[str, str]]:
    with open(INJURIES, encoding="utf-8", newline="") as fh:
        return [r for r in csv.DictReader(fh) if r["team"] == team and int(r["week"]) == week and r["report_status"] == "Out"]


# --- injuries -------------------------------------------------------------------------


def test_injuries_upsert_is_idempotent_and_moves_the_stamp_only_on_change(conn):
    first = load_injuries(conn)
    assert first["rows"] == INJURY_ROWS and first["inserted"] == INJURY_ROWS and first["skipped"] == 0
    stamp = ingest_injuries.injuries_stamp(conn)
    assert stamp.startswith(f"{INJURY_ROWS}-")
    again = load_injuries(conn)
    assert (again["inserted"], again["changed"], again["updated"]) == (0, 0, 0)
    assert ingest_injuries.injuries_stamp(conn) == stamp, "a re-ingest of the same file changes nothing"
    young = conn.execute("SELECT * FROM injuries WHERE full_name = 'Bryce Young' AND week = 3").fetchone()
    assert young["game_type"] == "REG" and young["position"] == "QB" and young["report_status"] == "Out"
    assert young["date_modified"].isoformat() == "2023-09-22T17:09:41+00:00"
    text = INJURIES.read_text(encoding="utf-8").replace("Bryce Young,Out,", "Bryce Young,Questionable,")
    changed = ingest_injuries.ingest_text(conn, text, "edited")
    assert (changed["inserted"], changed["changed"]) == (0, 1)
    assert ingest_injuries.injuries_stamp(conn) != stamp


def test_injuries_parse_normalises_and_skips_bad_records():
    header = "season,game_type,team,week,gsis_id,position,full_name,report_status,date_modified\n"
    body = (
        "2016,WC,OAK,18,00-1,qb,A One,Out,2017-01-06T20:00:00Z\n"      # WC -> POST, OAK -> LV, qb -> QB
        "2016,REG,KC,3,,WR,No Id,Out,NA\n"                              # keyed by name, no date
        "NA,REG,KC,3,00-2,WR,Bad Season,Out,2016-09-20T00:00:00Z\n"     # skipped
        "2016,REG,KC,3,00-3,CB,Dup,Questionable,2016-09-21T00:00:00Z\n"
        "2016,REG,KC,3,00-3,CB,Dup,Out,2016-09-22T00:00:00Z\n"          # later date_modified wins
        "2016,REG,KC,3,00-3,CB,Dup,Doubtful,2016-09-20T00:00:00Z\n"
    )
    rows, skipped = ingest_injuries.parse_rows(header + body)
    assert skipped == 1
    by_id = {r["gsis_id"]: r for r in rows}
    assert by_id["00-1"]["game_type"] == "POST" and by_id["00-1"]["team"] == "LV" and by_id["00-1"]["position"] == "QB"
    assert by_id["name:No Id"]["date_modified"] is None
    assert by_id["00-3"]["report_status"] == "Out"
    with pytest.raises(BadRequest):
        ingest_injuries.parse_rows("a,b\n1,2\n")


# --- play-by-play ---------------------------------------------------------------------


def test_pbp_aggregate_by_hand():
    """Four counted plays and five that must not count, checked by hand."""
    text = (
        "game_id,season,week,game_date,posteam,defteam,play_type,epa,success,pass\n"
        "G,2023,1,2023-09-10,AAA,BBB,pass,0.5,1,1\n"
        "G,2023,1,2023-09-10,AAA,BBB,run,-0.25,0,0\n"
        "G,2023,1,2023-09-10,AAA,BBB,pass,1.0,1,1\n"
        "G,2023,1,2023-09-10,BBB,AAA,run,0.75,1,0\n"
        "G,2023,1,2023-09-10,AAA,BBB,no_play,3.0,1,0\n"     # penalty: not a regular play
        "G,2023,1,2023-09-10,AAA,BBB,punt,-1.0,0,0\n"
        "G,2023,1,2023-09-10,BBB,AAA,kickoff,0,0,0\n"
        "G,2023,1,2023-09-10,AAA,BBB,pass,NA,0,1\n"          # no epa
        "G,2023,1,2023-09-10,,,,,,\n"
    )
    agg = ingest_pbp.aggregate(io.StringIO(text))
    rows, skipped = agg.rows()
    assert skipped == 0 and agg.plays == 4
    a, b = rows
    assert (a["team"], a["plays"]) == ("AAA", 3)
    assert a["off_epa_per_play"] == pytest.approx((0.5 - 0.25 + 1.0) / 3)
    assert a["def_epa_per_play"] == pytest.approx(0.75)
    assert a["pass_rate"] == pytest.approx(2 / 3) and a["success_rate"] == pytest.approx(2 / 3)
    assert (b["team"], b["plays"], b["pass_rate"], b["success_rate"]) == ("BBB", 1, 0.0, 1.0)
    assert b["off_epa_per_play"] == pytest.approx(0.75) and b["def_epa_per_play"] == pytest.approx(1.25 / 3)
    assert a["kickoff_at"] == "2023-09-10T17:00:00+00:00", "gameday 13:00 Eastern without the games row"


def test_pbp_fixture_team_game_rows(conn):
    ingest_fixture(conn)
    result = load_pbp(conn)
    assert result["rows"] == 10 and result["inserted"] == 10 and result["plays"] == 638
    det = conn.execute("SELECT * FROM team_game_stats WHERE game_id = '2023_01_DET_KC' AND team = 'DET'").fetchone()
    # Checked against the fixture: DET ran 66 pass/run plays with an epa summing to 3.0899..
    assert det["plays"] == 66 and det["season"] == 2023 and det["week"] == 1
    assert det["off_epa_per_play"] == pytest.approx(0.046817, abs=1e-6)
    assert det["def_epa_per_play"] == pytest.approx(-0.145734, abs=1e-6)
    assert det["pass_rate"] == pytest.approx(35 / 66) and det["success_rate"] == pytest.approx(27 / 66)
    assert det["kickoff_at"].isoformat() == "2023-09-08T00:20:00+00:00", "kickoff from the games table"
    with gzip.open(PBP, "rt", encoding="utf-8", newline="") as fh:
        plays = [r for r in csv.DictReader(fh) if r["game_id"] == "2023_01_DET_KC" and r["posteam"] == "DET"
                 and r["play_type"] in ("pass", "run")]
    assert det["off_epa_per_play"] == pytest.approx(sum(float(r["epa"]) for r in plays) / len(plays))
    stamp = ingest_pbp.stats_stamp(conn)
    again = load_pbp(conn)
    assert again["updated"] == 0 and ingest_pbp.stats_stamp(conn) == stamp


def test_pbp_download_streams_and_caps(monkeypatch):
    payload = PBP.read_bytes()

    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(ingest_pbp.urllib.request, "urlopen", lambda request, timeout=0: Response(payload))
    agg = ingest_pbp.aggregate_url("https://example.invalid/play_by_play_2023.csv.gz")
    assert agg.plays == 638 and len(agg.rows()[0]) == 10
    with pytest.raises(Upstream, match="over 1000 bytes"):
        ingest_pbp.aggregate_url("https://example.invalid/x.csv.gz", max_bytes=1000)
    with pytest.raises(BadRequest):
        ingest_pbp.aggregate(io.StringIO("a,b\n1,2\n"))


# --- signals --------------------------------------------------------------------------


def test_qb_change_across_a_season_boundary(conn):
    ingest_fixture(conn)
    ids = ["2023_01_BUF_NYJ", "2023_02_NYJ_DAL", "2023_03_NE_NYJ", "2023_01_DET_KC", "2016_01_CAR_DEN"]
    got = signals.signals_for(conn, ids)
    assert got["2023_01_BUF_NYJ"]["home_qb_changed"] == 1, "Flacco (2022 week 18) to Rodgers"
    assert got["2023_02_NYJ_DAL"]["away_qb_changed"] == 1, "Rodgers to Wilson"
    assert got["2023_03_NE_NYJ"]["home_qb_changed"] == 0, "Wilson again"
    assert got["2023_01_DET_KC"]["home_qb_changed"] == 0 and got["2023_01_DET_KC"]["away_qb_changed"] == 0
    assert got["2016_01_CAR_DEN"]["home_qb_changed"] == 0, "a team's first game reads 0"
    every = signals.signals_for(conn, None)
    assert all(every[g] == got[g] for g in ids), "the feed and the trade state agree"
    # An unknown starter reads 0 and the next game compares with the last known one.
    conn.execute("UPDATE games SET raw = raw || '{\"away_qb_id\": \"\"}' WHERE game_id = '2023_02_NYJ_DAL'")
    got = signals.signals_for(conn, ["2023_02_NYJ_DAL", "2023_03_NE_NYJ"])
    assert got["2023_02_NYJ_DAL"]["away_qb_changed"] == 0
    assert got["2023_03_NE_NYJ"]["home_qb_changed"] == 1, "Rodgers (last known) to Wilson"


def test_out_counts_and_the_date_modified_leakage_rule(conn):
    ingest_fixture(conn)
    load_injuries(conn)
    game = "2023_03_CAR_SEA"
    sea, car = fixture_outs("SEA", 3), fixture_outs("CAR", 3)
    got = signals.signals_for(conn, [game])[game]
    assert got["away_out_qb"] == 1 and got["away_out_count"] == len(car) == 1, "Bryce Young listed Out"
    assert got["home_out_qb"] == 0 and got["home_out_count"] == len(sea)
    kickoff = conn.execute("SELECT kickoff_at FROM games WHERE game_id = %s", (game,)).fetchone()["kickoff_at"]
    # Modified 30 minutes before kickoff: after the 60-minute decision time, so unseen.
    conn.execute("UPDATE injuries SET date_modified = %s WHERE full_name = 'Bryce Young' AND week = 3",
                 (kickoff - timedelta(minutes=30),))
    got = signals.signals_for(conn, [game])[game]
    assert got["away_out_qb"] == 0 and got["away_out_count"] == 0
    conn.execute("UPDATE settings SET value = '10' WHERE key = 'decision_minutes_before_kickoff'")
    assert signals.signals_for(conn, [game])[game]["away_out_qb"] == 1, "decision 10 minutes out sees it"
    conn.execute("UPDATE settings SET value = '0' WHERE key = 'decision_minutes_before_kickoff'")
    conn.execute("UPDATE injuries SET date_modified = %s WHERE full_name = 'Bryce Young' AND week = 3", (kickoff,))
    assert signals.signals_for(conn, [game])[game]["away_out_qb"] == 0, "modified at kickoff never counts"
    conn.execute("UPDATE injuries SET date_modified = NULL WHERE full_name = 'Bryce Young' AND week = 3")
    assert signals.signals_for(conn, [game])[game]["away_out_count"] == 0, "no date never counts"


def test_game_signals_team_stats_strictly_before_kickoff(conn):
    ingest_fixture(conn)
    load_injuries(conn)
    load_pbp(conn)
    out = signals.game_signals(conn, ["2023_03_CHI_KC", "2023_01_DET_KC", "nope"])
    assert set(out) == {"2023_03_CHI_KC", "2023_01_DET_KC"}
    kc = out["2023_03_CHI_KC"]["team_stats"]["home"]
    assert [r["game_id"] for r in kc] == ["2023_01_DET_KC", "2023_02_KC_JAX"], "oldest first, this game excluded"
    assert set(kc[0]) == set(signals.STATS_COLUMNS) and kc[0]["kickoff_at"] == "2023-09-08T00:20:00Z"
    assert out["2023_03_CHI_KC"]["team_stats"]["away"] == []
    assert out["2023_01_DET_KC"]["team_stats"] == {"home": [], "away": []}
    assert set(out["2023_01_DET_KC"]["signals"]) == {"home_qb_changed", "away_qb_changed", "home_out_qb",
                                                      "away_out_qb", "home_out_count", "away_out_count"}
    kickoff = conn.execute("SELECT kickoff_at FROM games WHERE game_id = '2023_03_CHI_KC'").fetchone()["kickoff_at"]
    latest = signals.team_stats_before(conn, "KC", kickoff, limit=1)
    assert [r["game_id"] for r in latest] == ["2023_02_KC_JAX"], "the cap keeps the most recent rows"
    assert signals.game_signals(conn, []) == {}


# --- games feed -----------------------------------------------------------------------


def test_games_feed_carries_signals_and_stats_and_its_etag_follows_them(client, conn, make_worker):
    ingest_fixture(conn)
    worker = make_worker()
    first = client.get("/api/v1/data/games", headers=worker.headers)
    assert first.status_code == 200 and first.headers["cache-control"] == "no-cache"
    body = first.json()
    assert body["count"] == len(body["games"]) == 2761 and body["team_game_stats"] == []
    assert all(set(g["signals"]) == set(signals.empty_signals()) for g in body["games"])
    etag = first.headers["etag"]
    assert client.get("/api/v1/data/games", headers={**worker.headers, "If-None-Match": etag}).status_code == 304

    load_injuries(conn)
    second = client.get("/api/v1/data/games", headers={**worker.headers, "If-None-Match": etag})
    assert second.status_code == 200 and second.headers["etag"] != etag, "injuries move the tag"
    car = next(g for g in second.json()["games"] if g["game_id"] == "2023_03_CAR_SEA")
    assert car["signals"]["away_out_qb"] == 1
    etag = second.headers["etag"]

    load_pbp(conn)
    third = client.get("/api/v1/data/games", headers={**worker.headers, "If-None-Match": etag})
    assert third.status_code == 200 and third.headers["etag"] != etag, "team stats move the tag"
    stats = third.json()["team_game_stats"]
    assert len(stats) == 10 and set(stats[0]) == set(signals.STATS_COLUMNS)
    assert [(s["kickoff_at"], s["game_id"], s["team"]) for s in stats] == sorted(
        (s["kickoff_at"], s["game_id"], s["team"]) for s in stats)
    assert stats[0]["game_id"] == "2023_01_DET_KC" and [s["team"] for s in stats[:2]] == ["DET", "KC"]
    etag = third.headers["etag"]

    conn.execute("UPDATE settings SET value = '30' WHERE key = 'decision_minutes_before_kickoff'")
    assert games_feed.feed_etag(conn) != etag, "the decision lead changes the injury signals"
    assert client.get("/api/v1/data/games").status_code == 401


# --- refresh and CLI ------------------------------------------------------------------


def test_signals_refresher_schedules_seasons_and_survives_failures(pool, conn, monkeypatch):
    ingest_fixture(conn)
    calls: list[tuple[str, int]] = []

    def fake_season(connect, kind, template, season):
        calls.append((kind, season))
        assert "{season}" in template
        if kind == "pbp" and season == 2025:
            raise Upstream("play-by-play fetch failed: HTTP Error 404")
        return {"rows": 1, "inserted": 1, "changed": 0, "skipped": 0}

    monkeypatch.setattr(data_refresh, "ingest_season", fake_season)
    clock = {"now": 100.0}
    refresher = data_refresh.SignalsRefresher(pool, clock=lambda: clock["now"])
    assert refresher.due() is False and refresher.next_at == 100.0 + data_refresh.STARTUP_DELAY, "empty: soon"
    clock["now"] = refresher.next_at
    result = refresher.tick()
    assert calls == [("injuries", 2024), ("injuries", 2025), ("pbp", 2024), ("pbp", 2025)]
    assert list(result["errors"]) == ["pbp:2025"] and len(result["results"]) == 3
    assert refresher.next_at == clock["now"] + 24 * 3600
    assert data_refresh.SIGNALS_STATUS.snapshot()["last_result"] == result
    monkeypatch.setattr(data_refresh, "ingest_season", lambda *a: (_ for _ in ()).throw(Upstream("down")))
    clock["now"] = refresher.next_at
    assert len(refresher.tick()["errors"]) == 4 and refresher.next_at == clock["now"] + data_refresh.RETRY_SECONDS
    assert data_refresh.SIGNALS_STATUS.snapshot()["last_error"]
    data_refresh.SIGNALS_STATUS.reset()


def test_cli_ingest_commands(test_db_url, conn, monkeypatch, capsys):
    from host.cli import main

    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("FLEET_DEV", "1")
    ingest_fixture(conn)
    assert main(["ingest-injuries", "--file", str(INJURIES)]) == 0
    assert f"{INJURY_ROWS} rows, {INJURY_ROWS} inserted" in capsys.readouterr().out
    assert main(["ingest-pbp", "--file", str(PBP)]) == 0
    assert "10 rows, 10 inserted" in capsys.readouterr().out
    seen: list[int] = []
    monkeypatch.setattr(data_refresh, "ingest_season", lambda connect, kind, template, season: seen.append(season) or
                        {"rows": 0, "inserted": 0, "changed": 0, "skipped": 0})
    assert main(["ingest-pbp", "--season", "all"]) == 0
    assert seen == list(range(1999, 2026)), "the full backfill runs from the first season to the current one"
    assert main(["ingest-injuries", "--season", "2023"]) == 0 and seen[-1] == 2023
    assert main(["ingest-injuries"]) == 1 and "--season" in capsys.readouterr().err
