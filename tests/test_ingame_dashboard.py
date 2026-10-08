"""The /trading page for in-game trading (docs/DASHBOARD.md "Trading page", step 6C
contract section 12): per assignment the live score and clock with the state age or
"state stale", the in-game model probability next to the home mid (the row's
.row-ingame line, step 7), an `in-game` chip on in-game orders and fills, the New
assignment form's in-game select and box, the per-assignment in-game toggle (in the
row's menu), the "In-game feed" group and the "Probe game state" button. Pages are read
through tests/pagecheck.py hooks (data-row, data-chip, data-card, data-form). Rows are inserted straight into the 0009 tables, so these tests do not depend
on the feed poller or the in-game approval path. The two calls into the trading side
(create_assignment with the in-game fields, assignments_ingame.set_ingame for the
toggle) are checked by their arguments and against the real functions."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import psycopg

from fleet.models.ingame_wp import FEATURE_NAMES, IngameWP
from fleet.sim.odds import devig
from host.trading import assignments, assignments_ingame
from host.web import prob
from tests.conftest import GAME_ID, flash_cookie, insert_game, insert_market, insert_model, set_setting, trade_setup
from tests.pagecheck import Node, page
from tests.test_style import declarations

FIXTURES = Path(__file__).parent / "fixtures"
COEF = [0.1, 0.8, 1.0, 0.2, 0.3, 0.05, -0.02, 0.04, 0.0, 0.0]
STATE = {"status": "in", "period": 3, "clock_seconds": 252, "home_score": 14, "away_score": 17, "possession": "home",
         "down": 2, "distance": 7, "yardline_100": 40, "home_timeouts": 3, "away_timeouts": 2}


def _ingame_model(conn: psycopg.Connection, status: str = "paper_ok") -> dict[str, Any]:
    assert len(COEF) == len(FEATURE_NAMES)
    params = {"l2": 1.0, "time_scale": 1.0, "fp_scale": 1.0, "seed": uuid.uuid4().hex[:6]}
    artifact = {"coef": COEF, "features": list(FEATURE_NAMES), "n_train": 50_000, "train_seasons": [2012, 2021]}
    return insert_model(conn, family="ingame_wp", params=params, status=status, artifact=artifact)


def _set_ingame(conn: psycopg.Connection, assignment: dict[str, Any], model: dict[str, Any] | None, on: bool) -> None:
    conn.execute("UPDATE assignments SET ingame_model_id = %s, trade_ingame = %s WHERE id = %s",
                 (model["id"] if model else None, on, assignment["id"]))


def _game_state(conn: psycopg.Connection, game_id: str = GAME_ID, age_s: float = 3.0, **state: Any) -> None:
    row = {**STATE, **state}
    conn.execute(
        """
        INSERT INTO game_state (game_id, ts, source, status, period, clock_seconds, home_score, away_score, possession,
                                down, distance, yardline_100, home_timeouts, away_timeouts)
        VALUES (%s, now() - make_interval(secs => %s), 'espn_summary', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (game_id, age_s, row["status"], row["period"], row["clock_seconds"], row["home_score"], row["away_score"],
         row["possession"], row["down"], row["distance"], row["yardline_100"], row["home_timeouts"], row["away_timeouts"]),
    )


def _lag(conn: psycopg.Connection, n: int, lag_s: float, source: str = "espn_summary", game_id: str = GAME_ID) -> None:
    for i in range(n):
        conn.execute(
            """
            INSERT INTO feed_lag (game_id, event_kind, event_key, source, feed_seen_at, market_moved_at, lag_s)
            VALUES (%s, 'score', %s, %s, now() - make_interval(secs => %s), now() - make_interval(secs => %s), %s)
            """,
            (game_id, f"{source}-{i}-{uuid.uuid4().hex[:6]}", source, 600 - i, 600 - i + lag_s, lag_s),
        )


