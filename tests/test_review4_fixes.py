"""Regression tests for the step 4 review findings: the approval lock fences (halt,
release handshake, heartbeat release, expired lease), the kickoff cutoff, the
read-only kill flag, the stale newest book, the worker's global max bet, the
kill-vs-activate and kill-vs-settlement interleavings, the moneyline filter, the
paper fill rules (limit price for resting bids, shared level liquidity, fee and
cost rounding), pooled games, surfaced exchange errors, the outcome words that are
not team codes, a cancelled trade job and the host-side trade_max_games cap."""
from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from typing import Any

from host import eligibility, kill, leases
from host.exchange import mapping, paper, settle, snapshots
from host.exchange.adapters import polymarket_clob, polymarket_us, teams
from host.exchange.adapters.base import Book, MarketSource, RateLimited, SourceError
from host.exchange.executor import Executor
from host.exchange.main import ExchangeLoop
from host.exchange.ratelimit import RateLimiter
from host.heartbeat import process_heartbeat
from host.scheduling import cancel_job
from host.trading import assignments, ledger, orders
from host.trading.limits import approve_order, order_cost_cents
from host.trading.state import trade_state
from tests.conftest import (
    GAME_ID, approve, approved_order, assignment_row, bankroll_of, heartbeat_body, insert_game, insert_market,
    insert_model, insert_snapshot, insert_worker, job_row, make_assignment, order_events, order_row, set_setting,
    trade_setup, worker_row,
)
from tests.test_exchange import NOW, bankroll, make_assignment as mk_assignment, make_game, make_market, make_model, make_order, order, snap

FEE = {"taker_rate": 0.05, "half_spread": 0.01}


def _wait_blocked(conn: Any, timeout: float = 10.0) -> bool:
    """True once some backend of this database waits on a lock."""
    end = time.time() + timeout
    while time.time() < end:
        n = conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"
        ).fetchone()["n"]
        if n:
            return True
        time.sleep(0.02)
    return False


def _approve_in_thread(pool: Any, worker: dict[str, Any], body: dict[str, Any]) -> tuple[threading.Thread, dict[str, Any]]:
    out: dict[str, Any] = {}

    def run() -> None:
        with pool.connection() as c:
            out["decision"] = approve_order(c, worker, body)

    t = threading.Thread(target=run)
    t.start()
    return t, out


# ------------------------------------------------------------ HIGH: halt and release fences


def test_halt_in_flight_blocks_the_approval_which_then_sees_halted(pool, conn):
    s = trade_setup(conn)
    approved_order(conn, s, size=10)
    wrow = worker_row(conn, s.worker.id)
    with pool.connection() as c2:
        assignments.halt_assignment(c2, s.assignment["id"], "owner", "owner halt")  # uncommitted
        t, out = _approve_in_thread(pool, wrow, s.body(size=10))
        assert _wait_blocked(conn), "the approval waits for the halt's approval lock"
        assert "decision" not in out
        c2.commit()
    t.join(10)
    assert out["decision"]["status"] == "rejected" and out["decision"]["reason"] == "assignment"
    assert assignment_row(conn, s.assignment["id"])["status"] == "halted"
    assert conn.execute("SELECT count(*) AS n FROM orders WHERE status = 'approved'").fetchone()["n"] == 0
    assert ledger.replay_problems(conn) == []


def test_release_handshake_in_flight_blocks_the_approval_which_then_sees_queued(pool, conn):
    s = trade_setup(conn)
    approved_order(conn, s, size=10)
    wrow = worker_row(conn, s.worker.id)
    with pool.connection() as c2:
        kill.approval_locks(c2)
        kill.cancel_active_orders(c2, s.worker.id, "drain", assignment_ids=[s.assignment["id"]], worker_id=s.worker.id)
        assert leases.release(c2, s.job["id"], s.job["lease_token"], None, None, s.worker.id, "drain") == "queued"
        t, out = _approve_in_thread(pool, wrow, s.body(size=10))
        assert _wait_blocked(conn)
        c2.commit()
    t.join(10)
    assert out["decision"] == {"status": "rejected", "order_id": out["decision"]["order_id"], "reason": "lease"}
    assert job_row(conn, s.job["id"])["status"] == "queued"
    assert conn.execute("SELECT count(*) AS n FROM orders WHERE status = 'approved'").fetchone()["n"] == 0


