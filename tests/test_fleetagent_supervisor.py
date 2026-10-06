"""Supervisor tests: register, reconcile, restart rule, adoption, native guard, ack, host loss."""

from __future__ import annotations

import os
import stat

from fleetagent import config
from tests.fake_machine_host import FakeMachineHost, make_run
from tests.test_fleetagent_harness import FakeClock, build_rig, host, rig  # noqa: F401 (fixtures)

DIGEST = "sha256:" + "b" * 64
REF = "reg.example/fleet/hello@" + DIGEST


def _assign(rig, epoch: int = 3, **run_over) -> dict:
    run = make_run(REF, **run_over)
    rig.host.assign("hello", run, epoch=epoch, keep=[DIGEST])
    rig.host.set_secrets({"HELLO_GREETING": "s3cr3t-greeting"})
    return run


def _docker_runs(rig) -> list[list[str]]:
    return [c for c in rig.docker.calls if c[:2] == ["run", "-d"]]


def _flag(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i in range(len(args) - 1) if args[i] == name]


# ------------------------------------------------------------------ register


def test_register_rotates_the_token_and_saves_it(rig) -> None:
    conf = config.load_conf(rig.state_dir)
    assert conf["machine_token"] != "t0"
    assert conf["machine_token"] == rig.host.machine("m_pre")["token"]
    assert stat.S_IMODE(os.stat(config.conf_path(rig.state_dir)).st_mode) == 0o600
    body = rig.host.registers[-1]
    assert body["machine_id"] == "m_pre" and body["machine_token"] == "t0" and body["agent_version"] == rig.sup.agent_version
    assert body["specs"]["disk_type"] == "ssd" and body["specs"]["ram_total_mb"] == 3800


def test_register_retry_uses_the_previous_token_after_a_lost_answer(tmp_path, monkeypatch, host) -> None:
    rig = build_rig(tmp_path, monkeypatch, host)
    assert rig.sup.boot()
    host.drop_response_next("register")
    assert rig.sup.register_once() is False
    # the host rotated the token but the answer never arrived: agent.conf still has the old one
    assert config.load_conf(rig.state_dir)["machine_token"] == "t0"
    assert host.machine("m_pre")["prev"] == "t0"
    assert rig.sup.register_once() is True
    new = config.load_conf(rig.state_dir)["machine_token"]
    assert new == host.machine("m_pre")["token"] and new != "t0"
    assert rig.sup.tick() is True and len(host.heartbeats) == 1
    assert host.machine("m_pre")["prev"] is None


def test_heartbeat_401_goes_back_to_register(rig) -> None:
    rig.host.machine("m_pre")["token"] = "rotated-elsewhere"
    assert rig.sup.tick() is False
    assert rig.sup.register_once() is False  # the old token is not the current or previous one any more
    rig.host.machine("m_pre")["prev"] = rig.sup.conf["machine_token"]
    assert rig.sup.register_once() is True


def test_enroll_command_writes_conf_and_status_runs(tmp_path, monkeypatch, host, capsys) -> None:
    from fleetagent.__main__ import main

    state = tmp_path / "state"
    monkeypatch.setenv("FLEET_AGENT_STATE_DIR", str(state))
    monkeypatch.setenv("FLEET_AGENT_DOCKER", str(tmp_path / "no-docker"))
    token = host.mint_enroll_token()
    assert main(["enroll", "--host", host.url, "--token", token, "--name", "box9"]) == 0
    conf = config.load_conf(str(state))
    assert conf["name"] == "box9" and conf["machine_id"] in host.machines
    assert stat.S_IMODE(os.stat(config.conf_path(str(state))).st_mode) == 0o600
    assert host.registers[-1]["specs"]["docker_ok"] is False
    assert main(["status"]) == 0
    assert "machine_token" in capsys.readouterr().out
    assert main(["enroll", "--host", host.url, "--token", "wrong"]) == 1


