"""Job params of the in-game model family (docs/INGAME.md, contract section 6).

A model search of family `ingame_wp` runs on play-by-play rows, not on the pre-game
backtest, so it carries its own eras instead of the settings ones: `train_seasons`
(default [2012, 2021]) and `validation_seasons` (default [2022, null], null = through
the newest season in the feed), plus `n` (default 20), `seed` (1), `top_k` (5) and
`train_fraction` (0.3, the share of train games the fit uses). The train era must end
before the validation era starts, so the selection never sees the held-out plays. As in
the pre-game search (host/eras.py), `seasons` is accepted for `train_seasons` (the Jobs
form sends it) and a null last train season is capped at the season before the
validation era.

Stored ingame_wp models are judged by their search's validation on held-out plays;
the pre-game jobs (backtest, validate, train) refuse them with a 400 rather than run a
moneyline backtest that means nothing for an in-game model.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.errors import BadRequest
from host.settings import FIRST_SEASON, LAST_SEASON

INGAME_FAMILY = "ingame_wp"
INGAME_PARAM_KEYS = ("l2", "time_scale", "fp_scale")
INGAME_KEYS = {"family", "n", "seed", "top_k", "train_seasons", "validation_seasons", "train_fraction", "seasons"}
INGAME_DEFAULTS: dict[str, Any] = {
    "train_seasons": [2012, 2021], "validation_seasons": [2022, None], "n": 20, "seed": 1, "top_k": 5,
    "train_fraction": 0.3,
}
MAX_N = 500


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int(params: dict[str, Any], key: str, low: int, high: int) -> int:
    value = params.get(key, INGAME_DEFAULTS[key])
    if not _is_int(value):
        raise BadRequest(f"{key} must be an integer")
    if value < low or value > high:
        raise BadRequest(f"{key} must be between {low} and {high}")
    return int(value)


def _season(value: Any, label: str) -> int:
    if not _is_int(value) or value < FIRST_SEASON or value > LAST_SEASON:
        raise BadRequest(f"{label} must be an integer between {FIRST_SEASON} and {LAST_SEASON}")
    return int(value)


def _pair(params: dict[str, Any], key: str, last_nullable: bool) -> list[int | None]:
    value = params.get(key, INGAME_DEFAULTS[key])
    if not isinstance(value, list) or len(value) != 2:
        raise BadRequest(f"{key} must be [first, last]")
    first = _season(value[0], f"{key} first")
    if value[1] is None:
        if not last_nullable:
            raise BadRequest(f"{key} last must be a season, not null")
        return [first, None]
    last = _season(value[1], f"{key} last")
    if last < first:
        raise BadRequest(f"{key} last must not be before the first")
    return [first, last]


def _fraction(params: dict[str, Any]) -> float:
    value = params.get("train_fraction", INGAME_DEFAULTS["train_fraction"])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise BadRequest("train_fraction must be a number")
    if value <= 0 or value > 1:
        raise BadRequest("train_fraction must be above 0 and at most 1")
    return float(value)


def ingame_search_params(params: dict[str, Any]) -> dict[str, Any]:
    """The checked params of a model_search of family ingame_wp, defaults filled in;
    400 on unknown keys, bad ranges or a train era that reaches the validation era."""
    unknown = sorted(set(params) - INGAME_KEYS)
    if unknown:
        raise BadRequest(f"unknown ingame_wp model_search params: {', '.join(unknown)}")
    if "seasons" in params:
        if "train_seasons" in params:
            raise BadRequest("give train_seasons or seasons, not both")
        params = {**{k: v for k, v in params.items() if k != "seasons"}, "train_seasons": params["seasons"]}
    validation = _pair(params, "validation_seasons", last_nullable=True)
    train = _pair(params, "train_seasons", last_nullable=True)
    if train[1] is None:  # capped at the season before the validation era
        train[1] = int(validation[0]) - 1  # type: ignore[arg-type]
        if train[1] < int(train[0]):  # type: ignore[arg-type]
            raise BadRequest(f"train_seasons {[train[0], None]} leave no season before validation_seasons {validation}")
    if int(train[1]) >= int(validation[0]):  # type: ignore[arg-type]
        raise BadRequest(
            f"train_seasons {train} must end before validation_seasons {validation} start:"
            " the selection must never see the held-out plays"
        )
    return {
        "family": INGAME_FAMILY,
        "train_seasons": train,
        "validation_seasons": validation,
        "n": _int(params, "n", 1, MAX_N),
        "seed": _int(params, "seed", -(2**53), 2**53),
        "top_k": _int(params, "top_k", 1, 20),
        "train_fraction": _fraction(params),
    }


def is_ingame_search(kind: str, params: dict[str, Any]) -> bool:
    return kind == "model_search" and isinstance(params, dict) and params.get("family") == INGAME_FAMILY


def refuse_ingame_model(conn: psycopg.Connection, kind: str, model_id: str | None) -> None:
    """400 when a pre-game job (backtest, validate, train) names an ingame_wp model."""
    if model_id is None:
        return
    row = conn.execute("SELECT family FROM models WHERE id = %s", (model_id,)).fetchone()
    if row is not None and row["family"] == INGAME_FAMILY:
        raise BadRequest(
            f"a {kind} job does not run ingame_wp models: an in-game model is validated on held-out plays by its"
            " model search, and its paper record comes from in-game trading"
        )
