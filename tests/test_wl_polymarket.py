"""The polymarket workload (docs/workloads-design.md sections 0, 2 and 7).

- The fleet/ code_version is pinned: changing fleet/ makes every worker self-update.
- workloads/polymarket/workload.toml has exactly the contract's values.
- workloads/polymarket/bootstrap.py against a local fake /dl: fresh install, reuse,
  sha256 and tar-member refusals (the same rules as fleet/worker/update.py), enroll,
  exit 78 without a token, and the final execve.
- The Dockerfile's fixed lines.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

import pytest

from fleet.worker import update as worker_update
from host.bundle import build_bundle, compute_code_version, fleet_package_dir, source_files
from host.workloads.manifest import load_manifest

REPO = Path(__file__).resolve().parent.parent
WORKLOAD = REPO / "workloads" / "polymarket"
PINNED_CODE_VERSION = "dbf3f177ff3a"
EM_DASH = chr(0x2014)
REAL_RUN = subprocess.run


def load_bootstrap() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wl_polymarket_bootstrap", WORKLOAD / "bootstrap.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ in the image context
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


# ------------------------------------------------------------- code version pin


def test_fleet_code_version_is_pinned() -> None:
    version = compute_code_version(source_files(fleet_package_dir()))
    assert version == PINNED_CODE_VERSION, (
        f"fleet/ code_version is {version}, pinned {PINNED_CODE_VERSION}. The worker code_version is a hash "
        "of fleet/*.py (host/bundle.py): any edit under fleet/ makes EVERY worker self-update on its next "
        "heartbeat, live-trading machines included. The workloads change must leave fleet/ byte-identical "
        "(docs/workloads-design.md section 0). Change fleet/ only deliberately, then update this pin."
    )


# --------------------------------------------------------------------- manifest


def test_manifest_has_the_contract_values() -> None:
    m = load_manifest(WORKLOAD)
    assert (m.schema, m.name, m.image, m.protocol) == (1, "polymarket", "fleet/polymarket", "fleet-worker")
    r, rt = m.resources, m.runtime
    assert (r.min_ram_mb, r.min_disk_mb, r.write_heavy) == (3000, 2048, False)
    assert (r.memory_max_pct, r.memory_max_mb, r.cpus) == (85, None, None)
    assert (rt.mode, rt.job_kinds, rt.network, rt.uts_host) == ("service", (), "host", True)
    assert (rt.uid, rt.state_volume, rt.stop_timeout_s, rt.no_restart_exit_codes, rt.nice) == (10001, True, 15, (78,), 5)
    assert m.container_secrets == ("FLEET_ENROLL_TOKEN",) and m.host_only_secrets == ()
    assert m.outbound_actions == () and m.needs_approval is False and m.can_trade is True


def test_dockerfile_fixed_lines() -> None:
    lines = [ln.strip() for ln in (WORKLOAD / "Dockerfile").read_text(encoding="utf-8").splitlines()]
    code = [ln for ln in lines if ln and not ln.startswith("#")]
    assert code[0] == "ARG BASE_IMAGE=debian:trixie-slim" and code[1] == "FROM ${BASE_IMAGE}"
    text = "\n".join(code)
    assert "if ! command -v python3" in text and "apt-get install -y --no-install-recommends python3 ca-certificates tzdata" in text
    assert "useradd --uid 10001" in text and " fleet;" in text
    assert "COPY bootstrap.py /opt/fleet/bootstrap.py" in text and "USER 10001:10001" in text
    assert code[-1] == 'ENTRYPOINT ["python3", "/opt/fleet/bootstrap.py"]'
    assert "COPY fleet" not in text and "pip" not in text, "no Polymarket code in the image"


def test_workload_files_are_stdlib_and_have_no_em_dashes() -> None:
    harness = REPO / "tools" / "workloads"
    for path in sorted(WORKLOAD.rglob("*")) + [Path(__file__), harness / "paper_parity.py"] + sorted(harness.glob("parity/*.py")):
        if path.is_file():
            assert EM_DASH not in path.read_text(encoding="utf-8"), path
    tree = ast.parse((WORKLOAD / "bootstrap.py").read_text(encoding="utf-8"))
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names <= set(sys.stdlib_module_names) | {"__future__"}, names


# ----------------------------------------------------------------- fake /dl host


@dataclass
class FakeDl:
    """GET /dl/version and /dl/worker.tar.gz from memory; counts requests."""

    data: bytes
    version: str
    sha256: str
    requests: list[str] = field(default_factory=list)
    url: str = ""


@pytest.fixture
def fake_dl() -> Iterator[FakeDl]:
    bundle = build_bundle()
    dl = FakeDl(data=bundle.data, version=bundle.code_version, sha256=bundle.sha256)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            dl.requests.append(self.path)
            if self.path == "/dl/version":
                body = json.dumps({"code_version": dl.version, "sha256": dl.sha256}).encode()
            elif self.path == "/dl/worker.tar.gz":
                body = dl.data
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    dl.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield dl
    finally:
        server.shutdown()
        server.server_close()


class Execed(Exception):
    def __init__(self, path: str, argv: list[str], env: dict[str, str]) -> None:
        super().__init__(path)
        self.path, self.argv, self.env = path, argv, env


@dataclass
class Box:
    module: ModuleType
    env: dict[str, str]
    state: Path
    secrets: Path
    enrolls: list[tuple[list[str], dict[str, str]]] = field(default_factory=list)
    enroll_rc: int = 0

    def run(self) -> int | Execed:
        try:
            return self.module.main(self.env)
        except Execed as exc:
            return exc


@pytest.fixture
def box(tmp_path: Path, fake_dl: FakeDl, monkeypatch: pytest.MonkeyPatch) -> Box:
    module = load_bootstrap()
    state, secrets = tmp_path / "state", tmp_path / "secrets"
    state.mkdir()
    secrets.mkdir()
    env = {"FLEET_HOST_URL": fake_dl.url + "/", "FLEET_STATE_DIR": str(state), "FLEET_SECRETS_DIR": str(secrets),
           "FLEET_NICE": "5", "FLEET_RUN_TOKEN": "run-secret", "FLEET_WORKLOAD": "polymarket", "PATH": "/usr/bin:/bin"}
    b = Box(module=module, env=env, state=state, secrets=secrets)

    def fake_run(cmd: list[str], env: dict[str, str], check: bool) -> subprocess.CompletedProcess:
        b.enrolls.append((cmd, env))
        if b.enroll_rc == 0:
            (state / "worker.conf").write_text(json.dumps({"host_url": fake_dl.url, "worker_id": "w1", "worker_token": "t"}))
        return subprocess.CompletedProcess(cmd, b.enroll_rc)

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        raise Execed(path, argv, env)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.os, "execve", fake_execve)
    monkeypatch.setattr(module.os, "nice", lambda inc: 5 if inc else 0)
    return b


def write_token(b: Box, value: str = "enroll-secret\n") -> None:
    (b.secrets / "FLEET_ENROLL_TOKEN").write_text(value)


def test_fresh_install_enroll_and_exec(box: Box, fake_dl: FakeDl, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")  # a dead proxy: the bootstrap must ignore it
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    write_token(box)
    out = box.run()
    assert isinstance(out, Execed), out
    app = box.state / "app"
    assert os.readlink(app / "current") == fake_dl.version
    installed = app / fake_dl.version / "fleet"
    assert (installed / "VERSION").read_text() == fake_dl.version + "\n"
    assert sorted(p.relative_to(installed.parent).as_posix() for p in installed.rglob("*.py")) == \
        sorted("fleet/" + rel for rel, _ in source_files(fleet_package_dir()))
    assert all((p.stat().st_mode & 0o777) == 0o644 for p in installed.rglob("*") if p.is_file())
    assert not (app / ".staging").exists() and not (app / "current.tmp").exists()
    assert fake_dl.requests == ["/dl/version", "/dl/worker.tar.gz"]
    # enroll: the token in the environment only, the host URL without the slash, the hostname as name
    (cmd, env), = box.enrolls
    assert cmd == [sys.executable, "-m", "fleet.worker", "enroll", "--host", fake_dl.url, "--name", os.uname().nodename]
    assert env["FLEET_ENROLL_TOKEN"] == "enroll-secret" and "enroll-secret" not in " ".join(cmd)
    assert env["PYTHONPATH"] == str(app / "current") and env["FLEET_STATE_DIR"] == str(box.state)
    # exec: python3 -m fleet.worker run, no tokens in the agent's environment
    assert out.path == sys.executable and out.argv == [sys.executable, "-m", "fleet.worker", "run"]
    assert out.env["PYTHONPATH"] == str(app / "current") and out.env["FLEET_STATE_DIR"] == str(box.state)
    assert "FLEET_ENROLL_TOKEN" not in out.env and "FLEET_RUN_TOKEN" not in out.env
    assert out.env["FLEET_WORKLOAD"] == "polymarket" and out.env["PATH"] == "/usr/bin:/bin"


def test_installed_tree_imports_with_the_bundle_version(box: Box, fake_dl: FakeDl) -> None:
    write_token(box)
    assert isinstance(box.run(), Execed)
    current = box.state / "app" / "current"
    code = "import sys; sys.path.insert(0, sys.argv[1]); import fleet; print(fleet.__version__)"
    probe = REAL_RUN([sys.executable, "-I", "-c", code, str(current)], capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == fake_dl.version


def test_existing_app_and_conf_are_reused_without_download(box: Box, fake_dl: FakeDl) -> None:
    write_token(box)
    assert isinstance(box.run(), Execed)
    fake_dl.requests.clear()
    (box.secrets / "FLEET_ENROLL_TOKEN").unlink()
    out = box.run()
    assert isinstance(out, Execed) and fake_dl.requests == [] and len(box.enrolls) == 1
    assert out.argv[-2:] == ["fleet.worker", "run"]


def test_existing_app_without_conf_enrolls_only(box: Box, fake_dl: FakeDl) -> None:
    write_token(box)
    assert isinstance(box.run(), Execed)
    (box.state / "worker.conf").unlink()
    fake_dl.requests.clear()
    assert isinstance(box.run(), Execed)
    assert fake_dl.requests == [] and len(box.enrolls) == 2


@pytest.mark.parametrize("token", [None, "", "  \n"])
def test_exit_78_without_token(box: Box, token: str | None) -> None:
    if token is not None:
        write_token(box, token)
    assert box.run() == 78
    assert box.enrolls == [] and not (box.state / "worker.conf").exists()
    assert (box.state / "app" / "current" / "fleet" / "__init__.py").is_file(), "the code is installed first"


def test_enroll_failure_exits_1_and_does_not_exec(box: Box) -> None:
    write_token(box)
    box.enroll_rc = 1
    assert box.run() == 1 and len(box.enrolls) == 1


def test_sha_mismatch_is_refused(box: Box, fake_dl: FakeDl) -> None:
    write_token(box)
    fake_dl.sha256 = "0" * 64
    assert box.run() == 1
    app = box.state / "app"
    assert not (app / "current").exists() and not (app / fake_dl.version).exists() and not (app / ".staging").exists()
    assert box.enrolls == []


def test_odd_version_is_refused(box: Box, fake_dl: FakeDl) -> None:
    fake_dl.version = "../evil"
    assert box.run() == 1 and not (box.state / "app" / "current").exists()


def test_host_down_exits_1(box: Box) -> None:
    box.env["FLEET_HOST_URL"] = "http://127.0.0.1:9"
    assert box.run() == 1


def _tarball(entries: list[tarfile.TarInfo | tuple[str, bytes]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for entry in entries:
            if isinstance(entry, tarfile.TarInfo):
                tar.addfile(entry)
            else:
                info = tarfile.TarInfo(entry[0])
                info.size = len(entry[1])
                tar.addfile(info, io.BytesIO(entry[1]))
    return raw.getvalue()


def _link(name: str, target: str, kind: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    return info


def _dev(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.CHRTYPE
    return info


BASE = [("fleet/__init__.py", b"")]
BAD_TARBALLS = {
    "symlink": BASE + [_link("fleet/x.py", "/etc/passwd", tarfile.SYMTYPE)],
    "hardlink": BASE + [_link("fleet/x.py", "fleet/__init__.py", tarfile.LNKTYPE)],
    "device": BASE + [_dev("fleet/dev")],
    "dotdot": BASE + [("fleet/../evil.py", b"x")],
    "absolute": BASE + [("/fleet/evil.py", b"x")],
    "other_top": BASE + [("other/evil.py", b"x")],
    "pyc": BASE + [("fleet/x.pyc", b"x")],
    "pycache": BASE + [("fleet/__pycache__/x.py", b"x")],
    "no_init": [("fleet/x.py", b"x")],
    "not_gzip": None,
}


@pytest.mark.parametrize("case", sorted(BAD_TARBALLS))
def test_bad_tar_members_are_refused_like_the_agent_does(box: Box, fake_dl: FakeDl, tmp_path: Path, case: str) -> None:
    entries = BAD_TARBALLS[case]
    data = b"not a tarball" if entries is None else _tarball(entries)
    fake_dl.data, fake_dl.sha256 = data, hashlib.sha256(data).hexdigest()
    write_token(box)
    assert box.run() == 1
    app = box.state / "app"
    assert not (app / "current").exists() and not (app / fake_dl.version).exists() and not (app / ".staging").exists()
    with pytest.raises(worker_update.UpdateError):  # the agent's own self-update refuses it too
        worker_update.extract_tarball(data, str(tmp_path / f"agent-{case}"))


def test_nice_is_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_bootstrap()
    calls: list[int] = []
    level = {"now": 2}

    def fake_nice(inc: int) -> int:
        calls.append(inc)
        level["now"] += inc
        return level["now"]

    monkeypatch.setattr(module.os, "nice", fake_nice)
    assert module.apply_nice({"FLEET_NICE": "5"}) == 5 and calls == [0, 3]
    calls.clear()
    assert module.apply_nice({}) == 5 and calls == [0], "already at the default 5: no change"
    assert module.apply_nice({"FLEET_NICE": "bogus"}) == 5


# ------------------------------------------------- the parity harness's normalizer


def load_parity_dump() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wl_parity_dump", REPO / "tools" / "workloads" / "parity" / "dump.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parity_normalizer_maps_identity_and_time_only() -> None:
    dump = load_parity_dump()
    lab = dump.Labels()
    order, crid = "1f0e7a52-58c4-4b9e-9a1d-3d1c2b7f4e10", "2bf44dc1480b147bc44ace15211118d8"
    lab.add(order, "O1")
    lab.add(crid, "crid:O1")
    lab.snapshots[41] = "S[K:sim:g:away|bid=0.64|ask=0.66|abcd]"
    assert lab.value(order) == "O1" and lab.value(f"paper:{crid}") == "paper:crid:O1"
    assert lab.value(f"paper:{order}:41:0") == "paper:O1:S[K:sim:g:away|bid=0.64|ask=0.66|abcd]:0"
    assert lab.value({"at": "2026-10-06T17:36:00+00:00", "order": order}) == {"at": "<time>", "order": "O1"}
    assert lab.value(f"rationale for {order}") == "rationale for O1"
    assert lab.value("my 0.68 vs ask 0.66") == "my 0.68 vs ask 0.66", "trading text is kept"
    assert lab.value(0.683343) == "0.683343" and lab.value(5) == 5


def test_parity_compare_reports_a_real_difference() -> None:
    dump = load_parity_dump()
    a: dict[str, list[dict[str, Any]]] = {t: [] for t in dump.TABLES}
    a["fills"] = [{"label": "F1", "size": 5, "price": "0.66"}]
    b = {t: list(rows) for t, rows in a.items()}
    assert dump.compare(a, b, ("A", "B")) == []
    b["fills"] = [{"label": "F1", "size": 4, "price": "0.66"}]
    diff = dump.compare(a, b, ("A", "B"))
    assert diff[0] == "--- fills: A 1 rows, B 1 rows" and any('"size": 4' in line for line in diff)
