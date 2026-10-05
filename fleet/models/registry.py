"""Model families by registry key."""

from __future__ import annotations

from fleet.models.base import Model
from fleet.models.elo_blend import EloBlend
from fleet.models.epa_blend import EpaBlend
from fleet.models.ingame_wp import IngameWP

# ingame_wp is the in-game family (docs/INGAME.md): it predicts from a game state, so
# only the in-game search (fleet.sim.ingame) and the in-game trade rules use it; the
# pre-game backtest, validate and train jobs refuse it (fleet.worker.jobs).
FAMILIES: dict[str, type[Model]] = {"elo_blend": EloBlend, "epa_blend": EpaBlend, "ingame_wp": IngameWP}
PREGAME_FAMILIES: tuple[str, ...] = ("elo_blend", "epa_blend")


def get_family(name: str) -> type[Model]:
    try:
        return FAMILIES[name]
    except KeyError:
        raise ValueError(f"unknown model family: {name!r}") from None
