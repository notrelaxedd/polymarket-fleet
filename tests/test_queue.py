"""Queue semantics exercised directly through host.queue (no HTTP unless noted)."""
from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest

from host import auth, queue
from host.errors import Conflict, Unauthorized
from tests.conftest import expire_lease, heartbeat_body, job_row, worker_row


def hb(pool, worker, **kw: Any) -> dict[str, Any]:
    """Run one heartbeat transaction for a worker through the pool."""
    with pool.connection() as c:
        return queue.process_heartbeat(c, worker.id, heartbeat_body(**kw))


def create(pool, kind: str = "sleep", **kw: Any) -> queue.CreateResult:
    with pool.connection() as c:
        return queue.create_job(c, kind, kw.pop("params", {"seconds": 5}), **kw)


def claim_one(pool, worker, role: str = "backtest", epoch: int = 1) -> dict[str, Any]:
    reply = hb(pool, worker, reported_role=role, acked_epoch=epoch, want_job=True)
    assert len(reply["claimed"]) == 1, reply
    return reply["claimed"][0]


def run_threads(n: int, fn) -> list[Any]:
    """Run fn(i) in n threads released together; return their results."""
    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n
    errors: list[BaseException] = []

    def runner(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[i] = fn(i)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    return results


def test_no_double_claim_under_concurrency(pool, make_worker):
    workers = [make_worker(f"w{i}", role="backtest") for i in range(20)]
    job = create(pool).job
    replies = run_threads(20, lambda i: hb(pool, workers[i], reported_role="backtest", acked_epoch=1))
    claimed = [c for r in replies for c in r["claimed"]]
    assert len(claimed) == 1
    assert claimed[0]["id"] == str(job["id"])


def test_targeted_claimed_before_untargeted(pool, conn, make_worker):
    w = make_worker("w", role="backtest")
    untargeted = create(pool).job
    targeted = create(pool, target=w.id).job
    assert worker_row(conn, w.id)["role_epoch"] == 1, "role unchanged, no epoch bump"
    first = claim_one(pool, w)
    assert first["id"] == str(targeted["id"])
    assert first["lease_seconds"] == 30
    assert job_row(conn, untargeted["id"])["status"] == "queued"


def test_release_does_not_increment_expiries(pool, conn, make_worker):
    w = make_worker("w", role="backtest")
    job = create(pool).job
    claimed = claim_one(pool, w)
    reply = hb(
        pool, w, reported_role="backtest", want_job=False,
        released=[{"id": claimed["id"], "lease_token": claimed["lease_token"],
                   "progress": 0.4, "checkpoint": {"elapsed": 2}}],
    )
    assert reply["lost"] == [] and reply["claimed"] == []
    row = job_row(conn, job["id"])
    assert row["status"] == "queued"
    assert row["expiries"] == 0
    assert row["checkpoint"] == {"elapsed": 2}
    assert row["lease_token"] is None and row["lease_worker_id"] is None
    events = [e["event"] for e in conn.execute(
        "SELECT event FROM job_events WHERE job_id = %s ORDER BY id", (job["id"],)).fetchall()]
    assert events == ["created", "claimed", "released"]


def test_reaper_requeues_with_checkpoint_then_fails_at_max_expiries(pool, conn, make_worker):
    w = make_worker("w", role="backtest")
    job = create(pool).job
    for expected in (1, 2):
        claimed = claim_one(pool, w)
        hb(pool, w, reported_role="backtest", want_job=False,
           jobs=[{"id": claimed["id"], "lease_token": claimed["lease_token"],
                  "progress": 0.1 * expected, "checkpoint": {"elapsed": expected}}])
        expire_lease(conn, job["id"])
        with pool.connection() as c:
            reaped = queue.reap(c)
        assert [str(r["id"]) for r in reaped] == [str(job["id"])]
        row = job_row(conn, job["id"])
        assert row["status"] == "queued"
        assert row["expiries"] == expected
        assert row["checkpoint"] == {"elapsed": expected}
        assert row["lease_token"] is None
    claim_one(pool, w)
    expire_lease(conn, job["id"])
    with pool.connection() as c:
        queue.reap(c)
    row = job_row(conn, job["id"])
    assert row["status"] == "failed"
    assert row["expiries"] == 3
    assert row["error"] == "failed after 3 expiries (last: lease expired)"
    assert row["checkpoint"] == {"elapsed": 2}
    assert row["finished_at"] is not None


def test_cancel_requested_then_expiry_becomes_cancelled(pool, conn, make_worker):
    w = make_worker("w", role="backtest")
    job = create(pool).job
    claimed = claim_one(pool, w)
    with pool.connection() as c:
        assert queue.cancel_job(c, job["id"])["status"] == "cancel_requested"
    reply = hb(pool, w, reported_role="backtest", want_job=False,
               jobs=[{"id": claimed["id"], "lease_token": claimed["lease_token"]}])
    assert reply["preempt"] == [claimed["id"]]
    expire_lease(conn, job["id"])
    with pool.connection() as c:
        queue.reap(c)
    assert job_row(conn, job["id"])["status"] == "cancelled"


def test_cancel_queued_and_release_of_cancel_requested(pool, conn, make_worker):
    w = make_worker("w", role="backtest")
    queued = create(pool).job
    with pool.connection() as c:
        assert queue.cancel_job(c, queued["id"])["status"] == "cancelled"
    job = create(pool).job
    claimed = claim_one(pool, w)
    with pool.connection() as c:
        queue.cancel_job(c, job["id"])
    hb(pool, w, reported_role="backtest", want_job=False,
       released=[{"id": claimed["id"], "lease_token": claimed["lease_token"], "checkpoint": {"elapsed": 1}}])
    assert job_row(conn, job["id"])["status"] == "cancelled"


def test_stale_lease_token_is_lost_and_refused(pool, conn, make_worker, client):
    w = make_worker("w", role="backtest")
    job = create(pool).job
    stale = claim_one(pool, w)
    expire_lease(conn, job["id"])
    with pool.connection() as c:
        queue.reap(c)
    reply = hb(pool, w, reported_role="backtest", want_job=False,
               jobs=[{"id": stale["id"], "lease_token": stale["lease_token"], "progress": 0.5}])
    assert reply["lost"] == [stale["id"]]
    with pool.connection() as c:
        with pytest.raises(Conflict):
            queue.checkpoint(c, job["id"], stale["lease_token"], {"x": 1}, 0.5)
    r = client.post(f"/api/v1/jobs/{job['id']}/checkpoint", headers=w.headers,
                    json={"lease_token": stale["lease_token"], "checkpoint": {"x": 1}, "progress": 0.5})
    assert r.status_code == 409, r.text
    r = client.post(f"/api/v1/jobs/{job['id']}/complete", headers=w.headers,
                    json={"lease_token": stale["lease_token"], "result": {}})
    assert r.status_code == 409


def test_checkpoint_complete_and_fail_paths(pool, conn, make_worker):
    w = make_worker("w", role="backtest")
    job = create(pool).job
    claimed = claim_one(pool, w)
    with pool.connection() as c:
        assert queue.checkpoint(c, job["id"], claimed["lease_token"], {"elapsed": 3}, 0.6) == "leased"
        done = queue.complete(c, job["id"], claimed["lease_token"], {"slept": 5})
        assert done["status"] == "succeeded" and done["progress"] == 1
        again = queue.complete(c, job["id"], claimed["lease_token"], {"slept": 5})
        assert again["status"] == "succeeded"
        with pytest.raises(Conflict):
            queue.fail(c, job["id"], claimed["lease_token"], "late")
    job2 = create(pool).job
    claimed2 = claim_one(pool, w)
    with pool.connection() as c:
        failed = queue.fail(c, job2["id"], claimed2["lease_token"], "boom")
    assert failed["status"] == "failed" and failed["error"] == "boom"
    assert job_row(conn, job2["id"])["lease_worker_id"] is None


def test_any_idle_sets_role_targets_and_auto_role(pool, conn, make_worker):
    w = make_worker("w")
    make_worker("offline", online=False)
    make_worker("disabled", enabled=False)
    result = create(pool, target="any_idle")
    assert result.created and not result.waiting_for_idle_worker
    assert result.job["target_worker_id"] == w.id
    row = worker_row(conn, w.id)
    assert row["desired_role"] == "backtest"
    assert row["role_epoch"] == 2
    assert row["auto_role"] is True
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'auto_role'").fetchone()["n"] == 1


def test_any_idle_waits_when_nobody_is_idle(pool, make_worker):
    make_worker("busy", role="train")
    result = create(pool, target="any_idle")
    assert result.waiting_for_idle_worker
    assert result.job["target_worker_id"] is None


def test_chosen_worker_sets_role_and_preempts_other_role(pool, conn, make_worker):
    from tests.conftest import insert_model

    w = make_worker("w", role="train")
    model = insert_model(conn)
    train_job = create(pool, kind="train", params={"model_id": str(model["id"]), "through": {"season": 2024, "week": 1}}).job
    claimed = claim_one(pool, w, role="train")
    assert claimed["id"] == str(train_job["id"])
    result = create(pool, target=w.id)
    row = worker_row(conn, w.id)
    assert row["desired_role"] == "backtest" and row["role_epoch"] == 2 and row["auto_role"] is True
    assert job_row(conn, train_job["id"])["preempt_requested"] is True
    reply = hb(pool, w, reported_role="train", acked_epoch=1, want_job=True,
               jobs=[{"id": claimed["id"], "lease_token": claimed["lease_token"]}])
    assert reply["preempt"] == [claimed["id"]]
    assert reply["claimed"] == []
    assert reply["desired_role"] == "backtest" and reply["role_epoch"] == 2
    assert result.job["target_worker_id"] == w.id


def test_chosen_unknown_worker_is_404(pool):
    from host.errors import NotFound
    with pytest.raises(NotFound):
        create(pool, target="w_nope")


def test_unknown_kind_is_400(pool):
    from host.errors import BadRequest
    with pytest.raises(BadRequest):
        create(pool, kind="mystery")


def test_auto_return_to_idle_only_when_nothing_remains(pool, conn, make_worker):
    w = make_worker("w")
    job = create(pool, target="any_idle").job
    # Switched but the targeted job is still queued: stay in the role.
    reply = hb(pool, w, reported_role="backtest", acked_epoch=2, want_job=False)
    assert reply["desired_role"] == "backtest" and reply["role_epoch"] == 2
    claimed = claim_one(pool, w, epoch=2)
    assert claimed["id"] == str(job["id"])
    # Leased job: stay in the role.
    reply = hb(pool, w, reported_role="backtest", acked_epoch=2, want_job=False,
               jobs=[{"id": claimed["id"], "lease_token": claimed["lease_token"]}])
    assert reply["desired_role"] == "backtest"
    with pool.connection() as c:
        queue.complete(c, job["id"], claimed["lease_token"], {"slept": 5})
    reply = hb(pool, w, reported_role="backtest", acked_epoch=2, want_job=True)
    assert reply["desired_role"] == "idle" and reply["role_epoch"] == 3
    assert reply["claimed"] == []
    row = worker_row(conn, w.id)
    assert row["auto_role"] is False and row["desired_role"] == "idle"


def test_owner_role_change_is_not_auto_returned(pool, conn, make_worker):
    w = make_worker("w")
    with pool.connection() as c:
        row = queue.set_role(c, w.id, "backtest", actor="owner")
    assert row["role_epoch"] == 2 and row["auto_role"] is False
    reply = hb(pool, w, reported_role="backtest", acked_epoch=2, want_job=True)
    assert reply["desired_role"] == "backtest"
    with pool.connection() as c:
        from host.errors import BadRequest
        with pytest.raises(BadRequest):
            queue.set_role(c, w.id, "gardener")


def test_concurrent_any_idle_never_picks_same_worker(pool, make_worker):
    workers = {make_worker(f"w{i}").id for i in range(3)}
    results = run_threads(10, lambda i: create(pool, target="any_idle"))
    targets = [r.job["target_worker_id"] for r in results if r.job["target_worker_id"]]
    assert len(targets) == 3
    assert set(targets) == workers
    assert sum(1 for r in results if r.waiting_for_idle_worker) == 7


def test_dispatcher_assigns_waiting_job_when_worker_goes_idle(pool, conn, make_worker):
    w = make_worker("w", role="train")
    job = create(pool, target="any_idle").job
    assert job["target_worker_id"] is None
    with pool.connection() as c:
        assert queue.dispatch(c) == []
    conn.execute("UPDATE workers SET desired_role='idle', reported_role='idle' WHERE id = %s", (w.id,))
    with pool.connection() as c:
        assigned = queue.dispatch(c)
    assert [str(a["id"]) for a in assigned] == [str(job["id"])]
    assert job_row(conn, job["id"])["target_worker_id"] == w.id
    row = worker_row(conn, w.id)
    assert row["desired_role"] == "backtest" and row["auto_role"] is True
    claimed = claim_one(pool, w, epoch=row["role_epoch"])
    assert claimed["id"] == str(job["id"])


def test_idempotency_key_returns_same_job(pool):
    first = create(pool, idempotency_key="k1")
    second = create(pool, idempotency_key="k1")
    assert first.created and not second.created
    assert first.job["id"] == second.job["id"]
    results = run_threads(5, lambda i: create(pool, idempotency_key="k2"))
    assert len({str(r.job["id"]) for r in results}) == 1


def test_claim_refused_until_epoch_acked(pool, conn, make_worker):
    w = make_worker("w")
    create(pool)
    with pool.connection() as c:
        queue.set_role(c, w.id, "backtest")
    assert hb(pool, w, reported_role="backtest", acked_epoch=1)["claimed"] == []
    assert hb(pool, w, reported_role="idle", acked_epoch=2)["claimed"] == []
    assert len(hb(pool, w, reported_role="backtest", acked_epoch=2)["claimed"]) == 1


def test_disabled_blocks_claims_and_kill_only_blocks_trade(pool, conn, make_worker):
    """Disabled workers never claim; the kill flag refuses trade claims only, batch
    roles keep claiming under kill (a stopped backtest hides research for nothing)."""
    w = make_worker("w", role="backtest", enabled=False)
    create(pool)
    assert hb(pool, w, reported_role="backtest")["claimed"] == []
    with pool.connection() as c:
        queue.set_enabled(c, w.id, True)
    conn.execute("UPDATE settings SET value = 'true' WHERE key = 'kill_switch'")
    reply = hb(pool, w, reported_role="backtest")
    assert reply["kill"] is True and len(reply["claimed"]) == 1, "backtest still claims under kill"
    trader = make_worker("t", role="trade")
    conn.execute("INSERT INTO jobs (kind, role) VALUES ('trade', 'trade')")
    reply = hb(pool, trader, reported_role="trade")
    assert reply["kill"] is True and reply["claimed"] == [], "trade never claims under kill"
    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'kill_switch'")
    assert hb(pool, trader, reported_role="trade")["claimed"] == [], "a trade worker claims with want_jobs, not want_job"


def test_release_reason_is_stored_and_oom_counts_as_expiry(pool, conn, make_worker):
    """A released[] entry may carry a reason; `oom` increments expiries like a lease
    expiry and fails the job at max_expiries, the other reasons do not."""
    w = make_worker("w", role="backtest")
    job = create(pool).job
    claimed = claim_one(pool, w)
    hb(pool, w, reported_role="backtest", want_job=False,
       released=[{"id": claimed["id"], "lease_token": claimed["lease_token"], "reason": "drain"}])
    row = job_row(conn, job["id"])
    assert row["status"] == "queued" and row["expiries"] == 0
    claimed = claim_one(pool, w)
    hb(pool, w, reported_role="backtest", want_job=False,
       released=[{"id": claimed["id"], "lease_token": claimed["lease_token"], "reason": "bogus"}])
    assert job_row(conn, job["id"])["expiries"] == 0, "unknown reasons are ignored"
    for expected in (1, 2):
        claimed = claim_one(pool, w)
        hb(pool, w, reported_role="backtest", want_job=False,
           released=[{"id": claimed["id"], "lease_token": claimed["lease_token"],
                      "checkpoint": {"elapsed": expected}, "reason": "oom"}])
        row = job_row(conn, job["id"])
        assert row["status"] == "queued" and row["expiries"] == expected
        assert row["checkpoint"] == {"elapsed": expected}
    claimed = claim_one(pool, w)
    with pool.connection() as c:
        status = queue.checkpoint(c, job["id"], claimed["lease_token"], {"elapsed": 3}, 0.3, True, w.id, "oom")
    assert status == "failed"
    row = job_row(conn, job["id"])
    assert row["status"] == "failed" and row["expiries"] == 3
    assert row["error"] == "failed after 3 expiries (last: out of memory)" and row["finished_at"] is not None
    assert row["lease_token"] is None and row["checkpoint"] == {"elapsed": 3}
    details = [e["detail"] for e in conn.execute(
        "SELECT detail FROM job_events WHERE job_id = %s AND event = 'released' ORDER BY id", (job["id"],)).fetchall()]
    assert details == [
        {"status": "queued", "reason": "drain"},
        {"status": "queued"},
        {"status": "queued", "reason": "oom", "expiries": 1},
        {"status": "queued", "reason": "oom", "expiries": 2},
        {"status": "failed", "reason": "oom", "expiries": 3},
    ]
    # cancel wins over oom, the expiry is still counted
    job2 = create(pool).job
    claimed = claim_one(pool, w)
    with pool.connection() as c:
        queue.cancel_job(c, job2["id"])
    hb(pool, w, reported_role="backtest", want_job=False,
       released=[{"id": claimed["id"], "lease_token": claimed["lease_token"], "reason": "oom"}])
    row = job_row(conn, job2["id"])
    assert row["status"] == "cancelled" and row["expiries"] == 1 and row["error"] is None


def test_held_jobs_on_reregister_renew_with_new_tokens(pool, conn, make_worker):
    w = make_worker("w", role="backtest")
    job = create(pool).job
    old = claim_one(pool, w)
    with pool.connection() as c:
        reply = queue.register(c, {"worker_id": w.id, "worker_token": w.token, "hostname": "box",
                                   "python_version": "3.13", "code_version": "abc", "boot_id": "b1"})
    assert reply["worker_id"] == w.id
    assert reply["worker_token"] != w.token
    assert [h["id"] for h in reply["held_jobs"]] == [str(job["id"])]
    new_token = reply["held_jobs"][0]["lease_token"]
    assert new_token != old["lease_token"]
    assert reply["held_jobs"][0]["checkpoint"] is None
    with pool.connection() as c:
        with pytest.raises(Unauthorized):
            auth.verify_worker_token(c, w.id, w.token)
        auth.verify_worker_token(c, w.id, reply["worker_token"])
    stale = hb(pool, w, reported_role="backtest", want_job=False,
               jobs=[{"id": old["id"], "lease_token": old["lease_token"]}])
    assert stale["lost"] == [old["id"]]
    fresh = hb(pool, w, reported_role="backtest", want_job=False,
               jobs=[{"id": old["id"], "lease_token": new_token, "progress": 0.7}])
    assert fresh["lost"] == []
    assert job_row(conn, job["id"])["progress"] == pytest.approx(0.7)
    events = [e["event"] for e in conn.execute(
        "SELECT event FROM job_events WHERE job_id = %s ORDER BY id", (job["id"],)).fetchall()]
    assert "re-leased" in events
    # An expired lease is not held: the reaper owns it.
    expire_lease(conn, job["id"])
    with pool.connection() as c:
        reply = queue.register(c, {"worker_id": w.id, "worker_token": reply["worker_token"]})
    assert reply["held_jobs"] == []


def test_lost_claim_reply_is_reoffered_not_doubled(pool, conn, make_worker):
    """MEDIUM: a worker that holds a lease it never reported gets that lease back
    (same job, same token) instead of a second job."""
    w = make_worker("w", role="backtest")
    j1 = create(pool).job
    j2 = create(pool).job
    first = claim_one(pool, w)
    assert first["id"] == str(j1["id"])
    # The reply above was "lost": the worker still wants a job and reports nothing.
    again = hb(pool, w, reported_role="backtest", want_job=True)
    assert [(c["id"], c["lease_token"]) for c in again["claimed"]] == [(first["id"], first["lease_token"])]
    assert job_row(conn, j2["id"])["status"] == "queued"
    assert job_row(conn, j1["id"])["lease_worker_id"] == w.id
    events = [e["event"] for e in conn.execute(
        "SELECT event FROM job_events WHERE job_id = %s ORDER BY id", (j1["id"],)).fetchall()]
    assert events == ["created", "claimed", "re-offered"]
    # Reported now: nothing is re-offered and nothing new is claimed while it runs.
    running = hb(pool, w, reported_role="backtest", want_job=False,
                 jobs=[{"id": first["id"], "lease_token": first["lease_token"]}])
    assert running["claimed"] == [] and running["lost"] == []
    with pool.connection() as c:
        queue.complete(c, j1["id"], first["lease_token"], {})
    second = claim_one(pool, w)
    assert second["id"] == str(j2["id"])
    # An unreported lease whose cancel was requested is cancelled outright.
    with pool.connection() as c:
        queue.cancel_job(c, j2["id"])
    reply = hb(pool, w, reported_role="backtest", want_job=True)
    assert reply["claimed"] == [], "nothing is re-offered or claimed for a cancelled orphan"
    assert job_row(conn, j2["id"])["status"] == "cancelled"
    assert hb(pool, w, reported_role="backtest", want_job=True)["preempt"] == []


def test_stale_heartbeat_cannot_move_acked_epoch_backwards(pool, conn, make_worker):
    w = make_worker("w")
    with pool.connection() as c:
        queue.set_role(c, w.id, "backtest")
    create(pool)
    assert len(hb(pool, w, reported_role="backtest", acked_epoch=2)["claimed"]) == 1
    hb(pool, w, reported_role="idle", acked_epoch=1, want_job=False)
    assert worker_row(conn, w.id)["acked_epoch"] == 2


def test_heartbeat_token_is_checked_under_the_row_lock(pool, make_worker):
    """LOW: the token is verified inside the locking UPDATE, so a heartbeat that raced a
    rotation is refused instead of acting with a dead token."""
    w = make_worker("w")
    with pool.connection() as c:
        with pytest.raises(Unauthorized):
            queue.process_heartbeat(c, w.id, heartbeat_body(), auth.hash_token("stale"))
        assert queue.process_heartbeat(c, w.id, heartbeat_body(), auth.hash_token(w.token))["lost"] == []


def test_system_chosen_target_is_cleared_on_release_and_expiry(pool, conn, make_worker):
    """MEDIUM: an any_idle/dispatcher target is dropped when the job comes back to the
    queue so another worker can take it; an owner-chosen target stays."""
    w1 = make_worker("w1")
    job = create(pool, target="any_idle").job
    assert job["target_worker_id"] == w1.id and job["target_auto"] is True
    claimed = claim_one(pool, w1, epoch=2)
    with pool.connection() as c:
        queue.set_role(c, w1.id, "idle", actor="owner")
    reply = hb(pool, w1, reported_role="backtest", acked_epoch=2, want_job=False,
               jobs=[{"id": claimed["id"], "lease_token": claimed["lease_token"]}])
    assert reply["preempt"] == [claimed["id"]]
    hb(pool, w1, reported_role="idle", acked_epoch=3, want_job=False,
       released=[{"id": claimed["id"], "lease_token": claimed["lease_token"], "checkpoint": {"elapsed": 7}}])
    row = job_row(conn, job["id"])
    assert row["status"] == "queued" and row["target_worker_id"] is None and row["target_auto"] is False
    w2 = make_worker("w2")
    with pool.connection() as c:
        assigned = queue.dispatch(c)
    assert [str(a["id"]) for a in assigned] == [str(job["id"])]
    assert job_row(conn, job["id"])["target_worker_id"] == w2.id
    claimed = claim_one(pool, w2, epoch=2)
    assert claimed["checkpoint"] == {"elapsed": 7}
    expire_lease(conn, job["id"])
    with pool.connection() as c:
        queue.reap(c)
    row = job_row(conn, job["id"])
    assert row["status"] == "queued" and row["target_worker_id"] is None
    # w1 is idle again and may be picked for the next any_idle job.
    later = create(pool, target="any_idle").job
    assert later["target_worker_id"] == w1.id
    # Owner-chosen targets survive release and expiry.
    w3 = make_worker("w3", role="backtest")
    pinned = create(pool, target=w3.id).job
    assert pinned["target_auto"] is False
    claimed = claim_one(pool, w3)
    hb(pool, w3, reported_role="backtest", want_job=False,
       released=[{"id": claimed["id"], "lease_token": claimed["lease_token"]}])
    assert job_row(conn, pinned["id"])["target_worker_id"] == w3.id
    claimed = claim_one(pool, w3)
    expire_lease(conn, pinned["id"])
    with pool.connection() as c:
        queue.reap(c)
    assert job_row(conn, pinned["id"])["target_worker_id"] == w3.id


def test_auto_return_waits_for_untargeted_work_of_its_role(pool, conn, make_worker):
    """LOW: an auto-role worker claims a waiting untargeted job instead of flipping to
    idle and being flipped straight back by the dispatcher."""
    w = make_worker("w")
    first = create(pool, target="any_idle").job
    claimed = claim_one(pool, w, epoch=2)
    assert claimed["id"] == str(first["id"])
    second = create(pool, target="any_idle")
    assert second.waiting_for_idle_worker
    with pool.connection() as c:
        queue.complete(c, first["id"], claimed["lease_token"], {})
    reply = hb(pool, w, reported_role="backtest", acked_epoch=2, want_job=True)
    assert reply["desired_role"] == "backtest" and reply["role_epoch"] == 2
    assert [c["id"] for c in reply["claimed"]] == [str(second.job["id"])]
    with pool.connection() as c:
        queue.complete(c, second.job["id"], reply["claimed"][0]["lease_token"], {})
    reply = hb(pool, w, reported_role="backtest", acked_epoch=2, want_job=True)
    assert reply["desired_role"] == "idle" and reply["role_epoch"] == 3 and reply["claimed"] == []


# ------------------------------------------------------------------ step 3: job params


def test_backtest_params_are_validated_and_settings_copied(pool, conn):
    from host.errors import BadRequest
    from tests.conftest import backtest_metrics, ingest_fixture, insert_model

    ingest_fixture(conn)
    model = insert_model(conn, metrics=backtest_metrics())
    job = create(pool, kind="backtest", params={"family": "elo_blend", "params": {"k": 20, "hfa": 60.5}}).job
    assert job["params"] == {
        "family": "elo_blend", "params": {"k": 20, "hfa": 60.5},
        "fee_model": {"taker_rate": 0.05, "half_spread": 0.01}, "default_bankroll_cents": 10000,
        "max_bet_cents": 2500, "trade_max_games": 6, "backtest_seasons": [2010, 2025],
    }, "the limits in force are copied in and the null last season is resolved from the games table"
    by_model = create(pool, kind="backtest", params={"model_id": str(model["id"]), "seasons": [2018, None]}).job
    assert by_model["params"]["model_id"] == str(model["id"]) and by_model["params"]["seasons"] == [2018, 2025]
    assert "family" not in by_model["params"] and by_model["role"] == "backtest"
    with pool.connection() as c:
        c.execute("UPDATE settings SET value = '{\"taker_rate\": 0.02, \"half_spread\": 0.0}' WHERE key = 'fee_model'")
        c.execute("UPDATE settings SET value = '[2015, 2020]' WHERE key = 'backtest_seasons'")
        c.execute("UPDATE settings SET value = '900' WHERE key = 'max_bet_cents'")
    later = create(pool, kind="backtest", params={"family": "elo_blend", "params": {}}).job
    assert later["params"]["fee_model"] == {"taker_rate": 0.02, "half_spread": 0.0}
    assert later["params"]["backtest_seasons"] == [2015, 2020] and later["params"]["max_bet_cents"] == 900
    assert job["params"]["max_bet_cents"] == 2500, "an earlier job keeps the limits it was sent with"
    assert create(pool, kind="backtest", params={"family": "elo_blend"}).job["params"]["params"] == {}, "params default to the family defaults"
    bad = [
        {}, {"family": "nope", "params": {}}, {"family": "elo_blend", "params": []},
        {"family": "elo_blend", "params": {"k": "fast"}}, {"family": "elo_blend", "params": {}, "extra": 1},
        {"model_id": "garbage"}, {"model_id": str(uuid.uuid4())}, {"model_id": str(model["id"]), "family": "elo_blend"},
        {"family": "elo_blend", "params": {}, "seasons": [2020]}, {"family": "elo_blend", "params": {}, "seasons": [2020, 2010]},
        {"family": "elo_blend", "params": {}, "seasons": [1990, None]}, {"family": "elo_blend", "params": {}, "seasons": "2020"},
    ]
    for params in bad:
        with pytest.raises(BadRequest) as info:
            create(pool, kind="backtest", params=params)
        if params.get("model_id") == str(uuid.uuid4()):
            assert info.value.message == "unknown model"
    with pytest.raises(BadRequest) as info:
        create(pool, kind="backtest", params={"model_id": str(uuid.uuid4())})
    assert info.value.message == "unknown model"


def test_model_search_and_train_params(pool, conn):
    from host.errors import BadRequest
    from tests.conftest import insert_model

    job = create(pool, kind="model_search", params={"family": "elo_blend"}).job
    assert job["role"] == "model_search"
    assert job["params"]["n"] == 200 and job["params"]["seed"] == 0 and job["params"]["top_k"] == 5
    assert job["params"]["backtest_seasons"] == [2010, None], "no games yet: the null stays"
    full = create(pool, kind="model_search", params={"family": "elo_blend", "n": 5000, "seed": -7, "top_k": 20, "seasons": [2016, 2019]}).job
    assert full["params"]["n"] == 5000 and full["params"]["seed"] == -7 and full["params"]["seasons"] == [2016, 2019]
    for params in [
        {}, {"family": "elo_blend", "n": 0}, {"family": "elo_blend", "n": 5001}, {"family": "elo_blend", "n": 2.5},
        {"family": "elo_blend", "top_k": 0}, {"family": "elo_blend", "top_k": 21}, {"family": "elo_blend", "seed": "x"},
        {"family": "elo_blend", "model_id": "x"}, {"family": "elo_blend", "n": True},
    ]:
        with pytest.raises(BadRequest):
            create(pool, kind="model_search", params=params)
    model = insert_model(conn)
    train = create(pool, kind="train", params={"model_id": str(model["id"]), "through": {"season": 2024, "week": 10}}).job
    assert train["role"] == "train" and train["params"]["through"] == {"season": 2024, "week": 10}
    assert train["params"]["model_id"] == str(model["id"]) and train["params"]["trade_max_games"] == 6
    for params in [
        {}, {"model_id": str(model["id"])}, {"model_id": str(uuid.uuid4()), "through": {"season": 2024, "week": 1}},
        {"model_id": str(model["id"]), "through": [2024, 1]}, {"model_id": str(model["id"]), "through": {"season": 2024, "week": 0}},
        {"model_id": str(model["id"]), "through": {"season": 2024, "week": 23}}, {"model_id": str(model["id"]), "through": {"season": 1990, "week": 1}},
        {"model_id": str(model["id"]), "through": {"season": 2024, "week": 1}, "n": 3},
    ]:
        with pytest.raises(BadRequest):
            create(pool, kind="train", params=params)
    # sleep keeps its shape: seconds in range when present, other keys tolerated, nothing copied in.
    assert create(pool, kind="sleep", params={"seconds": 3, "note": "x"}).job["params"] == {"seconds": 3, "note": "x"}
    assert create(pool, kind="sleep", params={}).job["params"] == {}
    for params in [{"seconds": 0}, {"seconds": 86401}, {"seconds": "7"}]:
        with pytest.raises(BadRequest):
            create(pool, kind="sleep", params=params)


def test_batch_jobs_refuse_untestable_seasons_and_unknown_param_names(pool, conn):
    """MEDIUM / LOW: a seasons range no backtest can score, and a hyperparameter the
    family does not know, are refused at creation instead of producing empty or
    default runs under the owner's chosen name."""
    from host.errors import BadRequest
    from tests.conftest import ingest_fixture

    for params in ({"family": "elo_blend", "params": {"K": 30, "HFA": 80}}, {"family": "elo_blend", "params": {"k": 30, "hfa_": 1}}):
        with pytest.raises(BadRequest) as info:
            create(pool, kind="backtest", params=params)
        assert info.value.message.startswith("unknown elo_blend params: ")
    # Without games there is nothing to judge a range against.
    assert create(pool, kind="model_search", params={"family": "elo_blend", "seasons": [2030, 2030]}).job["params"]["seasons"] == [2030, 2030]
    ingest_fixture(conn)  # 2016-2025, every season with moneylines
    for seasons in ([2030, 2030], [2016, 2018], [2017, 2018]):
        for kind, params in (("backtest", {"family": "elo_blend", "params": {}}), ("model_search", {"family": "elo_blend"})):
            with pytest.raises(BadRequest) as info:
                create(pool, kind=kind, params={**params, "seasons": seasons})
            assert info.value.message.startswith(f"no testable season in [{seasons[0]}, {seasons[1]}]"), (kind, seasons)
    ok = create(pool, kind="model_search", params={"family": "elo_blend", "seasons": [2016, 2019]}).job
    assert ok["params"]["seasons"] == [2016, 2019], "2019 has three earlier seasons with lines"
    assert create(pool, kind="backtest", params={"family": "elo_blend", "params": {"k": 30}, "seasons": [2019, None]}).job["params"]["seasons"] == [2019, 2025]


# ------------------------------------------------------------------ step 4: trade claims


def test_trade_claims_up_to_want_jobs(pool, conn, make_worker):
    """A trade worker claims up to `want_jobs` trade jobs per heartbeat (LIMIT n, oldest
    first); `want_job` (bool) still means one job for batch roles and nothing for trade."""
    from tests.conftest import insert_game, make_assignment, set_setting

    insert_game(conn)
    set_setting(conn, "max_paper_models_per_game", 10)
    created = [make_assignment(conn) for _ in range(4)]
    trader = make_worker("t", role="trade")
    assert hb(pool, trader, reported_role="trade", want_job=True)["claimed"] == []
    reply = hb(pool, trader, reported_role="trade", want_job=False, want_jobs=3)
    assert [c["id"] for c in reply["claimed"]] == [a["job_id"] for a in created[:3]]
    assert all(c["kind"] == "trade" and c["lease_seconds"] == 30 for c in reply["claimed"])
    for a in created[:3]:
        assert job_row(conn, a["job_id"])["lease_worker_id"] == trader.id
    held = [{"id": c["id"], "lease_token": c["lease_token"]} for c in reply["claimed"]]
    reply = hb(pool, trader, reported_role="trade", want_jobs=3, jobs=held)
    assert [c["id"] for c in reply["claimed"]] == [created[3]["job_id"]], "only the one left"
    events = [e["event"] for e in conn.execute(
        "SELECT event FROM job_events WHERE job_id = %s ORDER BY id", (created[0]["job_id"],)).fetchall()]
    assert events == ["created", "claimed"]
    tester = make_worker("b", role="backtest")
    create(pool)
    assert hb(pool, tester, reported_role="backtest", want_job=False, want_jobs=5)["claimed"] == [], "want_jobs is for trade"
    assert len(hb(pool, tester, reported_role="backtest", want_job=True)["claimed"]) == 1


def test_trade_claims_refused_under_kill_and_concurrent_claims_never_double(pool, conn, make_worker):
    from tests.conftest import insert_game, make_assignment

    insert_game(conn)
    created = [make_assignment(conn) for _ in range(3)]
    conn.execute("UPDATE settings SET value = 'true' WHERE key = 'kill_switch'")
    trader = make_worker("t", role="trade")
    reply = hb(pool, trader, reported_role="trade", want_jobs=3)
    assert reply["kill"] is True and reply["claimed"] == []
    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'kill_switch'")
    workers = [make_worker(f"t{i}", role="trade") for i in range(6)]
    replies = run_threads(6, lambda i: hb(pool, workers[i], reported_role="trade", want_jobs=2))
    claimed = [c["id"] for r in replies for c in r["claimed"]]
    assert sorted(claimed) == sorted(a["job_id"] for a in created) and len(claimed) == 3


def test_trade_jobs_never_fail_by_expiry(pool, conn, make_worker):
    """max_expiries is NULL on trade jobs: a crashed trade worker's job is requeued every
    time its lease expires and any trade worker reclaims it."""
    from tests.conftest import insert_game, make_assignment

    insert_game(conn)
    a = make_assignment(conn)
    assert job_row(conn, a["job_id"])["max_expiries"] is None
    trader = make_worker("t", role="trade")
    for n in range(1, 6):
        reply = hb(pool, trader, reported_role="trade", want_jobs=1)
        assert [c["id"] for c in reply["claimed"]] == [a["job_id"]]
        expire_lease(conn, a["job_id"])
        with pool.connection() as c:
            queue.reap(c)
        row = job_row(conn, a["job_id"])
        assert row["status"] == "queued" and row["expiries"] == n and row["error"] is None
    other = make_worker("u", role="trade")
    assert [c["id"] for c in hb(pool, other, reported_role="trade", want_jobs=1)["claimed"]] == [a["job_id"]]
    hb(pool, other, reported_role="trade", want_jobs=0, released=[{
        "id": a["job_id"], "lease_token": str(job_row(conn, a["job_id"])["lease_token"]), "reason": "oom"}])
    row = job_row(conn, a["job_id"])
    assert row["status"] == "queued" and row["expiries"] == 6, "even oom never fails a trade job"
