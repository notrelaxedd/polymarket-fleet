"""Outbound approval guardrail (docs/workloads-design.md sections 4.2 and 8): a workload with
declared outbound actions can only queue them; the host sends a row only after the owner approved it,
exactly once, with that workload's own host-only credentials.

Assumptions where the contract is silent:
- Approving or rejecting a row that is not `pending` is 409 (a repeated approve may also be an
  idempotent 200, but never changes the row).
- A repeated `dedupe_key` returns the existing row untouched: payload and status never change.
- With no sender for a kind, an approved row is not marked sent (it stays approved or becomes failed).
- Audit rows are found by `entity == str(action id)`; their action names are not asserted.
- Tests marked xfail document behaviour the contract does not require but a safe system should have.
"""
from __future__ import annotations

import threading
import uuid

import pytest

from host.errors import BadRequest, Conflict, Forbidden, NotFound
from host.workloads import loop as wl_loop
from host.workloads.errors import TooMany
from host.workloads.outbound import MAX_PENDING, approve, expire_old, queue_action, reject, send_approved
from tests.wl_helpers import (
    INTRUDER, OWNER, ALPHA_FROM_VALUE, ALPHA_KEY_VALUE, ALPHA_SMTP_VALUE, BRAVO_SMTP_VALUE, FakeSender,
    SecretWorld, audit_count, audit_for, bearer, call, database_text, get_run, insert_machine, insert_outbound,
    insert_wl_job, insert_workload, make_manifest, outbound_row, put_secret, secret_world, secrets_key, strict_client,
    wl_events,
)

OWNER_ACTOR = "owner@example.com"


def mail(**kw):
    return {"to": "someone@example.com", "subject": "Hello", "body": "Body text", **kw}


def pending_email(conn, workload="alpha", **kw):
    return insert_outbound(conn, workload, "email", payload=mail(), **kw)


def approve_api(client, action_id, headers=None):
    return client.post(f"/api/outbound/{action_id}/approve", headers=headers or OWNER)


def reject_api(client, action_id, reason="no thanks", headers=None):
    return client.post(f"/api/outbound/{action_id}/reject", json={"reason": reason}, headers=headers or OWNER)


def status(conn, action_id) -> str:
    return outbound_row(conn, action_id)["status"]


# ------------------------------------------------------------------ declared kinds only


def test_a_workload_without_declared_actions_gets_403_on_the_run_route(secret_world: SecretWorld):
    w = secret_world
    quiet = get_run(w.client, w.quiet, w.quiet_epoch)
    for kind in ("email", "log"):
        r = w.client.post("/api/v1/wl/outbound", json={"kind": kind, "payload": mail(), "dedupe_key": f"q-{kind}"}, headers=quiet.headers)
        assert r.status_code == 403, (kind, r.text)
    assert w.conn.execute("SELECT count(*) AS n FROM outbound_actions").fetchone()["n"] == 0


def test_the_function_raises_forbidden_for_an_undeclared_kind(secret_world: SecretWorld, pool):
    w = secret_world
    insert_workload(w.conn, make_manifest("logonly", actions=("log",)))
    for workload, kind in (("quiet", "email"), ("quiet", "log"), ("logonly", "email"), ("logonly", "sms"), ("alpha", "sms")):
        with pytest.raises(Forbidden):
            call(pool, queue_action, workload=workload, machine_id=None, job_id=None, kind=kind, payload=mail(), dedupe_key="k")
    assert w.conn.execute("SELECT count(*) AS n FROM outbound_actions").fetchone()["n"] == 0


def test_a_declared_kind_creates_a_pending_row_scoped_to_the_callers_workload_and_machine(secret_world: SecretWorld):
    w = secret_world
    job = insert_wl_job(w.conn, "alpha", "alpha")
    alpha = get_run(w.client, w.alpha, w.alpha_epoch)
    r = w.client.post(
        "/api/v1/wl/outbound",
        json={"kind": "email", "payload": mail(), "dedupe_key": "mail-1", "job_id": str(job["id"]),
              # a hostile body tries to set fields the workload must not control
              "status": "approved", "workload": "bravo", "machine_id": w.bravo.id, "decided_by": "me"},
        headers=alpha.headers,
    )
    assert r.status_code < 300, r.text
    body = r.json()
    assert body["status"] == "pending"
    row = outbound_row(w.conn, body["id"])
    assert (row["workload"], row["machine_id"], row["kind"], row["status"]) == ("alpha", w.alpha.id, "email", "pending")
    assert row["payload"] == mail() and row["dedupe_key"] == "mail-1" and str(row["job_id"]) == str(job["id"])
    assert row["decided_by"] is None and row["decided_at"] is None and row["sent_at"] is None


