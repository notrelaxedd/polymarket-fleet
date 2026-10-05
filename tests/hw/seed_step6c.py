"""Step 6 Part C rows for the screenshot database (docs/INGAME.md, contract sections
7-12): two ingame_wp lineages (one paper_ok, beating vegas_wp on 41,812 held-out
plays, one candidate that does not), a game in its third quarter (DEN @ LAC, ESPN
event 401800077) with a fresh game state, an in-game paper assignment on it holding a
partly filled in-game buy, feed_lag rows (12 measured ESPN summary events, median
6 s behind the market; 3 ESPN scoreboard events, "not enough data"), and yesterday's
SF @ LA game settled with a pre-game bet and two in-game bets (a win, a loss), so the ingame_wp lineage has
a paper record. Used by tests/hw/screenshots.py after the step 6B rows.

The live game's states are what host.exchange.gamestate.parse_summary extracts from
tests/fixtures/espn_summary_in.json with the teams renamed (`espn_payload`); the
probe page gets the same payload from `stub_espn`, so no capture reaches ESPN. Orders
go through host.trading.limits.approve_order (the in-game checks included), fills
through orders.record_fill, the settled game through settle_game. `check_step6c`
asserts the pages show all of it (through the step 7 data hooks); `probe` opens the
Exchange group and submits the "Probe game state" form.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fleet.models.ingame_wp import FEATURE_NAMES, IngameWP
from fleet.sim.odds import devig
from host import eligibility
from host.exchange import gamestate
from host.exchange.settle import settle_game
from host.trading import orders
from host.trading.assignments import create_assignment
from host.trading.limits import approve_order, fee_per_contract
from host.trading.sells import sell_fee_cents
from tests.conftest import insert_game, insert_market, insert_model, insert_snapshot, lease_trade_job, worker_row
from tests.pagecheck import page as parse

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "espn_summary_in.json"
LIVE_GAME, LIVE_EVENT, PAST_GAME = "2026_05_DEN_LAC", "401800077", "2026_04_SF_LA"
COEF = [0.02, 0.15, 0.4, 0.6, 0.45, -0.08, -0.015, 0.06, 0.0, 0.0]
PARAMS = {"l2": 0.8, "time_scale": 1.1, "fp_scale": 0.9}
FEE = {"taker_rate": 0.05}
RENAMES = [("Kansas City Chiefs", "Denver Broncos"), ("Kansas City", "Denver"), ("Chiefs", "Broncos"),
           ("Las Vegas Raiders", "Los Angeles Chargers"), ("Las Vegas", "Los Angeles"), ("Raiders", "Chargers"),
           ("Allegiant Stadium", "SoFi Stadium"), ("P.Mahomes", "B.Nix"), ("X.Worthy", "C.Sutton"), ("T.Kelce", "E.Engram"),
           ("I.Pacheco", "J.Dobbins"), ("A.Cole", "J.Scott"), ("R.Rice", "M.Mims"), ("M.Koonce", "K.Mack"),
           ("R.Spillane", "D.Perryman"), ("J.Hlavaty", "J.Cardona"), ("A.O'Connell", "J.Herbert"), ("D.Adams", "L.McConkey"),
           ("e31837", "fb4f14"), ("401800001", LIVE_EVENT)]
# (feed_lag event key, kind, seconds ago, ESPN summary lag s); the last three also reached the scoreboard.
EVENTS = [("possession:1:home:0-0:0", "possession", 5900, 5.1), ("score:0-7", "score", 5500, 7.4),
          ("possession:1:home:0-7:0", "possession", 5450, 4.2), ("score:3-7", "score", 5000, 6.0),
          ("possession:2:away:3-7:0", "possession", 4600, 5.6), ("score:3-14", "score", 4100, 8.9),
          ("score:10-14", "score", 3500, 6.3), ("score:10-17", "score", 2900, 4.8),
          ("possession:2:home:10-17:0", "possession", 2700, 5.9), ("score:17-17", "score", 1300, 6.6),
          ("score:17-20", "score", 700, 9.4), ("possession:3:away:17-20:0", "possession", 140, 6.1)]
SCOREBOARD_LAGS = (14.2, 17.5, 15.1)


def espn_payload() -> str:
    """The in-progress ESPN summary fixture, renamed to DEN @ LAC and event LIVE_EVENT."""
    text = FIXTURE.read_text()
    for old, new in RENAMES:
        text = text.replace(old, new)
    return re.sub(r"\bLV\b", "LAC", re.sub(r"\bKC\b", "DEN", text))


def stub_espn() -> None:
    """Serve espn_payload() to the probe (same process as the test server) for LIVE_EVENT; 404 otherwise."""
    payload = espn_payload()
    gamestate.default_fetch = lambda url: (200, payload) if f"event={LIVE_EVENT}" in url else (404, '{"code": 404}')


def validation(n_plays: int, log_loss: float, vegas: float) -> dict[str, Any]:
    """A held-out validation dict of the fleet/sim/ingame_eval.py shape, varied per group."""
    def group(i: int, base: float, vbase: float) -> dict[str, Any]:
        return {"n_plays": n_plays // 5 + 37 * i, "log_loss": round(base + 0.004 * i, 4), "vegas_log_loss": round(vbase + 0.003 * i, 4)}
    periods = {k: group(i, log_loss - 0.12 + 0.05 * i, vegas - 0.12 + 0.05 * i) for i, k in enumerate(("1", "2", "3", "4"))}
    periods["5"] = {"n_plays": n_plays // 90, "log_loss": round(log_loss + 0.14, 4), "vegas_log_loss": round(vegas + 0.12, 4)}
    buckets = {k: group(i, log_loss + (0.02 if k == "0" else -0.04), vegas + (0.02 if k == "0" else -0.04))
               for i, k in enumerate(("<=-9", "-8..-1", "0", "1..8", ">=9"))}
    calibration = [{"count": n_plays // 10 + 13 * i, "mean_p": round((i + 0.5) / 10, 3),
                    "mean_outcome": round((i + 0.5) / 10 + (0.012 if i % 2 else -0.009), 3),
                    "vegas_mean_p": round((i + 0.5) / 10 + 0.004, 3)} for i in range(10)]
    return {"n_plays": n_plays, "log_loss": log_loss, "vegas_log_loss": vegas, "beats_baseline": log_loss <= vegas,
            "brier": round(log_loss / 3.05, 4), "vegas_brier": round(vegas / 3.05, 4), "seasons": [2022, 2023, 2024, 2025],
            "n_skipped_no_vegas": 41, "by_period": periods, "by_score_bucket": buckets, "calibration": calibration,
            "era": "validation"}


def _ingame_model(conn: psycopg.Connection, params: dict[str, Any], val: dict[str, Any], coef: list[float]) -> dict[str, Any]:
    artifact = {"coef": coef, "features": list(FEATURE_NAMES), "n_train": 318_204, "train_seasons": [2012, 2021]}
    model = insert_model(conn, "ingame_wp", params, {"era": "search", "log_loss": round(val["log_loss"] - 0.006, 4),
                                                   "seasons": list(range(2012, 2022))},
                         summary=IngameWP.summary(params, val), artifact=artifact, validation=val)
    eligibility.recompute_lineage(conn, model["lineage_id"])
    return conn.execute("SELECT * FROM models WHERE id = %s", (model["id"],)).fetchone()


def _state(conn: psycopg.Connection, game_id: str, state: dict[str, Any], ago: float, play: dict[str, Any] | None = None) -> None:
    play = play or {}
    conn.execute(
        """
        INSERT INTO game_state (game_id, ts, source, event_ts, status, period, clock_seconds, home_score, away_score,
                                possession, down, distance, yardline_100, home_timeouts, away_timeouts, play_id, play_text)
        VALUES (%s, now() - make_interval(secs => %s), 'espn_summary', now() - make_interval(secs => %s + 1), %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (game_id, ago, ago, state["status"], state["period"], state["clock_seconds"], state["home_score"], state["away_score"],
         state["possession"], state["down"], state["distance"], state["yardline_100"], state["home_timeouts"],
         state["away_timeouts"], play.get("play_id"), play.get("play_text")),
    )


