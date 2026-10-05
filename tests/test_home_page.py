"""The Home page at / (docs/UI.md "Home"): four stats, "Needs attention" and the last
settled bets. Fleet moved to /fleet (its form posts land there)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from host import eligibility, kill
from tests.conftest import (
    approved_order, auth_state, flash_cookie, insert_game, insert_job, insert_model, insert_paper_bet,
    insert_validated_model, make_assignment, set_heartbeat_age, trade_setup, validation_metrics,
)
from tests.pagecheck import page


def _checked(conn) -> None:
    """The exchange credentials were probed and passed (otherwise Home asks for a probe)."""
    auth_state(conn, credentials_present=True, auth_ok=True, auth_checked_at=datetime.now(timezone.utc))


def _home(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    return page(r.text)


def test_home_is_the_landing_page_and_fleet_moved(client, conn, make_worker):
    _checked(conn)
    w = make_worker("box1")
    p = _home(client)
    assert p.page_name == "home" and p.one("h1").text == "Home" and p.one("details.intro").is_open
    assert [s.attr("data-stat") for s in p.select(".stats .stat")] == ["workers", "today", "open-orders", "best-model"]
    assert p.stat("workers").target == "/fleet" and p.prop("Workers online") == "1 / 1"
    assert p.prop("Today paper") == "$0.00" and p.stat("today").target == "/trading"
    assert p.prop("Open orders") == "0" and p.stat("open-orders").target == "/trading#open-orders"
    assert p.prop("Best model") == "-" and "none ranked yet" in p.stat("best-model").text
    assert p.card("attention").text.endswith("Nothing needs you.") and not p.has('[data-list="attention"]')
    assert "No settled bets yet." in p.card("recent").text
    assert not p.has('[data-row="worker"]'), "the worker rows live on /fleet"
    assert page(client.get("/fleet").text).has(f'[data-row="worker"][data-id="{w.id}"]')
    r = client.post(f"/workers/{w.id}/enabled", data={"enabled": "false"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/fleet" and flash_cookie(r) == "box1 disabled"
    r = client.post("/kill", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and flash_cookie(r).startswith("Trading killed")
    assert _home(client).flash.startswith("Trading killed")


def test_needs_attention_lists_each_signal(client, conn, make_worker):
    p = _home(client)
    unchecked = p.row("attention", "exchange-unchecked")
    assert unchecked.chip("unchecked").has_class("chip-warn") and unchecked.one("a.row-main").target == "/trading#exchange"
    assert p.card("attention").one(".count").text == "1"
    _checked(conn)
    gone = make_worker("gone")
    set_heartbeat_age(conn, gone.id, 600)
    recent = make_worker("blip")
    set_heartbeat_age(conn, recent.id, 120)
    off = make_worker("parked", enabled=False)
    set_heartbeat_age(conn, off.id, 3600)
    model = insert_model(conn)
    failed = insert_job(conn, "validate", status="failed", params=Jsonb({"model_id": str(model["id"])}), error="out of memory\ntrace")
    conn.execute("UPDATE jobs SET finished_at = now() WHERE id = %s", (failed["id"],))
    insert_game(conn)
    assignment = make_assignment(conn)
    conn.execute("UPDATE models SET status = 'retired' WHERE lineage_id = (SELECT lineage_id FROM assignments WHERE id = %s)",
                 (assignment["id"],))
    p = _home(client)
    keys = p.row_ids("attention")
    assert keys == [f"worker-{gone.id}", f"assignment-{assignment['id']}", f"validate-{failed['id']}"], keys
    row = p.row("attention", f"worker-{gone.id}")
    assert row.one(".row-title").text == "gone is offline" and row.one(".row-meta").text == "last seen 10 min ago"
    assert row.chip("offline").text == "offline" and row.one("a.row-main").target == "/fleet"
    row = p.row("attention", f"assignment-{assignment['id']}")
    assert row.one(".row-title").text == "KC @ LV: no eligible model" and row.chip("no-model").has_class("chip-bad")
    assert row.one(".row-meta").text == "paper assignment active, model retired"
    row = p.row("attention", f"validate-{failed['id']}")
    assert row.one(".row-meta").text == "out of memory" and row.one("a.row-main").target == f"/jobs/{failed['id']}"
    later = insert_job(conn, "validate", status="succeeded", params=Jsonb({"model_id": str(model["id"])}))
    assert later and f"validate-{failed['id']}" not in _home(client).row_ids("attention"), "a later success clears it"
    kill.set_kill(conn, "test")
    p = _home(client)
    assert p.row_ids("attention")[:2] == ["kill", "exchange-down"], "the top bar's signals lead"
    assert p.row("attention", "kill").chip("killed").text == "killed" and p.row("attention", "kill").one("a.row-main").target == "/settings#kill"
    assert all(item.has(".chip") and item.one(".chip").text for item in p.rows("attention")), "every chip has a word"


def test_home_stats_and_recent_bets(client, conn, make_worker):
    _checked(conn)
    make_worker("box1")
    make_worker("box2", online=False)
    best = insert_validated_model(conn, status="paper_ok")
    insert_game(conn)
    for i, cents in enumerate((150, -80, 0, 220, -30, 500)):
        insert_paper_bet(conn, best, "2026_05_KC_LV", 0.01, pnl_cents=cents, days_ago=6 - i)
    p = _home(client)
    assert p.prop("Workers online") == "1 / 2"
    assert p.stat("best-model").target == f"/models/{best['id']}" and p.prop("Best model") == "ROI +4.0%"
    assert p.stat("best-model").one(".stat-note").text.startswith("elo_blend K ")
    rows = p.rows("bet")
    assert len(rows) == 5 and [r.one(".row-value").text for r in rows] == ["+$5.00", "-$0.30", "+$2.20", "$0.00", "-$0.80"]
    assert rows[1].one(".row-value").has_class("neg") and rows[0].chip("win").has_class("chip-ok")
    assert rows[0].one(".row-title").text == "KC @ LV" and rows[0].one(".row-meta").text.startswith("paper · c · ")
    conn.execute("UPDATE settings SET value = 'true' WHERE key = 'live_enabled'")
    assert _home(client).prop("Today live") == "$0.00", "the current mode's P&L"


def test_live_assignment_halted_by_a_lost_eligibility_needs_attention(client, conn):
    """Leaving live_eligible halts the live assignment in the same transaction, so the
    rule must look at halted ones too (reached through the real path, no raw UPDATE)."""
    _checked(conn)
    setup = trade_setup(conn, mode="live", model_status="live_eligible")
    with conn.transaction():
        assert eligibility.recompute_lineage(conn, setup.model["lineage_id"]) == "candidate"
    aid = setup.assignment["id"]
    assert conn.execute("SELECT status FROM assignments WHERE id = %s", (aid,)).fetchone()["status"] == "halted"
    row = _home(client).row("attention", f"assignment-{aid}")
    assert row.one(".row-title").text == "KC @ LV: no eligible model" and row.chip("no-model").has_class("chip-warn")
    assert row.one(".row-meta").text == "live assignment halted, model candidate"
    assert row.one("a.row-main").target == "/trading#assignments"
    conn.execute("UPDATE games SET status = 'final' WHERE game_id = %s", (setup.game["game_id"],))
    assert f"assignment-{aid}" not in _home(client).row_ids("attention"), "a finished game needs nothing more"


def test_halted_paper_assignment_of_a_retired_lineage_is_not_listed(client, conn):
    """Retiring halts the assignments on purpose: listing them would repeat the owner's own action."""
    _checked(conn)
    setup = trade_setup(conn)
    conn.execute("UPDATE assignments SET status = 'halted' WHERE id = %s", (setup.assignment["id"],))
    conn.execute("UPDATE models SET status = 'retired' WHERE lineage_id = %s", (setup.model["lineage_id"],))
    assert not _home(client).has('[data-list="attention"]')


