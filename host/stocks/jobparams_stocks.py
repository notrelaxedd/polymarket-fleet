"""Params of the stock job kinds (contract sections 4 and 5), host.jobparams delegates here.

The host fills what the worker must not choose: `symbols` (the stock_symbols setting at
creation), `cost_bps` (stock_cost_bps) and the years, [first, null] resolved to the last
complete calendar year (New York). The search era never reaches into the validation era
(stock_validation_years): a null last search year is capped at the year before it, an
explicit overlap is refused, and so is a backtest of a stored model on years that
overlap it and a validate of a model whose search years overlap it. Unknown keys are
refused. stock_trade jobs are created only by an assignment
(host.stocks.assignments), never through the job form or POST /api/jobs.

    prepare_stock_params(conn, kind, params) -> dict   (400 on anything malformed)
    create_stock_job(conn, kind, params, actor, target=None) -> dict   (the job row)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from host.errors import BadRequest, NotFound
from host.settings import get_setting
from host.stocks.market import NEW_YORK
from host.stocks.models import FAMILIES, check_model_spec, get_model, model_id_of

STOCK_KINDS = ("stock_search", "stock_backtest", "stock_validate", "stock_trade")
KEYS = {
    "stock_search": {"n", "seed", "families", "years", "top_k"},
    "stock_backtest": {"model_id", "family", "params", "years"},
    "stock_validate": {"model_id"},
}
SEARCH_DEFAULTS = {"n": 200, "seed": 0, "top_k": 5}
FIRST_YEAR = 2016


def last_complete_year() -> int:
    return datetime.now(NEW_YORK).year - 1


def _int(params: dict[str, Any], key: str, low: int, high: int, default: int) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise BadRequest(f"{key} must be an integer between {low} and {high}")
    return value


def _years_value(value: Any, label: str) -> tuple[int, int | None]:
    if not isinstance(value, list) or len(value) != 2:
        raise BadRequest(f"{label} must be [first year, last year or null]")
    first, last = value
    for year in (first, last):
        if year is not None and (isinstance(year, bool) or not isinstance(year, int) or not FIRST_YEAR <= year <= 2100):
            raise BadRequest(f"{label} years must be between {FIRST_YEAR} and 2100")
    if first is None:
        raise BadRequest(f"{label} needs a first year")
    return first, last


def validation_years(conn: psycopg.Connection) -> list[int]:
    """stock_validation_years resolved; 400 when the era holds no complete year yet."""
    first, last = _years_value(get_setting(conn, "stock_validation_years", [2024, None]), "stock_validation_years")
    last = last_complete_year() if last is None else last
    if last < first:
        raise BadRequest(f"the validation era starting {first} has no complete year yet")
    return [first, last]


def search_years(conn: psycopg.Connection, value: Any = None) -> list[int]:
    """The search era: `value` or stock_backtest_years, resolved and kept before the
    validation era (a null end is capped there, an explicit overlap is 400)."""
    explicit = value is not None
    first, last = _years_value(value if explicit else get_setting(conn, "stock_backtest_years", [2017, None]),
                               "years" if explicit else "stock_backtest_years")
    val_first = validation_years(conn)[0]
    if last is None:
        last = min(last_complete_year(), val_first - 1)
    elif last >= val_first:
        raise BadRequest(f"years [{first}, {last}] overlap the validation era starting {val_first}")
    if last < first:
        raise BadRequest(f"the search era [{first}, {last}] is empty (the validation era starts {val_first})")
    return [first, last]


def _overlaps(years: list[int], era: list[int]) -> bool:
    return years[0] <= era[1] and era[0] <= years[1]


def model_search_years(conn: psycopg.Connection, model: dict[str, Any]) -> list[int] | None:
    """The years a stored model was searched on: its search job's params.years, else
    the years of its backtest metrics' first and last day; None when unknown."""
    if model.get("created_by_job_id") is not None:
        row = conn.execute("SELECT params FROM jobs WHERE id = %s", (model["created_by_job_id"],)).fetchone()
        years = (row["params"] or {}).get("years") if row and isinstance(row["params"], dict) else None
        if isinstance(years, list) and len(years) == 2 and all(isinstance(y, int) for y in years):
            return [int(years[0]), int(years[1])]
    bt = model.get("backtest_metrics") if isinstance(model.get("backtest_metrics"), dict) else {}
    try:
        return [int(str(bt["first_day"])[:4]), int(str(bt["last_day"])[:4])]
    except (KeyError, TypeError, ValueError):
        return None


