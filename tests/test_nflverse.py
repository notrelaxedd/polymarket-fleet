"""nflverse ingest: parsing the fixture, normalisation, time zones, status, idempotency."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from host import nflverse
from host.errors import BadRequest
from tests.conftest import FIXTURE_GAMES, flash_cookie, ingest_fixture

ROWS = 2761


def test_parse_counts_and_normalisation():
    rows = nflverse.parse(FIXTURE_GAMES.read_text(encoding="utf-8"))
    assert len(rows) == ROWS
    by_id = {r["game_id"]: r for r in rows}
    assert {r["season"] for r in rows} == set(range(2016, 2026))
    assert {r["game_type"] for r in rows} == {"REG", "POST"}
    assert by_id["2016_18_OAK_HOU"]["game_type"] == "POST", "WC/DIV/CON/SB all become POST"
    teams = {r["home_team"] for r in rows} | {r["away_team"] for r in rows}
    assert not teams & {"OAK", "SD", "STL"}, "old codes are normalised"
    assert {"LV", "LAC", "LA"} <= teams
    assert by_id["2016_01_SD_KC"]["away_team"] == "LAC" and by_id["2016_01_OAK_NO"]["away_team"] == "LV"
    assert by_id["2016_01_SD_KC"]["raw"]["away_team"] == "SD", "raw keeps the file's own code"
    first = by_id["2016_01_CAR_DEN"]
    assert first["home_score"] == 21 and first["away_score"] == 20 and first["status"] == "final"
    assert first["home_moneyline"] == 136 and first["away_moneyline"] == -150
    assert first["spread_line"] == -3.0 and first["total_line"] == 40.5 and first["div_game"] is False
    assert first["home_rest"] == 7 and first["temp"] == 85 and first["wind"] == 10 and first["roof"] == "outdoors"
    dome = by_id["2016_01_TB_ATL"]
    assert dome["temp"] is None and dome["wind"] is None and dome["div_game"] is True
    assert sum(1 for r in rows if r["home_moneyline"] is None or r["away_moneyline"] is None) == 1


def test_kickoff_uses_eastern_time_with_and_without_dst():
    # September (EDT, UTC-4): 20:30 local is 00:30 UTC the next day.
    assert nflverse.kickoff_at("2016-09-08", "20:30") == datetime(2016, 9, 9, 0, 30, tzinfo=timezone.utc)
    # January (EST, UTC-5): 16:35 local is 21:35 UTC.
    assert nflverse.kickoff_at("2017-01-07", "16:35") == datetime(2017, 1, 7, 21, 35, tzinfo=timezone.utc)
    # A missing time means the 13:00 local window.
    assert nflverse.kickoff_at("2025-12-07", "") == datetime(2025, 12, 7, 18, 0, tzinfo=timezone.utc)
    assert nflverse.kickoff_at("2025-10-05", None) == datetime(2025, 10, 5, 17, 0, tzinfo=timezone.utc)


def test_status_scheduled_without_both_scores():
    base = {"game_id": "2026_01_A_B", "season": "2026", "week": "1", "gameday": "2026-09-10", "home_team": "A", "away_team": "B"}
    assert nflverse.game_row({**base, "home_score": "", "away_score": ""})["status"] == "scheduled"
    assert nflverse.game_row({**base, "home_score": "7", "away_score": ""})["status"] == "scheduled"
    assert nflverse.game_row({**base, "home_score": "7", "away_score": "3"})["status"] == "final"
    assert nflverse.game_row({**base, "game_type": "SB"})["game_type"] == "POST"


def test_parse_rejects_other_files():
    with pytest.raises(BadRequest):
        nflverse.parse("a,b\n1,2\n")


def test_ingest_is_idempotent_and_updates_only_changed_rows(conn):
    first = ingest_fixture(conn)
    assert first["rows"] == ROWS and first["inserted"] == ROWS and first["changed"] == 0 and first["updated"] == ROWS
    assert str(first["source"]).endswith("games_sample.csv") and first["fetched_at"].tzinfo is not None
    assert nflverse.games_count(conn) == ROWS
    etag = nflverse.games_etag(conn)
    assert etag.startswith(f"{ROWS}-")
    before = conn.execute("SELECT game_id, updated_at FROM games ORDER BY game_id").fetchall()
    again = ingest_fixture(conn)
    assert again["inserted"] == 0 and again["changed"] == 0 and again["updated"] == 0
    assert conn.execute("SELECT game_id, updated_at FROM games ORDER BY game_id").fetchall() == before
    assert nflverse.games_etag(conn) == etag, "nothing changed, the ETag holds"
    # One row changes (a score correction): only that row's updated_at moves.
    conn.execute("UPDATE games SET updated_at = now() - interval '1 day'")
    row = conn.execute("SELECT * FROM games WHERE game_id = '2016_01_CAR_DEN'").fetchone()
    assert row["kickoff_at"] == datetime(2016, 9, 9, 0, 30, tzinfo=timezone.utc) and row["status"] == "final"
    assert row["home_team"] == "DEN" and row["gametime"] == "20:30"
    changed = {**row["raw"], "home_score": "28"}
    result = nflverse.upsert(conn, [nflverse.game_row(changed)])
    assert result == {"rows": 1, "inserted": 0, "changed": 1, "updated": 1}
    after = conn.execute("SELECT home_score, updated_at FROM games WHERE game_id = '2016_01_CAR_DEN'").fetchone()
    assert after["home_score"] == 28 and after["updated_at"] > row["updated_at"]
    untouched = conn.execute("SELECT count(*) AS n FROM games WHERE updated_at < now() - interval '1 hour'").fetchone()
    assert untouched["n"] == ROWS - 1
    assert nflverse.games_etag(conn) != etag
    assert nflverse.last_complete_season(conn) == 2025
    conn.execute("UPDATE games SET status = 'scheduled', home_score = NULL, away_score = NULL WHERE season = 2025 AND week = 5")
    assert nflverse.last_complete_season(conn) == 2024


def test_worker_games_shape(conn):
    ingest_fixture(conn)
    games = nflverse.worker_games(conn)
    assert len(games) == ROWS
    assert [g["kickoff_at"] for g in games] == sorted(g["kickoff_at"] for g in games), "kickoff order"
    first = games[0]
    assert set(first) == {
        "game_id", "season", "game_type", "week", "kickoff_at", "home_team", "away_team", "home_score",
        "away_score", "home_moneyline", "away_moneyline", "spread_line", "total_line", "home_rest", "away_rest",
        "div_game", "roof", "surface", "temp", "wind",
    }
    assert first["game_id"] == "2016_01_CAR_DEN" and first["kickoff_at"] == "2016-09-09T00:30:00Z"
    assert first["div_game"] == 0 and isinstance(first["season"], int)
    # The sim's own loader accepts the body the host serves.
    import json

    from fleet.sim.data import load_games

    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write(nflverse.dumps_games(games).decode("utf-8"))
    loaded = load_games(fh.name)
    assert len(loaded) == ROWS and loaded[0]["game_id"] == "2016_01_CAR_DEN"
    assert json.loads(nflverse.dumps_games(games))["count"] == ROWS


def test_fetch_refuses_oversized_bodies(monkeypatch):
    import io

    class Fake(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(nflverse.urllib.request, "urlopen", lambda *a, **k: Fake(b"x" * 2000))
    with pytest.raises(nflverse.Upstream):
        nflverse.fetch("http://example.invalid/games.csv", max_bytes=1000)
    assert nflverse.fetch("http://example.invalid/games.csv", max_bytes=5000) == "x" * 2000

    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(nflverse.urllib.request, "urlopen", boom)
    with pytest.raises(nflverse.Upstream):
        nflverse.fetch("http://example.invalid/games.csv")


def test_refresher_runs_at_startup_when_empty_then_every_interval(pool, conn, monkeypatch):
    """The host's data thread: first pass fetches only when games is empty, later passes
    every nflverse_refresh_hours; a failed fetch is logged and retried, never raised."""
    from host import data_refresh

    clock = {"now": 1000.0}
    calls: list[str] = []

    def fake_fetch(url, timeout=60, max_bytes=0):
        calls.append(url)
        if url.endswith("boom"):
            raise nflverse.Upstream("nflverse fetch failed: boom")
        return FIXTURE_GAMES.read_text(encoding="utf-8")

    monkeypatch.setattr(nflverse, "fetch", fake_fetch)
    conn.execute("""UPDATE settings SET value = '"https://example.invalid/games.csv"' WHERE key = 'nflverse_url'""")
    conn.execute("UPDATE settings SET value = '2' WHERE key = 'nflverse_refresh_hours'")
    refresher = data_refresh.DataRefresher(pool, clock=lambda: clock["now"])
    assert refresher.due() is True, "games is empty: fetch at startup"
    result = refresher.tick()
    assert result["rows"] == 2761 and calls == ["https://example.invalid/games.csv"]
    assert nflverse.games_count(conn) == 2761 and refresher.next_at == 1000.0 + 2 * 3600
    assert refresher.tick() is None and len(calls) == 1, "not due yet"
    clock["now"] += 2 * 3600 - 1
    assert refresher.tick() is None
    clock["now"] += 1
    assert refresher.tick()["updated"] == 0 and len(calls) == 2
    # A failure is swallowed, remembered and retried after RETRY_SECONDS.
    conn.execute("""UPDATE settings SET value = '"https://example.invalid/boom"' WHERE key = 'nflverse_url'""")
    clock["now"] += 2 * 3600
    assert refresher.tick() == {"error": "nflverse fetch failed: boom"}
    assert refresher.last_error and refresher.next_at == clock["now"] + data_refresh.RETRY_SECONDS
    clock["now"] += data_refresh.RETRY_SECONDS
    assert "error" in refresher.tick() and len(calls) == 4
    # A host that starts with games already loaded waits a full interval first.
    fresh = data_refresh.DataRefresher(pool, clock=lambda: clock["now"])
    assert fresh.due() is False and fresh.next_at == clock["now"] + 2 * 3600
    thread = data_refresh.DataRefreshThread(pool, interval=0.01)
    thread.refresher = fresh
    thread.start()
    thread.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_malformed_and_duplicate_rows_are_skipped_not_fatal(conn):
    """MEDIUM: one bad or duplicated record must not abort the whole ingest."""
    text = FIXTURE_GAMES.read_text(encoding="utf-8")
    header, body = text.split("\n", 1)
    cols = header.split(",")
    first = body.split("\n", 1)[0].split(",")
    assert len(first) == len(cols)

    def row(**changes):
        values = dict(zip(cols, first))
        values.update(changes)
        return ",".join(values[c] for c in cols)

    mutated = "\n".join([
        header,
        row(game_id="dup_1", home_score="7"), row(game_id="dup_1", home_score="9"),  # duplicate: last wins
        row(game_id="na_season", season="NA"),                                       # unreadable: skipped
        row(game_id="bad_day", gameday="2016-13-40"),                                # unreadable: skipped
        row(game_id="tbd", gametime="TBD", home_moneyline="NA", away_score="nan", temp="1e12", wind="inf"),
        row(game_id="late", gametime="8:20 PM"),
    ])
    rows, skipped = nflverse.parse_rows(mutated)
    assert skipped == 2 and [r["game_id"] for r in rows] == ["dup_1", "tbd", "late"]
    assert rows[0]["home_score"] == 9
    tbd = rows[1]
    assert tbd["home_moneyline"] is None and tbd["away_score"] is None and tbd["status"] == "scheduled"
    assert tbd["temp"] is None and tbd["wind"] is None
    assert tbd["kickoff_at"] == nflverse.kickoff_at(tbd["gameday"], None).isoformat(), "TBD falls back to 13:00 local"
    assert rows[2]["kickoff_at"] == tbd["kickoff_at"]
    result = nflverse.upsert(conn, rows)
    assert result == {"rows": 3, "inserted": 3, "changed": 0, "updated": 3}
    assert conn.execute("SELECT home_score FROM games WHERE game_id = 'dup_1'").fetchone()["home_score"] == 9
    assert nflverse.ingest_text(conn, mutated, "test")["skipped"] == 2


def test_games_etag_moves_when_an_earlier_transaction_commits_last(pool, conn):
    """MEDIUM: updated_at is stamped with the wall clock and writers are serialised, so
    a transaction that opened before another's commit still produces a newer ETag."""
    ingest_fixture(conn)
    before = nflverse.games_etag(conn)
    raw = conn.execute("SELECT raw FROM games WHERE game_id = '2016_01_CAR_DEN'").fetchone()["raw"]
    other = conn.execute("SELECT raw FROM games WHERE game_id = '2016_01_TB_ATL'").fetchone()["raw"]
    with pool.connection() as early, pool.connection() as late:
        early.execute("SELECT now()")  # opens the long transaction: now() is pinned here
        late_result = nflverse.upsert(late, [nflverse.game_row({**other, "home_score": "55"})])
        late.commit()
        middle = nflverse.games_etag(conn)
        assert late_result["changed"] == 1 and middle != before
        early_result = nflverse.upsert(early, [nflverse.game_row({**raw, "home_score": "99"})])
        early.commit()
    assert early_result["changed"] == 1
    assert conn.execute("SELECT home_score FROM games WHERE game_id = '2016_01_CAR_DEN'").fetchone()["home_score"] == 99
    after = nflverse.games_etag(conn)
    assert after != middle and after != before, "a worker holding the middle tag must not get 304"
    assert float(after.split("-")[1]) > float(middle.split("-")[1]), "the tag only moves forward"


