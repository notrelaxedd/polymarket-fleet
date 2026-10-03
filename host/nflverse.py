"""nflverse games.csv ingest: fetch (or read a file), normalise, upsert into `games`.

The CSV is CC BY 4.0 (attribution in the README and on the Models page). Rows keep
their full CSV record in `games.raw`; a re-ingest only touches rows whose raw record
changed, so `updated_at` (and the ETag of GET /api/v1/data/games) move only when the
data did. Writers queue on an advisory lock and stamp `updated_at` with
clock_timestamp(), so the ETag moves monotonically even when a long request
transaction commits after a later one. Team codes are normalised for continuity
(OAK -> LV, SD -> LAC, STL -> LA), kickoff_at is gameday + gametime in US Eastern as
UTC (13:00 local when the time is missing or not a time) and status is final once both
scores are present. A record that cannot be read is skipped and counted, an optional
field that is not a finite number in range becomes null, a duplicated game_id keeps
the last record.
"""
from __future__ import annotations

import csv
import io
import json
import urllib.request
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from host.errors import BadRequest, Upstream

DEFAULT_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
TIMEOUT_SECONDS = 60
MAX_BYTES = 50 * 1024 * 1024
CHUNK_ROWS = 500
TEAM_ALIASES = {"OAK": "LV", "SD": "LAC", "STL": "LA"}
EASTERN = ZoneInfo("America/New_York")
DEFAULT_GAMETIME = "13:00"
REQUIRED = ("game_id", "season", "week", "gameday", "home_team", "away_team")

COLUMNS = (
    "game_id", "season", "game_type", "week", "gameday", "gametime", "kickoff_at", "home_team",
    "away_team", "home_score", "away_score", "home_moneyline", "away_moneyline", "spread_line",
    "total_line", "home_rest", "away_rest", "div_game", "roof", "surface", "temp", "wind",
    "status", "raw",
)
COLUMN_TYPES = (
    "text", "integer", "text", "integer", "date", "text", "timestamptz", "text", "text",
    "integer", "integer", "integer", "integer", "real", "real", "integer", "integer",
    "boolean", "text", "text", "integer", "integer", "text", "jsonb",
)


INT_MIN, INT_MAX = -(2**31), 2**31 - 1
UPSERT_LOCK = "nflverse:games"