def test_a_log_kind_is_allowed_where_declared_and_stays_pending(secret_world: SecretWorld):
    w = secret_world
    alpha = get_run(w.client, w.alpha, w.alpha_epoch)
    r = w.client.post("/api/v1/wl/outbound", json={"kind": "log", "payload": {"m": "x"}, "dedupe_key": "log-1"}, headers=alpha.headers)
    assert r.status_code < 300 and status(w.conn, r.json()["id"]) == "pending"


def test_bravo_cannot_queue_a_log_it_did_not_declare(secret_world: SecretWorld):
    w = secret_world
    bravo = get_run(w.client, w.bravo, w.bravo_epoch)
    r = w.client.post("/api/v1/wl/outbound", json={"kind": "log", "payload": {}, "dedupe_key": "x"}, headers=bravo.headers)
    assert r.status_code == 403


def test_an_action_cannot_be_linked_to_another_workloads_job(secret_world: SecretWorld):
    """Assumption: refused (400/404), or accepted with the job link dropped; never stored pointing at bravo's job."""
    w = secret_world
    theirs = insert_wl_job(w.conn, "bravo", "bravo")
    alpha = get_run(w.client, w.alpha, w.alpha_epoch)
    r = w.client.post("/api/v1/wl/outbound", json={"kind": "log", "payload": {}, "dedupe_key": "linked", "job_id": str(theirs["id"])},
                      headers=alpha.headers)
    if r.status_code < 300:
        assert outbound_row(w.conn, r.json()["id"])["job_id"] is None
    else:
        assert r.status_code in (400, 404)
    assert not w.conn.execute("SELECT 1 FROM outbound_actions WHERE job_id = %s", (theirs["id"],)).fetchall()


def test_a_payload_above_64_kib_is_400(secret_world: SecretWorld, pool):
    w = secret_world
    alpha = get_run(w.client, w.alpha, w.alpha_epoch)
    big = {"to": "a@b.c", "subject": "s", "body": "x" * (70 * 1024)}
    r = w.client.post("/api/v1/wl/outbound", json={"kind": "email", "payload": big, "dedupe_key": "big"}, headers=alpha.headers)
    assert r.status_code == 400, r.status_code
    with pytest.raises(BadRequest):
        call(pool, queue_action, workload="alpha", machine_id=None, job_id=None, kind="email", payload=big, dedupe_key="big2")
    ok = w.client.post("/api/v1/wl/outbound", json={"kind": "email", "payload": mail(body="x" * 2000), "dedupe_key": "fine"}, headers=alpha.headers)
    assert ok.status_code < 300
    assert w.conn.execute("SELECT count(*) AS n FROM outbound_actions").fetchone()["n"] == 1


# ------------------------------------------------------------------ dedupe


def test_a_repeated_dedupe_key_returns_the_same_row(secret_world: SecretWorld, pool):
    w = secret_world
    alpha = get_run(w.client, w.alpha, w.alpha_epoch)
    first = w.client.post("/api/v1/wl/outbound", json={"kind": "email", "payload": mail(), "dedupe_key": "dup"}, headers=alpha.headers).json()
    second = w.client.post("/api/v1/wl/outbound", json={"kind": "email", "payload": mail(), "dedupe_key": "dup"}, headers=alpha.headers).json()
    assert first["id"] == second["id"] and second["status"] == "pending"
    row = call(pool, queue_action, workload="alpha", machine_id=w.alpha.id, job_id=None, kind="email", payload=mail(), dedupe_key="dup")
    assert str(row["id"]) == first["id"]
    assert w.conn.execute("SELECT count(*) AS n FROM outbound_actions").fetchone()["n"] == 1


def test_dedupe_keys_are_per_workload(secret_world: SecretWorld, pool):
    w = secret_world
    a = call(pool, queue_action, workload="alpha", machine_id=None, job_id=None, kind="email", payload=mail(), dedupe_key="shared")
    b = call(pool, queue_action, workload="bravo", machine_id=None, job_id=None, kind="email", payload=mail(), dedupe_key="shared")
    assert a["id"] != b["id"]
    assert w.conn.execute("SELECT count(*) AS n FROM outbound_actions").fetchone()["n"] == 2


