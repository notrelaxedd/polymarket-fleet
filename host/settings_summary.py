"""One-line summaries of the Settings groups (docs/UI.md "Settings"): the muted line
in each group header, so the page reads at a glance with every group closed.

Display only. Built from the stored settings (not from a rejected form's values), so a
header always says what is in force. Every reader tolerates a missing or malformed
value and shows "-" for it rather than failing the page. host/settings_forms.py merges
these into the values the template gets as ``summary_<group>``.
"""
from __future__ import annotations

from typing import Any

from host.money import format_cents
from host.settings import FLAG_NAMES

SEP = " · "  # a middle dot between the parts of a summary
GROUPS = ("limits", "trading", "ingame", "robustness", "replay", "fleet", "data")


def _dict(settings: dict[str, Any], key: str) -> dict[str, Any]:
    value = settings.get(key)
    return value if isinstance(value, dict) else {}


def _money(cents: Any) -> str:
    return format_cents(cents) if isinstance(cents, int) and not isinstance(cents, bool) else "-"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _pct(value: Any) -> str:
    """0.03 -> "3.0%" (percentages to one decimal, docs/UI.md "Numbers")."""
    number = _number(value)
    return "-" if number is None else f"{number * 100:.1f}%"


def _plain(value: Any) -> str:
    """A stored number as a person reads it: 0.25 -> "0.25", 5.0 -> "5"."""
    number = _number(value)
    if number is None:
        return "-"
    return str(int(number)) if number == int(number) else f"{number:g}"


def _era(value: Any) -> str:
    """[2010, 2021] -> "2010-2021"; [2022, null] -> "from 2022" (the end follows the data)."""
    if not isinstance(value, list) or not value or value[0] is None:
        return "-"
    if len(value) > 1 and value[1] is not None:
        return f"{value[0]}-{value[1]}"
    return f"from {value[0]}"


def limits(settings: dict[str, Any]) -> str:
    """The money rules: the bet cap, the daily stop per mode, the betting rule."""
    loss = _dict(settings, "max_daily_loss_cents")
    games = settings.get("trade_max_games")
    return SEP.join(
        [
            f"max bet {_money(settings.get('max_bet_cents'))}",
            f"daily loss {_money(loss.get('paper'))} paper, {_money(loss.get('live'))} live",
            f"min edge {_pct(settings.get('min_edge'))}",
            f"Kelly {_plain(settings.get('kelly_fraction'))}",
            f"bankroll {_money(settings.get('default_bankroll_cents'))}",
            f"{_plain(games)} games per worker",
        ]
    )


def _exposure(settings: dict[str, Any]) -> str:
    caps = _dict(settings, "max_exposure_cents")
    parts = [f"{_money(caps.get(mode))} {mode}" for mode in ("paper", "live") if caps.get(mode)]
    return "exposure " + (", ".join(parts) if parts else "uncapped")


def trading(settings: dict[str, Any]) -> str:
    """Where prices come from and how orders are approved; then the paper gate."""
    paper = _dict(settings, "thresholds_paper")
    pregame = settings.get("trade_pregame_only") is True
    return SEP.join(
        [
            str(settings.get("market_source") or "-"),
            f"participation {_pct(settings.get('participation'))}",
            "pregame only" if pregame else "orders after kickoff allowed",
            _exposure(settings),
            f"paper gate {_plain(paper.get('min_games'))} games, {_plain(paper.get('min_bets'))} bets",
        ]
    )


def ingame(settings: dict[str, Any]) -> str:
    """The in-game rules (step 6 Part C): the default switch, the bet cap, the feed-lag
    suspension and the polled sources; in-game orders are paper only."""
    sources = settings.get("gamestate_sources") if isinstance(settings.get("gamestate_sources"), list) else []
    lag = _plain(settings.get("ingame_max_lag_s"))
    return SEP.join(
        [
            "on for new assignments" if settings.get("trade_ingame") is True else "off by default",
            f"max bet {_money(settings.get('ingame_max_bet_cents'))}",
            f"min edge {_pct(settings.get('ingame_min_edge'))}",
            f"lag limit {lag} s" if lag != "-" else "lag limit -",
            "feed " + (", ".join(str(s) for s in sources) if sources else "off"),
            "paper only",
        ]
    )


def robustness(settings: dict[str, Any]) -> str:
    """The backtest gate and the two eras it is judged on."""
    gate = _dict(settings, "thresholds_backtest")
    flags = gate.get("forbid_flags") if isinstance(gate.get("forbid_flags"), list) else []
    forbidden = [flag.replace("_", "-") for flag in FLAG_NAMES if flag in flags]
    return SEP.join(
        [
            f"{_plain(gate.get('min_bets'))} bets, ROI {_pct(gate.get('min_roi'))}+",
            "validation on" if gate.get("require_validation") is True else "validation off",
            "forbids " + ", ".join(forbidden) if forbidden else "no flag forbidden",
            f"search {_era(settings.get('backtest_seasons'))}",
            f"held out {_era(settings.get('validation_seasons'))}",
        ]
    )


def replay(settings: dict[str, Any]) -> str:
    """Snapshot replay backtests and the nflverse signals (step 6 Part B)."""
    return SEP.join(
        [
            f"decides {_plain(settings.get('decision_minutes_before_kickoff'))} min before kickoff",
            "sim prices allowed" if settings.get("allow_sim_prices") is True else "sim prices off",
            f"signals every {_plain(settings.get('signals_refresh_hours'))} h",
        ]
    )


def fleet(settings: dict[str, Any]) -> str:
    """Worker timing and the owner's time zone."""
    expiries = settings.get("max_expiries")
    return SEP.join(
        [
            f"lease {_plain(settings.get('lease_seconds'))} s",
            f"heartbeat {_plain(settings.get('heartbeat_seconds'))} s",
            f"online {_plain(settings.get('online_after_seconds'))} s",
            "retry forever" if expiries is None else f"{_plain(expiries)} expiries",
            str(settings.get("tz") or "-"),
        ]
    )


def data(settings: dict[str, Any]) -> str:
    """The games refresh cadence (the page adds the row count and the last refresh)."""
    return f"refresh every {_plain(settings.get('nflverse_refresh_hours'))} h"


READERS = {
    "limits": limits, "trading": trading, "ingame": ingame, "robustness": robustness, "replay": replay, "fleet": fleet,
    "data": data,
}


def summaries(settings: dict[str, Any]) -> dict[str, str]:
    """``summary_<group>`` -> its one-line summary, for every group in GROUPS."""
    return {f"summary_{group}": READERS[group](settings) for group in GROUPS}
