"""The settings page forms: one group per form, dollars in, cents out.

Form field names differ from setting keys where the unit differs (``max_bet`` in
dollars becomes ``max_bet_cents``). ``form_values`` turns stored settings into the
strings the inputs show; ``parse_group`` turns a posted form back into a settings
update that host.settings.set_settings validates.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from host.errors import BadRequest
from host.money import cents_to_dollars, dollars_to_cents

GROUPS = ("trading", "fleet", "tz", "fees", "thresholds", "seasons", "nflverse", "trade")
# Keys no settings group may write: the kill switch moves through /kill and RESUME, the
# live switch through the typed phrase of the "Live trading" group (step 5).
GUARDED_KEYS = ("kill_switch", "live_enabled")
MARKET_SOURCES = ("sim", "polymarket_us", "polymarket_clob")
RATE_KEYS = ("orders_per_s", "cancels_per_s", "market_data_per_s", "account_per_s")
TRADE_INTS = (
    "book_max_age_s", "orphan_cancel_after_s", "gtd_seconds", "trade_tick_s", "market_lookahead_days",
    "max_paper_models_per_game", "snapshot_active_s", "snapshot_idle_s", "snapshot_retention_days",
)

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
    "taker_rate": "Taker fee rate",
    "half_spread": "Half spread",
    "min_bets": "Min bets",
    "min_roi": "Min ROI",
    "max_drawdown": "Max drawdown",
    "seasons_first": "First season",
    "seasons_last": "Last season",
    "nflverse_refresh_hours": "Refresh every (hours)",
    "nflverse_url": "games.csv URL",
    "participation": "Participation",
    "book_max_age_s": "Book max age (s)",
    "orphan_cancel_after_s": "Orphan cancel after (s)",
    "gtd_seconds": "Order lifetime (s)",
    "trade_tick_s": "Trade tick (s)",
    "market_lookahead_days": "Market lookahead (days)",
    "max_paper_models_per_game": "Max paper models per game",
    "snapshot_active_s": "Snapshot cadence, active (s)",
    "snapshot_idle_s": "Snapshot cadence, idle (s)",
    "snapshot_retention_days": "Snapshot retention (days)",
    "market_source": "Market source",
    "market_source_config": "Market source config",
    "scores_url": "Scores URL",
    "paper_min_games": "Paper min games",
    "paper_min_bets": "Paper min bets",
    "paper_min_days": "Paper min days",
    "paper_min_clv": "Paper min CLV",
    "paper_min_pnl": "Paper min P&L",
    "max_exposure_paper": "Max exposure (paper)",
    "max_exposure_live": "Max exposure (live)",
    "orders_per_s": "Orders per second",
    "cancels_per_s": "Cancels per second",
    "market_data_per_s": "Market data per second",
    "account_per_s": "Account calls per second",
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


def _parse_fees(form: dict[str, str]) -> dict[str, Any]:
    return {"fee_model": {"taker_rate": _number(form, "taker_rate"), "half_spread": _number(form, "half_spread")}}


def _parse_thresholds(form: dict[str, str]) -> dict[str, Any]:
    return {
        "thresholds_backtest": {
            "min_bets": _int(form, "min_bets"),
            "min_roi": _number(form, "min_roi"),
            "max_drawdown": _number(form, "max_drawdown"),
        }
    }


def _parse_seasons(form: dict[str, str]) -> dict[str, Any]:
    return {"backtest_seasons": [_int(form, "seasons_first"), _int(form, "seasons_last", nullable=True)]}


def _parse_nflverse(form: dict[str, str]) -> dict[str, Any]:
    return {"nflverse_refresh_hours": _int(form, "nflverse_refresh_hours"), "nflverse_url": _text(form, "nflverse_url")}


def _json_object(form: dict[str, str], name: str) -> dict[str, Any]:
    text = _text(form, name) or "{}"
    try:
        value = json.loads(text)
    except ValueError:
        raise BadRequest(f"{LABELS[name]} must be a JSON object") from None
    if not isinstance(value, dict):
        raise BadRequest(f"{LABELS[name]} must be a JSON object")
    return value


def _parse_trade(form: dict[str, str]) -> dict[str, Any]:
    """The step 4 trading group: approval, snapshots, market source, paper thresholds,
    exposure and rate limits. The checkbox sends nothing when unticked."""
    updates: dict[str, Any] = {name: _int(form, name) for name in TRADE_INTS}
    updates.update(
        {
            "participation": _number(form, "participation"),
            "trade_pregame_only": _text(form, "trade_pregame_only").lower() in {"1", "true", "on", "yes"},
            "market_source": _text(form, "market_source"),
            "market_source_config": _json_object(form, "market_source_config"),
            "scores_url": _text(form, "scores_url"),
            "thresholds_paper": {
                "min_games": _int(form, "paper_min_games"),
                "min_bets": _int(form, "paper_min_bets"),
                "min_days": _int(form, "paper_min_days"),
                "min_clv": _number(form, "paper_min_clv"),
                "min_pnl_cents": _dollars(form, "paper_min_pnl"),
            },
            "max_exposure_cents": {"paper": _dollars(form, "max_exposure_paper"), "live": _dollars(form, "max_exposure_live")},
            "rate_limits": {name: _number(form, name) for name in RATE_KEYS},
        }
    )
    return updates


PARSERS: dict[str, Callable[[dict[str, str]], dict[str, Any]]] = {
    "trading": _parse_trading,
    "fleet": _parse_fleet,
    "tz": _parse_tz,
    "fees": _parse_fees,
    "thresholds": _parse_thresholds,
    "seasons": _parse_seasons,
    "nflverse": _parse_nflverse,
    "trade": _parse_trade,
}


def parse_group(group: str, form: dict[str, str]) -> dict[str, Any]:
    """The settings update a posted group form stands for; 400 on an unknown group or bad input."""
    if group == "live":
        raise BadRequest("live trading is switched with the typed phrase in the Live trading group, not saved as a setting")
    parser = PARSERS.get(group)
    if parser is None:
        raise BadRequest(f"unknown settings group: {group!r}")
    updates = parser(form)
    assert not set(updates) & set(GUARDED_KEYS), "a settings group must never carry a guarded key"
    return updates


def form_values(settings: dict[str, Any]) -> dict[str, str]:
    """The strings each settings input shows for the stored values."""
    loss = settings.get("max_daily_loss_cents") or {}
    if not isinstance(loss, dict):
        loss = {}
    max_expiries = settings.get("max_expiries")
    fee = settings.get("fee_model") if isinstance(settings.get("fee_model"), dict) else {}
    thresholds = settings.get("thresholds_backtest") if isinstance(settings.get("thresholds_backtest"), dict) else {}
    seasons = settings.get("backtest_seasons") if isinstance(settings.get("backtest_seasons"), list) else [None, None]
    seasons = (list(seasons) + [None, None])[:2]
    paper = settings.get("thresholds_paper") if isinstance(settings.get("thresholds_paper"), dict) else {}
    exposure = settings.get("max_exposure_cents") if isinstance(settings.get("max_exposure_cents"), dict) else {}
    rates = settings.get("rate_limits") if isinstance(settings.get("rate_limits"), dict) else {}
    config = settings.get("market_source_config")
    trade = {name: _shown(settings.get(name)) for name in TRADE_INTS}
    trade.update({name: _shown(rates.get(name)) for name in RATE_KEYS})
    trade.update(
        {
            "participation": _shown(settings.get("participation")),
            "trade_pregame_only": "true" if settings.get("trade_pregame_only") is True else "",
            "market_source": str(settings.get("market_source") or MARKET_SOURCES[0]),
            "market_source_config": json.dumps(config, indent=2, sort_keys=True) if isinstance(config, dict) else "{}",
            "scores_url": str(settings.get("scores_url", "")),
            "paper_min_games": _shown(paper.get("min_games")),
            "paper_min_bets": _shown(paper.get("min_bets")),
            "paper_min_days": _shown(paper.get("min_days")),
            "paper_min_clv": _shown(paper.get("min_clv")),
            "paper_min_pnl": cents_to_dollars(paper.get("min_pnl_cents")),
            "max_exposure_paper": cents_to_dollars(exposure.get("paper")),
            "max_exposure_live": cents_to_dollars(exposure.get("live")),
        }
    )
    return {
        **trade,
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
        "taker_rate": _shown(fee.get("taker_rate")),
        "half_spread": _shown(fee.get("half_spread")),
        "min_bets": _shown(thresholds.get("min_bets")),
        "min_roi": _shown(thresholds.get("min_roi")),
        "max_drawdown": _shown(thresholds.get("max_drawdown")),
        "seasons_first": _shown(seasons[0]),
        "seasons_last": _shown(seasons[1]),
        "nflverse_refresh_hours": _shown(settings.get("nflverse_refresh_hours")),
        "nflverse_url": str(settings.get("nflverse_url", "")),
    }


def _shown(value: Any) -> str:
    """A stored number as the input shows it (blank for null)."""
    return "" if value is None else str(value)
