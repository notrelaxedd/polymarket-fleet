"""Stock models (contract section 5): rows from stock_search results, validation metrics
from stock_validate results, the owner's retire.

A stock_search result {"create_stock_models": [{family, params, params_hash, summary,
backtest_metrics}]} becomes stock_models rows, UNIQUE (family, params_hash): an existing
row is kept and its id returned, so a repeated /complete or a second search that finds
the same candidate never duplicates it. The host computes params_hash itself
(fleet.models.base.params_hash) and ignores the worker's. A stock_validate result
{"model_id", "validation_metrics"} fills the model's validation_metrics. Each changes
the status through host.stocks.eligibility.recompute. A stock_backtest result is
informational and stored on the job only. Malformed entries are skipped and named in a
job event: a bad entry never fails the job's /complete.
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from fleet.models.base import params_hash
from host.errors import BadRequest, Conflict, NotFound
from host.events import add_audit, add_job_event

FAMILIES = ("momentum", "meanrev", "trend", "buyhold")
RESULT_KINDS = ("stock_search", "stock_validate")
MAX_SUMMARY = 1000
MAX_ENTRIES = 50


def model_id_of(value: Any) -> int | None:
    """A stock model id (bigint) from an int or a digit string; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit() and len(value) < 19:
        return int(value) or None
    return None


def get_model(conn: psycopg.Connection, model_id: Any, for_update: bool = False) -> dict[str, Any]:
    """One stock_models row; 404 when missing."""
    mid = model_id_of(model_id)
    if mid is None:
        raise NotFound("stock model not found")
    row = conn.execute("SELECT * FROM stock_models WHERE id = %s" + (" FOR UPDATE" if for_update else ""), (mid,)).fetchone()
    if row is None:
        raise NotFound("stock model not found")
    return dict(row)


def check_model_spec(family: Any, params: Any) -> tuple[str, dict[str, Any]]:
    """(family, params) of a model spec; 400 for an unknown family or non-object params."""
    if family not in FAMILIES:
        raise BadRequest(f"unknown stock model family: {family!r}")
    if not isinstance(params, dict):
        raise BadRequest("params must be an object")
    for key, value in params.items():
        if not isinstance(key, str) or isinstance(value, (dict, list)):
            raise BadRequest(f"params.{key} must be a number or a string")
    return str(family), dict(params)


def create_model(conn: psycopg.Connection, entry: dict[str, Any], job_id: Any = None) -> tuple[int, bool]:
    """Insert one model from a search entry (or keep the existing row); (id, created)."""
    family, params = check_model_spec(entry.get("family"), entry.get("params"))
    metrics = entry.get("backtest_metrics")
    if metrics is not None and not isinstance(metrics, dict):
        raise BadRequest("backtest_metrics must be an object")
    summary = entry.get("summary")
    summary = str(summary)[:MAX_SUMMARY] if summary is not None else None
    phash = params_hash(params)
    row = conn.execute(
        """
        INSERT INTO stock_models (family, params, params_hash, summary, backtest_metrics, created_by_job_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (family, params_hash) DO NOTHING RETURNING id
        """,
        (family, Jsonb(params), phash, summary, Jsonb(metrics) if metrics is not None else None, job_id),
    ).fetchone()
    if row is None:
        existing = conn.execute(
            "SELECT id FROM stock_models WHERE family = %s AND params_hash = %s", (family, phash)
        ).fetchone()
        return int(existing["id"]), False
    conn.execute("UPDATE stock_models SET lineage_id = id WHERE id = %s", (row["id"],))
    return int(row["id"]), True


def set_validation(conn: psycopg.Connection, model_id: Any, metrics: Any) -> int:
    """Store a model's validation_metrics; its id."""
    if not isinstance(metrics, dict):
        raise BadRequest("validation_metrics must be an object")
    model = get_model(conn, model_id, for_update=True)
    conn.execute(
        "UPDATE stock_models SET validation_metrics = %s, updated_at = now() WHERE id = %s", (Jsonb(metrics), model["id"])
    )
    return int(model["id"])


