"""Every SQL operation of docs/PROTOCOL.md as plain functions taking a connection.

The implementations live in small modules; this is the single import point:

    from host import queue
    queue.process_heartbeat(conn, worker_id, body)
"""
from host.heartbeat import process_heartbeat, register, server_time
from host.leases import (
    checkpoint,
    claim,
    complete,
    fail,
    get_job,
    held_jobs,
    job_payload,
    lease_seconds,
    orphan_jobs,
    reap,
    release,
    renew,
)
from host.scheduling import (
    ANY_IDLE,
    CreateResult,
    auto_return_to_idle,
    cancel_job,
    create_job,
    dispatch,
    get_worker,
    idle_pick,
    set_enabled,
    set_role,
)
from host.settings import BATCH_ROLES, KIND_TO_ROLE, ROLES, role_for_kind

reaper = reap
dispatcher = dispatch

__all__ = [
    "ANY_IDLE",
    "BATCH_ROLES",
    "KIND_TO_ROLE",
    "ROLES",
    "CreateResult",
    "auto_return_to_idle",
    "cancel_job",
    "checkpoint",
    "claim",
    "complete",
    "create_job",
    "dispatch",
    "dispatcher",
    "fail",
    "get_job",
    "get_worker",
    "held_jobs",
    "idle_pick",
    "job_payload",
    "lease_seconds",
    "orphan_jobs",
    "process_heartbeat",
    "reap",
    "reaper",
    "register",
    "release",
    "renew",
    "role_for_kind",
    "server_time",
    "set_enabled",
    "set_role",
]
