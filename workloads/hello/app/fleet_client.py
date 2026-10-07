"""Fleet workload SDK (workload-v1): claim jobs, renew leases, report results, queue outbound actions.

Standard library only, Python 3.11+. Copy this file unchanged into every workload's `app/`
(a test checks that all copies are byte-identical). Contract: docs/workloads-design.md
sections 5.2 and 11; usage: workloads/README.md.

    sys.exit(run_forever({"mykind": handle}))     # handle(job) -> result dict
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never use env proxies
HTTP_TIMEOUT = 15.0


def log(message: str) -> None:
    """One line on stdout; the supervisor ships it to the host. Never log secrets or the token."""
    print(message, flush=True)


class ApiError(Exception):
    """The host answered with an error status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class Unauthorized(ApiError):
    """401: the run token is no longer valid (the machine was reassigned). run_forever exits 0."""


class LeaseLost(ApiError):
    """409: this job is no longer ours (lease expired or fenced). Stop working on it."""


class ConnectionFailed(Exception):
    """No usable answer from the host (refused, timeout, reset)."""


class _Shutdown(BaseException):
    """Raised in the main thread by the SIGTERM handler."""


class Client:
    """Talks to the host's /api/v1/wl routes with the run token."""

    def __init__(self, host_url: str, token: str, workload: str = "", machine_id: str = "", epoch: str = "") -> None:
        self.host_url = host_url.rstrip("/")
        self.token = token
        self.workload, self.machine_id, self.epoch = workload, machine_id, epoch
        self.secrets_dir = Path(os.environ.get("FLEET_SECRETS_DIR", "/run/fleet/secrets"))
        self.scratch_dir = Path(os.environ.get("FLEET_SCRATCH_DIR", "/scratch"))
        self.unauthorized = threading.Event()

    @classmethod
    def from_env(cls) -> "Client":
        env = os.environ
        missing = [k for k in ("FLEET_HOST_URL", "FLEET_RUN_TOKEN") if not env.get(k)]
        if missing:
            raise SystemExit(f"missing environment: {', '.join(missing)}")
        return cls(env["FLEET_HOST_URL"], env["FLEET_RUN_TOKEN"], env.get("FLEET_WORKLOAD", ""),
                   env.get("FLEET_MACHINE_ID", ""), env.get("FLEET_EPOCH", ""))

    def secret(self, name: str) -> str | None:
        """Value of /run/fleet/secrets/<NAME>, or None when the file is absent."""
        try:
            return (self.secrets_dir / name).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            return None

    def call(self, method: str, path: str, body: Any = None, retries: int = 0, delay: float = 1.0) -> Any:
        """One JSON request; connection failures are retried `retries` times."""
        for attempt in range(retries + 1):
            try:
                return self._once(method, path, body)
            except ConnectionFailed:
                if attempt == retries:
                    raise
                time.sleep(delay)

    def _once(self, method: str, path: str, body: Any) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "Authorization": "Bearer " + self.token, "User-Agent": "fleet-workload"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.host_url + path, data=data, method=method, headers=headers)
        try:
            with _opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 401:
                self.unauthorized.set()
                raise Unauthorized(401, detail) from None
            raise (LeaseLost if exc.code == 409 else ApiError)(exc.code, detail) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ConnectionFailed(f"{method} {path}: {exc}") from None
        return json.loads(raw) if raw.strip() else None

    def claim(self, kinds: list[str]) -> "Job | None":
        """Take one queued job of these kinds, or None."""
        reply = self.call("POST", "/api/v1/wl/claim", {"kinds": list(kinds)})
        data = (reply or {}).get("job")
        return Job(self, data) if data else None

    def outbound(self, kind: str, payload: dict[str, Any], dedupe_key: str, job: "Job | str | None" = None) -> dict[str, Any]:
        """Queue an action (it waits for the owner's approval). The same dedupe_key returns the same row."""
        body: dict[str, Any] = {"kind": kind, "payload": payload, "dedupe_key": dedupe_key}
        if job is not None:
            body["job_id"] = job.id if isinstance(job, Job) else str(job)
        return self.call("POST", "/api/v1/wl/outbound", body, retries=3)

    def outbound_status(self, action_id: str) -> dict[str, Any]:
        """{"id", "status", "error", "result"} of one of this workload's actions."""
        return self.call("GET", f"/api/v1/wl/outbound/{action_id}", retries=3)


