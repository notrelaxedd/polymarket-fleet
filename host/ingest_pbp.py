"""nflverse play-by-play: stream one season's CSV.gz, aggregate to team-game rows, upsert
into `team_game_stats` (docs/ROBUSTNESS.md B2).

The file (`play_by_play_{season}.csv.gz`, about 20 MB compressed and 400 columns) is
read as a stream (urllib or a local file, gzip, csv) and folded into running sums per
(game, team) as it goes, so a whole season of plays is never held in memory.

A play counts when it is a regular scrimmage play (play_type "pass" or "run") with a
finite epa. For each team-game: offence is the team as posteam, defence the team as
defteam. off_epa_per_play = mean epa on offence, def_epa_per_play = mean epa allowed on
defence, pass_rate = share of offensive plays with pass = 1 (play_type "pass" when the
column is absent), success_rate = share of offensive plays with success = 1 (epa > 0
when the column is absent), plays = offensive plays. kickoff_at comes from the games
table when it has the game, else gameday 13:00 US Eastern. Team codes go through the
games ingest's continuity aliases.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import math
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import IO, Any, Iterable

import psycopg

from host import nflverse
from host.errors import BadRequest, Upstream

DEFAULT_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
FIRST_SEASON = 1999
TIMEOUT_SECONDS = 120
MAX_BYTES = 200 * 1024 * 1024
UPSERT_LOCK = "nflverse:team_game_stats"
PLAY_TYPES = ("pass", "run")
REQUIRED = ("game_id", "posteam", "defteam", "play_type", "epa")
STAT_FIELDS = ("off_epa_per_play", "def_epa_per_play", "pass_rate", "plays", "success_rate")
FIELDS = ("game_id", "team", "season", "week", "kickoff_at") + STAT_FIELDS
TYPES = ("text", "text", "integer", "integer", "timestamptz", "double precision", "double precision",
         "double precision", "integer", "double precision")

csv.field_size_limit(16 * 1024 * 1024)


@dataclass
class _Side:
    """Running sums of one team in one game."""

    plays: int = 0
    epa: float = 0.0
    passes: int = 0
    successes: int = 0
    def_plays: int = 0
    def_epa: float = 0.0


def _float(value: str | None) -> float | None:
    try:
        number = float((value or "").strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _flag(value: str | None, fallback: bool) -> bool:
    number = _float(value)
    return fallback if number is None else number >= 0.5


class Aggregator:
    """Folds plays into per team-game sums; rows() gives the team_game_stats rows."""

    def __init__(self) -> None:
        self.sides: dict[tuple[str, str], _Side] = {}
        self.games: dict[str, dict[str, Any]] = {}
        self.plays = 0

    def _side(self, game_id: str, team: str) -> _Side:
        key = (game_id, team)
        if key not in self.sides:
            self.sides[key] = _Side()
        return self.sides[key]

    def add(self, raw: dict[str, str]) -> None:
        """One play record (any play; only regular plays with a finite epa count)."""
        if (raw.get("play_type") or "").strip() not in PLAY_TYPES:
            return
        epa = _float(raw.get("epa"))
        game_id = (raw.get("game_id") or "").strip()
        offence = nflverse.normalise_team(raw.get("posteam") or "")
        defence = nflverse.normalise_team(raw.get("defteam") or "")
        if epa is None or not game_id or not offence or not defence:
            return
        if game_id not in self.games:
            self.games[game_id] = {"season": raw.get("season"), "week": raw.get("week"),
                                   "game_date": raw.get("game_date")}
        off = self._side(game_id, offence)
        off.plays += 1
        off.epa += epa
        off.passes += _flag(raw.get("pass"), raw.get("play_type", "").strip() == "pass")
        off.successes += _flag(raw.get("success"), epa > 0)
        dfn = self._side(game_id, defence)
        dfn.def_plays += 1
        dfn.def_epa += epa
        self.plays += 1

    def rows(self) -> tuple[list[dict[str, Any]], int]:
        """(team_game_stats rows sorted by game and team, skipped games without a usable
        season, week or date)."""
        out: list[dict[str, Any]] = []
        skipped: set[str] = set()
        for (game_id, team), side in sorted(self.sides.items()):
            meta = self.games[game_id]
            season, week = nflverse._int(meta["season"]), nflverse._int(meta["week"])
            try:
                kickoff = nflverse.kickoff_at(meta["game_date"] or "", None).isoformat()
            except ValueError:
                kickoff = None
            if season is None or week is None:
                skipped.add(game_id)
                continue
            plays = side.plays
            out.append({
                "game_id": game_id, "team": team, "season": season, "week": week, "kickoff_at": kickoff,
                "off_epa_per_play": side.epa / plays if plays else None,
                "def_epa_per_play": side.def_epa / side.def_plays if side.def_plays else None,
                "pass_rate": side.passes / plays if plays else None,
                "plays": plays,
                "success_rate": side.successes / plays if plays else None,
            })
        return out, len(skipped)


def aggregate(lines: Iterable[str] | IO[str]) -> Aggregator:
    """Stream a play-by-play CSV (text lines) through an Aggregator; 400 when the
    columns are not those of nflverse play-by-play."""
    reader = csv.DictReader(lines)
    if not reader.fieldnames or not set(REQUIRED) <= set(reader.fieldnames):
        missing = sorted(set(REQUIRED) - set(reader.fieldnames or []))
        raise BadRequest("not an nflverse play-by-play file: missing columns " + ", ".join(missing))
    agg = Aggregator()
    for raw in reader:
        agg.add(raw)
    return agg


class _Capped(io.RawIOBase):
    """A readable byte stream that refuses to pass more than max_bytes."""

    def __init__(self, inner: Any, max_bytes: int) -> None:
        self.inner, self.max_bytes, self.seen = inner, max_bytes, 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self.inner.read(len(buffer))
        self.seen += len(data)
        if self.seen > self.max_bytes:
            raise Upstream(f"play-by-play fetch refused: body over {self.max_bytes} bytes")
        buffer[:len(data)] = data
        return len(data)


def _text_stream(binary: Any, gzipped: bool) -> IO[str]:
    raw = gzip.GzipFile(fileobj=binary) if gzipped else binary
    return io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")


def aggregate_file(path: str) -> Aggregator:
    """Aggregate a local play-by-play file (.csv.gz or .csv)."""
    try:
        with open(path, "rb") as fh:
            return aggregate(_text_stream(fh, path.endswith(".gz")))
    except (OSError, EOFError, UnicodeDecodeError, csv.Error) as exc:
        raise BadRequest(f"cannot read {path}: {exc}") from None


def aggregate_url(url: str, timeout: float = TIMEOUT_SECONDS, max_bytes: int = MAX_BYTES) -> Aggregator:
    """Download and aggregate a play-by-play CSV.gz as a stream; Upstream on failure."""
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-fleet host"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise Upstream(f"play-by-play fetch failed: HTTP {response.status}")
            capped = io.BufferedReader(_Capped(response, max_bytes), 1 << 20)
            return aggregate(_text_stream(capped, url.endswith(".gz")))
    except (Upstream, BadRequest):
        raise
    except Exception as exc:  # noqa: BLE001 - urllib, gzip and csv raise many types
        raise Upstream(f"play-by-play fetch failed: {exc}") from None


def upsert(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Insert or rewrite changed team-game rows (kickoff from games when known)."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (UPSERT_LOCK,))
    columns = ", ".join(FIELDS)
    typed = ", ".join(f"{name} {kind}" for name, kind in zip(FIELDS, TYPES))
    values = [name for name in FIELDS if name not in ("game_id", "team")]
    picked = ", ".join(
        "COALESCE((SELECT g.kickoff_at FROM games g WHERE g.game_id = t.game_id), t.kickoff_at)"
        if name == "kickoff_at" else f"t.{name}" for name in FIELDS
    )
    sql = (
        f"INSERT INTO team_game_stats ({columns}, updated_at) SELECT {picked}, clock_timestamp()"
        f" FROM jsonb_to_recordset(%s::jsonb) AS t({typed})"
        f" ON CONFLICT (game_id, team) DO UPDATE SET "
        + ", ".join(f"{name} = EXCLUDED.{name}" for name in values)
        + ", updated_at = clock_timestamp() WHERE "
        + " OR ".join(f"team_game_stats.{name} IS DISTINCT FROM EXCLUDED.{name}" for name in values)
        + " RETURNING (xmax = 0) AS inserted"
    )
    inserted = changed = 0
    for start in range(0, len(rows), 500):
        for row in conn.execute(sql, (json.dumps(rows[start:start + 500]),)).fetchall():
            inserted += 1 if row["inserted"] else 0
            changed += 0 if row["inserted"] else 1
    return {"rows": len(rows), "inserted": inserted, "changed": changed, "updated": inserted + changed}


def ingest_aggregate(conn: psycopg.Connection, agg: Aggregator, source: str) -> dict[str, Any]:
    """Upsert an Aggregator's rows; counts plus plays, skipped games and source."""
    rows, skipped = agg.rows()
    try:
        result = upsert(conn, rows)
    except psycopg.Error as exc:
        raise Upstream(f"play-by-play ingest failed: {str(exc).strip().splitlines()[0]}") from None
    result.update(plays=agg.plays, skipped=skipped, fetched_at=datetime.now(timezone.utc), source=source)
    return result


def ingest(conn: psycopg.Connection, season: int | None, source: str | None = None,
           template: str = DEFAULT_URL) -> dict[str, Any]:
    """Aggregate a local file (when `source` is given) or one season's download, then
    upsert. The download runs before any statement, so no transaction is held open."""
    if source:
        return ingest_aggregate(conn, aggregate_file(source), source)
    if season is None:
        raise BadRequest("a season is required to download play-by-play")
    url = template.replace("{season}", str(int(season)))
    return ingest_aggregate(conn, aggregate_url(url), url)


def stats_stamp(conn: psycopg.Connection) -> str:
    """`<count>-<max updated_at epoch>` of the team_game_stats table."""
    row = conn.execute("SELECT count(*) AS n, max(updated_at) AS t FROM team_game_stats").fetchone()
    return f"{row['n']}-{'0' if row['t'] is None else format(row['t'].timestamp(), '.6f')}"
