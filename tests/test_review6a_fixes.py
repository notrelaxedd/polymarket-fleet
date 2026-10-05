"""Regression tests for the step 6 Part A review findings on the host side: the two
eras never meet (settings, job creation, validate of an in-sample model, a stored
backtest on validation seasons), an unvalidated lineage never ranks on paper, the
startup recompute demotes lineages a migration made ineligible, p-values below 0.001
and the Models row's paper cell."""
from __future__ import annotations

import itertools
import uuid

import pytest
from psycopg.types.json import Jsonb

from host.errors import BadRequest
from host.jobparams import copied_settings, prepare_params
from host.settings import get_settings, validate_settings
from tests.conftest import (assignment_row, backtest_metrics, ingest_fixture, insert_model, insert_validated_model,
                            model_row, set_setting, trade_setup, validation_metrics)

LIVE_GAME = "2025_12_KC_BUF"


# the eras -----------------------------------------------------------------------------


def test_settings_reject_a_validation_era_that_empties_the_capped_search_era(client, conn):
    """Review 6A (high): with a null last search season the validation era must start
    after the first search season, else the capped search era is empty and the job
    used to fall back to [2010, last complete]."""
    ingest_fixture(conn)
    assert client.post("/api/settings", json={"backtest_seasons": [2010, None]}).status_code == 200
    assert client.post("/api/settings", json={"validation_seasons": [2015, None]}).status_code == 200, "2010 comes first"
    r = client.post("/api/settings", json={"backtest_seasons": [2016, None]})
    assert r.status_code == 400 and "after the first search season (2016)" in r.json()["detail"]
    r = client.post("/api/settings", json={"backtest_seasons": [2016, None], "validation_seasons": [2016, None]})
    assert r.status_code == 400
    assert client.post("/api/settings", json={"backtest_seasons": [2016, None], "validation_seasons": [2020, None]}).status_code == 200


def test_unresolvable_eras_refuse_the_job_instead_of_a_fallback(client, conn):
    """Review 6A (high) case (a): a validation era after the last complete season used
    to make the search carry [2010, 2025] and the hard-coded validation [2022, 2025]."""
    ingest_fixture(conn)
    assert client.post("/api/settings", json={"backtest_seasons": [2010, None], "validation_seasons": [2026, None]}).status_code == 200
    for kind, params in (("model_search", {"family": "elo_blend"}), ("validate", {"model_id": str(insert_model(conn)["id"])})):
        with pytest.raises(BadRequest, match="validation_seasons"):
            prepare_params(conn, kind, params)
        r = client.post("/api/jobs", json={"kind": kind, "params": params})
        assert r.status_code == 400 and "validation_seasons" in r.json()["detail"]
    backtest = prepare_params(conn, "backtest", {"family": "elo_blend", "params": {}})
    assert backtest["backtest_seasons"] == [2010, 2025], "no validation season exists yet, so nothing is held out"
    set_setting(conn, "backtest_seasons", [2016, None])
    set_setting(conn, "validation_seasons", [2015, None])  # written past the API: the job still refuses
    with pytest.raises(BadRequest, match="backtest_seasons"):
        copied_settings(conn, "model_search")
    assert copied_settings(conn, "train")["backtest_seasons"] == [2010, 2025], "train never reads the eras"


SEARCH_PAIRS = [[2010, None], [2016, None], [2020, None], [2010, 2015], [2010, 2019], [2016, 2021], [2018, 2023]]
VALIDATION_PAIRS = [[2012, None], [2016, None], [2017, 2019], [2020, None], [2022, None], [2022, 2023], [2026, None]]


def test_every_accepted_settings_pair_keeps_the_search_before_validation(conn):
    """Property: for every pair of eras validate_settings accepts, a model search and a
    validate either carry a search era that ends before the validation era starts or
    are refused (never a fallback that overlaps)."""
    ingest_fixture(conn)
    model = insert_model(conn)
    current = get_settings(conn)
    accepted = 0
    for search, validation in itertools.product(SEARCH_PAIRS, VALIDATION_PAIRS):
        try:
            validate_settings({"backtest_seasons": search, "validation_seasons": validation}, current)
        except BadRequest:
            continue
        accepted += 1
        set_setting(conn, "backtest_seasons", search)
        set_setting(conn, "validation_seasons", validation)
        for kind, params in (("model_search", {"family": "elo_blend"}), ("validate", {"model_id": str(model["id"])})):
            try:
                out = prepare_params(conn, kind, params)
            except BadRequest:
                continue
            assert out["backtest_seasons"][1] < out["validation_seasons"][0], (search, validation, kind, out)
    assert accepted >= 15, accepted


def test_validate_refuses_a_model_searched_on_the_validation_era(client, conn):
    """Review 6A (high): a step 3 to 5 model searched on [2010, null] (2013-2025) was
    "validated" on 2022-2025, in-sample, and ranked as held out."""
    ingest_fixture(conn)
    old = insert_model(conn, metrics=backtest_metrics(seasons=list(range(2013, 2026))))
    r = client.post("/api/jobs", json={"kind": "validate", "params": {"model_id": str(old["id"])}})
    assert r.status_code == 400 and "in-sample" in r.json()["detail"] and "2013-2025" in r.json()["detail"]
    child = insert_model(conn, parent=old, params=old["params"], trained_through=[2025, 5])
    assert client.post("/api/jobs", json={"kind": "validate", "params": {"model_id": str(child["id"])}}).status_code == 400, "a child shares the lineage's search"
    clean = insert_model(conn, params={"k": 30.0}, metrics=backtest_metrics(seasons=list(range(2013, 2022))))
    r = client.post("/api/jobs", json={"kind": "validate", "params": {"model_id": str(clean["id"])}})
    assert r.status_code == 201 and r.json()["params"]["validation_seasons"] == [2022, 2025]