def test_a_retried_dedupe_key_cannot_swap_the_payload_after_approval(secret_world: SecretWorld, pool):
    """Approve payload A, then the workload re-queues the same key with payload B: the approved row is untouched."""
    w = secret_world
    original = mail(subject="Approved subject", body="the approved body")
    row = call(pool, queue_action, workload="alpha", machine_id=None, job_id=None, kind="email", payload=original, dedupe_key="swap")
    assert approve_api(w.client, row["id"]).status_code == 200
    again = call(pool, queue_action, workload="alpha", machine_id=None, job_id=None, kind="email",
                 payload=mail(to="attacker@evil.example", subject="Evil", body="evil"), dedupe_key="swap")
    assert str(again["id"]) == str(row["id"])
    stored = outbound_row(w.conn, row["id"])
    assert stored["payload"] == original and stored["status"] == "approved"
    sender = FakeSender()
    assert call(pool, send_approved, {"email": sender, "log": FakeSender()}) == 1
    assert sender.calls[0][0]["payload"] == original


def test_a_rejected_action_stays_rejected_when_the_workload_retries(secret_world: SecretWorld, pool):
    w = secret_world
    row = call(pool, queue_action, workload="alpha", machine_id=None, job_id=None, kind="email", payload=mail(), dedupe_key="again")
    assert reject_api(w.client, row["id"]).status_code == 200
    again = call(pool, queue_action, workload="alpha", machine_id=None, job_id=None, kind="email", payload=mail(), dedupe_key="again")
    assert str(again["id"]) == str(row["id"])
    assert status(w.conn, row["id"]) == "rejected"
    sender = FakeSender()
    assert call(pool, send_approved, {"email": sender}) == 0 and sender.calls == []


# ------------------------------------------------------------------ the 500 pending cap


def bulk_pending(conn, workload: str, n: int) -> None:
    conn.execute(
        """
        INSERT INTO outbound_actions (workload, kind, payload, dedupe_key)
        SELECT %s, 'email', '{}'::jsonb, 'bulk-' || g FROM generate_series(1, %s) g
        """,
        (workload, n),
    )


def test_the_cap_is_500_pending_per_workload(secret_world: SecretWorld, pool):
    w = secret_world
    assert MAX_PENDING == 500
    bulk_pending(w.conn, "alpha", MAX_PENDING - 1)
    alpha = get_run(w.client, w.alpha, w.alpha_epoch)
    ok = w.client.post("/api/v1/wl/outbound", json={"kind": "email", "payload": mail(), "dedupe_key": "n500"}, headers=alpha.headers)
    assert ok.status_code < 300, ok.text
    r = w.client.post("/api/v1/wl/outbound", json={"kind": "email", "payload": mail(), "dedupe_key": "n501"}, headers=alpha.headers)
    assert r.status_code == 429, r.text
    with pytest.raises(TooMany):
        call(pool, queue_action, workload="alpha", machine_id=None, job_id=None, kind="email", payload=mail(), dedupe_key="n502")
    assert w.conn.execute("SELECT count(*) AS n FROM outbound_actions WHERE workload='alpha'").fetchone()["n"] == MAX_PENDING
    # Another workload is not affected.
    bravo = get_run(w.client, w.bravo, w.bravo_epoch)
    assert w.client.post("/api/v1/wl/outbound", json={"kind": "email", "payload": mail(), "dedupe_key": "b1"}, headers=bravo.headers).status_code < 300


def test_decided_rows_do_not_count_toward_the_cap(secret_world: SecretWorld, pool):
    w = secret_world
    bulk_pending(w.conn, "alpha", MAX_PENDING)
    with pytest.raises(TooMany):
        call(pool, queue_action, workload="alpha", machine_id=None, job_id=None, kind="email", payload=mail(), dedupe_key="over")
    one = w.conn.execute("SELECT id FROM outbound_actions WHERE workload='alpha' LIMIT 1").fetchone()["id"]
    assert approve_api(w.client, one).status_code == 200
    row = call(pool, queue_action, workload="alpha", machine_id=None, job_id=None, kind="email", payload=mail(), dedupe_key="room-again")
    assert row["status"] == "pending"


# ------------------------------------------------------------------ nothing is sent unless approved, exactly once


ALL_STATUSES = ("pending", "approved", "rejected", "expired", "sent", "failed", "sending")


def test_send_approved_sends_only_approved_rows_and_only_once(secret_world: SecretWorld, pool):
    w = secret_world
    rows = {s: insert_outbound(w.conn, "alpha", "log", payload={"s": s}, status=s) for s in ALL_STATUSES}
    log, email = FakeSender(result={"logged": True}), FakeSender()
    assert call(pool, send_approved, {"log": log, "email": email}) == 1
    assert log.ids == [str(rows["approved"]["id"])] and email.calls == []
    after = {s: outbound_row(w.conn, r["id"]) for s, r in rows.items()}
    assert after["approved"]["status"] == "sent" and after["approved"]["sent_at"] is not None
    assert after["approved"]["result"] == {"logged": True}
    for s in ALL_STATUSES:
        if s != "approved":
            assert after[s]["status"] == s, f"a {s} row must not change"
    assert call(pool, send_approved, {"log": log, "email": email}) == 0
    assert len(log.calls) == 1, "calling twice never sends twice"


