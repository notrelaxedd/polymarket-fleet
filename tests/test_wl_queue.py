"""Workload job queue guardrail (docs/workloads-design.md section 4.2 `queue.py`, section 5.2).

Same semantics as the Polymarket `jobs` queue (docs/PROTOCOL.md) on the separate `workload_jobs`
table: claims are scoped to a workload, its declared kinds and the machine's current epoch; every
write is fenced by (lease token, machine, epoch); the reaper requeues and eventually fails; a machine
reassignment releases its jobs; and none of it touches the Polymarket `jobs` table.

Assumptions where the contract is silent:
- A claim with the wrong epoch, workload or kinds returns None or raises a QueueError; either way
  nothing is leased.
- Releasing a job because its machine was reassigned does not count as an expiry.
- A `cancel_requested` job released by a reassignment becomes `cancelled` (like a normal release).
- Functions return the job payload / row dicts; the tests read results from the database instead,
  except `renew`, whose `{"status", "cancel"}` result is in the contract.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from host.errors import BadRequest, Conflict, NotFound, QueueError
from host.workloads import queue
from host.workloads.assign import assign
from host.workloads.queue import create_job
from tests.conftest import insert_job, lease_job, trade_setup
from tests.wl_helpers import (
    FakeMachine, assignment_of, audit_for, call, demo_site_manifest, expire_wl_lease, get_run, insert_machine,
    insert_wl_job, insert_workload, lease_wl_job, plain_manifest, polymarket_snapshot, wl_events, wl_job,
)


@dataclass
class QW:
    pool: Any
    conn: Any
    client: Any
    m1: FakeMachine  # hello, epoch 3
    m2: FakeMachine  # hello, epoch 1
    m3: FakeMachine  # other, epoch 2
    e1: int = 3
    e2: int = 1
    e3: int = 2


@pytest.fixture
def qw(pool, conn, client) -> QW:
    insert_workload(conn, plain_manifest("hello", "hello", "extra"), size_mb=50)
    insert_workload(conn, plain_manifest("other", "other"), size_mb=50)
    insert_workload(conn, demo_site_manifest(), size_mb=1200)
    return QW(
        pool, conn, client,
        insert_machine(conn, "m-one", workload="hello", epoch=3, state="running", acked_epoch=3),
        insert_machine(conn, "m-two", workload="hello", epoch=1, state="running", acked_epoch=1),
        insert_machine(conn, "m-three", workload="other", epoch=2, state="running", acked_epoch=2),
    )


def new_job(w: QW, workload="hello", kind="hello", **kw) -> dict[str, Any]:
    return call(w.pool, create_job, workload=workload, kind=kind, params=kw.pop("params", {"n": 1}), **kw)


def claim(w: QW, machine: FakeMachine, epoch: int, workload: str = "hello", kinds: list[str] | None = None) -> dict[str, Any] | None:
    return call(w.pool, queue.claim, machine_id=machine.id, workload=workload, epoch=epoch, kinds=kinds)


def claim1(w: QW, kinds: list[str] | None = None) -> dict[str, Any] | None:
    return claim(w, w.m1, w.e1, "hello", kinds)


def tok(job: dict[str, Any]) -> str:
    return str(job["lease_token"])


def jid(job: dict[str, Any]) -> str:
    return str(job["id"])


def lease_kw(job: dict[str, Any], machine: FakeMachine, epoch: int, token: str | None = None) -> dict[str, Any]:
    return {"job_id": jid(job), "lease_token": token or tok(job), "machine_id": machine.id, "epoch": epoch}


def complete(w: QW, job, machine=None, epoch=None, result=None, token=None):
    return call(w.pool, queue.complete, result=result if result is not None else {"ok": True}, **lease_kw(job, machine or w.m1, epoch or w.e1, token))


def renew(w: QW, job, machine=None, epoch=None, progress=0.5, checkpoint=None, token=None):
    return call(w.pool, queue.renew, progress=progress, checkpoint=checkpoint, **lease_kw(job, machine or w.m1, epoch or w.e1, token))


def release(w: QW, job, machine=None, epoch=None, progress=0.5, checkpoint=None, reason="shutdown", token=None):
    return call(w.pool, queue.release, progress=progress, checkpoint=checkpoint, reason=reason, **lease_kw(job, machine or w.m1, epoch or w.e1, token))


def fail(w: QW, job, machine=None, epoch=None, error="boom", token=None):
    return call(w.pool, queue.fail, error=error, **lease_kw(job, machine or w.m1, epoch or w.e1, token))


def row(w: QW, job) -> dict[str, Any]:
    return wl_job(w.conn, job["id"])


# ------------------------------------------------------------------ create_job


def test_create_job_makes_a_queued_row_with_an_event(qw: QW):
    job = new_job(qw, params={"name": "Ada"})
    r = row(qw, job)
    assert (r["workload"], r["kind"], r["status"], r["params"]) == ("hello", "hello", "queued", {"name": "Ada"})
    assert r["lease_token"] is None and r["lease_machine_id"] is None and r["progress"] == 0 and r["expiries"] == 0
    assert r["max_expiries"] == 3
    assert wl_events(qw.conn, job["id"]), "a created event is written"


def test_a_kind_the_manifest_does_not_declare_is_400(qw: QW):
    for kind in ("other", "nope", "HELLO", ""):
        with pytest.raises(BadRequest):
            new_job(qw, kind=kind)
    with pytest.raises(BadRequest):
        new_job(qw, workload="other", kind="hello")
    assert qw.conn.execute("SELECT count(*) AS n FROM workload_jobs").fetchone()["n"] == 0


def test_an_unknown_workload_is_refused(qw: QW):
    with pytest.raises(QueueError) as exc:
        new_job(qw, workload="no-such", kind="hello")
    assert exc.value.status in (400, 404)


def test_idempotency_key_returns_the_same_job(qw: QW):
    a = new_job(qw, idempotency_key="once")
    b = new_job(qw, idempotency_key="once")
    assert str(a["id"]) == str(b["id"])
    assert qw.conn.execute("SELECT count(*) AS n FROM workload_jobs").fetchone()["n"] == 1
    c = new_job(qw, idempotency_key="twice")
    assert str(c["id"]) != str(a["id"])


def test_create_job_stores_target_and_max_expiries(qw: QW):
    t = new_job(qw, target=qw.m2.id, max_expiries=5)
    assert row(qw, t)["target_machine_id"] == qw.m2.id and row(qw, t)["max_expiries"] == 5
    unlimited = new_job(qw, max_expiries=None)
    assert row(qw, unlimited)["max_expiries"] is None


def test_the_owner_api_creates_a_job_with_201_and_rejects_bad_kinds(qw: QW):
    r = qw.client.post("/api/workload-jobs", json={"workload": "hello", "kind": "hello", "params": {"name": "x"}})
    assert r.status_code == 201, r.text
    job_id = r.json()["id"]
    assert wl_job(qw.conn, job_id)["status"] == "queued"
    assert qw.client.get(f"/api/workload-jobs/{job_id}").status_code == 200
    bad = qw.client.post("/api/workload-jobs", json={"workload": "hello", "kind": "other", "params": {}})
    assert bad.status_code == 400
    assert qw.client.post("/api/workload-jobs", json={"workload": "no-such", "kind": "hello", "params": {}}).status_code in (400, 404)
    assert qw.conn.execute("SELECT count(*) AS n FROM workload_jobs").fetchone()["n"] == 1


# ------------------------------------------------------------------ claim


def test_a_claim_leases_one_queued_job_to_the_machine_at_its_epoch(qw: QW):
    job = new_job(qw, params={"name": "Ada"})
    got = claim1(qw)
    assert got is not None and str(got["id"]) == jid(job)
    assert got["kind"] == "hello" and got["params"] == {"name": "Ada"} and got["lease_token"]
    assert got["checkpoint"] is None and got["progress"] in (0, 0.0, None)
    lease_s = qw.conn.execute("SELECT value FROM settings WHERE key = 'lease_seconds'").fetchone()["value"]
    assert got["lease_seconds"] == lease_s
    r = row(qw, job)
    assert r["status"] == "leased" and r["lease_machine_id"] == qw.m1.id and r["lease_epoch"] == qw.e1
    assert str(r["lease_token"]) == tok(got) and r["started_at"] is not None
    left = qw.conn.execute(
        "SELECT extract(epoch FROM lease_expires_at - now()) AS s FROM workload_jobs WHERE id = %s", (job["id"],)
    ).fetchone()["s"]
    assert lease_s - 5 <= float(left) <= lease_s + 1
    assert claim1(qw) is None, "nothing else is queued"


def test_a_machine_only_claims_its_own_workloads_jobs(qw: QW):
    other = new_job(qw, workload="other", kind="other")
    assert claim1(qw) is None
    assert row(qw, other)["status"] == "queued"
    got = claim(qw, qw.m3, qw.e3, "other")
    assert got is not None and str(got["id"]) == jid(other)
    assert row(qw, other)["lease_machine_id"] == qw.m3.id


def test_two_workloads_that_share_a_kind_name_never_see_each_others_jobs(qw: QW):
    """Kinds are only unique within a workload: the workload filter, not the kind, keeps jobs apart."""
    insert_workload(qw.conn, plain_manifest("twin-a", "work"), size_mb=10)
    insert_workload(qw.conn, plain_manifest("twin-b", "work"), size_mb=10)
    ma = insert_machine(qw.conn, "twin-a-box", workload="twin-a", epoch=1, state="running")
    mb = insert_machine(qw.conn, "twin-b-box", workload="twin-b", epoch=1, state="running")
    job_b = new_job(qw, workload="twin-b", kind="work")
    assert claim(qw, ma, 1, "twin-a") is None
    assert claim(qw, ma, 1, "twin-a", kinds=["work"]) is None
    assert row(qw, job_b)["status"] == "queued"
    job_a = new_job(qw, workload="twin-a", kind="work")
    got = claim(qw, mb, 1, "twin-b")
    assert str(got["id"]) == jid(job_b), "each machine gets its own workload's job"
    assert row(qw, job_a)["status"] == "queued"


def test_claiming_under_another_workloads_name_is_refused(qw: QW):
    job = new_job(qw, workload="other", kind="other")
    try:
        got = claim(qw, qw.m1, qw.e1, "other")
    except QueueError:
        got = None
    assert got is None
    assert row(qw, job)["status"] == "queued" and row(qw, job)["lease_machine_id"] is None


@pytest.mark.parametrize("epoch_delta", [-1, 1, 5])
def test_claiming_at_the_wrong_epoch_leases_nothing(qw: QW, epoch_delta):
    job = new_job(qw)
    try:
        got = claim(qw, qw.m1, qw.e1 + epoch_delta)
    except QueueError:
        got = None
    assert got is None
    assert row(qw, job)["status"] == "queued"


def test_a_machine_with_nothing_assigned_claims_nothing(qw: QW):
    job = new_job(qw)
    idle = insert_machine(qw.conn, "idle-box", workload=None, epoch=4)
    try:
        got = claim(qw, idle, 4, "hello")
    except QueueError:
        got = None
    assert got is None and row(qw, job)["status"] == "queued"


def test_only_declared_kinds_are_claimed(qw: QW):
    legacy = insert_wl_job(qw.conn, "hello", "legacy")
    extra = new_job(qw, kind="extra")
    hello = new_job(qw, kind="hello")
    got = claim1(qw, kinds=["hello"])
    assert str(got["id"]) == jid(hello), "the kinds filter picks the matching kind, not the oldest"
    complete(qw, got)
    got = claim1(qw, kinds=["extra"])
    assert str(got["id"]) == jid(extra)
    complete(qw, got)
    for kinds in (None, ["legacy"], ["legacy", "hello"]):
        try:
            got = claim1(qw, kinds=kinds)
        except QueueError:
            got = None
        assert got is None, kinds
    assert row(qw, legacy)["status"] == "queued", "a kind the manifest does not declare is never handed out"


def test_a_kinds_filter_that_matches_nothing_leases_nothing(qw: QW):
    job = new_job(qw, kind="hello")
    try:
        got = claim1(qw, kinds=["extra"])
    except QueueError:
        got = None
    assert got is None and row(qw, job)["status"] == "queued"


def test_a_job_targeted_at_this_machine_comes_first_then_the_oldest(qw: QW):
    oldest = new_job(qw)
    targeted = new_job(qw, target=qw.m1.id)
    for_other = new_job(qw, target=qw.m2.id)
    first = claim1(qw)
    assert str(first["id"]) == jid(targeted), "targeted beats older untargeted"
    complete(qw, first)
    second = claim1(qw)
    assert str(second["id"]) == jid(oldest)
    complete(qw, second)
    assert claim1(qw) is None, "a job targeted at another machine is not claimable here"
    assert row(qw, for_other)["status"] == "queued"
    third = claim(qw, qw.m2, qw.e2)
    assert str(third["id"]) == jid(for_other)


def test_untargeted_jobs_go_out_oldest_first(qw: QW):
    a, b, c = new_job(qw), new_job(qw), new_job(qw)
    ids = []
    for _ in range(3):
        got = claim1(qw)
        ids.append(str(got["id"]))
        complete(qw, got)
    assert ids == [jid(a), jid(b), jid(c)]


def test_a_second_machine_never_gets_a_leased_job(qw: QW):
    job = new_job(qw)
    assert claim1(qw) is not None
    assert claim(qw, qw.m2, qw.e2) is None
    assert row(qw, job)["lease_machine_id"] == qw.m1.id


def test_no_double_claim_of_one_job_under_concurrency(qw: QW):
    job = new_job(qw)
    machines = [insert_machine(qw.conn, f"racer{i}", workload="hello", epoch=1, state="running") for i in range(8)]
    barrier = threading.Barrier(len(machines))
    results: list[Any] = []
    errors: list[BaseException] = []

    def run(m):
        try:
            barrier.wait()
            results.append(claim(qw, m, 1))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(m,)) for m in machines]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors, errors
    winners = [r for r in results if r is not None]
    assert len(winners) == 1 and str(winners[0]["id"]) == jid(job)
    assert row(qw, job)["lease_machine_id"] in {m.id for m in machines}


def test_many_jobs_many_claimers_each_job_goes_out_exactly_once(qw: QW):
    jobs = [new_job(qw, params={"i": i}) for i in range(6)]
    machines = [insert_machine(qw.conn, f"swarm{i}", workload="hello", epoch=1, state="running") for i in range(12)]
    barrier = threading.Barrier(len(machines))
    results: list[Any] = []
    errors: list[BaseException] = []

    def run(m):
        try:
            barrier.wait()
            results.append((m.id, claim(qw, m, 1)))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(m,)) for m in machines]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors, errors
    got = [(mid, r) for mid, r in results if r is not None]
    assert sorted(str(r["id"]) for _, r in got) == sorted(jid(j) for j in jobs)
    assert len({tok(r) for _, r in got}) == 6, "every lease has its own token"
    for mid, r in got:
        assert row(qw, r)["lease_machine_id"] == mid


# ------------------------------------------------------------------ fencing


OPS = ("renew", "release", "complete", "fail")


def run_op(w: QW, op: str, job, machine, epoch, token=None):
    return {"renew": renew, "release": release, "complete": complete, "fail": fail}[op](w, job, machine, epoch, token=token)


def frozen(r: dict[str, Any]) -> dict[str, Any]:
    keys = ("status", "lease_machine_id", "lease_epoch", "lease_token", "lease_expires_at", "progress", "checkpoint", "result", "error", "expiries")
    return {k: r[k] for k in keys}


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("case", ["wrong_token", "garbage_token", "wrong_machine", "wrong_epoch", "wrong_machine_and_epoch"])
def test_every_write_is_fenced_by_token_machine_and_epoch(qw: QW, op: str, case: str):
    job = new_job(qw)
    got = claim1(qw)
    before = frozen(row(qw, job))
    machine, epoch, token = qw.m1, qw.e1, None
    if case == "wrong_token":
        token = str(uuid.uuid4())
    elif case == "garbage_token":
        token = "not-a-uuid"
    elif case == "wrong_machine":
        machine, epoch = qw.m2, qw.e1
    elif case == "wrong_epoch":
        epoch = qw.e1 + 1
    elif case == "wrong_machine_and_epoch":
        machine, epoch = qw.m2, qw.e2
    with pytest.raises(Conflict):
        run_op(qw, op, got, machine, epoch, token)
    assert frozen(row(qw, job)) == before, "a refused write changes nothing"


@pytest.mark.parametrize("op", OPS)
def test_an_unknown_or_malformed_job_id_is_404(qw: QW, op: str):
    # Assumption: 404 like the Polymarket queue; a 409 "not leased" is also acceptable.
    for bad in ({"id": uuid.uuid4(), "lease_token": uuid.uuid4()}, {"id": "not-a-uuid", "lease_token": uuid.uuid4()}):
        with pytest.raises(QueueError) as exc:
            run_op(qw, op, bad, qw.m1, qw.e1)
        assert exc.value.status in (404, 409)


@pytest.mark.parametrize("op", OPS)
def test_a_job_that_is_not_leased_cannot_be_written(qw: QW, op: str):
    job = new_job(qw)
    fake = {"id": job["id"], "lease_token": uuid.uuid4()}
    with pytest.raises(Conflict):
        run_op(qw, op, fake, qw.m1, qw.e1)
    assert row(qw, job)["status"] == "queued"


def test_the_old_token_is_dead_after_a_release_and_the_new_claim_has_a_new_token(qw: QW):
    job = new_job(qw)
    first = claim1(qw)
    release(qw, first)
    with pytest.raises(Conflict):
        renew(qw, first)
    with pytest.raises(Conflict):
        complete(qw, first)
    second = claim(qw, qw.m2, qw.e2)
    assert str(second["id"]) == jid(job) and tok(second) != tok(first)
    with pytest.raises(Conflict):
        complete(qw, first)
    with pytest.raises(Conflict):
        complete(qw, first, qw.m2, qw.e2)
    assert row(qw, job)["status"] == "leased" and row(qw, job)["lease_machine_id"] == qw.m2.id


# ------------------------------------------------------------------ renew, release, complete, fail


def test_renew_stores_progress_and_checkpoint_extends_the_lease_and_reports_status(qw: QW):
    job = new_job(qw)
    got = claim1(qw)
    qw.conn.execute("UPDATE workload_jobs SET lease_expires_at = now() + interval '1 second' WHERE id = %s", (job["id"],))
    out = renew(qw, got, progress=0.4, checkpoint={"step": 2})
    assert out["status"] == "leased" and out["cancel"] is False
    r = row(qw, job)
    assert r["progress"] == pytest.approx(0.4) and r["checkpoint"] == {"step": 2} and r["status"] == "leased"
    left = qw.conn.execute("SELECT extract(epoch FROM lease_expires_at - now()) AS s FROM workload_jobs WHERE id = %s", (job["id"],)).fetchone()["s"]
    assert float(left) > 10, "the lease was extended"
    renew(qw, got, progress=0.6, checkpoint=None)
    assert row(qw, job)["checkpoint"] == {"step": 2}, "a renew without a checkpoint keeps the old one"
    assert row(qw, job)["progress"] == pytest.approx(0.6)


def test_release_keeps_the_checkpoint_and_clears_the_lease(qw: QW):
    job = new_job(qw)
    first = claim1(qw)
    renew(qw, first, progress=0.3, checkpoint={"step": 1})
    release(qw, first, progress=0.5, checkpoint={"step": 3})
    r = row(qw, job)
    assert r["status"] == "queued" and r["checkpoint"] == {"step": 3} and r["progress"] == pytest.approx(0.5)
    assert r["lease_token"] is None and r["lease_machine_id"] is None and r["lease_epoch"] is None and r["lease_expires_at"] is None
    assert r["expiries"] == 0, "a graceful release is not an expiry"
    again = claim(qw, qw.m2, qw.e2)
    assert again["checkpoint"] == {"step": 3} and again["progress"] == pytest.approx(0.5), "the next machine resumes from the checkpoint"
    release(qw, again, qw.m2, qw.e2, checkpoint=None, progress=0.7)
    assert row(qw, job)["checkpoint"] == {"step": 3}, "a release without a checkpoint keeps the old one"


def test_complete_stores_the_result_and_finishes_the_job(qw: QW):
    job = new_job(qw)
    got = claim1(qw)
    complete(qw, got, result={"greeting": "Hello, Ada!"})
    r = row(qw, job)
    assert r["status"] == "succeeded" and r["result"] == {"greeting": "Hello, Ada!"} and r["progress"] == 1
    assert r["finished_at"] is not None
    assert claim1(qw) is None


def test_complete_is_idempotent_and_never_overwrites_the_result(qw: QW):
    job = new_job(qw)
    got = claim1(qw)
    complete(qw, got, result={"n": 1})
    complete(qw, got, result={"n": 1})
    complete(qw, got, result={"n": 999})
    r = row(qw, job)
    assert r["status"] == "succeeded" and r["result"] == {"n": 1}
    with pytest.raises(Conflict):
        complete(qw, got, token=str(uuid.uuid4()))


def test_concurrent_completes_with_the_same_token_both_succeed_once(qw: QW):
    job = new_job(qw)
    got = claim1(qw)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run():
        try:
            barrier.wait()
            complete(qw, got, result={"n": 1})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(20) for t in threads]
    assert not errors, errors
    assert row(qw, job)["status"] == "succeeded"


def test_a_finished_job_cannot_be_completed_failed_or_released_again(qw: QW):
    job = new_job(qw)
    got = claim1(qw)
    fail(qw, got, error="first failure")
    assert row(qw, job)["status"] == "failed" and row(qw, job)["error"] == "first failure"
    for fn in (complete, renew, release):
        with pytest.raises(Conflict):
            fn(qw, got)
    other = new_job(qw)
    done = claim1(qw)
    complete(qw, done)
    for fn in (fail, renew, release):
        with pytest.raises(Conflict):
            fn(qw, done)
    assert row(qw, other)["status"] == "succeeded"


def test_a_failed_job_is_terminal_and_never_requeued(qw: QW):
    job = new_job(qw)
    got = claim1(qw)
    fail(qw, got, error="x" * 100)
    assert call(qw.pool, queue.reap) == 0
    assert row(qw, job)["status"] == "failed" and claim1(qw) is None


# ------------------------------------------------------------------ cancel


def test_cancelling_a_queued_job_cancels_it_at_once(qw: QW):
    job = new_job(qw)
    out = call(qw.pool, queue.cancel, job["id"], "owner")
    assert row(qw, job)["status"] == "cancelled" and row(qw, job)["finished_at"] is not None
    assert out is not None
    assert claim1(qw) is None


def test_cancelling_a_leased_job_requests_it_and_the_release_makes_it_cancelled(qw: QW):
    job = new_job(qw)
    got = claim1(qw)
    call(qw.pool, queue.cancel, job["id"], "owner")
    assert row(qw, job)["status"] == "cancel_requested"
    out = renew(qw, got, progress=0.5)
    assert out["cancel"] is True and out["status"] == "cancel_requested"
    release(qw, got, checkpoint={"saved": 1})
    r = row(qw, job)
    assert r["status"] == "cancelled" and r["finished_at"] is not None and r["lease_token"] is None
    assert claim(qw, qw.m2, qw.e2) is None, "a cancelled job is never run again"


def test_a_cancel_requested_job_is_not_claimable_and_the_reaper_cancels_it(qw: QW):
    job = new_job(qw)
    claim1(qw)
    call(qw.pool, queue.cancel, job["id"], "owner")
    assert claim(qw, qw.m2, qw.e2) is None
    expire_wl_lease(qw.conn, job["id"])
    assert call(qw.pool, queue.reap) == 1
    assert row(qw, job)["status"] == "cancelled"


def test_cancel_is_idempotent_and_refused_on_terminal_jobs(qw: QW):
    job = new_job(qw)
    claim1(qw)
    call(qw.pool, queue.cancel, job["id"], "owner")
    call(qw.pool, queue.cancel, job["id"], "owner")
    assert row(qw, job)["status"] == "cancel_requested"
    done = new_job(qw)
    got = claim1(qw)
    complete(qw, got)
    with pytest.raises(Conflict):
        call(qw.pool, queue.cancel, done["id"], "owner")
    with pytest.raises(NotFound):
        call(qw.pool, queue.cancel, uuid.uuid4(), "owner")
    assert row(qw, done)["status"] == "succeeded"


def test_cancel_through_the_owner_api_is_audited(qw: QW):
    job = new_job(qw)
    r = qw.client.post(f"/api/workload-jobs/{jid(job)}/cancel")
    assert r.status_code == 200, r.text
    assert row(qw, job)["status"] == "cancelled"
    assert audit_for(qw.conn, entity=jid(job)), "the cancel is in the audit log"
    assert qw.client.post(f"/api/workload-jobs/{jid(job)}/cancel").status_code in (200, 409)
    assert qw.client.post(f"/api/workload-jobs/{uuid.uuid4()}/cancel").status_code == 404


# ------------------------------------------------------------------ the reaper


def test_the_reaper_requeues_an_expired_lease_and_leaves_live_ones(qw: QW):
    stale = new_job(qw)
    live = new_job(qw)
    a = claim1(qw)
    b = claim(qw, qw.m2, qw.e2)
    assert {jid(stale), jid(live)} == {str(a["id"]), str(b["id"])}
    renew(qw, a, progress=0.3, checkpoint={"keep": "me"})
    expire_wl_lease(qw.conn, a["id"])
    before_events = len(wl_events(qw.conn, a["id"]))
    assert call(qw.pool, queue.reap) == 1
    r = row(qw, a)
    assert r["status"] == "queued" and r["expiries"] == 1 and r["checkpoint"] == {"keep": "me"}
    assert r["lease_token"] is None and r["lease_machine_id"] is None and r["lease_epoch"] is None
    assert len(wl_events(qw.conn, a["id"])) > before_events
    assert row(qw, b)["status"] == "leased" and row(qw, b)["expiries"] == 0, "a live lease is untouched"
    assert call(qw.pool, queue.reap) == 0
    with pytest.raises(Conflict):
        complete(qw, a)
    again = claim(qw, qw.m2, qw.e2)
    assert str(again["id"]) == str(a["id"]) and tok(again) != tok(a) and again["checkpoint"] == {"keep": "me"}


def test_the_reaper_fails_a_job_at_max_expiries(qw: QW):
    job = new_job(qw)
    for expected in (1, 2):
        got = claim1(qw)
        expire_wl_lease(qw.conn, got["id"])
        assert call(qw.pool, queue.reap) == 1
        assert row(qw, job)["status"] == "queued" and row(qw, job)["expiries"] == expected
    got = claim1(qw)
    expire_wl_lease(qw.conn, got["id"])
    assert call(qw.pool, queue.reap) == 1
    r = row(qw, job)
    assert r["status"] == "failed" and r["expiries"] == 3 and r["error"] and r["finished_at"] is not None
    assert r["lease_token"] is None
    assert claim1(qw) is None


def test_max_expiries_one_fails_on_the_first_expiry(qw: QW):
    job = new_job(qw, max_expiries=1)
    got = claim1(qw)
    expire_wl_lease(qw.conn, got["id"])
    call(qw.pool, queue.reap)
    assert row(qw, job)["status"] == "failed"


def test_a_job_without_max_expiries_is_requeued_forever(qw: QW):
    job = new_job(qw, max_expiries=None)
    for _ in range(6):
        got = claim1(qw)
        assert got is not None
        expire_wl_lease(qw.conn, got["id"])
        call(qw.pool, queue.reap)
    assert row(qw, job)["status"] == "queued" and row(qw, job)["expiries"] == 6


# ------------------------------------------------------------------ reassignment releases jobs


def lease_two(w: QW):
    """Two jobs leased to m1: one through a claim, one directly (a machine may hold several leases)."""
    a = new_job(w)
    ga = claim1(w)
    renew(w, ga, progress=0.2, checkpoint={"who": "a"})
    b = new_job(w)
    gb = lease_wl_job(w.conn, b["id"], w.m1, w.e1)
    return ga, gb, a, b


def test_release_machine_jobs_requeues_only_that_machines_leases(qw: QW):
    ga, gb, a, b = lease_two(qw)
    mine = new_job(qw)
    theirs = claim(qw, qw.m2, qw.e2)
    assert str(theirs["id"]) == jid(mine)
    assert call(qw.pool, queue.release_machine_jobs, qw.m1.id) == 2
    for job in (a, b):
        r = row(qw, job)
        assert r["status"] == "queued" and r["lease_token"] is None and r["lease_machine_id"] is None and r["expiries"] == 0
    assert row(qw, a)["checkpoint"] == {"who": "a"}, "the checkpoint survives"
    assert row(qw, mine)["status"] == "leased" and row(qw, mine)["lease_machine_id"] == qw.m2.id
    assert call(qw.pool, queue.release_machine_jobs, qw.m1.id) == 0
    with pytest.raises(Conflict):
        complete(qw, ga)


def test_release_machine_jobs_cancels_a_cancel_requested_job(qw: QW):
    job = new_job(qw)
    claim1(qw)
    call(qw.pool, queue.cancel, job["id"], "owner")
    assert call(qw.pool, queue.release_machine_jobs, qw.m1.id) == 1
    assert row(qw, job)["status"] == "cancelled"


def test_reassigning_a_machine_releases_its_leased_jobs(qw: QW):
    ga, gb, a, b = lease_two(qw)
    call(qw.pool, assign, qw.m1.id, "other", "owner", None)
    assert assignment_of(qw.conn, qw.m1.id)["workload"] == "other"
    for job in (a, b):
        r = row(qw, job)
        assert r["status"] == "queued" and r["lease_machine_id"] is None and r["lease_token"] is None and r["expiries"] == 0
    assert row(qw, a)["checkpoint"] == {"who": "a"}
    with pytest.raises(Conflict):
        complete(qw, ga)
    with pytest.raises(Conflict):
        renew(qw, gb)
    takeover = claim(qw, qw.m2, qw.e2)
    assert takeover is not None and takeover["id"] is not None


def test_stopping_a_machine_releases_its_leased_jobs_too(qw: QW):
    job = new_job(qw)
    claim1(qw)
    r = qw.client.post(f"/api/machines/{qw.m1.id}/assign", json={"workload": None})
    assert r.status_code == 200, r.text
    assert row(qw, job)["status"] == "queued" and row(qw, job)["lease_machine_id"] is None


def test_assigning_the_same_workload_again_keeps_the_leases(qw: QW):
    job = new_job(qw)
    got = claim1(qw)
    call(qw.pool, assign, qw.m1.id, "hello", "owner", None)
    assert row(qw, job)["status"] == "leased" and str(row(qw, job)["lease_token"]) == tok(got)
    assert assignment_of(qw.conn, qw.m1.id)["epoch"] == qw.e1


def test_a_refused_assignment_keeps_the_leases(qw: QW):
    job = new_job(qw)
    got = claim1(qw)
    r = qw.client.post(f"/api/machines/{qw.m1.id}/assign", json={"workload": "demo-site"})
    assert r.status_code == 422
    assert row(qw, job)["status"] == "leased" and str(row(qw, job)["lease_token"]) == tok(got)
    complete(qw, got)
    assert row(qw, job)["status"] == "succeeded"


def test_a_pinned_machine_keeps_its_leases_when_assign_is_refused(qw: QW):
    job = new_job(qw)
    claim1(qw)
    qw.conn.execute("UPDATE machines SET pinned = true, pinned_reason = 'live trading' WHERE id = %s", (qw.m1.id,))
    r = qw.client.post(f"/api/machines/{qw.m1.id}/assign", json={"workload": "other"})
    assert r.status_code == 409
    assert row(qw, job)["status"] == "leased"


# ------------------------------------------------------------------ the HTTP routes with run tokens


def test_the_run_routes_claim_heartbeat_and_complete(qw: QW):
    job = new_job(qw, params={"name": "Ada"})
    creds = get_run(qw.client, qw.m1, qw.e1)
    r = qw.client.post("/api/v1/wl/claim", json={"kinds": ["hello"]}, headers=creds.headers)
    assert r.status_code == 200, r.text
    j = r.json()["job"]
    assert j["id"] == jid(job) and j["params"] == {"name": "Ada"} and j["lease_token"] and j["lease_seconds"] > 0
    assert row(qw, job)["lease_machine_id"] == qw.m1.id and row(qw, job)["lease_epoch"] == qw.e1
    hb = qw.client.post(f"/api/v1/wl/jobs/{j['id']}/heartbeat", json={"lease_token": j["lease_token"], "progress": 0.5, "checkpoint": {"s": 1}}, headers=creds.headers)
    assert hb.status_code == 200 and hb.json() == {"status": "leased", "cancel": False}, hb.text
    done = qw.client.post(f"/api/v1/wl/jobs/{j['id']}/complete", json={"lease_token": j["lease_token"], "result": {"ok": 1}}, headers=creds.headers)
    assert done.status_code == 200, done.text
    assert row(qw, job)["status"] == "succeeded" and row(qw, job)["result"] == {"ok": 1}
    again = qw.client.post(f"/api/v1/wl/jobs/{j['id']}/complete", json={"lease_token": j["lease_token"], "result": {"ok": 2}}, headers=creds.headers)
    assert again.status_code == 200 and row(qw, job)["result"] == {"ok": 1}, "complete is idempotent over HTTP too"
    assert qw.client.post("/api/v1/wl/claim", json={"kinds": ["hello"]}, headers=creds.headers).json()["job"] is None


def test_the_run_routes_fence_by_machine_and_token(qw: QW):
    job = new_job(qw)
    c1 = get_run(qw.client, qw.m1, qw.e1)
    c2 = get_run(qw.client, qw.m2, qw.e2)
    j = qw.client.post("/api/v1/wl/claim", json={"kinds": ["hello"]}, headers=c1.headers).json()["job"]
    for action, body in (
        ("heartbeat", {"lease_token": j["lease_token"], "progress": 0.9}),
        ("release", {"lease_token": j["lease_token"], "checkpoint": {}, "progress": 0.9, "reason": "shutdown"}),
        ("complete", {"lease_token": j["lease_token"], "result": {}}),
        ("fail", {"lease_token": j["lease_token"], "error": "x"}),
    ):
        stolen = qw.client.post(f"/api/v1/wl/jobs/{j['id']}/{action}", json=body, headers=c2.headers)
        assert stolen.status_code == 409, (action, stolen.text)
        wrong = qw.client.post(f"/api/v1/wl/jobs/{j['id']}/{action}", json={**body, "lease_token": str(uuid.uuid4())}, headers=c1.headers)
        assert wrong.status_code == 409, (action, wrong.text)
    assert row(qw, job)["status"] == "leased" and row(qw, job)["lease_machine_id"] == qw.m1.id


def test_the_cancel_flow_over_http(qw: QW):
    job = new_job(qw)
    creds = get_run(qw.client, qw.m1, qw.e1)
    j = qw.client.post("/api/v1/wl/claim", json={}, headers=creds.headers).json()["job"]
    assert qw.client.post(f"/api/workload-jobs/{j['id']}/cancel").status_code == 200
    hb = qw.client.post(f"/api/v1/wl/jobs/{j['id']}/heartbeat", json={"lease_token": j["lease_token"], "progress": 0.1}, headers=creds.headers)
    assert hb.status_code == 200 and hb.json()["cancel"] is True
    rel = qw.client.post(f"/api/v1/wl/jobs/{j['id']}/release", json={"lease_token": j["lease_token"], "checkpoint": {}, "progress": 0.1, "reason": "stopped"}, headers=creds.headers)
    assert rel.status_code == 200, rel.text
    assert row(qw, job)["status"] == "cancelled"


def test_a_run_token_does_not_survive_reassignment_of_its_jobs(qw: QW):
    job = new_job(qw)
    creds = get_run(qw.client, qw.m1, qw.e1)
    j = qw.client.post("/api/v1/wl/claim", json={"kinds": ["hello"]}, headers=creds.headers).json()["job"]
    assert qw.client.post(f"/api/machines/{qw.m1.id}/assign", json={"workload": "other"}).status_code == 200
    r = qw.client.post(f"/api/v1/wl/jobs/{j['id']}/complete", json={"lease_token": j["lease_token"], "result": {"late": True}}, headers=creds.headers)
    assert r.status_code == 401
    assert row(qw, job)["status"] == "queued" and row(qw, job)["result"] is None


# ------------------------------------------------------------------ the Polymarket queue is untouched


def seed_polymarket(conn, make_worker):
    worker = make_worker("pm-worker", role="backtest")
    insert_job(conn, "backtest")
    insert_job(conn, "sleep")
    lease_job(conn, worker, kind="backtest")
    trade_setup(conn, mode="paper")
    return worker


def test_a_full_workload_queue_scenario_leaves_the_polymarket_tables_exactly_as_they_were(qw: QW, make_worker):
    seed_polymarket(qw.conn, make_worker)
    before = polymarket_snapshot(qw.conn)
    assert before["jobs"], "there is Polymarket data to protect"
    a, b, c = new_job(qw), new_job(qw, kind="extra"), new_job(qw, target=qw.m2.id)
    got = claim1(qw)
    renew(qw, got, checkpoint={"s": 1})
    release(qw, got, checkpoint={"s": 2})
    got = claim1(qw)
    complete(qw, got)
    other = claim1(qw)
    fail(qw, other, error="nope")
    call(qw.pool, queue.cancel, c["id"], "owner")
    d = new_job(qw, max_expiries=1)
    leased = claim(qw, qw.m2, qw.e2)
    expire_wl_lease(qw.conn, leased["id"])
    call(qw.pool, queue.reap)
    e = new_job(qw)
    claim1(qw)
    call(qw.pool, queue.release_machine_jobs, qw.m1.id)
    f = new_job(qw)
    claim1(qw)
    call(qw.pool, assign, qw.m1.id, "other", "owner", None)
    creds = get_run(qw.client, qw.m3, qw.e3)
    qw.client.post("/api/v1/wl/claim", json={"kinds": ["other"]}, headers=creds.headers)
    qw.client.post("/api/workload-jobs", json={"workload": "hello", "kind": "hello", "params": {}})
    qw.client.get("/api/workload-jobs")
    assert polymarket_snapshot(qw.conn) == before
    ids = {jid(x) for x in (a, b, c, d, e, f)}
    assert not qw.conn.execute("SELECT 1 FROM jobs WHERE id = ANY(%s::uuid[])", (list(ids),)).fetchall()
    assert not qw.conn.execute("SELECT 1 FROM job_events WHERE job_id = ANY(%s::uuid[])", (list(ids),)).fetchall()


def test_polymarket_workers_never_claim_workload_jobs(qw: QW, make_worker, heartbeat):
    insert_workload(qw.conn, plain_manifest("sleeper", "sleep", "backtest"), size_mb=10)
    wl = insert_wl_job(qw.conn, "sleeper", "backtest")
    worker = make_worker("pm-claimer", role="backtest")
    r = heartbeat(worker, want_job=True)
    assert r.status_code == 200
    assert r.json()["claimed"] == [], "only the Polymarket queue is visible to a Polymarket worker"
    assert wl_job(qw.conn, wl["id"])["status"] == "queued"
    pm = insert_job(qw.conn, "backtest")
    r = heartbeat(worker, want_job=True)
    assert [c["id"] for c in r.json()["claimed"]] == [str(pm["id"])], "control: the worker does claim a Polymarket job"
    assert wl_job(qw.conn, wl["id"])["status"] == "queued"


def test_the_polymarket_reaper_and_the_workload_reaper_stay_apart(qw: QW, make_worker):
    from tests.conftest import expire_lease, job_row

    worker = make_worker("pm-expiry", role="backtest")
    pm = lease_job(qw.conn, worker, kind="backtest")
    expire_lease(qw.conn, pm["id"])
    job = new_job(qw)
    got = claim1(qw)
    expire_wl_lease(qw.conn, got["id"])
    assert call(qw.pool, queue.reap) == 1, "the workload reaper counts only its own table"
    assert job_row(qw.conn, pm["id"])["status"] == "leased", "the workload reaper left the Polymarket lease alone"
    assert row(qw, job)["status"] == "queued"
    from host import queue as pm_queue

    assert len(call(qw.pool, pm_queue.reap)) == 1
    assert row(qw, job)["status"] == "queued" and row(qw, job)["expiries"] == 1
