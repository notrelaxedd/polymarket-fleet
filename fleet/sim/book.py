"""Walking a recorded order book (docs/TRADING.md "Executor and paper fills"; docs/ROBUSTNESS.md B1).

One pure function shared by the host's paper fill simulator (host/exchange/paper.py)
and the worker's snapshot replay backtests, so a paper fill and a replayed fill of the
same order on the same book are the same fill.

A level is `[price, size]` (strings or numbers). A buy walks the ask levels as given
(best, that is lowest, first) while `price <= limit`; a sell walks the bid levels as
given (best, that is highest, first) while `price >= limit`. Each level offers
`floor(participation * size)` contracts minus what `taken` says other orders already
took from it. A resting order fills at its own limit, a marketable one at the level.
"""

from __future__ import annotations

import math
from typing import Any

EPS = 1e-9
SIDES = ("buy", "sell")


def _level(level: Any) -> tuple[float, float] | None:
    try:
        price, size = float(level[0]), float(level[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not (math.isfinite(price) and math.isfinite(size)):
        return None
    return price, size


def crosses(price: float, limit: float, side: str) -> bool:
    """True when a level at `price` can fill an order with `limit` on `side`."""
    return price <= limit + EPS if side == "buy" else price >= limit - EPS


def walk(
    levels: Any,
    limit: float,
    size: int,
    participation: float,
    taken: dict[int, int] | None = None,
    side: str = "buy",
    resting: bool = False,
) -> list[dict[str, Any]]:
    """The fills an order of `size` contracts gets from one book side:
    [{"level": index, "price": fill price, "size": contracts}] in walk order."""
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, not {side!r}")
    left = int(size)
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(levels or []):
        if left <= 0:
            break
        parsed = _level(raw)
        if parsed is None:
            continue
        price, depth = parsed
        if not crosses(price, limit, side):
            break
        offered = int(math.floor(participation * depth + EPS)) - int((taken or {}).get(index, 0))
        take = min(left, offered)
        if take <= 0:
            continue
        out.append({"level": index, "price": limit if resting else price, "size": take})
        left -= take
    return out


def depth_through(levels: Any, limit: float, side: str = "buy") -> float:
    """Contracts offered on one book side at prices that cross `limit`."""
    total = 0.0
    for raw in levels or []:
        parsed = _level(raw)
        if parsed is None:
            continue
        price, depth = parsed
        if crosses(price, limit, side) and depth > 0:
            total += depth
    return total