def test_a_pending_rejected_or_expired_row_is_never_sent_whatever_the_sender(secret_world: SecretWorld, pool):
    w = secret_world
    for s in ("pending", "rejected", "expired"):
        insert_outbound(w.conn, "alpha", "email", payload=mail(), status=s)
    sender = FakeSender()
    for _ in range(3):
        assert call(pool, send_approved, {"email": sender, "log": sender}) == 0
    assert sender.calls == []


def test_the_sender_gets_the_action_and_only_its_workloads_host_only_secrets(secret_world: SecretWorld, pool):
    w = secret_world
    a = pending_email(w.conn, "alpha")
    b = pending_email(w.conn, "bravo")
    assert approve_api(w.client, a["id"]).status_code == 200 and approve_api(w.client, b["id"]).status_code == 200
    sender = FakeSender()
    assert call(pool, send_approved, {"email": sender, "log": FakeSender()}) == 2
    by_id = {str(action["id"]): (action, secrets) for action, secrets in sender.calls}
    action, secrets = by_id[str(a["id"])]
    assert action["workload"] == "alpha" and action["kind"] == "email" and action["payload"] == mail()
    assert secrets == {"SMTP_URL": ALPHA_SMTP_VALUE, "EMAIL_FROM": ALPHA_FROM_VALUE}
    action, secrets = by_id[str(b["id"])]
    assert action["workload"] == "bravo"
    assert secrets == {"SMTP_URL": BRAVO_SMTP_VALUE}, "bravo's sender never sees alpha's credentials"
    for _, s in sender.calls:
        assert ALPHA_KEY_VALUE not in s.values(), "container secrets are never given to a sender"


def test_a_workload_without_host_only_secrets_gets_none_not_everyone_elses(secret_world: SecretWorld, pool):
    w = secret_world
    insert_workload(w.conn, make_manifest("logonly", actions=("log",)))
    row = insert_outbound(w.conn, "logonly", "log", payload={"m": 1}, status="approved")
    sender = FakeSender()
    assert call(pool, send_approved, {"log": sender}) == 1
    assert sender.calls[0][1] == {}
    assert str(row["id"]) == sender.ids[0]


def test_a_failing_sender_marks_the_row_failed_and_is_not_retried_nor_blocks_others(secret_world: SecretWorld, pool):
    w = secret_world
    bad = insert_outbound(w.conn, "alpha", "email", payload=mail(), status="approved")
    good = insert_outbound(w.conn, "alpha", "log", payload={"m": 1}, status="approved")
    email = FakeSender(fail=RuntimeError("smtp is down"))
    log = FakeSender()
    call(pool, send_approved, {"email": email, "log": log})
    assert status(w.conn, good["id"]) == "sent" and log.ids == [str(good["id"])]
    row = outbound_row(w.conn, bad["id"])
    assert row["status"] == "failed" and "smtp is down" in (row["error"] or "")
    call(pool, send_approved, {"email": email, "log": log})
    assert len(email.calls) == 1, "a failed send is not retried behind the owner's back"
    assert status(w.conn, bad["id"]) == "failed"


def test_a_missing_sender_never_marks_a_row_sent(secret_world: SecretWorld, pool):
    w = secret_world
    email_row = insert_outbound(w.conn, "alpha", "email", payload=mail(), status="approved")
    log_row = insert_outbound(w.conn, "alpha", "log", payload={}, status="approved")
    log = FakeSender()
    call(pool, send_approved, {"log": log})
    assert status(w.conn, log_row["id"]) == "sent"
    assert status(w.conn, email_row["id"]) in ("approved", "failed")
    call(pool, send_approved, {})
    assert status(w.conn, email_row["id"]) != "sent"


def test_a_sender_error_that_echoes_the_credentials_is_not_stored_verbatim(secret_world: SecretWorld, pool):
    w = secret_world
    row = insert_outbound(w.conn, "alpha", "email", payload=mail(), status="approved")
    sender = FakeSender(fail=RuntimeError(f"login failed for {ALPHA_SMTP_VALUE}"))
    call(pool, send_approved, {"email": sender})
    stored = outbound_row(w.conn, row["id"])
    assert ALPHA_SMTP_VALUE not in (stored["error"] or "") and "alpha-smtp-pw-77ab" not in database_text(w.conn)
    page = w.client.get("/outbound")
    assert "alpha-smtp-pw-77ab" not in page.text