def test_heartbeat_release_in_flight_blocks_the_approval_on_the_job_row(pool, conn):
    """A release that does not take the approval lock (heartbeat released[], the reaper)
    is still fenced: the approval reads the job FOR SHARE and waits for the UPDATE."""
    s = trade_setup(conn)
    wrow = worker_row(conn, s.worker.id)
    with pool.connection() as c2:
        assert leases.release(c2, s.job["id"], s.job["lease_token"], None, None, s.worker.id, "preempt") == "queued"
        t, out = _approve_in_thread(pool, wrow, s.body(size=5))
        assert _wait_blocked(conn)
        c2.commit()
    t.join(10)
    assert out["decision"]["reason"] == "lease"
    assert job_row(conn, s.job["id"])["status"] == "queued"


def test_expired_lease_is_rejected_before_the_reaper_runs(conn):
    s = trade_setup(conn)
    conn.execute("UPDATE jobs SET lease_expires_at = now() - interval '10 minutes' WHERE id = %s", (s.job["id"],))
    assert approve(conn, s, size=5)["reason"] == "lease"
    conn.execute("UPDATE jobs SET lease_expires_at = now() + interval '10 minutes' WHERE id = %s", (s.job["id"],))
    assert approve(conn, s, size=5)["status"] == "approved"


# ------------------------------------------------------------------ kickoff cutoff


def test_resting_pregame_order_never_fills_in_game_and_is_cancelled_at_kickoff(conn):
    s = trade_setup(conn, kickoff_in_s=120)
    o = approved_order(conn, s, size=10)
    Executor().tick(conn)
    row = order_row(conn, o["id"])
    assert row["status"] == "open" and row["gtd_at"] == s.game["kickoff_at"], "gtd_at is capped at kickoff"
    conn.execute("UPDATE games SET kickoff_at = now() - interval '5 minutes' WHERE game_id = %s", (s.game["game_id"],))
    insert_snapshot(conn, s.market["id"], ask=0.50, bid=0.48)
    assert approve(conn, s, size=1)["reason"] == "kickoff"
    assert paper.process(conn) == 0, "an in-game snapshot fills nothing"
    assert order_row(conn, o["id"])["filled_size"] == 0
    counts = Executor().tick(conn)
    assert counts["kickoff"] == 1
    row = order_row(conn, o["id"])
    assert row["status"] == "cancelled" and order_events(conn, o["id"])[-1] == "cancelled"
    reason = conn.execute("SELECT detail FROM order_events WHERE order_id = %s ORDER BY id DESC LIMIT 1", (o["id"],)).fetchone()["detail"]
    assert reason == {"reason": "kickoff"}
    bank = bankroll_of(conn, s.assignment)
    assert bank["reserved_cents"] == 0 and bank["available_cents"] == 10_000
    assert ledger.replay_problems(conn) == []


def test_kickoff_cutoff_holds_when_not_pregame_only(conn):
    """Step 6C review: trade_pregame_only off no longer lets an order approved before
    kickoff rest into the game in play (only orders approved under the in-game rules
    trade it), so the GTD cap, the paper kickoff bound and the kickoff cancel apply."""
    set_setting(conn, "trade_pregame_only", False)
    s = trade_setup(conn, kickoff_in_s=120)
    o = approved_order(conn, s, size=10)
    Executor().tick(conn)
    assert order_row(conn, o["id"])["gtd_at"] == s.game["kickoff_at"], "gtd_at is capped at kickoff"
    conn.execute("UPDATE games SET kickoff_at = now() - interval '5 minutes' WHERE game_id = %s", (s.game["game_id"],))
    insert_snapshot(conn, s.market["id"], ask=0.50, bid=0.48)
    assert paper.process(conn) == 0 and order_row(conn, o["id"])["filled_size"] == 0
    assert Executor().tick(conn)["kickoff"] == 1
    assert order_row(conn, o["id"])["status"] == "cancelled"


# ------------------------------------------------------------- kill flag is read-only


