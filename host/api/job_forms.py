"""The jobs page: one send form per kind (backtest, model search, train, sleep).

`parse_job_form` turns a posted form into (kind, params) for queue.create_job, which
validates ranges and copies the settings in; `jobs_context` builds what the page's
forms need (models for the selects, families, defaults from settings).
"""
from __future__ import annotations

import json
from typing import Any

import psycopg

from fleet.models.registry import FAMILIES
from host import views
from host.errors import BadRequest
from host.jobparams import SEARCH_DEFAULTS
from host.leaderboard import short_params
from host.nflverse import last_complete_season
from host.settings import get_setting

KINDS = ("backtest", "model_search", "train", "sleep")
FAMILY_NAMES = tuple(sorted(FAMILIES))


def _text(form: dict[str, str], name: str) -> str:
    return (form.get(name) or "").strip()


def _int(form: dict[str, str], name: str, label: str, default: int | None = None) -> int | None:
    text = _text(form, name)
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        raise BadRequest(f"{label} must be a whole number") from None


def _seasons(form: dict[str, str]) -> list[int | None] | None:
    """[first, last] from the two season fields; None when both are blank."""
    first = _int(form, "seasons_first", "First season")
    last = _int(form, "seasons_last", "Last season")
    if first is None and last is None:
        return None
    if first is None:
        raise BadRequest("First season is required when a last season is given")
    return [first, last]


def _params_json(form: dict[str, str]) -> dict[str, Any]:
    text = _text(form, "params") or "{}"
    try:
        value = json.loads(text)
    except ValueError:
        raise BadRequest("Params must be a JSON object such as {\"k\": 24}") from None
    if not isinstance(value, dict):
        raise BadRequest("Params must be a JSON object")
    return value


def _backtest(form: dict[str, str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    model_id = _text(form, "model_id")
    if model_id:
        params["model_id"] = model_id
    else:
        params["family"] = _text(form, "family")
        params["params"] = _params_json(form)
    seasons = _seasons(form)
    if seasons is not None:
        params["seasons"] = seasons
    return params


def _model_search(form: dict[str, str]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "family": _text(form, "family"),
        "n": _int(form, "n", "Candidates", SEARCH_DEFAULTS["n"]),
        "seed": _int(form, "seed", "Seed", SEARCH_DEFAULTS["seed"]),
        "top_k": _int(form, "top_k", "Top k", SEARCH_DEFAULTS["top_k"]),
    }
    seasons = _seasons(form)
    if seasons is not None:
        params["seasons"] = seasons
    return params


def _train(form: dict[str, str]) -> dict[str, Any]:
    season = _int(form, "through_season", "Through season")
    week = _int(form, "through_week", "Through week")
    if season is None or week is None:
        raise BadRequest("Through season and week are required")
    return {"model_id": _text(form, "model_id"), "through": {"season": season, "week": week}}


def _sleep(form: dict[str, str]) -> dict[str, Any]:
    seconds = _int(form, "seconds", "Seconds", 60)
    if seconds is None or seconds < 1 or seconds > 86400:
        raise BadRequest("seconds must be between 1 and 86400")
    return {"seconds": seconds}


def parse_job_form(form: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """(kind, params) for a posted send form; BadRequest names the field at fault."""
    kind = _text(form, "kind")
    if kind == "backtest":
        return kind, _backtest(form)
    if kind == "model_search":
        return kind, _model_search(form)
    if kind == "train":
        return kind, _train(form)
    if kind == "sleep":
        return kind, _sleep(form)
    raise BadRequest(f"unknown job kind: {kind!r}")


def model_options(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Models for the selects, newest first, labelled "K 24 · HFA 55 · MOV on · thru 2024
    w18 · 8f173b7b" (untrained for a search candidate); the family is prefixed only
    when more than one family exists, so the label fits a phone-width select."""
    rows = conn.execute(
        "SELECT id, family, params, status, trained_through FROM models ORDER BY created_at DESC, id LIMIT 200"
    ).fetchall()
    families = {row["family"] for row in rows}
    out = []
    for row in rows:
        through = row["trained_through"]
        point = f"thru {through[0]} w{through[1]}" if isinstance(through, list) and len(through) == 2 else "untrained"
        label = short_params(row["family"], row["params"])
        if len(families) > 1:
            label = f"{row['family']} · {label}"
        out.append({"id": str(row["id"]), "label": f"{label} · {point} · {str(row['id'])[:8]}"})
    return out


def jobs_context(
    conn: psycopg.Connection, train_model: str | None = None, error: str | None = None,
    submitted: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Everything jobs.html needs besides the job list."""
    seasons = get_setting(conn, "backtest_seasons", [2010, None])
    seasons = (list(seasons) + [None, None])[:2] if isinstance(seasons, list) else [2010, None]
    last = last_complete_season(conn)
    values = {
        "seasons_first": "" if seasons[0] is None else str(seasons[0]),
        "seasons_last": "" if seasons[1] is None else str(seasons[1]),
        "n": str(SEARCH_DEFAULTS["n"]), "seed": str(SEARCH_DEFAULTS["seed"]), "top_k": str(SEARCH_DEFAULTS["top_k"]),
        "through_season": "" if last is None else str(last), "through_week": "22", "seconds": "60",
        "params": "{}", "model_id": train_model or "", "family": FAMILY_NAMES[0] if FAMILY_NAMES else "",
        "target": "any_idle",
    }
    values.update({k: v for k, v in (submitted or {}).items() if k in values})
    active = (submitted or {}).get("kind") or ("train" if train_model else "backtest")
    return {
        "names": views.worker_names(conn),
        "models": model_options(conn),
        "families": FAMILY_NAMES,
        "values": values,
        "active": active if active in KINDS else "backtest",
        "error": error,
        "last_complete_season": last,
    }
