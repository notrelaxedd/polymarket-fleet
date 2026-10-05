"""TEMPORARY dev harness (deleted before handoff)."""
from __future__ import annotations

import json
import os
import time

from fleet.worker import agent as agent_module
from tests.e2e_models import CountingRunner, ingest_through_cli, search_phase, train_phase
from tests.test_e2e import live_host, agents, start_agent, wait_for, settled, phase_enroll, drop_box  # noqa: F401

OUT = "/tmp/claude-0/-home-user/d5c47dc8-fa06-5cc7-915b-029b2e6fe82a/scratchpad/dev_models.json"


def test_dev(live_host, tmp_path, monkeypatch, agents) -> None:
    state_dir = str(tmp_path / "state")
    monkeypatch.setenv("FLEET_STATE_DIR", state_dir)
    monkeypatch.setattr(agent_module, "Runner", CountingRunner)
    worker_id, first = phase_enroll(live_host, state_dir, agents)
    ingest_through_cli(live_host, state_dir, monkeypatch)
    ids = search_phase(live_host, worker_id, wait_for, settled)
    child = train_phase(live_host, worker_id, ids[0], wait_for)
    models = {"roots": ids, "child": child}
    if os.environ.get("DEV_PHASE"):
        from tests.e2e_signals import phase_signals
        t = time.monotonic()
        phase_signals(live_host, state_dir, worker_id, first, models, tmp_path, wait_for, settled)
        print("PHASE", time.monotonic() - t)
    else:
        with open(OUT, "w") as fh:
            json.dump({m: live_host.get(f"/api/models/{m}") for m in [child] + ids}, fh, default=str)