def _live(client: Any) -> Node:
    return page(client.get("/trading").text).one("#trading-live")


def _assignment(client: Any, assignment: dict[str, Any]) -> Node:
    return _live(client).row("assignment", assignment["id"])


def _assign_form(client: Any, path: str = "/trading") -> Node:
    return page(client.get(path).text).form("assign")


def _kicked_off(conn: psycopg.Connection) -> Any:
    setup = trade_setup(conn)
    conn.execute("UPDATE games SET kickoff_at = now() - interval '100 minutes' WHERE game_id = %s", (GAME_ID,))
    return setup


def test_assignment_row_shows_score_clock_age_and_model_p_next_to_the_mid(client, conn):
    setup = _kicked_off(conn)
    model = _ingame_model(conn)
    _set_ingame(conn, setup.assignment, model, True)
    _game_state(conn, age_s=3)
    line = _assignment(client, setup.assignment).one(".row-meta.row-ingame")
    assert "Q3 4:12 · 17-14 · 3 s ago" in line.text, "away-home score, quarter clock and the state age on one line"
    chip = line.chip("ingame-on")
    assert chip.text == "in-game on" and chip.has_class("chip-ingame") and chip.has_class("chip-ok")
    assert not line.has('[data-chip="state-stale"]') and "state stale" not in line.text
    p = IngameWP.from_json(model["params"], model["artifact"]).predict(STATE, devig(-150, 130))
    shown = line.one("[data-p-home]")
    assert shown.attr("data-p-home") == f"{p:.3f}" and f"model LV {prob(p)}" in shown.text, "the in-game p of the home team"
    assert "mid 51%" in shown.text, "the home market mid (bid 0.50, ask 0.52) sits next to it"


def test_stale_state_reads_state_stale_and_missing_state_says_so(client, conn):
    setup = _kicked_off(conn)
    _set_ingame(conn, setup.assignment, _ingame_model(conn), True)
    line = _assignment(client, setup.assignment).one(".row-ingame")
    assert "no game state yet" in line.text
    set_setting(conn, "ingame_max_state_age_s", 30)
    _game_state(conn, age_s=120, status="half", period=2, clock_seconds=0)
    line = _assignment(client, setup.assignment).one(".row-ingame")
    stale = line.chip("state-stale")
    assert stale.text == "state stale" and stale.has_class("chip-warn")
    assert line.one(".game-line.is-stale").text == "Half · 17-14 · 2 min ago", "the stale line keeps the last state and its age, muted"
    shown = line.one("[data-p-home]")
    assert shown.has_class("muted") and shown.has_class("is-stale"), "a probability from a stale state is muted"
    assert shown.text.endswith("(from the stale state, not traded)"), "and marked: the host rejects in-game orders on it"
    _game_state(conn, age_s=90, status="final", period=4, clock_seconds=0, home_score=20)
    line = _assignment(client, setup.assignment).one(".row-ingame")
    assert not line.has('[data-chip="state-stale"]') and "Final · 17-20 · 1 min ago" in line.text, "a final state never goes stale"
    assert "not traded" not in line.text


def test_period_labels_for_overtime_end_of_period_and_final(conn):
    from host.trading.views_ingame import clock_label

    assert clock_label({**STATE, "period": 5, "clock_seconds": 61}) == "OT 1:01 · 17-14"
    assert clock_label({**STATE, "status": "end_period", "period": 1}) == "End Q1 · 17-14"
    assert clock_label({**STATE, "status": "final", "home_score": 20}) == "Final · 17-20"
    assert clock_label({"status": "pre", "home_score": None, "away_score": None}) == "Pre-game"


