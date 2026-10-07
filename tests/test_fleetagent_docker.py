"""docker.py (argv, parsing, errors), runargs.py and workdirs.py with fake runners."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import pytest

from fleetagent import runargs, workdirs
from fleetagent.docker import Docker, DockerError, normalize_ts, parse_size


class Recorder:
    """A runner that records argv and kwargs and answers with a canned result."""

    def __init__(self, stdout: str = "", stderr: str = "", rc: int = 0) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.result = (rc, stdout, stderr)

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, *self.result)


def test_parse_size_and_timestamps() -> None:
    assert parse_size("178MB") == 178_000_000 and parse_size("24.58kB") == 24_580 and parse_size("0B") == 0
    assert parse_size("1.5GiB") == int(1.5 * 1024**3) and parse_size("junk") == 0 and parse_size("") == 0
    assert normalize_ts("2026-10-06T20:00:00.5Z") == "2026-10-06T20:00:00.500000000Z"
    assert normalize_ts("2026-10-06T20:00:00Z") == "2026-10-06T20:00:00.000000000Z"
    assert normalize_ts("2026-10-06T20:00:00.123456789Z") == "2026-10-06T20:00:00.123456789Z"


def test_commands_use_the_binary_and_expected_arguments() -> None:
    rec = Recorder(stdout="abc123\n")
    d = Docker(runner=rec, binary="/opt/docker")
    d.pull("reg/fleet/x@sha256:1")
    d.stop("c1", 15)
    d.rm("c1")
    d.rmi("reg/fleet/x:old")
    d.container_prune("fleet.workload")
    assert d.run(["--name", "n", "img"], env={"A": "b"}) == "abc123"
    argv = [c[0] for c in rec.calls]
    assert all(a[0] == "/opt/docker" for a in argv)
    assert argv[0][1:] == ["pull", "--quiet", "reg/fleet/x@sha256:1"]
    assert argv[1][1:] == ["stop", "-t", "15", "c1"] and argv[2][1:] == ["rm", "-f", "c1"]
    assert argv[3][1:] == ["rmi", "reg/fleet/x:old"]
    assert argv[4][1:] == ["container", "prune", "-f", "--filter", "label=fleet.workload"]
    assert argv[5][1:] == ["run", "-d", "--name", "n", "img"] and rec.calls[5][1]["env"] == {"A": "b"}


def test_errors_become_docker_errors() -> None:
    with pytest.raises(DockerError) as exc:
        Docker(runner=Recorder(stderr="boom", rc=1)).pull("x")
    assert exc.value.returncode == 1 and "boom" in str(exc.value)

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    with pytest.raises(DockerError, match="not found"):
        Docker(runner=missing).info()

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    with pytest.raises(DockerError, match="timed out"):
        Docker(runner=slow).pull("x")
    with pytest.raises(DockerError):
        Docker(runner=Recorder(stdout="not json")).info()
    with pytest.raises(DockerError):
        Docker(runner=Recorder(stdout="")).run(["img"])


def test_info_parses_the_json_dump() -> None:
    rec = Recorder(stdout=json.dumps({"DockerRootDir": "/mnt/d", "ServerVersion": "26.1.5", "Other": {}}))
    info = Docker(runner=rec).info()
    assert info["DockerRootDir"] == "/mnt/d" and rec.calls[0][0][1:3] == ["info", "--format"]


def test_inspect_and_ps_parse_the_real_inspect_shape() -> None:
    row = {
        "Id": "a" * 64, "Name": "/fleet-hello-3", "Created": "2026-10-06T20:00:00Z", "Image": "sha256:" + "1" * 64,
        "State": {"Status": "exited", "Running": False, "ExitCode": 78, "StartedAt": "2026-10-06T20:00:01Z",
                  "FinishedAt": "2026-10-06T20:00:09Z", "Error": "", "OOMKilled": False},
        "Config": {"Image": "reg/fleet/hello@sha256:" + "2" * 64, "Labels": {"fleet.workload": "hello", "fleet.epoch": "3"},
                   "Env": ["FLEET_RUN_TOKEN=tok", "A=b"], "StopTimeout": 9},
    }

    def runner(cmd, **kw):
        if cmd[1] == "ps":
            return subprocess.CompletedProcess(cmd, 0, "a" * 64 + "\n", "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps([row]), "")

    d = Docker(runner=runner)
    (c,) = d.ps(["fleet.workload"])
    assert (c.name, c.workload, c.epoch, c.exit_code, c.running, c.stop_timeout) == ("fleet-hello-3", "hello", 3, 78, False, 9)
    assert c.env_value("FLEET_RUN_TOKEN") == "tok" and c.env_value("NOPE") is None
    assert d.inspect("zzz").id == "a" * 64
    assert Docker(runner=Recorder(stdout="[]", rc=1)).inspect("none") is None
    assert Docker(runner=Recorder()).ps(["x"]) == []


def test_ps_filters_by_every_label() -> None:
    rec = Recorder()
    Docker(runner=rec).ps(["fleet.workload", "fleet.epoch=3"])
    args = rec.calls[0][0][1:]
    assert args[:4] == ["ps", "-q", "--no-trunc", "-a"] and args.count("--filter") == 2 and "label=fleet.epoch=3" in args


def test_images_parse_the_real_listing() -> None:
    rows = [
        {"ID": "sha256:" + "a" * 64, "Repository": "reg/fleet/hello", "Tag": "<none>", "Digest": "sha256:" + "b" * 64, "Size": "52.4MB"},
        {"ID": "sha256:" + "c" * 64, "Repository": "python", "Tag": "3.13-slim", "Digest": "<none>", "Size": "178MB"},
        {"ID": "sha256:" + "d" * 64, "Repository": "<none>", "Tag": "<none>", "Digest": "<none>", "Size": "1MB"},
    ]
    imgs = Docker(runner=Recorder(stdout="".join(json.dumps(r) + "\n" for r in rows))).images()
    assert [i.ref for i in imgs] == ["reg/fleet/hello@sha256:" + "b" * 64, "python:3.13-slim", None]
    assert imgs[0].size_bytes == 52_400_000


def test_prune_outputs_are_parsed() -> None:
    assert Docker(runner=Recorder(stdout="Deleted Images:\nTotal reclaimed space: 1.5MB\n")).image_prune() == 1_500_000
    assert Docker(runner=Recorder(stdout="ID   RECLAIMABLE\nTotal:\t176.3MB\n")).builder_prune() == 176_300_000
    assert Docker(runner=Recorder(stdout="nothing")).builder_prune() == 0


def test_stats_parse_and_failure() -> None:
    out = json.dumps({"CPUPerc": "12.34%", "MemUsage": "41.5MiB / 3.7GiB"}) + "\n"
    assert Docker(runner=Recorder(stdout=out)).stats("c") == {"cpu_pct": 12.3, "mem_mb": 41}
    assert Docker(runner=Recorder(rc=1)).stats("c") is None
    assert Docker(runner=Recorder(stdout='{"CPUPerc": "--", "MemUsage": "0B / 0B"}\n')).stats("c") is None


def test_logs_merge_both_streams_in_time_order_and_pass_since() -> None:
    rec = Recorder(
        stdout="2026-10-06T20:00:01.000000002Z out two\n2026-10-06T20:00:00.5Z out one\n",
        stderr="2026-10-06T20:00:01.000000001Z err between\nplain line without a stamp\n",
    )
    rows = Docker(runner=rec).logs("c1", since="2026-10-06T20:00:00.000000000Z")
    assert rows == [
        ("2026-10-06T20:00:00.500000000Z", "stdout", "out one"),
        ("2026-10-06T20:00:01.000000001Z", "stderr", "err between"),
        ("2026-10-06T20:00:01.000000002Z", "stdout", "out two"),
    ]
    assert rec.calls[0][0][1:] == ["logs", "--timestamps", "--since", "2026-10-06T20:00:00.000000000Z", "c1"]
    with pytest.raises(DockerError):
        Docker(runner=Recorder(rc=1, stderr="no such container")).logs("c")


# ------------------------------------------------------------------- runargs


def test_image_digest_and_names() -> None:
    assert runargs.image_digest("reg/fleet/x@sha256:" + "a" * 64) == "sha256:" + "a" * 64
    assert runargs.image_digest("python:3.13-slim") is None
    assert runargs.container_name("hello", 7) == "fleet-hello-7"
    assert runargs.run_uid({}) == 10001 and runargs.run_uid({"uid": "2000"}) == 2000


def test_build_run_args_never_puts_the_token_or_a_token_in_env_on_argv() -> None:
    run = {"image": "img", "env": {"FLEET_RUN_TOKEN": "leak", "B": "2", "A": "1"}, "uid": 1234}
    args = runargs.build_run_args("w", 2, run, scratch="/s", state=None, secrets="/sec", group_add=77)
    assert "leak" not in args and args.count("FLEET_RUN_TOKEN") == 1 and args[-1] == "img"
    env = [args[i + 1] for i in range(len(args) - 1) if args[i] == "-e"]
    assert env == ["A=1", "B=2", "FLEET_RUN_TOKEN"]
    assert args[args.index("--user") + 1] == "1234:1234" and args[args.index("--group-add") + 1] == "77"


# ------------------------------------------------------------------- workdirs


def test_prepare_scratch_wipes_and_state_is_kept(tmp_path) -> None:
    data = str(tmp_path)
    chowns: list[Any] = []
    scratch = workdirs.prepare_scratch(data, "w", 1000, False, lambda *a: chowns.append(a))
    open(os.path.join(scratch, "junk"), "w").write("x")
    os.makedirs(os.path.join(scratch, "sub", "deep"))
    again = workdirs.prepare_scratch(data, "w", 1000, False, lambda *a: chowns.append(a))
    assert again == scratch and os.listdir(scratch) == [] and chowns == []
    state = workdirs.prepare_state(data, "w", 1000, True, lambda *a: chowns.append(a))
    open(os.path.join(state, "keep"), "w").write("x")
    assert workdirs.prepare_state(data, "w", 1000, True, lambda *a: chowns.append(a)) == state
    assert os.listdir(state) == ["keep"] and chowns == [(state, 1000, 1000)]  # chowned once, when created


def test_wipe_falls_back_to_a_helper_container_for_files_the_agent_cannot_delete(tmp_path, monkeypatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "owned-by-container").write_text("x")
    real_unlink = os.unlink

    def deny(path, *a, **kw):
        raise PermissionError(path)

    monkeypatch.setattr(os, "unlink", deny)
    rec = Recorder()

    def runner(cmd, **kw):
        rec.calls.append((cmd, kw))
        monkeypatch.setattr(os, "unlink", real_unlink)
        (scratch / "owned-by-container").unlink()  # what the helper container's `find -delete` does
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert workdirs.wipe_dir(str(scratch), Docker(runner=runner), "reg/fleet/x@sha256:1") is True
    helper = rec.calls[0][0]
    assert helper[1:3] == ["run", "--rm"] and "find" in helper and f"{scratch}:/wipe" in helper and "--network" in helper
    assert workdirs.wipe_dir(str(tmp_path / "missing")) is True