def test_enroll_reads_the_token_from_env_and_file(tmp_path, monkeypatch, host) -> None:
    from fleetagent.__main__ import main

    monkeypatch.setenv("FLEET_AGENT_STATE_DIR", str(tmp_path / "s1"))
    monkeypatch.setenv("FLEET_AGENT_DOCKER", str(tmp_path / "no-docker"))
    monkeypatch.setenv("FLEET_ENROLL_TOKEN", host.mint_enroll_token())
    assert main(["enroll", "--host", host.url]) == 0
    monkeypatch.delenv("FLEET_ENROLL_TOKEN")
    monkeypatch.setenv("FLEET_AGENT_STATE_DIR", str(tmp_path / "s2"))
    tfile = tmp_path / "token"
    tfile.write_text(host.mint_enroll_token() + "\n")
    assert main(["enroll", "--host", host.url, "--token-file", str(tfile)]) == 0
    monkeypatch.setenv("FLEET_AGENT_STATE_DIR", str(tmp_path / "s3"))
    assert main(["enroll", "--host", host.url]) == 2


def test_run_without_conf_exits_78(tmp_path, monkeypatch) -> None:
    from fleetagent.supervisor import Supervisor

    monkeypatch.setenv("FLEET_AGENT_RUN_DIR", str(tmp_path / "run"))
    assert Supervisor(state_dir=str(tmp_path / "empty")).run_forever() == 78


# ----------------------------------------------------------------- heartbeat


def test_heartbeat_reports_specs_native_and_cleanup(rig) -> None:
    rig.tick()
    body = rig.host.heartbeats[-1]
    assert set(body) >= {"specs", "native_polymarket", "acked_epoch", "container", "logs", "cleanup"}
    assert body["native_polymarket"] == "absent" and body["container"] is None and body["logs"] == []
    assert body["specs"]["docker_ok"] is True and body["specs"]["docker_root"] == "/var/lib/docker"
    assert body["specs"]["disk_type"] == "ssd" and body["specs"]["cpu_count"] == 2
    assert body["cleanup"] == {"images_removed": 0, "bytes_freed": 0, "low_disk": False}


def test_docker_down_is_reported_not_fatal(rig) -> None:
    rig.docker.down = True
    rig.tick()
    assert rig.host.heartbeats[-1]["specs"]["docker_ok"] is False


# ----------------------------------------------------------------- start


def test_starts_the_desired_workload_with_the_exact_flags(rig) -> None:
    _assign(rig, memory_mb=3000, state_volume=True, network="host", uts_host=True, nice=5)
    rig.tick()
    (args,) = _docker_runs(rig)
    flags = args[2:]
    assert args[:2] == ["run", "-d"] and flags[-1] == REF
    assert _flag(flags, "--name") == ["fleet-hello-3"]
    assert sorted(_flag(flags, "--label")) == ["fleet.epoch=3", "fleet.workload=hello"]
    for solo in ("--read-only",):
        assert solo in flags
    assert _flag(flags, "--tmpfs") == ["/tmp:rw,nosuid,size=256m"]
    assert _flag(flags, "--security-opt") == ["no-new-privileges"]
    assert _flag(flags, "--user") == ["10001:10001"]
    assert _flag(flags, "--memory") == ["3000m"] and _flag(flags, "--memory-swap") == ["-1"]
    assert _flag(flags, "--cpus") == ["1.0"] and _flag(flags, "--stop-timeout") == ["15"]
    assert _flag(flags, "--network") == ["host"] and _flag(flags, "--uts") == ["host"]
    assert _flag(flags, "--log-driver") == ["local"]
    assert sorted(_flag(flags, "--log-opt")) == ["max-file=3", "max-size=10m"]
    mounts = _flag(flags, "-v")
    assert f"{rig.data_dir}/hello/scratch:/scratch" in mounts and f"{rig.data_dir}/hello/state:/state" in mounts
    assert f"{rig.run_dir}/secrets/hello:/run/fleet/secrets:ro" in mounts
    env = _flag(flags, "-e")
    assert "FLEET_HOST_URL=http://host" in env and "FLEET_WORKLOAD=hello" in env and "FLEET_NICE=5" in env
    assert "FLEET_RUN_TOKEN" in env  # no value on the command line
    assert not any(tok in " ".join(flags) for tok in rig.host.run_tokens)
    cid = next(iter(rig.docker.containers))
    assert rig.docker.client_env[cid]["FLEET_RUN_TOKEN"] == rig.host.run_tokens[-1]
    assert rig.host.starts == [{"epoch": 3}]
    assert os.path.isdir(f"{rig.data_dir}/hello/state")


