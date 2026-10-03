"""Model routes: worker creation and metrics (bearer), owner leaderboard and edits."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from host import auth, leaderboard, models
from host.api.deps import DB, bearer, require_owner
from host.api.limits import small_payload
from host.api.serialize import jsonable

worker_router = APIRouter(prefix="/api/v1/models", tags=["models"])
owner_router = APIRouter(prefix="/api/models", tags=["models"], dependencies=[Depends(require_owner)])


class NewModelBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    job_id: str = Field(max_length=64)
    family: str = Field(max_length=64)
    params: dict[str, Any]
    artifact: dict[str, Any] | None = None
    backtest_metrics: dict[str, Any] | None = None
    summary: str | None = Field(default=None, max_length=models.MAX_WORKER_SUMMARY)
    parent_model_id: str | None = Field(default=None, max_length=64)
    trained_through: Any = None

    @field_validator("artifact", "backtest_metrics")
    @classmethod
    def _small(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return small_payload(value, "payload")


class BacktestBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    job_id: str = Field(max_length=64)
    backtest_metrics: dict[str, Any]

    @field_validator("backtest_metrics")
    @classmethod
    def _small(cls, value: dict[str, Any]) -> dict[str, Any]:
        return small_payload(value, "backtest_metrics")


class SummaryBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str = Field(max_length=4000)


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str = Field(max_length=32)


@worker_router.get("/{model_id}")
def get_model(model_id: str, token: str = Depends(bearer), conn: psycopg.Connection = DB) -> dict[str, Any]:
    """The model a job needs (params, artifact, lineage)."""
    auth.worker_for_token(conn, token)
    return jsonable(models.model_payload(models.get_model(conn, model_id)))


@worker_router.post("", status_code=201)
def post_model(
    body: NewModelBody, response: Response, token: str = Depends(bearer), conn: psycopg.Connection = DB
) -> dict[str, Any]:
    """Create a model row (201) or return the existing identical one (200)."""
    worker = auth.worker_for_token(conn, token)
    row, created = models.create_model(conn, body.model_dump(), worker["id"])
    if not created:
        response.status_code = 200
    return jsonable({"id": row["id"], "lineage_id": row["lineage_id"], "created": created, "status": row["status"]})


@worker_router.post("/{model_id}/backtest")
def post_backtest(
    model_id: str, body: BacktestBody, token: str = Depends(bearer), conn: psycopg.Connection = DB
) -> dict[str, Any]:
    """Store backtest metrics on the model's lineage and re-run eligibility."""
    worker = auth.worker_for_token(conn, token)
    row = models.set_backtest_metrics(conn, model_id, body.backtest_metrics, body.job_id, worker["id"])
    return jsonable({"id": row["id"], "lineage_id": row["lineage_id"], "status": row["status"]})


@owner_router.get("")
def get_leaderboard(conn: psycopg.Connection = DB) -> dict[str, Any]:
    """Ranked and unranked lineages."""
    return jsonable(leaderboard.leaderboard(conn))


@owner_router.get("/{model_id}")
def get_model_detail(model_id: str, conn: psycopg.Connection = DB) -> dict[str, Any]:
    """One model with its lineage members and related jobs."""
    return jsonable(leaderboard.model_detail(conn, model_id))


@owner_router.post("/{model_id}/summary")
def post_summary(
    model_id: str, body: SummaryBody, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> dict[str, Any]:
    """Edit the summary text (at most 600 characters)."""
    return jsonable(leaderboard.model_detail(conn, models.set_summary(conn, model_id, body.summary, actor)["id"]))


@owner_router.post("/{model_id}/status")
def post_status(
    model_id: str, body: StatusBody, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> dict[str, Any]:
    """Retire a lineage (the only owner-settable status)."""
    return jsonable(leaderboard.model_detail(conn, models.retire(conn, model_id, body.status, actor)["id"]))