def process_result(conn: psycopg.Connection, job: dict[str, Any], result: Any) -> dict[str, Any] | None:
    """The hook host.leases.complete calls for the stock result kinds. Returns what was
    added to the job's stored result ({"created_stock_models": [ids]} or
    {"validated_stock_model": id}), None when nothing applied."""
    from host.stocks import eligibility

    if job.get("kind") not in RESULT_KINDS or not isinstance(result, dict):
        return None
    skipped: list[str] = []
    if job["kind"] == "stock_search":
        entries = result.get("create_stock_models")
        ids: list[int] = []
        for i, entry in enumerate(entries[:MAX_ENTRIES] if isinstance(entries, list) else []):
            try:
                with conn.transaction():
                    mid, _ = create_model(conn, entry if isinstance(entry, dict) else {}, job.get("id"))
                ids.append(mid)
            except BadRequest as exc:
                skipped.append(f"entry {i}: {exc.message}")
        for mid in ids:
            eligibility.recompute(conn, mid)
        added: dict[str, Any] = {"created_stock_models": ids}
    else:
        params = job.get("params") if isinstance(job.get("params"), dict) else {}
        target = model_id_of(result.get("model_id")) or model_id_of(params.get("model_id"))
        try:
            with conn.transaction():
                mid = set_validation(conn, target, result.get("validation_metrics"))
            eligibility.recompute(conn, mid)
            added = {"validated_stock_model": mid}
        except (BadRequest, NotFound) as exc:
            skipped.append(exc.message)
            added = {"validated_stock_model": None}
    if skipped:
        add_job_event(conn, job["id"], "stock_result_skipped", None, {"problems": skipped[:20]})
    conn.execute("UPDATE jobs SET result = COALESCE(result, '{}'::jsonb) || %s WHERE id = %s", (Jsonb(added), job["id"]))
    return added


def retire_model(conn: psycopg.Connection, model_id: Any, actor: str) -> dict[str, Any]:
    """Retire a model (final): its active assignments are halted, their orders cancelled."""
    from host.stocks import assignments

    model = get_model(conn, model_id)
    if model["status"] == "retired":
        return model
    rows = conn.execute(
        "SELECT id FROM stock_assignments WHERE model_id = %s AND status = 'active' ORDER BY id", (model["id"],)
    ).fetchall()
    for row in rows:
        assignments.halt_assignment(conn, row["id"], "model retired", actor)
    model = get_model(conn, model["id"], for_update=True)
    if model["status"] == "retired":
        return model
    after = conn.execute(
        "UPDATE stock_models SET status = 'retired', updated_at = now() WHERE id = %s RETURNING *", (model["id"],)
    ).fetchone()
    add_audit(conn, "stock_model_retired", f"stock_model:{model['id']}", actor, {"status": model["status"]},
              {"status": "retired", "assignments_halted": [r["id"] for r in rows]})
    return dict(after)


def list_models(conn: psycopg.Connection, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Stock models, best backtest sharpe first."""
    rows = conn.execute(
        """
        SELECT * FROM stock_models WHERE (%(status)s::text IS NULL OR status = %(status)s)
         ORDER BY (backtest_metrics ->> 'sharpe')::double precision DESC NULLS LAST, id LIMIT %(limit)s
        """,
        {"status": status, "limit": max(1, min(int(limit), 1000))},
    ).fetchall()
    return [dict(r) for r in rows]


def require_tradable_status(model: dict[str, Any], mode: str) -> None:
    """400 unless the model may trade in `mode` (paper: paper_ok or live_eligible; live:
    live_eligible)."""
    if model["status"] == "retired":
        raise Conflict(f"stock model {model['id']} is retired")
    if mode == "live" and model["status"] != "live_eligible":
        raise BadRequest(f"stock model {model['id']} is {model['status']}; live needs live_eligible")
    if model["status"] not in ("paper_ok", "live_eligible"):
        raise BadRequest(f"stock model {model['id']} is {model['status']}; trading needs paper_ok or live_eligible")