def test_without_memory_cap_and_state_the_flags_are_omitted(rig) -> None:
    _assign(rig, memory_mb=None, cpus=None, state_volume=False)
    rig.tick()
    flags = _docker_runs(rig)[0]
    assert "--memory" not in flags and "--cpus" not in flags and "--uts" not in flags
    assert not any(m.endswith(":/state") for m in _flag(flags, "-v"))


def test_secret_files_are_mode_0440_without_root_and_0400_owned_by_uid_as_root(tmp_path, monkeypatch, host) -> None:
    rig = build_rig(tmp_path, monkeypatch, host).boot()
    _assign(rig)
    rig.tick()
    path = f"{rig.run_dir}/secrets/hello/HELLO_GREETING"
    assert open(path).read() == "s3cr3t-greeting" and stat.S_IMODE(os.stat(path).st_mode) == 0o440
    assert _flag(_docker_runs(rig)[0], "--group-add") == [str(os.getegid())]

    root_dir = tmp_path / "root"
    root_dir.mkdir()
    rig2 = build_rig(root_dir, monkeypatch, FakeMachineHost().start(), root=True)
    rig2.boot()
    _assign(rig2)
    rig2.tick()
    path2 = f"{rig2.run_dir}/secrets/hello/HELLO_GREETING"
    assert stat.S_IMODE(os.stat(path2).st_mode) == 0o400
    assert (path2, 10001, 10001) in rig2.chowns
    assert "--group-add" not in _docker_runs(rig2)[0]
    rig2.host.stop()


def test_epoch_is_acked_only_after_a_successful_start(rig) -> None:
    _assign(rig)
    rig.docker.fail_run = "driver failed"
    rig.tick()
    rig.tick()
    assert rig.host.acked_epoch == 0
    block = rig.host.last_container
    assert block["state"] == "starting" and "driver failed" in block["error"] and block["container_id"] is None
    assert not rig.fleet_containers()
    rig.docker.fail_run = None
    rig.tick()  # starts
    assert len(rig.fleet_containers()) == 1
    rig.tick()  # reports the ack
    assert rig.host.acked_epoch == 3
    assert rig.host.last_container["state"] == "running" and rig.host.last_container["epoch"] == 3
    assert rig.host.last_container["image_digest"] == DIGEST
    rig.tick()
    assert rig.host.last_container["cpu_pct"] == 1.5 and rig.host.last_container["mem_mb"] == 40


def test_a_refused_start_is_retried_and_not_acked(rig) -> None:
    _assign(rig)
    rig.host.fail_next("start", 500)
    rig.tick()
    rig.tick()
    assert rig.host.acked_epoch == 0 and "start refused" in rig.host.last_container["error"]
    rig.tick()
    rig.tick()
    assert rig.host.acked_epoch == 3 and len(rig.fleet_containers()) == 1


def test_pull_failure_is_reported_and_retried_later(rig) -> None:
    _assign(rig)
    rig.docker.fail_pull = "registry down"
    rig.tick()
    rig.tick(10)
    assert rig.host.last_container["state"] == "starting" and "registry down" in rig.host.last_container["error"]
    assert len(rig.docker.pulls) == 1  # no hammering: the retry waits 30 s
    rig.docker.fail_pull = None
    rig.tick(31)
    rig.tick()
    assert len(rig.fleet_containers()) == 1 and rig.host.acked_epoch == 3


def test_a_local_image_is_not_pulled_again(rig) -> None:
    rig.docker.add_image("reg.example/fleet/hello", digest=DIGEST)
    _assign(rig)
    rig.tick()
    assert rig.docker.pulls == [] and len(rig.fleet_containers()) == 1


