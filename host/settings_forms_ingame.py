"""The step 6 Part C settings group "ingame" (docs/INGAME.md), kept apart from
host/settings_forms.py, which registers it: the in-game trade rules, the lag
suspension and the game-state feed. Dollars in, cents out for the in-game max bet;
one checkbox per game-state source (unticked sends nothing and leaves the source out);
`trade_ingame` is the default of new assignments' in-game switch. The validators are
host/settings_schema_ingame.py.
"""
from __future__ import annotations

from typing import Any

from host.errors import BadRequest
from host.money import cents_to_dollars, dollars_to_cents

LABELS = {
    "ingame_tick_s": "In-game tick (s)",
    "ingame_max_state_age_s": "Max game-state age (s)",
    "ingame_quiet_seconds": "Quiet seconds after a score or possession change",
    "ingame_cutoff_seconds": "Cutoff (game seconds left)",
    "ingame_gtd_seconds": "In-game order lifetime (s)",
    "ingame_lag_min_events": "Lag: min measured events",
    "yahoo_poll_s": "Yahoo poll (s)",
    "ingame_dead_zone": "Dead zone",
    "ingame_min_edge": "In-game min edge",
    "ingame_max_lag_s": "Max feed lag (s)",
    "gamestate_poll_s": "Game-state poll (s)",
    "gamestate_max_rps": "ESPN max requests per second",
    "ingame_max_bet": "In-game max bet",
    "espn_summary_url": "ESPN summary URL",
    "yahoo_pbp_url": "Yahoo play-by-play URL",
}
INTS = (
    "ingame_tick_s", "ingame_max_state_age_s", "ingame_quiet_seconds", "ingame_cutoff_seconds", "ingame_gtd_seconds",
    "ingame_lag_min_events", "yahoo_poll_s",
)
NUMBERS = ("ingame_dead_zone", "ingame_min_edge", "ingame_max_lag_s", "gamestate_poll_s", "gamestate_max_rps")
TEXTS = ("espn_summary_url", "yahoo_pbp_url")
SOURCES = ("espn", "yahoo")


def _text(form: dict[str, str], name: str) -> str:
    return (form.get(name) or "").strip()


def _checked(form: dict[str, str], name: str) -> bool:
    return _text(form, name).lower() in {"1", "true", "on", "yes"}


def _int(form: dict[str, str], name: str) -> int:
    try:
        return int(_text(form, name))
    except ValueError:
        raise BadRequest(f"{LABELS[name]} must be a whole number") from None


def _number(form: dict[str, str], name: str) -> int | float:
    """A whole number stays an int (as the seeds store it), anything else a float."""
    text = _text(form, name)
    try:
        return int(text)
    except ValueError:
        pass
    try:
        value = float(text)
    except ValueError:
        raise BadRequest(f"{LABELS[name]} must be a number") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise BadRequest(f"{LABELS[name]} must be a number")
    return value


def parse_ingame(form: dict[str, str]) -> dict[str, Any]:
    """The settings update of the In-game group."""
    updates: dict[str, Any] = {name: _int(form, name) for name in INTS}
    updates.update({name: _number(form, name) for name in NUMBERS})
    updates.update({name: _text(form, name) for name in TEXTS})
    updates["ingame_max_bet_cents"] = dollars_to_cents(_text(form, "ingame_max_bet"), LABELS["ingame_max_bet"])
    updates["gamestate_sources"] = [source for source in SOURCES if _checked(form, f"gamestate_{source}")]
    updates["trade_ingame"] = _checked(form, "trade_ingame")
    return updates


PARSERS = {"ingame": parse_ingame}


def _shown(value: Any) -> str:
    return "" if value is None else str(value)


def form_values(settings: dict[str, Any]) -> dict[str, str]:
    """The strings the In-game group's inputs show for the stored values."""
    sources = settings.get("gamestate_sources") if isinstance(settings.get("gamestate_sources"), list) else []
    return {
        **{name: _shown(settings.get(name)) for name in INTS + NUMBERS},
        **{name: str(settings.get(name) or "") for name in TEXTS},
        "ingame_max_bet": cents_to_dollars(settings.get("ingame_max_bet_cents")),
        **{f"gamestate_{source}": "true" if source in sources else "" for source in SOURCES},
        "trade_ingame": "true" if settings.get("trade_ingame") is True else "",
    }
