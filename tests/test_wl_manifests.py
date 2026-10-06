"""Workload manifests, the shared SDK copy and the SDK against a fake run-API (docs/workloads-design.md 2, 11).

The Docker test (build the hello image, run it once against the fake API) runs only with
FLEET_WL_DOCKER_TESTS=1.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from host.workloads.manifest import discover, load_manifest, parse_manifest

ROOT = Path(__file__).resolve().parent.parent
WORKLOADS = ROOT / "workloads"
TEMPLATE = WORKLOADS / "_template"
HELLO_MAIN = WORKLOADS / "hello" / "app" / "main.py"
TOKEN = "run-token-test"
FOLDERS = discover(WORKLOADS)


# ---------------------------------------------------------------- manifests and the SDK copy


def test_workloads_are_discovered() -> None:
    assert "hello" in [p.name for p in FOLDERS]
    assert "_template" not in [p.name for p in FOLDERS]


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_manifest_parses(folder: Path) -> None:
    manifest = load_manifest(folder)
    assert manifest.name == folder.name


def test_hello_manifest_matches_the_contract() -> None:
    m = load_manifest(WORKLOADS / "hello")
    assert (m.protocol, m.runtime.mode, m.runtime.job_kinds) == ("workload-v1", "jobs", ("hello",))
    assert (m.resources.min_ram_mb, m.resources.min_disk_mb, m.resources.write_heavy) == (128, 300, False)
    assert m.resources.memory_max_mb == 128
    assert m.container_secrets == ("HELLO_GREETING",) and m.host_only_secrets == ()
    assert m.outbound_actions == ("log",) and m.needs_approval and not m.can_trade


def test_template_manifest_parses_under_its_own_name() -> None:
    data = tomllib.loads((TEMPLATE / "workload.toml").read_text(encoding="utf-8"))
    assert parse_manifest(data, data["name"]).name == data["name"]


def test_template_comments_every_key() -> None:
    lines = (TEMPLATE / "workload.toml").read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            assert lines[i - 1].lstrip().startswith("#"), f"no comment above: {line}"


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_fleet_client_is_byte_identical_to_the_template(folder: Path) -> None:
    copy = folder / "app" / "fleet_client.py"
    if load_manifest(folder).protocol == "fleet-worker":
        pytest.skip("a fleet-worker workload (polymarket) speaks the worker protocol and has no SDK copy")
    assert copy.read_bytes() == (TEMPLATE / "app" / "fleet_client.py").read_bytes()


def test_workload_files_have_no_em_dash_and_sdk_is_stdlib() -> None:
    for path in list(WORKLOADS.glob("_template/**/*")) + list(WORKLOADS.glob("hello/**/*")):
        if path.is_file() and "__pycache__" not in path.parts:
            assert chr(0x2014) not in path.read_text(encoding="utf-8"), path
    sdk = (TEMPLATE / "app" / "fleet_client.py").read_text(encoding="utf-8")
    assert len(sdk.splitlines()) <= 300
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for line in sdk.splitlines():
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__"):
            assert line.split()[1].split(".")[0] in stdlib, line


# ---------------------------------------------------------------- a fake run-API


class FakeHost:
    """The /api/v1/wl routes of the host, just enough for the SDK. Records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.actions: dict[str, dict[str, Any]] = {}
        self.revoked = False  # every request answers 401
        self.cancel = False
        self.cond = threading.Condition()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _reply(self, status: int, body: Any = None) -> None:
                raw = json.dumps(body if body is not None else {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method: str) -> None:
                size = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(size)) if size else None
                if fake.revoked or self.headers.get("Authorization") != "Bearer " + TOKEN:
                    return self._reply(401, {"detail": "invalid run token"})
                with fake.cond:
                    fake.requests.append((method, self.path, body))
                    status, reply = fake.route(method, self.path, body)
                    fake.cond.notify_all()
                self._reply(status, reply)

            def do_POST(self) -> None:
                self._handle("POST")

            def do_GET(self) -> None:
                self._handle("GET")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def route(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        parts = path.strip("/").split("/")  # api v1 wl ...
        if path == "/api/v1/wl/claim":
            return 200, {"job": self.jobs.pop(0) if self.jobs else None}
        if path == "/api/v1/wl/outbound" and method == "POST":
            key = body["dedupe_key"]
            self.actions.setdefault(key, {"id": f"act-{len(self.actions) + 1}", "status": "pending", **body})
            return 200, {"id": self.actions[key]["id"], "status": "pending"}
        if parts[:4] == ["api", "v1", "wl", "outbound"] and method == "GET":
            return 200, {"id": parts[4], "status": "pending", "error": None, "result": None}
        if parts[:4] == ["api", "v1", "wl", "jobs"] and len(parts) == 6:
            if parts[5] == "heartbeat":
                return 200, {"status": "leased", "cancel": self.cancel}
            return 200, {"status": parts[5]}
        return 404, {"detail": "no route"}

    def add_job(self, kind: str = "hello", params: dict[str, Any] | None = None, lease_seconds: float = 30) -> str:
        job_id = str(uuid.uuid4())
        self.jobs.append({"id": job_id, "kind": kind, "params": params or {}, "checkpoint": None, "progress": 0.0,
                          "lease_token": "lease-" + job_id[:8], "lease_seconds": lease_seconds})
        return job_id

    def wait_for(self, predicate: Callable[[list[tuple[str, str, Any]]], bool], timeout: float = 20.0) -> bool:
        with self.cond:
            return self.cond.wait_for(lambda: predicate(self.requests), timeout)

    def calls(self, suffix: str) -> list[Any]:
        with self.cond:
            return [b for m, p, b in self.requests if p.endswith(suffix)]


@pytest.fixture()
def fake() -> Iterator[FakeHost]:
    host = FakeHost()
    yield host
    host.server.shutdown()


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    secrets, scratch = tmp_path / "secrets", tmp_path / "scratch"
    secrets.mkdir()
    scratch.mkdir()
    return secrets, scratch


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]):
    monkeypatch.syspath_prepend(str(TEMPLATE / "app"))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setenv("FLEET_SECRETS_DIR", str(dirs[0]))
    monkeypatch.setenv("FLEET_SCRATCH_DIR", str(dirs[1]))
    sys.modules.pop("fleet_client", None)
    import fleet_client

    yield fleet_client
    sys.modules.pop("fleet_client", None)


