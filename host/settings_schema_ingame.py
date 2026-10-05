"""Validators of the in-game and game-state settings (docs/INGAME.md, contract
sections 0, 6 and 9), merged into host.settings_schema.SCHEMA.

- gamestate_poll_s: seconds between two polls of one live game, 3..5;
- gamestate_max_rps: the cap on ESPN requests per second, above 0 and at most 10;
- gamestate_sources: a list of distinct sources out of "espn" and "yahoo";
- espn_summary_url: an http(s) URL template holding {event_id};
- yahoo_pbp_url: empty (Yahoo off) or an http(s) URL template holding {event_id};
- yahoo_poll_s: 5..120;
- trade_ingame: the default of new assignments' in-game switch;
- ingame_*: the in-game trade rules and the lag suspension (ranges below).
"""
from __future__ import annotations

import re
from typing import Any, Callable

from host.money import MAX_CENTS

Validator = Callable[[Any], str | None]
GAMESTATE_SOURCES = ("espn", "yahoo")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return _is_int(value) or (isinstance(value, float) and value == value and value not in (float("inf"), float("-inf")))


def _bool(value: Any) -> str | None:
    return None if isinstance(value, bool) else "must be true or false"


def int_range(low: int, high: int) -> Validator:
    def check(value: Any) -> str | None:
        if not _is_int(value):
            return "must be an integer"
        return None if low <= value <= high else f"must be between {low} and {high}"

    return check


def number_range(low: float, high: float, low_open: bool = False) -> Validator:
    """A number in [low, high], or (low, high] with `low_open`."""

    def check(value: Any) -> str | None:
        if not _is_number(value):
            return "must be a number"
        if (value <= low if low_open else value < low) or value > high:
            return f"must be above {low} and at most {high}" if low_open else f"must be between {low} and {high}"
        return None

    return check


def _template_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 512 or not re.match(r"^https?://[^\s]+$", value):
        return "must be an http(s) URL"
    return None if "{event_id}" in value else "must contain {event_id}, such as ...summary?event={event_id}"


def _optional_template_url(value: Any) -> str | None:
    """Empty (the source is off) or an http(s) URL template holding {event_id}."""
    if value == "":
        return None
    error = _template_url(value)
    return None if error is None else "must be empty or " + error.removeprefix("must ")


def _sources(value: Any) -> str | None:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        return "must be a list of sources out of " + ", ".join(GAMESTATE_SOURCES)
    unknown = sorted(set(value) - set(GAMESTATE_SOURCES))
    if unknown:
        return "holds unknown sources: " + ", ".join(unknown) + " (known: " + ", ".join(GAMESTATE_SOURCES) + ")"
    if len(set(value)) != len(value):
        return "must not repeat a source"
    return None


INGAME_SCHEMA: dict[str, Validator] = {
    "trade_ingame": _bool,
    "ingame_tick_s": int_range(1, 60),
    "ingame_max_state_age_s": int_range(5, 300),
    "ingame_quiet_seconds": int_range(0, 300),
    "ingame_cutoff_seconds": int_range(0, 900),
    "ingame_dead_zone": number_range(0, 0.5),
    "ingame_min_edge": number_range(0, 0.5),
    "ingame_max_bet_cents": int_range(0, MAX_CENTS),
    "ingame_gtd_seconds": int_range(10, 3600),
    "ingame_max_lag_s": number_range(0, 600, low_open=True),
    "ingame_lag_min_events": int_range(1, 100),
    "gamestate_poll_s": number_range(3, 5),
    "gamestate_max_rps": number_range(0, 10, low_open=True),
    "gamestate_sources": _sources,
    "espn_summary_url": _template_url,
    "yahoo_pbp_url": _optional_template_url,
    "yahoo_poll_s": int_range(5, 120),
}
