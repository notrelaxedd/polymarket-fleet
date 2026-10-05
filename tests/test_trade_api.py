"""Worker trade routes, owner trading routes, trade claims and the orphan rule over HTTP."""
from __future__ import annotations

import sys
import threading
import time
import types
import uuid
from typing import Any

from host import loop
from host.api import trade as trade_routes
from host.trading import ledger, orders
from tests.conftest import (
    GAME_ID, approved_order, assignment_row, bankroll_of, heartbeat_body, insert_game, insert_market, insert_model,
    insert_snapshot, insert_worker, job_row, lease_trade_job, make_assignment, order_row, set_heartbeat_age, trade_setup,
)

OTHER_GAME = "2026_05_BUF_MIA"
STATE_KEYS = {"id", "job_id", "lease_token", "status", "mode", "max_bet_cents", "game", "model", "bankroll", "markets",
              "open_orders", "positions"}
MARKET_KEYS = {"id", "side", "bid", "ask", "mid", "tick", "min_size", "snapshot_id", "snapshot_at", "liquidity_usd_cents",
               "ask_depth", "status", "below_floor"}


def test_state_payload_shape(client, conn):
    s = trade_setup(conn)
    thin = insert_market(conn, GAME_ID, side="away")
    insert_snapshot(conn, thin["id"], liquidity_usd_cents=1_000)
    insert_market(conn, GAME_ID, side="away", confirmed=False)
    row = approved_order(conn, s, size=2)
    assert client.get("/api/v1/trade/state").status_code == 401
    r = client.get("/api/v1/trade/state", headers=s.worker.headers)
    assert r.status_code == 200, r.text
    state = r.json()
    assert set(state) == {"kill", "server_time", "settings", "assignments"} and state["kill"] is False
    assert set(state["settings"]) == {"min_edge", "kelly_fraction", "participation", "trade_pregame_only", "fee_model", "trade_tick_s",
                                      "max_bet_cents", "trade_max_games", "ingame_tick_s", "ingame_max_state_age_s",
                                      "ingame_quiet_seconds", "ingame_cutoff_seconds", "ingame_dead_zone", "ingame_min_edge",
                                      "ingame_max_bet_cents", "ingame_gtd_seconds"}
    assert state["settings"]["max_bet_cents"] == 2500 and state["settings"]["trade_max_games"] == 6
    assert state["settings"]["fee_model"] == {"taker_rate": 0.05, "half_spread": 0.01} and state["server_time"].endswith("Z")
    assert len(state["assignments"]) == 1
    a = state["assignments"][0]
    assert set(a) >= STATE_KEYS and a["id"] == str(s.assignment["id"]) and a["job_id"] == str(s.job["id"])
    assert a["lease_token"] == str(s.job["lease_token"]) and a["mode"] == "paper" and a["status"] == "active"
    assert a["game"]["game_id"] == GAME_ID and a["game"]["kickoff_at"].endswith("Z") and a["game"]["status"] == "scheduled"
    assert "raw" not in a["game"]
    assert a["model"]["id"] == str(s.model["id"]) and a["model"]["family"] == "elo_blend" and a["model"]["artifact"]["blend"]
    assert a["bankroll"] == {"available_cents": 10_000 - 106, "reserved_cents": 106, "open_cost_cents": 0, "realized_pnl_cents": 0}
    assert [m["side"] for m in a["markets"]] == ["away", "home"], "confirmed markets only"
    for m in a["markets"]:
        assert set(m) >= MARKET_KEYS
    home = next(m for m in a["markets"] if m["side"] == "home")
    assert home["snapshot_id"] == s.snapshot["id"] and home["ask"] == 0.52 and home["below_floor"] is False
    assert home["ask_depth"][0] == [0.52, 500]
    assert next(m for m in a["markets"] if m["side"] == "away")["below_floor"] is True
    assert a["open_orders"][0]["id"] == str(row["id"]) and a["open_orders"][0]["status"] == "approved"
    assert a["positions"] == []
    second = make_assignment(conn, game_id=GAME_ID)
    lease_trade_job(conn, s.worker, second)
    assert len(client.get("/api/v1/trade/state", headers=s.worker.headers).json()["assignments"]) == 2
    other = insert_worker(conn, "other", role="trade")
    assert client.get("/api/v1/trade/state", headers=other.headers).json()["assignments"] == []


