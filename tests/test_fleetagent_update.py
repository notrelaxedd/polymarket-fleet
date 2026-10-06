"""Self-update and rollback of the agent code (same scheme as the worker)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile

import pytest

from fleetagent import launch, update
from tests.fake_machine_host import FakeMachineHost, build_agent_tarball, make_run
from tests.test_fleetagent_harness import build_rig, host, rig  # noqa: F401 (fixtures)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tar(members: list[tuple[str, bytes | None, str]]) -> bytes:
    """(name, data, kind) with kind file|dir|symlink -> a gzip tarball."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tar.addfile(info)
            else:
                info.size = len(data or b"")
                tar.addfile(info, io.BytesIO(data or b""))
    return buf.getvalue()


def _serve(host: FakeMachineHost, version: str, data: bytes) -> None:
    host.set_agent(version, data)


def test_self_update_installs_verifies_and_swaps_current(tmp_path, host) -> None:
    app = str(tmp_path / "app")
    os.makedirs(app)
    update.install_version(app, "old1", build_agent_tarball("old1"))
    update.swap_current(app, "old1")
    _serve(host, "new2", build_agent_tarball("new2"))
    assert update.self_update(host.url, app, "old1") == "new2"
    assert os.readlink(os.path.join(app, "current")) == "new2"
    assert os.path.isfile(os.path.join(app, "new2", "fleetagent", "__init__.py"))
    assert open(os.path.join(app, "new2", "fleetagent", "VERSION")).read().strip() == "new2"
    assert launch.read_previous(app) == "old1"
    assert launch.read_pending(app) == {"version": "new2", "starts": 0}
    assert update.self_update(host.url, app, "new2") is None  # already running it


def test_self_update_refuses_a_bad_sha256(tmp_path, host) -> None:
    app = str(tmp_path / "app")
    host.set_agent("v9", build_agent_tarball("v9"), sha="0" * 64)
    with pytest.raises(update.UpdateError, match="sha256 mismatch"):
        update.self_update(host.url, app, "old1")
    assert not os.path.exists(os.path.join(app, "v9"))


@pytest.mark.parametrize(
    "members,message",
    [
        ([("fleet/__init__.py", b"", "file")], "unexpected top-level"),
        ([("fleetagent/__init__.py", b"", "file"), ("fleetagent/../../evil.py", b"x", "file")], "unsafe path"),
        ([("/etc/evil", b"x", "file")], "absolute path"),
        ([("fleetagent/__init__.py", b"", "file"), ("fleetagent/link", None, "symlink")], "unsupported member"),
        ([("fleetagent/__init__.py", b"", "file"), ("fleetagent/__pycache__/x.pyc", b"", "file")], "compiled file"),
        ([("fleetagent/other.py", b"", "file")], "no fleetagent/__init__.py"),
        ([("fleetagent2/__init__.py", b"", "file")], "unexpected top-level"),
    ],
)
def test_self_update_validates_every_member(tmp_path, host, members, message) -> None:
    app = str(tmp_path / "app")
    data = _tar(members)
    _serve(host, "evil1", data)
    with pytest.raises(update.UpdateError, match=message):
        update.self_update(host.url, app, "old1")
    assert not os.path.exists(os.path.join(app, "current"))
    leftovers = [n for n in os.listdir(app) if "evil1" in n] if os.path.isdir(app) else []
    assert leftovers == []


def test_self_update_errors_for_a_missing_bundle_a_bad_version_name_and_a_bad_version_list(tmp_path, host) -> None:
    app = str(tmp_path / "app")
    with pytest.raises(update.UpdateError, match="agent/version"):
        update.self_update(host.url, app, "old1")  # the fake host serves no bundle yet
    data = build_agent_tarball("x")
    host.set_agent("../up", data)
    with pytest.raises(update.UpdateError, match="odd name"):
        update.self_update(host.url, app, "old1")
    host.set_agent("bad3", data)
    os.makedirs(app, exist_ok=True)
    launch.mark_bad(app, "bad3")
    with pytest.raises(update.UpdateError, match="bad_versions"):
        update.self_update(host.url, app, "old1")
    with pytest.raises(update.UpdateError):
        update.self_update("http://127.0.0.1:9", app, "old1")


