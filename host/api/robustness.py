"""The "Robustness" section of the model page and of a validate job's result
(docs/ROBUSTNESS.md A2 and A3): the validation metrics and the stress table turned
into the lines and rows the template shows, with a one-line meaning per flag."""
from __future__ import annotations

from typing import Any

from host.eligibility import model_flags
from host.leaderboard import FLAG_MEANINGS, MARKET_BEATEN_P, validation_summary

REGIME_PAIRS = (
    ("favourite", "underdog"), ("home", "away"), ("divisional", "non_divisional"), ("primetime", "day"),
    ("cold_or_windy", "other_weather"),
)
REGIME_LABELS = {
    "favourite": "favourite", "underdog": "underdog", "home": "home", "away": "away", "divisional": "divisional",
    "non_divisional": "non-divisional", "primetime": "primetime", "day": "day", "cold_or_windy": "cold or windy",
    "other_weather": "other weather",
}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _pair(value: Any) -> list[float | None] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return [_num(value[0]), _num(value[1])]
    return None


def market_sentence(metrics: dict[str, Any]) -> str:
    """"Beats the market on log-loss: mean gain 0.0021 per game, p = 0.012" or the
    honest negative."""
    gain, p = _num(metrics.get("mean_ll_gain")), _num(metrics.get("market_p"))
    if gain is None or p is None:
        return "The market test has not been run."
    verdict = "Beats the market on log-loss" if p < MARKET_BEATEN_P else "Does not beat the market on log-loss"
    return f"{verdict}: mean gain {gain:+.4f} per game, p = {p:.3f} (sign-flip test, 10 000 flips; beaten means p < {MARKET_BEATEN_P})."


def calibration_rows(metrics: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The ten-bucket table with a label per bucket."""
    rows = (metrics or {}).get("calibration") if isinstance(metrics, dict) else None
    out = []
    for index, bucket in enumerate(rows or []):
        if isinstance(bucket, dict):
            out.append({"bucket": f"{index / 10:.1f}-{(index + 1) / 10:.1f}", **bucket})
    return out


def regime_rows(stress: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The regime table in its five pairs, each row {key, label, n_games, n_bets, roi, pnl_cents, mean_ll_gain}."""
    regimes = stress.get("regimes") if isinstance(stress, dict) else None
    if not isinstance(regimes, dict):
        return []
    out = []
    for pair in REGIME_PAIRS:
        for key in pair:
            entry = regimes.get(key)
            if isinstance(entry, dict):
                out.append({"key": key, "label": REGIME_LABELS[key], **entry})
    return out


def robustness_context(validation: dict[str, Any] | None, stress: Any) -> dict[str, Any] | None:
    """Everything `_robustness.html` renders; None when the model is not validated."""
    if not isinstance(validation, dict):
        return None
    stress = stress if isinstance(stress, dict) else {}
    ci = validation.get("ci") if isinstance(validation.get("ci"), dict) else {}
    decomposition = validation.get("brier_decomposition") if isinstance(validation.get("brier_decomposition"), dict) else {}
    neighbourhood = stress.get("neighbourhood") if isinstance(stress.get("neighbourhood"), dict) else None
    flags = model_flags(validation, stress)
    return {
        "metrics": validation,
        "summary": validation_summary({"validation_metrics": validation}),
        "ci": {key: _pair(ci.get(key)) for key in ("roi", "avg_clv", "max_drawdown", "hit_rate", "avg_edge")},
        "market": market_sentence(validation),
        "beats_market": (_num(validation.get("market_p")) or 1.0) < MARKET_BEATEN_P,
        "calib_slope": _num(validation.get("calib_slope")),
        "calib_intercept": _num(validation.get("calib_intercept")),
        "brier_decomposition": {key: _num(decomposition.get(key)) for key in ("reliability", "resolution", "uncertainty")},
        "calibration": calibration_rows(validation),
        "prices": [p for p in (stress.get("prices") or []) if isinstance(p, dict)],
        "neighbourhood": neighbourhood,
        "regimes": regime_rows(stress),
        "flags": [{"name": flag, "meaning": FLAG_MEANINGS.get(flag, "")} for flag in flags],
        "seed": stress.get("seed"),
    }
