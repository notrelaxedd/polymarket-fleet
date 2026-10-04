"""Job params validation per kind and the settings copied into them at creation.

docs/PROTOCOL.md "Job kinds and params": the host checks shapes and ranges (400),
refuses unknown keys for the three batch kinds and unknown hyperparameter names for
the family, checks that `model_id` exists, refuses a `seasons` range in which no
season can be tested (needs games and three earlier seasons with moneylines, judged
on the games table when it has rows), and copies the betting limits in force
(fee_model, default_bankroll_cents, max_bet_cents, trade_max_games, backtest_seasons
with null resolved to the last complete season) so the worker runs with the limits
that held when the job was sent. `sleep` keeps its test-only shape (`seconds`, extra
keys allowed).

Step 6 (docs/ROBUSTNESS.md A1): `validate` takes `{model_id, seed}`; a model search
and a validate also carry `validation_seasons` (settings, null resolved like the
search era) and `workers` (settings `search_workers`). The search era never reaches
into the validation era: a null last search season is capped at the season before
the validation era, and an explicit search range that overlaps it is refused.
"""
from __future__ import annotations

from typing import Any

import psycopg

from fleet.models.registry import FAMILIES
from host.errors import BadRequest
from host.leases import as_uuid
from host.models import known_family
from host.nflverse import last_complete_season
from host.settings import FIRST_SEASON, LAST_SEASON, get_setting