def test_background_pull_does_not_block_the_heartbeat(tmp_path, monkeypatch, host) -> None:
    import threading

    rig = build_rig(tmp_path, monkeypatch, host, background_pull=True).boot()
    gate = threading.Event()
    rig.docker.pull_gate = gate
    _assign(rig)
    rig.tick()
    assert rig.sup.reconciler.busy() and not rig.fleet_containers()
    rig.tick()
    assert host.last_container["state"] == "starting"
    gate.set()
    host.wait_for(lambda: not rig.sup.reconciler.busy())
    rig.tick()
    assert len(rig.fleet_containers()) == 1
    rig.tick()
    assert host.acked_epoch == 3


# --------------------------------------------------------------- native guard


def test_never_starts_or_stops_while_native_is_active(tmp_path, monkeypatch, host) -> None:
    rig = build_rig(tmp_path, monkeypatch, host, native="active").boot()
    _assign(rig)
    rig.tick()
    rig.tick()
    assert _docker_runs(rig) == [] and rig.docker.calls_of("stop") == [] and rig.docker.calls_of("rm") == []
    assert host.starts == [] and host.acked_epoch == 0
    assert host.heartbeats[-1]["native_polymarket"] == "active"
    # a container that is already running is left alone even though nothing is desired
    rig.docker.add_image("reg.example/fleet/old")
    cid = rig.docker.docker().run(["--name", "fleet-old-1", "--label", "fleet.workload=old", "--label", "fleet.epoch=1", "reg.example/fleet/old"])
    host.unassign()
    rig.tick()
    rig.tick()
    assert rig.docker.container(cid)["running"] and rig.docker.calls_of("stop") == []
    # the native worker stops: the next answer is acted on
    rig.systemctl.state = "inactive"
    rig.tick()
    assert rig.docker.container(cid) is None
    assert host.heartbeats[-1]["native_polymarket"] == "inactive"


def test_native_inactive_and_absent_both_allow_starting(tmp_path, monkeypatch, host) -> None:
    rig = build_rig(tmp_path, monkeypatch, host, native="inactive").boot()
    _assign(rig)
    rig.tick()
    assert len(rig.fleet_containers()) == 1 and host.heartbeats[-1]["native_polymarket"] == "inactive"


# ----------------------------------------------------------------- stop


def test_stops_an_undesired_container_and_wipes_scratch_and_secrets(rig) -> None:
    _assign(rig, epoch=3, stop_timeout_s=9)
    rig.tick()
    scratch = f"{rig.data_dir}/hello/scratch"
    secrets = f"{rig.run_dir}/secrets/hello"
    open(os.path.join(scratch, "junk.bin"), "w").write("x")
    assert os.path.isdir(secrets)
    rig.host.unassign()
    rig.tick()
    assert rig.docker.calls_of("stop", "-t", "9") and rig.fleet_containers() == []
    assert os.listdir(scratch) == [] and not os.path.exists(secrets)
    rig.tick()
    assert rig.host.acked_epoch == 4 and rig.host.last_container is None


def test_a_new_epoch_replaces_the_old_container(rig) -> None:
    _assign(rig, epoch=3)
    rig.tick()
    first = rig.fleet_containers()[0]["id"]
    _assign(rig, epoch=4)
    rig.tick()
    (c,) = rig.fleet_containers()
    assert c["id"] != first and c["labels"]["fleet.epoch"] == "4" and c["name"] == "fleet-hello-4"
    assert rig.docker.container(first) is None
    assert [s for s in rig.host.starts] == [{"epoch": 3}, {"epoch": 4}]
    assert os.path.exists(f"{rig.run_dir}/secrets/hello/HELLO_GREETING")  # rewritten for epoch 4


