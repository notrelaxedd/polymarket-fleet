"""The step 6 Part B settings groups (docs/ROBUSTNESS.md B1 and B2), kept apart from
host/settings_forms.py, which registers them.

- "replay": snapshot replay backtests, `decision_minutes_before_kickoff` (whole
  minutes, 0..300) and the `allow_sim_prices` checkbox (unticked sends nothing and
  stores false).
- "signals": the nflverse injury and play-by-play URL templates (each holds
  {season}) and `signals_refresh_hours`.
"""
from __future__ import annotations

from typing import Any

from host.errors import BadRequest

LABELS = {
    "decision_minutes_before_kickoff": "Decision minutes before kickoff",
    "nflverse_injuries_url": "Injuries URL",
    "nflverse_pbp_url": "Play-by-play URL",
    "signals_refresh_hours": "Signals refresh every (hours)",
}
TEXT_KEYS = ("nflverse_injuries_url", "nflverse_pbp_url")


def _text(form: dict[str, str], name: str) -> str:
    return (form.get(name) or "").strip()


def _int(form: dict[str, str], name: str) -> int:
    try:
        return int(_text(form, name))
    except ValueError:
        raise BadRequest(f"{LABELS[name]} must be a whole number") from None


def parse_replay(form: dict[str, str]) -> dict[str, Any]:
    """The snapshot replay group."""
    return {
        "decision_minutes_before_kickoff": _int(form, "decision_minutes_before_kickoff"),
        "allow_sim_prices": _text(form, "allow_sim_prices").lower() in {"1", "true", "on", "yes"},
    }


def parse_signals(form: dict[str, str]) -> dict[str, Any]:
    """The nflverse signals group."""
    updates: dict[str, Any] = {name: _text(form, name) for name in TEXT_KEYS}
    updates["signals_refresh_hours"] = _int(form, "signals_refresh_hours")
    return updates


PARSERS = {"replay": parse_replay, "signals": parse_signals}


def form_values(settings: dict[str, Any]) -> dict[str, str]:
    """The strings the two groups' inputs show for the stored values."""
    minutes = settings.get("decision_minutes_before_kickoff")
    hours = settings.get("signals_refresh_hours")
    return {
        "decision_minutes_before_kickoff": "" if minutes is None else str(minutes),
        "allow_sim_prices": "true" if settings.get("allow_sim_prices") is True else "",
        **{name: str(settings.get(name) or "") for name in TEXT_KEYS},
        "signals_refresh_hours": "" if hours is None else str(hours),
    }
