"""Job registry for the runner child process.

A job function has the signature run(params, checkpoint, emit, should_stop) -> result.
It calls emit(checkpoint, progress) after every unit of work (units must take < 3 s)
and raises JobStopped when should_stop() turns true between units.
"""

from __future__ import annotations

import time
from typing import Any, Callable

Emit = Callable[[dict[str, Any], float], None]
ShouldStop = Callable[[], bool]
JobFunc = Callable[[dict[str, Any], dict[str, Any] | None, Emit, ShouldStop], Any]


class JobStopped(Exception):
    """Raised by a job when should_stop() is true; the runner reports the last checkpoint."""


def run_sleep(
    params: dict[str, Any],
    checkpoint: dict[str, Any] | None,
    emit: Emit,
    should_stop: ShouldStop,
) -> dict[str, Any]:
    """Sleep params["seconds"] in 1 s units; checkpoint {"elapsed": n}; result {"slept": seconds}."""
    seconds = int(params.get("seconds", 0))
    elapsed = 0
    if checkpoint:
        elapsed = int(checkpoint.get("elapsed", 0))
    elapsed = max(0, min(elapsed, seconds))
    while elapsed < seconds:
        if should_stop():
            raise JobStopped()
        time.sleep(1.0)
        elapsed += 1
        emit({"elapsed": elapsed}, elapsed / seconds)
    return {"slept": seconds}


JOBS: dict[str, JobFunc] = {"sleep": run_sleep}