def test_request_approve_and_reject_through_http(client, conn):
    s = trade_setup(conn)
    body = s.body(size=10)
    assert client.post("/api/v1/orders/request", json=body).status_code == 401
    r = client.post("/api/v1/orders/request", json=body, headers=s.worker.headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["reason"] is None
    order = order_row(conn, r.json()["order_id"])
    assert order["cost_cents"] == 532 and order["worker_id"] == s.worker.id
    r = client.post("/api/v1/orders/request", json=body, headers=s.worker.headers)
    assert r.json()["order_id"] == str(order["id"]) and r.json()["duplicate"] is True
    fenced = client.post("/api/v1/orders/request", json=s.body(lease_token=str(uuid.uuid4())), headers=s.worker.headers)
    assert fenced.status_code == 200 and fenced.json()["status"] == "rejected" and fenced.json()["reason"] == "lease"
    other = insert_worker(conn, "other", role="trade")
    stolen = client.post("/api/v1/orders/request", json=s.body(), headers=other.headers)
    assert stolen.status_code == 200 and stolen.json()["reason"] == "lease", "the fence is the worker's own lease"
    assert client.post("/api/v1/orders/request", json=s.body(size=0), headers=s.worker.headers).status_code == 400
    assert client.post("/api/v1/orders/request", json=s.body(price=1.2), headers=s.worker.headers).status_code == 400
    assert client.post("/api/v1/orders/request", json={"client_request_id": "x"}, headers=s.worker.headers).status_code == 400
    big = client.post("/api/v1/orders/request", json=s.body(size=50, max_bet_cents=10**9), headers=s.worker.headers)
    assert big.json()["reason"] == "max_bet"
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == 532 and ledger.replay_problems(conn) == []


def test_worker_cancel_own_order(client, conn):
    s = trade_setup(conn)
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=OTHER_GAME, worker=s.worker)
    paper = approved_order(conn, s, size=5)
    r = client.post(f"/api/v1/orders/{paper['id']}/cancel", headers=s.worker.headers)
    assert r.status_code == 200 and r.json() == {"status": "cancelled"}
    assert order_row(conn, paper["id"])["status"] == "cancelled" and bankroll_of(conn, s.assignment)["reserved_cents"] == 0
    assert client.post(f"/api/v1/orders/{paper['id']}/cancel", headers=s.worker.headers).json() == {"status": "cancelled"}
    lv = approved_order(conn, live, size=5)
    orders.set_status(conn, lv["id"], "open", "executor", expected=("approved",))
    assert client.post(f"/api/v1/orders/{lv['id']}/cancel", headers=s.worker.headers).json() == {"status": "cancel_requested"}
    other = insert_worker(conn, "other", role="trade")
    assert client.post(f"/api/v1/orders/{lv['id']}/cancel", headers=other.headers).status_code == 409
    assert client.post(f"/api/v1/orders/{uuid.uuid4()}/cancel", headers=s.worker.headers).status_code == 404
    assert client.post("/api/v1/orders/nope/cancel", headers=s.worker.headers).status_code == 404
    assert client.post(f"/api/v1/orders/{lv['id']}/cancel").status_code == 401