def test_settings_api_cannot_raise_or_clear_the_kill_and_paper_fills_stop_under_the_flag(client, conn):
    s = trade_setup(conn)
    o = approved_order(conn, s, size=10)
    Executor().tick(conn)
    r = client.post("/api/settings", json={"kill_switch": True})
    assert r.status_code == 400 and "use /api/kill" in r.json()["detail"]
    assert not kill.is_killed(conn) and order_row(conn, o["id"])["status"] == "open"
    # a flag raised any other way still stops the simulator
    set_setting(conn, "kill_switch", True)
    insert_snapshot(conn, s.market["id"], ask=0.50, bid=0.48)
    assert paper.process(conn) == 0 and order_row(conn, o["id"])["filled_size"] == 0
    assert client.post("/api/settings", json={"kill_switch": False}).status_code == 400
    assert client.post("/api/settings", json={"live_enabled": True}).status_code == 400
    assert kill.is_killed(conn)
    assert client.post("/api/kill/reset", json={"confirm": "RESUME"}).status_code == 200
    assert not kill.is_killed(conn)
    assert paper.process(conn) >= 1 and order_row(conn, o["id"])["filled_size"] == 10


# -------------------------------------------------------- worker gets the global max bet


class _Sure:
    def predict(self, game: Any, market_p: Any, features: Any) -> float:
        return 0.60


def test_state_payload_carries_max_bet_and_the_worker_sizes_down(conn):
    from fleet.worker.trade import plan_proposals

    s = trade_setup(conn, bankroll_cents=100_000)
    set_setting(conn, "max_bet_cents", 2500)
    insert_snapshot(conn, s.market["id"], ask=0.52, bid=0.50, ask_depth=[[0.52, 5000]])
    state = trade_state(conn, s.worker.id)
    assert state["settings"]["max_bet_cents"] == 2500 and state["settings"]["trade_max_games"] == 6
    entry = state["assignments"][0]
    entry["game"]["kickoff_at"] = entry["game"]["kickoff_at"].isoformat()
    props = plan_proposals(entry, state["settings"], model=_Sure(), now=0)
    assert props and all(p["stake_cents"] <= 2500 for p in props)
    body = {k: v for k, v in props[0].items() if k not in ("side", "stake_cents")}
    body.update({k: str(v) for k, v in body.items() if k in ("job_id", "lease_token", "assignment_id", "market_id")})
    assert approve_order(conn, worker_row(conn, s.worker.id), body)["status"] == "approved"


# ---------------------------------------------------------- activate races the kill


def test_activate_all_paper_waits_for_a_kill_and_cannot_undo_its_halts(pool, conn, monkeypatch):
    s = trade_setup(conn)
    conn.execute("UPDATE assignments SET status = 'halted' WHERE id = %s", (s.assignment["id"],))
    s2 = trade_setup(conn, game_id="2026_05_BUF_MIA")
    checked, go = threading.Event(), threading.Event()
    real = assignments.killed_locked

    def slow(c: Any) -> bool:
        value = real(c)
        checked.set()
        go.wait(10)
        return value

    monkeypatch.setattr(assignments, "killed_locked", slow)
    out: dict[str, Any] = {}

    def run() -> None:
        with pool.connection() as c:
            out["n"] = assignments.activate_all_paper(c, "owner")

    t = threading.Thread(target=run)
    t.start()
    checked.wait(10)
    killer: dict[str, Any] = {}

    def run_kill() -> None:
        with pool.connection() as c2:
            killer["changed"] = kill.set_kill(c2, "owner")

    k = threading.Thread(target=run_kill)
    k.start()
    assert _wait_blocked(conn), "the kill waits for the activation holding the flag FOR SHARE"
    go.set()
    t.join(10)
    k.join(10)
    assert out["n"] == 1 and killer["changed"] is True
    assert kill.is_killed(conn)
    assert assignment_row(conn, s.assignment["id"])["status"] == "halted"
    assert assignment_row(conn, s2.assignment["id"])["status"] == "halted"


# ------------------------------------------------------- kill vs settlement lock order


