"""Ordered multi-process map for the search (docs/ROBUSTNESS.md, A5).

ordered_map runs task(item) for every item through a fork pool and hands the results
to on_result in item order (Pool.imap, chunksize 1), so the caller's checkpoint only
ever advances through completed indices in order. The pool workers inherit the
parent's memory (the games list among it) and ignore SIGTERM: the runner's stop signal
reaches the parent alone, which notices it between results (should_stop is polled
while waiting), kills the workers and raises JobStopped. At most `workers` in-flight
items are lost that way and repeated on resume.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
from typing import Any, Callable, Sequence

from fleet.sim.control import JobStopped, check_stop

POLL_SECONDS = 0.2
_CTX: dict[str, Any] = {}


def resolve_workers(value: Any) -> int:
    """"auto" = cpu_count - 1 (at least 1); an integer is clamped to at least 1; None = 1."""
    if value is None:
        return 1
    if isinstance(value, str) and value.strip().lower() == "auto":
        return max(1, (os.cpu_count() or 2) - 1)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def context() -> dict[str, Any]:
    """The shared context a task reads inside a worker (set by the pool initializer)."""
    return _CTX


def _init_worker(ctx: dict[str, Any]) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _CTX.clear()
    _CTX.update(ctx)


def _kill_workers(pool: Any) -> None:
    """SIGKILL the pool's workers (they ignore SIGTERM) so terminate() returns at once."""
    for proc in list(getattr(pool, "_pool", None) or []):
        try:
            if proc.exitcode is None:
                os.kill(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass


def ordered_map(task: Callable[[Any], Any], items: Sequence[Any], workers: int, ctx: dict[str, Any],
                should_stop: Callable[[], bool], on_result: Callable[[Any], None]) -> None:
    """Call on_result(task(item)) in item order using `workers` forked processes.
    Raises JobStopped (after stopping the pool) when should_stop() turns true."""
    if not items:
        return
    check_stop(should_stop)
    mp = multiprocessing.get_context("fork")
    pool = mp.Pool(max(1, int(workers)), initializer=_init_worker, initargs=(ctx,))
    try:
        results = pool.imap(task, items, chunksize=1)
        for _ in items:
            while True:
                if should_stop():
                    raise JobStopped()
                try:
                    result = results.next(timeout=POLL_SECONDS)
                except multiprocessing.TimeoutError:
                    continue
                break
            on_result(result)
        pool.close()
        pool.join()
    except BaseException:
        _kill_workers(pool)
        pool.terminate()
        pool.join()
        raise
