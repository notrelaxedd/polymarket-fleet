"""Human labels for machine identifiers (host/labels.py): the table, the Title Case
fallback, the Jinja `label` filter, and the pages and fleet API that show them. Forms,
data-* attributes and JSON keep the raw values; only the visible text changes."""
from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from host import web
from host.fleet_events import ROLE_NAMES, audit_text, job_text
from host.labels import label, role_names
from host.settings import KIND_TO_ROLE, ROLES
from tests.conftest import insert_job
from tests.pagecheck import fleet_html, page


@pytest.mark.parametrize(
    ("value", "group", "want"),
    [
        ("idle", "role", "Idle"),
        ("backtest", "role", "Backtest"),
        ("model_search", "role", "Model Search"),
        ("train", "role", "Training"),
        ("trade", "role", "Trading"),
        ("sleep", "kind", "Test Sleep"),
        ("validate", "kind", "Validation"),
        ("model_search", "kind", "Model Search"),
        ("queued", "status", "Queued"),
        ("leased", "status", "Running"),
        ("cancel_requested", "status", "Cancelling"),
        ("succeeded", "status", "Succeeded"),
        ("failed", "status", "Failed"),
        ("cancelled", "status", "Cancelled"),
        ("cancel_requested", "event", "Cancel Requested"),
        ("lease_expired", "event", "Lease Expired"),
        ("re-leased", "event", "Lease Renewed"),
        ("re-offered", "event", "Offered Again"),
        ("targeted", "event", "Sent to a Worker"),
        ("regime_dependent", "flag", "Regime Dependent"),
        ("paper_ok", "model", "Paper OK"),
        ("live_eligible", "model", "Live Eligible"),
        ("polymarket_us", "source", "Polymarket US"),
        ("vegas_wp", None, "Vegas WP"),
        ("cancel_requested", None, "Cancelling"),
    ],
)
def test_known_values(value: str, group: str | None, want: str) -> None:
    assert label(value, group) == want


def test_fallback_turns_unknown_snake_case_into_title_case() -> None:
    assert label("lease_expired_twice") == "Lease Expired Twice"
    assert label("rejected_by_exchange") == "Rejected by Exchange", "small words stay lower case after the first"
    assert label("clock_skew") == "Clock Skew" and label("stale_book") == "Stale Book"
    assert label("cancel-all") == "Cancel-All" and label("in-game model") == "In-Game Model"
    assert label("max_clv_roi") == "Max CLV ROI", "known abbreviations in capitals"
    assert label("settings_changed", "action") == "Settings Changed", "an unknown group falls through"


def test_text_that_is_not_an_identifier_and_none() -> None:
    assert label(None) == ""
    assert label("KC @ LV") == "KC @ LV" and label("box1 (Pi)") == "box1 (Pi)"
    assert label(2025) == "2025"


def test_every_role_and_kind_has_a_label() -> None:
    assert all(label(r, "role") != r for r in ROLES)
    assert all(label(k, "kind") != k for k in KIND_TO_ROLE)
    assert [r["name"] for r in role_names(ROLES)] == ["Idle", "Backtest", "Model Search", "Training", "Trading"]
    assert [dict(r) for r in ROLE_NAMES] == role_names(ROLES)


def test_jinja_filter_is_registered() -> None:
    assert web.ENV.filters["label"] is label
    tpl = web.ENV.from_string("{{ k | label('kind') }}|{{ s | label('status') }}|{{ x | label }}")
    assert tpl.render(k="model_search", s="leased", x="lease_expired") == "Model Search|Running|Lease Expired"


def test_fleet_event_texts_use_the_labels() -> None:
    assert job_text("claimed", "model_search", {}) == ("Took a Model Search job", "ok")
    assert job_text("failed", "sleep", {}) == ("A Test Sleep job failed", "hot")
    assert job_text("re-leased", "train", {}) == ("Lease Renewed (Training job)", "fg")
    assert audit_text("set_role", "owner@example.com", {"desired_role": "model_search"}) == (
        "Moved to Model Search by owner@example.com", "ok")
    assert audit_text("auto_kill", None, {"reason": "late_fill"}) == ("Kill switch on automatically: Late Fill", "hot")
    assert audit_text("settings_changed", "owner", {}) == ("Settings Changed", "fg")


def test_api_fleet_roles_are_title_case(client, make_worker) -> None:
    make_worker("box1", role="model_search")
    body = client.get("/api/fleet").json()
    names = {r["id"]: r["name"] for r in body["roles"]}
    assert names["model_search"] == "Model Search" and names["train"] == "Training"
    assert body["workers"][0]["desired_role"] == "model_search", "the API keeps the raw role id"


def test_jobs_list_and_job_page_show_labels_not_identifiers(client, conn, make_worker) -> None:
    make_worker("box1")
    job = insert_job(conn, "model_search", params=Jsonb({"family": "elo_blend", "n": 50}))
    insert_job(conn, "sleep", status="cancel_requested", params=Jsonb({"seconds": 5}))
    html = client.get("/jobs").text
    p = page(html)
    row = p.row("job", job["id"])
    assert row.one(".row-title").text.startswith("Model Search elo_blend n 50")
    assert p.one('select[data-switch="job-kind"] option[value="model_search"]').text == "Model Search"
    assert p.has('input[name="kind"][value="model_search"]'), "the form still posts the raw kind"
    assert "model_search" not in p.text and "cancel requested" not in p.text and "Cancelling" in p.text
    detail = page(client.get(f"/jobs/{job['id']}").text)
    assert detail.one("h1").text.startswith("Model Search") and detail.prop("role") == "Model Search"
    assert "model_search" not in detail.text, "no raw kind in the visible text"


def test_fleet_cards_show_role_names_and_keep_the_values(client, make_worker) -> None:
    w = make_worker("box1", role="model_search")
    p = page(fleet_html(client))
    select = p.row("worker", w.id).one('select[name="role"]')
    options = {o.attr("value"): o.text for o in select.select("option")}
    assert options == {"idle": "Idle", "backtest": "Backtest", "model_search": "Model Search", "train": "Training",
                       "trade": "Trading"}
    assert select.one("option[selected]").attr("value") == "model_search"
    assert "model_search" not in p.row("worker", w.id).text