def test_two_concurrent_send_passes_send_each_approved_row_exactly_once(secret_world: SecretWorld, pool):
    w = secret_world
    ids = [str(insert_outbound(w.conn, "alpha", "log", payload={"i": i}, status="approved")["id"]) for i in range(12)]
    sender = FakeSender(delay=0.02)
    barrier = threading.Barrier(3)
    counts: list[int] = []
    errors: list[BaseException] = []

    def run():
        try:
            barrier.wait()
            counts.append(call(pool, send_approved, {"log": sender, "email": sender}))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(3)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors, errors
    assert sorted(sender.ids) == sorted(ids), "every row sent once, none twice"
    assert sum(counts) == 12
    assert w.conn.execute("SELECT count(*) AS n FROM outbound_actions WHERE status = 'sent'").fetchone()["n"] == 12


def test_the_full_flow_pending_approved_sent_is_visible_to_the_workload(secret_world: SecretWorld, pool):
    w = secret_world
    alpha = get_run(w.client, w.alpha, w.alpha_epoch)
    r = w.client.post("/api/v1/wl/outbound", json={"kind": "log", "payload": {"m": "go"}, "dedupe_key": "flow"}, headers=alpha.headers)
    action_id = r.json()["id"]
    seen = lambda: w.client.get(f"/api/v1/wl/outbound/{action_id}", headers=alpha.headers).json()  # noqa: E731
    assert seen()["status"] == "pending"
    call(pool, send_approved, {"log": FakeSender(), "email": FakeSender()})
    assert seen()["status"] == "pending", "a pending row is not sent"
    approve_api(w.client, action_id)
    assert seen()["status"] == "approved", "approval alone does not send"
    call(pool, send_approved, {"log": FakeSender(result={"logged": True}), "email": FakeSender()})
    body = seen()
    assert body["status"] == "sent" and body["result"] == {"logged": True} and body["error"] is None
    assert set(body) >= {"id", "status", "error", "result"}


# ------------------------------------------------------------------ approve and reject


def test_approve_sets_the_decider_and_audits(secret_world: SecretWorld):
    w = secret_world
    row = pending_email(w.conn)
    audits = audit_count(w.conn)
    r = approve_api(w.client, row["id"])
    assert r.status_code == 200, r.text
    after = outbound_row(w.conn, row["id"])
    assert after["status"] == "approved" and after["decided_by"] == OWNER_ACTOR and after["decided_at"] is not None
    assert after["sent_at"] is None
    rows = audit_for(w.conn, entity=str(row["id"]))
    assert len(rows) == 1 and rows[0]["actor"] == OWNER_ACTOR
    assert audit_count(w.conn) == audits + 1


def test_reject_sets_the_decider_and_audits(secret_world: SecretWorld):
    w = secret_world
    row = pending_email(w.conn)
    r = reject_api(w.client, row["id"], reason="not today")
    assert r.status_code == 200, r.text
    after = outbound_row(w.conn, row["id"])
    assert after["status"] == "rejected" and after["decided_by"] == OWNER_ACTOR and after["decided_at"] is not None
    rows = audit_for(w.conn, entity=str(row["id"]))
    assert len(rows) == 1 and rows[0]["actor"] == OWNER_ACTOR


@pytest.mark.parametrize("state", ["rejected", "sent", "failed", "expired", "sending"])
def test_a_row_that_is_not_pending_cannot_be_approved_or_rejected(secret_world: SecretWorld, state):
    w = secret_world
    row = insert_outbound(w.conn, "alpha", "email", payload=mail(), status=state)
    audits = audit_count(w.conn)
    assert approve_api(w.client, row["id"]).status_code == 409
    assert reject_api(w.client, row["id"]).status_code == 409
    assert status(w.conn, row["id"]) == state
    assert audit_count(w.conn) == audits


def test_an_approved_row_cannot_be_rejected_and_a_second_approval_changes_nothing(secret_world: SecretWorld):
    w = secret_world
    row = pending_email(w.conn)
    assert approve_api(w.client, row["id"]).status_code == 200
    first = outbound_row(w.conn, row["id"])
    assert reject_api(w.client, row["id"]).status_code == 409
    again = approve_api(w.client, row["id"], headers={"Tailscale-User-Login": "someone-else@example.com"})
    assert again.status_code in (200, 409)
    after = outbound_row(w.conn, row["id"])
    assert after["status"] == "approved" and after["decided_by"] == first["decided_by"] and after["decided_at"] == first["decided_at"]