def test_a_workload_switch_wipes_the_other_workloads_files(rig) -> None:
    _assign(rig, epoch=3)
    rig.tick()
    rig.host.assign("other", make_run("reg.example/fleet/other@sha256:" + "c" * 64), epoch=4)
    rig.host.set_secrets({})
    rig.tick()
    (c,) = rig.fleet_containers()
    assert c["labels"]["fleet.workload"] == "other"
    assert not os.path.exists(f"{rig.run_dir}/secrets/hello")
    assert os.path.isdir(f"{rig.run_dir}/secrets/other")


# ----------------------------------------------------------------- restart rule


def _running(rig):
    (c,) = rig.fleet_containers()
    return c


def test_restart_three_seconds_after_an_exit(rig) -> None:
    _assign(rig)
    rig.tick()
    first = _running(rig)["id"]
    rig.docker.exit(first, 1)
    rig.tick(0.1)  # noticed: scheduled, not restarted yet
    assert rig.sup.reconciler.managed.state == "exited"
    rig.clock.advance(2.0)
    rig.sup.service()
    assert rig.docker.container(first) is not None and len(_docker_runs(rig)) == 1
    rig.clock.advance(1.5)
    rig.sup.service()
    second = _running(rig)
    assert second["id"] != first and len(_docker_runs(rig)) == 2
    assert rig.sup.reconciler.managed.restarts == 1
    rig.tick()
    assert rig.host.last_container["restarts"] == 1 and rig.host.last_container["state"] == "running"
    assert len(rig.host.starts) == 2 and rig.host.run_tokens[0] != rig.host.run_tokens[1]


def test_exit_code_zero_is_restarted_too(rig) -> None:
    _assign(rig)
    rig.tick()
    rig.docker.exit(_running(rig)["id"], 0)
    rig.tick(0.1)
    rig.tick(3.0)
    assert _running(rig)["running"] and len(_docker_runs(rig)) == 2


def test_exit_78_is_not_restarted_and_reported_failed(rig) -> None:
    _assign(rig)
    rig.tick()
    cid = _running(rig)["id"]
    rig.docker.exit(cid, 78)
    for _ in range(4):
        rig.tick(30.0)
    assert len(_docker_runs(rig)) == 1 and rig.docker.container(cid) is not None
    block = rig.host.last_container
    assert block["state"] == "failed" and block["exit_code"] == 78 and block["restarts"] == 0
    assert rig.sup.reconciler.managed.error.startswith("exit code 78")
    # an agent restart still does not restart it
    sup2 = rig.make_sup()
    assert sup2.boot() and sup2.register_once()
    sup2.tick()
    assert len(_docker_runs(rig)) == 1


def test_custom_no_restart_codes(rig) -> None:
    _assign(rig, no_restart_exit_codes=[3, 78])
    rig.tick()
    rig.docker.exit(_running(rig)["id"], 3)
    rig.tick(10)
    assert rig.sup.reconciler.managed.state == "failed" and rig.sup.reconciler.managed.exit_code == 3
    assert len(_docker_runs(rig)) == 1


def test_an_oom_kill_is_noted_and_restarted(rig) -> None:
    _assign(rig)
    rig.tick()
    c = _running(rig)
    c["oom"] = True
    rig.docker.exit(c["id"], 137)
    rig.tick(0.1)
    assert "out of memory" in rig.sup.reconciler.managed.error
    rig.tick(3.1)
    assert len(_docker_runs(rig)) == 2


def test_between_heartbeats_poll_notices_an_exit(rig) -> None:
    _assign(rig)
    rig.tick()
    assert rig.sup.reconciler.poll_exit() is False
    rig.docker.exit(_running(rig)["id"], 2)
    assert rig.sup.reconciler.poll_exit() is True


# ----------------------------------------------------------------- adoption


