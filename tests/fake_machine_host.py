"""In-memory fake of the host's machine API (docs/workloads-design.md sections 5.1 and 5.4).

A threading HTTPServer on 127.0.0.1:0 plus controls the tests call directly:
  mint_enroll_token(), assign(workload, run, epoch=None), unassign(), set_secrets(),
  set_agent(version, tarball), fail_next(route, status), drop_response_next(route),
  heartbeats / registers / starts (recorded bodies), shipped_logs(), machine().

Register mirrors the real host: an enroll token creates the machine; a machine id plus
its current or previous token rotates the token (the previous stays valid until the
first heartbeat with the new one). A heartbeat needs the current token. /start mints a
run token and returns the configured secrets, 409 when the epoch is stale.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import fleetagent


def build_agent_tarball(version: str, overrides: dict[str, bytes] | None = None) -> bytes:
    """Tarball with one top-level dir fleetagent/ built from the real package plus fleetagent/VERSION."""
    root = os.path.dirname(os.path.abspath(fleetagent.__file__))
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
                    _add(tar, "fleetagent/" + rel, overrides.pop(rel))
                    continue
                info = tar.gettarinfo(full, arcname="fleetagent/" + rel)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with open(full, "rb") as fh:
                    tar.addfile(info, fh)
        for rel, data in overrides.items():
            _add(tar, "fleetagent/" + rel, data)
        _add(tar, "fleetagent/VERSION", version.encode() + b"\n")
    return buf.getvalue()


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def make_run(image: str = "reg.example/fleet/hello@sha256:" + "b" * 64, **over: Any) -> dict[str, Any]:
    """A `run` block as the host sends it, with overrides."""
    run: dict[str, Any] = {
        "image": image, "protocol": "workload-v1", "mode": "jobs", "network": "bridge", "uts_host": False, "uid": 10001,
        "memory_mb": 256, "cpus": 1.0, "nice": 0, "stop_timeout_s": 15, "state_volume": False, "scratch_mb": 512,
        "no_restart_exit_codes": [78],
        "env": {"FLEET_HOST_URL": "http://host", "FLEET_WORKLOAD": "hello", "FLEET_MACHINE_ID": "m_x", "FLEET_EPOCH": "1"},
    }
    run.update(over)
    return run


class FakeMachineHost:
    def __init__(self, heartbeat_seconds: float = 0.2, agent_version: str = "agent-v1") -> None:
        self.lock = threading.RLock()
        self.heartbeat_seconds = heartbeat_seconds
        self.agent_version = agent_version
        self.tarball: bytes | None = None
        self.tarball_sha: str | None = None
        self.enroll_tokens: set[str] = set()
        self.machines: dict[str, dict[str, Any]] = {}
        self.registers: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.starts: list[dict[str, Any]] = []
        self.run_tokens: list[str] = []
        self.epoch = 1
        self.workload: str | None = None
        self.run: dict[str, Any] | None = None
        self.keep_images: list[str] = []
        self.secret_values: dict[str, str] = {}
        self.acked_epoch = 0
        self.last_container: dict[str, Any] | None = None
        self._fail: dict[str, list[int]] = {}
        self._drop: dict[str, int] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # ---------------------------------------------------------------- server

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FakeMachineHost":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -------------------------------------------------------------- controls

    def mint_enroll_token(self) -> str:
        token = secrets.token_urlsafe(16)
        self.enroll_tokens.add(token)
        return token

    def assign(self, workload: str, run: dict[str, Any] | None = None, epoch: int | None = None, keep: list[str] | None = None) -> int:
        with self.lock:
            self.epoch = epoch if epoch is not None else self.epoch + 1
            self.workload = workload
            self.run = run if run is not None else make_run()
            if keep is not None:
                self.keep_images = keep
        return self.epoch

    def unassign(self) -> int:
        with self.lock:
            self.epoch += 1
            self.workload, self.run = None, None
        return self.epoch

    def set_secrets(self, values: dict[str, str]) -> None:
        self.secret_values = dict(values)

    def set_agent(self, version: str, tarball: bytes | None = None, sha: str | None = None) -> None:
        self.agent_version = version
        self.tarball = tarball
        self.tarball_sha = sha or (hashlib.sha256(tarball).hexdigest() if tarball else None)

    def fail_next(self, route: str, status: int = 500, count: int = 1) -> None:
        """The next `count` calls of route ("register", "heartbeat", "start") answer `status`."""
        self._fail.setdefault(route, []).extend([status] * count)

    def drop_response_next(self, route: str, count: int = 1) -> None:
        """Process the next `count` calls of route but close the connection without answering."""
        self._drop[route] = self._drop.get(route, 0) + count

    def machine(self, machine_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            return self.machines[machine_id or next(iter(self.machines))]

    def shipped_logs(self) -> list[dict[str, Any]]:
        with self.lock:
            return [line for hb in self.heartbeats for line in hb.get("logs") or []]

    def wait_for(self, predicate: Callable[[], Any], timeout: float = 10.0, interval: float = 0.02) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() > deadline:
                raise TimeoutError("condition not met within %.1fs" % timeout)
            time.sleep(interval)

    # -------------------------------------------------------------- handlers

    def _answer(self, route: str) -> int | None:
        with self.lock:
            queue = self._fail.get(route)
            return queue.pop(0) if queue else None

    def _should_drop(self, route: str) -> bool:
        with self.lock:
            if self._drop.get(route, 0) > 0:
                self._drop[route] -= 1
                return True
        return False

    def handle_register(self, body: dict[str, Any]) -> tuple[int, Any]:
        with self.lock:
            self.registers.append(body)
            token = secrets.token_urlsafe(24)
            if body.get("enroll_token"):
                if body["enroll_token"] not in self.enroll_tokens:
                    return 401, {"detail": "bad enroll token"}
                self.enroll_tokens.discard(body["enroll_token"])
                mid = "m_" + secrets.token_hex(3)
                self.machines[mid] = {"id": mid, "name": body.get("name"), "token": token, "prev": None, "specs": body.get("specs")}
            else:
                mid = str(body.get("machine_id"))
                m = self.machines.get(mid)
                presented = body.get("machine_token")
                if m is None or presented not in (m["token"], m["prev"]):
                    return 401, {"detail": "unknown machine or bad token"}
                m["prev"], m["token"], m["specs"] = m["token"], token, body.get("specs")
            return 200, {
                "machine_id": mid, "machine_token": token, "heartbeat_seconds": self.heartbeat_seconds,
                "server_time": _now(), "agent_version": self.agent_version,
            }

    def handle_heartbeat(self, machine_id: str, bearer: str, body: dict[str, Any]) -> tuple[int, Any]:
        with self.lock:
            m = self.machines.get(machine_id)
            if m is None or bearer != m["token"]:
                return 401, {"detail": "bad token"}
            m["prev"] = None
            self.heartbeats.append(body)
            self.acked_epoch = max(self.acked_epoch, int(body.get("acked_epoch") or 0))
            self.last_container = body.get("container")
            return 200, {
                "epoch": self.epoch, "workload": self.workload, "run": self.run, "secrets_version": "v1",
                "keep_images": list(self.keep_images), "agent_version": self.agent_version, "server_time": _now(),
                "heartbeat_seconds": self.heartbeat_seconds,
            }

    def handle_start(self, machine_id: str, bearer: str, body: dict[str, Any]) -> tuple[int, Any]:
        with self.lock:
            m = self.machines.get(machine_id)
            if m is None or bearer != m["token"]:
                return 401, {"detail": "bad token"}
            self.starts.append(body)
            if self.workload is None or body.get("epoch") != self.epoch:
                return 409, {"detail": "epoch is not current"}
            token = "run_" + secrets.token_urlsafe(18)
            self.run_tokens.append(token)
            return 200, {"run_token": token, "secrets": dict(self.secret_values)}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _make_handler(host: FakeMachineHost) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _send(self, status: int, payload: Any, ctype: str = "application/json") -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/dl/agent/version":
                if host.tarball is None:
                    return self._send(404, {"detail": "no agent bundle"})
                return self._send(200, {"agent_version": host.agent_version, "sha256": host.tarball_sha})
            if self.path == "/dl/agent.tar.gz":
                if host.tarball is None:
                    return self._send(404, {"detail": "no agent bundle"})
                return self._send(200, host.tarball, "application/gzip")
            self._send(404, {"detail": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"detail": "bad json"})
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            parts = self.path.strip("/").split("/")  # api v1 machines <id|register> [heartbeat|start]
            if parts[:3] != ["api", "v1", "machines"] or len(parts) < 4:
                return self._send(404, {"detail": "not found"})
            route = "register" if parts[3] == "register" else (parts[4] if len(parts) > 4 else "")
            forced = host._answer(route)
            if forced:
                return self._send(forced, {"detail": "injected failure"})
            if route == "register":
                status, payload = host.handle_register(body)
            elif route == "heartbeat":
                status, payload = host.handle_heartbeat(parts[3], bearer, body)
            elif route == "start":
                status, payload = host.handle_start(parts[3], bearer, body)
            else:
                return self._send(404, {"detail": "not found"})
            if host._should_drop(route):
                self.close_connection = True
                self.connection.close()
                return
            self._send(status, payload)

    return Handler
