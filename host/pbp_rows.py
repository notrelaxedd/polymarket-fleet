"""nflverse play-by-play ingest: one `pbp_rows` row per play for the in-game model.

play_by_play_{season}.csv.gz (CC BY 4.0, about 50 000 plays and 20 MB per season) is
streamed through gzip and csv one record at a time, so a season is never held in
memory, and upserted in batches. The rows are the training data of the `ingame_wp`
family (docs/INGAME.md, contract section 3):

- score_diff is home minus away before the play. nflverse's total_home_score and
  total_away_score are the score after the play, so the score before a play is the
  running total after the previous record of the same game (in file order).
- seconds_remaining is game_seconds_remaining in regulation (3600 at kickoff, 0 at the
  end of the fourth quarter); overtime plays carry the overtime clock
  (quarter_seconds_remaining) with half 3.
- yardline_100 is nflverse's own: the distance to the opponent's end zone for the team
  in possession; posteam_is_home says which team that is (null without a possession).
- vegas_wp is nflverse's vegas_home_wp, home_win comes from the final score (1, 0.5 for
  a tie, 0; null while a game has no final score) and pregame_p_home is the game's
  closing moneyline from `games`, devigged as fleet.sim.odds does (null when missing).

A record is kept when it has a clock and either a down or is a kickoff or a timeout;
the game-start, end-of-quarter and end-of-game markers are dropped. A re-ingest only
rewrites rows whose values changed, and writers queue on an advisory lock.
"""
from __future__ import annotations

import csv
import gzip
import http.client
import io
import json
import urllib.request
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, TextIO

import psycopg

from fleet.sim.odds import devig
from host.errors import BadRequest, Upstream
from host.settings import get_setting

DEFAULT_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
TIMEOUT_SECONDS = 120
BATCH_ROWS = 1000
UPSERT_LOCK = "nflverse:pbp_rows"
FIRST_SEASON, LAST_SEASON = 1999, 2100
MISSING = ("", "NA", "NaN", "nan")
HALVES = {"Half1": 1, "Half2": 2, "Overtime": 3}
REQUIRED = ("play_id", "game_id", "season", "home_team", "game_seconds_remaining", "game_half",
            "total_home_score", "total_away_score")

COLUMNS = (
    "game_id", "play_id", "season", "home_win", "score_diff", "seconds_remaining", "half", "down",
    "ydstogo", "yardline_100", "posteam_is_home", "home_timeouts", "away_timeouts", "pregame_p_home",
    "vegas_wp",
)
COLUMN_TYPES = (
    "text", "text", "integer", "real", "integer", "integer", "integer", "integer", "integer",
    "integer", "boolean", "integer", "integer", "real", "real",
)


def _text(value: str | None) -> str | None:
    text = (value or "").strip()
    return None if text in MISSING else text


def _float(value: str | None) -> float | None:
    """A finite number or None (blank, NA, not a number, infinite)."""
    text = _text(value)
    if text is None:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _int(value: str | None) -> int | None:
    number = _float(value)
    return None if number is None else int(round(number))


def _play_id(value: str | None) -> str | None:
    """nflverse play ids are integers; "40.0" and "40" are the same play."""
    number = _float(value)
    if number is not None and number == int(number):
        return str(int(number))
    return _text(value)


def season_range(text: str) -> tuple[int, int]:
    """A season ("2023") or a range ("2012-2025") as (first, last); BadRequest otherwise."""
    parts = text.strip().split("-")
    try:
        bounds = [int(part) for part in parts]
    except ValueError:
        raise BadRequest(f"not a season or a season range: {text!r}") from None
    lo, hi = (bounds[0], bounds[-1]) if len(bounds) in (1, 2) else (0, -1)
    if not FIRST_SEASON <= lo <= hi <= LAST_SEASON:
        raise BadRequest(f"not a season or a season range: {text!r}")
    return lo, hi


