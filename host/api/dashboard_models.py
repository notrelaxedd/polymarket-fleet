"""The Models pages: leaderboard, model detail, summary edit and retire forms."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from host import leaderboard, models, nflverse, web
from host.api.dashboard import page
from host.api.dashboard_forms import FORM
from host.api.deps import DB, require_owner
from host.data_refresh import refresh_now
from host.errors import BadRequest, Upstream
from host.settings import get_setting

router = APIRouter(tags=["dashboard-models"], dependencies=[Depends(require_owner)])

NFLVERSE_ATTRIBUTION = (
    "Game data from nflverse (games.csv), licensed CC BY 4.0."
)


def _calibration(metrics: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = (metrics or {}).get("calibration") if isinstance(metrics, dict) else None
    out = []
    for index, bucket in enumerate(rows or []):
        if not isinstance(bucket, dict):
            continue
        out.append({"bucket": f"{index / 10:.1f}-{(index + 1) / 10:.1f}", **bucket})
    return out


@router.get("/models", response_class=HTMLResponse)
def models_page(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """The leaderboard: ranked lineages, then the unranked ones."""
    board = leaderboard.leaderboard(conn)
    return page(request, conn, "models.html", attribution=NFLVERSE_ATTRIBUTION, **board)


@router.get("/models/{model_id}", response_class=HTMLResponse)
def model_page(request: Request, model_id: str, conn: psycopg.Connection = DB) -> HTMLResponse:
    """One model: params, metrics (overall and per season), calibration, lineage, jobs."""
    model = leaderboard.model_detail(conn, model_id)
    metrics = model.get("backtest_metrics") if isinstance(model.get("backtest_metrics"), dict) else {}
    return page(
        request, conn, "model.html", model=model, metrics=metrics,
        per_season=[s for s in (metrics.get("per_season") or []) if isinstance(s, dict)],
        calibration=_calibration(metrics), attribution=NFLVERSE_ATTRIBUTION,
    )


@router.post("/models/{model_id}/summary")
def post_summary(
    model_id: str, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """The inline summary edit form (models page and detail page)."""
    back = web.safe_next(form.get("next"), "/models")
    try:
        row = models.set_summary(conn, model_id, form.get("summary") or "", actor)
    except BadRequest as exc:
        conn.rollback()
        return web.redirect(back, f"summary not saved: {exc.message}")
    return web.redirect(back, f"summary of {str(row['id'])[:8]} saved")


@router.post("/models/{model_id}/retire")
def post_retire(
    model_id: str, form: dict[str, str] = FORM, actor: str = Depends(require_owner), conn: psycopg.Connection = DB
) -> Response:
    """Retire the whole lineage."""
    row = models.retire(conn, model_id, "retired", actor)
    return web.redirect(web.safe_next(form.get("next"), f"/models/{row['id']}"), f"lineage of {str(row['id'])[:8]} retired")


@router.post("/data/refresh")
def post_data_refresh(conn: psycopg.Connection = DB) -> Response:
    """The "Refresh now" button on the settings page; a failure is flashed, not a 500."""
    url = str(get_setting(conn, "nflverse_url", nflverse.DEFAULT_URL))
    try:
        result = refresh_now(conn, url)
    except (Upstream, BadRequest) as exc:
        conn.rollback()
        return web.redirect("/settings#nflverse", f"refresh failed: {exc.message}")
    skipped = f", {result['skipped']} skipped" if result["skipped"] else ""
    return web.redirect("/settings#nflverse", f"games refreshed: {result['rows']} rows, {result['updated']} updated{skipped}")