def test_a_restarted_agent_adopts_the_running_container(rig) -> None:
    _assign(rig)
    rig.tick()
    cid = _running(rig)["id"]
    rig.docker.emit(cid, "stdout", "before the restart")
    rig.tick()
    sup2 = rig.make_sup()
    assert sup2.boot() and sup2.register_once()
    assert sup2.reconciler.acked_epoch == 3
    assert sup2.reconciler.managed.container_id == cid and sup2.reconciler.managed.state == "running"
    # the redaction list is rebuilt from the secret files and the container's run token
    assert {"s3cr3t-greeting", rig.host.run_tokens[-1]} <= set(sup2.logs.secrets["hello"])
    before = len(_docker_runs(rig))
    sup2.tick()
    sup2.tick()
    assert len(_docker_runs(rig)) == before and rig.docker.calls_of("stop") == []
    assert rig.host.last_container["container_id"] == cid and rig.host.acked_epoch == 3
    # logs shipped before the restart are not shipped again (cursors persist)
    assert [l["line"] for l in rig.host.shipped_logs()].count("before the restart") == 1


def test_adoption_of_a_stale_epoch_container_replaces_it(rig) -> None:
    rig.docker.add_image("reg.example/fleet/hello")
    old = rig.docker.docker().run(["--name", "fleet-hello-2", "--label", "fleet.workload=hello", "--label", "fleet.epoch=2", "x"])
    _assign(rig, epoch=5)
    rig.sup.boot()
    assert rig.sup.reconciler.acked_epoch == 2
    rig.tick()
    assert rig.docker.container(old) is None and _running(rig)["labels"]["fleet.epoch"] == "5"


# ----------------------------------------------------------------- logs


def test_logs_are_shipped_redacted_capped_and_cut(rig) -> None:
    _assign(rig)
    rig.tick()
    cid = _running(rig)["id"]
    token = rig.host.run_tokens[-1]
    rig.docker.emit(cid, "stdout", "hello world")
    rig.docker.emit(cid, "stderr", "greeting is s3cr3t-greeting and token " + token)
    rig.docker.emit(cid, "stdout", "x" * 5000)
    rig.tick()
    lines = rig.host.heartbeats[-1]["logs"]
    texts = [l["line"] for l in lines]
    assert "hello world" in texts
    assert "greeting is [redacted] and token [redacted]" in texts
    assert max(len(t) for t in texts) == 2048
    assert {"stdout", "stderr"} <= {l["stream"] for l in lines}
    assert all("s3cr3t-greeting" not in l["line"] and token not in l["line"] for l in rig.host.shipped_logs())
    assert all(l["ts"].endswith("Z") for l in lines)


def test_at_most_200_lines_per_heartbeat_and_nothing_is_lost_or_repeated(rig) -> None:
    _assign(rig)
    rig.tick()
    rig.tick()  # the agent's own "pulling" and "started" lines go out first
    cid = _running(rig)["id"]
    seen = len(rig.host.heartbeats)
    for i in range(450):
        rig.docker.emit(cid, "stdout", f"line {i}")
    for _ in range(4):
        rig.tick()
    sizes = [len(hb["logs"]) for hb in rig.host.heartbeats[seen:] if hb["logs"]]
    assert sizes == [200, 200, 50]
    shipped = [l["line"] for l in rig.host.shipped_logs() if l["line"].startswith("line ")]
    assert shipped == [f"line {i}" for i in range(450)]


def test_at_most_64_kib_per_heartbeat(rig) -> None:
    _assign(rig)
    rig.tick()
    cid = _running(rig)["id"]
    for i in range(100):
        rig.docker.emit(cid, "stdout", f"{i:03d}" + "y" * 2000)
    rig.tick()
    first = rig.host.heartbeats[-1]["logs"]
    assert 0 < len(first) < 100 and sum(len(l["line"]) for l in first) <= 64 * 1024
    for _ in range(5):
        rig.tick()
    assert len([l for l in rig.host.shipped_logs() if l["line"][:3].isdigit()]) == 100


def test_a_failed_heartbeat_does_not_lose_logs(rig) -> None:
    _assign(rig)
    rig.tick()
    rig.tick()
    cid = _running(rig)["id"]
    rig.docker.emit(cid, "stdout", "keep me")
    rig.host.fail_next("heartbeat", 503)
    rig.tick()
    assert "keep me" not in [l["line"] for l in rig.host.shipped_logs()]
    rig.tick()
    assert [l["line"] for l in rig.host.shipped_logs()].count("keep me") == 1