def home_win(home_score: int | None, away_score: int | None) -> float | None:
    """1, 0.5 for a tie, 0 from the final score; None without one."""
    if home_score is None or away_score is None:
        return None
    if home_score == away_score:
        return 0.5
    return 1.0 if home_score > away_score else 0.0


def keep(record: dict[str, str]) -> bool:
    """A play the model can learn from: it has a clock, and a down, or is a kickoff or a timeout."""
    if _float(record.get("game_seconds_remaining")) is None or record.get("game_half") not in HALVES:
        return False
    if _int(record.get("down")) is not None:
        return True
    return _text(record.get("play_type")) == "kickoff" or _int(record.get("timeout")) == 1


def map_record(record: dict[str, str], score_before: tuple[int, int], pregame: float | None) -> dict[str, Any]:
    """One kept nflverse record to a `pbp_rows` row; score_before is (home, away) before the play."""
    half = HALVES[record["game_half"]]
    clock_key = "quarter_seconds_remaining" if half == 3 else "game_seconds_remaining"
    seconds = _int(record.get(clock_key))
    if seconds is None:
        seconds = _int(record.get("game_seconds_remaining"))
    down = _int(record.get("down"))
    posteam = _text(record.get("posteam"))
    home = (record.get("home_team") or "").strip()
    return {
        "game_id": record["game_id"].strip(),
        "play_id": _play_id(record.get("play_id")),
        "season": _int(record.get("season")),
        "home_win": home_win(_int(record.get("home_score")), _int(record.get("away_score"))),
        "score_diff": score_before[0] - score_before[1],
        "seconds_remaining": max(0, seconds or 0),
        "half": half,
        "down": down,
        "ydstogo": _int(record.get("ydstogo")) if down is not None else None,
        "yardline_100": _int(record.get("yardline_100")) if posteam else None,
        "posteam_is_home": None if posteam is None else posteam == home,
        "home_timeouts": _int(record.get("home_timeouts_remaining")),
        "away_timeouts": _int(record.get("away_timeouts_remaining")),
        "pregame_p_home": pregame,
        "vegas_wp": _float(record.get("vegas_home_wp")),
    }


def map_records(records: Iterable[dict[str, str]], pregame: dict[str, float | None]) -> Iterator[dict[str, Any]]:
    """Lazily map a season's records (in file order) to rows, tracking each game's score.

    The score before a play is the total after the previous record of that game (any
    record, kept or not); a record without a game id, play id or season is skipped.
    """
    game_id: str | None = None
    score = (0, 0)
    for record in records:
        gid = (record.get("game_id") or "").strip()
        if gid != game_id:
            game_id, score = gid, (0, 0)
        before = score
        home_total, away_total = _int(record.get("total_home_score")), _int(record.get("total_away_score"))
        if home_total is not None and away_total is not None:
            score = (home_total, away_total)
        if not gid or not keep(record):
            continue
        row = map_record(record, before, pregame.get(gid))
        if row["play_id"] is None or row["season"] is None:
            continue
        yield row


def pregame_probs(conn: psycopg.Connection, season: int) -> dict[str, float | None]:
    """game_id -> devigged closing-moneyline home probability for one season's games."""
    rows = conn.execute(
        "SELECT game_id, home_moneyline, away_moneyline FROM games WHERE season = %s", (season,)
    ).fetchall()
    return {r["game_id"]: devig(r["home_moneyline"], r["away_moneyline"]) for r in rows}


def season_url(conn: psycopg.Connection, season: int) -> str:
    """The settings nflverse_pbp_url (a template with {season}) or the default."""
    template = str(get_setting(conn, "nflverse_pbp_url", "") or DEFAULT_URL)
    return template.replace("{season}", str(season))


