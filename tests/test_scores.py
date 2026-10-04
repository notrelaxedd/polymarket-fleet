"""ESPN scoreboard: fixture parse, mapping to game ids by teams and date, finals
written with raw.score_source espn, no change when not completed."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from host.exchange import scores
from tests.test_exchange import make_assignment, make_game, make_model

FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "espn_scoreboard.json").read_text()
KC_LV = datetime(2026, 10, 5, 20, 15, tzinfo=timezone.utc)
DAL_PHI = datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc)
GB_CHI = datetime(2026, 10, 4, 20, 25, tzinfo=timezone.utc)


def test_parse_scoreboard_fixture():
    entries = scores.parse_scoreboard(FIXTURE)
    assert [e["event_id"] for e in entries] == ["401800001", "401800002", "401800003"], "malformed events are dropped"
    kc = entries[0]
    assert (kc["home"], kc["away"], kc["home_score"], kc["away_score"], kc["completed"]) == ("LV", "KC", 17, 27, True)
    assert kc["date"] == KC_LV
    live = entries[1]
    assert (live["home"], live["away"], live["completed"], live["home_score"]) == ("DAL", "PHI", False, 14)
    tie = entries[2]
    assert (tie["home"], tie["away"], tie["home_score"], tie["away_score"], tie["completed"]) == ("CHI", "GB", 24, 24, True)
    assert scores.parse_scoreboard("not json") == [] and scores.parse_scoreboard("[]") == [] and scores.parse_scoreboard({"events": 1}) == []
    assert scores.parse_scoreboard(json.loads(FIXTURE)) == entries, "a decoded payload parses the same"
    assert scores.parse_event({"competitions": [{"competitors": [{"homeAway": "home", "team": {"abbreviation": "KC"}}]}]}) is None


def test_apply_writes_finals_by_teams_and_date_only_when_completed(conn):
    make_game(conn, "2026_05_KC_LV", "LV", "KC", KC_LV)
    make_game(conn, "2026_05_PHI_DAL", "DAL", "PHI", DAL_PHI)
    make_game(conn, "2026_05_GB_CHI", "CHI", "GB", GB_CHI + timedelta(hours=1), home_ml=None, away_ml=None)
    make_game(conn, "2026_12_KC_LV", "LV", "KC", KC_LV + timedelta(days=49))
    conn.execute("UPDATE games SET raw = %s WHERE game_id = '2026_05_KC_LV'", (Jsonb({"stadium": "Allegiant"}),))
    changed = scores.apply(conn, scores.parse_scoreboard(FIXTURE))
    assert sorted(changed) == ["2026_05_GB_CHI", "2026_05_KC_LV"]
    rows = {r["game_id"]: r for r in conn.execute("SELECT * FROM games").fetchall()}
    kc = rows["2026_05_KC_LV"]
    assert (kc["status"], kc["home_score"], kc["away_score"]) == ("final", 17, 27)
    assert kc["raw"] == {"stadium": "Allegiant", "score_source": "espn"}, "raw keeps its fields and gains the source"
    assert rows["2026_05_PHI_DAL"]["status"] == "scheduled" and rows["2026_05_PHI_DAL"]["home_score"] is None, "in progress: untouched"
    assert (rows["2026_05_GB_CHI"]["status"], rows["2026_05_GB_CHI"]["home_score"]) == ("final", 24), "kickoff an hour off still matches by date"
    assert rows["2026_12_KC_LV"]["status"] == "scheduled", "the rematch seven weeks later is another game"
    assert scores.apply(conn, scores.parse_scoreboard(FIXTURE)) == [], "already final with the same score: no change"
    assert scores.match_game(conn, {"home": "LV", "away": "KC", "date": None}) is None


def test_poll_only_fetches_while_a_game_with_assignments_awaits_a_final(conn):
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return FIXTURE

    now = KC_LV + timedelta(hours=3)
    make_game(conn, "2026_05_KC_LV", "LV", "KC", KC_LV)
    assert scores.poll(conn, now, fetch) == {"waiting": 0, "changed": []} and calls == [], "no assignment: nothing to fetch"
    make_assignment(conn, "2026_05_KC_LV", make_model(conn))
    assert scores.poll(conn, KC_LV - timedelta(hours=1), fetch)["waiting"] == 0 and calls == [], "not kicked off yet"
    result = scores.poll(conn, now, fetch)
    assert result == {"waiting": 1, "changed": ["2026_05_KC_LV"]}
    assert calls == ["https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"]
    assert scores.poll(conn, now, fetch)["waiting"] == 0 and len(calls) == 1, "final now: no more polling"
    conn.execute("UPDATE settings SET value = %s WHERE key = 'scores_url'", (Jsonb("https://scores.test/board"),))
    make_game(conn, "2026_05_PHI_DAL", "DAL", "PHI", DAL_PHI)
    make_assignment(conn, "2026_05_PHI_DAL", make_model(conn), status="halted")
    assert scores.poll(conn, now, fetch) == {"waiting": 1, "changed": []}, "in progress on the board: still waiting"
    assert calls[-1] == "https://scores.test/board"
