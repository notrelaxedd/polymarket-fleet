"""Step 6C integration seams: the Models pages' "Assign in-game" link preselects the
in-game model on /trading, the Jobs form sends an ingame_wp search with its own eras
and keeps ingame_wp models and the family out of the pre-game forms, fills carry their
order's ingame flag, and settlement takes state_at_entry from the approval event."""
from __future__ import annotations

import json

from fleet.sim.ingame import search_from_params
from host.exchange.settle_sells import state_at
from host.trading import views
from tests.conftest import GAME_ID, flash_cookie, insert_game, insert_market, insert_model, lease_job, trade_setup
from tests.pagecheck import page
from tests.test_ingame_dashboard import _game_state, _ingame_model, _order
from tests.test_ingame_job import JOB_PARAMS, fixture_rows


def test_ingame_model_query_preselects_the_ingame_select(client, conn):
    insert_game(conn)
    insert_market(conn)
    ingame = _ingame_model(conn)
    doc = page(client.get(f"/trading?ingame_model={ingame['id']}").text)
    assert doc.card("assign").one("details.disclosure").is_open
    form = doc.form("assign")
    assert form.input("ingame_model_id").one(f'option[value="{ingame["id"]}"]').has_attr("selected")
    assert not form.input("model_id").has(f'option[value="{ingame["id"]}"]')


def test_models_page_assign_ingame_link_targets_the_preselect(client, conn):
    ingame = _ingame_model(conn)
    doc = page(client.get(f"/models/{ingame['id']}").text)
    assert doc.action("assign-ingame").target == f"/trading?ingame_model={ingame['id']}#assign"
    assert not doc.has('[data-action="assign"]') and not doc.has('[data-action="validate"]')


def test_jobs_form_sends_ingame_eras_for_an_ingame_search(client, conn):
    data = {"kind": "model_search", "family": "ingame_wp", "n": "12", "seed": "3", "top_k": "2", "seasons_first": "2010",
            "seasons_last": "", "ingame_train_first": "2014", "ingame_train_last": "2020", "ingame_validation_first": "2021",
            "ingame_validation_last": "", "target": "any_idle"}
    r = client.post("/jobs", data=data, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("model_search job "), r.text
    job = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    params = job["params"]
    assert params["family"] == "ingame_wp" and "seasons" not in params
    assert params["train_seasons"] == [2014, 2020] and params["validation_seasons"] == [2021, None]
    assert (params["n"], params["seed"], params["top_k"]) == (12, 3, 2)


def test_jobs_form_ingame_search_with_blank_eras_takes_the_host_defaults(client, conn):
    data = {"kind": "model_search", "family": "ingame_wp", "n": "", "seed": "", "top_k": "", "seasons_first": "2010",
            "seasons_last": "", "target": "any_idle"}
    r = client.post("/jobs", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    params = conn.execute("SELECT params FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()["params"]
    assert params["train_seasons"] == [2012, 2021] and params["validation_seasons"] == [2022, None]
    assert params["n"] == 20 and params["seed"] == 1 and params["top_k"] == 5


def test_jobs_form_refuses_a_half_given_ingame_era(client, conn):
    data = {"kind": "model_search", "family": "ingame_wp", "n": "5", "seed": "1", "top_k": "1",
            "ingame_train_last": "2020", "target": "any_idle"}
    r = client.post("/jobs", data=data, follow_redirects=False)
    assert r.status_code == 400 and "In-game train first season is required" in r.text


def test_jobs_page_keeps_ingame_out_of_the_pregame_forms(client, conn):
    pregame = insert_model(conn)
    ingame = _ingame_model(conn)
    html = client.get("/jobs").text
    doc = page(html)
    backtest, search = doc.form("backtest"), doc.form("model_search")
    assert not backtest.input("family").has('option[value="ingame_wp"]') and search.input("family").has('option[value="ingame_wp"]')
    assert str(pregame["id"]) in html and str(ingame["id"]) not in html
    eras = search.one('[data-card="ingame-eras"]')
    assert eras.has('input[name="ingame_train_first"]') and eras.has('input[name="ingame_validation_last"]')


def test_list_fills_carries_the_order_ingame_flag(conn):
    setup = trade_setup(conn)
    for ingame in (True, False):
        order = _order(conn, setup, ingame, status="filled")
        conn.execute("INSERT INTO fills (order_id, price, size, fee_cents, mode) VALUES (%s, 0.5, 10, 0, 'paper')", (order["id"],))
    flags = sorted(f["ingame"] for f in views.list_fills(conn))
    assert flags == [False, True]


def test_state_at_entry_prefers_the_approval_event(conn):
    setup = trade_setup(conn)
    order = _order(conn, setup, True)
    _game_state(conn, age_s=1.0, period=4, clock_seconds=100, home_score=21, away_score=17, possession="away")
    at_approval = {"period": 2, "clock_seconds": 300, "home_score": 7, "away_score": 3, "possession": "home"}
    conn.execute(
        "INSERT INTO order_events (order_id, to_status, actor, detail) VALUES (%s, 'approved', 'w', %s::jsonb)",
        (order["id"], '{"ingame": true, "state_at_entry": ' + json.dumps(at_approval) + "}"),
    )
    assert state_at(conn, GAME_ID, order["created_at"], order["id"]) == at_approval
    fallback = state_at(conn, GAME_ID, "infinity", None)
    assert fallback is not None and fallback["period"] == 4 and fallback["possession"] == "away"



def test_real_host_accepts_the_ingame_search_create_models(client, conn, make_worker):
    """The entries the in-game search returns (what the agent posts unchanged) are
    created by the real host as ingame_wp roots with their validation, and a lineage
    below the play floor stays a candidate."""
    rows = fixture_rows()
    result = search_from_params(dict(JOB_PARAMS, n=2), lambda: iter(rows), lambda c, p: None, lambda: False)
    w = make_worker(role="model_search")
    job = lease_job(conn, w, "model_search", dict(JOB_PARAMS, n=2))
    assert len(result["create_models"]) == 2
    for entry in result["create_models"]:
        r = client.post("/api/v1/models", json={**entry, "job_id": str(job["id"])}, headers=w.headers)
        assert r.status_code == 201, r.text
        row = conn.execute("SELECT * FROM models WHERE id = %s", (r.json()["id"],)).fetchone()
        assert row["family"] == "ingame_wp" and row["lineage_id"] == row["id"]
        assert row["validation_metrics"]["era"] == "validation" and row["backtest_metrics"]["era"] == "search"
        assert row["status"] == "candidate"  # a two-game validation era is far below 10 000 plays