def _ask(conn: psycopg.Connection, trader_id: str, job: dict[str, Any], assignment: dict[str, Any], market: dict[str, Any],
         snap: dict[str, Any], price: float, size: int, my_p: float, ingame: bool) -> dict[str, Any]:
    """One buy through the real approval (in-game checks when `ingame`), then opened."""
    edge = round(my_p - price - fee_per_contract(price, FEE), 3)
    body = {"client_request_id": f"{'ingame-' if ingame else ''}{uuid.uuid4().hex}", "job_id": str(job["id"]),
            "lease_token": str(job["lease_token"]), "assignment_id": str(assignment["id"]), "market_id": str(market["id"]),
            "snapshot_id": int(snap["id"]), "price": price, "size": size, "my_p": round(my_p, 3), "market_p": float(snap["mid"]),
            "edge": edge, "order_side": "buy",
            "rationale": f"{'in-game ' if ingame else ''}my {my_p:.2f} vs ask {price:.2f}, fee {fee_per_contract(price, FEE):.3f}, edge {edge:.3f}"}
    if ingame:
        body.update(ingame=True, gtd_seconds=60)
    decision = approve_order(conn, worker_row(conn, trader_id), body)
    assert decision["status"] == "approved", f"the {'in-game' if ingame else 'pre-game'} buy was rejected: {decision}"
    return orders.set_status(conn, decision["order_id"], "open", "exchange", expected=("approved",), submitted_at=datetime.now(timezone.utc))