def test_release_handshake(client, conn, monkeypatch):
    s = trade_setup(conn)
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=OTHER_GAME, worker=s.worker)
    p1 = approved_order(conn, s, size=2)
    p2 = approved_order(conn, s, size=3)
    orders.set_status(conn, p2["id"], "open", "executor", expected=("approved",))
    lv = approved_order(conn, live, size=2)
    orders.set_status(conn, lv["id"], "open", "executor", expected=("approved",))
    stranger = trade_setup(conn, game_id="2026_05_DAL_NYG")
    foreign = approved_order(conn, stranger, size=1)
    monkeypatch.setattr(trade_routes, "RELEASE_WAIT_SECONDS", 0.5)
    jobs = [{"id": str(s.job["id"]), "lease_token": str(s.job["lease_token"])},
            {"id": str(live.job["id"]), "lease_token": str(live.job["lease_token"])},
            {"id": str(stranger.job["id"]), "lease_token": str(stranger.job["lease_token"])},
            {"id": "garbage", "lease_token": "x"}]
    assert client.post("/api/v1/trade/release", json={"jobs": jobs}).status_code == 401
    started = time.monotonic()
    r = client.post("/api/v1/trade/release", json={"jobs": jobs}, headers=s.worker.headers)
    elapsed = time.monotonic() - started
    assert r.status_code == 200, r.text
    assert r.json() == {"cancelled": 2, "pending": 1, "released": [str(s.job["id"]), str(live.job["id"])]}
    assert 0.4 < elapsed < 3, "waited the bounded time for the live cancel"
    assert order_row(conn, p1["id"])["status"] == "cancelled" and order_row(conn, p2["id"])["status"] == "cancelled"
    assert order_row(conn, lv["id"])["status"] == "cancel_requested"
    assert order_row(conn, foreign["id"])["status"] == "approved", "another worker's job and order are untouched"
    for job in (s.job, live.job):
        row = job_row(conn, job["id"])
        assert row["status"] == "queued" and row["lease_token"] is None and row["expiries"] == 0
    assert job_row(conn, stranger.job["id"])["status"] == "leased"
    events = conn.execute("SELECT detail FROM job_events WHERE job_id = %s ORDER BY id DESC LIMIT 1", (s.job["id"],)).fetchone()
    assert events["detail"] == {"status": "queued", "reason": "drain"}
    # the exchange confirms within the wait: pending drops to zero
    orders.confirm_cancelled(conn, lv["id"], "exchange")
    job2 = lease_trade_job(conn, s.worker, live.assignment)
    live.job = job2
    lv2 = approved_order(conn, live, size=1)
    orders.set_status(conn, lv2["id"], "open", "executor", expected=("approved",))

    def confirm() -> None:
        time.sleep(0.15)
        orders.confirm_cancelled(conn, lv2["id"], "exchange")

    t = threading.Thread(target=confirm)
    t.start()
    r = client.post("/api/v1/trade/release", headers=s.worker.headers,
                    json={"jobs": [{"id": str(job2["id"]), "lease_token": str(job2["lease_token"])}]})
    t.join()
    assert r.json() == {"cancelled": 0, "pending": 0, "released": [str(job2["id"])]}
    assert order_row(conn, lv2["id"])["status"] == "cancelled"
    assert client.post("/api/v1/trade/release", json={"jobs": []}, headers=s.worker.headers).json() == {
        "cancelled": 0, "pending": 0, "released": []}
    assert ledger.replay_problems(conn) == []


def test_want_jobs_claims_up_to_the_slots(client, conn):
    insert_game(conn)
    insert_market(conn)
    created = [make_assignment(conn) for _ in range(3)]
    w = insert_worker(conn, "trader", role="trade")
    r = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("trade", want_jobs=2), headers=w.headers)
    assert r.status_code == 200, r.text
    claimed = r.json()["claimed"]
    assert [c["id"] for c in claimed] == [a["job_id"] for a in created[:2]], "oldest first, two slots"
    assert all(c["kind"] == "trade" and c["params"] == {"assignment_id": str(a["id"])} for c, a in zip(claimed, created))
    held = [{"id": c["id"], "lease_token": c["lease_token"]} for c in claimed]
    r = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("trade", want_jobs=0, jobs=held), headers=w.headers)
    assert r.json()["claimed"] == [] and r.json()["lost"] == []
    r = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("trade", want_jobs=5, jobs=held), headers=w.headers)
    assert [c["id"] for c in r.json()["claimed"]] == [created[2]["job_id"]]
    r = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("trade", want_job=True, jobs=held), headers=w.headers)
    assert r.json()["claimed"] == [], "want_job (bool) means nothing to a trade worker"
    assert client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("trade", want_jobs=-1), headers=w.headers).status_code == 400
    assert client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("trade", want_jobs=101), headers=w.headers).status_code == 400
    state = client.get("/api/v1/trade/state", headers=w.headers).json()
    assert len(state["assignments"]) == 3


def test_kill_refuses_trade_claims(client, conn):
    insert_game(conn)
    make_assignment(conn)
    w = insert_worker(conn, "trader", role="trade")
    client.post("/api/kill")
    r = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("trade", want_jobs=3), headers=w.headers)
    assert r.json()["kill"] is True and r.json()["claimed"] == []
    assert job_row(conn, assignment_row(conn, conn.execute("SELECT id FROM assignments").fetchone()["id"])["job_id"])["status"] == "queued"
    client.post("/api/kill/reset", json={"confirm": "RESUME"})
    r = client.post(f"/api/v1/workers/{w.id}/heartbeat", json=heartbeat_body("trade", want_jobs=3), headers=w.headers)
    assert r.json()["kill"] is False and len(r.json()["claimed"]) == 1, "the job is claimable again even though the assignment is halted"


