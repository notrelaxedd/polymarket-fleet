"""The second and third summary sentences every family shares (docs/MODELS.md, "Summary")."""

from __future__ import annotations

from typing import Any

FEW_BETS = 50  # below the leaderboard's ranking gate the summary says so
MARKET_BEATEN_P = 0.05  # the market test level at which the summary says "beats the closing line"


def num(metrics: dict[str, Any], key: str) -> float:
    value = metrics.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def span_text(metrics: dict[str, Any]) -> str:
    seasons = metrics.get("seasons") or []
    if not seasons:
        return "no seasons"
    if seasons[0] == seasons[-1]:
        return str(seasons[0])
    return f"{seasons[0]}-{seasons[-1]}"


def bets_sentence(params: dict[str, Any], metrics: dict[str, Any]) -> str:
    """How it bet in the backtest."""
    span = span_text(metrics)
    n_bets = int(num(metrics, "n_bets"))
    if n_bets == 0:
        min_edge = 100 * float(params.get("min_edge", 0.0))
        return (f"Across {span} it never found an edge above its {min_edge:.1f}% minimum after fees, "
                "so it placed no bets.")
    note = " (too few bets to judge)" if n_bets < FEW_BETS else ""
    drawdown = metrics.get("max_drawdown")
    dd_text = "an unknown" if drawdown is None else f"a {100 * num(metrics, 'max_drawdown'):.0f}%"
    return (f"Across {span} it placed {n_bets} bets at an average edge of "
            f"{100 * num(metrics, 'avg_edge'):.1f}% and returned {100 * num(metrics, 'roi'):+.1f}% on stake "
            f"with {dd_text} max drawdown{note}.")


def calibration_sentence(metrics: dict[str, Any]) -> str:
    """Log-loss against the market and the plain verdict."""
    ll, mll = num(metrics, "log_loss"), num(metrics, "market_log_loss")
    market_p = metrics.get("market_p")
    market_p = float(market_p) if isinstance(market_p, (int, float)) and not isinstance(market_p, bool) else None
    if metrics.get("n_games", 0) and ll < mll - 0.002 and (market_p is None or market_p < MARKET_BEATEN_P):
        verdict = "it beats the closing line on calibration, but treat the edge as unproven"
    elif metrics.get("n_games", 0) and ll < mll - 0.002:
        verdict = (f"it is ahead of the closing line but not significantly (market test p {market_p:.2f}), "
                   "so treat the edge as unproven")
    else:
        verdict = "it leans on the market and adds little, so treat the edge as unproven"
    return (f"Log-loss {ll:.3f} against the market's {mll:.3f}; {verdict} until paper trading "
            f"shows positive CLV.")