def _fill(conn: psycopg.Connection, order: dict[str, Any], price: float, size: int, snap: dict[str, Any]) -> None:
    orders.record_fill(conn, order["id"], price, size, sell_fee_cents(price, size, FEE), "paper", "exchange", snapshot_id=snap["id"])


def _game(conn: psycopg.Connection, game_id: str, home: str, away: str, week: int, titles: tuple[str, str]) -> list[dict[str, Any]]:
    insert_game(conn, game_id, home=home, away=away, kickoff_in_s=3600, week=week)
    markets = [insert_market(conn, game_id, side=side) for side in ("home", "away")]
    for market, title in zip(markets, titles):
        conn.execute("UPDATE markets SET title = %s WHERE id = %s", (title, market["id"]))
    return markets


def _assign(conn: psycopg.Connection, trader_id: str, game_id: str, model_id: Any, ingame_id: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    a = create_assignment(conn, game_id, model_id, "paper", 10_000, "owner@example.com", ingame_model_id=ingame_id, trade_ingame=True)
    return a, lease_trade_job(conn, SimpleNamespace(id=trader_id), a)


def _feed_lag(conn: psycopg.Connection) -> None:
    for i, (key, kind, ago, lag) in enumerate(EVENTS):
        sources = [("espn_summary", 0, lag)]
        if i >= len(EVENTS) - len(SCOREBOARD_LAGS):
            sources.append(("espn_scoreboard", 9, SCOREBOARD_LAGS[i - len(EVENTS) + len(SCOREBOARD_LAGS)]))
        for source, later, measured in sources:
            conn.execute(
                """
                INSERT INTO feed_lag (game_id, event_kind, event_key, event_ts, source, feed_seen_at, market_moved_at, lag_s)
                VALUES (%s, %s, %s, now() - make_interval(secs => %s + 2), %s, now() - make_interval(secs => %s),
                        now() - make_interval(secs => %s), %s)
                """,
                (LIVE_GAME, kind, key, ago, source, ago - later, ago - later + measured, measured),
            )


def seed_ingame(url: str, trader_id: str, model_id: str) -> dict[str, str]:
    """Everything the step 6C captures show (the module docstring lists it); returns their ids."""
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        good = _ingame_model(conn, PARAMS, validation(41_812, 0.4473, 0.4519), COEF)
        weak = _ingame_model(conn, {"l2": 6.2, "time_scale": 0.6, "fp_scale": 1.8}, validation(41_812, 0.4611, 0.4519),
                             [0.0, 0.09, 0.5, 0.4, 0.3, -0.05, -0.01, 0.03, 0.0, 0.0])
        wp = IngameWP.from_json(PARAMS, good["artifact"])
        # Yesterday's SF @ LA: a pre-game buy, in-game buys in Q2 (LA) and Q4 (SF), LA wins 27-24, settled.
        la, sf = _game(conn, PAST_GAME, "LA", "SF", 4, ("Rams beat 49ers (Week 4)", "49ers beat Rams (Week 4)"))
        conn.execute("UPDATE games SET home_moneyline = -120, away_moneyline = 100 WHERE game_id = %s", (PAST_GAME,))
        past, past_job = _assign(conn, trader_id, PAST_GAME, model_id, good["id"])
        snap = insert_snapshot(conn, la["id"], bid=0.52, ask=0.54, liquidity_usd_cents=260_000)
        pre = _ask(conn, trader_id, past_job, past, la, snap, 0.54, 20, 0.60, ingame=False)
        _fill(conn, pre, 0.54, 20, snap)
        conn.execute("UPDATE games SET kickoff_at = now() - interval '40 minutes' WHERE game_id = %s", (PAST_GAME,))
        q2 = {"status": "in", "period": 2, "clock_seconds": 400, "home_score": 10, "away_score": 7, "possession": "home",
              "down": 1, "distance": 10, "yardline_100": 45, "home_timeouts": 3, "away_timeouts": 3}
        _state(conn, PAST_GAME, q2, 2)
        snap = insert_snapshot(conn, la["id"], bid=0.64, ask=0.66, liquidity_usd_cents=240_000)
        inplay = _ask(conn, trader_id, past_job, past, la, snap, 0.66, 7, wp.predict(q2, devig(-120, 100)), ingame=True)
        _fill(conn, inplay, 0.66, 7, snap)
        q4 = {**q2, "period": 4, "clock_seconds": 430, "home_score": 20, "away_score": 24, "possession": "away", "yardline_100": 62}
        _state(conn, PAST_GAME, q4, 1)
        p_sf = 1.0 - wp.predict(q4, devig(-120, 100))
        ask = round(p_sf - 0.07, 2)
        snap = insert_snapshot(conn, sf["id"], bid=round(ask - 0.02, 2), ask=ask, liquidity_usd_cents=250_000)
        late = _ask(conn, trader_id, past_job, past, sf, snap, ask, int(4.5 // ask), p_sf, ingame=True)
        _fill(conn, late, ask, int(4.5 // ask), snap)
        conn.execute("UPDATE games SET status = 'final', home_score = 27, away_score = 24 WHERE game_id = %s", (PAST_GAME,))
        final = {**q4, "status": "final", "period": 4, "clock_seconds": 0, "home_score": 27, "away_score": 24, "possession": None,
                 "down": None, "distance": None, "yardline_100": None}
        _state(conn, PAST_GAME, final, 0)
        settle_game(conn, PAST_GAME, "scores")
        day = "interval '26 hours'"
        conn.execute(f"UPDATE orders SET created_at = created_at - {day} WHERE assignment_id = %s", (past["id"],))
        conn.execute(f"UPDATE fills SET ts = ts - {day} WHERE order_id IN (SELECT id FROM orders WHERE assignment_id = %s)", (past["id"],))
        conn.execute(f"UPDATE game_state SET ts = ts - {day}, event_ts = event_ts - {day} WHERE game_id = %s", (PAST_GAME,))
        conn.execute(f"UPDATE price_snapshots SET ts = ts - {day} WHERE market_id IN (SELECT id FROM markets WHERE game_id = %s)",
                     (PAST_GAME,))
        conn.execute(f"UPDATE markets SET last_snapshot_at = last_snapshot_at - {day} WHERE game_id = %s", (PAST_GAME,))
        conn.execute("UPDATE games SET kickoff_at = now() - interval '27 hours' WHERE game_id = %s", (PAST_GAME,))
        # Tonight's DEN @ LAC in the third quarter: the parsed fixture plays, the feed events, an in-game buy.
        home, away = _game(conn, LIVE_GAME, "LAC", "DEN", 5, ("Chargers beat Broncos (Week 5)", "Broncos beat Chargers (Week 5)"))
        conn.execute("UPDATE games SET raw = raw || %s WHERE game_id = %s", (Jsonb({"espn": LIVE_EVENT}), LIVE_GAME))
        live, live_job = _assign(conn, trader_id, LIVE_GAME, model_id, good["id"])
        conn.execute("UPDATE games SET kickoff_at = now() - interval '100 minutes' WHERE game_id = %s", (LIVE_GAME,))
        _feed_lag(conn)
        states = gamestate.parse_summary(espn_payload())
        assert len(states) >= 2 and states[-1]["play_id"] is None, "the fixture parses into plays plus the situation"
        last = states[-2]["event_ts"]
        for play in states[:-1]:
            _state(conn, LIVE_GAME, play, 8 + (last - play["event_ts"]).total_seconds(), play)
        _state(conn, LIVE_GAME, states[-1], 3)
        insert_snapshot(conn, away["id"], bid=0.75, ask=0.77, liquidity_usd_cents=210_000, age_s=2)
        snap = insert_snapshot(conn, home["id"], bid=0.23, ask=0.25, liquidity_usd_cents=230_000)
        bought = _ask(conn, trader_id, live_job, live, home, snap, 0.25, 16, wp.predict(states[-1], devig(-150, 130)), ingame=True)
        _fill(conn, bought, 0.25, 10, snap)
        conn.execute("UPDATE orders SET created_at = now() - interval '45 seconds' WHERE id = %s", (bought["id"],))
        conn.execute("UPDATE fills SET ts = now() - interval '41 seconds' WHERE order_id = %s", (bought["id"],))
        return {"ingame_model": str(good["id"]), "ingame_weak": str(weak["id"]), "live_assignment": str(live["id"]),
                "ingame_order": str(bought["id"]), "past_assignment": str(past["id"])}


def touch_ingame(conn: psycopg.Connection) -> None:
    """Keep the live game's situation 3 s old and its books fresh while captures run."""
    conn.execute("UPDATE game_state SET ts = now() - interval '3 seconds' WHERE id = (SELECT max(id) FROM game_state WHERE game_id = %s)",
                 (LIVE_GAME,))
    conn.execute("UPDATE markets SET last_snapshot_at = now() - interval '2 seconds' WHERE game_id = %s", (LIVE_GAME,))
    conn.execute("UPDATE price_snapshots SET ts = now() - interval '2 seconds' WHERE id IN"
                 " (SELECT max(id) FROM price_snapshots WHERE market_id IN (SELECT id FROM markets WHERE game_id = %s) GROUP BY market_id)",
                 (LIVE_GAME,))


def probe(page: Any) -> None:
    """Open the Trading page's Exchange group and submit its "Probe game state" form for
    the live game's event (the page is already on /trading)."""
    page.click('details[data-key="trading-exchange"] > summary')
    form = page.locator('[data-form="probe-gamestate"]')
    form.locator("input[name=event]").fill(LIVE_EVENT)
    with page.expect_navigation(wait_until="networkidle"):
        form.locator("button").click()


def check_step6c(server_url: str, ids: dict[str, str]) -> None:
    """The pages show what the step 6C captures are for (fails loudly when a seed drifts)."""
    with httpx.Client(base_url=server_url, trust_env=False, headers={"Origin": server_url}) as client:
        trading = parse(client.get("/trading").text)
        live = trading.row("assignment", ids["live_assignment"])
        line = live.one(".row-ingame")
        assert line.chip("ingame-on").text == "in-game on" and "Q3 8:32 · 20-17 · " in line.text and line.has("[data-p-home]"), line.text
        assert not line.has('[data-chip="state-stale"]') and live.has('[data-form="ingame-toggle"]')
        assert trading.card("orders").row("order", ids["ingame_order"]).chip("ingame").text == "in-game"
        assert trading.card("fills").has('[data-chip="ingame"]')
        feed = trading.card("ingame")
        assert feed.one('li[data-source="espn_summary"]').text == "ESPN: median 6 s behind the market over 12 events"
        assert feed.one('li[data-source="espn_scoreboard"]').text == "ESPN scoreboard: not enough data (3 of 5 events measured)"
        assert feed.chip("feed-ok").text == "not suspended"
        exchange = trading.card("exchange")
        assert exchange.has('[data-form="probe-gamestate"]')
        assert exchange.one(f'datalist#probe-events option[value="{LIVE_EVENT}"]').text == "DEN @ LAC"
        past = trading.row("assignment", ids["past_assignment"]).one(".row-ingame")
        assert "Final · 24-27 · 1 d ago" in past.text and not past.has('[data-chip="state-stale"]'), "the settled game ends on its final state"
        settings = parse(client.get("/settings").text)
        assert settings.form("ingame").target == "/settings/ingame"
        for key in ("trade_ingame", "ingame_max_lag_s", "ingame_lag_min_events", "gamestate_poll_s", "espn_summary_url", "yahoo_poll_s"):
            assert settings.field(key).has(f'[name="{key}"]'), key
        models = parse(client.get("/models").text)
        group = models.one("#ingame")
        assert len(group.rows("ingame-model")) >= 2, "both ingame_wp lineages in the In-game models group"
        assert group.row("ingame-model", ids["ingame_model"]).one(".row-ingame").text.startswith("in-game paper 2 bets · ")
        assert not models.has('[data-row="model"] .row-ingame'), "in-game bets belong to the ingame_wp lineage"
        model = parse(client.get(f"/models/{ids['ingame_model']}").text)
        assert model.has("#ingame-validation") and model.has('[data-chip="beats-vegas"]') and model.action("assign-ingame")
        assert model.prop("in-game paper record").startswith("1 game · 2 bets"), "plurals"
        assert model.prop("fitted on").startswith("train seasons 2012-2021"), "an in-game model is fitted by the search"
        board = client.get("/api/models").json()
        found = {m["id"]: m["status"] for m in board["ranked"] + board["unranked"]}
        assert found.get(ids["ingame_model"]) == "paper_ok" and found.get(ids["ingame_weak"]) == "candidate", found
        probed = parse(client.post("/exchange/probe-gamestate", data={"event": LIVE_EVENT}).text)
        assert probed.one("main > h1").text.startswith("Game-state probe") and probed.one("dd.c-game").text == LIVE_GAME
        assert "clock_seconds" in probed.one("pre#parsed").text
