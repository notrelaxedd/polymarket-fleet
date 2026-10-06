"""Secret isolation guardrail (docs/workloads-design.md sections 4.2, 5 and 8).

Two workloads, alpha and bravo, hold container secrets and host-only (sender) secrets. The
tests prove: secrets leave the host only through `/api/v1/machines/{id}/start`, only for the
machine's current workload and epoch, never host-only ones; run, machine and worker tokens are
each useless on the others' routes; nothing readable ever reaches an owner route, a page, an
audit row or a table; values are encrypted at rest.

Assumptions where the contract is silent:
- Validation errors (bad body, bad name) are 400, as everywhere in this app.
- A secret stored with scope host_only is never delivered to a container, even if a later
  manifest edit lists its name under `container` (the stored scope is the truth).
- A malformed FLEET_SECRETS_KEY counts as "no key": writes are 409, never a 500.
- Claiming with kinds the workload does not declare returns no job (200) or 400; either way
  the other workload's job stays queued.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import uuid
from typing import Any

import pytest

from host import auth
from host.errors import Conflict, Unauthorized
from host.workloads.secrets import (
    delete_secret, host_only_secrets, list_secret_names, secrets_for_machine, secrets_version, set_secret,
)
from host.workloads.tokens import run_scope
from tests.conftest import heartbeat_body
from tests.wl_helpers import (
    ALL_SECRET_VALUES, ALPHA_FROM_VALUE, ALPHA_KEY_VALUE, ALPHA_SMTP_VALUE, ALPHA_VALUES, BRAVO_KEY_VALUE,
    BRAVO_SMTP_VALUE, BRAVO_VALUES, INTRUDER, OWNER, SecretWorld, assignment_of, audit_for, bearer, call,
    database_text, forms_of, get_run, insert_machine, insert_wl_job, insert_workload, lease_wl_job,
    machine_heartbeat_body, make_manifest, mint_run, no_secrets_key, put_secret, replace_manifest,
    secret_world, secrets_key, start_run, strict_client, wl_job,
)

FORBIDDEN_FOR_ALPHA = (BRAVO_KEY_VALUE, BRAVO_SMTP_VALUE, ALPHA_SMTP_VALUE, ALPHA_FROM_VALUE)
FORBIDDEN_FOR_BRAVO = (ALPHA_KEY_VALUE, ALPHA_SMTP_VALUE, ALPHA_FROM_VALUE, BRAVO_SMTP_VALUE)


def uid() -> str:
    return str(uuid.uuid4())


# ------------------------------------------------------------------ /start: who gets which secrets


def test_start_returns_only_the_container_secrets_of_the_assigned_workload(secret_world: SecretWorld):
    w = secret_world
    r = start_run(w.client, w.alpha, w.alpha_epoch)
    assert r.status_code == 200, r.text
    assert r.json()["secrets"] == {"ALPHA_KEY": ALPHA_KEY_VALUE}
    assert r.json()["run_token"]
    assert "no-store" in r.headers["cache-control"].lower()
    for forbidden in FORBIDDEN_FOR_ALPHA:
        assert forbidden not in r.text


def test_a_machine_assigned_bravo_never_gets_alphas_secrets(secret_world: SecretWorld):
    w = secret_world
    r = start_run(w.client, w.bravo, w.bravo_epoch)
    assert r.status_code == 200, r.text
    assert r.json()["secrets"] == {"BRAVO_KEY": BRAVO_KEY_VALUE}
    for forbidden in FORBIDDEN_FOR_BRAVO:
        assert forbidden not in r.text


def test_asking_for_the_other_machines_epoch_does_not_give_its_secrets(secret_world: SecretWorld):
    """bravo's machine is at epoch 2: asking for alpha's epoch 5 on bravo's id is a stale epoch."""
    w = secret_world
    r = start_run(w.client, w.bravo, w.alpha_epoch)
    assert r.status_code == 409
    for value in ALL_SECRET_VALUES:
        assert value not in r.text


def test_a_workload_without_container_secrets_starts_with_an_empty_dict(secret_world: SecretWorld):
    w = secret_world
    r = start_run(w.client, w.quiet, w.quiet_epoch)
    assert r.status_code == 200 and r.json()["secrets"] == {}


@pytest.mark.parametrize("delta", [-1, 1, -5, 100])
def test_a_stale_or_wrong_epoch_is_409_and_mints_nothing(secret_world: SecretWorld, delta: int):
    w = secret_world
    r = start_run(w.client, w.alpha, w.alpha_epoch + delta)
    assert r.status_code == 409, r.text
    for value in ALL_SECRET_VALUES:
        assert value not in r.text
    assert assignment_of(w.conn, w.alpha.id)["run_token_hash"] is None


def test_start_with_nothing_assigned_is_409(secret_world: SecretWorld):
    w = secret_world
    empty = insert_machine(w.conn, "emptybox", workload=None, epoch=3)
    r = start_run(w.client, empty, 3)
    assert r.status_code == 409, r.text
    assert assignment_of(w.conn, empty.id)["run_token_hash"] is None


def test_start_rejects_a_missing_epoch(secret_world: SecretWorld):
    w = secret_world
    r = w.client.post(f"/api/v1/machines/{w.alpha.id}/start", json={}, headers=w.alpha.headers)
    assert r.status_code in (400, 409)
    assert assignment_of(w.conn, w.alpha.id)["run_token_hash"] is None