def _copies(conn: psycopg.Connection) -> dict[str, Any]:
    symbols = get_setting(conn, "stock_symbols", ["SPY"])
    return {"symbols": list(symbols) if isinstance(symbols, list) else ["SPY"],
            "cost_bps": get_setting(conn, "stock_cost_bps", 5)}


def _reject_unknown(kind: str, params: dict[str, Any]) -> None:
    unknown = sorted(set(params) - KEYS[kind])
    if unknown:
        raise BadRequest(f"unknown {kind} params: {', '.join(unknown)}")


def _search(conn: psycopg.Connection, params: dict[str, Any]) -> dict[str, Any]:
    families = params.get("families", list(FAMILIES))
    if not isinstance(families, list) or not families or any(f not in FAMILIES for f in families):
        raise BadRequest(f"families must be a non-empty list of {', '.join(FAMILIES)}")
    return {
        "n": _int(params, "n", 1, 5000, SEARCH_DEFAULTS["n"]),
        "seed": _int(params, "seed", -(2**53), 2**53, SEARCH_DEFAULTS["seed"]),
        "families": list(dict.fromkeys(families)),
        "top_k": _int(params, "top_k", 1, 20, SEARCH_DEFAULTS["top_k"]),
        "years": search_years(conn, params.get("years")),
        **_copies(conn),
    }


def _stored_model(conn: psycopg.Connection, value: Any) -> dict[str, Any]:
    if model_id_of(value) is None:
        raise BadRequest("model_id must be a stock model id")
    try:
        return get_model(conn, value)
    except NotFound:  # 400 here: the job names a model that does not exist
        raise BadRequest("unknown stock model") from None


def _backtest(conn: psycopg.Connection, params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "model_id" in params:
        if "family" in params or "params" in params:
            raise BadRequest("stock_backtest takes model_id or family + params, not both")
        model = _stored_model(conn, params["model_id"])
        out.update(model_id=int(model["id"]), model={"family": model["family"], "params": model["params"]})
        years = search_years(conn, params.get("years"))  # refuses an overlap with the validation era
        out["validation_years"] = validation_years(conn)  # so the worker can double-check the eras
    elif "family" in params:
        family, spec = check_model_spec(params["family"], params.get("params", {}))
        out["model"] = {"family": family, "params": spec}
        years = search_years(conn, params.get("years"))
    else:
        raise BadRequest("stock_backtest needs model_id or family + params")
    return {**out, "years": years, **_copies(conn)}


def _validate(conn: psycopg.Connection, params: dict[str, Any]) -> dict[str, Any]:
    if "model_id" not in params:
        raise BadRequest("stock_validate needs model_id")
    model = _stored_model(conn, params["model_id"])
    era = validation_years(conn)
    searched = model_search_years(conn, model)
    if searched is not None and _overlaps(searched, era):
        raise BadRequest(f"model {model['id']} was searched on {searched}, which overlaps the validation era {era}")
    out = {"model_id": int(model["id"]), "model": {"family": model["family"], "params": model["params"]},
           "years": era, **_copies(conn)}
    if searched is not None:
        out["search_years"] = searched  # the worker double-checks the eras
    return out


def prepare_stock_params(conn: psycopg.Connection, kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validated params of a stock job kind with the host-filled values."""
    if kind == "stock_trade":
        raise BadRequest("stock_trade jobs are created by a stock assignment (POST /api/stocks/assignments)")
    _reject_unknown(kind, params)
    if kind == "stock_search":
        return _search(conn, params)
    if kind == "stock_backtest":
        return _backtest(conn, params)
    return _validate(conn, params)


def create_stock_job(
    conn: psycopg.Connection, kind: str, params: dict[str, Any] | None, actor: str, target: str | None = None,
) -> dict[str, Any]:
    """Queue a stock_search, stock_backtest or stock_validate job (params filled by
    prepare_stock_params through host.queue.create_job); the job row. `target` is None
    (any worker of the role), "any_idle" or a worker id, as for POST /api/jobs."""
    from host import queue  # deferred: host.queue -> host.jobparams imports this module

    if kind not in KEYS:
        raise BadRequest(f"kind must be one of {', '.join(KEYS)}")
    return dict(queue.create_job(conn, kind, params or {}, target, None, actor=actor).job)
