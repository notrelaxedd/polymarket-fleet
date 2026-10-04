"""Fleet-wide token buckets, one per category of `settings.rate_limits`.

The exchange process is the only one that talks to an exchange, so a bucket here is
fleet-wide by construction. Paper orders take no tokens. Priority when several
categories compete for the loop's time: cancels, orders, fills, snapshots (market
data). A 429 halves the category's refill rate for 60 s.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

CATEGORIES = {
    "cancels": "cancels_per_s",
    "orders": "orders_per_s",
    "fills": "account_per_s",
    "account": "account_per_s",
    "market_data": "market_data_per_s",
}
PRIORITY = ("cancels", "orders", "fills", "market_data")
DEFAULT_LIMITS = {"orders_per_s": 5.0, "cancels_per_s": 10.0, "market_data_per_s": 10.0, "account_per_s": 2.0}
BACKOFF_SECONDS = 60.0


def _seconds(now: datetime | float | None) -> float:
    if now is None:
        return time.monotonic()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


class TokenBucket:
    """Refills `rate` tokens per second up to `capacity` (one second's worth, at
    least one token)."""

    def __init__(self, rate: float, now: float) -> None:
        self.rate = max(0.0, float(rate))
        self.capacity = max(1.0, self.rate)
        self.tokens = self.capacity
        self.updated = now
        self.halved_until = 0.0

    def effective_rate(self, now: float) -> float:
        return self.rate / 2.0 if now < self.halved_until else self.rate

    def refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.effective_rate(now))
        self.updated = now

    def take(self, n: float, now: float) -> bool:
        self.refill(now)
        if self.tokens + 1e-9 >= n:
            self.tokens -= n
            return True
        return False

    def wait_seconds(self, n: float, now: float) -> float:
        self.refill(now)
        missing = n - self.tokens
        rate = self.effective_rate(now)
        if missing <= 0:
            return 0.0
        return float("inf") if rate <= 0 else missing / rate


class RateLimiter:
    def __init__(self, limits: dict[str, Any] | None = None, now: datetime | float | None = None) -> None:
        start = _seconds(now)
        merged = {**DEFAULT_LIMITS, **{k: v for k, v in (limits or {}).items() if isinstance(v, (int, float))}}
        self.limits = merged
        self.buckets: dict[str, TokenBucket] = {}
        for category, key in CATEGORIES.items():
            self.buckets[category] = TokenBucket(merged.get(key, 1.0), start)

    def update_limits(self, limits: dict[str, Any] | None, now: datetime | float | None = None) -> None:
        """Apply changed settings without losing the buckets' state."""
        at = _seconds(now)
        for category, key in CATEGORIES.items():
            rate = (limits or {}).get(key, DEFAULT_LIMITS[key])
            bucket = self.buckets[category]
            if isinstance(rate, (int, float)) and float(rate) != bucket.rate:
                bucket.refill(at)
                bucket.rate = float(rate)
                bucket.capacity = max(1.0, bucket.rate)
                bucket.tokens = min(bucket.tokens, bucket.capacity)

    def take(self, category: str, n: float = 1.0, now: datetime | float | None = None) -> bool:
        """True and one token fewer when the category has a token; False otherwise."""
        bucket = self.buckets.get(category)
        if bucket is None:
            return True
        return bucket.take(n, _seconds(now))

    def wait_seconds(self, category: str, n: float = 1.0, now: datetime | float | None = None) -> float:
        bucket = self.buckets.get(category)
        return 0.0 if bucket is None else bucket.wait_seconds(n, _seconds(now))

    def on_429(self, category: str, now: datetime | float | None = None) -> None:
        """Halve the refill rate for 60 s after the exchange answered 429."""
        bucket = self.buckets.get(category)
        if bucket is not None:
            at = _seconds(now)
            bucket.refill(at)
            bucket.halved_until = at + BACKOFF_SECONDS

    def effective_rate(self, category: str, now: datetime | float | None = None) -> float:
        bucket = self.buckets.get(category)
        return 0.0 if bucket is None else bucket.effective_rate(_seconds(now))

    @staticmethod
    def ordered(categories: list[str]) -> list[str]:
        """Categories sorted by priority (cancels first, market data last)."""
        rank = {name: i for i, name in enumerate(PRIORITY)}
        return sorted(categories, key=lambda c: rank.get(c, len(PRIORITY)))