def test_start_mints_a_new_token_each_time_and_the_old_one_dies(secret_world: SecretWorld):
    w = secret_world
    first = get_run(w.client, w.alpha, w.alpha_epoch)
    ok = w.client.post("/api/v1/wl/claim", json={"kinds": ["alpha"]}, headers=first.headers)
    assert ok.status_code == 200, ok.text
    second = get_run(w.client, w.alpha, w.alpha_epoch)
    assert second.token != first.token
    assert w.client.post("/api/v1/wl/claim", json={"kinds": ["alpha"]}, headers=first.headers).status_code == 401
    assert w.client.post("/api/v1/wl/claim", json={"kinds": ["alpha"]}, headers=second.headers).status_code == 200


def test_only_the_hash_of_the_run_token_is_stored(secret_world: SecretWorld):
    w = secret_world
    creds = get_run(w.client, w.alpha, w.alpha_epoch)
    stored = assignment_of(w.conn, w.alpha.id)["run_token_hash"]
    assert stored == auth.hash_token(creds.token) == hashlib.sha256(creds.token.encode()).hexdigest()
    assert creds.token not in database_text(w.conn)


# ------------------------------------------------------------------ reassignment fences everything


def test_after_reassignment_the_old_run_token_is_401_on_every_run_route(secret_world: SecretWorld):
    w = secret_world
    old = get_run(w.client, w.alpha, w.alpha_epoch)
    job = insert_wl_job(w.conn, "alpha", "alpha")
    lease = lease_wl_job(w.conn, job["id"], w.alpha, w.alpha_epoch)
    posted = w.client.post(
        "/api/v1/wl/outbound",
        json={"kind": "log", "payload": {"m": "hi"}, "dedupe_key": "k-old"}, headers=old.headers,
    )
    assert posted.status_code < 300, posted.text
    action_id = posted.json()["id"]
    assert w.client.post(f"/api/machines/{w.alpha.id}/assign", json={"workload": "bravo"}).status_code == 200
    token = str(lease["lease_token"])
    jid = str(job["id"])
    calls = [
        ("POST", "/api/v1/wl/claim", {"kinds": ["alpha"]}),
        ("POST", f"/api/v1/wl/jobs/{jid}/heartbeat", {"lease_token": token, "progress": 0.5}),
        ("POST", f"/api/v1/wl/jobs/{jid}/release", {"lease_token": token, "checkpoint": {}, "progress": 0.5, "reason": "shutdown"}),
        ("POST", f"/api/v1/wl/jobs/{jid}/complete", {"lease_token": token, "result": {}}),
        ("POST", f"/api/v1/wl/jobs/{jid}/fail", {"lease_token": token, "error": "x"}),
        ("POST", "/api/v1/wl/outbound", {"kind": "log", "payload": {}, "dedupe_key": "k-new"}),
        ("GET", f"/api/v1/wl/outbound/{action_id}", None),
    ]
    for method, path, body in calls:
        r = w.client.request(method, path, json=body, headers=old.headers)
        assert r.status_code == 401, (path, r.status_code, r.text)
    assert assignment_of(w.conn, w.alpha.id)["run_token_hash"] is None


def test_after_reassignment_start_serves_the_new_workloads_secrets_at_the_new_epoch_only(secret_world: SecretWorld):
    w = secret_world
    assert w.client.post(f"/api/machines/{w.alpha.id}/assign", json={"workload": "bravo"}).status_code == 200
    new_epoch = assignment_of(w.conn, w.alpha.id)["epoch"]
    assert new_epoch == w.alpha_epoch + 1
    stale = start_run(w.client, w.alpha, w.alpha_epoch)
    assert stale.status_code == 409
    for value in ALL_SECRET_VALUES:
        assert value not in stale.text
    fresh = start_run(w.client, w.alpha, new_epoch)
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["secrets"] == {"BRAVO_KEY": BRAVO_KEY_VALUE}
    for forbidden in FORBIDDEN_FOR_BRAVO:
        assert forbidden not in fresh.text


def test_the_epoch_is_the_fence_not_the_workload_name(secret_world: SecretWorld):
    """alpha -> bravo -> alpha: the first alpha epoch is not valid again just because alpha is back."""
    w = secret_world
    first = get_run(w.client, w.alpha, w.alpha_epoch)
    assert w.client.post(f"/api/machines/{w.alpha.id}/assign", json={"workload": "bravo"}).status_code == 200
    assert w.client.post(f"/api/machines/{w.alpha.id}/assign", json={"workload": "alpha"}).status_code == 200
    now = assignment_of(w.conn, w.alpha.id)
    assert now["workload"] == "alpha" and now["epoch"] == w.alpha_epoch + 2
    assert start_run(w.client, w.alpha, w.alpha_epoch).status_code == 409
    assert w.client.post("/api/v1/wl/claim", json={"kinds": ["alpha"]}, headers=first.headers).status_code == 401
    again = start_run(w.client, w.alpha, now["epoch"])
    assert again.status_code == 200 and again.json()["secrets"] == {"ALPHA_KEY": ALPHA_KEY_VALUE}
    assert w.client.post("/api/v1/wl/claim", json={"kinds": ["alpha"]}, headers=first.headers).status_code == 401


