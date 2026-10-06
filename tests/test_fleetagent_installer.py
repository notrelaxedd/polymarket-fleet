"""deploy/install_agent.sh, the systemd unit and repo hygiene for fleetagent/ (stdlib only, no em-dashes)."""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

from tests.fake_machine_host import build_agent_tarball

REPO = pathlib.Path(__file__).resolve().parent.parent
HOST = "https://cpizzle.example.ts.net"
INSTALLER = REPO / "deploy" / "install_agent.sh"
UNIT = REPO / "deploy" / "fleet-agent.service"


def _served_script() -> str:
    return INSTALLER.read_text().replace("__FLEET_HOST_URL__", HOST)


def _run(args: list[str], env_extra: dict[str, str] | None = None, tmp_path: pathlib.Path | None = None, script: str | None = None):
    # A PATH with python3, tar and bash but no systemctl: the script must get past argument
    # parsing and die at the prerequisite check instead of installing anything.
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for tool in ("python3", "tar", "bash", "id", "cat", "tr"):
        real = shutil.which(tool)
        if real and not (bindir / tool).exists():
            (bindir / tool).symlink_to(real)
    env = {"PATH": str(bindir), "HOME": str(tmp_path)}
    env.update(env_extra or {})
    return subprocess.run(["bash", "-s", "--", *args], input=script or _served_script(), text=True, capture_output=True, env=env, timeout=30)


def test_script_is_executable_and_parses() -> None:
    assert INSTALLER.stat().st_mode & 0o111
    assert subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True).returncode == 0


def test_served_installer_accepts_the_real_host_url(tmp_path) -> None:
    proc = _run([HOST, "tok3n"], tmp_path=tmp_path)
    assert "usage:" not in proc.stderr, proc.stderr
    assert "systemctl not found" in proc.stderr or "run as root" in proc.stderr, proc.stderr
    assert proc.returncode == 1


def test_served_installer_accepts_token_from_env_without_url(tmp_path) -> None:
    proc = _run([], env_extra={"FLEET_ENROLL_TOKEN": "tok3n"}, tmp_path=tmp_path)
    assert "usage:" not in proc.stderr, proc.stderr


def test_token_file_name_and_reenroll_options_parse(tmp_path) -> None:
    tfile = tmp_path / "tok"
    tfile.write_text("abc\n")
    proc = _run([HOST, f"--token-file={tfile}", "--name", "box7", "--reenroll"], tmp_path=tmp_path)
    assert "usage:" not in proc.stderr and "unknown option" not in proc.stderr, proc.stderr
    proc = _run([HOST, "--token-file", str(tfile), "--name=box7"], tmp_path=tmp_path)
    assert "usage:" not in proc.stderr and "unknown option" not in proc.stderr, proc.stderr


def test_missing_url_unreadable_token_file_and_unknown_option_are_refused(tmp_path) -> None:
    raw = INSTALLER.read_text()
    assert "usage:" in _run(["tok3n"], tmp_path=tmp_path, script=raw).stderr
    assert "cannot read token file" in _run([HOST, "--token-file", str(tmp_path / "nope")], tmp_path=tmp_path).stderr
    assert "unknown option" in _run([HOST, "--bogus"], tmp_path=tmp_path).stderr
    proc = _run(["--help"], tmp_path=tmp_path)
    assert proc.returncode == 2 and "usage:" in proc.stderr


def test_a_first_install_needs_a_token(tmp_path) -> None:
    proc = _run([HOST], tmp_path=tmp_path)
    assert "an enroll token is required" in proc.stderr and proc.returncode == 1


def test_the_unit_written_by_the_installer_is_the_unit_file() -> None:
    text = INSTALLER.read_text()
    match = re.search(r"cat > \"\$UNIT\" <<'UNITEOF'\n(.*?)\nUNITEOF\n", text, re.S)
    assert match is not None
    assert match.group(1) + "\n" == UNIT.read_text()


