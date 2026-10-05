"""Model rows: creation (idempotent, lineage inheritance), metrics, summary, retire.

A root model is its own lineage (`lineage_id = id`); a child created by training
inherits the parent's lineage, status, backtest, validation, stress and snapshot metrics
(step 6: the validation write is host/model_validation.py, the snapshot replay routing
host/snapshot_store.py). Every write that can
move a lineage's status reads the thresholds first (host.eligibility.thresholds,
FOR SHARE) and ends in recompute_lineage. The job a worker names must be bound to
the write: a root comes from a model_search job, a child from the train job whose
params.model_id is its parent, and backtest metrics from the backtest job whose
params.model_id is that model (409 otherwise), so a lease on some other job cannot
create or promote models.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from fleet.models.base import params_hash
from fleet.models.registry import FAMILIES
from host.eligibility import recompute_lineage, thresholds
from host.errors import BadRequest, Conflict, NotFound
from host.events import add_job_event
from host.leases import as_uuid
from host.snapshot_store import SNAPSHOTS, backtest_route, store_snapshot_result

STATUSES = ("candidate", "paper_ok", "live_eligible", "retired")
MAX_SUMMARY = 600
MAX_WORKER_SUMMARY = 2000
WORKER_FIELDS = (
    "id", "lineage_id", "family", "params", "artifact", "parent_model_id", "trained_through", "status",
    "backtest_metrics", "validation_metrics", "stress_metrics",
)
METRIC_FIELDS = ("backtest_metrics", "validation_metrics", "stress_metrics")
INGAME_FAMILY = "ingame_wp"  # its root artifact is the traded model (see _existing_root)


def known_family(family: Any) -> bool:
    return isinstance(family, str) and family in FAMILIES


def get_model(conn: psycopg.Connection, model_id: Any, for_update: bool = False) -> dict[str, Any]:
    """One model row; 404 when missing or the id is not a uuid."""
    mid = as_uuid(model_id)
    if mid is None:
        raise NotFound("model not found")
    sql = "SELECT * FROM models WHERE id = %s" + (" FOR UPDATE" if for_update else "")
    row = conn.execute(sql, (mid,)).fetchone()
    if row is None:
        raise NotFound("model not found")
    return row


def model_payload(row: dict[str, Any]) -> dict[str, Any]:
    """GET /api/v1/models/{id}: the fields a worker needs."""
    return {key: row.get(key) for key in WORKER_FIELDS}


def lineage_rows(conn: psycopg.Connection, lineage_id: Any) -> list[dict[str, Any]]:
    """Every row of a lineage, root first then by creation time."""
    return conn.execute(
        "SELECT * FROM models WHERE lineage_id = %s ORDER BY (id = lineage_id) DESC, created_at, id", (lineage_id,)
    ).fetchall()


def job_leased_by(conn: psycopg.Connection, job_id: Any, worker_id: str) -> dict[str, Any]:
    """The job when the calling worker holds its lease, else 409."""
    jid = as_uuid(job_id)
    row = None
    if jid is not None:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = %s AND lease_worker_id = %s AND status IN ('leased', 'cancel_requested')",
            (jid, worker_id),
        ).fetchone()
    if row is None:
        raise Conflict("job_id is not a job leased by this worker")
    return row


def _job_model_id(job: dict[str, Any]) -> str:
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    return str(params.get("model_id") or "")


def require_job(job: dict[str, Any], kind: str, model_id: Any = None) -> None:
    """409 unless the leased job is of `kind` (and, when given, its params.model_id is
    `model_id`): the job must be the one whose work this write reports."""
    if job["kind"] != kind:
        raise Conflict(f"job_id is a {job['kind']} job, not a {kind} job")
    if model_id is not None and _job_model_id(job) != str(model_id):
        raise Conflict(f"job_id is a {kind} job for another model")


def _finite(value: Any, name: str) -> Any:
    """400 when a JSON value holds NaN or Infinity (Postgres jsonb refuses them)."""
    try:
        json.dumps(value, allow_nan=False)
    except ValueError:
        raise BadRequest(f"{name} must not contain NaN or Infinity") from None
    return value


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _trained_through(value: Any) -> list[int] | None:
    if value is None:
        return None
    if isinstance(value, dict) and set(value) >= {"season", "week"}:
        value = [value["season"], value["week"]]
    if (
        isinstance(value, (list, tuple)) and len(value) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    ):
        return [int(value[0]), int(value[1])]
    raise BadRequest("trained_through must be null or [season, week]")


def _object_or_none(value: Any, name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BadRequest(f"{name} must be an object or null")
    return value


def _summary(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRequest("summary must be a string or null")
    text = value.strip()
    if len(text) > limit:
        raise BadRequest(f"summary must be at most {limit} characters")
    return text or None


def validate_new_model(body: dict[str, Any]) -> dict[str, Any]:
    """The checked fields of a POST /api/v1/models body (400 on a bad one)."""
    family = body.get("family")
    if not known_family(family):
        raise BadRequest(f"unknown model family: {family!r}")
    params = body.get("params")
    if not isinstance(params, dict):
        raise BadRequest("params must be an object")
    for key, item in params.items():
        if not _number(item):
            raise BadRequest(f"params.{key} must be a number")
    _finite(params, "params")
    return {
        "family": family,
        "params": params,
        "params_hash": params_hash(params),
        "artifact": _finite(_object_or_none(body.get("artifact"), "artifact"), "artifact"),
        "backtest_metrics": _finite(_object_or_none(body.get("backtest_metrics"), "backtest_metrics"), "backtest_metrics"),
        "validation_metrics": _finite(_object_or_none(body.get("validation_metrics"), "validation_metrics"), "validation_metrics"),
        "stress_metrics": _finite(_object_or_none(body.get("stress_metrics"), "stress_metrics"), "stress_metrics"),
        "summary": _summary(body.get("summary"), MAX_WORKER_SUMMARY),
        "parent_model_id": body.get("parent_model_id"),
        "trained_through": _trained_through(body.get("trained_through")),
    }


def find_existing(conn: psycopg.Connection, fields: dict[str, Any]) -> dict[str, Any] | None:
    """The row with the same (family, params_hash, trained_through), serialised on a lock."""
    key = f"model:{fields['family']}:{fields['params_hash']}:{fields['trained_through']}"
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
    return conn.execute(
        """
        SELECT * FROM models
         WHERE family = %s AND params_hash = %s AND trained_through IS NOT DISTINCT FROM %s::jsonb
        """,
        (fields["family"], fields["params_hash"], Jsonb(fields["trained_through"]) if fields["trained_through"] is not None else None),
    ).fetchone()


def _store_metrics(conn: psycopg.Connection, lineage_id: Any, metrics: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE models SET backtest_metrics = %s, updated_at = now() WHERE lineage_id = %s",
        (Jsonb(metrics), lineage_id),
    )


def store_validation(
    conn: psycopg.Connection, lineage_id: Any, validation: dict[str, Any] | None, stress: dict[str, Any] | None
) -> None:
    """Validation and stress metrics on every row of a lineage (a null leaves that
    column as it is)."""
    conn.execute(
        """
        UPDATE models SET validation_metrics = COALESCE(%s, validation_metrics), stress_metrics = COALESCE(%s, stress_metrics),
               updated_at = now() WHERE lineage_id = %s
        """,
        (Jsonb(validation) if validation is not None else None, Jsonb(stress) if stress is not None else None, lineage_id),
    )


def _replace_fit(conn: psycopg.Connection, lineage_id: Any, fields: dict[str, Any]) -> None:
    """A refit ingame_wp root: the artifact, its metrics (a null clears) and the summary
    (a null keeps the old one) in one UPDATE."""
    conn.execute(
        """
        UPDATE models SET artifact = %s, backtest_metrics = %s, validation_metrics = %s, stress_metrics = %s,
               summary = COALESCE(%s, summary), updated_at = now() WHERE lineage_id = %s
        """,
        (Jsonb(fields["artifact"]), *(Jsonb(fields[k]) if fields[k] is not None else None for k in METRIC_FIELDS),
         fields["summary"], lineage_id),
    )


def _existing_root(
    conn: psycopg.Connection, existing: dict[str, Any], fields: dict[str, Any], limits: dict[str, Any]
) -> dict[str, Any]:
    """An identity hit on a root that carries metrics: the latest evaluation wins
    (like POST /models/{id}/backtest and /validation), so the first, possibly empty,
    search no longer fixes the lineage's metrics for good. An ingame_wp root has no
    train job, so its artifact is the traded model: a posted artifact replaces it
    together with all three metrics and the summary (_replace_fit), never the metrics
    alone, so the gate judges the coefficients those metrics measured."""
    if existing["id"] != existing["lineage_id"]:
        return existing
    if existing["family"] == INGAME_FAMILY and fields["artifact"] is not None and fields["artifact"] != existing["artifact"]:
        _replace_fit(conn, existing["lineage_id"], fields)
        changed: dict[str, Any] = {"artifact": fields["artifact"]}
    else:
        changed = {key: fields[key] for key in METRIC_FIELDS if fields[key] is not None and fields[key] != existing[key]}
    if not changed:
        return existing
    if "backtest_metrics" in changed:
        _store_metrics(conn, existing["lineage_id"], changed["backtest_metrics"])
    if "validation_metrics" in changed or "stress_metrics" in changed:
        store_validation(conn, existing["lineage_id"], changed.get("validation_metrics"), changed.get("stress_metrics"))
    status = recompute_lineage(conn, existing["lineage_id"], limits)
    row = get_model(conn, existing["id"])
    row["status"] = status or row["status"]
    return row


def create_model(
    conn: psycopg.Connection, body: dict[str, Any], worker_id: str
) -> tuple[dict[str, Any], bool]:
    """POST /api/v1/models: (row, created). Idempotent on the identity index."""
    job = job_leased_by(conn, body.get("job_id"), worker_id)
    fields = validate_new_model(body)
    parent_id = fields["parent_model_id"]
    if parent_id is None:
        require_job(job, "model_search")
    else:
        require_job(job, "train", parent_id)
    limits = thresholds(conn)  # before any model row lock (see host.eligibility)
    existing = find_existing(conn, fields)
    if existing is not None:
        row = _existing_root(conn, existing, fields, limits)
        add_job_event(conn, job["id"], "model_exists", worker_id, {"model_id": str(row["id"]), "status": row["status"]})
        return row, False
    model_id = uuid.uuid4()
    if parent_id is not None:
        # Locked: a backtest committing on the lineage meanwhile cannot leave the child
        # with the old metrics while every other row gets the new ones.
        parent = conn.execute("SELECT * FROM models WHERE id = %s FOR UPDATE", (as_uuid(parent_id),)).fetchone()
        if parent is None:
            raise BadRequest("unknown parent model")
        if parent["family"] != fields["family"] or parent["params_hash"] != fields["params_hash"]:
            raise BadRequest("a child must keep its parent's family and params")
        # A child is the same model trained further: it shares the parent's lineage,
        # status and metrics (its own metrics fields are ignored).
        lineage_id, status = parent["lineage_id"], parent["status"]
        metrics = {key: parent[key] for key in METRIC_FIELDS}
        parent_uuid: uuid.UUID | None = parent["id"]
        snapshot = parent.get("snapshot_metrics")  # the lineage's snapshot replay numbers too
    else:
        lineage_id, status, parent_uuid, snapshot = model_id, "candidate", None, None
        metrics = {key: fields[key] for key in METRIC_FIELDS}
    row = conn.execute(
        """
        INSERT INTO models (id, lineage_id, family, params, params_hash, artifact, parent_model_id,
                            trained_through, summary, status, backtest_metrics, validation_metrics, stress_metrics,
                            snapshot_metrics, created_by_job_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (
            model_id, lineage_id, fields["family"], Jsonb(fields["params"]), fields["params_hash"],
            Jsonb(fields["artifact"]) if fields["artifact"] is not None else None, parent_uuid,
            Jsonb(fields["trained_through"]) if fields["trained_through"] is not None else None,
            fields["summary"], status, *(Jsonb(metrics[key]) if metrics[key] is not None else None for key in METRIC_FIELDS),
            Jsonb(snapshot) if snapshot is not None else None, job["id"],
        ),
    ).fetchone()
    new_status = recompute_lineage(conn, lineage_id, limits)
    add_job_event(conn, job["id"], "model_created", worker_id, {"model_id": str(model_id), "status": new_status})
    row["status"] = new_status or row["status"]
    return row, True