def test_unknown_and_malformed_ids_are_404(secret_world: SecretWorld):
    w = secret_world
    for bad in (str(uuid.uuid4()), "not-a-uuid", "0"):
        assert approve_api(w.client, bad).status_code == 404
        assert reject_api(w.client, bad).status_code == 404


def test_the_functions_raise_conflict_and_not_found(secret_world: SecretWorld, pool):
    w = secret_world
    row = pending_email(w.conn)
    out = call(pool, approve, row["id"], OWNER_ACTOR, None)
    assert out["status"] == "approved"
    with pytest.raises(Conflict):
        call(pool, reject, row["id"], OWNER_ACTOR, None, "late")
    with pytest.raises(Conflict):
        call(pool, approve, insert_outbound(w.conn, "alpha", "email", payload=mail(), status="expired")["id"], OWNER_ACTOR, None)
    with pytest.raises(NotFound):
        call(pool, approve, uuid.uuid4(), OWNER_ACTOR, None)


def test_a_concurrent_approve_and_reject_has_exactly_one_winner(secret_world: SecretWorld, pool):
    w = secret_world
    for _ in range(5):
        row = pending_email(w.conn)
        barrier = threading.Barrier(2)
        results: list[str] = []

        def run(fn, *args):
            barrier.wait()
            try:
                call(pool, fn, row["id"], OWNER_ACTOR, None, *args)
                results.append("ok")
            except Conflict:
                results.append("conflict")

        t1 = threading.Thread(target=run, args=(approve,))
        t2 = threading.Thread(target=run, args=(reject, "x"))
        t1.start(); t2.start(); t1.join(20); t2.join(20)
        assert sorted(results) == ["conflict", "ok"], results
        assert status(w.conn, row["id"]) in ("approved", "rejected")
        assert len(audit_for(w.conn, entity=str(row["id"]))) == 1


# ------------------------------------------------------------------ only the owner may decide


def test_only_the_owner_can_approve_or_reject(secret_world: SecretWorld, config, make_worker):
    w = secret_world
    row = pending_email(w.conn)
    run = get_run(w.client, w.alpha, w.alpha_epoch)
    worker = make_worker("outbound-probe")
    with strict_client(config) as c:
        for headers in ({}, INTRUDER, bearer(run.token), w.alpha.headers, bearer(worker.token),
                        {**bearer(run.token), **INTRUDER}):
            for path, body in ((f"/api/outbound/{row['id']}/approve", None), (f"/api/outbound/{row['id']}/reject", {"reason": "x"})):
                r = c.post(path, json=body, headers=headers)
                assert r.status_code in (401, 403), (path, headers, r.status_code)
            assert c.get("/api/outbound", headers=headers).status_code in (401, 403)
    assert status(w.conn, row["id"]) == "pending"
    assert audit_for(w.conn, entity=str(row["id"])) == []


def test_a_request_from_a_registered_machine_ip_cannot_approve(secret_world: SecretWorld, config):
    w = secret_world
    insert_machine(w.conn, "approver-box", remote_ip="100.64.0.7")
    row = pending_email(w.conn)
    with strict_client(config) as c:
        r = c.post(f"/api/outbound/{row['id']}/approve", headers={**OWNER, "X-Forwarded-For": "9.9.9.9, 100.64.0.7"})
        assert r.status_code == 403, r.text
        r = c.post(f"/api/outbound/{row['id']}/reject", json={"reason": "x"}, headers={**OWNER, "X-Forwarded-For": "100.64.0.7"})
        assert r.status_code == 403
        assert status(w.conn, row["id"]) == "pending"
        ok = c.post(f"/api/outbound/{row['id']}/approve", headers={**OWNER, "X-Forwarded-For": "100.64.0.99"})
        assert ok.status_code == 200, ok.text
    assert status(w.conn, row["id"]) == "approved"


def test_a_foreign_origin_cannot_approve(secret_world: SecretWorld, config):
    w = secret_world
    row = pending_email(w.conn)
    with strict_client(config) as c:
        r = c.post(f"/api/outbound/{row['id']}/approve", headers={**OWNER, "Origin": "http://evil.example"})
        assert r.status_code == 403
    assert status(w.conn, row["id"]) == "pending"


# ------------------------------------------------------------------ expiry


