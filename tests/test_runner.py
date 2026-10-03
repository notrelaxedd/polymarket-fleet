"""Runner tests: child JSONL contract and the parent-side Runner handle."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fleet.worker.runner import MODULE, Runner, child_env


def _wait_finished(runner: Runner, timeout: float) -> None:
    assert runner.wait(timeout), "runner did not finish in time"


def _run_child(stdin: bytes, timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", MODULE], input=stdin, capture_output=True, env=child_env(), timeout=timeout,
    )


def test_sleep_job_emits_checkpoints_and_completes() -> None:
    runner = Runner({"id": "j1", "kind": "sleep", "params": {"seconds": 2}, "checkpoint": None})
    runner.start()
    _wait_finished(runner, 10.0)
    assert runner.outcome == "done"
    assert runner.result == {"slept": 2}
    checkpoint, progress, seq = runner.snapshot()
    assert checkpoint == {"elapsed": 2}
    assert progress == 1.0
    assert seq == 2
    assert runner.poll() == 0


def test_sleep_job_resumes_from_checkpoint() -> None:
    runner = Runner({"id": "j2", "kind": "sleep", "params": {"seconds": 3}, "checkpoint": {"elapsed": 2}})
    started = time.monotonic()
    runner.start()
    _wait_finished(runner, 10.0)
    assert time.monotonic() - started < 2.5
    assert runner.outcome == "done"
    assert runner.result == {"slept": 3}
    assert runner.snapshot()[0] == {"elapsed": 3}


def test_sigterm_mid_way_yields_stopped_with_last_checkpoint() -> None:
    runner = Runner({"id": "j3", "kind": "sleep", "params": {"seconds": 10}, "checkpoint": None})
    runner.start()
    deadline = time.monotonic() + 5.0
    while runner.snapshot()[2] < 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert runner.snapshot()[0] == {"elapsed": 1}
    started = time.monotonic()
    runner.stop(grace=3.0)
    assert time.monotonic() - started < 2.0
    assert runner.outcome == "stopped"
    assert runner.poll() == 0
    assert not runner.kill_sent
    checkpoint, progress, _ = runner.snapshot()
    assert checkpoint["elapsed"] >= 1
    assert 0.0 < progress < 1.0


def test_sigkill_after_grace_when_sigterm_ignored() -> None:
    stubborn = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    runner = Runner({"id": "j4", "kind": "sleep", "params": {}}, command=[sys.executable, "-c", stubborn])
    runner.start()
    time.sleep(0.3)
    started = time.monotonic()
    runner.stop(grace=0.5)
    assert time.monotonic() - started < 3.0
    assert runner.kill_sent
    assert runner.poll() == -signal.SIGKILL
    assert runner.outcome == "crashed"


def test_malformed_job_prints_error_line_and_exits_zero() -> None:
    proc = _run_child(b"this is not json")
    assert proc.returncode == 0
    lines = [json.loads(line) for line in proc.stdout.decode().splitlines() if line.strip()]
    assert len(lines) == 1
    assert "malformed job" in lines[0]["error"]


def test_unknown_kind_prints_error_line() -> None:
    proc = _run_child(json.dumps({"id": "x", "kind": "nope", "params": {}}).encode())
    assert proc.returncode == 0
    lines = [json.loads(line) for line in proc.stdout.decode().splitlines() if line.strip()]
    assert lines == [{"error": "unknown job kind: 'nope'"}]


def test_child_stdout_matches_contract_exactly() -> None:
    proc = _run_child(json.dumps({"id": "x", "kind": "sleep", "params": {"seconds": 1}, "checkpoint": None}).encode())
    assert proc.returncode == 0
    lines = [json.loads(line) for line in proc.stdout.decode().splitlines() if line.strip()]
    assert lines == [{"checkpoint": {"elapsed": 1}, "progress": 1.0}, {"done": True, "result": {"slept": 1}}]


def test_child_env_sets_omp_threads_and_keeps_pythonpath() -> None:
    env = child_env({"PYTHONPATH": "/opt/extra", "PATH": os.environ.get("PATH", "")})
    assert env["OMP_NUM_THREADS"] == "1"
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert "/opt/extra" in parts
    assert any(os.path.isdir(os.path.join(p, "fleet")) for p in parts)


@pytest.mark.parametrize("seconds", [0])
def test_zero_second_sleep_is_done_immediately(seconds: int) -> None:
    runner = Runner({"id": "j5", "kind": "sleep", "params": {"seconds": seconds}})
    runner.start()
    _wait_finished(runner, 10.0)
    assert runner.outcome == "done"
    assert runner.result == {"slept": 0}


# ------------------------------------------------- review fixes: process group


def _alive(pid: int) -> bool:
    """True while the process exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state not in ("Z", "X")