def test_kill_during_settlement_waits_instead_of_deadlocking(pool, conn, monkeypatch):
    kickoff = NOW - timedelta(hours=4)
    make_game(conn, kickoff=kickoff, status="final", home_score=10, away_score=20)
    m = make_market(conn, "2026_05_KC_LV", "away")
    a = mk_assignment(conn, "2026_05_KC_LV", make_model(conn), bankroll_cents=100_000)
    o = make_order(conn, a, m, 0.50, 10, status="open", submitted_at=kickoff - timedelta(hours=1))
    real_cancel = orders.cancel_order
    holding = threading.Event()

    def slow_cancel(c: Any, order_id: Any, actor: Any, reason: str) -> str:
        holding.set()
        time.sleep(0.5)
        return real_cancel(c, order_id, actor, reason)

    monkeypatch.setattr(orders, "cancel_order", slow_cancel)
    errors: dict[str, str] = {}

    def run_settle() -> None:
        try:
            with pool.connection() as c:
                settle.settle_game(c, "2026_05_KC_LV", "test")
        except Exception as exc:  # noqa: BLE001
            errors["settle"] = repr(exc)

    def run_kill() -> None:
        holding.wait(5)
        try:
            with pool.connection() as c:
                kill.set_kill(c, "owner")
        except Exception as exc:  # noqa: BLE001
            errors["kill"] = repr(exc)

    threads = [threading.Thread(target=run_settle), threading.Thread(target=run_kill)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert errors == {}
    assert kill.is_killed(conn)
    assert assignment_row(conn, a["id"])["status"] == "settled" and order(conn, o["id"])["status"] == "cancelled"
    assert ledger.replay_problems(conn) == []


# --------------------------------------------------- CLOB: moneylines only, one per side


def _event(markets: list[dict[str, Any]]) -> dict[str, Any]:
    start = (NOW + timedelta(days=2)).isoformat()
    return {"id": "e1", "title": "Chiefs vs. Raiders", "startDate": start,
            "markets": [dict(m, gameStartTime=start) for m in markets]}


def test_clob_spread_total_and_half_markets_are_not_moneylines(conn):
    teams_json = json.dumps(["Chiefs", "Raiders"])
    event = _event([
        {"id": "ml", "question": "Chiefs vs. Raiders", "outcomes": teams_json, "clobTokenIds": json.dumps(["t1", "t2"])},
        {"id": "sp", "question": "Spread: Chiefs (-9.5)", "outcomes": teams_json, "clobTokenIds": json.dumps(["t3", "t4"]),
         "sportsMarketType": "spreads", "line": -9.5},
        {"id": "sp2", "question": "Chiefs (-3.5) vs. Raiders (+3.5)", "outcomes": teams_json, "clobTokenIds": json.dumps(["t5", "t6"])},
        {"id": "half", "question": "1st Half Winner: Chiefs vs. Raiders", "outcomes": teams_json, "clobTokenIds": json.dumps(["t7", "t8"])},
        {"id": "ou", "question": "Chiefs vs. Raiders: O/U 45.5", "outcomes": json.dumps(["Over", "Under"]), "clobTokenIds": json.dumps(["t9", "t10"])},
        {"id": "typed", "question": "Chiefs vs. Raiders", "outcomes": teams_json, "clobTokenIds": json.dumps(["t11", "t12"]),
         "sportsMarketType": "moneyline"},
    ])
    infos = polymarket_clob.parse_events(json.dumps([event]))
    assert sorted(i.market_ref for i in infos) == ["t1", "t11", "t12", "t2"]
    make_game(conn)
    rows = [mapping.upsert_market(conn, i, mapping.match(conn, i, NOW, 8)) for i in infos]
    confirmed = [(r["market_ref"], r["side"]) for r in rows if r["mapping_confirmed"]]
    assert sorted(confirmed) == [("t1", "away"), ("t2", "home")], "a second listing per side is left for the owner"
    second = [r for r in rows if r["market_ref"] in ("t11", "t12")]
    assert all(not r["mapping_confirmed"] and r["game_id"] == "2026_05_KC_LV" and r["mapping_confidence"] <= 0.5 for r in second)


def test_min_size_overflow_skips_the_record_not_the_payload():
    event = _event([
        {"id": "bad", "question": "Chiefs vs. Raiders", "outcomes": json.dumps(["Chiefs", "Raiders"]), "clobTokenIds": json.dumps(["b1", "b2"]),
         "orderMinSize": "inf"},
        {"id": "huge", "question": "Chiefs vs. Raiders", "outcomes": json.dumps(["Chiefs", "Raiders"]), "clobTokenIds": json.dumps(["h1", "h2"]),
         "orderMinSize": 1e30},
    ])
    infos = polymarket_clob.parse_events(json.dumps([event]))
    assert len(infos) == 4 and all(i.min_size == 1 for i in infos)
    us = polymarket_us.parse_markets(json.dumps([
        {"id": "m1", "title": "Chiefs vs Raiders", "outcome": "Chiefs", "min_order_size": "inf"},
        {"id": "m2", "title": "Chiefs vs Raiders", "outcome": "Raiders", "min_order_size": 1e30},
        {"id": "m3", "title": "Chiefs vs Raiders", "outcome": "Raiders", "min_order_size": 5},
    ]))
    assert [(i.market_ref, i.min_size) for i in us] == [("m1", 1), ("m2", 1), ("m3", 5)]


# ------------------------------------------------------------- outcome words, team codes


def test_outcome_words_are_not_team_codes():
    rec = {"id": "m1", "title": "Will the Saints beat the Falcons?", "home_team": "NO", "away_team": "ATL", "outcome": "No",
           "start_time": "2026-10-11T17:00:00Z"}
    info = polymarket_us.parse_market(rec, polymarket_us.DEFAULTS["fields"])
    assert (info.home_team, info.away_team, info.side) == ("NO", "ATL", None), "a No outcome is not the Saints"
    assert teams.resolve("No") is None and teams.resolve("was") is None and teams.resolve("ten") is None
    assert teams.resolve("Min") is None and teams.resolve("Yes") is None and teams.resolve("YES") is None
    assert teams.resolve("NO") == "NO" and teams.resolve("WAS") == "WAS" and teams.resolve("Saints") == "NO"
    assert teams.find("Will the Saints beat the Falcons? Yes or no") == ["NO", "ATL"]
    assert teams.find("Ten things about the Vikings (min 3)") == ["MIN"]
    assert teams.find("WAS @ TEN") == ["WAS", "TEN"]
    yes = {"id": "m2", "title": "Will the Saints beat the Falcons?", "home_team": "NO", "away_team": "ATL", "outcome": "NO"}
    assert polymarket_us.parse_market(yes, polymarket_us.DEFAULTS["fields"]).side == "home", "the upper-case code is the team"


# ----------------------------------------------------------------- paper fill rules


def test_two_orders_share_one_levels_liquidity_in_submission_order(conn):
    make_game(conn)
    m = make_market(conn, "2026_05_KC_LV", "home")
    a1 = mk_assignment(conn, "2026_05_KC_LV", make_model(conn), bankroll_cents=100_000)
    a2 = mk_assignment(conn, "2026_05_KC_LV", make_model(conn), bankroll_cents=100_000)
    o1 = make_order(conn, a1, m, 0.52, 100, status="open", submitted_at=NOW)
    o2 = make_order(conn, a2, m, 0.52, 100, status="open", submitted_at=NOW + timedelta(seconds=1))
    snap(conn, m["id"], 0.50, 0.52, NOW + timedelta(seconds=2), size=100, levels=1)
    assert paper.process(conn) == 1
    assert order(conn, o1["id"])["filled_size"] == 50 and order(conn, o2["id"])["filled_size"] == 0, "half of 100, once"
    snap(conn, m["id"], 0.50, 0.52, NOW + timedelta(seconds=4), size=100, levels=1)
    assert paper.process(conn) == 1
    assert order(conn, o1["id"])["filled_size"] == 100 and order(conn, o2["id"])["filled_size"] == 0
    snap(conn, m["id"], 0.50, 0.52, NOW + timedelta(seconds=6), size=100, levels=1)
    paper.process(conn)
    assert order(conn, o2["id"])["filled_size"] == 50, "o2 gets the level once o1 is done"
    assert ledger.replay_problems(conn) == []


def test_marketable_order_takes_the_ask_but_a_resting_one_fills_at_its_limit(conn):
    make_game(conn)
    m = make_market(conn, "2026_05_KC_LV", "home")
    a = mk_assignment(conn, "2026_05_KC_LV", make_model(conn), bankroll_cents=100_000)
    marketable = make_order(conn, a, m, 0.55, 10, status="open", submitted_at=NOW)
    snap(conn, m["id"], 0.50, 0.52, NOW + timedelta(seconds=1), size=100)
    paper.process(conn)
    assert float(order(conn, marketable["id"])["avg_fill_price"]) == 0.52, "price improvement for a marketable order"
    resting = make_order(conn, a, m, 0.50, 10, status="open", submitted_at=NOW + timedelta(seconds=2))
    snap(conn, m["id"], 0.50, 0.53, NOW + timedelta(seconds=3), size=100)
    assert paper.process(conn) == 0
    snap(conn, m["id"], 0.38, 0.40, NOW + timedelta(seconds=5), size=500)
    assert paper.process(conn) == 1
    row = order(conn, resting["id"])
    assert row["status"] == "filled" and float(row["avg_fill_price"]) == 0.50, "a crossed resting bid fills at its limit"
    assert ledger.replay_problems(conn) == []


def _open_paper_order(conn: Any, assignment: dict[str, Any], market: dict[str, Any], price: float, size: int, fee: dict[str, Any], crid: str) -> dict[str, Any]:
    cost, fee_est = order_cost_cents(price, size, fee)
    row = conn.execute(
        """INSERT INTO orders (client_request_id, assignment_id, market_id, mode, price, size, cost_cents, fee_cents_est, status, submitted_at)
           VALUES (%s, %s, %s, 'paper', %s, %s, %s, %s, 'open', %s) RETURNING *""",
        (crid, assignment["id"], market["id"], price, size, cost, fee_est, NOW),
    ).fetchone()
    ledger.reserve(conn, assignment["bankroll"]["id"], cost, row["id"])
    return row


def test_fee_rounding_on_the_cumulative_size_fills_the_whole_order(conn):
    make_game(conn)
    m = make_market(conn, "2026_05_KC_LV", "home")
    a = mk_assignment(conn, "2026_05_KC_LV", make_model(conn), bankroll_cents=100_000)
    set_setting(conn, "participation", 1.0)
    row = _open_paper_order(conn, a, m, 0.86, 2, FEE, "fee-1")
    assert row["cost_cents"] == 173
    for i in range(1, 4):
        snapshots.record_snapshot(conn, m["id"], Book(bids=[[0.84, 1]], asks=[[0.86, 1]], fetched_at=NOW), NOW + timedelta(seconds=2 * i))
    paper.process(conn)
    r = order(conn, row["id"])
    assert r["status"] == "filled" and r["filled_size"] == 2
    fees = [f["fee_cents"] for f in conn.execute("SELECT fee_cents FROM fills WHERE order_id = %s ORDER BY id", (row["id"],)).fetchall()]
    assert fees == [1, 0] and bankroll(conn, a)["reserved_cents"] == 0
    assert ledger.replay_problems(conn) == []


def test_half_cent_prices_round_the_same_way_everywhere(conn):
    make_game(conn)
    m = make_market(conn, "2026_05_KC_LV", "home")
    conn.execute("UPDATE markets SET tick = 0.001 WHERE id = %s", (m["id"],))
    a = mk_assignment(conn, "2026_05_KC_LV", make_model(conn), bankroll_cents=100_000)
    set_setting(conn, "participation", 1.0)
    set_setting(conn, "fee_model", {"taker_rate": 0.0, "half_spread": 0.01})
    row = _open_paper_order(conn, a, m, 0.985, 1, {"taker_rate": 0.0}, "half-1")
    assert row["cost_cents"] == 99
    snapshots.record_snapshot(conn, m["id"], Book(bids=[[0.98, 10]], asks=[[0.985, 10]], fetched_at=NOW), NOW + timedelta(seconds=2))
    paper.process(conn)
    r = order(conn, row["id"])
    b = bankroll(conn, a)
    assert r["status"] == "filled" and orders.fill_cost_cents(0.985, 1) == 99
    assert b["reserved_cents"] == 0 and b["open_cost_cents"] == 99, "no stranded cent"
    assert ledger.replay_problems(conn) == []
    conn.execute("UPDATE games SET status = 'final', home_score = 20, away_score = 10 WHERE game_id = '2026_05_KC_LV'")
    settle.settle_game(conn, "2026_05_KC_LV")
    assert bankroll(conn, a)["open_cost_cents"] == 0 and ledger.replay_problems(conn) == []


# --------------------------------------------------------------- pooled paper games


def test_pooled_games_count_each_game_once_per_lineage(conn):
    make_game(conn, kickoff=NOW - timedelta(hours=4), status="final", home_score=10, away_score=20)
    root = make_model(conn)
    child = insert_model(conn, parent=root)
    make_market(conn, "2026_05_KC_LV", "away")
    for model in (root, child):
        mk_assignment(conn, "2026_05_KC_LV", model)
    settle.settle_game(conn, "2026_05_KC_LV")
    stats = eligibility.paper_stats(conn, root["lineage_id"])
    assert stats["games"] == 1 and stats["bets"] == 0


# ------------------------------------------------------- exchange errors are surfaced


class _Failing(MarketSource):
    name = "failing"

    def __init__(self, exc: Exception, good: set[str] | None = None) -> None:
        self.exc = exc
        self.good = good or set()

    def list_markets(self, games: Any, lookahead_days: int) -> list[Any]:
        return []

    def fetch_book(self, market_ref: str) -> Book:
        if market_ref in self.good:
            return Book(bids=[[0.5, 100]], asks=[[0.52, 100]], fetched_at=NOW)
        raise self.exc


def test_failed_book_fetches_and_429s_reach_last_error_and_the_limiter(pool, conn):
    make_game(conn)
    make_market(conn, "2026_05_KC_LV", "home", ref="a")
    make_market(conn, "2026_05_KC_LV", "away", ref="b")
    conn.commit()
    loop = ExchangeLoop(pool)
    loop.source, loop.source_name = _Failing(SourceError("GET ... failed")), "polymarket_us"
    assert loop.run_task("snapshots", NOW) is None
    assert loop.last_error and loop.last_error.startswith("snapshots: every book fetch failed (2 of 2)")
    loop.source = _Failing(SourceError("boom"), good={"a"})
    result = loop.run_task("snapshots", NOW + timedelta(seconds=40))
    assert result["stored"] == 1 and result["failed"] == 1
    assert loop.last_error == "snapshots: 1 of 2 book fetches failed, last: b: boom"
    limiter = RateLimiter({"market_data_per_s": 10}, now=NOW)
    counts: dict[str, Any] = {}
    try:
        snapshots.poll(conn, _Failing(RateLimited("book request answered 429")), limiter, NOW + timedelta(seconds=80))
    except SourceError as exc:
        counts["error"] = str(exc)
    assert "429" in counts["error"]
    assert limiter.effective_rate("market_data", NOW + timedelta(seconds=80)) == 5.0, "halved for 60 s after a 429"
    assert limiter.effective_rate("market_data", NOW + timedelta(seconds=141)) == 10.0
    loop.source = _Failing(SourceError("x"), good={"a", "b"})
    assert loop.run_task("snapshots", NOW + timedelta(seconds=200))["error"] is None and loop.last_error is None


def test_snapshot_pass_budget_defers_the_rest(conn):
    make_game(conn)
    make_market(conn, "2026_05_KC_LV", "home", ref="a")
    make_market(conn, "2026_05_KC_LV", "away", ref="b")
    counts = snapshots.poll(conn, _Failing(SourceError("x"), good={"a", "b"}), None, NOW, budget_s=-1.0)
    assert counts["deferred"] == 2 and counts["stored"] == 0 and counts["error"] is None


def test_failed_settlement_is_reported_by_the_settle_task(pool, conn, monkeypatch):
    make_game(conn, kickoff=NOW - timedelta(hours=4), status="final", home_score=10, away_score=20)
    make_market(conn, "2026_05_KC_LV", "away")
    mk_assignment(conn, "2026_05_KC_LV", make_model(conn))
    conn.commit()

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ledger.LedgerError("ledger says no")

    monkeypatch.setattr(settle, "upsert_score", broken)
    loop = ExchangeLoop(pool)
    result = loop.run_task("settle", NOW)
    assert result["settled"] == [] and "ledger says no" in result["error"]
    assert loop.last_error.startswith("settle: settlement of 2026_05_KC_LV failed")
    assert conn.execute("SELECT status FROM assignments").fetchone()["status"] == "active", "rolled back, retried next pass"


# ------------------------------------------------------------ cancelled trade job


def test_cancelling_a_trade_job_halts_the_assignment_and_cancels_its_orders(conn):
    s = trade_setup(conn)
    o = approved_order(conn, s, size=10)
    Executor().tick(conn)
    assert order_row(conn, o["id"])["status"] == "open"
    job = cancel_job(conn, s.job["id"], "owner")
    assert job["status"] == "cancel_requested"
    assert assignment_row(conn, s.assignment["id"])["status"] == "halted"
    assert order_row(conn, o["id"])["status"] == "cancelled" and bankroll_of(conn, s.assignment)["reserved_cents"] == 0
    assert approve(conn, s, size=1)["reason"] == "lease"
    assert ledger.replay_problems(conn) == []


def test_cancel_trade_job_from_the_jobs_page(client, conn):
    s = trade_setup(conn)
    o = approved_order(conn, s, size=10)
    r = client.post(f"/jobs/{s.job['id']}/cancel", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert assignment_row(conn, s.assignment["id"])["status"] == "halted" and order_row(conn, o["id"])["status"] == "cancelled"


# ------------------------------------------------------- host-side trade_max_games cap


def test_trade_max_games_caps_claims_on_the_host(pool, conn):
    insert_game(conn)
    set_setting(conn, "max_paper_models_per_game", 10)
    set_setting(conn, "trade_max_games", 2)
    created = [make_assignment(conn) for _ in range(4)]
    trader = insert_worker(conn, "t", role="trade")
    with pool.connection() as c:
        reply = process_heartbeat(c, trader.id, heartbeat_body("trade", want_job=False, want_jobs=4))
    assert [x["id"] for x in reply["claimed"]] == [str(a["job_id"]) for a in created[:2]], "capped at trade_max_games"
    held = [{"id": x["id"], "lease_token": x["lease_token"]} for x in reply["claimed"]]
    with pool.connection() as c:
        reply = process_heartbeat(c, trader.id, heartbeat_body("trade", want_job=False, want_jobs=4, jobs=held))
    assert reply["claimed"] == []
    set_setting(conn, "trade_max_games", 0)
    other = insert_worker(conn, "u", role="trade")
    with pool.connection() as c:
        assert process_heartbeat(c, other.id, heartbeat_body("trade", want_job=False, want_jobs=3))["claimed"] == []


# ------------------------------------------------------------------ dashboard details


def test_assign_select_puts_trained_models_first_and_names_untrained_ones(client, conn):
    insert_game(conn)
    insert_market(conn, GAME_ID)
    untrained = insert_model(conn, status="candidate", params={"k": 40.0, "hfa": 70.0, "mov_scale": 1})
    trained = insert_model(conn, status="paper_ok", params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, trained_through=[2024, 18])
    html = client.get("/trading").text
    select = html.split('name="model_id"')[1].split("</select>")[0]
    assert select.index(str(trained["id"])) < select.index(str(untrained["id"])), "trained first although older"
    assert f'<option value="{untrained["id"]}">elo_blend · K 40 · HFA 70 · MOV on · untrained: mirrors the market, never trades · ' in html


def test_open_orders_name_the_model_and_cancelled_orders_show_their_cause(client, conn):
    s = trade_setup(conn)
    o = approved_order(conn, s, size=10)
    Executor().tick(conn)
    html = client.get("/fragments/trading").text
    open_rows = html.split('id="open-orders"')[1].split('id="orders"')[0]
    assert f'data-assignment="{s.assignment["id"]}">elo_blend {str(s.model["id"])[:8]}</a>' in open_rows
    orders.cancel_order(conn, o["id"], "owner", "owner cancel")
    html = client.get("/fragments/trading").text
    recent = html.split('id="orders"')[1].split('id="fills"')[0]
    assert '<span class="badge st-cancelled">cancelled</span> <span class="muted small cause">owner cancel</span>' in recent