def test_final_logs_of_a_removed_container_are_still_shipped(rig) -> None:
    _assign(rig)
    rig.tick()
    cid = _running(rig)["id"]
    rig.docker.emit(cid, "stdout", "last words s3cr3t-greeting")
    rig.host.unassign()
    rig.tick()
    rig.tick()
    texts = [l["line"] for l in rig.host.shipped_logs()]
    assert "last words [redacted]" in texts
    assert any(l["stream"] == "agent" and "removed container" in l["line"] for l in rig.host.shipped_logs())


# ----------------------------------------------------------------- host loss


def test_losing_the_host_never_stops_a_container(rig) -> None:
    _assign(rig)
    rig.tick()
    cid = _running(rig)["id"]
    rig.host.unassign()
    rig.host.fail_next("heartbeat", 500, count=3)
    for _ in range(3):
        rig.tick()
    assert rig.docker.container(cid)["running"] and rig.docker.calls_of("stop") == []
    rig.host.stop()
    for _ in range(3):
        rig.tick()
    assert rig.docker.container(cid)["running"] and rig.sup.misses >= 3 and rig.docker.calls_of("stop") == []


def test_a_crashed_container_stays_down_until_the_host_answers_start(rig) -> None:
    _assign(rig)
    rig.tick()
    rig.docker.exit(_running(rig)["id"], 1)
    rig.tick(0.1)
    rig.host.fail_next("start", 500, count=2)
    rig.tick(3.1)
    assert len(_docker_runs(rig)) == 1
    rig.tick(6)
    rig.tick(6)
    assert len(_docker_runs(rig)) == 2


# ----------------------------------------------------------------- low disk, cleanup


def test_low_disk_prunes_first_then_refuses_to_pull(rig) -> None:
    rig.docker.add_image("reg.example/fleet/stale", digest="sha256:" + "d" * 64)
    _assign(rig)
    rig.free_mb[0] = 800  # below max(1024 MB, 10% of the disk)
    rig.tick()
    assert rig.docker.pulls == [] and not rig.fleet_containers()
    assert "image" in rig.docker.prune_calls and "builder" in rig.docker.prune_calls
    assert rig.docker.calls_of("rmi")  # the stale fleet image was removed in the prune pass
    rig.tick()
    body = rig.host.heartbeats[-1]
    assert body["cleanup"]["low_disk"] is True and body["cleanup"]["images_removed"] == 1
    assert body["container"]["state"] == "failed" and "low_disk" in body["container"]["error"]
    rig.free_mb[0] = 50_000
    rig.tick(31)
    rig.tick()
    assert len(rig.docker.pulls) == 1 and len(rig.fleet_containers()) == 1
    assert rig.host.heartbeats[-1]["cleanup"]["low_disk"] is False


def test_low_disk_threshold_is_ten_percent_of_a_big_disk(rig) -> None:
    _assign(rig)
    rig.free_mb[0] = 9_000  # the fake disk is about 97 GB: 10% is 9.7 GB
    rig.tick()
    assert rig.docker.pulls == []
    rig.free_mb[0] = 12_000
    rig.tick(31)
    assert len(rig.docker.pulls) == 1


def test_low_disk_does_not_block_a_local_image(rig) -> None:
    rig.docker.add_image("reg.example/fleet/hello", digest=DIGEST)
    _assign(rig)
    rig.free_mb[0] = 300
    rig.tick()
    assert len(rig.fleet_containers()) == 1 and rig.docker.pulls == []


def test_cleanup_after_a_start_keeps_keep_images_and_drops_the_rest(rig) -> None:
    keep = "sha256:" + "e" * 64
    rig.docker.add_image("reg.example/fleet/previous", digest=keep)
    rig.docker.add_image("reg.example/fleet/ancient", digest="sha256:" + "f" * 64)
    rig.docker.add_image("python", "3.13-slim", digest="sha256:" + "9" * 64)
    rig.host.assign("hello", make_run(REF), epoch=3, keep=[DIGEST, keep])
    rig.tick()
    repos = {i["Repository"] for i in rig.docker.images}
    assert "reg.example/fleet/ancient" not in repos
    assert {"reg.example/fleet/previous", "python", "reg.example/fleet/hello"} <= repos
    rig.tick()
    assert rig.host.heartbeats[-1]["cleanup"]["images_removed"] == 1


