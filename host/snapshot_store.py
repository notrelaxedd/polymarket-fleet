"""Snapshot replay results on the host (docs/ROBUSTNESS.md B1).

A backtest job carries `price_source` ("closing_line" or "snapshots"); a snapshot one
also carries the replay settings in force when it was created (`snapshot_settings`).
POST /api/v1/models/{id}/backtest (host.models.set_backtest_metrics) routes by the
job's price source: closing-line
metrics land in `backtest_metrics` on the whole lineage and re-run eligibility, as
before; snapshot metrics land in `models.snapshot_metrics` on the whole lineage, never
touch `backtest_metrics` and leave the status alone (eligibility does not read them in
this step). The metrics' own `price_source` must agree with the job's (400), so a
snapshot run can never overwrite the closing-line numbers the gate judges, nor the
other way round. A snapshot backtest whose last season is null (in the request or in
settings backtest_seasons) replays through the latest season in games, the season in
progress included, not only the last complete one.
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host.errors import BadRequest
from host.events import add_job_event
from host.settings import get_setting

PRICE_SOURCES = ("closing_line", "snapshots")
SNAPSHOTS = "snapshots"
DEFAULT_DECISION_MINUTES, MAX_DECISION_MINUTES = 60, 300
DEFAULT_PARTICIPATION = 0.5
DEFAULT_PLATFORM = "sim"  # the seeded market_source


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def snapshot_settings(conn: psycopg.Connection) -> dict[str, Any]:
    """The settings a snapshot backtest carries: decision_minutes_before_kickoff
    (0..300, default 60), allow_sim_prices (true only when the setting is exactly
    true), price_platform (settings market_source) and participation (0..1, default
    0.5). A malformed stored value falls back to its default."""
    minutes = get_setting(conn, "decision_minutes_before_kickoff", DEFAULT_DECISION_MINUTES)
    if not _is_int(minutes) or not 0 <= minutes <= MAX_DECISION_MINUTES:
        minutes = DEFAULT_DECISION_MINUTES
    participation = get_setting(conn, "participation", DEFAULT_PARTICIPATION)
    if not isinstance(participation, (int, float)) or isinstance(participation, bool) or not 0 <= participation <= 1:
        participation = DEFAULT_PARTICIPATION
    platform = get_setting(conn, "market_source", DEFAULT_PLATFORM)
    return {
        "decision_minutes_before_kickoff": int(minutes),
        "allow_sim_prices": get_setting(conn, "allow_sim_prices", False) is True,
        "price_platform": platform if isinstance(platform, str) and platform else DEFAULT_PLATFORM,
        "participation": float(participation),
    }


def latest_season(conn: psycopg.Connection) -> int | None:
    """The newest season in the games table (the season in progress included), None
    when it is empty. A snapshot backtest with no explicit last season replays through
    it: its played games are scored, its unplayed ones are skipped."""
    row = conn.execute("SELECT max(season) AS season FROM games").fetchone()
    return None if row is None or row["season"] is None else int(row["season"])


def job_price_source(job: dict[str, Any]) -> str:
    """The job's params.price_source ("closing_line" when absent)."""
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    return str(params.get("price_source") or PRICE_SOURCES[0])


def metrics_price_source(metrics: dict[str, Any]) -> str:
    """The metrics' price_source ("closing_line" when absent, as a closing-line run reports)."""
    return str(metrics.get("price_source") or PRICE_SOURCES[0])


def store_snapshot_metrics(conn: psycopg.Connection, lineage_id: Any, metrics: dict[str, Any]) -> None:
    """Snapshot metrics on every row of a lineage; backtest_metrics and status stay."""
    conn.execute(
        "UPDATE models SET snapshot_metrics = %s, updated_at = now() WHERE lineage_id = %s",
        (Jsonb(metrics), lineage_id),
    )


def backtest_route(job: dict[str, Any], metrics: dict[str, Any]) -> str:
    """The column a backtest result goes to, named by the job's price source; 400 when
    the metrics report another one."""
    source = job_price_source(job)
    if metrics_price_source(metrics) != source:
        raise BadRequest(
            f"backtest_metrics.price_source is {metrics_price_source(metrics)!r} but the job's price_source is {source!r}"
        )
    return source


def store_snapshot_result(
    conn: psycopg.Connection, model: dict[str, Any], metrics: dict[str, Any], job: dict[str, Any], worker_id: str
) -> None:
    """A snapshot result of `model` (row locked by the caller): stored on its lineage
    and logged as a job event; the status is reported unchanged."""
    store_snapshot_metrics(conn, model["lineage_id"], metrics)
    add_job_event(
        conn, job["id"], "model_snapshot_backtest", worker_id,
        {"model_id": str(model["id"]), "status": model["status"], "n_bets": metrics.get("n_bets")},
    )