def test_validate_reads_the_creating_search_job_when_metrics_are_missing(client, conn):
    ingest_fixture(conn)
    job_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO jobs (id, kind, role, params, status) VALUES (%s, 'model_search', 'model_search', %s, 'succeeded')",
        (job_id, Jsonb({"family": "elo_blend", "backtest_seasons": [2010, 2025]})),
    )
    model = insert_model(conn, params={"k": 33.0})
    conn.execute("UPDATE models SET created_by_job_id = %s WHERE id = %s", (job_id, model["id"]))
    r = client.post("/api/jobs", json={"kind": "validate", "params": {"model_id": str(model["id"])}})
    assert r.status_code == 400 and "2010-2025" in r.json()["detail"]


def test_a_stored_backtest_must_stay_in_the_search_era(client, conn):
    """Review 6A (high): a backtest with model_id on validation seasons replaced the
    lineage's search-era metrics, and the next validate dropped the overfit flag."""
    ingest_fixture(conn)
    model = insert_model(conn)
    r = client.post("/api/jobs", json={"kind": "backtest", "params": {"model_id": str(model["id"]), "seasons": [2022, None]}})
    assert r.status_code == 400 and "before the validation era" in r.json()["detail"]
    ok = client.post("/api/jobs", json={"kind": "backtest", "params": {"model_id": str(model["id"]), "seasons": [2019, 2021]}})
    assert ok.status_code == 201
    free = client.post("/api/jobs", json={"kind": "backtest", "params": {"family": "elo_blend", "params": {}, "seasons": [2022, None]}})
    assert free.status_code == 201, "a backtest by family and params is not stored on a lineage"


# the leaderboard ----------------------------------------------------------------------


def test_an_unvalidated_lineage_never_ranks_on_its_paper_record(client, conn):
    """Review 6A (medium): 5 paper games and 30 paper bets ranked an unvalidated lineage
    first, above every validated one."""
    ingest_fixture(conn)
    unvalidated = insert_model(conn, metrics=backtest_metrics())
    validated = insert_validated_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1})
    games = [r["game_id"] for r in conn.execute("SELECT game_id FROM games WHERE season = 2025 ORDER BY game_id LIMIT 6")]
    for game_id in games:
        conn.execute(
            "INSERT INTO model_scores (model_id, game_id, mode, lineage_id, n_bets, stake_cents, pnl_cents, avg_clv)"
            " VALUES (%s, %s, 'paper', %s, 6, 6000, 100, 0.01)",
            (unvalidated["id"], game_id, unvalidated["lineage_id"]),
        )
    board = client.get("/api/models").json()
    assert [e["id"] for e in board["ranked"]] == [str(validated["id"])]
    entry = next(e for e in board["unranked"] if e["id"] == str(unvalidated["id"]))
    assert entry["unranked_reason"] == "not validated" and entry["rank_mode"] == "paper" and "rank" not in entry
    assert 'chip-unvalidated' in client.get("/models").text


# the startup recompute ----------------------------------------------------------------


def test_startup_recompute_demotes_lineages_without_validation(conn):
    """Review 6A (medium): migration 0006 made the gate stricter but nothing re-judged
    existing lineages until their next settlement, so a live_eligible lineage without
    validation could keep a live assignment."""
    from host.startup import recompute_statuses

    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME)
    paper_ok = insert_model(conn, status="paper_ok", params={"k": 31.0})
    good = insert_validated_model(conn, status="paper_ok", params={"k": 32.0})
    out = recompute_statuses(conn)
    assert model_row(conn, live.model["id"])["status"] == "candidate"
    assert model_row(conn, paper_ok["id"])["status"] == "candidate"
    assert model_row(conn, good["id"])["status"] == "paper_ok", "a lineage that passes keeps its status"
    assert assignment_row(conn, live.assignment["id"])["status"] == "halted", "its live assignment is halted"
    assert str(live.model["lineage_id"]) in out["changed"] and str(good["lineage_id"]) not in out["changed"]
    assert recompute_statuses(conn)["changed"] == [], "a second boot changes nothing"


def test_host_main_runs_the_startup_recompute():
    import inspect

    from host import main

    source = inspect.getsource(main.main)
    assert source.index("db.migrate(") < source.index("startup.run(") < source.index("create_app(")


# the page -----------------------------------------------------------------------------


def test_tiny_market_p_reads_below_one_in_a_thousand(client, conn):
    """Review 6A (low): p = 1/10001 printed as "p = 0.000"."""
    model = insert_validated_model(conn, validation=validation_metrics(market_p=1 / 10001))
    row = client.get("/models").text
    assert "p &lt; 0.001" in row and "p 0.000" not in row
    detail = client.get(f"/models/{model['id']}").text
    assert "p &lt; 0.001 (sign-flip test" in detail and "p = 0.000" not in detail
    other = insert_validated_model(conn, params={"k": 19.0}, validation=validation_metrics(market_p=0.0123))
    assert "p = 0.012 (sign-flip test" in client.get(f"/models/{other['id']}").text


def test_the_paper_cell_wraps_so_the_summary_keeps_its_width(client, conn):
    """Review 6A (medium): a nowrap paper cell squeezed the summary to one word per line
    at 1280 px and pushed the buttons out of the table."""
    insert_validated_model(conn)
    page = client.get("/models").text
    assert 'class="c-paper"' in page and 'class="nowrap c-paper"' not in page
    css = client.get("/static/style.css").text
    assert "table.models td.c-summary { min-width: 14rem; }" in css
