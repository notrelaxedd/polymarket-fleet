"""Model families by registry key."""

from __future__ import annotations

from fleet.models.base import Model
from fleet.models.elo_blend import EloBlend
from fleet.models.epa_blend import EpaBlend

FAMILIES: dict[str, type[Model]] = {"elo_blend": EloBlend, "epa_blend": EpaBlend}


def get_family(name: str) -> type[Model]:
    try:
        return FAMILIES[name]
    except KeyError:
        raise ValueError(f"unknown model family: {name!r}") from None
