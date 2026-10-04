"""The served installer (placeholder substituted) must accept the real host URL."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent
HOST = "https://cpizzle.example.ts.net"


def _served_script() -> str:
    text = (REPO / "deploy" / "install_worker.sh").read_text()
    return text.replace("__FLEET_HOST_URL__", HOST)


def _run(args: list[str], env_extra: dict[str, str] | None = None, tmp_path: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    # A PATH with python3, tar and bash but no systemctl: the script must get past argument
    # parsing and die at the prerequisite check instead of installing anything.
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for tool in ("python3", "tar", "bash", "id", "cat", "tr"):
        real = shutil.which(tool)
        if real and not (bindir / tool).exists():
            (bindir / tool).symlink_to(real)
    env = {"PATH": str(bindir), "HOME": str(tmp_path)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", "-s", "--", *args], input=_served_script(), text=True, capture_output=True, env=env, timeout=30)


def test_served_installer_accepts_the_real_host_url(tmp_path) -> None:
    proc = _run([HOST, "tok3n"], tmp_path=tmp_path)
    assert "usage:" not in proc.stderr, proc.stderr
    assert "systemctl not found" in proc.stderr or "run as root" in proc.stderr, proc.stderr


def test_served_installer_accepts_token_from_env_without_url(tmp_path) -> None:
    proc = _run([], env_extra={"FLEET_ENROLL_TOKEN": "tok3n"}, tmp_path=tmp_path)
    assert "usage:" not in proc.stderr, proc.stderr


def test_served_installer_still_rejects_a_missing_url_in_the_raw_script(tmp_path) -> None:
    raw = (REPO / "deploy" / "install_worker.sh").read_text()
    proc = subprocess.run(["bash", "-s", "--", "tok3n"], input=raw, text=True, capture_output=True, timeout=30)
    assert "usage:" in proc.stderr