def test_assignment_without_ingame_shows_off_and_a_toggle_form(client, conn):
    setup = trade_setup(conn)
    model = _ingame_model(conn)
    row = _assignment(client, setup.assignment)
    assert not row.has(".row-ingame") and not row.has(".chip-ingame"), "nothing in-game set: no in-game line, the row stays short"
    form = row.one('.menu [data-form="ingame-toggle"]')
    assert form.target == f"/assignments/{setup.assignment['id']}/ingame" and form.attr("method") == "post", \
        "an active assignment carries the in-game toggle form in its menu"
    assert form.has(f'option[value="{model["id"]}"]') and form.has('input[name="trade_ingame"]')
    assert not form.has("[checked]") and not form.has("option[selected]")
    conn.execute("UPDATE games SET status = 'final' WHERE game_id = %s", (GAME_ID,))
    row = _assignment(client, setup.assignment)
    assert not row.has('[data-form="ingame-toggle"]'), "no toggle once the game is final"


def test_toggle_form_preselects_the_current_model_and_box(client, conn):
    setup = trade_setup(conn)
    model = _ingame_model(conn)
    _set_ingame(conn, setup.assignment, model, True)
    row = _assignment(client, setup.assignment)
    form = row.one('[data-form="ingame-toggle"]')
    assert form.one(f'option[value="{model["id"]}"]').has_attr("selected")
    assert form.one('input[name="trade_ingame"]').has_attr("checked")
    assert row.one(".row-ingame").chip("ingame-on").text == "in-game on"


def _order(conn: psycopg.Connection, setup: Any, ingame: bool, status: str = "open", **cols: Any) -> dict[str, Any]:
    return conn.execute(
        """
        INSERT INTO orders (client_request_id, assignment_id, worker_id, job_id, market_id, mode, price, size, cost_cents,
                            status, filled_size, my_p, market_p, edge, rationale, ingame, reject_reason)
        VALUES (%s, %s, %s, %s, %s, 'paper', 0.5, 10, 500, %s, %s, 0.6, 0.5, 0.1, 'r', %s, %s) RETURNING *
        """,
        (uuid.uuid4().hex, setup.assignment["id"], setup.worker.id, setup.job["id"], setup.market["id"], status,
         10 if status == "filled" else 0, ingame, cols.get("reject_reason")),
    ).fetchone()


def test_ingame_chip_on_ingame_orders_and_fills_only(client, conn):
    setup = trade_setup(conn)
    ingame_open = _order(conn, setup, True)
    pregame_open = _order(conn, setup, False)
    filled = _order(conn, setup, True, status="filled")
    fill = conn.execute(
        "INSERT INTO fills (order_id, price, size, fee_cents, mode) VALUES (%s, 0.5, 10, 0, 'paper') RETURNING *", (filled["id"],)
    ).fetchone()
    plain = _order(conn, setup, False, status="filled")
    plain_fill = conn.execute(
        "INSERT INTO fills (order_id, price, size, fee_cents, mode) VALUES (%s, 0.5, 10, 0, 'paper') RETURNING *", (plain["id"],)
    ).fetchone()
    live = _live(client)
    chip = '[data-chip="ingame"].chip-ingame'

    def tagged(card: str, kind: str, row_id: Any) -> bool:
        row = live.card(card).row(kind, row_id)
        return row.has(chip) and row.one(chip).text == "in-game"

    assert tagged("open-orders", "order", ingame_open["id"]) and not tagged("open-orders", "order", pregame_open["id"])
    assert tagged("orders", "order", filled["id"]) and not tagged("orders", "order", plain["id"])
    assert tagged("fills", "fill", fill["id"]) and not tagged("fills", "fill", plain_fill["id"])


def test_ingame_rejection_reasons_read_in_words(client, conn):
    setup = trade_setup(conn)
    stale = _order(conn, setup, True, status="rejected", reject_reason="ingame_stale")
    lag = _order(conn, setup, True, status="rejected", reject_reason="ingame_lag_suspended")
    set_setting(conn, "ingame_max_bet_cents", 300)
    big = _order(conn, setup, True, status="rejected", reject_reason="max_bet")
    recent = _live(client).card("orders")
    assert "In-Game Stale: game state too old" in recent.row("order", stale["id"]).one(".reason").text
    assert "In-Game Lag Suspended: feed lag suspends in-game buys" in recent.row("order", lag["id"]).one(".reason").text
    assert "over max bet $5.00 > $3.00" in recent.row("order", big["id"]).one(".reason").text, \
        "an in-game order's cap is ingame_max_bet_cents"