def test_exited_containers_are_removed_by_the_hourly_cleanup(rig) -> None:
    _assign(rig, epoch=3)
    rig.tick()
    stray = rig.docker.docker().run(["--name", "fleet-gone-1", "--label", "fleet.workload=gone", "--label", "fleet.epoch=1", "img"])
    rig.docker.exit(stray, 0)
    rig.tick(3601)
    assert rig.docker.container(stray) is None and len(rig.fleet_containers()) == 1


# ----------------------------------------------------------------- once / loop


def test_run_loop_heartbeats_on_a_fixed_schedule_and_polls(rig) -> None:
    clock: FakeClock = rig.clock
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(max(seconds, 0.05))
        if len(rig.host.heartbeats) >= 3:
            rig.sup.stop.set()

    rig.sup._sleep = sleep
    rig.sup.options.poll_interval = 1.0
    assert rig.sup.active_loop() is None
    assert len(rig.host.heartbeats) >= 3
    assert all(s <= 0.1 for s in sleeps)


def test_run_once_does_one_pass(tmp_path, monkeypatch, host) -> None:
    rig = build_rig(tmp_path, monkeypatch, host)
    _assign(rig)
    assert rig.sup.run_once() == 0
    assert len(host.heartbeats) == 1 and len(rig.fleet_containers()) == 1
    host.stop()
    assert rig.make_sup().run_once() == 1


def test_an_unreadable_systemctl_means_do_nothing_and_say_nothing(rig) -> None:
    import subprocess

    def garbage(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "Failed to connect to bus\n")

    rig.sup._systemctl = garbage
    _assign(rig)
    rig.tick()
    assert _docker_runs(rig) == [] and "native_polymarket" not in rig.host.heartbeats[-1]
    rig.sup._systemctl = rig.systemctl
    rig.tick()
    assert len(_docker_runs(rig)) == 1 and rig.host.heartbeats[-1]["native_polymarket"] == "absent"


DOCKER_STUB = """#!/bin/sh
if [ "$1" = info ]; then echo '{"DockerRootDir": "/var/lib/docker", "ServerVersion": "1.2"}'; fi
"""
SYSTEMCTL_STUB = """#!/bin/sh
if [ "$1" = is-active ]; then echo inactive; exit 3; fi
echo enabled
"""


def test_run_once_command_uses_the_env_overrides(tmp_path, monkeypatch, host) -> None:
    import signal

    from fleetagent.__main__ import main

    for name, body in (("docker", DOCKER_STUB), ("systemctl", SYSTEMCTL_STUB)):
        (tmp_path / name).write_text(body)
        (tmp_path / name).chmod(0o755)
    for name, sub in (("FLEET_AGENT_STATE_DIR", "state"), ("FLEET_AGENT_RUN_DIR", "run"), ("FLEET_AGENT_DATA_DIR", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("FLEET_AGENT_DOCKER", str(tmp_path / "docker"))
    monkeypatch.setenv("FLEET_AGENT_SYSTEMCTL", str(tmp_path / "systemctl"))
    config.save_conf(str(tmp_path / "state"), {"host_url": host.url, "machine_id": "m_pre", "machine_token": "t0"})
    host.machines["m_pre"] = {"id": "m_pre", "name": "x", "token": "t0", "prev": None, "specs": None}
    saved = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        assert main(["run", "--once"]) == 0
    finally:
        signal.signal(signal.SIGTERM, saved[0])
        signal.signal(signal.SIGINT, saved[1])
    (hb,) = host.heartbeats
    assert hb["native_polymarket"] == "inactive" and hb["specs"]["docker_version"] == "1.2" and hb["container"] is None
