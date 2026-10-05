"""The jobs page: one send form per kind (backtest, model search, train, validate, sleep).

`parse_job_form` turns a posted form into (kind, params) for queue.create_job, which
validates ranges and copies the settings in; `jobs_context` builds what the page's
forms need (models for the selects, families, defaults from settings, and the step 6
B1 snapshot replay settings the backtest form's price source choice explains).
"""
from __future__ import annotations

import json
from typing import Any

import psycopg

from fleet.models.registry import FAMILIES, PREGAME_FAMILIES
from host import views
from host.errors import BadRequest
from host.ingame_jobparams import INGAME_DEFAULTS, INGAME_FAMILY
from host.jobparams import SEARCH_DEFAULTS, VALIDATE_DEFAULTS
from host.leaderboard import short_params
from host.nflverse import last_complete_season
from host.settings import get_setting
from host.snapshot_store import PRICE_SOURCES, snapshot_settings

KINDS = ("backtest", "model_search", "train", "validate", "sleep")
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
    source = _text(form, "price_source")
    if source:
        params["price_source"] = source  # host.jobparams refuses an unknown one
    return params


def _ingame_eras(form: dict[str, str]) -> dict[str, Any]:
    """train_seasons and validation_seasons of an ingame_wp search from the form's
    in-game era fields (blank = the host defaults; a blank last = open end)."""
    out: dict[str, Any] = {}
    for key, prefix, label in (("train_seasons", "ingame_train", "In-game train"),
                               ("validation_seasons", "ingame_validation", "In-game validation")):
        first = _int(form, f"{prefix}_first", f"{label} first season")
        last = _int(form, f"{prefix}_last", f"{label} last season")
        if first is None and last is None:
            continue
        if first is None:
            raise BadRequest(f"{label} first season is required when a last season is given")
        out[key] = [first, last]
    return out


def _model_search(form: dict[str, str]) -> dict[str, Any]:
    if _text(form, "family") == INGAME_FAMILY:  # its own eras, not the pre-game seasons
        return {
            "family": INGAME_FAMILY,
            "n": _int(form, "n", "Candidates", INGAME_DEFAULTS["n"]),
            "seed": _int(form, "seed", "Seed", INGAME_DEFAULTS["seed"]),
            "top_k": _int(form, "top_k", "Top k", INGAME_DEFAULTS["top_k"]),
            **_ingame_eras(form),
        }
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


def _validate(form: dict[str, str]) -> dict[str, Any]:
    return {"model_id": _text(form, "model_id"), "seed": _int(form, "validate_seed", "Seed", VALIDATE_DEFAULTS["seed"])}


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
    if kind == "validate":
        return kind, _validate(form)
    if kind == "sleep":
        return kind, _sleep(form)
    raise BadRequest(f"unknown job kind: {kind!r}")


def model_options(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Models for the selects, newest first, labelled "K 24 · HFA 55 · MOV on · thru 2024
    w18 · 8f173b7b" (untrained for a search candidate); the family is prefixed only
    when more than one family exists, so the label fits a phone-width select. ingame_wp
    models are left out: backtest, train and validate refuse them."""
    rows = conn.execute(
        "SELECT id, family, params, status, trained_through FROM models WHERE family <> %s"
        " ORDER BY created_at DESC, id LIMIT 200",
        (INGAME_FAMILY,),
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
    submitted: dict[str, str] | None = None, validate_model: str | None = None,
) -> dict[str, Any]:
    """Everything jobs.html needs besides the job list; `train_model` or
    `validate_model` preselects that model and that kind. `new_open` opens the "New
    job" disclosure (a prefill link, or a rejected post re-rendered with its error)."""
    seasons = get_setting(conn, "backtest_seasons", [2010, None])
    seasons = (list(seasons) + [None, None])[:2] if isinstance(seasons, list) else [2010, None]
    validation = get_setting(conn, "validation_seasons", [2022, None])
    validation = (list(validation) + [None, None])[:2] if isinstance(validation, list) else [2022, None]
    last = last_complete_season(conn)
    values = {
        "seasons_first": "" if seasons[0] is None else str(seasons[0]),
        "seasons_last": "" if seasons[1] is None else str(seasons[1]),
        "n": str(SEARCH_DEFAULTS["n"]), "seed": str(SEARCH_DEFAULTS["seed"]), "top_k": str(SEARCH_DEFAULTS["top_k"]),
        "through_season": "" if last is None else str(last), "through_week": "22", "seconds": "60",
        "params": "{}", "model_id": train_model or validate_model or "", "family": FAMILY_NAMES[0] if FAMILY_NAMES else "",
        "target": "any_idle", "validate_seed": str(VALIDATE_DEFAULTS["seed"]), "price_source": PRICE_SOURCES[0],
        "ingame_train_first": "", "ingame_train_last": "", "ingame_validation_first": "", "ingame_validation_last": "",
    }
    values.update({k: v for k, v in (submitted or {}).items() if k in values})
    active = (submitted or {}).get("kind") or ("train" if train_model else "validate" if validate_model else "backtest")
    return {
        "new_open": bool(submitted or error or train_model or validate_model),
        "names": views.worker_names(conn),
        "models": model_options(conn),
        "families": FAMILY_NAMES,
        "pregame_families": tuple(f for f in FAMILY_NAMES if f in PREGAME_FAMILIES),
        "ingame_defaults": INGAME_DEFAULTS,
        "values": values,
        "active": active if active in KINDS else "backtest",
        "error": error,
        "last_complete_season": last,
        "validation_span": f"{validation[0]}-{validation[1] if validation[1] is not None else (last or 'last complete')}",
        "price_sources": PRICE_SOURCES,
        "replay": replay_context(conn),
    }


def replay_context(conn: psycopg.Connection) -> dict[str, Any]:
    """The snapshot replay settings a snapshot backtest would carry, and whether it
    would be refused (platform sim while allow_sim_prices is off)."""
    replay = snapshot_settings(conn)
    replay["sim_refused"] = replay["price_platform"] == "sim" and not replay["allow_sim_prices"]
    return replay
