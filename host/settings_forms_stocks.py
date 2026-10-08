"""The step 9 settings group "stocks" (docs/ALPACA.md "Step 9", contract section 8),
kept apart from host/settings_forms.py, which registers it: the daily bar feed, the
decision and polling cadence, the backtest cost, the money limits of stock orders, the
search and validation years and the two stock thresholds objects. Dollars in, cents
out for every money field (as the Limits group); a blank last year means "the last
complete year" (null). The symbols are one text field, split on commas and spaces and
upper-cased. The validators are host/settings_schema_stocks.py.
"""
from __future__ import annotations

import re
from typing import Any

from host.errors import BadRequest
from host.money import cents_to_dollars, dollars_to_cents

LABELS = {
    "stock_symbols": "Symbols",
    "stock_history_start": "History start",
    "stock_history_feed": "History feed",
    "stock_bars_hour": "Bars refresh hour",
    "stock_decision_lead_min": "Decision lead (min)",
    "stock_trade_tick_s": "Stock trade tick (s)",
    "stock_broker_poll_s": "Broker poll (s)",
    "stock_orders_poll_s": "Orders poll (s)",
    "stock_max_assignments": "Max stock assignments",
    "stock_cost_bps": "Backtest cost (bps)",
    "stock_price_band": "Price band",
    "stock_max_order": "Max order",
    "stock_max_position": "Max position",
    "stock_default_bankroll": "Default stock bankroll",
    "stock_max_daily_loss_paper": "Max daily loss, paper stocks",
    "stock_max_daily_loss_live": "Max daily loss, live stocks",
    "stock_backtest_first": "Search first year",
    "stock_backtest_last": "Search last year",
    "stock_validation_first": "Validation first year",
    "stock_validation_last": "Validation last year",
    "stock_bt_min_sharpe": "Min Sharpe",
    "stock_bt_max_drawdown": "Max drawdown (backtest)",
    "stock_bt_min_trades": "Min trades",
    "stock_bt_min_validation_sharpe": "Min validation Sharpe",
    "stock_paper_min_days": "Min paper days",
    "stock_paper_min_return": "Min paper return",
    "stock_paper_max_drawdown": "Max paper drawdown",
}
INTS = (
    "stock_bars_hour", "stock_decision_lead_min", "stock_trade_tick_s", "stock_broker_poll_s", "stock_orders_poll_s",
    "stock_max_assignments",
)
NUMBERS = ("stock_cost_bps", "stock_price_band")
MONEY = {"stock_max_order": "stock_max_order_cents", "stock_max_position": "stock_max_position_cents",
         "stock_default_bankroll": "stock_default_bankroll_cents"}
BACKTEST = {"stock_bt_min_sharpe": "min_sharpe", "stock_bt_max_drawdown": "max_drawdown",
            "stock_bt_min_trades": "min_trades", "stock_bt_min_validation_sharpe": "min_validation_sharpe"}
PAPER = {"stock_paper_min_days": "min_days", "stock_paper_min_return": "min_return",
         "stock_paper_max_drawdown": "max_drawdown"}
WHOLE = ("min_trades", "min_days")  # thresholds keys stored as integers
FEEDS = ("sip", "iex")
LEAD_RANGE, TICK_RANGE = (12, 120), (5, 300)  # host/settings_schema_stocks.py
CUTOFF_MIN = 11  # host.stocks.market.MOC_CUTOFF: no decision in the last 11 minutes
SPLIT = re.compile(r"[\s,]+")


def _text(form: dict[str, str], name: str) -> str:
    return (form.get(name) or "").strip()


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


def _years(form: dict[str, str], prefix: str) -> list[int | None]:
    """[first, last] with a blank last year as null."""
    first = _int(form, f"{prefix}_first")
    last = _int(form, f"{prefix}_last") if _text(form, f"{prefix}_last") else None
    return [first, last]


def _symbols(form: dict[str, str]) -> list[str]:
    return [s.upper() for s in SPLIT.split(_text(form, "stock_symbols")) if s]