def test_expire_old_moves_only_week_old_pending_rows(secret_world: SecretWorld, pool):
    w = secret_world
    old = insert_outbound(w.conn, "alpha", "email", payload=mail(), age_days=8)
    fresh = insert_outbound(w.conn, "alpha", "email", payload=mail(), age_days=6.5)
    old_approved = insert_outbound(w.conn, "alpha", "email", payload=mail(), status="approved", age_days=9)
    old_sent = insert_outbound(w.conn, "alpha", "email", payload=mail(), status="sent", age_days=9)
    old_rejected = insert_outbound(w.conn, "alpha", "email", payload=mail(), status="rejected", age_days=9)
    assert call(pool, expire_old) == 1
    assert status(w.conn, old["id"]) == "expired" and status(w.conn, fresh["id"]) == "pending"
    assert status(w.conn, old_approved["id"]) == "approved"
    assert status(w.conn, old_sent["id"]) == "sent" and status(w.conn, old_rejected["id"]) == "rejected"
    assert call(pool, expire_old) == 0


def test_expire_old_takes_a_days_argument(secret_world: SecretWorld, pool):
    w = secret_world
    two_days = insert_outbound(w.conn, "alpha", "email", payload=mail(), age_days=2)
    assert call(pool, expire_old) == 0
    assert call(pool, expire_old, 1) == 1
    assert status(w.conn, two_days["id"]) == "expired"


def test_an_expired_row_cannot_be_approved_or_sent(secret_world: SecretWorld, pool):
    w = secret_world
    old = insert_outbound(w.conn, "alpha", "email", payload=mail(), age_days=8)
    call(pool, expire_old)
    assert approve_api(w.client, old["id"]).status_code == 409
    sender = FakeSender()
    assert call(pool, send_approved, {"email": sender}) == 0 and sender.calls == []


def test_the_loop_expires_old_pending_rows(secret_world: SecretWorld, pool):
    w = secret_world
    old = insert_outbound(w.conn, "alpha", "email", payload=mail(), age_days=8)
    wl_loop.run_once(pool)
    wl_loop.send_once(pool)
    assert status(w.conn, old["id"]) == "expired"


# ------------------------------------------------------------------ the real senders, through the loop


class SmtpLog:
    """Every SMTP session the senders opened: init args, logins and the messages sent."""

    def __init__(self) -> None:
        self.sessions: list[dict] = []


@pytest.fixture
def fake_smtp(monkeypatch):
    """Replaces the methods of smtplib.SMTP and SMTP_SSL themselves (not the names), so a sender that
    captured the classes at import time still never opens a socket."""
    import smtplib

    log = SmtpLog()

    def init(self, *args, **kwargs):
        self._fake = {"init": (args, kwargs), "logins": [], "messages": []}
        log.sessions.append(self._fake)
        self.local_hostname = "localhost"
        self.sock = None
        self.esmtp_features = {}
        self.does_esmtp = True

    def login(self, user, password, *a, **k):
        self._fake["logins"].append((user, password))
        return (235, b"ok")

    def send_message(self, msg, *a, **k):
        self._fake["messages"].append(str(msg))
        return {}

    def sendmail(self, from_addr, to_addrs, msg, *a, **k):
        text = msg if isinstance(msg, str) else msg.decode("utf-8", "replace")
        self._fake["messages"].append(f"{from_addr}\n{to_addrs}\n{text}")
        return {}

    def ok(self, *a, **k):
        return (250, b"ok")

    for cls in (smtplib.SMTP, smtplib.SMTP_SSL):
        monkeypatch.setattr(cls, "__init__", init)
    monkeypatch.setattr(smtplib.SMTP, "login", login)
    monkeypatch.setattr(smtplib.SMTP, "send_message", send_message)
    monkeypatch.setattr(smtplib.SMTP, "sendmail", sendmail)
    monkeypatch.setattr(smtplib.SMTP, "__enter__", lambda self: self)
    monkeypatch.setattr(smtplib.SMTP, "__exit__", lambda self, *exc: False)
    for name in ("starttls", "ehlo", "helo", "ehlo_or_helo_if_needed", "noop", "quit", "close", "connect", "set_debuglevel"):
        monkeypatch.setattr(smtplib.SMTP, name, ok)
    return log


def test_the_loop_never_connects_to_smtp_for_unapproved_rows(secret_world: SecretWorld, pool, fake_smtp):
    w = secret_world
    for s in ("pending", "rejected", "expired"):
        insert_outbound(w.conn, "alpha", "email", payload=mail(), status=s)
    wl_loop.run_once(pool)
    wl_loop.send_once(pool)
    wl_loop.run_once(pool)
    wl_loop.send_once(pool)
    assert fake_smtp.sessions == []