def test_orphan_rule_cancels_silent_workers_orders(pool, conn):
    s = trade_setup(conn)
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=OTHER_GAME, worker=s.worker)
    alive = trade_setup(conn, game_id="2026_05_DAL_NYG")
    p = approved_order(conn, s, size=2)
    lv = approved_order(conn, live, size=2)
    orders.set_status(conn, lv["id"], "open", "executor", expected=("approved",))
    ok = approved_order(conn, alive, size=2)
    assert loop.run_once(pool) == {"reaped": 0, "dispatched": 0, "orphaned": 0}
    set_heartbeat_age(conn, s.worker.id, 29)
    assert loop.run_once(pool)["orphaned"] == 0, "under orphan_cancel_after_s"
    set_heartbeat_age(conn, s.worker.id, 31)
    assert loop.run_once(pool)["orphaned"] == 2
    assert order_row(conn, p["id"])["status"] == "cancelled" and order_row(conn, lv["id"])["status"] == "cancel_requested"
    assert order_row(conn, ok["id"])["status"] == "approved"
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == 0
    assert job_row(conn, s.job["id"])["status"] == "leased", "jobs follow the normal lease expiry"
    assert loop.run_once(pool)["orphaned"] == 0, "cancel_requested is not re-cancelled"
    events = conn.execute("SELECT actor, detail FROM order_events WHERE order_id = %s ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
    assert events["actor"] == "orphan" and events["detail"] == {"reason": "worker silent"}


# ------------------------------------------------------------------ owner routes


def test_owner_assignments_routes(client, conn):
    insert_game(conn)
    insert_market(conn)
    model = insert_model(conn, status="paper_ok", params={"k": 20.0})
    r = client.post("/api/assignments", json={"game_id": GAME_ID, "model_id": str(model["id"])})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["mode"] == "paper" and body["status"] == "active" and body["bankroll"]["available_cents"] == 10_000
    assert body["created_by"] == "dev" and job_row(conn, body["job_id"])["status"] == "queued"
    r = client.post("/api/assignments", json={"game_id": GAME_ID, "model_id": str(model["id"]), "bankroll_cents": 2500})
    assert r.status_code == 409 and "already has a paper assignment" in r.json()["detail"]
    assert client.post("/api/assignments", json={"game_id": "nope", "model_id": str(model["id"])}).status_code == 400
    assert client.post("/api/assignments", json={"game_id": GAME_ID, "model_id": str(model["id"]), "mode": "x"}).status_code == 400
    assert client.post("/api/assignments", json={"game_id": GAME_ID, "model_id": str(model["id"]), "bankroll_cents": -1}).status_code == 400
    listed = client.get("/api/assignments").json()
    assert [a["id"] for a in listed] == [body["id"]]
    assert listed[0]["game"]["home_team"] == "LV" and listed[0]["model"]["family"] == "elo_blend" and listed[0]["open_orders"] == 0
    assert listed[0]["job"]["status"] == "queued" and listed[0]["bankroll"]["reserved_cents"] == 0
    assert client.get("/api/assignments", params={"status": "halted"}).json() == []
    r = client.post(f"/api/assignments/{body['id']}/halt", json={"reason": "testing"})
    assert r.status_code == 200 and r.json()["status"] == "halted"
    assert client.get("/api/assignments", params={"status": "halted"}).json()[0]["id"] == body["id"]
    assert client.post(f"/api/assignments/{body['id']}/activate").json()["status"] == "active"
    detail = client.get(f"/api/assignments/{body['id']}").json()
    assert detail["orders"] == [] and detail["positions"] == [] and detail["fills"] == []
    assert detail["bankroll"]["available_cents"] == 10_000 and detail["game"]["home_team"] == "LV", "the list row shape plus the lists"
    assert detail["model"]["family"] == "elo_blend" and detail["job"]["status"] == "queued" and detail["open_orders"] == 0
    assert client.get(f"/api/assignments/{uuid.uuid4()}").status_code == 404
    r = client.post(f"/api/assignments/{body['id']}/settle")
    assert r.status_code == 409 and "not final" in r.json()["detail"]
    actions = [a["action"] for a in client.get("/api/audit", params={"limit": 10}).json()]
    assert actions[:3] == ["assignment_activated", "assignment_halted", "assignment_created"]


def test_owner_settle_and_probe_depend_on_the_exchange_module(client, conn, monkeypatch):
    s = trade_setup(conn)
    conn.execute("UPDATE games SET status = 'final', home_score = 24, away_score = 20 WHERE game_id = %s", (GAME_ID,))
    monkeypatch.setitem(sys.modules, "host.exchange.settle", None)
    monkeypatch.setitem(sys.modules, "host.exchange.probe", None)
    r = client.post(f"/api/assignments/{s.assignment['id']}/settle")
    assert r.status_code == 503 and r.json()["detail"] == "exchange module not available"
    r = client.post("/api/exchange/probe")
    assert r.status_code == 503 and r.json()["detail"] == "exchange module not available"
    calls: list[Any] = []
    settle = types.ModuleType("host.exchange.settle")
    settle.settle_game = lambda conn, game_id, actor="settle": calls.append((game_id, actor)) or {"settled": [str(s.assignment["id"])]}
    probe = types.ModuleType("host.exchange.probe")
    probe.probe_markets = lambda conn: {"source": "sim", "url": None, "status": 200, "payload": "[]", "error": None}
    monkeypatch.setitem(sys.modules, "host.exchange.settle", settle)
    monkeypatch.setitem(sys.modules, "host.exchange.probe", probe)
    r = client.post(f"/api/assignments/{s.assignment['id']}/settle")
    assert r.status_code == 200 and r.json() == {"settled": [str(s.assignment["id"])]} and calls == [(GAME_ID, "dev")]
    assert client.post("/api/exchange/probe").json()["source"] == "sim"


def test_owner_orders_fills_and_cancel_all(client, conn):
    s = trade_setup(conn)
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=OTHER_GAME, worker=s.worker)
    a = approved_order(conn, s, size=2)
    b = approved_order(conn, s, size=3)
    orders.set_status(conn, b["id"], "open", "executor", expected=("approved",))
    orders.record_fill(conn, b["id"], 0.52, 1, 1, "paper", "paper-sim")
    lv = approved_order(conn, live, size=4)
    orders.set_status(conn, lv["id"], "open", "executor", expected=("approved",))
    rejected = client.post("/api/v1/orders/request", json=s.body(size=50), headers=s.worker.headers).json()
    listed = client.get("/api/orders").json()
    assert [o["id"] for o in listed] == [rejected["order_id"], str(lv["id"]), str(b["id"]), str(a["id"])]
    assert listed[0]["reject_reason"] == "max_bet" and listed[0]["rationale"].startswith("my 0.58")
    assert listed[-1]["market_title"].endswith("home wins") and listed[-1]["worker_name"].startswith("trader")
    assert [o["id"] for o in client.get("/api/orders", params={"status": "active"}).json()] == [str(lv["id"]), str(b["id"]), str(a["id"])]
    assert len(client.get("/api/orders", params={"status": "rejected"}).json()) == 1
    assert client.get("/api/orders", params={"status": "weird"}).status_code == 400
    assert len(client.get("/api/orders", params={"limit": 1}).json()) == 1
    detail = client.get(f"/api/orders/{b['id']}").json()
    assert [e["to_status"] for e in detail["events"]] == ["approved", "open", "partial"] and len(detail["fills"]) == 1
    fills = client.get("/api/fills").json()
    assert len(fills) == 1 and fills[0]["order_id"] == str(b["id"]) and fills[0]["size"] == 1 and fills[0]["side"] == "home"
    r = client.post(f"/api/orders/{a['id']}/cancel")
    assert r.status_code == 200 and r.json() == {"status": "cancelled"}
    assert client.post(f"/api/orders/{uuid.uuid4()}/cancel").status_code == 404
    r = client.post("/api/cancel-all", json={"mode": "paper"})
    assert r.json() == {"cancelled": 1, "requested": 0}
    assert order_row(conn, b["id"])["status"] == "cancelled" and order_row(conn, lv["id"])["status"] == "open"
    r = client.post("/api/cancel-all")
    assert r.json() == {"cancelled": 0, "requested": 1}
    assert order_row(conn, lv["id"])["status"] == "cancel_requested"
    assert client.post("/api/cancel-all", json={"mode": "bogus"}).status_code == 400
    assert conn.execute("SELECT value FROM settings WHERE key = 'kill_switch'").fetchone()["value"] is False
    assert assignment_row(conn, s.assignment["id"])["status"] == "active", "cancel-all is not a kill"
    assert [a["action"] for a in client.get("/api/audit", params={"limit": 2}).json()] == ["cancel_all", "cancel_all"]
    assert ledger.replay_problems(conn) == []