def _int(value: str | None) -> int | None:
    """An optional integer column: null when blank, not a number, not finite or out of
    the integer column's range."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    rounded = int(round(number))
    return rounded if INT_MIN <= rounded <= INT_MAX else None


def _required_int(value: str | None, name: str) -> int:
    """A required integer column (season, week); ValueError skips the record."""
    number = _int(value)
    if number is None:
        raise ValueError(f"{name} is not a number: {value!r}")
    return number


def _float(value: str | None) -> float | None:
    """An optional real column: null when blank, not a number or not finite."""
    try:
        number = float((value or "").strip() or "nan")
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _text(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def normalise_team(code: str) -> str:
    """OAK -> LV, SD -> LAC, STL -> LA; everything else upper-cased."""
    code = (code or "").strip().upper()
    return TEAM_ALIASES.get(code, code)


def normalise_game_type(value: str | None) -> str:
    """REG stays, every playoff round (WC, DIV, CON, SB) is POST."""
    return "REG" if (value or "REG").strip().upper() == "REG" else "POST"


def kickoff_at(gameday: str, gametime: str | None) -> datetime:
    """gameday + gametime in America/New_York as a UTC datetime (13:00 local when the
    time is missing or not HH:MM, such as TBD); ValueError on a bad date."""
    day = datetime.strptime(gameday.strip(), "%Y-%m-%d")
    time_part = (gametime or "").strip() or DEFAULT_GAMETIME
    try:
        clock = datetime.strptime(time_part, "%H:%M")
    except ValueError:
        clock = datetime.strptime(DEFAULT_GAMETIME, "%H:%M")
    local = day.replace(hour=clock.hour, minute=clock.minute, tzinfo=EASTERN)
    return local.astimezone(timezone.utc)


def game_row(raw: dict[str, str]) -> dict[str, Any]:
    """One CSV record to the `games` columns (raw kept whole for change detection)."""
    home_score, away_score = _int(raw.get("home_score")), _int(raw.get("away_score"))
    gametime = _text(raw.get("gametime"))
    return {
        "game_id": raw["game_id"].strip(),
        "season": _required_int(raw["season"], "season"),
        "game_type": normalise_game_type(raw.get("game_type")),
        "week": _required_int(raw["week"], "week"),
        "gameday": raw["gameday"].strip(),
        "gametime": gametime,
        "kickoff_at": kickoff_at(raw["gameday"], gametime).isoformat(),
        "home_team": normalise_team(raw["home_team"]),
        "away_team": normalise_team(raw["away_team"]),
        "home_score": home_score,
        "away_score": away_score,
        "home_moneyline": _int(raw.get("home_moneyline")),
        "away_moneyline": _int(raw.get("away_moneyline")),
        "spread_line": _float(raw.get("spread_line")),
        "total_line": _float(raw.get("total_line")),
        "home_rest": _int(raw.get("home_rest")),
        "away_rest": _int(raw.get("away_rest")),
        "div_game": bool(_int(raw.get("div_game")) or 0),
        "roof": _text(raw.get("roof")),
        "surface": _text(raw.get("surface")),
        "temp": _int(raw.get("temp")),
        "wind": _int(raw.get("wind")),
        "status": "final" if home_score is not None and away_score is not None else "scheduled",
        "raw": {k: v for k, v in raw.items() if k is not None},
    }


def parse_rows(text: str) -> tuple[list[dict[str, Any]], int]:
    """(rows, skipped): every readable record of a games.csv as `games` rows, the last
    record winning for a duplicated game_id; 400 when it is not that file."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not set(REQUIRED) <= set(reader.fieldnames):
        raise BadRequest("not a games.csv: missing columns " + ", ".join(sorted(set(REQUIRED) - set(reader.fieldnames or []))))
    rows: dict[str, dict[str, Any]] = {}
    skipped = 0
    for raw in reader:
        if not all((raw.get(key) or "").strip() for key in REQUIRED):
            skipped += 1
            continue
        try:
            row = game_row(raw)
        except (ValueError, TypeError):
            skipped += 1
            continue
        rows[row["game_id"]] = row
    return list(rows.values()), skipped


def parse(text: str) -> list[dict[str, Any]]:
    return parse_rows(text)[0]


def fetch(url: str, timeout: float = TIMEOUT_SECONDS, max_bytes: int = MAX_BYTES) -> str:
    """Download the CSV; Upstream (502) on any network failure or a body over max_bytes."""
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-fleet host"})
    chunks: list[bytes] = []
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise Upstream(f"nflverse fetch failed: HTTP {response.status}")
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise Upstream(f"nflverse fetch refused: body over {max_bytes} bytes")
                chunks.append(chunk)
    except Upstream:
        raise
    except Exception as exc:  # noqa: BLE001 - urllib raises many types; all mean "unavailable"
        raise Upstream(f"nflverse fetch failed: {exc}") from None
    return b"".join(chunks).decode("utf-8-sig")


def read_source(source: str | None, url: str) -> str:
    """The CSV text: a local file when `source` is given, else the download."""
    if source:
        try:
            with open(source, "r", encoding="utf-8-sig", newline="") as fh:
                return fh.read()
        except OSError as exc:
            raise BadRequest(f"cannot read {source}: {exc}") from None
    return fetch(url)