def test_the_real_email_sender_uses_only_its_own_workloads_credentials_and_sends_once(secret_world: SecretWorld, pool, fake_smtp):
    w = secret_world
    insert_workload(w.conn, make_manifest("charlie", kinds=["charlie"], host_only=("SMTP_URL", "EMAIL_FROM"), actions=("email",)))
    put_secret(w.client, "charlie", "SMTP_URL", "smtps://charlieuser:charlie-pw-5511@smtp.charlie.example:465")
    put_secret(w.client, "charlie", "EMAIL_FROM", "charlie@charlie.example")
    a = insert_outbound(w.conn, "alpha", "email", payload=mail(to="a-target@example.com", subject="Alpha subject"), status="approved")
    c = insert_outbound(w.conn, "charlie", "email", payload=mail(to="c-target@example.com", subject="Charlie subject"), status="approved")
    pending = insert_outbound(w.conn, "alpha", "email", payload=mail(to="never@example.com", subject="Pending subject"))
    wl_loop.run_once(pool)
    wl_loop.send_once(pool)
    assert status(w.conn, a["id"]) == "sent" and status(w.conn, c["id"]) == "sent"
    assert status(w.conn, pending["id"]) == "pending"
    sessions = fake_smtp.sessions
    assert len(sessions) == 2, sessions
    by_login = {s["logins"][0][0]: s for s in sessions if s["logins"]}
    assert set(by_login) == {"alphauser", "charlieuser"}
    assert by_login["alphauser"]["logins"][0] == ("alphauser", "alpha-smtp-pw-77ab")
    assert by_login["charlieuser"]["logins"][0] == ("charlieuser", "charlie-pw-5511")
    alpha_mail, charlie_mail = "\n".join(by_login["alphauser"]["messages"]), "\n".join(by_login["charlieuser"]["messages"])
    assert "a-target@example.com" in alpha_mail and "Alpha subject" in alpha_mail and ALPHA_FROM_VALUE in alpha_mail
    assert "c-target@example.com" in charlie_mail and "Charlie subject" in charlie_mail and "charlie@charlie.example" in charlie_mail
    for text in (alpha_mail, charlie_mail):
        assert "never@example.com" not in text and "Pending subject" not in text
    assert "charlie@" not in alpha_mail and "charlie-pw" not in alpha_mail
    assert ALPHA_FROM_VALUE not in charlie_mail and "alpha-smtp-pw" not in charlie_mail
    wl_loop.run_once(pool)
    wl_loop.send_once(pool)
    assert len(fake_smtp.sessions) == 2, "a sent row is never sent again"


def test_the_real_log_sender_records_an_event_on_the_job_and_only_after_approval(secret_world: SecretWorld, pool):
    w = secret_world
    job = insert_wl_job(w.conn, "alpha", "alpha")
    waiting = insert_outbound(w.conn, "alpha", "log", payload={"m": "hi"}, job_id=job["id"])
    wl_loop.run_once(pool)
    wl_loop.send_once(pool)
    assert status(w.conn, waiting["id"]) == "pending"
    assert not [e for e in wl_events(w.conn, job["id"]) if e["event"] == "outbound_sent"]
    assert approve_api(w.client, waiting["id"]).status_code == 200
    wl_loop.run_once(pool)
    wl_loop.send_once(pool)
    after = outbound_row(w.conn, waiting["id"])
    assert after["status"] == "sent" and after["result"] == {"logged": True} and after["sent_at"] is not None
    events = [e for e in wl_events(w.conn, job["id"]) if e["event"] == "outbound_sent"]
    assert len(events) == 1
    wl_loop.run_once(pool)
    wl_loop.send_once(pool)
    assert len([e for e in wl_events(w.conn, job["id"]) if e["event"] == "outbound_sent"]) == 1


def test_a_log_row_without_a_job_is_still_sent(secret_world: SecretWorld, pool):
    w = secret_world
    row = insert_outbound(w.conn, "alpha", "log", payload={"m": "no job"}, status="approved")
    wl_loop.run_once(pool)
    wl_loop.send_once(pool)
    assert status(w.conn, row["id"]) == "sent"


def test_email_never_logs_in_without_tls():
    """Review M3: a server (or an on-path attacker) that offers no STARTTLS must not receive
    the SMTP credentials in clear text."""
    import smtplib

    from host.workloads.senders import EmailSender

    class NoTls:
        def __init__(self, *a, **k):
            self.logged_in = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")

        def login(self, user, password):
            self.logged_in = True
            raise AssertionError("credentials sent without TLS")

        def send_message(self, msg):
            raise AssertionError("must not send")

    sender = EmailSender(smtp=NoTls)
    action = {"kind": "email", "payload": {"to": "a@example.com", "subject": "s", "body": "b"}}
    secrets = {"SMTP_URL": "smtp://user:pass@mail.example.com:587", "EMAIL_FROM": "me@example.com"}
    with pytest.raises(ValueError, match="STARTTLS"):
        sender.send(action, secrets)
