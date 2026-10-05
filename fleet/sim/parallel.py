"""Ordered multi-process map for the search (docs/ROBUSTNESS.md, A5).

ordered_map runs task(item) for every item in `workers` forked processes and hands the
results to on_result in item order, so the caller's checkpoint only ever advances
through completed indices in order. The workers inherit the parent's memory (the games
list among it) through the fork; items and results travel over one pipe per worker.

Window. An index is handed to an idle worker only while it is less than `workers` past
the first unfinished index (the head). Running items plus items finished out of order
behind a slow head are therefore at most `workers`, and that is all a stop can lose.

Stopping. The runner signals its child's whole process group with SIGTERM, so each
worker moves to a process group of its own (same session: the memory watchdog and the
runner's session kill still reach it) and keeps SIGTERM's default disposition. The
parent alone sees the stop, notices it between results (should_stop is polled while
waiting), terminates the workers and raises JobStopped. A worker asks the kernel for
SIGKILL when its parent dies (PR_SET_PDEATHSIG, Linux), so a SIGKILLed runner leaves no
orphan computing on. Workers write nothing to stdout (the runner's JSONL pipe): it is
redirected to /dev/null.

Worker death. A worker that exits while the map runs (SIGKILL from the OOM killer, a
segfault) raises RuntimeError("search worker died ...") in the parent, so the job
errors and can be retried instead of waiting forever for a result that never comes.
An exception inside task(item) is sent back and re-raised in the parent.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import sys
import traceback
from multiprocessing.connection import Connection, wait
from typing import Any, Callable, Sequence

from fleet.sim.control import JobStopped, check_stop

POLL_SECONDS = 0.2
JOIN_SECONDS = 2.0
PR_SET_PDEATHSIG = 1
_CTX: dict[str, Any] = {}


def available_cpus() -> int:
    """CPUs this process may run on (the affinity mask, which taskset and cpusets
    narrow), else os.cpu_count()."""
    if hasattr(os, "sched_getaffinity"):
        try:
            return max(1, len(os.sched_getaffinity(0)))
        except OSError:
            pass
    return os.cpu_count() or 2


def resolve_workers(value: Any) -> int:
    """"auto" = available CPUs - 1 (at least 1); an integer is clamped to at least 1; None = 1."""
    if value is None:
        return 1
    if isinstance(value, str) and value.strip().lower() == "auto":
        return max(1, available_cpus() - 1)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def context() -> dict[str, Any]:
    """The shared context a task reads inside a worker (set when the worker starts)."""
    return _CTX


def _die_with_parent(parent: int) -> None:
    """SIGKILL this process when its parent exits (Linux); exit now if it already has."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
    except (OSError, AttributeError):
        return
    if os.getppid() != parent:
        os._exit(1)


def _init_worker(ctx: dict[str, Any], parent: int) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _die_with_parent(parent)
    try:
        os.setpgid(0, 0)
    except OSError:
        pass
    try:
        sys.stdout.flush()
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
    except OSError:
        pass
    _CTX.clear()
    _CTX.update(ctx)


def _worker_main(conn: Connection, task: Callable[[Any], Any], ctx: dict[str, Any], parent: int) -> None:
    """Loop: receive (index, item), send (index, True, result) or (index, False, error)."""
    _init_worker(ctx, parent)
    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            return
        if message is None:
            return
        index, item = message
        try:
            reply = (index, True, task(item))
        except BaseException as exc:  # noqa: BLE001  (re-raised in the parent)
            reply = (index, False, _portable(exc))
        try:
            conn.send(reply)
        except (BrokenPipeError, OSError):
            return


def _portable(exc: BaseException) -> BaseException:
    """The exception itself when it pickles, else a RuntimeError with its traceback."""
    import pickle

    try:
        pickle.loads(pickle.dumps(exc))
        return exc
    except Exception:  # noqa: BLE001
        return RuntimeError("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


class _Pool:
    """`workers` forked processes, each with its own pipe."""

    def __init__(self, task: Callable[[Any], Any], workers: int, ctx: dict[str, Any]) -> None:
        mp = multiprocessing.get_context("fork")
        self.procs: list[Any] = []
        self.conns: list[Connection] = []
        parent = os.getpid()
        for _ in range(workers):
            here, there = mp.Pipe()
            proc = mp.Process(target=_worker_main, args=(there, task, ctx, parent), daemon=True)
            proc.start()
            there.close()
            self.procs.append(proc)
            self.conns.append(here)

    def dead(self) -> list[int | None]:
        """Exit codes of workers that are no longer running."""
        return [p.exitcode for p in self.procs if not p.is_alive()]

    def close(self) -> None:
        """Ask every worker to finish, then end the stragglers."""
        for conn in self.conns:
            try:
                conn.send(None)
            except (BrokenPipeError, OSError):
                pass
        for proc in self.procs:
            proc.join(JOIN_SECONDS)
        self.terminate()

    def terminate(self) -> None:
        for proc in self.procs:
            if proc.is_alive():
                proc.terminate()
        for proc in self.procs:
            proc.join(JOIN_SECONDS)
            if proc.is_alive():
                proc.kill()
                proc.join(JOIN_SECONDS)
        for conn in self.conns:
            conn.close()


def ordered_map(task: Callable[[Any], Any], items: Sequence[Any], workers: int, ctx: dict[str, Any],
                should_stop: Callable[[], bool], on_result: Callable[[Any], None]) -> None:
    """Call on_result(task(item)) in item order using `workers` forked processes.
    Raises JobStopped (after ending the workers) when should_stop() turns true, and
    RuntimeError when a worker dies."""
    if not items:
        return
    check_stop(should_stop)
    workers = max(1, min(int(workers), len(items)))
    pool = _Pool(task, workers, ctx)
    try:
        _drive(pool, items, workers, should_stop, on_result)
    except BaseException:
        pool.terminate()
        raise
    pool.close()


def _drive(pool: _Pool, items: Sequence[Any], workers: int, should_stop: Callable[[], bool],
           on_result: Callable[[Any], None]) -> None:
    busy: dict[int, int] = {}  # worker slot -> item index
    done: dict[int, Any] = {}
    head = next_index = 0
    while head < len(items):
        if should_stop():
            raise JobStopped()
        for slot in range(workers):
            if slot not in busy and next_index < min(len(items), head + workers):
                pool.conns[slot].send((next_index, items[next_index]))
                busy[slot] = next_index
                next_index += 1
        waiting = [pool.conns[slot] for slot in busy]
        ready = wait(waiting + [p.sentinel for p in pool.procs], timeout=POLL_SECONDS)
        for slot, conn in enumerate(pool.conns):
            if conn in ready and slot in busy:
                try:
                    index, ok, payload = conn.recv()
                except (EOFError, OSError):
                    pool.procs[slot].join(POLL_SECONDS)
                    raise RuntimeError(f"search worker died (exit {pool.procs[slot].exitcode}); the job can be retried")
                if not ok:
                    raise payload
                done[index] = payload
                del busy[slot]
        if pool.dead():
            raise RuntimeError(f"search worker died (exit {pool.dead()}); the job can be retried")
        while head in done:
            if should_stop():  # checked before every result, as between units of a single-process run
                raise JobStopped()
            on_result(done.pop(head))
            head += 1
