"""In-game approval (docs/INGAME.md "In-game trade rules", step 6C contract section 10).

A request with `ingame` true is dispatched here from host.trading.limits (buys) and
host.trading.sells (sells). It runs under the same lock and transaction as any other
approval and keeps every limit; only the pre-game `kickoff` rejection is replaced by
the in-game checks, in this order after `killed`, `lease`, `assignment`, `market`:

1. `ingame_disabled`: the assignment has trade_ingame off or no in-game model;
2. `ingame_paper_only`: a live assignment (in-game orders are paper-only in this step);
3. `ingame_stale`: no game state, a state older than ingame_max_state_age_s (exactly
   at the age still passes), a state that does not show the game in progress (status
   other than "in", period or clock missing) or a final game;
4. `ingame_quiet`: fewer than ingame_quiet_seconds since the last score or possession
   change (exactly at the quiet seconds passes);
5. `ingame_cutoff`: game seconds remaining at or below ingame_cutoff_seconds
   (regulation: (4 - period) * 900 + clock; overtime: the clock);
6. `ingame_lag_suspended` (buys only): host.exchange.feedlag.lag_status suspends;

then the usual checks (mode, stale book, liquidity, participation, price band,
max_bet also against ingame_max_bet_cents, bankroll, daily loss, exposure, buying
power for buys; the sell checks of host.trading.sells for sells). The order row is
stored with `ingame` true and its approval event carries the game state at approval
(`state_at_entry`); the executor gives it a GTD of ingame_gtd_seconds.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

import psycopg

from host.exchange.feedlag import lag_status
from host.exchange.gamestate import latest_state
from host.trading import limits, sells

__all__ = [
    "REASONS", "BUY_CHECKS", "SELL_CHECKS", "seconds_remaining", "entry_state", "state_problem", "prepare",
    "gtd_seconds",
]

REASONS = (
    "ingame_disabled", "ingame_paper_only", "ingame_stale", "ingame_quiet", "ingame_cutoff", "ingame_lag_suspended",
)
DEFAULTS: dict[str, float] = {
    "ingame_max_state_age_s": 30.0, "ingame_quiet_seconds": 20.0, "ingame_cutoff_seconds": 120.0,
    "ingame_max_bet_cents": 500.0, "ingame_gtd_seconds": 60.0,
}
REGULATION_PERIODS = 4
PERIOD_SECONDS = 900
ENTRY_KEYS = ("period", "clock_seconds", "home_score", "away_score", "possession")

Check = Callable[[psycopg.Connection, dict[str, Any]], bool]


def setting(settings: dict[str, Any], key: str) -> float:
    """A numeric in-game setting, its default when missing or unusable."""
    value = settings.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULTS[key]
    return float(value)


def gtd_seconds(settings: dict[str, Any]) -> int:
    """The GTD of an in-game order: settings.ingame_gtd_seconds (at least 1 s)."""
    return max(1, int(setting(settings, "ingame_gtd_seconds")))


def seconds_remaining(state: dict[str, Any]) -> float | None:
    """Game seconds left: regulation (4 - period) * 900 + clock, overtime the clock;
    None when the period or the clock is unknown."""
    period, clock = state.get("period"), state.get("clock_seconds")
    if period is None or clock is None:
        return None
    if int(period) > REGULATION_PERIODS:
        return float(clock)
    return float((REGULATION_PERIODS - int(period)) * PERIOD_SECONDS + int(clock))


def entry_state(info: dict[str, Any] | None) -> dict[str, Any] | None:
    """The game state stored with an in-game order: {period, clock_seconds, home_score,
    away_score, possession} (bets.state_at_entry)."""
    if info is None:
        return None
    state = info.get("state") or {}
    return {k: state.get(k) for k in ENTRY_KEYS}


def state_problem(info: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> str | None:
    """The first of ingame_stale, ingame_quiet, ingame_cutoff that a latest_state()
    result fails, else None. Pure: the boundaries live here."""
    if info is None:
        return "ingame_stale"
    state = info.get("state") or {}
    remaining = seconds_remaining(state)
    age = info.get("age_s")
    if state.get("status") != "in" or remaining is None or age is None:
        return "ingame_stale"
    if float(age) > setting(settings, "ingame_max_state_age_s"):
        return "ingame_stale"
    change = info.get("last_change") or {}
    if change.get("ts") is not None:
        if (now - change["ts"]).total_seconds() < setting(settings, "ingame_quiet_seconds"):
            return "ingame_quiet"
    if remaining <= setting(settings, "ingame_cutoff_seconds"):
        return "ingame_cutoff"
    return None


def prepare(conn: psycopg.Connection, ctx: dict[str, Any]) -> None:
    """Add the in-game inputs to an approval context: the latest game state (read at
    the approval's clock) and the extra max-bet cap."""
    game = ctx.get("game")
    ctx["game_state"] = latest_state(conn, game["game_id"], ctx["now"]) if game is not None else None
    ctx["extra_max_bet_cents"] = int(setting(ctx["settings"], "ingame_max_bet_cents"))


def _check_disabled(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    a = ctx["assignment"]
    return not a.get("trade_ingame") or a.get("ingame_model_id") is None


def _check_paper_only(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    return ctx["mode"] == "live"


def _problem(ctx: dict[str, Any]) -> str | None:
    if "ingame_problem" not in ctx:
        game = ctx.get("game")
        if game is None or game.get("status") == "final":
            ctx["ingame_problem"] = "ingame_stale"
        else:
            ctx["ingame_problem"] = state_problem(ctx.get("game_state"), ctx["settings"], ctx["now"])
    return ctx["ingame_problem"]


def _check_stale(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    return _problem(ctx) == "ingame_stale"


def _check_quiet(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    return _problem(ctx) == "ingame_quiet"


def _check_cutoff(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    return _problem(ctx) == "ingame_cutoff"


def _check_lag(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    return bool(lag_status(conn)["suspended"])


HEAD: tuple[tuple[str, Check], ...] = (
    ("killed", limits._check_killed), ("lease", limits._check_lease), ("assignment", limits._check_assignment),
    ("market", limits._check_market), ("ingame_disabled", _check_disabled), ("ingame_paper_only", _check_paper_only),
    ("ingame_stale", _check_stale), ("ingame_quiet", _check_quiet), ("ingame_cutoff", _check_cutoff),
)


def _after_kickoff(checks: tuple[tuple[str, Check], ...]) -> tuple[tuple[str, Check], ...]:
    """The checks that follow `kickoff` in a pre-game list (mode onwards)."""
    names = [name for name, _ in checks]
    return checks[names.index("kickoff") + 1:]


BUY_CHECKS: tuple[tuple[str, Check], ...] = HEAD + (("ingame_lag_suspended", _check_lag),) + _after_kickoff(limits.CHECKS)
SELL_CHECKS: tuple[tuple[str, Check], ...] = HEAD + _after_kickoff(sells.SELL_CHECKS)