def _data_card(client):  # noqa: ANN001, ANN202 - a TestClient
    """The nflverse games group of the Settings page."""
    from tests.pagecheck import page

    return page(client.get("/settings").text).card("nflverse")


def test_refresh_failures_are_flashed_and_shown_in_settings(client, conn, monkeypatch):
    """MEDIUM: a broken upstream file is a flash and a Settings note, never a 500."""
    from host import data_refresh

    data_refresh.STATUS.reset()
    assert "No refresh since the host started." in _data_card(client).text
    monkeypatch.setattr(nflverse, "fetch", lambda url, timeout=60, max_bytes=0: "a,b\n1,2\n")
    r = client.post("/data/refresh", follow_redirects=False)
    assert r.status_code == 303 and "refresh failed: not a games.csv" in flash_cookie(r)
    assert client.post("/api/data/refresh").status_code == 400
    card = _data_card(client)
    assert "Last refresh failed" in card.text and "not a games.csv" in card.text
    assert conn.execute("SELECT count(*) AS n FROM games").fetchone()["n"] == 0
    # A file with a few bad records still loads, and the note reports the skipped count.
    text = FIXTURE_GAMES.read_text(encoding="utf-8")
    broken = text + text.split("\n")[1].replace("2016_01_CAR_DEN", "x_bad").replace("2016,", "NA,", 1) + "\n"
    monkeypatch.setattr(nflverse, "fetch", lambda url, timeout=60, max_bytes=0: broken)
    r = client.post("/data/refresh", follow_redirects=False)
    assert flash_cookie(r) == "games refreshed: 2761 rows, 2761 updated, 1 skipped"
    card = _data_card(client)
    assert "Last refresh failed" not in card.text and "2761 rows, 2761 inserted, 0 changed, 1 skipped." in card.text
    body = client.post("/api/data/refresh").json()
    assert body["skipped"] == 1 and body["updated"] == 0 and body["rows"] == 2761
    data_refresh.STATUS.reset()