def test_stopping_a_machine_kills_its_run_token(secret_world: SecretWorld):
    w = secret_world
    creds = get_run(w.client, w.alpha, w.alpha_epoch)
    assert w.client.post(f"/api/machines/{w.alpha.id}/assign", json={"workload": None}).status_code == 200
    assert w.client.post("/api/v1/wl/claim", json={"kinds": ["alpha"]}, headers=creds.headers).status_code == 401
    assert start_run(w.client, w.alpha, w.alpha_epoch + 1).status_code == 409, "nothing assigned"


def test_a_disabled_machine_loses_its_run_token(secret_world: SecretWorld):
    w = secret_world
    creds = get_run(w.client, w.alpha, w.alpha_epoch)
    r = w.client.post(f"/api/machines/{w.alpha.id}/enabled", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert w.client.post("/api/v1/wl/claim", json={"kinds": ["alpha"]}, headers=creds.headers).status_code == 401


def test_run_scope_returns_the_scope_and_refuses_everything_else(secret_world: SecretWorld, pool):
    w = secret_world
    token = mint_run(pool, w.alpha, w.alpha_epoch)
    scope = call(pool, run_scope, token)
    assert (scope["machine_id"], scope["workload"], scope["epoch"]) == (w.alpha.id, "alpha", w.alpha_epoch)
    newer = mint_run(pool, w.alpha, w.alpha_epoch)
    with pytest.raises(Unauthorized):
        call(pool, run_scope, token)
    assert call(pool, run_scope, newer)["machine_id"] == w.alpha.id
    for bad in ("", "nope", w.alpha.token):
        with pytest.raises(Unauthorized):
            call(pool, run_scope, bad)
    # Moving the assignment (epoch + 1) kills the token. The token carries no epoch of its own, so this
    # depends on every epoch-changing path clearing run_token_hash; `assign` is the path under test.
    from host.workloads.assign import assign

    call(pool, assign, w.alpha.id, "bravo", "owner", None)
    with pytest.raises(Unauthorized):
        call(pool, run_scope, newer)


# ------------------------------------------------------------------ one workload cannot reach the other


def test_a_run_token_cannot_claim_another_workloads_jobs(secret_world: SecretWorld):
    w = secret_world
    foreign = insert_wl_job(w.conn, "bravo", "bravo", params={"secret_input": "bravo-only"})
    alpha = get_run(w.client, w.alpha, w.alpha_epoch)
    for kinds in (["bravo"], ["alpha", "bravo"], None):
        r = w.client.post("/api/v1/wl/claim", json={"kinds": kinds}, headers=alpha.headers)
        assert r.status_code in (200, 400), r.text
        if r.status_code == 200:
            assert r.json()["job"] is None
        assert "bravo-only" not in r.text
    assert wl_job(w.conn, foreign["id"])["status"] == "queued"


def test_a_run_token_cannot_touch_another_workloads_leased_job(secret_world: SecretWorld):
    w = secret_world
    job = insert_wl_job(w.conn, "bravo", "bravo")
    lease = lease_wl_job(w.conn, job["id"], w.bravo, w.bravo_epoch)
    alpha = get_run(w.client, w.alpha, w.alpha_epoch)
    token, jid = str(lease["lease_token"]), str(job["id"])
    for action, body in (
        ("heartbeat", {"lease_token": token, "progress": 0.9, "checkpoint": {"stolen": True}}),
        ("release", {"lease_token": token, "checkpoint": {}, "progress": 0.9, "reason": "shutdown"}),
        ("complete", {"lease_token": token, "result": {"stolen": True}}),
        ("fail", {"lease_token": token, "error": "stolen"}),
    ):
        r = w.client.post(f"/api/v1/wl/jobs/{jid}/{action}", json=body, headers=alpha.headers)
        assert r.status_code in (404, 409), (action, r.status_code, r.text)
    row = wl_job(w.conn, job["id"])
    assert row["status"] == "leased" and row["lease_machine_id"] == w.bravo.id and row["result"] is None
    assert row["progress"] == 0 and row["checkpoint"] is None


def test_a_run_token_cannot_read_another_workloads_outbound_actions(secret_world: SecretWorld):
    w = secret_world
    alpha = get_run(w.client, w.alpha, w.alpha_epoch)
    bravo = get_run(w.client, w.bravo, w.bravo_epoch)
    theirs = w.client.post("/api/v1/wl/outbound", json={"kind": "email", "payload": {"to": "a@b.c", "subject": "s", "body": "private"},
                                                         "dedupe_key": "same-key"}, headers=bravo.headers)
    assert theirs.status_code < 300, theirs.text
    their_id = theirs.json()["id"]
    r = w.client.get(f"/api/v1/wl/outbound/{their_id}", headers=alpha.headers)
    assert r.status_code == 404 and "private" not in r.text
    mine = w.client.post("/api/v1/wl/outbound", json={"kind": "email", "payload": {"to": "x@y.z", "subject": "t", "body": "mine"},
                                                       "dedupe_key": "same-key"}, headers=alpha.headers)
    assert mine.status_code < 300, mine.text
    assert mine.json()["id"] != their_id, "dedupe keys are per workload"
    assert w.client.get(f"/api/v1/wl/outbound/{mine.json()['id']}", headers=alpha.headers).status_code == 200
    assert w.client.get(f"/api/v1/wl/outbound/{their_id}", headers=bravo.headers).status_code == 200


def test_a_run_token_cannot_call_start_or_the_machine_routes(secret_world: SecretWorld):
    w = secret_world
    alpha = get_run(w.client, w.alpha, w.alpha_epoch)
    for machine in (w.alpha, w.bravo):
        r = w.client.post(f"/api/v1/machines/{machine.id}/start", json={"epoch": 2}, headers=alpha.headers)
        assert r.status_code == 401
        r = w.client.post(f"/api/v1/machines/{machine.id}/heartbeat", json=machine_heartbeat_body(2), headers=alpha.headers)
        assert r.status_code == 401


def test_a_machine_token_is_bound_to_its_own_machine_id(secret_world: SecretWorld):
    w = secret_world
    r = w.client.post(f"/api/v1/machines/{w.bravo.id}/start", json={"epoch": w.bravo_epoch}, headers=w.alpha.headers)
    assert r.status_code == 401
    for value in ALL_SECRET_VALUES:
        assert value not in r.text
    assert assignment_of(w.conn, w.bravo.id)["run_token_hash"] is None


# ------------------------------------------------------------------ token types on each other's routes


def _worker_routes(c: dict[str, str]) -> list[tuple[str, str, Any]]:
    j, t = c["jid"], c["tok"]
    return [
        ("GET", "/api/v1/trade/state", None),
        ("POST", "/api/v1/orders/request", {"client_request_id": "cr-1", "job_id": j, "lease_token": t, "assignment_id": j,
                                            "market_id": j, "price": 0.5, "size": 1}),
        ("POST", f"/api/v1/orders/{j}/cancel", None),
        ("POST", "/api/v1/trade/release", {"jobs": []}),
        ("GET", "/api/v1/data/games", None),
        ("GET", "/api/v1/data/prices", None),
        ("POST", f"/api/v1/workers/{c['wid']}/heartbeat", heartbeat_body()),
        ("POST", f"/api/v1/jobs/{j}/checkpoint", {"lease_token": t, "progress": 0.1}),
        ("POST", f"/api/v1/jobs/{j}/complete", {"lease_token": t, "result": {}}),
        ("POST", f"/api/v1/jobs/{j}/fail", {"lease_token": t, "error": "x"}),
        ("GET", f"/api/v1/models/{j}", None),
    ]


def _machine_routes(c: dict[str, str]) -> list[tuple[str, str, Any]]:
    return [
        ("POST", f"/api/v1/machines/{c['mid']}/heartbeat", machine_heartbeat_body(5)),
        ("POST", f"/api/v1/machines/{c['mid']}/start", {"epoch": 5}),
    ]


def _run_routes(c: dict[str, str]) -> list[tuple[str, str, Any]]:
    j, t = c["jid"], c["tok"]
    return [
        ("POST", "/api/v1/wl/claim", {"kinds": ["alpha"]}),
        ("POST", f"/api/v1/wl/jobs/{j}/heartbeat", {"lease_token": t, "progress": 0.1}),
        ("POST", f"/api/v1/wl/jobs/{j}/release", {"lease_token": t, "checkpoint": {}, "progress": 0.1, "reason": "shutdown"}),
        ("POST", f"/api/v1/wl/jobs/{j}/complete", {"lease_token": t, "result": {}}),
        ("POST", f"/api/v1/wl/jobs/{j}/fail", {"lease_token": t, "error": "x"}),
        ("POST", "/api/v1/wl/outbound", {"kind": "log", "payload": {}, "dedupe_key": "matrix"}),
        ("GET", f"/api/v1/wl/outbound/{j}", None),
    ]


FAMILIES = {"worker": _worker_routes, "machine": _machine_routes, "run": _run_routes}
FOREIGN = {
    "worker": ("machine", "run", "other_machine", "garbage", "none"),
    "machine": ("worker", "run", "other_machine", "garbage", "none"),
    "run": ("worker", "machine", "other_machine", "garbage", "none"),
}
MATRIX = [(fam, tok) for fam, toks in FOREIGN.items() for tok in toks]


@pytest.fixture
def matrix(secret_world: SecretWorld, make_worker):
    w = secret_world
    worker = make_worker("matrix-worker")
    run = get_run(w.client, w.alpha, w.alpha_epoch)
    ctx = {"jid": uid(), "tok": uid(), "wid": worker.id, "mid": w.alpha.id}
    tokens = {"worker": worker.token, "machine": w.alpha.token, "run": run.token, "other_machine": w.bravo.token,
              "garbage": "not-a-real-token", "none": None}
    return w.client, ctx, tokens


def _headers(token: str | None) -> dict[str, str]:
    return bearer(token) if token else {}


@pytest.mark.parametrize("family, foreign", MATRIX, ids=[f"{f}-routes-with-{t}-token" for f, t in MATRIX])
def test_each_token_type_is_refused_on_the_other_families_routes(matrix, family, foreign):
    client, ctx, tokens = matrix
    for method, path, body in FAMILIES[family](ctx):
        r = client.request(method, path, json=body, headers=_headers(tokens[foreign]))
        assert r.status_code == 401, (family, foreign, method, path, r.status_code, r.text)


@pytest.mark.parametrize("family, own", [("worker", "worker"), ("machine", "machine"), ("run", "run")])
def test_sanity_the_right_token_is_not_refused_as_unauthorized(matrix, family, own):
    """Proves the route lists above are real: with its own token a route is not a 401."""
    client, ctx, tokens = matrix
    for method, path, body in FAMILIES[family](ctx):
        r = client.request(method, path, json=body, headers=_headers(tokens[own]))
        assert r.status_code != 401, (family, method, path, r.status_code, r.text)


def test_owner_routes_do_not_accept_any_bearer_token(secret_world: SecretWorld, config, make_worker, pool):
    w = secret_world
    worker = make_worker("owner-probe")
    run = get_run(w.client, w.alpha, w.alpha_epoch)
    with strict_client(config) as c:
        for token in (worker.token, w.alpha.token, run.token):
            h = bearer(token)
            for method, path, body in (
                ("GET", "/api/workloads", None), ("GET", "/api/machines", None), ("GET", "/api/outbound", None),
                ("GET", "/api/workloads/alpha/secrets", None),
                ("PUT", "/api/workloads/alpha/secrets/ALPHA_KEY", {"value": "attacker-value"}),
                ("DELETE", "/api/workloads/alpha/secrets/ALPHA_KEY", None),
                ("POST", f"/api/machines/{w.alpha.id}/assign", {"workload": "bravo"}),
            ):
                r = c.request(method, path, json=body, headers=h)
                assert r.status_code == 401, (method, path, r.status_code, r.text)
    assert assignment_of(w.conn, w.alpha.id)["workload"] == "alpha"
    assert call(pool, secrets_for_machine, w.alpha.id, w.alpha_epoch) == {"ALPHA_KEY": ALPHA_KEY_VALUE}


def test_a_machine_cannot_write_secrets_even_with_the_owner_login(secret_world: SecretWorld, config, pool):
    w = secret_world
    ip_machine = insert_machine(w.conn, "iphost", remote_ip="100.64.0.7")
    with strict_client(config) as c:
        h = {**OWNER, "X-Forwarded-For": "9.9.9.9, 100.64.0.7"}
        r = c.put("/api/workloads/alpha/secrets/ALPHA_KEY", json={"value": "attacker-value"}, headers=h)
        assert r.status_code == 403, r.text
        r = c.get("/api/workloads/alpha/secrets", headers=h)
        assert r.status_code == 403
        r = c.put("/api/workloads/alpha/secrets/ALPHA_KEY", json={"value": "attacker-value"}, headers=INTRUDER)
        assert r.status_code == 401
    assert ip_machine.id
    assert call(pool, secrets_for_machine, w.alpha.id, w.alpha_epoch) == {"ALPHA_KEY": ALPHA_KEY_VALUE}


# ------------------------------------------------------------------ the functions behind /start


def test_secrets_for_machine_is_epoch_fenced_and_container_scoped(secret_world: SecretWorld, pool):
    w = secret_world
    assert call(pool, secrets_for_machine, w.alpha.id, w.alpha_epoch) == {"ALPHA_KEY": ALPHA_KEY_VALUE}
    assert call(pool, secrets_for_machine, w.bravo.id, w.bravo_epoch) == {"BRAVO_KEY": BRAVO_KEY_VALUE}
    for epoch in (w.alpha_epoch - 1, w.alpha_epoch + 1):
        with pytest.raises(Conflict):
            call(pool, secrets_for_machine, w.alpha.id, epoch)
    empty = insert_machine(w.conn, "nothing", workload=None, epoch=4)
    with pytest.raises(Conflict):
        call(pool, secrets_for_machine, empty.id, 4)


def test_host_only_secrets_are_per_workload(secret_world: SecretWorld, pool):
    assert call(pool, host_only_secrets, "alpha") == {"SMTP_URL": ALPHA_SMTP_VALUE, "EMAIL_FROM": ALPHA_FROM_VALUE}
    assert call(pool, host_only_secrets, "bravo") == {"SMTP_URL": BRAVO_SMTP_VALUE}
    assert call(pool, host_only_secrets, "quiet") == {}


def test_unset_declared_secrets_are_simply_absent(conn, pool, secrets_key):
    insert_workload(conn, make_manifest("partial", container=("P_ONE", "P_TWO")))
    m = insert_machine(conn, "partialbox", workload="partial", epoch=2, state="running")
    call(pool, set_secret, "partial", "P_ONE", "one-value-xyz", "owner", None)
    assert call(pool, secrets_for_machine, m.id, 2) == {"P_ONE": "one-value-xyz"}


def test_a_secret_stored_host_only_is_not_delivered_after_a_manifest_edit_moves_its_name(secret_world: SecretWorld):
    """Editing workload.toml so SMTP_URL is listed as a container secret must not leak the stored
    host-only value into the container."""
    w = secret_world
    replace_manifest(w.conn, make_manifest("alpha", kinds=["alpha"], container=("ALPHA_KEY", "SMTP_URL"),
                                           host_only=("EMAIL_FROM",), actions=("email", "log")))
    r = start_run(w.client, w.alpha, w.alpha_epoch)
    assert r.status_code == 200, r.text
    assert ALPHA_SMTP_VALUE not in r.text
    assert r.json()["secrets"].get("SMTP_URL") is None


def test_secrets_version_is_stable_and_changes_with_a_container_secret(secret_world: SecretWorld, pool):
    w = secret_world
    v1 = call(pool, secrets_version, "alpha")
    assert isinstance(v1, str) and v1 and call(pool, secrets_version, "alpha") == v1
    assert call(pool, secrets_version, "bravo") != v1
    assert put_secret(w.client, "alpha", "ALPHA_KEY", "alpha-rotated-value-0042").status_code < 300
    v2 = call(pool, secrets_version, "alpha")
    assert v2 != v1
    for value in (ALPHA_KEY_VALUE, "alpha-rotated-value-0042"):
        assert value not in v2 and value not in v1
    r = start_run(w.client, w.alpha, w.alpha_epoch)
    assert r.json()["secrets"] == {"ALPHA_KEY": "alpha-rotated-value-0042"}


def test_the_machine_heartbeat_carries_no_secrets_and_no_run_token(secret_world: SecretWorld, pool):
    w = secret_world
    r = w.client.post(f"/api/v1/machines/{w.alpha.id}/heartbeat", json=machine_heartbeat_body(w.alpha_epoch), headers=w.alpha.headers)
    assert r.status_code == 200, r.text
    for value in ALL_SECRET_VALUES:
        assert value not in r.text
    assert "run_token" not in r.text
    assert r.json()["secrets_version"] == call(pool, secrets_version, "alpha")


# ------------------------------------------------------------------ nothing readable leaves the host


OWNER_GETS = [
    "/api/workloads", "/api/workloads/alpha", "/api/workloads/bravo", "/api/workloads/alpha/secrets",
    "/api/workloads/bravo/secrets", "/api/machines", "/api/outbound", "/api/outbound?status=pending",
    "/api/workload-jobs", "/api/settings",
]
PAGES = [
    "/", "/fleet", "/jobs", "/models", "/trading", "/settings", "/machines", "/workloads", "/workloads/alpha",
    "/workloads/bravo", "/outbound", "/fragments/home", "/fragments/fleet", "/fragments/topbar", "/fragments/trading",
]


def _no_value(text: str) -> None:
    for value in ALL_SECRET_VALUES:
        for form in {value, html.escape(value), html.escape(value, quote=False)}:
            assert form not in text, f"{value!r} leaked"


def test_owner_get_routes_never_contain_a_secret_value(secret_world: SecretWorld):
    w = secret_world
    get_run(w.client, w.alpha, w.alpha_epoch)
    w.client.post("/api/v1/wl/outbound", json={"kind": "log", "payload": {"m": "x"}, "dedupe_key": "leakcheck"},
                  headers=get_run(w.client, w.alpha, w.alpha_epoch).headers)
    for path in OWNER_GETS + [f"/api/machines/{w.alpha.id}/logs?limit=200"]:
        r = w.client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        _no_value(r.text)


def test_the_secret_list_names_every_secret_and_says_whether_it_is_set(secret_world: SecretWorld, pool):
    w = secret_world
    names = call(pool, list_secret_names, "alpha")
    by_name = {s["name"]: s for s in names}
    assert set(by_name) >= {"ALPHA_KEY", "SMTP_URL", "EMAIL_FROM"}
    assert by_name["ALPHA_KEY"]["scope"] == "container" and by_name["SMTP_URL"]["scope"] == "host_only"
    assert all(s["declared"] is True and s["set"] is True and s["updated_at"] is not None for s in names)
    for s in names:
        assert not set(s) & {"value", "ciphertext", "nonce"}
        assert ALPHA_KEY_VALUE not in json.dumps(s, default=str)
    r = w.client.get("/api/workloads/alpha/secrets")
    assert r.status_code == 200
    for name in ("ALPHA_KEY", "SMTP_URL", "EMAIL_FROM"):
        assert name in r.text
    assert "ciphertext" not in r.text and "nonce" not in r.text


def test_the_put_response_does_not_echo_the_value(secret_world: SecretWorld):
    w = secret_world
    r = put_secret(w.client, "alpha", "ALPHA_KEY", "echo-check-value-5521")
    assert r.status_code < 300
    assert "echo-check-value-5521" not in r.text


@pytest.mark.parametrize("path", PAGES)
def test_dashboard_pages_never_contain_a_secret_value(secret_world: SecretWorld, path: str):
    w = secret_world
    r = w.client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    _no_value(r.text)


def test_machine_logs_page_never_contains_a_secret_value(secret_world: SecretWorld):
    w = secret_world
    r = w.client.get(f"/machines/{w.alpha.id}/logs")
    assert r.status_code == 200
    _no_value(r.text)


def test_audit_rows_never_contain_a_secret_value(secret_world: SecretWorld):
    w = secret_world
    put_secret(w.client, "alpha", "ALPHA_KEY", "second-alpha-value-9d9d")
    w.client.delete("/api/workloads/bravo/secrets/BRAVO_KEY")
    rows = w.conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    assert len(audit_for(w.conn, action="secret_set")) == 6
    assert len(audit_for(w.conn, action="secret_delete")) == 1
    dumped = json.dumps(rows, default=str)
    for value in ALL_SECRET_VALUES + ("second-alpha-value-9d9d",):
        assert value not in dumped


def test_no_secret_value_appears_anywhere_in_the_database(secret_world: SecretWorld):
    w = secret_world
    get_run(w.client, w.alpha, w.alpha_epoch)
    get_run(w.client, w.bravo, w.bravo_epoch)
    text = database_text(w.conn)
    for value in ALL_SECRET_VALUES:
        for form in forms_of(value):
            assert form not in text, f"{value!r} stored as {form!r}"


def test_values_are_encrypted_with_a_fresh_nonce_each_time(secret_world: SecretWorld):
    w = secret_world
    rows = w.conn.execute("SELECT * FROM workload_secrets ORDER BY workload, name").fetchall()
    assert len(rows) == 5
    for row in rows:
        value = (ALPHA_VALUES if row["workload"] == "alpha" else BRAVO_VALUES)[row["name"]]
        blob = bytes(row["ciphertext"])
        assert blob and bytes(row["nonce"])
        for form in forms_of(value):
            assert form.encode() not in blob and form.encode() not in bytes(row["nonce"])
        assert len(blob) >= len(value.encode()), "the ciphertext is at least as long as the value"
        assert len(bytes(row["nonce"])) == 24, "NaCl SecretBox nonce"
    assert len({bytes(r["nonce"]) for r in rows}) == len(rows), "no nonce is reused"
    # The same plaintext under two names, and the same name written twice: different bytes each time.
    put_secret(w.client, "alpha", "ALPHA_KEY", "identical-plaintext-value")
    first = w.conn.execute("SELECT nonce, ciphertext FROM workload_secrets WHERE workload='alpha' AND name='ALPHA_KEY'").fetchone()
    put_secret(w.client, "alpha", "ALPHA_KEY", "identical-plaintext-value")
    second = w.conn.execute("SELECT nonce, ciphertext FROM workload_secrets WHERE workload='alpha' AND name='ALPHA_KEY'").fetchone()
    assert bytes(first["nonce"]) != bytes(second["nonce"]) and bytes(first["ciphertext"]) != bytes(second["ciphertext"])
    put_secret(w.client, "alpha", "SMTP_URL", "identical-plaintext-value")
    other = w.conn.execute("SELECT nonce, ciphertext FROM workload_secrets WHERE workload='alpha' AND name='SMTP_URL'").fetchone()
    assert bytes(other["ciphertext"]) != bytes(second["ciphertext"])


def test_start_never_logs_a_secret_or_a_run_token(secret_world: SecretWorld, caplog):
    w = secret_world
    with caplog.at_level(logging.DEBUG):
        r = start_run(w.client, w.alpha, w.alpha_epoch)
    assert r.status_code == 200
    assert ALPHA_KEY_VALUE not in caplog.text
    assert r.json()["run_token"] not in caplog.text


def test_overwriting_a_secret_leaves_no_trace_of_the_old_value(secret_world: SecretWorld):
    w = secret_world
    put_secret(w.client, "alpha", "ALPHA_KEY", "rotated-value-3b3b")
    text = database_text(w.conn)
    for form in forms_of(ALPHA_KEY_VALUE) + forms_of("rotated-value-3b3b"):
        assert form not in text
    assert start_run(w.client, w.alpha, w.alpha_epoch).json()["secrets"] == {"ALPHA_KEY": "rotated-value-3b3b"}


# ------------------------------------------------------------------ writes: key, names, sizes


def test_without_the_key_writes_are_409_and_store_nothing(client, conn, no_secrets_key):
    insert_workload(conn, make_manifest("alpha", kinds=["alpha"], container=("ALPHA_KEY",)))
    r = put_secret(client, "alpha", "ALPHA_KEY", "never-stored-value")
    assert r.status_code == 409, r.text
    assert "never-stored-value" not in r.text
    assert conn.execute("SELECT count(*) AS n FROM workload_secrets").fetchone()["n"] == 0
    assert audit_for(conn, action="secret_set") == []


def test_the_function_raises_secrets_unavailable_without_the_key(pool, conn, no_secrets_key):
    from host.workloads.errors import SecretsUnavailable

    insert_workload(conn, make_manifest("alpha", kinds=["alpha"], container=("ALPHA_KEY",)))
    with pytest.raises(SecretsUnavailable):
        call(pool, set_secret, "alpha", "ALPHA_KEY", "v", "owner", None)


def test_without_the_key_start_is_409_when_the_workload_declares_container_secrets(client, conn, no_secrets_key):
    insert_workload(conn, make_manifest("alpha", kinds=["alpha"], container=("ALPHA_KEY",)))
    insert_workload(conn, make_manifest("quiet", kinds=["quiet"]))
    a = insert_machine(conn, "alphabox", workload="alpha", epoch=2, state="running")
    q = insert_machine(conn, "quietbox", workload="quiet", epoch=2, state="running")
    r = start_run(client, a, 2)
    assert r.status_code == 409, r.text
    assert assignment_of(conn, a.id)["run_token_hash"] is None
    ok = start_run(client, q, 2)
    assert ok.status_code == 200 and ok.json()["secrets"] == {}


@pytest.mark.parametrize("key", ["", "not base64 at all!!", "c2hvcnQ=", "QUJD" * 11])
def test_a_malformed_key_is_treated_as_no_key(client, conn, monkeypatch, key):
    monkeypatch.setenv("FLEET_SECRETS_KEY", key)
    insert_workload(conn, make_manifest("alpha", kinds=["alpha"], container=("ALPHA_KEY",)))
    r = put_secret(client, "alpha", "ALPHA_KEY", "never-stored-value")
    assert r.status_code == 409, r.text
    assert conn.execute("SELECT count(*) AS n FROM workload_secrets").fetchone()["n"] == 0


@pytest.mark.parametrize("name", ["NOT_DECLARED", "BRAVO_KEY", "ALPHA_KEY_2", "alpha_key", "1BAD", "A" * 65])
def test_setting_an_undeclared_or_invalid_name_is_400(secret_world: SecretWorld, name: str):
    w = secret_world
    before = w.conn.execute("SELECT count(*) AS n FROM workload_secrets").fetchone()["n"]
    r = put_secret(w.client, "alpha", name, "sneaky-value-2222")
    assert r.status_code == 400, r.text
    assert w.conn.execute("SELECT count(*) AS n FROM workload_secrets").fetchone()["n"] == before
    assert "sneaky-value-2222" not in database_text(w.conn)


def test_the_function_refuses_an_undeclared_name(secret_world: SecretWorld, pool):
    from host.errors import BadRequest

    with pytest.raises(BadRequest):
        call(pool, set_secret, "alpha", "BRAVO_KEY", "v", "owner", None)


def test_a_secret_for_an_unknown_workload_is_404(secret_world: SecretWorld):
    assert put_secret(secret_world.client, "no-such-workload", "ALPHA_KEY", "v").status_code == 404


def test_the_scope_comes_from_the_manifest_not_from_the_request(secret_world: SecretWorld):
    w = secret_world
    r = w.client.put("/api/workloads/alpha/secrets/SMTP_URL", json={"value": "smtps://x:y@z:465", "scope": "container"})
    assert r.status_code < 300 or r.status_code == 400, r.text  # an unknown body field is ignored or refused
    row = w.conn.execute("SELECT scope FROM workload_secrets WHERE workload='alpha' AND name='SMTP_URL'").fetchone()
    assert row["scope"] == "host_only"
    assert "smtps://x:y@z:465" not in start_run(w.client, w.alpha, w.alpha_epoch).text


def test_the_value_limit_is_16_kib(secret_world: SecretWorld):
    w = secret_world
    assert put_secret(w.client, "alpha", "ALPHA_KEY", "x" * 16384).status_code < 300
    assert put_secret(w.client, "alpha", "ALPHA_KEY", "y" * 16385).status_code == 400
    assert start_run(w.client, w.alpha, w.alpha_epoch).json()["secrets"] == {"ALPHA_KEY": "x" * 16384}


@pytest.mark.parametrize("body", [{}, {"value": None}, {"value": 123}, {"value": ["a"]}, {"value": {"a": 1}}])
def test_a_non_string_or_missing_value_is_400(secret_world: SecretWorld, body):
    w = secret_world
    r = w.client.put("/api/workloads/alpha/secrets/ALPHA_KEY", json=body)
    assert r.status_code == 400, r.text
    assert start_run(w.client, w.alpha, w.alpha_epoch).json()["secrets"] == {"ALPHA_KEY": ALPHA_KEY_VALUE}


def test_unusual_values_round_trip_exactly(secret_world: SecretWorld):
    w = secret_world
    value = " leading and trailing space \n\tnewéline ☃ {\"json\": true} "
    assert put_secret(w.client, "alpha", "ALPHA_KEY", value).status_code < 300
    assert start_run(w.client, w.alpha, w.alpha_epoch).json()["secrets"] == {"ALPHA_KEY": value}


def test_delete_removes_the_secret_from_start_and_audits_without_the_value(secret_world: SecretWorld):
    w = secret_world
    r = w.client.delete("/api/workloads/alpha/secrets/ALPHA_KEY")
    assert r.status_code < 300, r.text
    assert start_run(w.client, w.alpha, w.alpha_epoch).json()["secrets"] == {}
    assert len(audit_for(w.conn, action="secret_delete")) == 1
    assert ALPHA_KEY_VALUE not in json.dumps(audit_for(w.conn, action="secret_delete"), default=str)
    assert w.conn.execute("SELECT count(*) AS n FROM workload_secrets WHERE workload='alpha' AND name='ALPHA_KEY'").fetchone()["n"] == 0


def test_delete_function_removes_only_the_named_secret(secret_world: SecretWorld, pool):
    call(pool, delete_secret, "bravo", "SMTP_URL", "owner", None)
    assert call(pool, host_only_secrets, "bravo") == {}
    assert call(pool, host_only_secrets, "alpha")["SMTP_URL"] == ALPHA_SMTP_VALUE
    assert call(pool, secrets_for_machine, secret_world.bravo.id, 2) == {"BRAVO_KEY": BRAVO_KEY_VALUE}
