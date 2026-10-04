"""The smoke order (docs/LIVE.md "Smoke order") on the FakeLiveGateway."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from host import kill
from host.errors import BadRequest, Conflict
from host.exchange import smoke
from host.trading import ledger, limits, views
from host.trading.positions import owner_tz
from tests.conftest import (
    GAME_ID, audit_rows, bankroll_of, enable_live, insert_game, insert_market, insert_snapshot, order_events, order_row,
    set_setting, trade_setup,
)
from tests.fake_gateway import FakeLiveGateway

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def phrase(conn) -> str:
    return "SMOKE " + datetime.now(timezone.utc).astimezone(owner_tz(conn)).date().isoformat()


def live_market(conn, bid: float = 0.50, ask: float = 0.52, game_id: str = GAME_ID, liquidity: int = 300_000) -> dict:
    insert_game(conn, game_id)
    market = insert_market(conn, game_id, platform="polymarket_us")
    insert_snapshot(conn, market["id"], bid=bid, ask=ask, liquidity_usd_cents=liquidity)
    return conn.execute("SELECT * FROM markets WHERE id = %s", (market["id"],)).fetchone()


def test_smoke_requires_live_auth_and_phrase(pool, conn):
    live_market(conn)
    gw = FakeLiveGateway()
    with pytest.raises(Conflict, match="live trading is off"):
        smoke.run_smoke(pool, phrase(conn), gateway=gw)
    enable_live(conn)
    for text in (phrase(conn).lower(), phrase(conn) + " ", "SMOKE", phrase(conn)[:-1] + "9", ""):
        with pytest.raises(BadRequest, match="SMOKE"):
            smoke.run_smoke(pool, text, gateway=gw)
    conn.execute("UPDATE exchange_state SET auth_ok = false")
    with pytest.raises(Conflict, match="auth_ok"):
        smoke.run_smoke(pool, phrase(conn), gateway=gw)
    enable_live(conn)
    kill.set_kill(conn, "owner")
    with pytest.raises(Conflict, match="kill"):
        smoke.run_smoke(pool, phrase(conn), gateway=gw)
    assert gw.calls == {} and conn.execute("SELECT count(*) AS n FROM orders").fetchone()["n"] == 0
    assert audit_rows(conn, "smoke_order") == []


def test_smoke_rests_below_bid_and_is_cancelled_after_hold(pool, conn):
    enable_live(conn)
    market = live_market(conn, bid=0.50, ask=0.52)
    live_market(conn, bid=0.60, ask=0.62, game_id="2026_05_BUF_MIA", liquidity=10_000)
    gw = FakeLiveGateway(clock=lambda: NOW)
    sleeps: list[float] = []
    result = smoke.run_smoke(pool, phrase(conn), hold_seconds=3, gateway=gw, now=NOW, sleep=sleeps.append)
    assert result["approved"] and result["status"] == "cancelled" and result["reason"] is None
    assert result["market_id"] == str(market["id"]), "the most liquid live-tradable market"
    assert result["price"] == 0.45 and result["size"] == 1 and result["exchange_order_id"] == "ex-1"
    assert [e["to_status"] for e in result["timeline"]] == ["approved", "submitting", "open", "cancel_requested", "cancelled"]
    assert sleeps == [3] and gw.placed and gw.cancelled == ["ex-1"] and gw.remote == {}
    row = order_row(conn, result["order_id"])
    assert row["kind"] == "smoke" and row["mode"] == "live" and row["assignment_id"] is None and row["worker_id"] is None
    assert float(row["price"]) == 0.45 and row["size"] == 1 and 45 <= row["cost_cents"] <= 47, "price plus the taker fee"
    assert row["client_request_id"].startswith("smoke-") and row["status"] == "cancelled"
    assert order_events(conn, row["id"]) == ["approved", "submitting", "open", "cancel_requested", "cancelled"]
    audit = audit_rows(conn, "smoke_order")[-1]
    assert audit["confirmation_text"] == phrase(conn) and audit["after"]["price"] == 0.45 and audit["actor"] == "cli"
    listed = [o for o in views.list_orders(conn, limit=10) if o["id"] == row["id"]]
    assert listed and listed[0]["kind"] == "smoke", "visible on /trading flagged smoke"
    # a chosen market, a bid near the floor and the configured hold
    low = live_market(conn, bid=0.03, ask=0.05, game_id="2026_05_DAL_PHI")
    set_setting(conn, "smoke_hold_seconds", 7)
    sleeps.clear()
    result = smoke.run_smoke(pool, phrase(conn), market_id=low["id"], gateway=gw, now=NOW, sleep=sleeps.append)
    assert result["price"] == 0.01 and result["status"] == "cancelled" and result["hold_seconds"] == 7 and sleeps == [7]
    with pytest.raises(Conflict):
        smoke.run_smoke(pool, phrase(conn), market_id=insert_market(conn, GAME_ID, confirmed=False)["id"], gateway=gw)


def test_smoke_touches_no_bankroll(pool, conn):
    live = trade_setup(conn, mode="live", model_status="live_eligible")
    paper = trade_setup(conn, game_id="2026_05_BUF_MIA")
    market = live_market(conn, game_id="2026_05_DAL_PHI")
    before = {a["id"]: bankroll_of(conn, a) for a in (live.assignment, paper.assignment)}
    ledger_rows = conn.execute("SELECT count(*) AS n FROM ledger").fetchone()["n"]
    gw = FakeLiveGateway(clock=lambda: NOW)
    result = smoke.run_smoke(pool, phrase(conn), market_id=market["id"], hold_seconds=1, gateway=gw, now=NOW, sleep=lambda s: None)
    assert result["status"] == "cancelled"
    for a in (live.assignment, paper.assignment):
        assert bankroll_of(conn, a) == before[a["id"]]
    assert conn.execute("SELECT count(*) AS n FROM ledger").fetchone()["n"] == ledger_rows, "no reserve, no release"
    assert conn.execute("SELECT count(*) AS n FROM bankrolls").fetchone()["n"] == 2
    assert ledger.replay_problems(conn) == []
    # even a fill on the smoke order moves no bankroll money
    row = order_row(conn, result["order_id"])
    assert row["assignment_id"] is None
    from host.trading import orders

    assert orders.remaining_reservation_cents(conn, row) == 0


def test_smoke_goes_through_max_bet_and_kill(pool, conn):
    enable_live(conn)
    market = live_market(conn)
    gw = FakeLiveGateway(clock=lambda: NOW)
    set_setting(conn, "max_bet_cents", 10)
    result = smoke.run_smoke(pool, phrase(conn), market_id=market["id"], gateway=gw, now=NOW, sleep=lambda s: None)
    assert result["approved"] is False and result["reason"] == "max_bet" and result["status"] == "rejected"
    assert gw.calls == {} and order_row(conn, result["order_id"])["reject_reason"] == "max_bet"
    assert [e["to_status"] for e in result["timeline"]] == ["rejected"]
    set_setting(conn, "max_bet_cents", 5000)
    conn.execute("UPDATE exchange_state SET buying_power_cents = 10")
    assert limits.approve_smoke(conn, market, 0.45, 1, "cli")["reason"] == "buying_power"
    conn.execute("UPDATE exchange_state SET buying_power_cents = 10000, balance_checked_at = now() - interval '10 minutes'")
    assert limits.approve_smoke(conn, market, 0.45, 1, "cli")["reason"] == "buying_power", "stale buying power"
    enable_live(conn)
    assert limits.approve_smoke(conn, market, 0.60, 1, "cli")["reason"] == "price_band", "above the ask + 0.05"
    conn.execute("UPDATE exchange_state SET auth_ok = false")
    assert limits.approve_smoke(conn, market, 0.45, 1, "cli")["reason"] == "mode"
    enable_live(conn)
    kill.set_kill(conn, "owner")
    assert limits.approve_smoke(conn, market, 0.45, 1, "cli")["reason"] == "killed"
    with pytest.raises(Conflict, match="kill"):
        smoke.run_smoke(pool, phrase(conn), market_id=market["id"], gateway=gw, now=NOW, sleep=lambda s: None)
    kill.reset_kill(conn, "owner", "RESUME")
    assert limits.approve_smoke(conn, market, 0.45, 1, "cli")["reason"] == "mode", "the kill turned live off"


# ------------------------------------------------------------------ the exchange CLI

@pytest.fixture
def xcli(test_db_url: str, monkeypatch, capsys):
    """python -m host.exchange.cli against the per-test database with the fake gateway."""
    from host.exchange import cli

    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("FLEET_DEV", "1")
    gw = FakeLiveGateway(clock=lambda: NOW)
    state = {"creds": object()}
    monkeypatch.setattr(cli, "build_gateway", lambda config: (gw, state["creds"]))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr("host.exchange.smoke.time.sleep", lambda s: None)

    def run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    run.gateway = gw  # type: ignore[attr-defined]
    run.state = state  # type: ignore[attr-defined]
    return run


def test_cli_smoke_cancel_all_direct_probe_and_auth_check(xcli, conn):
    enable_live(conn)
    market = live_market(conn)
    gw = xcli.gateway
    code, out, err = xcli("exchange-smoke", "--confirm", "SMOKE 1999-01-01", "--drive")
    assert code == 1 and "SMOKE" in err and "confirmation" in err
    code, out, _ = xcli("exchange-smoke", "--confirm", phrase(conn), "--market", str(market["id"]), "--hold", "1", "--drive")
    assert code == 0 and "cancelled" in out and "exchange_order_id=ex-1" in out and "cancel_requested" in out
    assert gw.remote == {} and conn.execute("SELECT count(*) AS n FROM orders WHERE kind = 'smoke'").fetchone()["n"] == 1
    # cancel-all --direct: a resting live order of ours and a stranger
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id="2026_05_BUF_MIA")
    from host.exchange.adapters.base import PaperGateway
    from host.exchange.executor import Executor
    from tests.conftest import approved_order

    row = approved_order(conn, live, size=2)
    Executor(PaperGateway(), gw).tick(conn, NOW)
    gw.add_remote_order(client_id="stranger", exchange_order_id="ex-s")
    code, out, _ = xcli("cancel-all", "--direct")
    assert code == 0 and "ex-s" in out and "remote=2 cancelled=2 rows_closed=1 still_open=0" in out
    assert order_row(conn, row["id"])["status"] == "cancelled" and gw.remote == {}
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []
    code, out, _ = xcli("cancel-all")
    assert code == 0 and out.strip() == "cancelled=0 requested=0", "without --direct: the database cancel-all"
    code, out, _ = xcli("probe-account")
    assert code == 0 and '"key_present": true' in out and '"key_hint": "ab12"' in out and "balance" in out
    gw.fail_next("balance", RuntimeError("boom"))
    code, out, _ = xcli("auth-check")
    assert code == 0 and '"auth_ok": false' in out and "boom" in out
    assert conn.execute("SELECT auth_failures FROM exchange_state").fetchone()["auth_failures"] == 1
    code, out, _ = xcli("auth-check")
    assert '"auth_ok": true' in out and conn.execute("SELECT auth_failures FROM exchange_state").fetchone()["auth_failures"] == 0
    xcli.state["creds"] = None
    code, _, err = xcli("exchange-smoke", "--confirm", phrase(conn), "--drive")
    assert code == 1 and "no credentials" in err
    code, out, _ = xcli("probe-account")
    assert code == 0 and '"key_present": false' in out