def test_unit_has_the_required_settings() -> None:
    unit = UNIT.read_text()
    for line in (
        "User=fleet-agent", "SupplementaryGroups=docker", "StateDirectory=fleet-agent fleet-workloads", "RuntimeDirectory=fleet-agent",
        "RuntimeDirectoryPreserve=yes", "Restart=always", "RestartSec=3", "RestartPreventExitStatus=78", "KillMode=process",
        "ExecStart=/usr/bin/python3 -m fleetagent run", "NoNewPrivileges=yes", "ProtectSystem=strict", "PrivateTmp=yes",
        "Environment=PYTHONPATH=/var/lib/fleet-agent/app/current",
    ):
        assert line in unit.splitlines(), line


def test_installer_never_touches_the_native_worker_or_its_state() -> None:
    text = INSTALLER.read_text()
    assert "fleet-worker" not in text and "/var/lib/fleet/" not in text and "/var/lib/fleet " not in text and '"/var/lib/fleet"' not in text
    assert "install_worker" not in text


def test_docker_is_installed_only_when_missing_and_daemon_json_only_when_absent() -> None:
    text = INSTALLER.read_text()
    assert re.search(r"if ! command -v docker >/dev/null 2>&1; then\n\s+echo \"installing docker.io\"", text)
    assert "apt-get install" in text and "docker.io" in text
    assert 'if [ ! -e "$DAEMON_JSON" ]; then' in text
    body = re.search(r"cat > \"\$DAEMON_JSON\" <<'JSONEOF'\n(.*?)\nJSONEOF", text, re.S).group(1)
    assert json.loads(body) == {"log-driver": "local", "log-opts": {"max-size": "10m", "max-file": "3"}}
    assert text.index('if [ ! -e "$DAEMON_JSON" ]') < text.index("installing docker.io")  # the first daemon start already uses it


def test_embedded_tarball_check_accepts_only_fleetagent_trees(tmp_path) -> None:
    text = INSTALLER.read_text()
    code = re.search(r"# BEGIN tarball-check\n(.*?)# END tarball-check", text, re.S).group(1)
    script = tmp_path / "check.py"
    script.write_text(code)

    def verdict(data: bytes) -> int:
        tarball = tmp_path / "t.tar.gz"
        tarball.write_bytes(data)
        return subprocess.run([sys.executable, str(script), str(tarball)], capture_output=True).returncode

    assert verdict(build_agent_tarball("v1")) == 0
    # a worker tarball (top level fleet/) is not an agent tarball
    from tests.fake_host import build_worker_tarball

    assert verdict(build_worker_tarball("w1")) == 1
    assert verdict(b"not a tarball") == 1


# ------------------------------------------------------------------ hygiene

OWNED = ["fleetagent", "deploy/install_agent.sh", "deploy/fleet-agent.service", ".github/workflows/ci.yml"]


def _owned_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for item in OWNED:
        path = REPO / item
        files += [p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts] if path.is_dir() else [path]
    files += sorted((REPO / "tests").glob("test_fleetagent_*.py")) + [REPO / "tests" / "fake_machine_host.py"]
    return files


def test_no_em_dashes_in_any_owned_file() -> None:
    bad = [str(p.relative_to(REPO)) for p in _owned_files() if chr(0x2014) in p.read_text(encoding="utf-8")]
    assert bad == []


def _imports(path: pathlib.Path) -> set[str]:
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_fleetagent_is_standard_library_only_and_self_contained() -> None:
    allowed = set(sys.stdlib_module_names) | {"fleetagent"}
    for path in (REPO / "fleetagent").glob("*.py"):
        extra = _imports(path) - allowed
        assert not extra, f"{path.name} imports {sorted(extra)}"


def test_modules_stay_around_300_lines_and_have_type_hints() -> None:
    for path in (REPO / "fleetagent").glob("*.py"):
        assert len(path.read_text().splitlines()) <= 330, path.name
        assert "from __future__ import annotations" in path.read_text(), path.name


def test_ci_checks_fleetagent_too() -> None:
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "compileall -q fleet fleetagent" in ci
    assert "fleetagent" in ci.split("Assert")[1]
