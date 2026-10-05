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
into the validation era (host/eras.py): a null last search season is capped at the
season before the validation era; an explicit search range that overlaps it, a
backtest of a stored model on seasons that overlap it, eras that do not resolve, and
a validate of a model whose own search reached into it are refused.
"""
from __future__ import annotations

from typing import Any

import psycopg

from fleet.models.registry import FAMILIES
from host.eras import (_season, check_before_validation, check_validate_era, required_validation_seasons,
                       resolve_seasons, search_era)
from host.errors import BadRequest
from host.leases import as_uuid
from host.models import known_family
from host.nflverse import last_complete_season
from host.settings import FIRST_SEASON, LAST_SEASON, get_setting
from host.snapshot_store import PRICE_SOURCES, SNAPSHOTS, latest_season, snapshot_settings

COPIED_SETTINGS = ("fee_model", "default_bankroll_cents", "max_bet_cents", "trade_max_games")
SEARCH_DEFAULTS = {"n": 200, "seed": 0, "top_k": 5}
VALIDATE_DEFAULTS = {"seed": 1}
ERA_KINDS = ("model_search", "validate")  # the kinds that carry the validation era
STRICT_ERA_KINDS = ("backtest", "model_search", "validate")  # refused when the eras do not resolve
MAX_WEEK = 22
MIN_HISTORY_SEASONS = 3  # fleet.sim.backtest skips a test season with less history
KEYS = {
    "backtest": {"model_id", "family", "params", "seasons", "price_source"},
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
    if "price_source" in params:  # absent means closing_line (the worker's default too)
        if params["price_source"] not in PRICE_SOURCES:
            raise BadRequest(f"price_source must be one of {', '.join(PRICE_SOURCES)}")
        out["price_source"] = params["price_source"]
    if "seasons" in params:
        seasons = params["seasons"]
        if out.get("price_source") == SNAPSHOTS and isinstance(seasons, list) and len(seasons) == 2 and seasons[1] is None:
            seasons = [seasons[0], latest_season(conn)]  # a replay runs through the season in progress
        out["seasons"] = resolve_seasons(conn, seasons)
        check_testable(conn, out["seasons"])
        if "model_id" in out and out.get("price_source") != SNAPSHOTS:
            # A closing-line result becomes the lineage's search-era metrics, so it must
            # end before the validation era. A snapshot replay is stored apart, in
            # snapshot_metrics, and replays recorded prices of recent seasons by design.
            check_before_validation(conn, out["seasons"])
    return out


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
    """The limits a batch job carries, read from settings at creation: the search era
    (host.eras.search_era: a null last season capped at the season before the
    validation era) and, for a model search and a validate, the validation era and the
    search pool size. A backtest, a search or a validate whose eras do not resolve
    cleanly is refused (400) rather than run on a fallback era; a train job, which
    never reads the eras, keeps the step 3 fallback."""
    out = {key: get_setting(conn, key) for key in COPIED_SETTINGS}
    try:
        out["backtest_seasons"] = search_era(conn)
    except BadRequest:
        if kind in STRICT_ERA_KINDS:
            raise
        out["backtest_seasons"] = [2010, last_complete_season(conn)]
    if kind in ERA_KINDS:
        out["validation_seasons"] = required_validation_seasons(conn)
        out["workers"] = get_setting(conn, "search_workers", "auto")
    return out


def _snapshot_copies(conn: psycopg.Connection, kind: str, out: dict[str, Any]) -> dict[str, Any]:
    """The replay settings a snapshot backtest carries ({} for any other job). Without
    explicit seasons its backtest_seasons run from the setting's first season through
    the latest season in games (the season in progress included), whatever last season
    the setting stores: a replay selects nothing, so neither the search-era last season
    nor the validation cap applies, and the worker drops seasons without recorded
    markets anyway."""
    if kind != "backtest" or out.get("price_source") != SNAPSHOTS:
        return {}
    copies = snapshot_settings(conn)
    seasons = get_setting(conn, "backtest_seasons", [2010, None])
    latest = latest_season(conn)
    if "seasons" not in out and isinstance(seasons, list) and len(seasons) == 2 and latest is not None:
        try:
            copies["backtest_seasons"] = resolve_seasons(conn, [seasons[0], latest], "backtest_seasons")
        except BadRequest:
            pass  # keep the closing-line copy
    return copies


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
    if kind == "validate":
        check_validate_era(conn, out["model_id"], out["validation_seasons"])
    out.update(_snapshot_copies(conn, kind, out))
    return out
