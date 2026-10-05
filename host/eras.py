"""The two eras a batch job carries (docs/ROBUSTNESS.md A1): the search era
(settings `backtest_seasons`) and the held-out validation era (`validation_seasons`).

Selection must never see the validation era, so every route that could let it is
refused with a 400 naming the era instead of falling back to a default:
- a search era that does not resolve, is empty once a null last season is capped at
  the season before the validation era, or reaches into the validation era;
- a validation era that does not resolve (malformed, or starting after the last
  complete season);
- a validate job on a model whose own search era reached into the validation era
  (its "validation" would be in-sample: models searched on the step 3 default
  [2010, null] must be searched again);
- a backtest of a stored model (its result becomes the lineage's search-era metrics)
  on seasons that reach into the validation era.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.errors import BadRequest
from host.nflverse import last_complete_season
from host.settings import FIRST_SEASON, LAST_SEASON, get_setting

DEFAULT_SEARCH_ERA: list[int | None] = [2010, None]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _season(value: Any, label: str) -> int:
    if not _is_int(value) or value < FIRST_SEASON or value > LAST_SEASON:
        raise BadRequest(f"{label} must be an integer between {FIRST_SEASON} and {LAST_SEASON}")
    return int(value)


def resolve_seasons(conn: psycopg.Connection, value: Any, label: str = "seasons") -> list[int | None]:
    """[first, last] with a null last replaced by the last complete season (kept null
    when the games table has none)."""
    if not isinstance(value, list) or len(value) != 2:
        raise BadRequest(f"{label} must be [first, last]")
    first = _season(value[0], f"{label} first")
    last = None if value[1] is None else _season(value[1], f"{label} last")
    if last is None:
        last = last_complete_season(conn)
    if last is not None and last < first:
        raise BadRequest(f"{label} last must not be before first")
    return [first, last]


def validation_first(conn: psycopg.Connection) -> int | None:
    """The first season of the validation era as set (resolved or not); None when the
    setting is missing or malformed."""
    value = get_setting(conn, "validation_seasons")
    if isinstance(value, list) and len(value) == 2 and _is_int(value[0]):
        return int(value[0])
    return None


def validation_seasons(conn: psycopg.Connection) -> list[int | None] | None:
    """Settings `validation_seasons` with a null last resolved to the last complete
    season; None when the setting is missing, malformed or has no complete season."""
    value = get_setting(conn, "validation_seasons")
    if value is None:
        return None
    try:
        return resolve_seasons(conn, value, "validation_seasons")
    except BadRequest:
        return None


def required_validation_seasons(conn: psycopg.Connection) -> list[int | None]:
    """The resolved validation era, or a 400 that names the setting."""
    resolved = validation_seasons(conn)
    if resolved is None:
        raise BadRequest(
            f"validation_seasons {get_setting(conn, 'validation_seasons')} does not resolve to a complete season:"
            " fix it in Settings before sending a model search or a validate job"
        )
    return resolved


def search_era(conn: psycopg.Connection) -> list[int | None]:
    """Settings `backtest_seasons` resolved: a null last season becomes the last
    complete season capped at the season before the validation era. 400 when it is
    malformed, empty once capped, or reaches into the validation era."""
    seasons = get_setting(conn, "backtest_seasons", DEFAULT_SEARCH_ERA)
    if not isinstance(seasons, list) or len(seasons) != 2:
        raise BadRequest("backtest_seasons must be [first, last]")
    vfirst = validation_first(conn)
    last = seasons[1]
    if last is None:
        last = last_complete_season(conn)
        if vfirst is not None:
            last = vfirst - 1 if last is None else min(last, vfirst - 1)
    try:
        resolved = resolve_seasons(conn, [seasons[0], last], "backtest_seasons")
    except BadRequest as exc:
        raise BadRequest(
            f"backtest_seasons {seasons} is empty or malformed once it ends before the validation era"
            f" (which starts in {vfirst}): {exc.message}"
        ) from exc
    if vfirst is not None and resolved[1] is not None and resolved[1] >= vfirst:
        raise BadRequest(f"backtest_seasons {seasons} must end before the validation era (which starts in {vfirst})")
    return resolved


def check_before_validation(conn: psycopg.Connection, seasons: list[int | None], label: str = "seasons") -> None:
    """400 when a search era reaches into the validation era (selection would see
    the held-out seasons)."""
    vfirst = validation_first(conn)
    if vfirst is None:
        return
    last = seasons[1] if seasons[1] is not None else last_complete_season(conn)
    if last is None or last >= vfirst:
        raise BadRequest(f"{label} must end before the validation era (which starts in {vfirst})")


def _job_search_seasons(conn: psycopg.Connection, job_id: Any) -> list[int] | None:
    """The [first, last] a model_search job searched, from its params."""
    if job_id is None:
        return None
    row = conn.execute("SELECT kind, params FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if row is None or row["kind"] != "model_search" or not isinstance(row["params"], dict):
        return None
    era = row["params"].get("seasons") or row["params"].get("backtest_seasons")
    if isinstance(era, list) and len(era) == 2 and all(_is_int(s) for s in era):
        return [int(era[0]), int(era[1])]
    return None


def model_search_seasons(conn: psycopg.Connection, model_id: str) -> list[int] | None:
    """The seasons the lineage of a model was selected on: its search-era metrics'
    tested seasons, else the range of the search job that created the lineage root;
    None when neither is known."""
    row = conn.execute(
        "SELECT r.backtest_metrics, r.created_by_job_id FROM models m JOIN models r ON r.id = m.lineage_id WHERE m.id = %s",
        (model_id,),
    ).fetchone()
    if row is None:
        return None
    metrics = row["backtest_metrics"] if isinstance(row["backtest_metrics"], dict) else {}
    tested = [int(s) for s in metrics.get("seasons") or [] if _is_int(s)]
    if tested:
        return tested
    era = _job_search_seasons(conn, row["created_by_job_id"])
    return list(range(era[0], era[1] + 1)) if era else None


def check_validate_era(conn: psycopg.Connection, model_id: str, validation: list[int | None]) -> None:
    """400 when the model's lineage was searched on seasons at or after the start of
    the validation era: its validation would be in-sample."""
    searched = model_search_seasons(conn, model_id)
    if searched and max(searched) >= int(validation[0]):  # type: ignore[arg-type]
        raise BadRequest(
            f"this model was searched on {min(searched)}-{max(searched)}, which reaches into the validation era"
            f" (which starts in {validation[0]}): its validation would be in-sample; run a new model search on"
            " seasons before the validation era and validate its models instead"
        )