def test_owner_markets_link_and_exchange_state(client, conn):
    insert_game(conn)
    insert_game(conn, OTHER_GAME, home="MIA", away="BUF")
    known = insert_market(conn)
    insert_snapshot(conn, known["id"])
    loose = conn.execute(
        "INSERT INTO markets (platform, market_ref, title, mapping_confidence) VALUES ('polymarket_us', 'm-1', 'Bills to win', 0.4) RETURNING *"
    ).fetchone()
    unmatched = client.get("/api/markets", params={"unmatched": 1}).json()
    assert [m["id"] for m in unmatched] == [str(loose["id"])] and unmatched[0]["snapshot_age_s"] is None
    everything = client.get("/api/markets").json()
    assert {m["id"] for m in everything} == {str(loose["id"]), str(known["id"])}
    assert next(m for m in everything if m["id"] == str(known["id"]))["snapshot_age_s"] < 5
    assert client.post(f"/api/markets/{loose['id']}/link", json={"game_id": OTHER_GAME, "side": "up"}).status_code == 400
    assert client.post(f"/api/markets/{loose['id']}/link", json={"game_id": "nope", "side": "away"}).status_code == 400
    assert client.post(f"/api/markets/{uuid.uuid4()}/link", json={"game_id": OTHER_GAME, "side": "away"}).status_code == 404
    r = client.post(f"/api/markets/{loose['id']}/link", json={"game_id": OTHER_GAME, "side": "away"})
    assert r.status_code == 200 and r.json()["mapping_confirmed"] is True and r.json()["mapping_confidence"] == 1.0
    assert r.json()["game_id"] == OTHER_GAME and r.json()["side"] == "away"
    assert client.get("/api/markets", params={"unmatched": 1}).json() == []
    assert client.get("/api/audit", params={"limit": 1}).json()[0]["action"] == "market_linked"
    state = client.get("/api/exchange").json()
    assert state["down"] is True and state["heartbeat_age_s"] is None and state["auth_ok"] is False and state["market_source"] is None
    conn.execute("UPDATE exchange_state SET heartbeat_at = now(), market_source = 'sim', last_error = 'boom'")
    state = client.get("/api/exchange").json()
    assert state["down"] is False and state["market_source"] == "sim" and state["last_error"] == "boom"