def test_feed_block_without_events_says_not_enough_data(client, conn):
    live = _live(client)
    feed = live.card("ingame")
    assert feed.attr("id") == "ingame-feed" and feed.one(".disclosure-title").text == "In-game feed"
    assert feed.chip("feed-ok").text == "not suspended" and not feed.one("details.disclosure").is_open
    assert live.cards()[:2] == ["assignments", "ingame"], "the feed group follows the assignments"
    assert "not enough data" in feed.text and "no event measured yet" in feed.one(".disclosure-summary").text


def test_feed_block_per_source_lag_and_enough_events(client, conn):
    insert_game(conn)
    _lag(conn, 12, 6.0)
    _lag(conn, 3, 9.0, source="yahoo")
    feed = _live(client).card("ingame")
    assert feed.one('li[data-source="espn_summary"]').text == "ESPN: median 6 s behind the market over 12 events"
    assert feed.one('li[data-source="yahoo"]').text == "Yahoo: not enough data (3 of 5 events measured)"
    assert feed.chip("feed-ok").text == "not suspended" and feed.chip("feed-ok").has_class("chip-ok")
    assert "within 20 s" in feed.one(".feed-state").text
    assert "ESPN 6 s behind · Yahoo 3 of 5 events" in feed.one(".disclosure-summary").text, "the closed header reads the lag"


def test_feed_lines_follow_the_source_order(client, conn):
    insert_game(conn)
    _lag(conn, 6, 5.0, source="yahoo")
    _lag(conn, 6, 5.0, source="espn_scoreboard")
    _lag(conn, 6, 5.0)
    feed = _live(client).card("ingame")
    found = [li.attr("data-source") for li in feed.select("li[data-source]")]
    assert found == ["espn_summary", "espn_scoreboard", "yahoo"], "ESPN before its scoreboard fallback, then Yahoo"


def test_feed_block_shows_the_suspension(client, conn):
    insert_game(conn)
    _lag(conn, 12, 45.0)
    feed = _live(client).card("ingame")
    assert feed.has_class("ingame-feed") and feed.has_class("is-suspended")
    assert feed.chip("feed-suspended").text == "buys suspended" and feed.chip("feed-suspended").has_class("chip-bad")
    assert "median lag 45 s is over 20 s" in feed.text and "Sells stay allowed" in feed.text
    assert feed.one("details.disclosure").is_open, "a suspension opens the group"


def test_feed_ahead_of_the_market_reads_ahead(conn):
    from host.trading.views_ingame import lag_text

    assert lag_text("ESPN", {"n": 6, "median_lag_s": -4.2}, 5) == "ESPN: median 4 s ahead of the market over 6 events"


def test_new_assignment_form_has_the_ingame_select_and_box(client, conn):
    insert_game(conn)
    insert_market(conn)
    pregame = insert_model(conn, status="paper_ok", trained_through=[2025, 18])
    ingame = _ingame_model(conn)
    form = _assign_form(client)
    main = form.input("model_id")
    assert main.has(f'option[value="{pregame["id"]}"]') and not main.has(f'option[value="{ingame["id"]}"]'), \
        "an in-game model is not a pre-game model"
    select = form.input("ingame_model_id")
    assert select.one('option[value=""]').text == "no in-game model" and select.has(f'option[value="{ingame["id"]}"]')
    assert not select.has(f'option[value="{pregame["id"]}"]')
    marker = form.input("ingame_form")
    assert marker.attr("type") == "hidden" and marker.attr("value") == "1"
    box = form.input("trade_ingame")
    assert box.attr("type") == "checkbox" and not box.has_attr("checked"), "unticked by default (settings.trade_ingame false)"
    assert "Trade in-game (paper only)" in box.closest("label").text
    set_setting(conn, "trade_ingame", True)
    assert _assign_form(client).input("trade_ingame").has_attr("checked")