# The grandchild ignores SIGTERM, keeps the inherited stdout pipe open and writes its
# pid only once its handler is installed (so the test cannot signal it too early).
GRANDCHILD = (
    "import signal, subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import os, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "open(sys.argv[1], \"w\").write(str(os.getpid())); time.sleep(60)', sys.argv[1]])\n"
)


def _wait_pid_file(path) -> int:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            text = path.read_text()
            if text:
                return int(text)
        except OSError:
            pass
        time.sleep(0.02)
    raise AssertionError("grandchild pid file never appeared")


def _wait_dead(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return False


def test_stop_kills_grandchildren_when_child_ignores_sigterm(tmp_path) -> None:
    pid_file = tmp_path / "pid"
    stubborn = GRANDCHILD + "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"
    runner = Runner({"id": "g1", "kind": "sleep", "params": {}}, command=[sys.executable, "-c", stubborn, str(pid_file)])
    runner.start()
    grandchild = _wait_pid_file(pid_file)
    assert _alive(grandchild)
    assert os.getpgid(runner.pid) == runner.pid, "runner child must lead its own process group"
    started = time.monotonic()
    runner.stop(grace=0.5)
    elapsed = time.monotonic() - started
    assert runner.kill_sent
    assert runner.poll() == -signal.SIGKILL
    assert _wait_dead(grandchild), "grandchild survived the stop"
    assert elapsed < 2.5, f"stop took {elapsed:.2f}s"


def test_stop_kills_lingering_grandchild_when_child_exits(tmp_path) -> None:
    """The child honours SIGTERM but its grandchild (holding stdout) does not: the group
    is killed and the stop does not block on the reader."""
    pid_file = tmp_path / "pid"
    polite = GRANDCHILD + "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\ntime.sleep(60)\n"
    runner = Runner({"id": "g2", "kind": "sleep", "params": {}}, command=[sys.executable, "-c", polite, str(pid_file)])
    runner.start()
    grandchild = _wait_pid_file(pid_file)
    started = time.monotonic()
    runner.stop(grace=3.0)
    elapsed = time.monotonic() - started
    assert runner.poll() == 0
    assert not runner.kill_sent
    assert runner.group_killed
    assert _wait_dead(grandchild), "lingering grandchild survived"
    assert elapsed < 2.0, f"stop took {elapsed:.2f}s"


def test_kill_after_child_exit_does_not_raise() -> None:
    runner = Runner({"id": "g3", "kind": "sleep", "params": {"seconds": 0}})
    runner.start()
    _wait_finished(runner, 10.0)
    runner.kill()
    runner.terminate()
    runner.reap_group()
    assert runner.outcome == "done"


def test_resumed_job_reports_stored_progress_before_first_unit() -> None:
    runner = Runner({"id": "p1", "kind": "sleep", "params": {"seconds": 4}, "checkpoint": {"elapsed": 3}, "progress": 0.75})
    assert runner.snapshot()[1] == 0.75
    assert Runner({"id": "p2", "kind": "sleep", "params": {}, "progress": "bad"}).snapshot()[1] == 0.0
    assert Runner({"id": "p3", "kind": "sleep", "params": {}}).snapshot()[1] == 0.0


# ------------------------------------------------- step 2: session RSS sum


# The child forks a grandchild that holds ~30 MB resident (filled, not lazily zeroed)
# and writes "ready" once the allocation is done; both then sleep.
RSS_TREE = (
    "import os, sys, time\n"
    "if os.fork() == 0:\n"
    "    block = b'x' * (30 * 1024 * 1024)\n"
    "    open(sys.argv[1], 'w').write('ready')\n"
    "    time.sleep(60)\n"
    "time.sleep(60)\n"
)


def _wait_file(path, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = path.read_text()
            if text:
                return text
        except OSError:
            pass
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


def test_session_rss_sum_includes_grandchild_and_excludes_strangers(tmp_path) -> None:
    from fleet.common import sysinfo

    ready = tmp_path / "ready"
    runner = Runner({"id": "m1", "kind": "sleep", "params": {}}, command=[sys.executable, "-c", RSS_TREE, str(ready)])
    runner.start()
    stranger_ready = tmp_path / "stranger"
    stranger = subprocess.Popen(
        [sys.executable, "-c", RSS_TREE.replace("30 * 1024", "100 * 1024"), str(stranger_ready)],
        start_new_session=True,
    )
    try:
        _wait_file(ready)
        _wait_file(stranger_ready)
        pids = sysinfo.session_pids(runner.pid)
        assert runner.pid in pids and len(pids) == 2, pids
        assert os.getpid() not in pids and stranger.pid not in pids
        total_kb = sysinfo.session_rss_kb(runner.pid)
        assert total_kb >= 30 * 1024, f"grandchild's 30 MB missing from {total_kb} kB"
        assert total_kb < 100 * 1024, f"unrelated 100 MB process counted: {total_kb} kB"
        assert sysinfo.session_rss_kb(stranger.pid) >= 100 * 1024
    finally:
        runner.stop(grace=0.5)
        stranger.kill()
        stranger.wait(5.0)
    # The orphaned grandchild is reaped by PID 1 asynchronously; a zombie is not counted.
    deadline = time.monotonic() + 3.0
    while sysinfo.session_pids(runner.pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sysinfo.session_pids(runner.pid) == []
    assert sysinfo.session_rss_kb(runner.pid) == 0


def test_session_rss_parses_stat_after_last_paren_and_skips_vanished(tmp_path) -> None:
    from fleet.common import sysinfo

    proc = tmp_path / "proc"
    for pid, comm, sid, rss in ((10, "(a b) (c)", 10, 1000), (11, "python", 10, 500), (12, "python", 99, 7000)):
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "stat").write_text(f"{pid} ({comm}) S 1 {pid} {sid} 0 -1 4194304 0 0 0 0 0 0 0 0 20 0 1 0 1 1 1 0\n")
        (d / "status").write_text(f"Name:\t{comm}\nVmRSS:\t    {rss} kB\nThreads:\t1\n")
    (proc / "13").mkdir()
    (proc / "13" / "stat").write_text("13 (dead) S 1 13 10 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 1 1 1 0\n")
    (proc / "14").mkdir()
    (proc / "14" / "stat").write_text("14 (zombie) Z 1 14 10 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 1 1 1 0\n")
    (proc / "14" / "status").write_text("Name:\tzombie\nVmRSS:\t    9999 kB\n")
    (proc / "self").mkdir()
    (proc / "meminfo").write_text("MemTotal: 1 kB\n")
    assert sysinfo.session_pids(10, str(proc)) == [10, 11, 13], "13 has no status file (vanished), 14 is a zombie"
    assert sysinfo.session_rss_kb(10, str(proc)) == 1500
    assert sysinfo.session_rss_kb(99, str(proc)) == 7000
    assert sysinfo.session_rss_kb(5, str(proc)) == 0
    assert sysinfo.session_rss_kb(10, str(tmp_path / "missing")) == 0


RSS_FORK_TREE = r"""
import os, sys, time
buf = bytearray(120 * 1024 * 1024)
for i in range(0, len(buf), 4096):
    buf[i] = 1
for _ in range(3):
    if os.fork() == 0:
        time.sleep(60)
        os._exit(0)
open(sys.argv[1], "w").write("ready")
time.sleep(60)
"""


def test_session_memory_counts_shared_pages_once(tmp_path) -> None:
    """MEDIUM: forked children share their parent's buffer copy-on-write. The watchdog
    must measure the session's real footprint (proportional, anonymous memory), not
    VmRSS summed per process, which counts the buffer once per child."""
    from fleet.common import sysinfo

    if not Path("/proc/self/smaps_rollup").exists():
        pytest.skip("no smaps_rollup on this kernel")
    ready = tmp_path / "ready"
    runner = Runner({"id": "m2", "kind": "sleep", "params": {}}, command=[sys.executable, "-c", RSS_FORK_TREE, str(ready)])
    runner.start()
    try:
        _wait_file(ready)
        pids = sysinfo.session_pids(runner.pid)
        assert len(pids) == 4, pids
        measured_kb = sysinfo.session_rss_kb(runner.pid)
        vmrss_kb = sum(sysinfo._kb_fields(f"/proc/{pid}/status", ("VmRSS",)).get("VmRSS", 0) for pid in pids)
    finally:
        runner.stop(grace=0.5)
    buffer_kb = 120 * 1024
    assert measured_kb >= buffer_kb, f"the buffer itself is missing from {measured_kb} kB"
    assert measured_kb < 2 * buffer_kb, f"shared pages counted more than once: {measured_kb} kB"
    assert vmrss_kb > 3 * buffer_kb, f"VmRSS sum should overstate (sanity): {vmrss_kb} kB"


def test_process_mem_prefers_rollup_then_status(tmp_path) -> None:
    from fleet.common import sysinfo

    d = tmp_path / "p"
    d.mkdir()
    (d / "status").write_text("Name:\tx\nVmRSS:\t    5000 kB\nRssAnon:\t    1200 kB\nRssFile:\t    3000 kB\nRssShmem:\t    300 kB\n")
    assert sysinfo.process_mem_kb(str(d)) == 1500, "RssAnon + RssShmem when there is no smaps_rollup"
    (d / "smaps_rollup").write_text("Rss:  5000 kB\nPss:  2000 kB\nPss_Anon:  700 kB\nPss_File:  1200 kB\nPss_Shmem:  100 kB\n")
    assert sysinfo.process_mem_kb(str(d)) == 800, "Pss_Anon + Pss_Shmem from smaps_rollup"
    (d / "smaps_rollup").write_text("Rss:  5000 kB\nPss:  2000 kB\n")
    assert sysinfo.process_mem_kb(str(d)) == 2000, "plain Pss on kernels without the split"
    (d / "smaps_rollup").write_text("")
    (d / "status").write_text("Name:\tx\nVmRSS:\t    5000 kB\n")
    assert sysinfo.process_mem_kb(str(d)) == 5000, "VmRSS as the last resort"
    assert sysinfo.process_mem_kb(str(tmp_path / "gone")) == 0