def upsert(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Insert new rows and update rows whose raw record changed; counts of each.

    Writers queue on an advisory transaction lock and stamp updated_at with
    clock_timestamp() (the wall clock, not the transaction start), so a later writer
    always carries a later stamp and the games ETag never repeats a value.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (UPSERT_LOCK,))
    columns = ", ".join(COLUMNS)
    typed = ", ".join(f"{name} {kind}" for name, kind in zip(COLUMNS, COLUMN_TYPES))
    updates = ", ".join(f"{name} = EXCLUDED.{name}" for name in COLUMNS if name != "game_id")
    sql = (
        f"INSERT INTO games ({columns}, updated_at) SELECT {columns}, clock_timestamp()"
        f" FROM jsonb_to_recordset(%s::jsonb) AS t({typed})"
        f" ON CONFLICT (game_id) DO UPDATE SET {updates}, updated_at = clock_timestamp()"
        " WHERE games.raw IS DISTINCT FROM EXCLUDED.raw RETURNING (xmax = 0) AS inserted"
    )
    inserted = changed = 0
    for start in range(0, len(rows), CHUNK_ROWS):
        chunk = rows[start:start + CHUNK_ROWS]
        for row in conn.execute(sql, (json.dumps(chunk),)).fetchall():
            if row["inserted"]:
                inserted += 1
            else:
                changed += 1
    return {"rows": len(rows), "inserted": inserted, "changed": changed, "updated": inserted + changed}


def ingest_text(conn: psycopg.Connection, text: str, source: str) -> dict[str, Any]:
    """Parse and upsert a games.csv body; the counts (plus skipped records), fetched_at
    and source. A database refusal (one the parser could not prevent) is reported as
    Upstream so the API and the dashboard show it instead of a bare 500."""
    fetched_at = datetime.now(timezone.utc)
    rows, skipped = parse_rows(text)
    try:
        result = upsert(conn, rows)
    except psycopg.Error as exc:
        raise Upstream(f"nflverse ingest failed: {str(exc).strip().splitlines()[0]}") from None
    result["skipped"] = skipped
    result["fetched_at"] = fetched_at
    result["source"] = source
    return result


def ingest(conn: psycopg.Connection, source: str | None = None, url: str = DEFAULT_URL) -> dict[str, Any]:
    """Read (file or download), parse and upsert; the counts plus fetched_at."""
    return ingest_text(conn, read_source(source, url), source or url)


def games_count(conn: psycopg.Connection) -> int:
    return int(conn.execute("SELECT count(*) AS n FROM games").fetchone()["n"])


def last_complete_season(conn: psycopg.Connection) -> int | None:
    """The newest season whose every game is final, or None when there is none."""
    row = conn.execute(
        "SELECT season FROM games GROUP BY season HAVING bool_and(status = 'final') ORDER BY season DESC LIMIT 1"
    ).fetchone()
    return None if row is None else int(row["season"])


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def worker_games(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every game in the MODELS.md field set, sorted by kickoff then id."""
    rows = conn.execute(
        """
        SELECT game_id, season, game_type, week, kickoff_at, home_team, away_team, home_score,
               away_score, home_moneyline, away_moneyline, spread_line, total_line, home_rest,
               away_rest, (div_game::int) AS div_game, roof, surface, temp, wind
          FROM games ORDER BY kickoff_at, game_id
        """
    ).fetchall()
    return [{k: _jsonable(v) for k, v in row.items()} for row in rows]


def games_etag(conn: psycopg.Connection) -> str:
    """`<count>-<max updated_at epoch>` (microsecond precision, so two changes within
    one second still differ): changes exactly when a row is added or changed, and only
    ever moves forward because upsert serialises writers and stamps the wall clock."""
    row = conn.execute("SELECT count(*) AS n, max(updated_at) AS t FROM games").fetchone()
    stamp = "0" if row["t"] is None else f"{row['t'].timestamp():.6f}"
    return f"{row['n']}-{stamp}"


def dumps_games(games: list[dict[str, Any]]) -> bytes:
    """The response body of GET /api/v1/data/games."""
    return json.dumps({"games": games, "count": len(games)}, separators=(",", ":")).encode("utf-8")