def make_client(sdk, fake: FakeHost):
    return sdk.Client(fake.url, TOKEN, "hello", "m_abc123", "4")


def spawn_hello(fake: FakeHost, dirs: tuple[Path, Path], **env: str) -> subprocess.Popen:
    full = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    full.update(PYTHONDONTWRITEBYTECODE="1", FLEET_HOST_URL=fake.url, FLEET_RUN_TOKEN=TOKEN, FLEET_WORKLOAD="hello", FLEET_MACHINE_ID="m_abc123",
               FLEET_EPOCH="4", FLEET_SECRETS_DIR=str(dirs[0]), FLEET_SCRATCH_DIR=str(dirs[1]), **env)
    return subprocess.Popen([sys.executable, str(HELLO_MAIN)], env=full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def finish(proc: subprocess.Popen, timeout: float = 15.0) -> tuple[int, str]:
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        pytest.fail("hello did not exit:\n" + out)
    return proc.returncode, out


# ---------------------------------------------------------------- SDK unit tests


def test_secret_reads_file_or_none(sdk, fake: FakeHost, dirs) -> None:
    (dirs[0] / "HELLO_GREETING").write_text("Ahoy\n")
    client = make_client(sdk, fake)
    assert client.secret("HELLO_GREETING") == "Ahoy"
    assert client.secret("NOPE") is None


def test_claim_progress_renew_and_complete(sdk, fake: FakeHost, dirs) -> None:
    client = make_client(sdk, fake)
    assert client.claim(["hello"]) is None
    job_id = fake.add_job(params={"steps": 2}, lease_seconds=0.6)  # renews every 0.2 s
    job = client.claim(["hello"])
    assert job is not None and job.id == job_id and job.params == {"steps": 2}
    assert fake.calls("/claim")[-1] == {"kinds": ["hello"]}
    assert job.scratch == dirs[1] / job_id and job.scratch.is_dir()
    job.progress(0.5, {"step": 1})
    beat = fake.calls("/heartbeat")[0]
    assert beat == {"lease_token": job.lease_token, "progress": 0.5, "checkpoint": {"step": 1}}
    before = len(fake.calls("/heartbeat"))
    assert fake.wait_for(lambda reqs: len([r for r in reqs if r[1].endswith("/heartbeat")]) >= before + 2, 5)  # thread renews
    job.complete({"ok": True})
    assert fake.calls("/complete") == [{"lease_token": job.lease_token, "result": {"ok": True}}]
    assert not job.scratch.exists()
    time.sleep(0.5)
    n = len(fake.calls("/heartbeat"))
    time.sleep(0.5)
    assert len(fake.calls("/heartbeat")) == n  # renewing stopped


def test_release_and_fail_delete_scratch(sdk, fake: FakeHost) -> None:
    client = make_client(sdk, fake)
    fake.add_job()
    fake.add_job()
    first, second = client.claim(["hello"]), client.claim(["hello"])
    first.release("because")
    second.fail("boom")
    assert fake.calls("/release")[0]["reason"] == "because"
    assert fake.calls("/fail")[0]["error"] == "boom"
    assert not first.scratch.exists() and not second.scratch.exists()


def test_cancel_flag_from_heartbeat(sdk, fake: FakeHost) -> None:
    client = make_client(sdk, fake)
    fake.add_job()
    job = client.claim(["hello"])
    fake.cancel = True
    job.progress(0.1)
    assert job.cancelled.is_set()
    job.release("cancelled")


def test_outbound_and_status(sdk, fake: FakeHost) -> None:
    client = make_client(sdk, fake)
    fake.add_job()
    job = client.claim(["hello"])
    first = client.outbound("log", {"message": "hi"}, "k1", job)
    again = client.outbound("log", {"message": "hi"}, "k1", job)
    assert first == again == {"id": "act-1", "status": "pending"}
    body = fake.calls("/outbound")[0]
    assert body == {"kind": "log", "payload": {"message": "hi"}, "dedupe_key": "k1", "job_id": job.id}
    assert client.outbound_status("act-1")["status"] == "pending"
    job.release()


def test_401_raises_unauthorized_and_409_lease_lost(sdk, fake: FakeHost) -> None:
    client = make_client(sdk, fake)
    fake.revoked = True
    with pytest.raises(sdk.Unauthorized):
        client.claim(["hello"])
    fake.revoked = False
    client = make_client(sdk, fake)  # a 401 is remembered by the client that saw it
    fake.add_job()
    job = client.claim(["hello"])
    job.lost.set()
    with pytest.raises(sdk.LeaseLost):
        job.progress(0.2)
    job.release()  # a lost job is not reported again, scratch is still removed
    assert fake.calls("/release") == [] and not job.scratch.exists()


def test_run_forever_returns_zero_on_401(sdk, fake: FakeHost) -> None:
    fake.revoked = True
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        assert sdk.run_forever({"hello": lambda job: {}}, idle_sleep=0.1, client=make_client(sdk, fake)) == 0
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


# ---------------------------------------------------------------- the hello app in a subprocess


def test_hello_runs_a_job_with_secret_and_notify(fake: FakeHost, dirs) -> None:
    (dirs[0] / "HELLO_GREETING").write_text("Ahoy")
    job_id = fake.add_job(params={"name": "Ada", "steps": 3, "notify": True})
    proc = spawn_hello(fake, dirs)
    assert fake.wait_for(lambda reqs: any(p.endswith("/complete") for _, p, _ in reqs))
    proc.send_signal(signal.SIGTERM)
    code, out = finish(proc)
    assert code == 0
    result = fake.calls("/complete")[0]["result"]
    assert result == {"greeting": "Ahoy, Ada!", "machine": "m_abc123", "epoch": 4, "steps": 3, "outbound_id": "act-1"}
    assert fake.actions[f"hello:{job_id}"]["kind"] == "log"
    assert len(fake.calls("/heartbeat")) >= 3
    assert not (dirs[1] / job_id).exists()
    assert "Ahoy" not in out and TOKEN not in out


def test_hello_default_greeting_and_bad_params_fail_the_job(fake: FakeHost, dirs) -> None:
    fake.add_job(params={})
    fake.add_job(params={"steps": 99})
    proc = spawn_hello(fake, dirs)
    assert fake.wait_for(lambda reqs: any(p.endswith("/fail") for _, p, _ in reqs))
    proc.send_signal(signal.SIGTERM)
    assert finish(proc)[0] == 0
    assert fake.calls("/complete")[0]["result"]["greeting"] == "Hello, world!"
    assert "steps must be an integer" in fake.calls("/fail")[0]["error"]


def test_sigterm_mid_job_releases_with_reason_shutdown(fake: FakeHost, dirs) -> None:
    job_id = fake.add_job(params={"steps": 40})
    proc = spawn_hello(fake, dirs, HELLO_STEP_SECONDS="0.3")
    assert fake.wait_for(lambda reqs: sum(p.endswith("/heartbeat") for _, p, _ in reqs) >= 2)
    assert (dirs[1] / job_id).is_dir()
    proc.send_signal(signal.SIGTERM)
    code, _ = finish(proc)
    assert code == 0
    release = fake.calls("/release")
    assert len(release) == 1 and release[0]["reason"] == "shutdown"
    assert release[0]["checkpoint"]["step"] >= 2 and 0 < release[0]["progress"] < 1
    assert fake.calls("/complete") == [] and not (dirs[1] / job_id).exists()


def test_401_mid_job_exits_zero_and_releases_nothing(fake: FakeHost, dirs) -> None:
    fake.add_job(params={"steps": 40})
    proc = spawn_hello(fake, dirs, HELLO_STEP_SECONDS="0.3")
    assert fake.wait_for(lambda reqs: sum(p.endswith("/heartbeat") for _, p, _ in reqs) >= 1)
    fake.revoked = True
    code, _ = finish(proc)
    assert code == 0
    assert fake.calls("/release") == [] and fake.calls("/complete") == [] and not any(dirs[1].iterdir())


def test_401_at_start_exits_zero(fake: FakeHost, dirs) -> None:
    fake.revoked = True
    code, out = finish(spawn_hello(fake, dirs))
    assert code == 0 and "no longer valid" in out


# ---------------------------------------------------------------- Docker (FLEET_WL_DOCKER_TESTS=1)


@pytest.mark.skipif(os.environ.get("FLEET_WL_DOCKER_TESTS") != "1", reason="set FLEET_WL_DOCKER_TESTS=1 to build and run the image")
def test_hello_image_runs_once_against_the_fake_api(fake: FakeHost, tmp_path: Path) -> None:
    prefix = os.environ.get("FLEET_WL_DOCKER_PREFIX", "hello-builder")
    image, name = f"{prefix}-hello:test", f"{prefix}-hello-{uuid.uuid4().hex[:6]}"
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o755)
    (secrets / "HELLO_GREETING").write_text("Ahoy")
    (secrets / "HELLO_GREETING").chmod(0o444)
    secrets.chmod(0o755)
    try:
        subprocess.run(["docker", "build", "-q", "-t", image, str(WORKLOADS / "hello")], check=True, capture_output=True, timeout=300)
        job_id = fake.add_job(params={"name": "Docker", "steps": 3, "notify": True})
        run = subprocess.run(
            ["docker", "run", "-d", "--name", name, "--network", "host", "--read-only", "--user", "10001:10001",
             "--tmpfs", "/scratch:rw,mode=1777,size=16m", "-v", f"{secrets}:/run/fleet/secrets:ro",
             "-e", f"FLEET_HOST_URL={fake.url}", "-e", f"FLEET_RUN_TOKEN={TOKEN}", "-e", "FLEET_WORKLOAD=hello",
             "-e", "FLEET_MACHINE_ID=m_abc123", "-e", "FLEET_EPOCH=7", image],
            capture_output=True, text=True, timeout=60)
        assert run.returncode == 0, run.stderr
        assert fake.wait_for(lambda reqs: any(p.endswith("/complete") for _, p, _ in reqs), 40)
        result = fake.calls("/complete")[0]["result"]
        assert result == {"greeting": "Ahoy, Docker!", "machine": "m_abc123", "epoch": 7, "steps": 3, "outbound_id": "act-1"}
        assert fake.actions[f"hello:{job_id}"]["kind"] == "log"
        stop = subprocess.run(["docker", "stop", "-t", "10", name], capture_output=True, text=True, timeout=30)
        assert stop.returncode == 0
        info = subprocess.run(["docker", "inspect", "-f", "{{.State.ExitCode}}", name], capture_output=True, text=True)
        assert info.stdout.strip() == "0"
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True).stdout
        assert "Ahoy" not in logs and TOKEN not in logs
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "rmi", "-f", image], capture_output=True)