def _check_window(lead_min: int, tick_s: int) -> None:
    """A decision is due only from close - lead until the order cutoff (close - 11
    minutes, host.stocks.market.MOC_CUTOFF), and the worker looks once per tick: a
    window shorter than two ticks would skip sessions without a word. Out-of-range values
    are left to the validators."""
    if not (LEAD_RANGE[0] <= lead_min <= LEAD_RANGE[1] and TICK_RANGE[0] <= tick_s <= TICK_RANGE[1]):
        return
    if (lead_min - CUTOFF_MIN) * 60 < 2 * tick_s:
        raise BadRequest(f"the decision window (Decision lead minus {CUTOFF_MIN} minutes, here"
                         f" {(lead_min - CUTOFF_MIN) * 60} s) must cover at least two stock trade ticks"
                         f" ({2 * tick_s} s): raise the lead or shorten the tick")


def parse_stocks(form: dict[str, str]) -> dict[str, Any]:
    """The settings update of the Stocks group."""
    updates: dict[str, Any] = {name: _int(form, name) for name in INTS}
    _check_window(updates["stock_decision_lead_min"], updates["stock_trade_tick_s"])
    updates.update({name: _number(form, name) for name in NUMBERS})
    updates.update({key: dollars_to_cents(_text(form, name), LABELS[name]) for name, key in MONEY.items()})
    updates["stock_max_daily_loss_cents"] = {
        mode: dollars_to_cents(_text(form, f"stock_max_daily_loss_{mode}"), LABELS[f"stock_max_daily_loss_{mode}"])
        for mode in ("paper", "live")
    }
    updates["stocks_enabled"] = _text(form, "stocks_enabled").lower() in {"1", "true", "on", "yes"}
    updates["stock_symbols"] = _symbols(form)
    updates["stock_history_start"] = _text(form, "stock_history_start")
    updates["stock_history_feed"] = _text(form, "stock_history_feed")
    updates["stock_backtest_years"] = _years(form, "stock_backtest")
    updates["stock_validation_years"] = _years(form, "stock_validation")
    updates["thresholds_stock_backtest"] = {
        key: (_int(form, name) if key in WHOLE else _number(form, name)) for name, key in BACKTEST.items()
    }
    updates["thresholds_stock_paper"] = {
        key: (_int(form, name) if key in WHOLE else _number(form, name)) for name, key in PAPER.items()
    }
    return updates


PARSERS = {"stocks": parse_stocks}


def _shown(value: Any) -> str:
    return "" if value is None else str(value)


def _pair(value: Any) -> list[Any]:
    return (list(value) + [None, None])[:2] if isinstance(value, list) else [None, None]


def _dict(settings: dict[str, Any], key: str) -> dict[str, Any]:
    value = settings.get(key)
    return value if isinstance(value, dict) else {}


def form_values(settings: dict[str, Any]) -> dict[str, str]:
    """The strings the Stocks group's inputs show for the stored values."""
    loss = _dict(settings, "stock_max_daily_loss_cents")
    backtest, paper = _dict(settings, "thresholds_stock_backtest"), _dict(settings, "thresholds_stock_paper")
    search, validation = _pair(settings.get("stock_backtest_years")), _pair(settings.get("stock_validation_years"))
    symbols = settings.get("stock_symbols") if isinstance(settings.get("stock_symbols"), list) else []
    return {
        **{name: _shown(settings.get(name)) for name in INTS + NUMBERS},
        **{name: cents_to_dollars(settings.get(key)) for name, key in MONEY.items()},
        **{f"stock_max_daily_loss_{mode}": cents_to_dollars(loss.get(mode)) for mode in ("paper", "live")},
        "stocks_enabled": "true" if settings.get("stocks_enabled") is True else "",
        "stock_symbols": ", ".join(str(s) for s in symbols),
        "stock_history_start": _shown(settings.get("stock_history_start")),
        "stock_history_feed": str(settings.get("stock_history_feed") or FEEDS[0]),
        "stock_backtest_first": _shown(search[0]), "stock_backtest_last": _shown(search[1]),
        "stock_validation_first": _shown(validation[0]), "stock_validation_last": _shown(validation[1]),
        **{name: _shown(backtest.get(key)) for name, key in BACKTEST.items()},
        **{name: _shown(paper.get(key)) for name, key in PAPER.items()},
    }