def test_post_assignment_passes_the_ingame_fields(client, conn, monkeypatch):
    calls: list[dict[str, Any]] = []
    real = assignments.create_assignment

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(assignments, "create_assignment", spy)
    insert_game(conn)
    insert_market(conn)
    model = insert_model(conn, status="paper_ok")
    ingame = _ingame_model(conn)
    form = {"game_id": GAME_ID, "model_id": str(model["id"]), "mode": "paper", "bankroll": "50", "max_bet": "",
            "ingame_model_id": str(ingame["id"]), "ingame_form": "1", "trade_ingame": "on"}
    r = client.post("/assignments", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    assert calls[-1] == {"ingame_model_id": str(ingame["id"]), "trade_ingame": True}
    other = insert_model(conn, status="paper_ok", params={"k": 30.0, "hfa": 50.0, "mov_scale": 0})
    form.update(model_id=str(other["id"]), ingame_model_id="")
    form.pop("trade_ingame")
    assert client.post("/assignments", data=form, follow_redirects=False).status_code == 303
    assert calls[-1] == {"trade_ingame": False}, "an unticked box is an explicit off, no model is no keyword"
    third = insert_model(conn, status="paper_ok", params={"k": 31.0, "hfa": 50.0, "mov_scale": 0})
    old_form = {"game_id": GAME_ID, "model_id": str(third["id"]), "mode": "paper", "bankroll": "50"}
    assert client.post("/assignments", data=old_form, follow_redirects=False).status_code == 303
    assert calls[-1] == {}, "a post without the in-game fields keeps the step 4 call"


def test_post_assignment_with_ingame_fields_reaches_the_row(client, conn):
    insert_game(conn)
    insert_market(conn)
    model = insert_model(conn, status="paper_ok")
    ingame = _ingame_model(conn)
    form = {"game_id": GAME_ID, "model_id": str(model["id"]), "mode": "paper", "bankroll": "50",
            "ingame_model_id": str(ingame["id"]), "ingame_form": "1", "trade_ingame": "on"}
    assert client.post("/assignments", data=form, follow_redirects=False).status_code == 303
    row = conn.execute("SELECT ingame_model_id, trade_ingame FROM assignments WHERE model_id = %s", (model["id"],)).fetchone()
    assert row["ingame_model_id"] == ingame["id"] and row["trade_ingame"] is True
    form.update(model_id=str(insert_model(conn, status="paper_ok", params={"k": 9.0})["id"]), ingame_model_id=str(model["id"]))
    r = client.post("/assignments", data=form)
    assert r.status_code == 400 and "the in-game model must be an ingame_wp model" in r.text, "a refusal re-renders the form"
    again = page(r.text).form("assign")
    assert "the in-game model must be an ingame_wp model" in again.one(".inline-error").text
    assert again.input("trade_ingame").has_attr("checked"), "the submitted box is kept"


def test_toggle_route_calls_set_ingame_and_flashes(client, conn, monkeypatch):
    setup = trade_setup(conn)
    model = _ingame_model(conn)
    calls: list[tuple[Any, ...]] = []

    def fake(c: Any, assignment_id: Any, actor: Any, changes: dict[str, Any]) -> dict[str, Any]:
        calls.append((str(assignment_id), changes["ingame_model_id"], changes["trade_ingame"], actor))
        return {"id": setup.assignment["id"], "ingame_model_id": changes["ingame_model_id"],
                "trade_ingame": changes["trade_ingame"], "orders_cancelled": 0 if changes["trade_ingame"] else 2}

    monkeypatch.setattr(assignments_ingame, "set_ingame", fake)
    aid = setup.assignment["id"]
    r = client.post(f"/assignments/{aid}/ingame", data={"ingame_model_id": str(model["id"]), "trade_ingame": "on"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/trading#assignments"
    assert calls[-1][:3] == (str(aid), str(model["id"]), True) and calls[-1][3]
    assert "in-game trading on" in flash_cookie(r)
    r = client.post(f"/assignments/{aid}/ingame", data={"ingame_model_id": ""}, follow_redirects=False)
    assert calls[-1][1:3] == (None, False)
    assert "in-game trading off, no in-game model, 2 open in-game orders cancelled" in flash_cookie(r)


def test_toggle_refusal_is_a_flash(client, conn, monkeypatch):
    from host.errors import BadRequest

    setup = trade_setup(conn)

    def refuse(*_: Any) -> dict[str, Any]:
        raise BadRequest("the in-game model must be an ingame_wp model")

    monkeypatch.setattr(assignments_ingame, "set_ingame", refuse)
    r = client.post(f"/assignments/{setup.assignment['id']}/ingame", data={"ingame_model_id": "x", "trade_ingame": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "in-game change refused: the in-game model must be an ingame_wp model" in flash_cookie(r)


def test_toggle_route_against_the_real_function(client, conn):
    setup = trade_setup(conn)
    model = _ingame_model(conn)
    aid = setup.assignment["id"]
    r = client.post(f"/assignments/{aid}/ingame", data={"ingame_model_id": str(model["id"]), "trade_ingame": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    row = conn.execute("SELECT ingame_model_id, trade_ingame FROM assignments WHERE id = %s", (aid,)).fetchone()
    assert row["ingame_model_id"] == model["id"] and row["trade_ingame"] is True
    assert "in-game trading on" in flash_cookie(r)
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'assignment_ingame'").fetchone()["n"] == 1
    r = client.post(f"/assignments/{aid}/ingame", data={"ingame_model_id": str(setup.model["id"]), "trade_ingame": "on"},
                    follow_redirects=False)
    assert "in-game change refused" in flash_cookie(r), "a pre-game model is not an in-game model"


def test_probe_game_state_renders_parsed_game_and_event(client, conn, monkeypatch):
    import host.exchange.gamestate as gamestate

    insert_game(conn)
    conn.execute("UPDATE games SET raw = %s WHERE game_id = %s", (json.dumps({"espn": "401547417"}), GAME_ID))
    payload = (FIXTURES / "espn_summary_in.json").read_text()
    urls: list[str] = []

    def fetch(url: str) -> tuple[int, str]:
        urls.append(url)
        return 200, payload

    monkeypatch.setattr(gamestate, "default_fetch", fetch)
    exchange = _live(client).card("exchange")
    form = exchange.one('[data-form="probe-gamestate"]')
    assert form.target == "/exchange/probe-gamestate" and form.attr("method") == "post"
    assert form.input("event").attr("inputmode") == "numeric" and "Probe game state" in form.text
    assert exchange.action("probe").target == "/exchange/probe" and "Probe markets" in exchange.text
    r = client.post("/exchange/probe-gamestate", data={"event": "401547417"})
    probe = page(r.text)
    assert r.status_code == 200 and probe.one("main > h1").text.startswith("Game-state probe") and probe.page_name == "probe"
    assert urls and urls[-1].endswith("event=401547417")
    assert probe.one("dd.c-event").text == "401547417"
    assert probe.one("dd.c-game").text == GAME_ID and probe.stat("probe-event").one(".stat-note").text == GAME_ID
    parsed = probe.one("pre#parsed").text
    assert "clock_seconds" in parsed and "home_score" in parsed, "the parser's states, pretty printed"
    assert probe.has("pre#payload")


def test_probe_game_state_unknown_event_and_bad_input(client, conn, monkeypatch):
    import host.exchange.gamestate as gamestate

    monkeypatch.setattr(gamestate, "default_fetch", lambda url: (404, "not found"))
    r = client.post("/exchange/probe-gamestate", data={"event": "999"})
    assert r.status_code == 200 and "no game has this ESPN event id" in r.text
    r = client.post("/exchange/probe-gamestate", data={"event": "401?x=1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/trading#exchange"
    assert "digits only" in flash_cookie(r)


def test_probe_event_suggestions_list_assigned_games(client, conn):
    setup = trade_setup(conn)
    conn.execute("UPDATE games SET raw = %s WHERE game_id = %s", (json.dumps({"espn": "401547999"}), GAME_ID))
    exchange = _live(client).card("exchange")
    assert exchange.one('datalist#probe-events option[value="401547999"]').text == "KC @ LV"
    assert exchange.one('[data-form="probe-gamestate"]').input("event").attr("list") == "probe-events"
    assert setup.assignment["id"]


def test_fragment_carries_the_ingame_parts(client, conn):
    setup = _kicked_off(conn)
    _set_ingame(conn, setup.assignment, _ingame_model(conn), True)
    _game_state(conn, age_s=1)
    frag = page(client.get("/fragments/trading").text)
    assert frag.card("ingame").attr("id") == "ingame-feed" and frag.has('[data-form="probe-gamestate"]')
    assert "Q3 4:12 · 17-14" in frag.row("assignment", setup.assignment["id"]).one(".row-ingame").text


def test_phone_rules_for_the_new_controls():
    """At 390 px the probe forms stack as full-width tap targets, the in-game Save is a
    tap target, and the in-game chip keeps its state colours (only an accent ring)."""
    phone = "max-width: 699.98px"
    assert "flex: 1 1 100%" in declarations(".probe-row > form", phone)
    assert "width: 100%" in declarations(".probe-row .btn", phone) and "width: 100%" in declarations(".probe-gamestate input", phone)
    assert "min-height: var(--tap)" in declarations(".ingame-form .btn")
    assert "width: 100%" in declarations(".ingame-form select")
    ring = declarations(".chip.chip-ingame")
    assert "border: 1px solid var(--accent)" in ring and "background" not in ring and "color" not in ring.replace("border", "")


def test_live_assignment_has_no_ingame_toggle(client, conn):
    setup = trade_setup(conn, mode="live", model_status="live_eligible")
    row = _assignment(client, setup.assignment)
    assert "In-game orders are paper only" in row.one(".menu .ingame-note").text
    assert not row.has('[data-form="ingame-toggle"]') and not row.has('form[action$="/ingame"]')


def test_assign_button_of_an_ingame_model_preselects_the_ingame_select(client, conn):
    insert_game(conn)
    insert_market(conn)
    ingame = _ingame_model(conn)
    doc = page(client.get(f"/trading?model={ingame['id']}").text)
    assert doc.card("assign").one("details.disclosure").is_open
    assert doc.form("assign").input("ingame_model_id").one(f'option[value="{ingame["id"]}"]').has_attr("selected")


def test_ingame_model_page_lists_the_assignments_trading_it_in_game(client, conn):
    setup = trade_setup(conn)
    model = _ingame_model(conn)
    doc = page(client.get(f"/models/{model['id']}").text)
    assert not doc.has('[data-row="assignment"]')
    _set_ingame(conn, setup.assignment, model, True)
    doc = page(client.get(f"/models/{model['id']}").text)
    row = doc.card("assignments").row("assignment", setup.assignment["id"])
    assert "KC @ LV" in row.text and "in-game" in row.one(".row-meta").text, "named as the assignment's in-game model"
    pre = page(client.get(f"/models/{setup.model['id']}").text).card("assignments").row("assignment", setup.assignment["id"])
    assert "in-game" not in pre.one(".row-meta").text, "the pre-game lineage keeps its own row unmarked"
