"""The /trading page for in-game trading (docs/DASHBOARD.md "Trading page", step 6C
contract section 12): per assignment the live score and clock with the state age or
"state stale", the in-game model probability next to the home mid, an `in-game` chip
on in-game orders and fills, the New assignment form's in-game select and box, the
per-assignment in-game toggle, the "In-game feed" block and the "Probe game state"
button. Rows are inserted straight into the 0009 tables, so these tests do not depend
on the feed poller or the in-game approval path. The two calls into the trading side
(create_assignment with the in-game fields, assignments_ingame.set_ingame for the
toggle) are checked by their arguments and against the real functions."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import psycopg

from fleet.models.ingame_wp import FEATURE_NAMES, IngameWP
from fleet.sim.odds import devig
from host.trading import assignments, assignments_ingame
from tests.conftest import GAME_ID, flash_cookie, insert_game, insert_market, insert_model, set_setting, trade_setup

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


def _live(client: Any) -> str:
    return client.get("/trading").text.split('id="trading-live"')[1]


def _row(html: str, attr: str, value: Any) -> str:
    return re.search(rf'<tr[^>]*{attr}="{value}"[^>]*>.*?</tr>', html, re.S).group(0)


def _section(html: str, ident: str) -> str:
    return re.search(rf'<(section|div)[^>]*id="{ident}"[^>]*>.*?</\1>', html, re.S).group(0)


def _kicked_off(conn: psycopg.Connection) -> Any:
    setup = trade_setup(conn)
    conn.execute("UPDATE games SET kickoff_at = now() - interval '100 minutes' WHERE game_id = %s", (GAME_ID,))
    return setup


def test_assignment_row_shows_score_clock_age_and_model_p_next_to_the_mid(client, conn):
    setup = _kicked_off(conn)
    model = _ingame_model(conn)
    _set_ingame(conn, setup.assignment, model, True)
    _game_state(conn, age_s=3)
    row = _row(_live(client), "data-assignment", setup.assignment["id"])
    cell = re.search(r'<td class="wide c-ingame">.*?</td>', row, re.S).group(0)
    assert "Q3 4:12 · 17-14 · 3 s ago" in cell, "away-home score, quarter clock and the state age on one line"
    assert '<span class="chip chip-ingame">in-game on</span>' in cell
    assert "state stale" not in cell
    p = IngameWP.from_json(model["params"], model["artifact"]).predict(STATE, devig(-150, 130))
    assert f'data-p-home="{p:.3f}"' in cell and f"model LV {p:.2f}" in cell, "the in-game p of the home team"
    assert "mid 0.51" in cell, "the home market mid (bid 0.50, ask 0.52) sits next to it"


def test_stale_state_reads_state_stale_and_missing_state_says_so(client, conn):
    setup = _kicked_off(conn)
    _set_ingame(conn, setup.assignment, _ingame_model(conn), True)
    cell = _row(_live(client), "data-assignment", setup.assignment["id"])
    assert "no game state yet" in cell
    set_setting(conn, "ingame_max_state_age_s", 30)
    _game_state(conn, age_s=120, status="half", period=2, clock_seconds=0)
    cell = _row(_live(client), "data-assignment", setup.assignment["id"])
    assert '<span class="stale-age state-stale">state stale</span>' in cell
    assert "Half · 17-14 · 2 min ago" in cell, "the stale line keeps the last state and its age, muted"
    assert '<span class="ingame-p muted small"' in cell and "(from the stale state, not traded)" in cell, \
        "a probability from a stale state is muted and marked, not shown as current"
    _game_state(conn, age_s=90, status="final", period=4, clock_seconds=0, home_score=20)
    cell = _row(_live(client), "data-assignment", setup.assignment["id"])
    assert "state stale" not in cell and "Final · 17-20 · 1 min ago" in cell, "a final state never goes stale"


def test_period_labels_for_overtime_end_of_period_and_final(conn):
    from host.trading.views_ingame import clock_label

    assert clock_label({**STATE, "period": 5, "clock_seconds": 61}) == "OT 1:01 · 17-14"
    assert clock_label({**STATE, "status": "end_period", "period": 1}) == "End Q1 · 17-14"
    assert clock_label({**STATE, "status": "final", "home_score": 20}) == "Final · 17-20"
    assert clock_label({"status": "pre", "home_score": None, "away_score": None}) == "Pre-game"


def test_assignment_without_ingame_shows_off_and_a_toggle_form(client, conn):
    setup = trade_setup(conn)
    model = _ingame_model(conn)
    row = _row(_live(client), "data-assignment", setup.assignment["id"])
    assert '<span class="muted small">off</span>' in row and "chip-ingame" not in row
    form = re.search(rf'<form method="post" action="/assignments/{setup.assignment["id"]}/ingame" class="ingame-form">.*?</form>', row, re.S)
    assert form, "an active assignment carries the in-game toggle form"
    assert f'<option value="{model["id"]}">' in form.group(0) and 'name="trade_ingame"' in form.group(0)
    assert 'checked' not in form.group(0)
    conn.execute("UPDATE games SET status = 'final' WHERE game_id = %s", (GAME_ID,))
    row = _row(_live(client), "data-assignment", setup.assignment["id"])
    assert "ingame-form" not in row, "no toggle once the game is final"


def test_toggle_form_preselects_the_current_model_and_box(client, conn):
    setup = trade_setup(conn)
    model = _ingame_model(conn)
    _set_ingame(conn, setup.assignment, model, True)
    row = _row(_live(client), "data-assignment", setup.assignment["id"])
    assert f'<option value="{model["id"]}" selected>' in row
    assert '<input type="checkbox" name="trade_ingame" checked>' in row
    assert '<span class="chip chip-ingame">in-game on</span>' in row


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
    chip = '<span class="chip chip-ingame">in-game</span>'
    open_orders = _section(live, "open-orders")
    assert chip in _row(open_orders, "data-order", ingame_open["id"])
    assert chip not in _row(open_orders, "data-order", pregame_open["id"])
    recent = _section(live, "orders")
    assert chip in _row(recent, "data-order", filled["id"]) and chip not in _row(recent, "data-order", plain["id"])
    fills = _section(live, "fills")
    assert chip in _row(fills, "data-fill", fill["id"]) and chip not in _row(fills, "data-fill", plain_fill["id"])


def test_ingame_rejection_reasons_read_in_words(client, conn):
    setup = trade_setup(conn)
    stale = _order(conn, setup, True, status="rejected", reject_reason="ingame_stale")
    lag = _order(conn, setup, True, status="rejected", reject_reason="ingame_lag_suspended")
    set_setting(conn, "ingame_max_bet_cents", 300)
    big = _order(conn, setup, True, status="rejected", reject_reason="max_bet")
    recent = _section(_live(client), "orders")
    assert "ingame_stale: game state too old" in _row(recent, "data-order", stale["id"])
    assert "ingame_lag_suspended: feed lag suspends in-game buys" in _row(recent, "data-order", lag["id"])
    assert "over max bet $5.00 &gt; $3.00" in _row(recent, "data-order", big["id"]), "an in-game order's cap is ingame_max_bet_cents"


def test_feed_block_without_events_says_not_enough_data(client, conn):
    feed = _section(_live(client), "ingame-feed")
    assert "<h3>In-game feed" in feed and "not suspended" in feed
    assert 'id="ingame-feed"' in _section(_live(client), "exchange"), "the feed block sits in the exchange block"
    assert "not enough data" in feed


def test_feed_block_per_source_lag_and_enough_events(client, conn):
    insert_game(conn)
    _lag(conn, 12, 6.0)
    _lag(conn, 3, 9.0, source="yahoo")
    feed = _section(_live(client), "ingame-feed")
    assert '<li data-source="espn_summary">ESPN: median 6 s behind the market over 12 events</li>' in feed
    assert '<li data-source="yahoo">Yahoo: not enough data (3 of 5 events measured)</li>' in feed
    assert '<span class="chip chip-ok">not suspended</span>' in feed
    assert "within 20 s" in feed


def test_feed_lines_follow_the_source_order(client, conn):
    insert_game(conn)
    _lag(conn, 6, 5.0, source="yahoo")
    _lag(conn, 6, 5.0, source="espn_scoreboard")
    _lag(conn, 6, 5.0)
    feed = _section(_live(client), "ingame-feed")
    found = re.findall(r'<li data-source="([a-z_]+)">', feed)
    assert found == ["espn_summary", "espn_scoreboard", "yahoo"], "ESPN before its scoreboard fallback, then Yahoo"


def test_feed_block_shows_the_suspension(client, conn):
    insert_game(conn)
    _lag(conn, 12, 45.0)
    feed = _section(_live(client), "ingame-feed")
    assert 'class="ingame-feed is-suspended"' in feed
    assert '<span class="chip chip-bad">buys suspended</span>' in feed
    assert "median lag 45 s is over 20 s" in feed and "Sells stay allowed" in feed


def test_feed_ahead_of_the_market_reads_ahead(conn):
    from host.trading.views_ingame import lag_text

    assert lag_text("ESPN", {"n": 6, "median_lag_s": -4.2}, 5) == "ESPN: median 4 s ahead of the market over 6 events"


def test_new_assignment_form_has_the_ingame_select_and_box(client, conn):
    insert_game(conn)
    insert_market(conn)
    pregame = insert_model(conn, status="paper_ok", trained_through=[2025, 18])
    ingame = _ingame_model(conn)
    html = client.get("/trading").text
    form = html.split('id="assign"')[1].split('id="trading-live"')[0]
    main = re.search(r'<select name="model_id">.*?</select>', form, re.S).group(0)
    assert str(pregame["id"]) in main and str(ingame["id"]) not in main, "an in-game model is not a pre-game model"
    select = re.search(r'<select name="ingame_model_id">.*?</select>', form, re.S).group(0)
    assert '<option value="">no in-game model</option>' in select and f'<option value="{ingame["id"]}">' in select
    assert str(pregame["id"]) not in select
    assert '<input type="hidden" name="ingame_form" value="1">' in form
    assert '<input type="checkbox" name="trade_ingame">' in form, "unticked by default (settings.trade_ingame false)"
    set_setting(conn, "trade_ingame", True)
    html = client.get("/trading").text
    assert '<input type="checkbox" name="trade_ingame" checked>' in html.split('id="trading-live"')[0]


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
    assert '<input type="checkbox" name="trade_ingame" checked>' in r.text, "the submitted box is kept"


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
    live = _live(client)
    exchange = _section(live, "exchange")
    assert '<form method="post" action="/exchange/probe-gamestate" class="probe-gamestate">' in exchange
    assert 'name="event"' in exchange and "Probe game state" in exchange and "Probe markets" in exchange
    r = client.post("/exchange/probe-gamestate", data={"event": "401547417"})
    assert r.status_code == 200 and "Game-state probe" in r.text
    assert urls and urls[-1].endswith("event=401547417")
    assert '<dd class="c-event"><code>401547417</code></dd>' in r.text
    assert f'<dd class="c-game">{GAME_ID}</dd>' in r.text
    parsed = re.search(r'<pre id="parsed">(.*?)</pre>', r.text, re.S).group(1)
    assert "clock_seconds" in parsed and "home_score" in parsed, "the parser's states, pretty printed"
    assert '<pre id="payload">' in r.text


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
    exchange = _section(_live(client), "exchange")
    assert '<option value="401547999">KC @ LV</option>' in exchange
    assert setup.assignment["id"]


def test_fragment_carries_the_ingame_parts(client, conn):
    setup = _kicked_off(conn)
    _set_ingame(conn, setup.assignment, _ingame_model(conn), True)
    _game_state(conn, age_s=1)
    html = client.get("/fragments/trading").text
    assert 'id="ingame-feed"' in html and "Q3 4:12 · 17-14" in html and "/exchange/probe-gamestate" in html


def test_phone_rules_for_the_new_controls():
    css = (Path(__file__).parents[1] / "host" / "static" / "style.css").read_text()
    block = css.split("/* step 6C: in-game.")[1]
    phone = css.split("@media (max-width: 700px)")[-1].split("/* step 6C:")[1]
    assert "@media" not in block.split("@media (max-width: 700px)")[1], "the 6C phone rules sit in the last phone block"
    assert ".ingame-form .btn { flex: 1; min-height: var(--tap); }" in phone
    assert ".probe-gamestate .btn { width: 100%; min-height: var(--tap); }" in phone
    assert "table.assignments td.c-ingame { min-width: 0; }" in phone, "no fixed width that could scroll sideways at 390 px"
    assert "\u2014" not in block


def test_live_assignment_has_no_ingame_toggle(client, conn):
    setup = trade_setup(conn, mode="live", model_status="live_eligible")
    row = _row(_live(client), "data-assignment", setup.assignment["id"])
    assert "in-game orders are paper only" in row and "ingame-form" not in row


def test_assign_button_of_an_ingame_model_preselects_the_ingame_select(client, conn):
    insert_game(conn)
    insert_market(conn)
    ingame = _ingame_model(conn)
    html = client.get(f"/trading?model={ingame['id']}").text
    form = html.split('id="assign"')[1].split('id="trading-live"')[0]
    assert '<details class="card send assign" id="assign" open>' in html
    assert f'<option value="{ingame["id"]}" selected>' in re.search(r'<select name="ingame_model_id">.*?</select>', form, re.S).group(0)
