"""nflverse injury reports: fetch one season (or read a file), normalise, upsert into
`injuries` (docs/ROBUSTNESS.md B2).

The source is the free weekly injury report file of nflverse-data
(`injuries_{season}.csv`, CC BY 4.0 like the schedules). One row per (season,
game_type, week, team, player). game_type folds every playoff round into POST the way
the games ingest does, team codes go through the same continuity aliases (OAK -> LV,
SD -> LAC, STL -> LA), a player without a gsis_id is keyed by "name:<full name>" and a
record that cannot be read is skipped and counted. A duplicated key keeps the record
with the latest date_modified (the last one on a tie).

Upserts are idempotent: a row is rewritten (and its updated_at, which the games feed
ETag reads, moved) only when one of its fields changed. Writers queue on an advisory
lock and stamp updated_at with clock_timestamp(), like host/nflverse.py.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any

import psycopg

from host import nflverse
from host.errors import BadRequest, Upstream

DEFAULT_URL = "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.csv"
FIRST_SEASON = 2009
MAX_BYTES = 30 * 1024 * 1024
CHUNK_ROWS = 1000
UPSERT_LOCK = "nflverse:injuries"
REQUIRED = ("season", "team", "week")
FIELDS = ("season", "game_type", "week", "team", "gsis_id", "full_name", "position", "report_status", "date_modified")
TYPES = ("integer", "text", "integer", "text", "text", "text", "text", "text", "timestamptz")


def season_url(template: str, season: int) -> str:
    """The download URL of one season from a template with {season}."""
    return template.replace("{season}", str(int(season)))


def _modified(value: str | None) -> str | None:
    """An ISO timestamp (Z or offset, naive is UTC) as a UTC ISO string, or None."""
    text = (value or "").strip()
    if not text or text.upper() == "NA":
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _text(value: str | None) -> str | None:
    text = (value or "").strip()
    return None if not text or text.upper() == "NA" else text


def injury_row(raw: dict[str, str]) -> dict[str, Any]:
    """One CSV record to an `injuries` row; ValueError skips it."""
    gsis = _text(raw.get("gsis_id"))
    name = _text(raw.get("full_name"))
    if not gsis and not name:
        raise ValueError("no player id or name")
    team = nflverse.normalise_team(raw.get("team") or "")
    if not team:
        raise ValueError("no team")
    return {
        "season": nflverse._required_int(raw.get("season"), "season"),
        "game_type": nflverse.normalise_game_type(raw.get("game_type")),
        "week": nflverse._required_int(raw.get("week"), "week"),
        "team": team,
        "gsis_id": gsis or f"name:{name}",
        "full_name": name,
        "position": (_text(raw.get("position")) or "").upper() or None,
        "report_status": _text(raw.get("report_status")),
        "date_modified": _modified(raw.get("date_modified")),
    }


def _key(row: dict[str, Any]) -> tuple[Any, ...]:
    return row["season"], row["game_type"], row["week"], row["team"], row["gsis_id"]


def parse_rows(text: str) -> tuple[list[dict[str, Any]], int]:
    """(rows, skipped) of an injuries CSV body; 400 when it is not that file."""
    reader = csv.DictReader(io.StringIO(text))
    names = set(reader.fieldnames or [])
    if not set(REQUIRED) <= names or not ({"gsis_id", "full_name"} & names):
        raise BadRequest("not an nflverse injuries file: missing columns")
    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    skipped = 0
    for raw in reader:
        try:
            row = injury_row(raw)
        except (ValueError, TypeError):
            skipped += 1
            continue
        key = _key(row)
        old = rows.get(key)
        if old is None or (old["date_modified"] or "") <= (row["date_modified"] or ""):
            rows[key] = row
    return list(rows.values()), skipped


def upsert(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Insert new rows and rewrite changed ones; counts of each."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (UPSERT_LOCK,))
    columns = ", ".join(FIELDS)
    typed = ", ".join(f"{name} {kind}" for name, kind in zip(FIELDS, TYPES))
    keys = ("season", "game_type", "week", "team", "gsis_id")
    values = [name for name in FIELDS if name not in keys]
    updates = ", ".join(f"{name} = EXCLUDED.{name}" for name in values)
    distinct = " OR ".join(f"injuries.{name} IS DISTINCT FROM EXCLUDED.{name}" for name in values)
    sql = (
        f"INSERT INTO injuries ({columns}, updated_at) SELECT {columns}, clock_timestamp()"
        f" FROM jsonb_to_recordset(%s::jsonb) AS t({typed})"
        f" ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}, updated_at = clock_timestamp()"
        f" WHERE {distinct} RETURNING (xmax = 0) AS inserted"
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
    """Parse and upsert an injuries CSV body; counts, skipped, fetched_at and source."""
    fetched_at = datetime.now(timezone.utc)
    rows, skipped = parse_rows(text)
    try:
        result = upsert(conn, rows)
    except psycopg.Error as exc:
        raise Upstream(f"injuries ingest failed: {str(exc).strip().splitlines()[0]}") from None
    result.update(skipped=skipped, fetched_at=fetched_at, source=source)
    return result


def fetch_season(template: str, season: int) -> tuple[str, str]:
    """(text, url) of one season's download; Upstream on any failure."""
    url = season_url(template, season)
    return nflverse.fetch(url, max_bytes=MAX_BYTES), url


def ingest(conn: psycopg.Connection, season: int | None, source: str | None = None,
           template: str = DEFAULT_URL) -> dict[str, Any]:
    """Read a file (when `source` is given) or download one season, then upsert."""
    if source:
        return ingest_text(conn, nflverse.read_source(source, ""), source)
    if season is None:
        raise BadRequest("a season is required to download injuries")
    text, url = fetch_season(template, season)
    return ingest_text(conn, text, url)


def injuries_stamp(conn: psycopg.Connection) -> str:
    """`<count>-<max updated_at epoch>` of the injuries table."""
    row = conn.execute("SELECT count(*) AS n, max(updated_at) AS t FROM injuries").fetchone()
    return f"{row['n']}-{'0' if row['t'] is None else format(row['t'].timestamp(), '.6f')}"
