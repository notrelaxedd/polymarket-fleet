"""Step 5 phase of the end-to-end test (tests/test_e2e.py): live trading on the real
host, the real agent in the trade role and the real exchange loop (sim market source)
whose live gateway is the scripted FakeLiveGateway (tests/fake_gateway.py), injected
through ExchangeLoop(gateway_factory=...).

A second shifted game goes in through the ingest CLI; the exchange starts with
"credentials", its auth probe reads the fake balance into exchange_state; the owner
enables live through the Settings form (a wrong phrase first: 400 with the error
inline, then the exact dated phrase); the trained lineage is forced live_eligible in
the database (the gate itself is tested in test_settlement and test_limits); a live
assignment is created; the worker proposes, the approval passes buying power, the
executor places on the fake gateway and records the exchange id; a fill arrives
through the fills poll; a second order times out on place and is reconciled from the
exchange's open orders; an order that is not ours appears on the exchange: the
open-order audit cancels it and pulls the kill switch (auto-kill unknown_order, the
reason on the top bar, live off, the live order cancelled through the gateway);
RESUME; re-enable; the smoke order rests below the bid and is cancelled after a 1 s
hold; cancel-all --direct through the CLI entry point with the loop stopped; then
simulate-final settles the live assignment: a bets row and live P&L on /api/pnl.
"""
from __future__ import annotations

import contextlib
import io
import time
from pathlib import Path
from typing import Any, Callable

import psycopg

from host import db
from host.exchange import cli as exchange_cli
from host.exchange import main as exchange_main
from host.exchange.smoke import run_smoke
from tests.e2e_trading import (
    ExchangeThread, SimClock, order_in, order_status, orders_of, pick_minutes, rows, run_cli, set_min_edge,
    shifted_game_csv, trade_status,
)
from tests.fake_gateway import FakeCredentials, FakeLiveGateway

SOURCE_GAME = "2025_02_NYG_DAL"
LIVE_GAME = "2026_05_NYG_DAL"
BANKROLL_CENTS = 20_000
BALANCE_CENTS = 100_000
LIVE_SETTINGS = {"open_orders_audit_s": 5, "live_fills_poll_s": 1, "auth_probe_interval_s": 10, "min_edge": 1.0}
RECONCILE_TIMEOUT = 12.0
AUDIT_TIMEOUT = 10.0


def execute(host: Any, sql: str, params: tuple[Any, ...] = ()) -> None:
    """One statement straight on the host's database (forcing the lineage status)."""
    with psycopg.connect(host.database_url, autocommit=True) as conn:
        conn.execute(sql, params)


def live_state(host: Any) -> dict[str, Any]:
    return host.get("/api/live")


def phase_live(host: Any, state_dir: str, worker_id: str, agent: Any, models: dict[str, Any], tmp_path: Path,
               monkeypatch: Any, wait_for: Callable[..., Any], settled: Callable[..., Any]) -> None:
    started = time.monotonic()
    csv_path, game = shifted_game_csv(tmp_path, SOURCE_GAME, LIVE_GAME)
    out = run_cli(["ingest-games", "--file", str(csv_path)])
    assert "1 inserted" in out, out
    host.post("/api/settings", LIVE_SETTINGS)
    clock = SimClock()
    clock.minute = pick_minutes(clock, game)[0]
    gateway = FakeLiveGateway()
    gateway.balance_payload = {"balance_cents": BALANCE_CENTS, "buying_power_cents": BALANCE_CENTS, "server_time": None}
    monkeypatch.setenv("DATABASE_URL", host.database_url)
    exchange = ExchangeThread(host.database_url, clock, gateway=gateway).start()
    try:
        _run(host, state_dir, worker_id, agent, models["child"], gateway, exchange, monkeypatch, wait_for, settled)
    finally:
        exchange.close()
    assert time.monotonic() - started < 100.0, "the live phase stays inside the e2e budget"


def _enable_live(host: Any, phrase: str) -> None:
    """The Settings form: a wrong phrase is a 400 with the error inline (nothing
    changes), the exact dated phrase turns live on with a flash."""
    wrong = host.client.post("/settings/live", data={"confirm": "ENABLE LIVE TRADING"}, headers={"Origin": host.url}, follow_redirects=False)
    assert wrong.status_code == 400 and "text/html" in wrong.headers["content-type"]
    assert f'<p class="error inline-error">confirmation must be exactly &#34;{phrase}&#34;</p>' in wrong.text
    assert 'name="confirm" value="ENABLE LIVE TRADING"' in wrong.text, "the typed text is kept"
    assert live_state(host)["live_enabled"] is False
    resp = host.form("/settings/live", {"confirm": phrase})
    assert resp.headers["location"] == "/settings#live"
    state = live_state(host)
    assert state["live_enabled"] is True and state["live_enabled_by"] == "dev" and state["live_enabled_at"]
    topbar = host.client.get("/fragments/topbar").text
    assert 'class="pill live">LIVE</span>' in topbar and ">PAPER<" not in topbar