def test_best_model_uses_the_models_page_headline(client, conn):
    """No held-out bets reads "ROI -" on Home as on /models and the model page, not "ROI +0.0%"."""
    model = insert_validated_model(conn, params={"k": 22.0, "hfa": 50.0, "mov_scale": 1},
                                   validation=validation_metrics(n_bets=0, roi=0.0))
    best = _home(client).stat("best-model")
    assert best.one(".stat-value").text == "ROI -" and best.target == f"/models/{model['id']}"
    assert page(client.get("/models").text).stat("best").one(".stat-value").text == "ROI -"


def test_home_stats_and_attention_refresh_from_their_fragment(client, conn, make_worker):
    """#home-live holds the stats, Needs attention and Recent; GET /fragments/home serves
    its inner HTML and app.js refetches it, so the numbers are never older than the
    "updated N s ago" line says."""
    _checked(conn)
    make_worker("box1")
    p = _home(client)
    live = p.one("#home-live")
    assert live.has('.stats [data-stat="open-orders"]') and live.card("attention") and live.card("recent")
    assert p.count(".stats") == 1 and live.has(".stats"), "the only stats grid is inside the refreshed region"
    setup = trade_setup(conn)
    approved_order(conn, setup)
    r = client.get("/fragments/home")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    frag = page(r.text)
    assert not frag.has("main") and not frag.has("h1"), "the fragment is the region's inner HTML only"
    assert frag.prop("Open orders") == "1" and frag.prop("Workers online") == "2 / 2"
    assert [s.attr("data-stat") for s in frag.select(".stats .stat")] == ["workers", "today", "open-orders", "best-model"]
    js = (Path(__file__).resolve().parent.parent / "host" / "static" / "app.js").read_text()
    assert 'refresh("home-live", "/fragments/home")' in js and '"home-live"' in js.split("function tick")[1].split("function ")[0]
