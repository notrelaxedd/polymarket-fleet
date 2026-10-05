"""The game-state feed's ESPN request budget (docs/INGAME.md, "Live game state").

One sliding window (at most `gamestate_max_rps` requests per second, at least one per
window) and one jittered 429/403 backoff (15 s doubling to 300 s, 0.8 to 1.2 times)
cover every ESPN request: the summaries, the scoreboard fallback and the scores task's
scoreboard. `PollerState` holds both between passes; host/exchange/gamestate.py drives
it and re-exports it.
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from host.exchange.adapters.base import truncate

__all__ = ["Answer", "BACKOFF_MAX_S", "BACKOFF_START_S", "Fetch", "PollerState"]

Fetch = Callable[[str], tuple[int, str]]
Answer = tuple[int | None, str, str | None, float]

BACKOFF_START_S = 15.0
BACKOFF_MAX_S = 300.0


@dataclass
class PollerState:
    """What the poller remembers between passes (one per exchange process). Requests
    are stamped by `clock` (epoch seconds) when they leave; without one, by the pass's
    `now`. `board` is the newest scoreboard answer: (stamp, status, text or error)."""

    last_poll: dict[str, float] = field(default_factory=dict)
    requests: deque[float] = field(default_factory=deque)
    backoff_until: dict[str, float] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    yahoo_noted: bool = False
    pending_fallback: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_board: float = float("-inf")
    board_wanted: bool = False
    board: tuple[float, int | None, str] | None = None
    clock: Callable[[], float] | None = None

    def stamp(self, now_ts: float) -> float:
        return self.clock() if self.clock is not None else now_ts

    def allow(self, now_ts: float, rps: float) -> bool:
        """True when one more request keeps every window of max(1, 1/rps) seconds at
        or under rps * window requests (at least one)."""
        window = max(1.0, 1.0 / rps)
        cap = max(1, int(rps * window + 1e-9))
        while self.requests and self.requests[0] <= now_ts - window:
            self.requests.popleft()
        return len(self.requests) < cap

    def back_off(self, source: str, now_ts: float, rng: random.Random) -> float:
        """Start or extend the source's backoff; the time it ends."""
        self.failures[source] = self.failures.get(source, 0) + 1
        delay = min(BACKOFF_MAX_S, BACKOFF_START_S * 2 ** (self.failures[source] - 1))
        until = now_ts + min(BACKOFF_MAX_S, delay * (0.8 + 0.4 * rng.random()))
        self.backoff_until[source] = until
        return until

    def send(self, fetch: Fetch, url: str, now_ts: float, rps: float, rng: random.Random) -> Answer | None:
        """One ESPN request through the shared window and backoff, stamped as it leaves:
        (status, text, error, stamp), or None (nothing sent) while backing off or with
        the window full. A 200 resets the backoff; a 429 or 403 starts or extends it."""
        ts = self.stamp(now_ts)
        if self.backoff_until.get("espn", 0.0) > ts or not self.allow(ts, rps):
            return None
        self.requests.append(ts)
        status, text, error = _get(fetch, url)
        if status == 200:
            self.failures["espn"] = 0
        elif status in (429, 403):
            self.back_off("espn", ts, rng)
        return status, text, error, ts

    def ask_board(self, fetch: Fetch, url: str, now_ts: float, rps: float, rng: random.Random) -> Answer | None:
        """send() for the scoreboard, kept as `board` for the scores task and the fallback."""
        answer = self.send(fetch, url, now_ts, rps, rng)
        if answer is not None:
            status, text, error, ts = answer
            self.board_wanted = False
            self.board = (ts, status, text if status == 200 else error or f"scoreboard answered {status}: {truncate(text, 256)}")
        return answer


def _get(fetch: Fetch, url: str) -> tuple[int | None, str, str | None]:
    try:
        status, text = fetch(url)
        return status, text, None
    except Exception as exc:  # noqa: BLE001 - a failed fetch leaves the state stale
        return None, "", str(exc)