# ------------------------------------------------------------------ CLI


def test_cli_trading_commands(test_db_url, conn, monkeypatch, capsys):
    from host.cli import main

    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("FLEET_DEV", "1")

    def run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    insert_game(conn)
    insert_market(conn)
    model = insert_model(conn, status="paper_ok", params={"k": 21.0})
    code, out, _ = run("assign", GAME_ID, str(model["id"]), "--bankroll", "250", "--max-bet", "10")
    assert code == 0 and "mode=paper bankroll=$250.00" in out and f"game={GAME_ID}" in out
    row = conn.execute("SELECT * FROM assignments").fetchone()
    assert row["max_bet_cents"] == 1000 and row["created_by"] == "cli"
    code, _, err = run("assign", GAME_ID, str(model["id"]))
    assert code == 1 and "already has a paper assignment" in err
    code, _, err = run("assign", GAME_ID, str(model["id"]), "--bankroll", "abc")
    assert code == 1 and "bankroll" in err
    code, out, _ = run("assignments")
    assert code == 0 and str(row["id"])[:8] in out and "250.00" in out and "queued" in out
    s = trade_setup(conn, game_id=OTHER_GAME)
    a = approved_order(conn, s, size=2)
    code, out, _ = run("orders", "--status", "active")
    assert code == 0 and str(a["id"])[:8] in out and "approved" in out
    code, out, _ = run("orders", "--status", "nope")
    assert code == 1
    code, out, _ = run("cancel-all", "--mode", "paper")
    assert code == 0 and out.strip() == "cancelled=1 requested=0"
    assert order_row(conn, a["id"])["status"] == "cancelled"
    code, out, _ = run("ledger-check")
    assert code == 0 and "ledger ok: 2 bankroll(s)" in out
    conn.execute("UPDATE bankrolls SET available_cents = available_cents + 1 WHERE assignment_id = %s", (row["id"],))
    code, out, err = run("ledger-check")
    assert code == 1 and "cached" in out and "2 problem(s)" in err
    conn.execute("UPDATE exchange_state SET heartbeat_at = now(), market_source = 'sim'")
    code, out, _ = run("exchange-state")
    assert code == 0 and '"market_source": "sim"' in out and '"down": false' in out
    monkeypatch.setitem(sys.modules, "host.exchange.settle", None)
    code, _, err = run("simulate-final", GAME_ID, "--home", "21", "--away", "17")
    assert code == 1 and "exchange module not available" in err
