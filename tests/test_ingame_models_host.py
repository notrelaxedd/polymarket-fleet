"""Host side of the in-game model family (step 6C, contract sections 6, 11 and 12):
the ingame_wp model_search params, the refusal of pre-game jobs on ingame_wp models,
the ingame_wp eligibility rule (paper_ok over 10000 plays beating vegas_wp, never
live_eligible), the leaderboard's in-game group and the Models pages, the weekly
pbp_rows refresh and the ingame_lag_min_events seed."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from host import eligibility, jobparams, leaderboard
from host.errors import BadRequest
from host.ingame_eligibility import MIN_PLAYS, ingame_status, meets_ingame
from host.pbp_refresh import FIRST_DELAY, OFF_SEASON_RECHECK, RETRY_SECONDS, WEEK, PbpRowsRefresher, in_season
from tests.conftest import (
    insert_game, insert_model, insert_validated_model, lease_job, model_row, set_setting,
)
from tests.pagecheck import page

INGAME_PARAMS = {"l2": 1.0, "time_scale": 1.0, "fp_scale": 1.0}


def ingame_validation(n_plays: int = 25000, log_loss: float = 0.45, vegas_log_loss: float = 0.46, **extra: Any) -> dict[str, Any]:
    """A validation dict of the fleet/sim/ingame_eval.py shape."""
    group = {"n_plays": (n_plays or 0) // 5, "log_loss": log_loss, "vegas_log_loss": vegas_log_loss}
    out = {
        "n_plays": n_plays, "log_loss": log_loss, "vegas_log_loss": vegas_log_loss,
        "beats_baseline": log_loss <= vegas_log_loss, "brier": 0.15, "vegas_brier": 0.155, "seasons": [2022, 2023, 2024],
        "n_skipped_no_vegas": 12,
        "by_period": {k: dict(group) for k in ("1", "2", "3", "4", "5")},
        "by_score_bucket": {k: dict(group) for k in ("<=-9", "-8..-1", "0", "1..8", ">=9")},
        "calibration": [{"count": 100, "mean_p": (i + 0.5) / 10, "mean_outcome": (i + 0.5) / 10,
                         "vegas_mean_p": (i + 0.5) / 10} for i in range(10)],
        "era": "validation",
    }
    out.update(extra)
    return out


def insert_ingame(conn, status: str = "candidate", validation: dict[str, Any] | None = None, **params: float):
    return insert_model(conn, family="ingame_wp", params={**INGAME_PARAMS, **params}, status=status,
                        artifact={"coef": [0.0] * 10}, metrics={"era": "search", "log_loss": 0.44},
                        validation=validation)


def add_score(conn, model, game_id: str, n_bets: int = 2, pnl: int = 100, clv: float | None = 0.05,
              ingame_bets: int = 0, ingame_pnl: int = 0, mode: str = "paper") -> None:
    conn.execute(
        """
        INSERT INTO model_scores (model_id, game_id, mode, lineage_id, n_bets, stake_cents, pnl_cents, avg_clv,
                                  ingame_n_bets, ingame_pnl_cents)
        VALUES (%s, %s, %s, %s, %s, 1000, %s, %s, %s, %s)
        """,
        (model["id"], game_id, mode, model["lineage_id"], n_bets, pnl, clv, ingame_bets, ingame_pnl),
    )


# ------------------------------------------------------------------ job params


def test_ingame_search_params_defaults_and_ranges(conn):
    out = jobparams.prepare_params(conn, "model_search", {"family": "ingame_wp"})
    assert out == {"family": "ingame_wp", "train_seasons": [2012, 2021], "validation_seasons": [2022, None], "n": 20,
                   "seed": 1, "top_k": 5, "train_fraction": 0.3}, "contract defaults, no settings copied in"
    custom = jobparams.prepare_params(conn, "model_search", {
        "family": "ingame_wp", "train_seasons": [2014, 2019], "validation_seasons": [2020, 2023], "n": 7, "seed": 9,
        "top_k": 2, "train_fraction": 1})
    assert custom["train_seasons"] == [2014, 2019] and custom["validation_seasons"] == [2020, 2023]
    assert (custom["n"], custom["seed"], custom["top_k"], custom["train_fraction"]) == (7, 9, 2, 1.0)
    form = jobparams.prepare_params(conn, "model_search", {"family": "ingame_wp", "seasons": [2010, None], "n": 200})
    assert form["train_seasons"] == [2010, 2021] and "seasons" not in form, "the Jobs form's seasons, capped below validation"
    capped = jobparams.prepare_params(conn, "model_search", {"family": "ingame_wp", "train_seasons": [2012, None],
                                                             "validation_seasons": [2020, None]})
    assert capped["train_seasons"] == [2012, 2019]
    bad = [
        ({"grid": [INGAME_PARAMS]}, "unknown ingame_wp model_search params: grid"),
        ({"seasons": [2012, 2020], "train_seasons": [2012, 2020]}, "train_seasons or seasons, not both"),
        ({"train_seasons": [2012, 2022]}, "must end before validation_seasons"),
        ({"train_seasons": [2012, 2021], "validation_seasons": [2021, None]}, "must end before"),
        ({"train_seasons": [2022, None]}, "leave no season before validation_seasons"),
        ({"train_seasons": [2020, 2012]}, "must not be before the first"),
        ({"validation_seasons": [None, 2024]}, "validation_seasons first must be an integer"),
        ({"train_seasons": "2012-2021"}, "train_seasons must be [first, last]"),
        ({"n": 0}, "n must be between 1 and 500"),
        ({"n": 501}, "n must be between 1 and 500"),
        ({"n": True}, "n must be an integer"),
        ({"top_k": 21}, "top_k must be between 1 and 20"),
        ({"train_fraction": 0}, "train_fraction must be above 0"),
        ({"train_fraction": 1.01}, "train_fraction must be above 0"),
        ({"train_fraction": "half"}, "train_fraction must be a number"),
    ]
    for params, message in bad:
        with pytest.raises(BadRequest, match=message.replace("[", r"\[").replace("]", r"\]")):
            jobparams.prepare_params(conn, "model_search", {"family": "ingame_wp", **params})


def test_ingame_search_job_through_the_api(client, conn):
    r = client.post("/api/jobs", json={"kind": "model_search", "params": {"family": "ingame_wp", "n": 3}})
    assert r.status_code == 201, r.text
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (uuid.UUID(r.json()["id"]),)).fetchone()
    assert job["role"] == "model_search" and job["params"]["family"] == "ingame_wp" and job["params"]["n"] == 3
    assert job["params"]["validation_seasons"] == [2022, None] and "fee_model" not in job["params"]
    r = client.post("/api/jobs", json={"kind": "model_search", "params": {"family": "ingame_wp", "train_seasons": [2012, 2023]}})
    assert r.status_code == 400 and "must end before" in r.text


def test_pregame_jobs_refuse_ingame_models(conn):
    model = insert_ingame(conn)
    with pytest.raises(BadRequest, match="a backtest does not run ingame_wp"):
        jobparams.prepare_params(conn, "backtest", {"family": "ingame_wp", "params": INGAME_PARAMS})
    for kind, params in (("backtest", {"model_id": str(model["id"])}), ("validate", {"model_id": str(model["id"])}),
                         ("train", {"model_id": str(model["id"]), "through": {"season": 2024, "week": 3}})):
        with pytest.raises(BadRequest, match=f"a {kind} job does not run ingame_wp models"):
            jobparams.prepare_params(conn, kind, params)


# ------------------------------------------------------------------ eligibility


def test_ingame_rule_at_its_boundaries():
    assert meets_ingame(ingame_validation(n_plays=MIN_PLAYS)), "10000 plays exactly passes"
    assert not meets_ingame(ingame_validation(n_plays=MIN_PLAYS - 1))
    assert meets_ingame(ingame_validation(log_loss=0.46, vegas_log_loss=0.46)), "equal log-loss beats the baseline"
    assert not meets_ingame(ingame_validation(log_loss=0.4601, vegas_log_loss=0.46))
    assert not meets_ingame(ingame_validation(beats_baseline="yes")), "only a true beats_baseline counts"
    assert not meets_ingame(ingame_validation(n_plays=None)) and not meets_ingame(None) and not meets_ingame({})
    good = ingame_validation()
    assert ingame_status("candidate", good) == "paper_ok" and ingame_status("paper_ok", None) == "candidate"
    assert ingame_status("live_eligible", good) == "paper_ok", "never live_eligible in this step"
    assert ingame_status("retired", good) == "retired"


def test_worker_created_ingame_models_get_their_status(client, conn, make_worker):
    w = make_worker(role="model_search")
    job = lease_job(conn, w, "model_search", {"family": "ingame_wp"})
    cases = [
        (ingame_validation(), "paper_ok"),
        (ingame_validation(n_plays=MIN_PLAYS - 1), "candidate"),
        (ingame_validation(log_loss=0.47), "candidate"),
        (None, "candidate"),
    ]
    for i, (validation, status) in enumerate(cases):
        body = {"job_id": str(job["id"]), "family": "ingame_wp", "params": {**INGAME_PARAMS, "l2": 0.5 + i},
                "artifact": {"coef": [0.1] * 10}, "backtest_metrics": {"era": "search", "log_loss": 0.44},
                "validation_metrics": validation, "summary": None, "parent_model_id": None, "trained_through": None}
        r = client.post("/api/v1/models", json=body, headers=w.headers)
        assert r.status_code == 201, r.text
        assert r.json()["status"] == status, (i, r.json())


def test_ingame_lineage_is_never_live_eligible(conn):
    game = insert_game(conn, "2025_01_KC_LV", kickoff_in_s=-30 * 86400, season=2025)
    set_setting(conn, "thresholds_paper", {"min_games": 0, "min_bets": 0, "min_days": 0, "min_clv": 0.0,
                                           "min_pnl_cents": 0, "clv_ci_excludes_zero": False})
    pregame = insert_validated_model(conn, status="paper_ok")
    ingame = insert_ingame(conn, status="paper_ok", validation=ingame_validation())
    for model in (pregame, ingame):
        add_score(conn, model, game["game_id"], ingame_bets=2 if model is ingame else 0, ingame_pnl=100)
    assert eligibility.recompute_paper(conn, pregame["lineage_id"]) == "live_eligible", "control: the gate promotes"
    assert eligibility.recompute_paper(conn, ingame["lineage_id"]) == "paper_ok", "the paper gate skips ingame_wp"
    assert model_row(conn, ingame["id"])["status"] == "paper_ok"
    conn.execute("UPDATE models SET status = 'live_eligible' WHERE lineage_id = %s", (ingame["lineage_id"],))
    assert eligibility.recompute_lineage(conn, ingame["lineage_id"]) == "paper_ok", "brought back from live_eligible"
    conn.execute("UPDATE models SET validation_metrics = %s WHERE id = %s",
                 (Jsonb(ingame_validation(n_plays=50)), ingame["id"]))
    assert eligibility.recompute_lineage(conn, ingame["lineage_id"]) == "candidate"
    assert eligibility.recompute_all(conn) >= 2 and model_row(conn, ingame["id"])["status"] == "candidate"
    conn.execute("UPDATE models SET status = 'retired' WHERE id = %s", (ingame["id"],))
    assert eligibility.recompute_lineage(conn, ingame["lineage_id"]) == "retired"


# ------------------------------------------------------------------ leaderboard and pages


def test_leaderboard_lists_ingame_lineages_apart_with_their_validation(conn):
    game = insert_game(conn, "2025_02_KC_LV", kickoff_in_s=-20 * 86400, season=2025)
    pregame = insert_validated_model(conn)
    worse = insert_ingame(conn, validation=ingame_validation(log_loss=0.47), l2=2.0)
    best = insert_ingame(conn, validation=ingame_validation(log_loss=0.43), l2=3.0)
    good = insert_ingame(conn, validation=ingame_validation(log_loss=0.455), l2=4.0)
    bare = insert_ingame(conn, l2=5.0)
    add_score(conn, pregame, game["game_id"], n_bets=3, pnl=-50, ingame_bets=0)
    add_score(conn, best, game["game_id"], n_bets=4, pnl=250, clv=None, ingame_bets=4, ingame_pnl=250)
    board = leaderboard.leaderboard(conn)
    assert [e["id"] for e in board["ranked"]] == [pregame["id"]], "in-game lineages never rank with pre-game ones"
    ingame = [e for e in board["unranked"] if e["is_ingame"]]
    assert [e["id"] for e in ingame] == [best["id"], good["id"], worse["id"], bare["id"]], "beating vegas_wp first, by gain"
    assert all(e["unranked_reason"] == "in-game model" for e in ingame)
    top = ingame[0]
    v = top["ingame_validation"]
    assert v["n_plays"] == 25000 and v["beats_baseline"] is True and v["ll_gain"] == pytest.approx(0.03)
    assert [g["label"] for g in v["by_period"]] == ["Q1", "Q2", "Q3", "Q4", "OT"] and len(v["calibration"]) == 10
    assert [g["key"] for g in v["by_score_bucket"]] == ["<=-9", "-8..-1", "0", "1..8", ">=9"]
    assert top["ingame"] == {"games": 1, "bets": 4, "pnl_cents": 250} and top["validated"] is True
    assert top["short_params"] == "L2 3.00 · time 1.00 · field 1.00"
    assert ingame[-1]["validated"] is False and ingame[-1]["ingame_validation"] is None
    assert board["ranked"][0]["ingame"] is None and board["ranked"][0]["is_ingame"] is False
    assert board["ranked"][0]["paper"]["bets"] == 3, "the pooled paper record is unchanged"
    detail = leaderboard.model_detail(conn, best["id"])
    assert detail["is_ingame"] and detail["ingame"]["bets"] == 4 and detail["ingame_validation"]["n_plays"] == 25000
    assert "beats the vegas_wp baseline" in detail["ingame_reason"]


def test_models_pages_show_the_ingame_group(client, conn):
    game = insert_game(conn, "2025_03_KC_LV", kickoff_in_s=-10 * 86400, season=2025)
    pregame = insert_validated_model(conn)
    doc = page(client.get("/models").text)
    assert not doc.has("#ingame") and not doc.has(".row-ingame"), "no in-game group without in-game lineages or bets"
    model = insert_ingame(conn, status="paper_ok", validation=ingame_validation(log_loss=0.44))
    add_score(conn, pregame, game["game_id"], ingame_bets=2, ingame_pnl=-120)
    add_score(conn, model, game["game_id"], clv=None, ingame_bets=3, ingame_pnl=480)
    doc = page(client.get("/models").text)
    group = doc.one("#ingame").card("ingame")
    assert group.one(".disclosure-title").text == "In-game models" and not group.is_open
    row = group.row("ingame-model", model["id"])
    assert row.chip("beats-vegas").text == "beats vegas_wp" and row.chip("paper_ok").text == "paper ok"
    assert row.one(".row-ingame").text == "in-game paper 3 bets · +$4.80"
    assert row.one('.menu [data-action="assign-ingame"]').target == f"/trading?ingame_model={model['id']}#assign"
    pre = doc.row("model", pregame["id"])
    assert pre.one(".row-ingame").text == "in-game 2 bets · -$1.20", "a pre-game lineage with in-game bets shows its in-game line"
    assert not doc.has(f'[data-row="model"][data-id="{model["id"]}"]'), "an ingame_wp lineage is never in the pre-game lists"
    detail = page(client.get(f"/models/{model['id']}").text)
    assert detail.card("ingame-validation").has("details[data-key=model-ingame-validation]")
    assert detail.has("table.ingame-by-period") and detail.has("table.ingame-by-score") and detail.has("table.ingame-calibration")
    assert [r.first("td").text for r in detail.one("table.ingame-by-period").select("tbody tr")] == ["Q1", "Q2", "Q3", "Q4", "OT"]
    assert not detail.has('[data-action="validate"]') and not detail.has('[data-action="replay-snapshots"]'), "no pre-game jobs offered"
    assert detail.action("assign-ingame").text == "Assign in-game" and "3 bets" in detail.prop("in-game paper record")
    assert detail.stat("ingame-paper").one(".stat-value").text == "+$4.80"
    pre_page = page(client.get(f"/models/{pregame['id']}").text)
    assert pre_page.has('[data-action="replay-snapshots"]') and not pre_page.has("#ingame-validation")
    assert pre_page.prop("in-game bets").startswith("2 bets · -$1.20")


# ------------------------------------------------------------------ data refresh and seed


def test_pbp_rows_refresh_runs_weekly_in_season(conn, pool):
    now = datetime.now(timezone.utc)
    assert in_season(conn, now) is None, "no games, no season"
    insert_game(conn, "2026_05_KC_LV", kickoff_in_s=-3 * 86400, season=2026)
    assert in_season(conn, now) == 2026
    assert in_season(conn, now + timedelta(days=30)) is None, "weeks after the last game the season is over"
    clock = {"now": 1000.0}
    calls: list[int] = []
    outcome: dict[str, Any] = {"fail": False}

    def ingest(_conn, season):
        calls.append(season)
        if outcome["fail"]:
            raise RuntimeError("download failed")
        return {"rows": 10, "inserted": 10, "changed": 0, "games": 1}

    refresher = PbpRowsRefresher(pool.connection, clock=lambda: clock["now"], ingest=ingest, now=lambda: now)
    assert refresher.tick() is None and refresher.next_at == 1000.0 + FIRST_DELAY
    clock["now"] += FIRST_DELAY
    assert refresher.tick() == {"season": 2026, "rows": 10, "inserted": 10, "changed": 0, "games": 1}
    assert calls == [2026] and refresher.next_at == clock["now"] + WEEK
    clock["now"] += WEEK - 1
    assert refresher.tick() is None
    clock["now"] += 1
    outcome["fail"] = True
    assert refresher.tick() == {"season": 2026, "error": "download failed"}
    assert refresher.next_at == clock["now"] + RETRY_SECONDS and refresher.last_error == "download failed"
    off = PbpRowsRefresher(pool.connection, clock=lambda: clock["now"], ingest=ingest, now=lambda: now + timedelta(days=60))
    off.next_at = clock["now"]
    assert off.tick() == {"skipped": "off season"} and off.next_at == clock["now"] + OFF_SEASON_RECHECK
    assert calls == [2026, 2026], "nothing downloaded out of season"


def test_migration_seeds_ingame_lag_min_events(conn):
    row = conn.execute("SELECT value FROM settings WHERE key = 'ingame_lag_min_events'").fetchone()
    assert row is not None and row["value"] == 5
