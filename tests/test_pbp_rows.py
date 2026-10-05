"""In-game training rows: nflverse play-by-play ingest, the CLI and the worker feed.

The fixture tests/fixtures/pbp_rows_sample.csv.gz is a real slice of
https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_2023.csv.gz
(downloaded 2026-10-05): every record of 2023_01_DET_KC (DET won 21-20 at KC) and of
2023_01_BUF_NYJ (NYJ won 22-16 in overtime on a punt return), cut to the columns the
ingest reads plus score_differential and week, which the tests use as a cross-check.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
from pathlib import Path
from typing import Any

import pytest

from fleet.sim.odds import devig
from host import nflverse, pbp_rows
from host.api import data_pbp
from host.cli import main
from host.errors import BadRequest
from tests.conftest import FIXTURE_GAMES

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pbp_rows_sample.csv.gz"
FEED = "/api/v1/data/pbp"
DET_KC, BUF_NYJ = "2023_01_DET_KC", "2023_01_BUF_NYJ"


def _records() -> list[dict[str, str]]:
    with gzip.open(FIXTURE, "rt", newline="") as fh:
        return list(csv.DictReader(fh))


def _rows(pregame: dict[str, float | None] | None = None) -> list[dict[str, Any]]:
    with pbp_rows.open_source(str(FIXTURE)) as stream:
        return list(pbp_rows.map_records(csv.DictReader(stream), pregame or {}))


def _by_play(rows: list[dict[str, Any]], game_id: str, play_id: str) -> dict[str, Any]:
    return next(r for r in rows if r["game_id"] == game_id and r["play_id"] == play_id)


def _insert_det_kc(conn) -> float:
    """The DET at KC games.csv row (KC -198, DET +164); its devigged home probability."""
    rows, _ = nflverse.parse_rows(FIXTURE_GAMES.read_text(encoding="utf-8"))
    nflverse.upsert(conn, [r for r in rows if r["game_id"] == DET_KC])
    p = devig(-198, 164)
    assert p is not None
    return p


def test_fixture_is_small_and_whole_games() -> None:
    assert FIXTURE.stat().st_size < 300 * 1024
    records = _records()
    assert {r["game_id"] for r in records} == {DET_KC, BUF_NYJ}
    assert any(r["game_half"] == "Overtime" for r in records if r["game_id"] == BUF_NYJ)


def test_row_mapping_on_real_plays() -> None:
    rows = _rows({DET_KC: 0.6})
    records = _records()
    assert len(rows) < len(records), "game start and end-of-quarter markers are dropped"
    assert not any(r["play_id"] == "1" for r in rows)
    kickoff = _by_play(rows, DET_KC, "40")
    assert kickoff == {
        "game_id": DET_KC, "play_id": "40", "season": 2023, "home_win": 0.0, "score_diff": 0,
        "seconds_remaining": 3600, "half": 1, "down": None, "ydstogo": None, "yardline_100": 35,
        "posteam_is_home": False, "home_timeouts": 3, "away_timeouts": 3, "pregame_p_home": 0.6,
        "vegas_wp": pytest.approx(0.644948393106461),
    }
    first_down = _by_play(rows, DET_KC, "56")
    assert (first_down["down"], first_down["ydstogo"], first_down["yardline_100"]) == (1, 10, 75)
    assert _by_play(rows, DET_KC, "150")["posteam_is_home"] is True, "KC is home"
    # Home perspective, before the play: BUF trails 13-16 on its kickoff return after the NYJ field goal.
    assert _by_play(rows, BUF_NYJ, "3548")["score_diff"] == 3
    tying_fg = _by_play(rows, BUF_NYJ, "3824")
    assert tying_fg["score_diff"] == 3 and tying_fg["seconds_remaining"] == 6 and tying_fg["half"] == 2
    assert _by_play(rows, BUF_NYJ, "3844")["score_diff"] == 0, "the tying field goal counts from the next play"
    timeout = _by_play(rows, BUF_NYJ, "3696")
    assert timeout["posteam_is_home"] is None and timeout["yardline_100"] is None and timeout["down"] is None
    assert (timeout["home_timeouts"], timeout["away_timeouts"]) == (2, 1)
    # Overtime: half 3 on the overtime clock; the walk-off punt return starts tied.
    ot_kick = _by_play(rows, BUF_NYJ, "3902")
    assert (ot_kick["half"], ot_kick["seconds_remaining"], ot_kick["score_diff"]) == (3, 600, 0)
    punt = _by_play(rows, BUF_NYJ, "4010")
    assert (punt["half"], punt["seconds_remaining"], punt["score_diff"], punt["down"]) == (3, 561, 0, 4)
    assert {r["home_win"] for r in rows if r["game_id"] == BUF_NYJ} == {1.0}
    assert {r["home_win"] for r in rows if r["game_id"] == DET_KC} == {0.0}
    assert {r["pregame_p_home"] for r in rows if r["game_id"] == BUF_NYJ} == {None}


def test_every_row_agrees_with_nflverse_columns() -> None:
    raw = {(r["game_id"], r["play_id"]): r for r in _records()}
    rows = _rows()
    assert len({(r["game_id"], r["play_id"]) for r in rows}) == len(rows)
    for row in rows:
        rec = raw[(row["game_id"], row["play_id"])]
        posteam = rec["posteam"]
        if posteam and rec["score_differential"]:
            sign = 1 if posteam == rec["home_team"] else -1
            assert row["score_diff"] == sign * int(float(rec["score_differential"])), row
            assert row["posteam_is_home"] is (posteam == rec["home_team"])
            assert row["yardline_100"] == int(float(rec["yardline_100"]))
        assert row["vegas_wp"] == pytest.approx(float(rec["vegas_home_wp"]))
        assert row["home_timeouts"] == int(rec["home_timeouts_remaining"])
        assert row["away_timeouts"] == int(rec["away_timeouts_remaining"])
        if row["half"] == 1:
            assert 1800 <= row["seconds_remaining"] <= 3600
        elif row["half"] == 2:
            assert 0 <= row["seconds_remaining"] <= 1800
        else:
            assert row["half"] == 3 and rec["game_half"] == "Overtime" and 0 <= row["seconds_remaining"] <= 600
        assert row["down"] is not None or rec["play_type"] == "kickoff" or rec["timeout"] == "1"


def test_season_range_and_urls(conn) -> None:
    assert pbp_rows.season_range("2023") == (2023, 2023)
    assert pbp_rows.season_range(" 2012-2025 ") == (2012, 2025)
    for bad in ("2025-2012", "x", "2012-2013-2014", "-5", "1800", ""):
        with pytest.raises(BadRequest):
            pbp_rows.season_range(bad)
    assert pbp_rows.season_url(conn, 2021).endswith("/pbp/play_by_play_2021.csv.gz")
    conn.execute("""UPDATE settings SET value = '"https://mirror.example/pbp_{season}.csv.gz"' WHERE key = 'nflverse_pbp_url'""")
    assert pbp_rows.season_url(conn, 2019) == "https://mirror.example/pbp_2019.csv.gz"
    conn.execute("DELETE FROM settings WHERE key = 'nflverse_pbp_url'")
    assert pbp_rows.season_url(conn, 2019) == pbp_rows.DEFAULT_URL.replace("{season}", "2019")


def test_ingest_is_idempotent_and_rewrites_only_changed_rows(conn) -> None:
    p_home = _insert_det_kc(conn)
    n = len(_rows())
    first = pbp_rows.ingest_season(conn, 2023, str(FIXTURE))
    assert (first["rows"], first["inserted"], first["changed"], first["games"], first["with_pregame"]) == (n, n, 0, 2, 1)
    stored = conn.execute("SELECT * FROM pbp_rows WHERE game_id = %s AND play_id = '40'", (DET_KC,)).fetchone()
    assert stored["pregame_p_home"] == pytest.approx(p_home, abs=1e-6) and stored["seconds_remaining"] == 3600
    assert conn.execute("SELECT count(*) AS n FROM pbp_rows WHERE game_id = %s AND pregame_p_home IS NULL",
                        (BUF_NYJ,)).fetchone()["n"] > 0
    etag = data_pbp.feed_etag(conn, 2023, 2023)
    again = pbp_rows.ingest_season(conn, 2023, str(FIXTURE))
    assert (again["rows"], again["inserted"], again["changed"]) == (n, 0, 0)
    assert conn.execute("SELECT count(*) AS n FROM pbp_rows").fetchone()["n"] == n
    assert data_pbp.feed_etag(conn, 2023, 2023) == etag, "an idempotent re-ingest keeps the ETag"
    conn.execute("UPDATE pbp_rows SET vegas_wp = 0.01 WHERE game_id = %s AND play_id = '56'", (DET_KC,))
    assert data_pbp.feed_etag(conn, 2023, 2023) != etag, "a changed row moves the ETag"
    fixed = pbp_rows.ingest_season(conn, 2023, str(FIXTURE))
    assert (fixed["inserted"], fixed["changed"]) == (0, 1)
    assert data_pbp.feed_etag(conn, 2023, 2023) == etag


def test_ingest_streams_from_the_settings_url(conn, monkeypatch) -> None:
    opened: list[str] = []

    def fake_urlopen(request, timeout=None):
        opened.append(request.full_url)
        return io.BytesIO(FIXTURE.read_bytes())

    monkeypatch.setattr(pbp_rows.urllib.request, "urlopen", fake_urlopen)
    result = pbp_rows.ingest_season(conn, 2023)
    assert opened == [pbp_rows.DEFAULT_URL.replace("{season}", "2023")]
    assert result["inserted"] == len(_rows()) and result["source"] == opened[0]

    def failing_urlopen(request, timeout=None):
        raise OSError("HTTP Error 404: Not Found")

    monkeypatch.setattr(pbp_rows.urllib.request, "urlopen", failing_urlopen)
    with pytest.raises(pbp_rows.Upstream, match="404"):
        pbp_rows.ingest_season(conn, 2031)


def test_ingest_refuses_other_files(conn, tmp_path) -> None:
    other = tmp_path / "games.csv.gz"
    other.write_bytes(gzip.compress(b"game_id,season\n2023_01_DET_KC,2023\n"))
    with pytest.raises(BadRequest, match="not a play_by_play csv"):
        pbp_rows.ingest_season(conn, 2023, str(other))
    truncated = tmp_path / "cut.csv.gz"
    truncated.write_bytes(FIXTURE.read_bytes()[:4000])
    with pytest.raises(pbp_rows.Upstream):
        pbp_rows.ingest_season(conn, 2023, str(truncated))


def test_cli_ingest_pbp_rows(test_db_url, monkeypatch, capsys, conn) -> None:
    for key, value in {"DATABASE_URL": test_db_url, "FLEET_DEV": "1", "FLEET_PUBLIC_URL": "http://127.0.0.1:8080"}.items():
        monkeypatch.setenv(key, value)
    n = len(_rows())
    assert main(["ingest-pbp-rows", "--season", "2023", "--file", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"season 2023: {n} plays in 2 games (0 with a closing moneyline)") and f"{n} inserted, 0 changed" in out
    assert main(["ingest-pbp-rows", "--season", "2023", "--file", str(FIXTURE)]) == 0
    assert "0 inserted, 0 changed" in capsys.readouterr().out
    assert main(["ingest-pbp-rows", "--season", "2022-2023", "--file", str(FIXTURE)]) == 1
    assert "exactly one" in capsys.readouterr().err
    assert main(["ingest-pbp-rows", "--season", "20x3"]) == 1
    assert conn.execute("SELECT count(*) AS n FROM pbp_rows").fetchone()["n"] == n


def _lines(body: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in gzip.decompress(body).decode("utf-8").splitlines()]


def test_feed_auth_etag_and_body(client, conn, make_worker) -> None:
    _insert_det_kc(conn)
    pbp_rows.ingest_season(conn, 2023, str(FIXTURE))
    assert client.get(FEED + "?seasons=2023").status_code == 401
    assert client.get(FEED, headers={"Authorization": "Bearer nope"}).status_code == 401
    worker = make_worker()
    reply = client.get(FEED + "?seasons=2012-2025", headers=worker.headers)
    assert reply.status_code == 200
    assert reply.headers["content-type"] == "application/x-ndjson+gzip"
    assert "content-encoding" not in reply.headers
    etag = reply.headers["etag"]
    lines = _lines(reply.content)
    rows = conn.execute("SELECT * FROM pbp_rows").fetchall()
    assert len(lines) == len(rows) == len(_rows())
    assert list(lines[0]) == list(pbp_rows.COLUMNS)
    assert lines[0]["game_id"] == BUF_NYJ, "game then play order"
    kickoff = next(r for r in lines if r["game_id"] == DET_KC and r["play_id"] == "40")
    assert kickoff["vegas_wp"] == 0.644948 and kickoff["posteam_is_home"] is False and kickoff["home_win"] == 0.0
    assert kickoff["pregame_p_home"] == round(devig(-198, 164) or 0.0, 6)
    ot = [r for r in lines if r["half"] == 3]
    assert ot and all(r["game_id"] == BUF_NYJ and r["seconds_remaining"] <= 600 for r in ot)
    assert etag == f'"{data_pbp.feed_etag(conn, 2012, 2025)}"' and etag.startswith(f'"{len(rows)}-2023-')
    not_modified = client.get(FEED + "?seasons=2012-2025", headers={**worker.headers, "If-None-Match": etag})
    assert not_modified.status_code == 304 and not_modified.content == b"" and not_modified.headers["etag"] == etag
    again = client.get(FEED + "?seasons=2012-2025", headers=worker.headers)
    assert again.content == reply.content, "deterministic bytes (served from the cache)"
    every = client.get(FEED, headers=worker.headers)
    assert every.status_code == 200 and _lines(every.content) == lines
    empty = client.get(FEED + "?seasons=2012-2022", headers=worker.headers)
    assert empty.status_code == 200 and _lines(empty.content) == [] and empty.headers["etag"].startswith('"0-0-')
    assert client.get(FEED + "?seasons=2025-2012", headers=worker.headers).status_code == 400
    conn.execute("UPDATE pbp_rows SET down = 2 WHERE game_id = %s AND play_id = '56'", (DET_KC,))
    changed = client.get(FEED + "?seasons=2012-2025", headers={**worker.headers, "If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag
    assert next(r for r in _lines(changed.content) if r["play_id"] == "56" and r["game_id"] == DET_KC)["down"] == 2
