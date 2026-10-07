"""Skeleton workload: one job kind, "work". Replace handle() with the real work.

Rules of a handler (see workloads/README.md, "The container contract"):
- return a small JSON-able dict: it becomes the job's result;
- raise an exception to fail the job (the traceback is stored as the error);
- call job.progress(p, checkpoint) now and then (p is 0..1); it also renews the lease;
- keep files in job.scratch only: it is deleted after the job ends;
- never print a secret or the run token: stdout and stderr are shipped to the host.
"""
from __future__ import annotations

import sys
from typing import Any

from fleet_client import Client, Job, log, run_forever


def make_handler(client: Client):
    def handle(job: Job) -> dict[str, Any] | None:
        secret = client.secret("EXAMPLE_SECRET")  # None when it is not set on the host
        log(f"job {job.id[:8]} params={sorted(job.params)} secret_set={secret is not None}")
        total = 3
        for step in range(1, total + 1):
            if job.cancelled.is_set():  # the owner cancelled the job: give it back
                job.release("cancelled")
                return None
            (job.scratch / f"step-{step}.txt").write_text(f"step {step}\n", encoding="utf-8")
            job.progress(step / total, {"step": step})
        return {"steps": total}

    return handle


def main() -> int:
    client = Client.from_env()
    log("workload started")
    return run_forever({"work": make_handler(client)}, client=client)


if __name__ == "__main__":
    sys.exit(main())