COPIED_SETTINGS = ("fee_model", "default_bankroll_cents", "max_bet_cents", "trade_max_games")
SEARCH_DEFAULTS = {"n": 200, "seed": 0, "top_k": 5}
VALIDATE_DEFAULTS = {"seed": 1}
ERA_KINDS = ("model_search", "validate")  # the kinds that carry the validation era
MAX_WEEK = 22
MIN_HISTORY_SEASONS = 3  # fleet.sim.backtest skips a test season with less history
KEYS = {
    "backtest": {"model_id", "family", "params", "seasons"},
    "model_search": {"family", "n", "seed", "seasons", "top_k"},
    "train": {"model_id", "through"},
    "validate": {"model_id", "seed"},
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return _is_int(value) or (isinstance(value, float) and value == value and abs(value) != float("inf"))


def _int_field(params: dict[str, Any], key: str, low: int, high: int, default: int | None = None) -> int:
    value = params.get(key, default)
    if not _is_int(value):
        raise BadRequest(f"{key} must be an integer")
    if value < low or value > high:
        raise BadRequest(f"{key} must be between {low} and {high}")
    return int(value)


def _reject_unknown(kind: str, params: dict[str, Any]) -> None:
    unknown = sorted(set(params) - KEYS[kind])
    if unknown:
        raise BadRequest(f"unknown {kind} params: {', '.join(unknown)}")


def _model_id(conn: psycopg.Connection, value: Any) -> str:
    mid = as_uuid(value) if isinstance(value, str) else None
    if mid is None:
        raise BadRequest("model_id must be a uuid")
    if conn.execute("SELECT 1 FROM models WHERE id = %s", (mid,)).fetchone() is None:
        raise BadRequest("unknown model")
    return str(mid)


def _family(value: Any) -> str:
    if not known_family(value):
        raise BadRequest(f"unknown model family: {value!r}")
    return str(value)


def _model_params(value: Any, family: str) -> dict[str, Any]:
    """Numbers only, and every key one the family knows (a typo would otherwise run
    the family defaults under the owner's chosen name)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BadRequest("params must be an object of hyperparameters")
    for key, item in value.items():
        if not _is_number(item):
            raise BadRequest(f"params.{key} must be a number")
    known = FAMILIES[family].PARAM_KEYS
    unknown = sorted(set(value) - set(known)) if known else []
    if unknown:
        raise BadRequest(f"unknown {family} params: {', '.join(unknown)}")
    return value


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


def check_testable(conn: psycopg.Connection, seasons: list[int | None]) -> None:
    """400 when no season of [first, last] can be backtested: a testable season has
    games and at least MIN_HISTORY_SEASONS earlier seasons with moneylines. Skipped
    while the games table is empty (nothing to judge against)."""
    first, last = seasons
    rows = conn.execute(
        "SELECT season, bool_or(home_moneyline IS NOT NULL AND away_moneyline IS NOT NULL) AS lines"
        " FROM games GROUP BY season ORDER BY season"
    ).fetchall()
    if not rows or last is None:
        return
    with_lines = [r["season"] for r in rows if r["lines"]]
    for row in rows:
        season = row["season"]
        if first <= season <= last and len([s for s in with_lines if s < season]) >= MIN_HISTORY_SEASONS:
            return
    raise BadRequest(
        f"no testable season in [{first}, {last}]: a test season needs games and {MIN_HISTORY_SEASONS}"
        " earlier seasons with moneylines"
    )


def _through(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"season", "week"}:
        raise BadRequest("through must be {\"season\": int, \"week\": int}")
    season = _season(value["season"], "through.season")
    if not _is_int(value["week"]) or value["week"] < 1 or value["week"] > MAX_WEEK:
        raise BadRequest(f"through.week must be between 1 and {MAX_WEEK}")
    return {"season": season, "week": int(value["week"])}


def _sleep(params: dict[str, Any]) -> dict[str, Any]:
    if "seconds" in params:
        _int_field(params, "seconds", 1, 86400)
    return dict(params)


def _backtest(conn: psycopg.Connection, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown("backtest", params)
    out: dict[str, Any] = {}
    if "model_id" in params:
        if "family" in params or "params" in params:
            raise BadRequest("backtest takes model_id or family + params, not both")
        out["model_id"] = _model_id(conn, params["model_id"])
    elif "family" in params:
        out["family"] = _family(params["family"])
        out["params"] = _model_params(params.get("params"), out["family"])
    else:
        raise BadRequest("backtest needs model_id or family + params")
    if "seasons" in params:
        out["seasons"] = resolve_seasons(conn, params["seasons"])
        check_testable(conn, out["seasons"])
    return out


def validation_seasons(conn: psycopg.Connection) -> list[int | None] | None:
    """Settings `validation_seasons` with a null last resolved to the last complete
    season; None when the setting is missing or malformed."""
    value = get_setting(conn, "validation_seasons")
    if value is None:
        return None
    try:
        return resolve_seasons(conn, value, "validation_seasons")
    except BadRequest:
        return None


def check_before_validation(conn: psycopg.Connection, seasons: list[int | None], label: str = "seasons") -> None:
    """400 when a search era reaches into the validation era (selection would see
    the held-out seasons)."""
    validation = validation_seasons(conn)
    if validation is None:
        return
    last = seasons[1] if seasons[1] is not None else last_complete_season(conn)
    if last is not None and last >= validation[0]:
        raise BadRequest(f"{label} must end before the validation era (which starts in {validation[0]})")


def _model_search(conn: psycopg.Connection, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown("model_search", params)
    out: dict[str, Any] = {
        "family": _family(params.get("family")),
        "n": _int_field(params, "n", 1, 5000, SEARCH_DEFAULTS["n"]),
        "seed": _int_field(params, "seed", -(2**53), 2**53, SEARCH_DEFAULTS["seed"]),
        "top_k": _int_field(params, "top_k", 1, 20, SEARCH_DEFAULTS["top_k"]),
    }
    if "seasons" in params:
        out["seasons"] = resolve_seasons(conn, params["seasons"])
        check_testable(conn, out["seasons"])
        check_before_validation(conn, out["seasons"])
    return out


def _validate(conn: psycopg.Connection, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown("validate", params)
    if "model_id" not in params:
        raise BadRequest("validate needs model_id")
    return {
        "model_id": _model_id(conn, params["model_id"]),
        "seed": _int_field(params, "seed", -(2**53), 2**53, VALIDATE_DEFAULTS["seed"]),
    }


def _train(conn: psycopg.Connection, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown("train", params)
    if "model_id" not in params:
        raise BadRequest("train needs model_id")
    return {"model_id": _model_id(conn, params["model_id"]), "through": _through(params.get("through"))}


def copied_settings(conn: psycopg.Connection, kind: str = "backtest") -> dict[str, Any]:
    """The limits a batch job carries, read from settings at creation. A null last
    search season resolves to the last complete season, capped at the season before
    the validation era; a model search and a validate also carry the validation era
    and the search pool size."""
    out = {key: get_setting(conn, key) for key in COPIED_SETTINGS}
    seasons = get_setting(conn, "backtest_seasons", [2010, None])
    validation = validation_seasons(conn)
    try:
        if not isinstance(seasons, list) or len(seasons) != 2:
            raise BadRequest("backtest_seasons must be [first, last]")
        last = seasons[1]
        if last is None:
            last = last_complete_season(conn)
            if validation is not None and last is not None:
                last = min(last, validation[0] - 1)
        out["backtest_seasons"] = resolve_seasons(conn, [seasons[0], last], "backtest_seasons")
    except BadRequest:
        out["backtest_seasons"] = [2010, last_complete_season(conn)]
    if kind in ERA_KINDS:
        out["validation_seasons"] = validation if validation is not None else [2022, last_complete_season(conn)]
        out["workers"] = get_setting(conn, "search_workers", "auto")
    return out


def prepare_params(conn: psycopg.Connection, kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validated params for `kind`, with the settings copied in for the batch kinds."""
    if kind == "sleep":
        return _sleep(params)
    if kind == "backtest":
        out = _backtest(conn, params)
    elif kind == "model_search":
        out = _model_search(conn, params)
    elif kind == "train":
        out = _train(conn, params)
    elif kind == "validate":
        out = _validate(conn, params)
    else:
        return dict(params)
    out.update(copied_settings(conn, kind))
    return out
