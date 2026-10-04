"""POST /api/v1/models/{id}/validation: the validation-era metrics and the stress
table of a model, stored on its whole lineage (docs/ROBUSTNESS.md A1 to A3).

The job named must be leased by the caller and be the one whose work the write
reports: a `validate` job whose `params.model_id` is this model, or a `model_search`
job that created or found this model (a `model_created` or `model_exists` job event
of that job names it). Eligibility runs after the write like after a backtest.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.eligibility import recompute_lineage, thresholds
from host.errors import BadRequest, Conflict
from host.events import add_job_event
from host.models import _finite, get_model, job_leased_by, require_job, store_validation

VALIDATION_KINDS = ("validate", "model_search")


def _search_job_names_model(conn: psycopg.Connection, job: dict[str, Any], model_id: Any) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM job_events WHERE job_id = %s AND event IN ('model_created', 'model_exists')
           AND detail ->> 'model_id' = %s LIMIT 1
        """,
        (job["id"], str(model_id)),
    ).fetchone()
    return row is not None


def require_validation_job(conn: psycopg.Connection, job: dict[str, Any], model_id: Any) -> None:
    """409 unless the leased job is the validate job of this model or the search job
    that created (or found) it."""
    if job["kind"] == "validate":
        require_job(job, "validate", model_id)
        return
    if job["kind"] == "model_search":
        if not _search_job_names_model(conn, job, model_id):
            raise Conflict("job_id is a model_search job that did not create this model")
        return
    raise Conflict(f"job_id is a {job['kind']} job, not a validate or model_search job")


def _metrics(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BadRequest(f"{name} must be an object")
    return _finite(value, name)


def set_validation_metrics(
    conn: psycopg.Connection, model_id: Any, validation: Any, stress: Any, job_id: Any, worker_id: str
) -> dict[str, Any]:
    """Store validation and stress metrics on the whole lineage, then eligibility."""
    job = job_leased_by(conn, job_id, worker_id)
    validation = _metrics(validation, "validation_metrics")
    stress = _metrics(stress, "stress_metrics")
    model = get_model(conn, model_id)
    require_validation_job(conn, job, model["id"])
    limits = thresholds(conn)  # before any model row lock (see host.eligibility)
    model = get_model(conn, model_id, for_update=True)
    store_validation(conn, model["lineage_id"], validation, stress)
    status = recompute_lineage(conn, model["lineage_id"], limits)
    add_job_event(conn, job["id"], "model_validation", worker_id, {"model_id": str(model["id"]), "status": status})
    return get_model(conn, model_id)
