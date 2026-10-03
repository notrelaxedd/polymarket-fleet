"""In-memory fake of the host protocol (docs/PROTOCOL.md) for worker tests.

A threading HTTPServer on 127.0.0.1:0 plus a control API the tests call directly.
Mirrors the host's review fixes: register accepts the previous worker token once
(the first heartbeat with the new token clears it), a heartbeat with want_job that
does not report a lease this worker still holds gets that job re-offered in
claimed[] (same lease token), renew keeps progress/checkpoint when they are null,
and job payloads carry the stored progress. fail_next() injects error answers.

Step 2: a release's "reason" is stored in the released event's detail; reason "oom"
counts as a lease expiry (the job fails once expiries reaches max_expiries, default
3); set_kill() drives the kill flag in every reply, and batch claims keep flowing
under kill. The heartbeat reply also carries cancel[] (the preempt[] ids whose job is
cancel_requested) so the agent can name the right release reason.

Step 3: GET /api/v1/data/games serves the rows given to set_games() with an ETag
(304 on If-None-Match), GET /api/v1/models/{id} serves models seeded with add_model(),
POST /api/v1/models creates a model (idempotent on family + params_hash +
trained_through, 409 when job_id is not leased by the caller) and
POST /api/v1/models/{id}/backtest stores metrics; models() and model_posts() expose
the store and the recorded calls. hold_posts() parks matching POSTs until
release_holds() (they then get a 503), which lets a test stop an agent between two
posts of a sequence; fail_next(..., status=0) drops the connection without an answer.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import socket
import tarfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import fleet

BATCH_ROLES = ("backtest", "model_search", "train")
KIND_TO_ROLE = {"sleep": "backtest"}
MODEL_FIELDS = ("family", "params", "artifact", "backtest_metrics", "summary", "parent_model_id", "trained_through")
HOLD_TIMEOUT = 60.0


def _canonical(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def params_hash(params: Any) -> str:
    """sha256 of the canonical JSON (sorted keys, 6 decimals), first 16 hex (docs/MODELS.md)."""
    text = json.dumps(_canonical(params), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _role_for(kind: str) -> str:
    return KIND_TO_ROLE.get(kind, kind)


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def build_worker_tarball(version: str, overrides: dict[str, bytes] | None = None) -> bytes:
    """Tarball with one top-level dir fleet/ built from the real package, plus fleet/VERSION.
    overrides maps a path relative to fleet/ (e.g. "worker/agent.py") to replacement bytes."""
    root = os.path.dirname(os.path.abspath(fleet.__file__))
    overrides = dict(overrides or {})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for name in sorted(filenames):
                if name.endswith(".pyc") or name == "VERSION":
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                if rel in overrides:
                    _add_bytes(tar, "fleet/" + rel, overrides.pop(rel))
                    continue
                info = tar.gettarinfo(full, arcname="fleet/" + rel)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with open(full, "rb") as fh:
                    tar.addfile(info, fh)
        for rel, data in overrides.items():
            _add_bytes(tar, "fleet/" + rel, data)
        _add_bytes(tar, "fleet/VERSION", version.encode("utf-8") + b"\n")
    return buf.getvalue()


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


class FakeHost:
    """Fake host: start() it, talk to it over HTTP, poke it through the control methods."""

    def __init__(
        self,
        lease_seconds: float = 30.0,
        heartbeat_seconds: float = 5,
        code_version: str | None = None,
        max_expiries: int | None = 3,
    ) -> None:
        self.lock = threading.RLock()
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.max_expiries = max_expiries
        self.kill_switch = False
        self.workers: dict[str, dict[str, Any]] = {}
        self.enroll_tokens: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, int]] = []
        self.code_version = code_version or fleet.__version__
        self.failures: list[dict[str, Any]] = []
        self.holds: list[dict[str, Any]] = []
        self._hold_event = threading.Event()
        self.games_rows: list[Any] = []
        self.games_etag = "0-0"
        self.models_store: dict[str, dict[str, Any]] = {}
        self.model_calls: list[dict[str, Any]] = []
        self._tarball: bytes | None = None
        self._sha_override: str | None = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.host = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self.server.serve_forever, name="fake-host", daemon=True)

    # ------------------------------------------------------------- lifecycle

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def start(self) -> "FakeHost":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.release_holds()
        self.server.shutdown()
        self.server.server_close()

    # --------------------------------------------------------------- control

    def mint_enroll_token(self, ttl: float = 3600.0) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.enroll_tokens[token] = {"expires_at": time.time() + ttl, "used_by": None}
        return token

    def set_desired_role(self, worker_id: str, role: str) -> None:
        with self.lock:
            w = self.workers[worker_id]
            w["desired_role"] = role
            w["role_epoch"] += 1
            w["auto_role"] = False
            for job in self.jobs.values():
                if job["lease_worker_id"] == worker_id and job["status"] == "leased" and job["role"] != role:
                    job["preempt_requested"] = True

    def set_enabled(self, worker_id: str, enabled: bool) -> None:
        with self.lock:
            self.workers[worker_id]["enabled"] = enabled

    def set_kill(self, flag: bool) -> None:
        """Mirror of settings.kill_switch: every register and heartbeat reply carries it."""
        with self.lock:
            self.kill_switch = bool(flag)

    def rotate_token(self, worker_id: str) -> str:
        """Rotate the worker token as a register elsewhere would, keeping the old one
        valid for one more register (prev token). Returns the new token."""
        with self.lock:
            w = self.workers[worker_id]
            w["prev_token"] = w["token"]
            w["token"] = secrets.token_urlsafe(32)
            return w["token"]

    def fail_next(self, suffix: str, status: int = 503, count: int = 1) -> None:
        """Answer the next `count` requests whose path ends with /<suffix> with `status`
        (0 = drop the connection without an answer, like a network failure)."""
        with self.lock:
            self.failures.append({"suffix": "/" + suffix.strip("/"), "status": status, "count": count})

    def hold_posts(self, suffix: str, skip: int = 0) -> None:
        """After `skip` matching POSTs pass through, park every further POST whose path
        ends with /<suffix> until release_holds(); a parked request then gets a 503 and
        is never processed. The agent sees a timeout meanwhile."""
        with self.lock:
            self._hold_event.clear()
            self.holds.append({"suffix": "/" + suffix.strip("/"), "skip": skip})

    def release_holds(self) -> None:
        with self.lock:
            self.holds.clear()
            self._hold_event.set()

    def _should_hold(self, path: str) -> bool:
        with self.lock:
            for entry in self.holds:
                if path.endswith(entry["suffix"]):
                    if entry["skip"] > 0:
                        entry["skip"] -= 1
                        return False
                    return True
        return False

    # ------------------------------------------------------- step 3 control

    def set_games(self, rows: list[Any]) -> str:
        """Replace the games rows served to workers; returns the new ETag."""
        with self.lock:
            self.games_rows = list(rows)
            self.games_etag = "%d-%d" % (len(rows), int(time.time() * 1000))
            return self.games_etag

    def add_model(self, model: dict[str, Any] | None = None) -> str:
        """Seed a model row (defaults filled in); returns its id."""
        row = {"id": str(uuid.uuid4()), "family": "elo_blend", "params": {}, "artifact": None, "parent_model_id": None,
               "trained_through": None, "status": "candidate", "backtest_metrics": None, "summary": None}
        row.update(model or {})
        row.setdefault("lineage_id", row["id"])
        row["_key"] = (row["family"], params_hash(row["params"]), json.dumps(row.get("trained_through")))
        with self.lock:
            self.models_store[row["id"]] = row
        return row["id"]

    def models(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self._public_model(m) for m in self.models_store.values()]

    @staticmethod
    def _public_model(row: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in row.items() if not k.startswith("_")}

    def model_posts(self) -> list[dict[str, Any]]:
        """Recorded POST /api/v1/models and /models/{id}/backtest calls, oldest first:
        {"path", "body", "worker_id", "response"}."""
        with self.lock:
            return [dict(c) for c in self.model_calls]

    def _take_failure(self, path: str) -> int | None:
        with self.lock:
            for entry in self.failures:
                if entry["count"] > 0 and path.endswith(entry["suffix"]):
                    entry["count"] -= 1
                    return int(entry["status"])
        return None

    def enqueue_job(self, kind: str = "sleep", params: dict[str, Any] | None = None, target: str | None = None) -> str:
        """Insert a queued job. With target, flip that worker into the job's role (auto_role)."""
        role = _role_for(kind)
        job = {
            "id": str(uuid.uuid4()), "kind": kind, "role": role, "status": "queued",
            "params": params or {}, "checkpoint": None, "progress": 0.0,
            "target_worker_id": target, "lease_worker_id": None, "lease_token": None,
            "lease_expires_at": None, "expiries": 0, "preempt_requested": False,
            "result": None, "error": None, "created_at": time.time(),
        }
        with self.lock:
            self.jobs[job["id"]] = job
            if target is not None:
                w = self.workers[target]
                if w["desired_role"] != role:
                    w["desired_role"] = role
                    w["role_epoch"] += 1
                    w["auto_role"] = True
                    for other in self.jobs.values():
                        if other["lease_worker_id"] == target and other["status"] == "leased" and other["role"] != role:
                            other["preempt_requested"] = True
        return job["id"]

    def lease_to(self, worker_id: str, job_id: str, checkpoint: dict[str, Any] | None = None, progress: float = 0.0) -> str:
        """Lease a job to a worker directly (simulates a job held across a restart)."""
        with self.lock:
            job = self.jobs[job_id]
            job.update(
                status="leased", lease_worker_id=worker_id, lease_token=str(uuid.uuid4()),
                lease_expires_at=time.time() + self.lease_seconds, checkpoint=checkpoint, progress=progress,
            )
            self._event(job_id, "claimed", worker_id)
            return job["lease_token"]

    def request_preempt(self, job_id: str) -> None:
        with self.lock:
            self.jobs[job_id]["preempt_requested"] = True

    def cancel_job(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            if job["status"] == "queued":
                job["status"] = "cancelled"
            elif job["status"] == "leased":
                job["status"] = "cancel_requested"

    def expire_lease(self, job_id: str) -> None:
        """Act as the reaper: requeue the job (or fail it past max_expiries) and clear its lease."""
        with self.lock:
            job = self.jobs[job_id]
            job["status"] = "cancelled" if job["status"] == "cancel_requested" else "queued"
            self._count_expiry(job)
            self._clear_lease(job)
            self._event(job_id, "lease_expired", None)

    def set_code_version(self, version: str, tarball: bytes | None = None, sha256_override: str | None = None) -> None:
        with self.lock:
            self.code_version = version
            self._tarball = tarball if tarball is not None else build_worker_tarball(version)
            self._sha_override = sha256_override

    def tarball(self) -> bytes:
        with self.lock:
            if self._tarball is None:
                self._tarball = build_worker_tarball(self.code_version)
            return self._tarball

    def tarball_sha256(self) -> str:
        with self.lock:
            return self._sha_override or hashlib.sha256(self.tarball()).hexdigest()

    def job(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            return dict(self.jobs[job_id])

    def worker(self, worker_id: str) -> dict[str, Any]:
        with self.lock:
            return dict(self.workers[worker_id])

    def job_events(self, job_id: str) -> list[str]:
        with self.lock:
            return [e["event"] for e in self.events if e["job_id"] == job_id]

    def releases(self, job_id: str) -> list[dict[str, Any]]:
        """Detail dicts ({"checkpoint", "reason"}) of the job's released events, oldest first."""
        with self.lock:
            return [dict(e["detail"] or {}) for e in self.events if e["job_id"] == job_id and e["event"] == "released"]

    def wait_for(self, predicate: Callable[[], Any], timeout: float = 10.0, interval: float = 0.02) -> Any:
        """Poll predicate until it returns a truthy value; raise on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() > deadline:
                raise TimeoutError("condition not met within %.1fs" % timeout)
            time.sleep(interval)

    # -------------------------------------------------------------- internals

    def _event(self, job_id: str, event: str, worker_id: str | None, detail: Any = None) -> None:
        self.events.append({"job_id": job_id, "event": event, "worker_id": worker_id, "detail": detail, "t": time.time()})

    @staticmethod
    def _clear_lease(job: dict[str, Any]) -> None:
        job["lease_worker_id"] = None
        job["lease_token"] = None
        job["lease_expires_at"] = None
        job["preempt_requested"] = False

    def _count_expiry(self, job: dict[str, Any]) -> None:
        """expiries += 1; a queued job fails once expiries reaches max_expiries."""
        job["expiries"] += 1
        if job["status"] == "queued" and self.max_expiries is not None and job["expiries"] >= self.max_expiries:
            job["status"] = "failed"
            job["error"] = "failed after %d expiries (last: lease expired)" % job["expiries"]
            job["finished_at"] = time.time()

    def _release(self, job: dict[str, Any], worker_id: str, checkpoint: Any, progress: Any, reason: Any = None) -> None:
        job["status"] = "cancelled" if job["status"] == "cancel_requested" else "queued"
        if checkpoint is not None:
            job["checkpoint"] = checkpoint
        if progress is not None:
            job["progress"] = float(progress)
        if reason not in ("drain", "preempt", "cancel", "oom", "shutdown", "stopped"):
            reason = None
        if reason == "oom":
            self._count_expiry(job)
        self._clear_lease(job)
        self._event(job["id"], "released", worker_id, {"checkpoint": checkpoint, "reason": reason})

    def _job_payload(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": job["id"], "kind": job["kind"], "params": job["params"], "checkpoint": job["checkpoint"],
            "progress": job["progress"], "lease_token": job["lease_token"], "lease_seconds": self.lease_seconds,
        }

    def _worker_fields(self, w: dict[str, Any]) -> dict[str, Any]:
        return {
            "desired_role": w["desired_role"], "role_epoch": w["role_epoch"], "kill": self.kill_switch,
            "code_version": self.code_version, "server_time": _now_iso(), "heartbeat_seconds": self.heartbeat_seconds,
        }

    # --------------------------------------------------------------- routes

    def auth_worker(self, worker_id: str, header: str | None) -> dict[str, Any]:
        if not header or not header.startswith("Bearer "):
            raise ApiError(401, "missing bearer token")
        token = header[len("Bearer "):]
        with self.lock:
            w = self.workers.get(worker_id)
            if w is None or not secrets.compare_digest(w["token"], token):
                raise ApiError(401, "bad token")
            return w

    def auth_any_worker(self, header: str | None) -> dict[str, Any]:
        if not header or not header.startswith("Bearer "):
            raise ApiError(401, "missing bearer token")
        token = header[len("Bearer "):]
        with self.lock:
            for w in self.workers.values():
                if secrets.compare_digest(w["token"], token):
                    return w
        raise ApiError(401, "bad token")

    def register(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if body.get("enroll_token"):
                tok = self.enroll_tokens.get(body["enroll_token"])
                if tok is None or tok["used_by"] or tok["expires_at"] < time.time():
                    raise ApiError(401, "bad enroll token")
                wid = "w_" + secrets.token_hex(3)
                w = {
                    "id": wid, "name": body.get("name") or body.get("hostname") or wid, "token": "", "prev_token": None,
                    "desired_role": "idle", "role_epoch": 1, "reported_role": "idle", "acked_epoch": 0,
                    "auto_role": False, "enabled": True, "last_heartbeat_at": None, "code_version": None,
                }
                self.workers[wid] = w
                tok["used_by"] = wid
            elif body.get("worker_id") and body.get("worker_token"):
                w = self.workers.get(body["worker_id"])
                presented = str(body["worker_token"])
                if w is None or not (
                    secrets.compare_digest(w["token"], presented)
                    or (w.get("prev_token") and secrets.compare_digest(w["prev_token"], presented))
                ):
                    raise ApiError(401, "bad worker token")
            else:
                raise ApiError(400, "enroll_token or worker_id+worker_token required")
            w["prev_token"] = w["token"] if w["token"] else None
            w["token"] = secrets.token_urlsafe(32)
            for key in ("hostname", "python_version", "code_version", "boot_id"):
                w[key] = body.get(key)
            held = []
            now = time.time()
            for job in self.jobs.values():
                if job["lease_worker_id"] == w["id"] and job["status"] in ("leased", "cancel_requested") and job["lease_expires_at"] and job["lease_expires_at"] > now:
                    job["lease_token"] = str(uuid.uuid4())
                    job["lease_expires_at"] = now + self.lease_seconds
                    self._event(job["id"], "re-leased", w["id"])
                    held.append(self._job_payload(job))
            resp = {"worker_id": w["id"], "worker_token": w["token"], "held_jobs": held}
            resp.update(self._worker_fields(w))
            return resp

    def heartbeat(self, w: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            now = time.time()
            wid = w["id"]
            w["last_heartbeat_at"] = now
            w["prev_token"] = None
            for key in ("cpu_pct", "ram_used_mb", "ram_total_mb", "reported_role", "acked_epoch", "code_version", "skew_ms"):
                if key in body:
                    w[key] = body[key]
            lost: list[str] = []
            reported: set[str] = set()
            for entry in body.get("jobs") or []:
                reported.add(str(entry.get("id")))
                job = self.jobs.get(str(entry.get("id")))
                if job and job["lease_token"] == entry.get("lease_token") and job["lease_worker_id"] == wid and job["status"] in ("leased", "cancel_requested"):
                    job["lease_expires_at"] = now + self.lease_seconds
                    if entry.get("progress") is not None:
                        job["progress"] = float(entry["progress"])
                    if entry.get("checkpoint") is not None:
                        job["checkpoint"] = entry["checkpoint"]
                else:
                    lost.append(str(entry.get("id")))
            for entry in body.get("released") or []:
                reported.add(str(entry.get("id")))
                job = self.jobs.get(str(entry.get("id")))
                if job and job["lease_token"] == entry.get("lease_token") and job["status"] in ("leased", "cancel_requested"):
                    self._release(job, wid, entry.get("checkpoint"), entry.get("progress"), entry.get("reason"))
            preempt = [j["id"] for j in self.jobs.values() if j["lease_worker_id"] == wid and (j["preempt_requested"] or j["status"] == "cancel_requested")]
            cancel = [j["id"] for j in self.jobs.values() if j["lease_worker_id"] == wid and j["status"] == "cancel_requested"]
            in_sync = w["reported_role"] == w["desired_role"] and w["acked_epoch"] == w["role_epoch"]
            if w["auto_role"] and w["desired_role"] != "idle" and in_sync:
                busy = any(
                    (j["lease_worker_id"] == wid and j["status"] in ("leased", "cancel_requested"))
                    or (j["target_worker_id"] == wid and j["status"] == "queued")
                    for j in self.jobs.values()
                )
                if not busy:
                    w["desired_role"] = "idle"
                    w["role_epoch"] += 1
                    w["auto_role"] = False
                    in_sync = False
            claimed: list[dict[str, Any]] = []
            if w["enabled"] and body.get("want_job") and in_sync and w["desired_role"] in BATCH_ROLES:
                orphans = [
                    j for j in self.jobs.values()
                    if j["lease_worker_id"] == wid and j["status"] in ("leased", "cancel_requested") and j["id"] not in reported
                ]
                for job in orphans:
                    job["lease_expires_at"] = now + self.lease_seconds
                    self._event(job["id"], "re-offered", wid)
                    claimed.append(self._job_payload(job))
                candidates = [] if orphans else [
                    j for j in self.jobs.values()
                    if j["status"] == "queued" and j["role"] == w["desired_role"] and j["target_worker_id"] in (None, wid)
                ]
                candidates.sort(key=lambda j: (0 if j["target_worker_id"] == wid else 1, j["created_at"]))
                if candidates:
                    job = candidates[0]
                    job.update(status="leased", lease_worker_id=wid, lease_token=str(uuid.uuid4()), lease_expires_at=now + self.lease_seconds, preempt_requested=False)
                    self._event(job["id"], "claimed", wid)
                    claimed.append(self._job_payload(job))
            resp = {"preempt": preempt, "cancel": cancel, "lost": lost, "claimed": claimed}
            resp.update(self._worker_fields(w))
            self.heartbeats.append({"t": time.monotonic(), "worker_id": wid, "request": body, "response": resp})
            return resp

    def checkpoint(self, w: dict[str, Any], job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            job = self._fenced_job(job_id, body)
            if body.get("checkpoint") is not None:
                job["checkpoint"] = body["checkpoint"]
            if body.get("progress") is not None:
                job["progress"] = float(body["progress"])
            if body.get("release"):
                self._release(job, w["id"], body.get("checkpoint"), body.get("progress"), body.get("reason"))
            return {"status": job["status"]}

    def complete(self, w: dict[str, Any], job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job["status"] == "succeeded" and job.get("done_token") == body.get("lease_token"):
                return {"status": "succeeded"}
            job = self._fenced_job(job_id, body)
            job.update(status="succeeded", result=body.get("result"), progress=1.0, done_token=job["lease_token"], finished_at=time.time())
            self._clear_lease(job)
            self._event(job_id, "succeeded", w["id"])
            return {"status": "succeeded"}

    def fail(self, w: dict[str, Any], job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            job = self._fenced_job(job_id, body)
            job.update(status="failed", error=str(body.get("error")), finished_at=time.time())
            self._clear_lease(job)
            self._event(job_id, "failed", w["id"], {"error": job["error"]})
            return {"status": "failed"}

    def games(self, w: dict[str, Any], if_none_match: str | None) -> tuple[int, Any, str]:
        """(status, body, etag): 304 with no body when the ETag matches."""
        with self.lock:
            if if_none_match and if_none_match.strip() == self.games_etag:
                return 304, None, self.games_etag
            return 200, list(self.games_rows), self.games_etag

    def get_model(self, w: dict[str, Any], model_id: str) -> dict[str, Any]:
        with self.lock:
            model = self.models_store.get(model_id)
            if model is None:
                raise ApiError(404, "no such model")
            return self._public_model(model)

    def _job_leased_by(self, w: dict[str, Any], job_id: Any) -> dict[str, Any]:
        job = self.jobs.get(str(job_id))
        if job is None or job["lease_worker_id"] != w["id"] or job["status"] not in ("leased", "cancel_requested"):
            raise ApiError(409, "job_id is not a job leased by this worker")
        return job

    def create_model(self, w: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._job_leased_by(w, body.get("job_id"))
            if not isinstance(body.get("family"), str) or not isinstance(body.get("params"), dict):
                raise ApiError(400, "family and params are required")
            key = (body["family"], params_hash(body["params"]), json.dumps(body.get("trained_through")))
            existing = next((m for m in self.models_store.values() if m["_key"] == key), None)
            if existing is not None:
                resp = {"id": existing["id"], "lineage_id": existing["lineage_id"], "created": False}
            else:
                row: dict[str, Any] = {"id": str(uuid.uuid4()), "_key": key, "job_id": body.get("job_id")}
                for name in MODEL_FIELDS:
                    row[name] = body.get(name)
                parent = self.models_store.get(str(body.get("parent_model_id") or ""))
                if parent is not None:
                    row.update(lineage_id=parent["lineage_id"], status=parent["status"], backtest_metrics=parent.get("backtest_metrics"))
                else:
                    row.update(lineage_id=row["id"], status="candidate")
                self.models_store[row["id"]] = row
                resp = {"id": row["id"], "lineage_id": row["lineage_id"], "created": True}
            self.model_calls.append({"path": "/api/v1/models", "body": body, "worker_id": w["id"], "response": resp})
            return resp

    def post_backtest(self, w: dict[str, Any], model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._job_leased_by(w, body.get("job_id"))
            model = self.models_store.get(model_id)
            if model is None:
                raise ApiError(404, "no such model")
            if not isinstance(body.get("backtest_metrics"), dict):
                raise ApiError(400, "backtest_metrics must be an object")
            for row in self.models_store.values():
                if row["lineage_id"] == model["lineage_id"]:
                    row["backtest_metrics"] = body["backtest_metrics"]
            resp = {"id": model_id, "lineage_id": model["lineage_id"], "status": model["status"]}
            self.model_calls.append({"path": f"/api/v1/models/{model_id}/backtest", "body": body, "worker_id": w["id"], "response": resp})
            return resp

    def _fenced_job(self, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise ApiError(404, "no such job")
        if job["status"] not in ("leased", "cancel_requested") or job["lease_token"] != body.get("lease_token"):
            raise ApiError(409, "lease token mismatch or job not leased")
        return job


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    @property
    def host(self) -> FakeHost:
        return self.server.host  # type: ignore[attr-defined]

    def _send(self, status: int, payload: Any, raw: bytes | None = None, etag: str | None = None) -> None:
        body = b"" if status == 304 else (raw if raw is not None else json.dumps(payload).encode("utf-8"))
        self.send_response(status)
        if etag is not None:
            self.send_header("ETag", etag)
        if status != 304:
            self.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)
        with self.host.lock:
            self.host.requests.append((self.command, self.path, status))

    def _drop_connection(self) -> None:
        """Simulate a network failure: no answer at all."""
        with self.host.lock:
            self.host.requests.append((self.command, self.path, 0))
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _injected(self) -> bool:
        """Apply fail_next / hold_posts; True when the request was answered here."""
        injected = self.host._take_failure(self.path)
        if injected is not None:
            if self.command == "POST":
                self._body()
            if injected == 0:
                self._drop_connection()
            else:
                self._send(injected, {"detail": "injected failure"})
            return True
        if self.command == "POST" and self.host._should_hold(self.path):
            self._body()
            self.host._hold_event.wait(HOLD_TIMEOUT)
            self._send(503, {"detail": "held"})
            return True
        return False

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            raise ApiError(400, "invalid JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "body must be an object")
        return data

    def do_GET(self) -> None:
        try:
            if self._injected():
                return
            parts = self.path.strip("/").split("/")
            if self.path == "/dl/version":
                self._send(200, {"code_version": self.host.code_version, "sha256": self.host.tarball_sha256()})
            elif self.path == "/dl/worker.tar.gz":
                self._send(200, None, raw=self.host.tarball())
            elif self.path == "/healthz":
                self._send(200, {"ok": True, "db": True})
            elif parts == ["api", "v1", "data", "games"]:
                w = self.host.auth_any_worker(self.headers.get("Authorization"))
                status, body, etag = self.host.games(w, self.headers.get("If-None-Match"))
                self._send(status, body, etag=etag)
            elif len(parts) == 4 and parts[:3] == ["api", "v1", "models"]:
                w = self.host.auth_any_worker(self.headers.get("Authorization"))
                self._send(200, self.host.get_model(w, parts[3]))
            else:
                self._send(404, {"detail": "not found"})
        except ApiError as exc:
            self._send(exc.status, {"detail": exc.detail})

    def do_POST(self) -> None:
        try:
            if self._injected():
                return
            self._send(200, self._dispatch_post())
        except ApiError as exc:
            self._send(exc.status, {"detail": exc.detail})

    def _dispatch_post(self) -> dict[str, Any]:
        parts = self.path.strip("/").split("/")
        auth = self.headers.get("Authorization")
        if parts == ["api", "v1", "workers", "register"]:
            return self.host.register(self._body())
        if len(parts) == 5 and parts[:3] == ["api", "v1", "workers"] and parts[4] == "heartbeat":
            w = self.host.auth_worker(parts[3], auth)
            return self.host.heartbeat(w, self._body())
        if len(parts) == 5 and parts[:3] == ["api", "v1", "jobs"] and parts[4] in ("checkpoint", "complete", "fail"):
            w = self.host.auth_any_worker(auth)
            handler = {"checkpoint": self.host.checkpoint, "complete": self.host.complete, "fail": self.host.fail}[parts[4]]
            return handler(w, parts[3], self._body())
        if parts == ["api", "v1", "models"]:
            w = self.host.auth_any_worker(auth)
            return self.host.create_model(w, self._body())
        if len(parts) == 5 and parts[:3] == ["api", "v1", "models"] and parts[4] == "backtest":
            w = self.host.auth_any_worker(auth)
            return self.host.post_backtest(w, parts[3], self._body())
        raise ApiError(404, "not found")


# ------------------------------------------------------------ test-only jobs
# Registered in the runner child through env FLEET_TEST_JOBS="tests.fake_host:TEST_JOBS".
# They shadow the real batch kinds so agent tests never run a backtest.


def run_echo(params: dict[str, Any], checkpoint: dict[str, Any] | None, emit: Any, should_stop: Any) -> dict[str, Any]:
    """Return params["result"] (if any) plus what the child handed the job: the params
    without _context, the context keys, the model, the number of rows in games_path.
    params["marker"] names a file to create (proves the job ran)."""
    out = dict(params.get("result") or {})
    context = params.get("_context")
    out["params"] = {k: v for k, v in params.items() if k != "_context"}
    out["has_context"] = isinstance(context, dict)
    out["context_keys"] = sorted(context) if isinstance(context, dict) else None
    out["model"] = context.get("model") if isinstance(context, dict) else None
    out["games_rows"] = None
    games_path = context.get("games_path") if isinstance(context, dict) else None
    if games_path:
        try:
            with open(games_path, "r", encoding="utf-8") as fh:
                out["games_rows"] = len(json.load(fh))
        except (OSError, ValueError):
            out["games_rows"] = None
    if params.get("marker"):
        with open(str(params["marker"]), "w", encoding="utf-8") as fh:
            fh.write("ran\n")
    emit({"echoed": True}, 1.0)
    return out


TEST_JOBS = {"echo": run_echo, "backtest": run_echo, "model_search": run_echo, "train": run_echo}
TEST_JOBS_SPEC = "tests.fake_host:TEST_JOBS"
