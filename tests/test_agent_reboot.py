"""Reboot requests and hardware telemetry in the worker agent (docs/FLEET_UI_CONTRACT.md,
"Worker -> host additions"), against tests/fake_host.py like tests/test_agent.py."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterator

import pytest

from fleet.common import hwinfo
from fleet.worker import config
from fleet.worker.__main__ import main as cli_main
from fleet.worker.agent import Agent, AgentOptions
from tests.fake_host import FakeHost
from tests.test_agent import HB, AgentThread, _held_trade_jobs, _leased

TICK = 0.2


@pytest.fixture
def host() -> Iterator[FakeHost]:
    fake = FakeHost(lease_seconds=30.0, heartbeat_seconds=HB).start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> str:
    directory = str(tmp_path / "state")
    os.makedirs(directory)
    monkeypatch.setenv("FLEET_STATE_DIR", directory)
    monkeypatch.delenv("FLEET_RUN_DIR", raising=False)
    monkeypatch.delenv(config.REBOOT_TRIGGER_ENV, raising=False)
    return directory


@pytest.fixture
def run_dir(tmp_path, monkeypatch) -> Path:
    """What the systemd unit sets: FLEET_RUN_DIR and FLEET_REBOOT_TRIGGER in it."""
    directory = tmp_path / "run"
    directory.mkdir()
    monkeypatch.setenv("FLEET_RUN_DIR", str(directory))
    monkeypatch.setenv(config.REBOOT_TRIGGER_ENV, str(directory / "reboot"))
    return directory


@pytest.fixture
def enrolled(host: FakeHost, state_dir: str) -> str:
    token = host.mint_enroll_token()
    assert cli_main(["enroll", f"--host={host.url}", f"--token={token}", "--name=box1"]) == 0
    return config.load_conf(state_dir)["worker_id"]


def _stopped(agent: AgentThread, timeout: float = 15.0) -> int:
    agent.thread.join(timeout)
    assert not agent.thread.is_alive(), "the agent did not stop for the reboot"
    return agent.result[0]


def _registers(host: FakeHost) -> list[str]:
    return [p for m, p, s in host.requests if p.endswith("/workers/register") and s == 200]


# ----------------------------------------------------------------- reboot


def test_reboot_drains_releases_with_checkpoint_and_writes_the_trigger(host: FakeHost, state_dir: str, run_dir: Path, enrolled: str) -> None:
    host.set_desired_role(enrolled, "backtest")
    agent = AgentThread(state_dir).start()
    try:
        job_id = host.enqueue_job("sleep", {"seconds": 10})
        host.wait_for(_leased(host, job_id, min_elapsed=1), timeout=8.0)
        rid = host.request_reboot(enrolled)
        assert _stopped(agent) == 0
    finally:
        agent.stop()
    assert (run_dir / "reboot").read_text() == rid + "\n"
    assert agent.agent.running == {} and agent.agent.stopping is True
    job = host.job(job_id)
    assert job["status"] == "queued" and job["lease_worker_id"] is None
    assert job["checkpoint"]["elapsed"] >= 1
    assert host.releases(job_id)[0]["reason"] == "shutdown"
    final = host.heartbeats[-1]["request"]
    assert final["want_job"] is False and [r["id"] for r in final["released"]] == [job_id]


def test_reboot_hands_trade_jobs_back_through_the_release_handshake(host: FakeHost, state_dir: str, run_dir: Path, enrolled: str) -> None:
    host.set_trade_settings(trade_tick_s=TICK)
    aid = host.add_assignment()
    host.set_desired_role(enrolled, "trade")
    agent = AgentThread(state_dir, trade_tick_s=TICK).start()
    try:
        host.wait_for(lambda: host.orders(aid, status="open"), timeout=8.0)
        job_id = _held_trade_jobs(host, enrolled)[0]
        rid = host.request_reboot(enrolled)
        assert _stopped(agent) == 0
    finally:
        agent.stop()
    release = host.trade_calls("/trade/release")[-1]
    assert release["response"]["released"] == [job_id]
    assert host.job(job_id)["status"] == "queued" and host.orders(aid, status="open") == []
    assert agent.agent.trade_jobs == {}
    assert (run_dir / "reboot").read_text().strip() == rid


def test_reboot_in_a_reply_with_a_role_change_skips_the_drain_and_stops(host: FakeHost, state_dir: str, run_dir: Path, enrolled: str) -> None:
    agent = Agent(state_dir=state_dir, options=AgentOptions(heartbeat_seconds=HB, http_timeout=2.0))
    assert agent.boot() and agent.register_once()
    agent.desired_role, agent.role_epoch = "backtest", agent.role_epoch + 1
    agent.handle_response({"reboot": "rb_1", "preempt": [], "claimed": []})
    assert agent.stop.is_set() and agent.reboot_id == "rb_1"
    assert agent.role == "idle", "the shutdown path, not a drain, hands everything back"
    assert agent.run_forever() == 0, "run_forever on a set stop event goes straight to shutdown"
    assert (run_dir / "reboot").read_text().strip() == "rb_1"


@pytest.mark.parametrize("where", ["leftover", "done"])
def test_an_already_handled_reboot_id_is_ignored(host: FakeHost, state_dir: str, run_dir: Path, enrolled: str, where: str, caplog) -> None:
    """A trigger still on tmpfs at start means that reboot never happened: it is moved
    aside, its id ignored and can_reboot is false. An id in reboot.done (moved by an
    earlier start) is ignored too, but reboots stay on."""
    name = "reboot" if where == "leftover" else "reboot.done"
    (run_dir / name).write_text("rb_old\n")
    host.request_reboot(enrolled, "rb_old")
    caplog.set_level(logging.WARNING, logger="fleet.agent")
    agent = AgentThread(state_dir).start()
    try:
        host.wait_for(lambda: agent.agent.heartbeat_count >= 4, timeout=8.0)
        assert agent.thread.is_alive() and not agent.agent.stop.is_set()
        assert host.worker(enrolled)["can_reboot"] is (where == "done")
    finally:
        agent.stop()
    assert not (run_dir / "reboot").exists(), "a leftover trigger must not stay where fleet-reboot.path fires on it"
    assert (run_dir / "reboot.done").read_text().strip() == "rb_old"
    assert agent.agent.reboot_id is None
    if where == "leftover":
        assert "fleet-reboot.path did not reboot" in caplog.text


def test_a_new_reboot_id_after_a_handled_one_reboots(host: FakeHost, state_dir: str, run_dir: Path, enrolled: str) -> None:
    (run_dir / "reboot.done").write_text("rb_old\n")
    agent = AgentThread(state_dir).start()
    try:
        host.wait_for(lambda: agent.agent.heartbeat_count >= 1, timeout=8.0)
        host.request_reboot(enrolled, "rb_new")
        assert _stopped(agent) == 0
    finally:
        agent.stop()
    assert (run_dir / "reboot").read_text().strip() == "rb_new"


def test_without_a_trigger_the_request_is_logged_once_and_ignored(host: FakeHost, state_dir: str, enrolled: str, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="fleet.agent")
    host.set_desired_role(enrolled, "backtest")
    agent = AgentThread(state_dir).start()
    try:
        host.wait_for(lambda: agent.agent.heartbeat_count >= 1, timeout=8.0)
        assert host.worker(enrolled)["can_reboot"] is False
        rid = host.request_reboot(enrolled)
        start = agent.agent.heartbeat_count
        host.wait_for(lambda: agent.agent.heartbeat_count >= start + 5, timeout=8.0)
        assert agent.thread.is_alive() and not agent.agent.stop.is_set() and agent.agent.reboot_id is None
    finally:
        agent.stop()
    assert sum(rid in r.getMessage() for r in caplog.records) == 1


def test_an_unwritable_trigger_directory_turns_reboots_off(host: FakeHost, state_dir: str, tmp_path: Path, enrolled: str, monkeypatch) -> None:
    monkeypatch.setenv(config.REBOOT_TRIGGER_ENV, str(tmp_path / "missing" / "reboot"))
    agent = Agent(state_dir=state_dir, options=AgentOptions(heartbeat_seconds=HB, http_timeout=2.0))
    assert agent.boot() and agent.register_once()
    assert host.worker(enrolled)["can_reboot"] is False
    agent.handle_response({"reboot": "rb_1"})
    assert not agent.stop.is_set()


def test_settle_reboot_trigger(tmp_path: Path) -> None:
    trigger = str(tmp_path / "reboot")
    assert config.settle_reboot_trigger(trigger) == (None, False)
    assert config.write_reboot_trigger(trigger, "rb_1")
    assert config.read_reboot_trigger(trigger) == "rb_1"
    assert config.settle_reboot_trigger(trigger) == ("rb_1", True)
    assert not os.path.exists(trigger)
    assert config.settle_reboot_trigger(trigger) == ("rb_1", False)
    assert sorted(os.listdir(tmp_path)) == ["reboot.done"], "no temp files left behind"


# -------------------------------------------------------------- telemetry


@pytest.fixture
def fake_disk(tmp_path: Path, monkeypatch) -> str:
    disk = tmp_path / "sys" / "block" / "nvme0n1"
    (disk / "queue").mkdir(parents=True)
    (disk / "queue" / "rotational").write_text("0\n")
    (disk / "removable").write_text("0\n")
    (disk / "stat").write_text("1 0 2 3 4 0 3000000 5 0 6 7\n")
    wear = tmp_path / "wear.json"
    wear.write_text(json.dumps({"devices": {"nvme0n1": {"wear_pct": 4}}}))
    monkeypatch.setattr(hwinfo, "boot_disk", lambda: str(disk))
    monkeypatch.setattr(hwinfo, "temp_c", lambda: 47.5)
    monkeypatch.setattr(hwinfo, "WEAR_FILE", str(wear))
    real_wear = hwinfo.wear_pct
    monkeypatch.setattr(hwinfo, "wear_pct", lambda d: real_wear(d, str(wear)))
    return str(disk)


def test_heartbeat_carries_the_telemetry(host: FakeHost, state_dir: str, enrolled: str, fake_disk: str) -> None:
    agent = AgentThread(state_dir).start()
    try:
        hb = host.wait_for(lambda: host.heartbeats[-1] if host.heartbeats else None)
    finally:
        agent.stop()
    req = hb["request"]
    assert {k: req[k] for k in ("temp_c", "boot_media", "wear_pct", "disk_gb_written")} == {
        "temp_c": 47.5, "boot_media": "ssd", "wear_pct": 4.0, "disk_gb_written": 1.54,
    }
    assert host.worker(enrolled)["disk_gb_written"] == 1.54


def test_heartbeat_telemetry_keys_are_present_on_any_machine(host: FakeHost, state_dir: str, enrolled: str) -> None:
    agent = AgentThread(state_dir).start()
    try:
        hb = host.wait_for(lambda: host.heartbeats[-1] if host.heartbeats else None)
    finally:
        agent.stop()
    req = hb["request"]
    assert req["boot_media"] in ("flash", "ssd", "hdd", "unknown")
    for key in ("temp_c", "wear_pct", "disk_gb_written"):
        assert req[key] is None or isinstance(req[key], float)


def test_register_carries_can_reboot_and_boot_media(host: FakeHost, state_dir: str, run_dir: Path, enrolled: str, fake_disk: str) -> None:
    agent = Agent(state_dir=state_dir, options=AgentOptions(heartbeat_seconds=HB, http_timeout=2.0))
    assert agent.boot()
    payload = agent.register_payload()
    assert payload["can_reboot"] is True and payload["boot_media"] == "ssd"
    assert agent.register_once()
    assert host.worker(enrolled)["can_reboot"] is True and host.worker(enrolled)["boot_media"] == "ssd"


def test_boot_disk_is_looked_up_once(host: FakeHost, state_dir: str, enrolled: str, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(hwinfo, "boot_disk", lambda: calls.append(1) or None)
    agent = AgentThread(state_dir).start()
    try:
        host.wait_for(lambda: agent.agent.heartbeat_count >= 3, timeout=8.0)
    finally:
        agent.stop()
    assert calls == [1]


# ------------------------------------------------------------ status file


def test_status_goes_to_the_run_dir_and_the_old_copy_is_removed(host: FakeHost, state_dir: str, run_dir: Path, enrolled: str) -> None:
    legacy = Path(state_dir) / "status.json"
    legacy.write_text("{}")
    agent = AgentThread(state_dir).start()
    try:
        host.wait_for(lambda: (run_dir / "status.json").exists() and agent.agent.heartbeat_count >= 1, timeout=8.0)
    finally:
        agent.stop()
    assert not legacy.exists()
    assert json.loads((run_dir / "status.json").read_text())["state"] in ("ACTIVE", "REGISTER")
    assert sorted(os.listdir(state_dir)) == ["worker.conf"]


def test_status_without_a_run_dir_stays_in_the_state_dir(host: FakeHost, state_dir: str, enrolled: str) -> None:
    agent = AgentThread(state_dir).start()
    try:
        host.wait_for(lambda: agent.agent.heartbeat_count >= 1, timeout=8.0)
    finally:
        agent.stop()
    assert (Path(state_dir) / "status.json").exists()
    assert config.load_status(state_dir)["heartbeat_count"] >= 1


def test_status_by_hand_without_env_finds_the_runtime_dir(tmp_path: Path, monkeypatch, capsys) -> None:
    """Root running `python3 -m fleet.worker status` has neither FLEET_STATE_DIR nor
    FLEET_RUN_DIR: the default state dir maps to /run/fleet when that exists."""
    state, run = tmp_path / "var-lib-fleet", tmp_path / "run-fleet"
    state.mkdir()
    monkeypatch.setattr(config, "DEFAULT_STATE_DIR", str(state))
    monkeypatch.setattr(config, "DEFAULT_RUN_DIR", str(run))
    monkeypatch.delenv("FLEET_STATE_DIR", raising=False)
    monkeypatch.delenv("FLEET_RUN_DIR", raising=False)
    config.save_conf(str(state), {"host_url": "http://h", "worker_id": "w_1", "worker_token": "secret-token-1234"})
    assert config.status_path(str(state)) == str(state / "status.json"), "no runtime dir: the state dir"
    run.mkdir()
    monkeypatch.setenv("FLEET_RUN_DIR", str(run))  # the agent, under systemd
    config.save_status(str(state), {"heartbeat_count": 7})
    monkeypatch.delenv("FLEET_RUN_DIR")  # the owner, by hand
    assert (run / "status.json").exists() and not (state / "status.json").exists()
    assert cli_main(["status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == {"heartbeat_count": 7}
    assert out["conf"]["worker_id"] == "w_1"
