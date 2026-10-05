"""The Models pages: leaderboard, model detail, summary edit and retire forms."""
from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from host import leaderboard, models, nflverse, web
from host.api.dashboard import page
from host.api.dashboard_forms import FORM
from host.api.deps import DB, require_owner
from host.api.model_view import detail_stats, gate_verdict, lineage_assignments
from host.api.models_ingame_view import add_pregame_lines, ingame_row, ingame_stats, ingame_verdict
from host.api.models_view import board_view, status_state, status_word
from host.api.robustness import calibration_rows, robustness_context
from host.data_refresh import refresh_now
from host.errors import BadRequest, Upstream
from host.settings import get_setting

router = APIRouter(tags=["dashboard-models"], dependencies=[Depends(require_owner)])

NFLVERSE_ATTRIBUTION = (
    "Game data from nflverse (games.csv), licensed CC BY 4.0."
)


@router.get("/models", response_class=HTMLResponse)
def models_page(request: Request, conn: psycopg.Connection = DB) -> HTMLResponse:
    """The leaderboard: ranked lineages, then the unranked ones, then the in-game
    (ingame_wp) lineages in their own group; a pre-game row gets its in-game line when
    its lineage has in-game bets."""
    raw = leaderboard.leaderboard(conn)
    ingame = [ingame_row(m) for m in raw["unranked"] if m.get("is_ingame")]
    board = board_view({**raw, "unranked": [m for m in raw["unranked"] if not m.get("is_ingame")]})
    add_pregame_lines(board["ranked"] + board["unranked"])
    return page(request, conn, "models.html", attribution=NFLVERSE_ATTRIBUTION, ingame_models=ingame, **board)


@router.get("/models/{model_id}", response_class=HTMLResponse)
def model_page(request: Request, model_id: str, conn: psycopg.Connection = DB) -> HTMLResponse:
    """One model: params, the robustness section (validation era, stress tests), the
    snapshot replay (step 6 B1), search-era metrics (overall and per season),
    calibration, lineage, jobs; on top the three stats and the gate verdict in words.
    (step 6C) An ingame_wp model shows its held-out validation against vegas_wp instead
    (_model_ingame.html), with its own stats and status rule."""
    model = leaderboard.model_detail(conn, model_id)
    common = {"attribution": NFLVERSE_ATTRIBUTION, "assignments": lineage_assignments(conn, model["lineage_id"]),
              "status_state": status_state(model.get("status")), "status_word": status_word(model.get("status"))}
    if model.get("is_ingame"):  # judged on held-out plays against vegas_wp, no moneyline backtest
        return page(request, conn, "model.html", model=model, iv=model.get("ingame_validation"),
                    stats=ingame_stats(model), verdict=ingame_verdict(model), **common)
    metrics = model.get("backtest_metrics") if isinstance(model.get("backtest_metrics"), dict) else {}
    validation = model.get("validation_metrics") if isinstance(model.get("validation_metrics"), dict) else None
    snapshot = model.get("snapshot_metrics") if isinstance(model.get("snapshot_metrics"), dict) else None
    return page(
        request, conn, "model.html", model=model, metrics=metrics,
        per_season=[s for s in (metrics.get("per_season") or []) if isinstance(s, dict)],
        calibration=calibration_rows(metrics),
        robustness=robustness_context(validation, model.get("stress_metrics")),
        validation_per_season=[s for s in ((validation or {}).get("per_season") or []) if isinstance(s, dict)],
        snapshot_metrics=snapshot,
        snapshot_per_season=[s for s in ((snapshot or {}).get("per_season") or []) if isinstance(s, dict)],
        stats=detail_stats(model), verdict=gate_verdict(conn, model), **common,
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