def set_backtest_metrics(
    conn: psycopg.Connection, model_id: Any, metrics: Any, job_id: Any, worker_id: str
) -> dict[str, Any]:
    """POST /api/v1/models/{id}/backtest: metrics on the whole lineage, then eligibility;
    a snapshot replay result goes to snapshot_metrics instead (host.snapshot_store)."""
    job = job_leased_by(conn, job_id, worker_id)
    if not isinstance(metrics, dict):
        raise BadRequest("backtest_metrics must be an object")
    _finite(metrics, "backtest_metrics")
    model = get_model(conn, model_id)
    require_job(job, "backtest", model["id"])
    if backtest_route(job, metrics) == SNAPSHOTS:
        store_snapshot_result(conn, get_model(conn, model_id, for_update=True), metrics, job, worker_id)
        return get_model(conn, model_id)
    limits = thresholds(conn)
    model = get_model(conn, model_id, for_update=True)
    _store_metrics(conn, model["lineage_id"], metrics)
    status = recompute_lineage(conn, model["lineage_id"], limits)
    add_job_event(conn, job["id"], "model_backtest", worker_id, {"model_id": str(model["id"]), "status": status})
    return get_model(conn, model_id)


# The owner's edits (summary, retire) live in host.model_owner and are re-exported here.
from host.model_owner import retire, set_summary  # noqa: E402,F401