def _run(host: Any, state_dir: str, worker_id: str, agent: Any, model_id: str, gw: FakeLiveGateway,
         exchange: ExchangeThread, monkeypatch: Any, wait_for: Callable[..., Any], settled: Callable[..., Any]) -> None:
    # The exchange starts with credentials: the auth probe reads the fake balance.
    state = wait_for(lambda: (s := live_state(host))["auth_ok"] and s["credentials_present"] and s, "auth probe recorded")
    assert state["balance_cents"] == BALANCE_CENTS and state["buying_power_cents"] == BALANCE_CENTS
    assert state["clock_skew_ms"] == 0 and state["auth_failures"] == 0 and state["problems"] == []
    assert state["live_enabled"] is False and gw.calls["balance"] >= 1 and gw.calls["open_orders"] >= 1, "startup audit ran"
    markets = wait_for(
        lambda: (ms := host.get(f"/api/markets?game_id={LIVE_GAME}")) and len(ms) == 2
        and all(m["mapping_confirmed"] and m["snapshot_age_s"] is not None for m in ms) and ms,
        "two sim markets with snapshots for the live game",
    )
    away = next(m for m in markets if m["side"] == "away")
    page = host.client.get("/settings").text
    assert "<dt>credentials</dt><dd>yes " in page and '<span class="chip chip-ok">ok</span>' in page

    # The typed switch, then the lineage forced live_eligible and a live assignment.
    phrase = state["expected_phrase"]
    _enable_live(host, phrase)
    execute(host, "UPDATE models SET status = 'live_eligible', updated_at = now() WHERE lineage_id = (SELECT lineage_id FROM models WHERE id = %s)", (model_id,))
    created = host.post("/api/assignments", {"game_id": LIVE_GAME, "model_id": model_id, "mode": "live", "bankroll_cents": BANKROLL_CENTS}, expect=201)
    aid, job_id = created["id"], created["job_id"]
    assert created["mode"] == "live" and created["status"] == "active" and created["bankroll"]["available_cents"] == BANKROLL_CENTS
    assert 'class="is-live' in host.client.get("/trading").text or "is-live" in host.client.get("/trading").text

    # The worker claims the live trade job; the approval passes buying power; the
    # executor places on the fake gateway and records the exchange id.
    host.set_role(worker_id, "trade")
    wait_for(settled(host, worker_id, "trade"), "worker in the trade role")
    wait_for(lambda: (j := host.job(job_id))["status"] == "leased" and j["lease_worker_id"] == worker_id, "live trade job claimed")
    set_min_edge(host, 0.0)
    first = wait_for(order_in(host, aid, "open"), "first live order placed and open", timeout=15.0)
    set_min_edge(host, 1.0)
    assert first["mode"] == "live" and first["exchange_order_id"] == "ex-1" and first["worker_id"] == worker_id
    assert gw.placed == [first["client_request_id"]] and [o["client_order_id"] for o in gw.open_orders()] == [first["client_request_id"]]
    assert [e["to_status"] for e in host.get(f"/api/orders/{first['id']}")["events"]] == ["approved", "submitting", "open"]
    assert host.get(f"/api/assignments/{aid}")["bankroll"]["reserved_cents"] == first["cost_cents"]
    assert first["cost_cents"] <= BALANCE_CENTS, "the approval was checked against the fake buying power"
    assert first["exchange_order_id"] in host.client.get("/fragments/trading").text

    # A fill arrives through the fills poll (idempotent on the exchange fill id).
    fill = gw.add_fill(first["client_request_id"], float(first["price"]), int(first["size"]))
    wait_for(lambda: order_status(host, first["id"]) == "filled", "first live order filled from the exchange", timeout=8.0)
    fills = [f for f in host.get("/api/fills?limit=50") if f["order_id"] == first["id"]]
    assert len(fills) == 1 and fills[0]["size"] == first["size"] and fills[0]["mode"] == "live"
    assert rows(host, "SELECT exchange_fill_id FROM fills WHERE order_id = %s", (first["id"],))[0]["exchange_fill_id"] == fill["exchange_fill_id"]
    time.sleep(2.0)  # two more polls see the same fill and record nothing
    assert len([f for f in host.get("/api/fills?limit=50") if f["order_id"] == first["id"]]) == 1
    detail = host.get(f"/api/assignments/{aid}")
    assert detail["positions"][0]["size"] == first["size"] and detail["bankroll"]["reserved_cents"] == 0
    assert "ledger ok" in run_cli(["ledger-check"])

    # The second place times out after the exchange accepted it: the row stays
    # submitting and is reconciled from open_orders() (exchange id adopted).
    gw.place_mode = "timeout"
    set_min_edge(host, 0.0)
    second = wait_for(order_in(host, aid, "submitting", exclude={first["id"]}), "second live order left submitting", timeout=15.0)
    set_min_edge(host, 1.0)
    gw.place_mode = "ok"
    assert second["exchange_order_id"] is None and gw.placed[-1] == second["client_request_id"]
    opened = wait_for(lambda: (o := host.get(f"/api/orders/{second['id']}"))["status"] == "open" and o, "second order reconciled to open", timeout=RECONCILE_TIMEOUT)
    assert opened["exchange_order_id"] == "ex-2" and opened["events"][-1]["detail"]["reconciled"] is True
    assert gw.placed.count(second["client_request_id"]) == 1, "never resubmitted"

    # An order that is not ours appears on the exchange: cancelled, auto-kill.
    stranger = gw.add_remote_order(client_id="not-ours-1", market_ref=away["market_ref"], price=0.4, size=3)
    wait_for(lambda: host.get("/api/settings")["kill_switch"] is True, "auto-kill after the open-order audit", timeout=AUDIT_TIMEOUT)
    assert stranger["exchange_order_id"] in gw.cancelled and stranger["exchange_order_id"] not in gw.remote
    state = live_state(host)
    assert state["live_enabled"] is False and state["killed"] is True and state["auto_kill_reasons"] == ["unknown_order"]
    topbar = host.client.get("/fragments/topbar").text
    assert 'data-auto-kill="unknown_order"' in topbar and "TRADING KILLED automatically: " in topbar
    assert 'data-auto-kill="unknown_order"' in host.client.get("/").text
    assert 'auto-kill-reason">unknown_order</span>' in host.client.get("/settings").text
    audit = host.get("/api/audit?limit=5")
    assert audit[0]["action"] == "auto_kill" and audit[0]["actor"] == "auto:unknown_order"
    assert audit[0]["after"]["orders"][0]["exchange_order_id"] == stranger["exchange_order_id"]
    assert [a["action"] for a in audit[:3]] == ["auto_kill", "kill_cancel_all", "kill"]
    assert audit[1]["after"]["orders_cancel_requested"] == [second["id"]] and audit[1]["after"]["live_enabled"] is False
    cancelled = wait_for(lambda: (o := host.get(f"/api/orders/{second['id']}"))["status"] == "cancelled" and o, "live order cancelled through the gateway")
    assert "ex-2" in gw.cancelled and gw.remote == {} and cancelled["filled_size"] == 0
    assert [e["to_status"] for e in cancelled["events"]] == ["approved", "submitting", "open", "cancel_requested", "cancelled"]
    assert host.get(f"/api/assignments/{aid}")["status"] == "halted" and host.get(f"/api/assignments/{aid}")["bankroll"]["reserved_cents"] == 0
    wait_for(lambda: trade_status(state_dir).get("last_tick", {}).get("kill") is True, "the worker's tick sees the auto-kill")
    assert "ledger ok" in run_cli(["ledger-check"])

    # RESUME clears the flag only; live stays off until typed again.
    host.form("/kill/reset", {"confirm": "RESUME"})
    state = live_state(host)
    assert state["killed"] is False and state["live_enabled"] is False and state["auto_kill_reasons"] == []
    assert 'data-auto-kill' not in host.client.get("/fragments/topbar").text
    _enable_live(host, phrase)
    wait_for(lambda: trade_status(state_dir).get("last_tick", {}).get("kill") is False, "the worker's tick sees the reset")

    # The smoke order: one contract 5 cents under the bid, resting, cancelled after 1 s.
    pool = db.make_pool(host.database_url, min_size=1, max_size=2)
    try:
        smoke = run_smoke(pool, "SMOKE " + phrase[-10:], market_id=away["id"], hold_seconds=1, gateway=gw)
    finally:
        pool.close()
    assert smoke["approved"] and smoke["status"] == "cancelled" and smoke["size"] == 1, smoke
    assert smoke["price"] == max(0.01, round(float(away["best_bid"]) - 0.05, 2)) or smoke["price"] < float(away["best_bid"])
    assert [e["to_status"] for e in smoke["timeline"]] == ["approved", "submitting", "open", "cancel_requested", "cancelled"]
    assert smoke["exchange_order_id"] in gw.cancelled and gw.remote == {}
    smoke_row = host.get(f"/api/orders/{smoke['order_id']}")
    assert smoke_row["kind"] == "smoke" and smoke_row["mode"] == "live" and smoke_row["assignment_id"] is None
    assert "chip-smoke" in host.client.get("/trading").text
    assert host.get("/api/audit?limit=20")[0]["action"] == "smoke_order" or any(a["action"] == "smoke_order" for a in host.get("/api/audit?limit=20"))

    # The assignment is activated again; a third order rests on the exchange when the
    # loop stops; cancel-all --direct (the CLI entry point) cancels it on the exchange
    # and closes the row.
    assert host.post(f"/api/assignments/{aid}/activate")["status"] == "active"
    set_min_edge(host, 0.0)
    third = wait_for(order_in(host, aid, "open", exclude={first["id"], second["id"]}), "third live order open", timeout=15.0)
    set_min_edge(host, 1.0)
    assert third["exchange_order_id"] in gw.remote
    exchange.close()
    monkeypatch.setattr(exchange_main, "load_credentials", lambda: FakeCredentials())
    monkeypatch.setattr(exchange_main, "build_live_gateway", lambda config, creds, limiter=None: gw)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert exchange_cli.main(["cancel-all", "--direct"]) == 0, out.getvalue()
    text = out.getvalue()
    assert third["exchange_order_id"] in text and "remote=1 cancelled=1 rows_closed=1 still_open=0" in text, text
    assert gw.remote == {} and third["exchange_order_id"] in gw.cancelled
    closed = host.get(f"/api/orders/{third['id']}")
    assert closed["status"] == "cancelled" and closed["events"][-1]["detail"] == {"reason": "cancel-all --direct"}
    assert host.get(f"/api/assignments/{aid}")["bankroll"]["reserved_cents"] == 0
    direct = next(a for a in host.get("/api/audit?limit=10") if a["action"] == "cancel_all")
    assert direct["after"]["direct"] is True and direct["actor"] == "cli"
    assert "ledger ok" in run_cli(["ledger-check"])

    # simulate-final (sim source): the filled live order becomes a bet; live P&L.
    side = next(m["side"] for m in markets if m["id"] == first["market_id"])
    scores = ["--home", "17", "--away", "27"] if side == "away" else ["--home", "27", "--away", "17"]
    paper_pnl = host.get("/api/pnl")["by_mode"]["paper"]
    out = run_cli(["simulate-final", LIVE_GAME] + scores)
    assert f'"winner": "{side}"' in out and '"assignments": 1' in out and '"bets": 1' in out, out
    bets = rows(host, "SELECT * FROM bets WHERE assignment_id = %s", (aid,))
    assert len(bets) == 1 and bets[0]["order_id"] == first["id"] and bets[0]["mode"] == "live" and bets[0]["result"] == "win"
    assert bets[0]["pnl_cents"] > 0 and bets[0]["clv"] is not None and bets[0]["worker_id"] == worker_id
    pnl_cents = bets[0]["pnl_cents"]
    settled_row = host.get(f"/api/assignments/{aid}")
    assert settled_row["status"] == "settled" and settled_row["bankroll"]["realized_pnl_cents"] == pnl_cents
    assert settled_row["bankroll"]["available_cents"] == BANKROLL_CENTS + pnl_cents
    assert host.job(job_id)["status"] == "succeeded" and host.job(job_id)["result"]["n_bets"] == 1
    pnl = host.get("/api/pnl")
    assert pnl["by_mode"]["live"] == {"today_cents": pnl_cents, "all_time_cents": pnl_cents}
    assert pnl["by_mode"]["paper"] == paper_pnl and pnl["all_time_cents"] == pnl_cents + paper_pnl["all_time_cents"]
    dollars = f"${pnl_cents // 100}.{pnl_cents % 100:02d}"
    assert f"live today {dollars} &middot; all {dollars}" in host.client.get("/fragments/topbar").text
    score = rows(host, "SELECT * FROM model_scores WHERE game_id = %s", (LIVE_GAME,))
    assert len(score) == 1 and score[0]["mode"] == "live" and score[0]["n_bets"] == 1
    assert sorted(o["status"] for o in orders_of(host, aid) if o["status"] != "rejected") == ["cancelled", "cancelled", "filled"]
    assert "ledger ok" in run_cli(["ledger-check"])

    # Live off for the crash phase; the worker drops the finished job and goes idle.
    host.form("/settings/live/off")
    assert live_state(host)["live_enabled"] is False
    wait_for(lambda: host.worker(worker_id)["current_jobs"] == [], "live trade job dropped by the worker")
    host.set_role(worker_id, "idle")
    wait_for(settled(host, worker_id, "idle"), "worker idle after live trading")