class Job:
    """A leased job. Progress calls renew the lease, and so does a background thread."""

    def __init__(self, client: Client, data: dict[str, Any]) -> None:
        self.client = client
        self.id: str = str(data["id"])
        self.kind: str = data["kind"]
        self.params: dict[str, Any] = data.get("params") or {}
        self.checkpoint: dict[str, Any] | None = data.get("checkpoint")
        self.lease_token: str = data["lease_token"]
        self.lease_seconds: float = float(data.get("lease_seconds") or 60)
        self.last_progress: float = float(data.get("progress") or 0.0)
        self.scratch = client.scratch_dir / self.id
        self.cancelled = threading.Event()  # the owner asked to cancel: release soon
        self.lost = threading.Event()
        self.finished = False
        self._stop = threading.Event()
        self.scratch.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._renew_loop, daemon=True, name=f"renew-{self.id[:8]}").start()

    def _beat(self) -> None:
        body: dict[str, Any] = {"lease_token": self.lease_token, "progress": self.last_progress}
        if self.checkpoint is not None:
            body["checkpoint"] = self.checkpoint
        reply = self.client.call("POST", f"/api/v1/wl/jobs/{self.id}/heartbeat", body) or {}
        if reply.get("cancel"):
            self.cancelled.set()

    def _renew_loop(self) -> None:
        while not self._stop.wait(max(0.2, self.lease_seconds / 3)):
            try:
                self._beat()
            except LeaseLost:
                self.lost.set()
                return
            except Unauthorized:
                return
            except (ApiError, ConnectionFailed) as exc:
                log(f"lease renew failed: {exc}")

    def progress(self, p: float, checkpoint: dict[str, Any] | None = None) -> None:
        """Record progress 0..1 (and an optional checkpoint) and renew the lease now."""
        if self.client.unauthorized.is_set():
            raise Unauthorized(401, "run token no longer valid")
        if self.lost.is_set():
            raise LeaseLost(409, "lease lost")
        self.last_progress = max(0.0, min(1.0, float(p)))
        if checkpoint is not None:
            self.checkpoint = checkpoint
        try:
            self._beat()
        except LeaseLost:
            self.lost.set()
            raise
        except ConnectionFailed as exc:
            log(f"progress not delivered: {exc}")  # the renew thread keeps trying

    def _end(self, path: str, body: dict[str, Any]) -> None:
        self._stop.set()
        try:
            if not self.lost.is_set():
                self.client.call("POST", f"/api/v1/wl/jobs/{self.id}/{path}", {"lease_token": self.lease_token, **body}, retries=4)
        finally:
            self.finished = True
            shutil.rmtree(self.scratch, ignore_errors=True)

    def complete(self, result: dict[str, Any]) -> None:
        self._end("complete", {"result": result})

    def fail(self, error: str) -> None:
        self._end("fail", {"error": str(error)[-16000:]})

    def release(self, reason: str = "released") -> None:
        """Give the job back: it is requeued with its checkpoint (cancelled if a cancel was requested)."""
        self._end("release", {"checkpoint": self.checkpoint, "progress": self.last_progress, "reason": reason})


Handler = Callable[[Job], "dict[str, Any] | None"]


def run_forever(handlers: dict[str, Handler], idle_sleep: float = 5, client: Client | None = None) -> int:
    """Claim and run jobs until SIGTERM or SIGINT (release the current job with reason "shutdown",
    return 0) or until the host answers 401 (return 0, release nothing). A handler returns the result
    dict; an exception fails the job; returning None means it already called complete, fail or release."""
    client = client or Client.from_env()
    current: list[Job] = []

    def on_term(signum: int, frame: Any) -> None:
        raise _Shutdown()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    backoff = idle_sleep
    try:
        while True:
            try:
                job = client.claim(list(handlers))
                backoff = idle_sleep
            except Unauthorized:
                log("run token no longer valid; exiting")
                return 0
            except (ApiError, ConnectionFailed) as exc:
                log(f"claim failed: {exc}")
                backoff = min(30.0, backoff * 2)
                time.sleep(backoff)
                continue
            if job is None:
                time.sleep(idle_sleep)
                continue
            current.append(job)
            try:
                _run_one(job, handlers)
            except Unauthorized:
                _drop(job)
                log("run token no longer valid; exiting")
                return 0
            except (LeaseLost, ApiError, ConnectionFailed) as exc:
                _drop(job)  # the lease expires on the host and the job is requeued
                log(f"job {job.id} dropped: {exc}")
            current.clear()  # not in a finally: _Shutdown must still see the job
    except _Shutdown:
        for job in current:
            if not job.finished:
                try:
                    job.release("shutdown")
                except Exception as exc:  # the lease expires and the job is requeued anyway
                    log(f"release on shutdown failed: {exc}")
            # The signal may land inside a finished job's own cleanup: always free its scratch.
            shutil.rmtree(job.scratch, ignore_errors=True)
        return 0


def _drop(job: Job) -> None:
    job._stop.set()
    shutil.rmtree(job.scratch, ignore_errors=True)


def _run_one(job: Job, handlers: dict[str, Handler]) -> None:
    handler = handlers.get(job.kind)
    if handler is None:
        job.fail(f"no handler for kind {job.kind!r}")
        return
    try:
        result = handler(job)
    except (Unauthorized, LeaseLost):
        raise
    except Exception:
        log(f"job {job.id} failed:\n" + traceback.format_exc())
        job.fail(traceback.format_exc())
        return
    if not job.finished:
        job.complete(result if isinstance(result, dict) else {})
