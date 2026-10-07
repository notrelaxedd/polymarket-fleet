"""tools/host/autoupdate.sh against a local git remote, with fake docker and curl on PATH:
fast-forward and redeploy, the exchange postponed while games are live, rollback on a failed
health check, the hold file. No database, no Docker."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "host" / "autoupdate.sh"
INSTALLER = REPO / "tools" / "host" / "install_autoupdate.sh"

FAKE_DOCKER = """#!/bin/bash
echo "$*" >> "$FAKE_LOG"
case "$*" in
  "compose config --services") printf 'db\\nhost\\nexchange\\nregistry\\n' ;;
  "compose exec -T db psql"*) [ "${FAKE_DB_DOWN:-0}" = 1 ] && exit 2; echo "${FAKE_BUSY:-0}" ;;
esac
exit 0
"""
FAKE_CURL = """#!/bin/bash
[ -e "$FAKE_UNHEALTHY" ] && exit 22
echo '{"ok": true, "db": true}'
"""

pytestmark = pytest.mark.skipif(not (shutil.which("git") and shutil.which("flock")), reason="needs git and flock")


def _git(cwd: pathlib.Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


class Box:
    """A bare 'GitHub' remote, a host checkout of it, and the fakes."""

    def __init__(self, tmp: pathlib.Path) -> None:
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.dev = tmp / "dev"
        self.host = tmp / "host"
        self.state = tmp / "state"
        self.log = tmp / "docker.log"
        self.unhealthy = tmp / "unhealthy"
        bindir = tmp / "bin"
        bindir.mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o755)
        self.path = f"{bindir}:{os.environ['PATH']}"
        _git(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        _git(tmp, "clone", "-q", str(self.origin), str(self.dev))
        _git(self.dev, "checkout", "-q", "-b", "main")
        (self.dev / "tools" / "host").mkdir(parents=True)
        shutil.copy(SCRIPT, self.dev / "tools" / "host" / "autoupdate.sh")
        self.commit("first")
        _git(tmp, "clone", "-q", str(self.origin), str(self.host))

    def commit(self, msg: str) -> str:
        (self.dev / "f.txt").write_text(msg)
        _git(self.dev, "add", "-A")
        _git(self.dev, "commit", "-q", "-m", msg)
        _git(self.dev, "push", "-q", "origin", "main")
        return _git(self.dev, "rev-parse", "HEAD")

    def run(self, *args: str, busy: int = 0, db_down: bool = False) -> subprocess.CompletedProcess[str]:
        if self.log.exists():
            self.log.unlink()
        env = {**os.environ, "PATH": self.path, "FAKE_LOG": str(self.log), "FAKE_BUSY": str(busy),
               "FAKE_DB_DOWN": "1" if db_down else "0", "FAKE_UNHEALTHY": str(self.unhealthy),
               "FLEET_AUTOUPDATE_STATE": str(self.state), "FLEET_HEALTH_TRIES": "2", "FLEET_HEALTH_DELAY": "0"}
        return subprocess.run(["bash", str(self.host / "tools" / "host" / "autoupdate.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def ups(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if line.startswith("compose up")]

    def head(self) -> str:
        return _git(self.host, "rev-parse", "HEAD")

    def state_of(self, name: str) -> str:
        p = self.state / name
        return p.read_text().strip() if p.exists() else ""


@pytest.fixture()
def box(tmp_path: pathlib.Path) -> Box:
    return Box(tmp_path)


def test_scripts_are_executable_parse_and_have_no_em_dashes() -> None:
    for script in (SCRIPT, INSTALLER):
        assert script.stat().st_mode & 0o111
        assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
        assert chr(0x2014) not in script.read_text()


def test_first_pass_deploys_everything_then_idles(box: Box) -> None:
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert box.ups() == ["compose up -d --build"]
    assert box.state_of("host") == box.state_of("exchange") == box.head()
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert box.ups() == []


def test_new_commit_is_pulled_and_deployed(box: Box) -> None:
    box.run()
    new = box.commit("second")
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert box.head() == new
    assert box.ups() == ["compose up -d --build"]
    assert "pulled" in r.stdout and "deployed" in r.stdout


def test_exchange_waits_while_games_are_live(box: Box) -> None:
    box.run()
    old = box.head()
    new = box.commit("second")
    r = box.run(busy=2)
    assert r.returncode == 0, r.stderr
    assert box.ups() == ["compose up -d --build db host registry"]
    assert "postponed" in r.stdout
    assert box.state_of("host") == new and box.state_of("exchange") == old
    r = box.run(busy=1)  # still live: nothing to redo for host, exchange still waits
    assert box.ups() == []
    r = box.run(busy=0)  # games over: the exchange follows
    assert r.returncode == 0, r.stderr
    assert box.ups() == ["compose up -d --build"]
    assert box.state_of("exchange") == new


def test_unreachable_database_counts_as_busy(box: Box) -> None:
    box.run()
    box.commit("second")
    r = box.run(db_down=True)
    assert r.returncode == 0, r.stderr
    assert box.ups() == ["compose up -d --build db host registry"]


def test_force_exchange_ignores_live_games(box: Box) -> None:
    box.run()
    new = box.commit("second")
    r = box.run("--force-exchange", busy=3)
    assert r.returncode == 0, r.stderr
    assert box.ups() == ["compose up -d --build"]
    assert box.state_of("exchange") == new


def test_unhealthy_commit_is_rolled_back_and_skipped(box: Box) -> None:
    box.run()
    good = box.head()
    bad = box.commit("broken")
    box.unhealthy.touch()
    r = box.run()
    assert r.returncode != 0
    assert "rolled back" in r.stderr
    assert box.head() == good
    assert box.state_of("bad") == bad and box.state_of("host") == good
    assert box.ups() == ["compose up -d --build", "compose up -d --build"]
    box.unhealthy.unlink()
    r = box.run()  # the bad commit is not pulled again
    assert r.returncode == 0, r.stderr
    assert box.head() == good and box.ups() == []
    fixed = box.commit("fixed")
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert box.head() == fixed and box.state_of("host") == fixed


def test_hold_file_pauses_updates(box: Box) -> None:
    box.run()
    old = box.head()
    box.commit("second")
    (box.state / "hold").touch()
    r = box.run()
    assert r.returncode == 0 and "paused" in r.stdout
    assert box.head() == old and box.ups() == []


def test_local_edits_block_the_pull(box: Box) -> None:
    box.run()
    box.commit("second")
    (box.host / "f.txt").write_text("edited on the host")
    r = box.run()
    assert r.returncode != 0
    assert "cannot fast-forward" in r.stderr
    assert box.ups() == []