def test_the_supervisor_updates_and_exits_75_without_touching_containers(tmp_path, monkeypatch, host) -> None:
    rig = build_rig(tmp_path, monkeypatch, host, self_update=True, agent_version="old1").boot()
    host.assign("hello", make_run(), epoch=3)
    _serve(host, "new2", build_agent_tarball("new2"))
    rig.tick()
    assert rig.sup.exit_code is None  # a container start was in progress this tick
    assert len(rig.fleet_containers()) == 1
    rig.tick()
    assert rig.sup.exit_code == 75
    assert os.readlink(os.path.join(rig.state_dir, "app", "current")) == "new2"
    assert len(rig.fleet_containers()) == 1 and rig.docker.calls_of("stop") == []


def test_a_failed_update_is_retried_no_sooner_than_a_minute(tmp_path, monkeypatch, host) -> None:
    rig = build_rig(tmp_path, monkeypatch, host, self_update=True, agent_version="old1").boot()
    host.set_agent("new2", build_agent_tarball("new2"), sha="1" * 64)
    rig.tick()
    assert rig.sup.exit_code is None and "sha256 mismatch" in rig.sup.last_error
    host.set_agent("new2", build_agent_tarball("new2"))
    rig.tick(10)
    assert rig.sup.exit_code is None  # inside the 60 s back-off
    rig.tick(61)
    assert rig.sup.exit_code == 75


def test_rollback_after_three_failed_starts(tmp_path) -> None:
    app = str(tmp_path / "state" / "app")
    os.makedirs(app)
    update.install_version(app, "good01", build_agent_tarball("good01"))
    update.swap_current(app, "good01")
    broken = build_agent_tarball("bad02", overrides={"supervisor.py": b"raise RuntimeError('boom at import')\n"})
    update.install_version(app, "bad02", broken)
    launch.record_previous(app, "good01")
    launch.write_pending(app, "bad02", 0)
    update.swap_current(app, "bad02")
    env = {
        **os.environ, "PYTHONPATH": os.path.join(app, "current"), "FLEET_AGENT_STATE_DIR": str(tmp_path / "state"),
        "FLEET_AGENT_RUN_DIR": str(tmp_path / "run"), "FLEET_AGENT_DATA_DIR": str(tmp_path / "data"),
    }
    codes = [
        subprocess.run([sys.executable, "-m", "fleetagent", "run"], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=60).returncode
        for _ in range(3)
    ]
    assert codes == [1, 1, 75]
    assert os.readlink(os.path.join(app, "current")) == "good01"
    assert launch.bad_versions(app) == {"bad02"} and launch.read_pending(app) is None
    with pytest.raises(update.UpdateError, match="bad_versions"):
        host = FakeMachineHost().start()
        try:
            host.set_agent("bad02", broken)
            update.self_update(host.url, app, "good01")
        finally:
            host.stop()


def test_a_successful_register_clears_the_pending_marker(tmp_path, monkeypatch, host) -> None:
    rig = build_rig(tmp_path, monkeypatch, host)
    app = os.path.join(rig.state_dir, "app")
    os.makedirs(app)
    launch.write_pending(app, "v2", 1)
    assert rig.sup.boot() and rig.sup.register_once()
    assert launch.read_pending(app) is None


def test_launch_note_start_counts_only_the_pending_version(tmp_path) -> None:
    app = str(tmp_path)
    update.install_version(app, "v1", build_agent_tarball("v1"))
    update.swap_current(app, "v1")
    assert launch.note_start(app) is None
    launch.write_pending(app, "v1", 0)
    assert launch.note_start(app) == 1 and launch.note_start(app) == 2
    launch.write_pending(app, "other", 5)
    assert launch.note_start(app) is None and launch.read_pending(app) is None
    assert launch.failed_start(app, None) == 1 and launch.failed_start(app, 2) == 1
    assert launch.failed_start(app, 3) == 1  # nothing to roll back to


def test_swap_is_atomic_and_leaves_no_temp_link(tmp_path) -> None:
    app = str(tmp_path)
    for v in ("a1", "b2"):
        update.install_version(app, v, build_agent_tarball(v))
    update.swap_current(app, "a1")
    update.swap_current(app, "b2")
    assert os.readlink(os.path.join(app, "current")) == "b2"
    assert not [n for n in os.listdir(app) if n.startswith(".current")]
    with pytest.raises(update.UpdateError, match="running version"):
        update.install_version(app, "b2", build_agent_tarball("b2"))
