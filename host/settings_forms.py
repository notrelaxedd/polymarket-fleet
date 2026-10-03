"""The settings page forms: one group per form, dollars in, cents out.

Form field names differ from setting keys where the unit differs (``max_bet`` in
dollars becomes ``max_bet_cents``). ``form_values`` turns stored settings into the
strings the inputs show; ``parse_group`` turns a posted form back into a settings
update that host.settings.set_settings validates.
"""
from __future__ import annotations

from typing import Any, Callable

from host.errors import BadRequest
from host.money import cents_to_dollars, dollars_to_cents

GROUPS = ("trading", "fleet", "tz")

LABELS = {
    "max_bet": "Max bet",
    "max_daily_loss_paper": "Max daily loss (paper)",
    "max_daily_loss_live": "Max daily loss (live)",
    "default_bankroll": "Default bankroll per game",
    "liquidity_floor": "Liquidity floor",
    "min_edge": "Min edge",
    "kelly_fraction": "Kelly fraction",
    "trade_max_games": "Max games per trade worker",
    "lease_seconds": "Lease seconds",
    "heartbeat_seconds": "Heartbeat seconds",
    "online_after_seconds": "Online-after seconds",
    "max_expiries": "Max lease expiries",
    "tz": "Time zone",
}


def _text(form: dict[str, str], name: str) -> str:
    return (form.get(name) or "").strip()


def _int(form: dict[str, str], name: str, nullable: bool = False) -> int | None:
    text = _text(form, name)
    if not text and nullable:
        return None
    try:
        return int(text)
    except ValueError:
        raise BadRequest(f"{LABELS[name]} must be a whole number") from None


def _number(form: dict[str, str], name: str) -> float:
    text = _text(form, name)
    try:
        return float(text)
    except ValueError:
        raise BadRequest(f"{LABELS[name]} must be a number") from None


def _dollars(form: dict[str, str], name: str) -> int:
    return dollars_to_cents(_text(form, name), LABELS[name])


def _parse_trading(form: dict[str, str]) -> dict[str, Any]:
    return {
        "max_bet_cents": _dollars(form, "max_bet"),
        "max_daily_loss_cents": {
            "paper": _dollars(form, "max_daily_loss_paper"),
            "live": _dollars(form, "max_daily_loss_live"),
        },
        "default_bankroll_cents": _dollars(form, "default_bankroll"),
        "liquidity_floor_cents": _dollars(form, "liquidity_floor"),
        "min_edge": _number(form, "min_edge"),
        "kelly_fraction": _number(form, "kelly_fraction"),
        "trade_max_games": _int(form, "trade_max_games"),
    }


def _parse_fleet(form: dict[str, str]) -> dict[str, Any]:
    return {
        "lease_seconds": _int(form, "lease_seconds"),
        "heartbeat_seconds": _int(form, "heartbeat_seconds"),
        "online_after_seconds": _int(form, "online_after_seconds"),
        "max_expiries": _int(form, "max_expiries", nullable=True),
    }


def _parse_tz(form: dict[str, str]) -> dict[str, Any]:
    return {"tz": _text(form, "tz")}


PARSERS: dict[str, Callable[[dict[str, str]], dict[str, Any]]] = {
    "trading": _parse_trading,
    "fleet": _parse_fleet,
    "tz": _parse_tz,
}


def parse_group(group: str, form: dict[str, str]) -> dict[str, Any]:
    """The settings update a posted group form stands for; 400 on an unknown group or bad input."""
    parser = PARSERS.get(group)
    if parser is None:
        raise BadRequest(f"unknown settings group: {group!r}")
    return parser(form)


def form_values(settings: dict[str, Any]) -> dict[str, str]:
    """The strings each settings input shows for the stored values."""
    loss = settings.get("max_daily_loss_cents") or {}
    if not isinstance(loss, dict):
        loss = {}
    max_expiries = settings.get("max_expiries")
    return {
        "max_bet": cents_to_dollars(settings.get("max_bet_cents")),
        "max_daily_loss_paper": cents_to_dollars(loss.get("paper")),
        "max_daily_loss_live": cents_to_dollars(loss.get("live")),
        "default_bankroll": cents_to_dollars(settings.get("default_bankroll_cents")),
        "liquidity_floor": cents_to_dollars(settings.get("liquidity_floor_cents")),
        "min_edge": str(settings.get("min_edge", "")),
        "kelly_fraction": str(settings.get("kelly_fraction", "")),
        "trade_max_games": str(settings.get("trade_max_games", "")),
        "lease_seconds": str(settings.get("lease_seconds", "")),
        "heartbeat_seconds": str(settings.get("heartbeat_seconds", "")),
        "online_after_seconds": str(settings.get("online_after_seconds", "")),
        "max_expiries": "" if max_expiries is None else str(max_expiries),
        "tz": str(settings.get("tz", "")),
    }
