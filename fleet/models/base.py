"""The Model interface every family implements (docs/MODELS.md) and params_hash."""

from __future__ import annotations

import hashlib
import json
import random
from typing import Any, Callable


def _canonical(value: Any) -> Any:
    """Floats rounded to 6 decimals, dict keys sorted (by json.dumps), recursively."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        rounded = round(value, 6)
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def canonical_json(params: dict[str, Any]) -> str:
    return json.dumps(_canonical(params), sort_keys=True, separators=(",", ":"))


def params_hash(params: dict[str, Any]) -> str:
    """sha256 of the canonical JSON (sorted keys, floats to 6 decimals), first 16 hex."""
    return hashlib.sha256(canonical_json(params).encode("utf-8")).hexdigest()[:16]


class Model:
    """Base class; subclasses set `family` and implement every method below.

    games are dicts from fleet.sim.data sorted by kickoff; `through` is an inclusive
    (season, week) tuple or None for everything; `features` is the dict built by
    fleet.sim.data.features_of.
    """

    family: str = ""
    PARAM_KEYS: tuple[str, ...] = ()  # the hyperparameter names the host accepts; () = unchecked

    def __init__(self, params: dict[str, Any]) -> None:
        self.params: dict[str, Any] = dict(params)

    def fit(self, games: list[dict[str, Any]], through: tuple[int, int] | None,
            should_stop: Callable[[], bool],
            on_season: Callable[[int], None] | None = None) -> None:
        """Replay the history; on_season(season) is called after each season is replayed."""
        raise NotImplementedError

    def predict(self, game: dict[str, Any], market_p: float | None, features: dict[str, Any]) -> float:
        raise NotImplementedError

    def observe(self, game: dict[str, Any]) -> None:
        """Learn a finished game's result after its prediction was recorded (walk-forward hook)."""

    def to_json(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def from_json(cls, params: dict[str, Any], artifact: dict[str, Any]) -> "Model":
        raise NotImplementedError

    @staticmethod
    def search_space(rng: random.Random) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def summary(params: dict[str, Any], metrics: dict[str, Any]) -> str:
        raise NotImplementedError