@contextmanager
def open_source(source: str) -> Iterator[TextIO]:
    """A text stream over a play_by_play csv.gz: a local path or an http(s) URL.

    Bytes are decompressed as they are read; Upstream (502) on a network failure,
    BadRequest on a local file that cannot be read.
    """
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "polymarket-fleet host"})
        try:
            response = urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - urllib raises many types; all mean "unavailable"
            raise Upstream(f"nflverse pbp fetch failed: {exc}") from None
        raw: Any = response
    else:
        try:
            raw = open(source, "rb")
        except OSError as exc:
            raise BadRequest(f"cannot read {source}: {exc}") from None
    try:
        with gzip.GzipFile(fileobj=raw) as unzipped:
            yield io.TextIOWrapper(unzipped, encoding="utf-8-sig", newline="")
    finally:
        raw.close()


def upsert(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Insert new rows and rewrite rows whose values changed; counts of each."""
    columns = ", ".join(COLUMNS)
    typed = ", ".join(f"{name} {kind}" for name, kind in zip(COLUMNS, COLUMN_TYPES))
    rest = [name for name in COLUMNS if name not in ("game_id", "play_id")]
    updates = ", ".join(f"{name} = EXCLUDED.{name}" for name in rest)
    old = ", ".join(f"pbp_rows.{name}" for name in rest)
    new = ", ".join(f"EXCLUDED.{name}" for name in rest)
    sql = (
        f"INSERT INTO pbp_rows ({columns}) SELECT {columns} FROM jsonb_to_recordset(%s::jsonb) AS t({typed})"
        f" ON CONFLICT (game_id, play_id) DO UPDATE SET {updates}"
        f" WHERE ({old}) IS DISTINCT FROM ({new}) RETURNING (xmax = 0) AS inserted"
    )
    unique = {(row["game_id"], row["play_id"]): row for row in rows}
    inserted = changed = 0
    for row in conn.execute(sql, (json.dumps(list(unique.values())),)).fetchall():
        if row["inserted"]:
            inserted += 1
        else:
            changed += 1
    return {"inserted": inserted, "changed": changed}


def ingest_stream(conn: psycopg.Connection, stream: TextIO, season: int) -> dict[str, Any]:
    """Upsert every kept play of one season's csv text stream, BATCH_ROWS at a time."""
    reader = csv.DictReader(stream)
    if not reader.fieldnames or not set(REQUIRED) <= set(reader.fieldnames):
        missing = sorted(set(REQUIRED) - set(reader.fieldnames or []))
        raise BadRequest("not a play_by_play csv: missing columns " + ", ".join(missing))
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (UPSERT_LOCK,))
    pregame = pregame_probs(conn, season)
    counts = {"rows": 0, "inserted": 0, "changed": 0, "games": 0}
    games: set[str] = set()
    batch: list[dict[str, Any]] = []
    for row in map_records(reader, pregame):
        batch.append(row)
        games.add(row["game_id"])
        if len(batch) >= BATCH_ROWS:
            _flush(conn, batch, counts)
    _flush(conn, batch, counts)
    counts["games"] = len(games)
    counts["with_pregame"] = sum(1 for gid in games if pregame.get(gid) is not None)
    return counts


def _flush(conn: psycopg.Connection, batch: list[dict[str, Any]], counts: dict[str, int]) -> None:
    if not batch:
        return
    try:
        result = upsert(conn, batch)
    except psycopg.Error as exc:
        raise Upstream(f"pbp ingest failed: {str(exc).strip().splitlines()[0]}") from None
    counts["rows"] += len(batch)
    counts["inserted"] += result["inserted"]
    counts["changed"] += result["changed"]
    batch.clear()


def ingest_season(conn: psycopg.Connection, season: int, source: str | None = None) -> dict[str, Any]:
    """Stream one season (a local csv.gz or the settings URL) into pbp_rows; the counts and source."""
    where = source or season_url(conn, season)
    with open_source(where) as stream:
        try:
            result = ingest_stream(conn, stream, season)
        except (OSError, EOFError, UnicodeDecodeError, csv.Error, http.client.HTTPException) as exc:
            raise Upstream(f"nflverse pbp read failed: {exc}") from None
    result["season"] = season
    result["source"] = where
    return result
