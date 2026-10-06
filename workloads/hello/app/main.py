"""hello: the smallest useful workload. Kind "hello", params {name?, steps 1..60 (default 3), notify}.

Reads the secret HELLO_GREETING (default "Hello"), writes and reads back a file in the job's
scratch directory on every step, reports progress per step and returns the greeting. With
notify it queues a "log" outbound action, which waits for the owner's approval on /outbound.

The greeting is a secret: it is never printed, so it never reaches the shipped logs.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

from fleet_client import Client, Job, log, run_forever

MAX_STEPS = 60


def parse_params(params: dict[str, Any]) -> tuple[str, int, bool]:
    """(name, steps, notify) from the job params; ValueError fails the job with a clear message."""
    name = params.get("name", "world")
    if not isinstance(name, str) or not 0 < len(name) <= 100:
        raise ValueError("name must be a string of 1 to 100 characters")
    steps = params.get("steps", 3)
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= MAX_STEPS:
        raise ValueError(f"steps must be an integer 1..{MAX_STEPS}")
    notify = params.get("notify", False)
    if not isinstance(notify, bool):
        raise ValueError("notify must be true or false")
    return name, steps, notify


def make_handler(client: Client, step_seconds: float = 0.0):
    """The handler for kind "hello" (step_seconds slows each step, for demos and tests)."""

    def handle(job: Job) -> dict[str, Any] | None:
        name, steps, notify = parse_params(job.params)
        greeting = client.secret("HELLO_GREETING") or "Hello"
        start = int((job.checkpoint or {}).get("step", 0))
        for step in range(start + 1, steps + 1):
            if job.cancelled.is_set():
                job.release("cancelled")
                return None
            path = job.scratch / f"step-{step}.txt"
            path.write_text(f"step {step} of {steps}\n", encoding="utf-8")
            if path.read_text(encoding="utf-8") != f"step {step} of {steps}\n":
                raise RuntimeError(f"scratch read-back mismatch at step {step}")
            job.progress(step / steps, {"step": step})
            log(f"job {job.id[:8]}: step {step}/{steps}")
            if step_seconds:
                time.sleep(step_seconds)
        result: dict[str, Any] = {
            "greeting": f"{greeting}, {name}!",
            "machine": os.environ.get("FLEET_MACHINE_ID", ""),
            "epoch": _as_int(os.environ.get("FLEET_EPOCH", "")),
            "steps": steps,
        }
        if notify:
            queued = client.outbound("log", {"message": result["greeting"]}, f"hello:{job.id}", job)
            result["outbound_id"] = queued["id"]
        return result

    return handle


def _as_int(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def main() -> int:
    client = Client.from_env()
    step_seconds = float(os.environ.get("HELLO_STEP_SECONDS", "0") or 0)
    log("hello workload started")
    return run_forever({"hello": make_handler(client, step_seconds)}, client=client)


if __name__ == "__main__":
    sys.exit(main())
