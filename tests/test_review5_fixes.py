"""Regressions for the step 5 review findings (live safety, gateway, operability):
fills are read before any live row is closed and a failed fills call closes nothing,
a live expiry goes through the cancel path, the signed destination and template are
pinned, a clock skew pauses placements only, demotions halt live assignments,
response bodies are redacted, the direct cancel-all turns live off first, buying
power counts fills since the probe, the smoke order cleans up after itself, live
orders stay on the live platform, and the dashboard shows what is pending."""
from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from host import kill
from host.eligibility import recompute_all
from host.errors import BadRequest, Conflict
from host.exchange import live_sync, smoke
from host.exchange.adapters import live_parse
from host.exchange.adapters.base import NotConfigured, PaperGateway, RateLimited, SourceError
from host.exchange.adapters.live_policy import base_url_problem, template_problem
from host.exchange.adapters.polymarket_us import live_config_with_defaults
from host.exchange.adapters.polymarket_us_live import AuthError, LiveGateway
from host.exchange.credentials import Credentials, CredentialsError
from host.exchange.executor import Executor
from host.exchange.ratelimit import RateLimiter
from host.models import retire
from host.settings import set_settings
from host.trading import assignments, ledger, orders
from host.trading.live import live_state
from host.trading.positions import owner_tz
from tests.conftest import (
    GAME_ID, approve, approved_order, assignment_row, audit_rows, auth_state, bankroll_of, enable_live, insert_game,
    insert_market, insert_model, insert_snapshot, order_events, order_row, set_setting, trade_setup,
)
from tests.fake_gateway import FakeCredentials, FakeLiveGateway, live_loop

NOW = datetime.now(timezone.utc).replace(microsecond=0)
LIVE_GAME = "2026_05_BUF_MIA"
KEY = "KEY-abcdef-1234"
SEED = bytes(range(32))
ROOT = Path(__file__).resolve().parent.parent


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


@pytest.fixture
def gw() -> FakeLiveGateway:
    return FakeLiveGateway(clock=lambda: NOW)


@pytest.fixture
def live(conn):
    return trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME)


def fills_of(conn, order_id) -> list[dict]:
    return conn.execute("SELECT * FROM fills WHERE order_id = %s ORDER BY id", (order_id,)).fetchall()


def live_enabled(conn) -> bool:
    return conn.execute("SELECT value FROM settings WHERE key = 'live_enabled'").fetchone()["value"] is True


def _numbered(inner):
    """Wrap a transport so every place answer carries a fresh exchange order id."""
    calls = inner.calls

    def http(method, url, headers_in, body, timeout):
        status, headers, text = inner(method, url, headers_in, body, timeout)
        return status, headers, text.replace('"ex-a"', f'"ex-{len(calls)}"')

    http.calls = calls  # type: ignore[attr-defined]
    return http


def http_answering(status: int, text: str, headers: dict[str, str] | None = None):
    calls: list[dict[str, Any]] = []

    def http(method, url, headers_in, body, timeout):
        calls.append({"method": method, "url": url, "headers": dict(headers_in), "body": body})
        return status, headers or {"Date": "Sat, 03 Oct 2026 15:00:01 GMT"}, text

    http.calls = calls  # type: ignore[attr-defined]
    return http


# ------------------------------------------------- HIGH: fills before any close

def test_expiry_absorbs_a_fill_that_landed_before_gtd(conn, gw, live):
    row = approved_order(conn, live, size=5)
    ex = Executor(PaperGateway(), gw)
    ex.tick(conn, NOW)
    gtd = order_row(conn, row["id"])["gtd_at"]
    gw.add_fill(row["client_request_id"], 0.52, 5, fee_cents=1, fill_id="f-late")  # filled 1 s before gtd, not yet polled
    ex.tick(conn, gtd + timedelta(seconds=1))
    after = order_row(conn, row["id"])
    assert after["status"] == "filled" and after["filled_size"] == 5 and [f["exchange_fill_id"] for f in fills_of(conn, row["id"])] == ["f-late"]
    bank = bankroll_of(conn, live.assignment)
    assert bank["open_cost_cents"] == 260 and bank["reserved_cents"] == 0 and ledger.replay_problems(conn) == []
    assert audit_rows(conn, "auto_kill") == [] and order_events(conn, row["id"]) == ["approved", "submitting", "open", "cancel_requested", "filled"]
    # an unfilled expiry: cancel_requested (reason gtd), then expired once the exchange no longer lists it
    second = approved_order(conn, live, size=2)
    ex.tick(conn, at(10))
    gtd = order_row(conn, second["id"])["gtd_at"]
    gw.cancel_results = ["ack_only"]
    ex.tick(conn, gtd)
    assert order_row(conn, second["id"])["status"] == "cancel_requested" and gw.remote, "acknowledged but still listed: not closed"
    assert live_sync.audit_open_orders(conn, gw, gtd + timedelta(seconds=1))["unknown"] == [] and not kill.is_killed(conn), "our own order is never unknown"
    ex.tick(conn, gtd + timedelta(seconds=2))
    assert order_row(conn, second["id"])["status"] == "expired" and gw.remote == {}
    assert order_events(conn, second["id"])[-2:] == ["cancel_requested", "expired"]
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []


def test_failed_fills_call_closes_nothing(conn, gw, live):
    # the open-order audit
    row = approved_order(conn, live, size=5)
    ex = Executor(PaperGateway(), gw)
    ex.tick(conn, NOW)
    gw.add_fill(row["client_request_id"], 0.52, 5, fee_cents=1, fill_id="f-x")  # fully filled: gone from the open list
    gw.fail_next("fills", RateLimited("429"))
    out = live_sync.audit_open_orders(conn, gw, at(3))
    assert out["closed"] == {} and "fills unavailable" in out["error"]
    assert order_row(conn, row["id"])["status"] == "open" and bankroll_of(conn, live.assignment)["reserved_cents"] == row["cost_cents"]
    assert live_sync.audit_open_orders(conn, gw, at(4))["closed"] == {str(row["id"]): "filled"}, "the next pass closes it from its fill"
    assert bankroll_of(conn, live.assignment)["open_cost_cents"] == 260
    # the submitting grace
    stuck = approved_order(conn, live, size=2)
    gw.place_mode = "timeout"
    ex.tick(conn, at(5))
    gw.place_mode = "ok"
    gw.remote.pop(next(e for e, o in gw.remote.items() if o["client_order_id"] == stuck["client_request_id"]))
    gw.add_fill(stuck["client_request_id"], 0.52, 2, exchange_order_id="ex-stuck", fill_id="f-y")
    gw.fail_next("fills", RuntimeError("503"))
    ex.tick(conn, at(70))
    assert order_row(conn, stuck["id"])["status"] == "submitting", "the grace does not expire a row while fills are unavailable"
    ex.tick(conn, at(71))
    assert order_row(conn, stuck["id"])["status"] == "filled"
    # the cancel confirmation
    third = approved_order(conn, live, size=3)
    ex.tick(conn, at(80))
    orders.cancel_order(conn, third["id"], "owner", "owner cancel")
    gw.add_fill(third["client_request_id"], 0.52, 1, fill_id="f-z", ts=at(80.5))
    gw.fail_next("fills", TimeoutError("fills timed out"))
    ex.tick(conn, at(81))
    assert order_row(conn, third["id"])["status"] == "cancel_requested" and gw.calls["cancel"] == 1
    ex.tick(conn, at(83))
    after = order_row(conn, third["id"])
    assert after["status"] == "cancelled" and after["filled_size"] == 1
    assert ledger.replay_problems(conn) == [] and audit_rows(conn, "auto_kill") == []


def test_direct_cancel_all_with_fills_down_leaves_rows_for_the_exchange(conn, gw, live):
    row = approved_order(conn, live, size=2)
    Executor(PaperGateway(), gw).tick(conn, NOW)
    gw.fail_next("fills", RateLimited("429"))
    out = live_sync.cancel_all_direct(conn, gw, at(1), lambda s: None, "cli")
    assert out["rows_closed"] == {} and "fills unavailable" in out["error"] and gw.remote == {}
    assert order_row(conn, row["id"])["status"] == "cancel_requested" and not live_enabled(conn)
    assert audit_rows(conn, "cancel_all")[-1]["after"]["error"].startswith("fills unavailable")
    Executor(PaperGateway(), gw).tick(conn, at(2))
    assert order_row(conn, row["id"])["status"] == "cancelled", "the exchange process confirms it on its next pass"


def test_late_fill_for_a_closed_row_auto_kills(conn, gw, live):
    row = approved_order(conn, live, size=5)
    ex = Executor(PaperGateway(), gw)
    ex.tick(conn, NOW)
    orders.cancel_order(conn, row["id"], "owner", "owner cancel")
    ex.tick(conn, at(1))
    assert order_row(conn, row["id"])["status"] == "cancelled"
    gw.add_fill(row["client_request_id"], 0.52, 2, exchange_order_id="ex-1", fill_id="f-after")
    assert live_sync.poll_fills(conn, gw, at(2)) == 0, "a just-closed row is still polled"
    assert kill.is_killed(conn) and live_state(conn)["auto_kill_reasons"] == ["late_fill"]
    detail = audit_rows(conn, "auto_kill")[-1]["after"]
    assert detail["order_id"] == str(row["id"]) and detail["fill_id"] == "f-after" and detail["status"] == "cancelled"


# ---------------------------------- HIGH: the database cannot redirect signing

def test_settings_cannot_redirect_or_forge_the_signed_request(conn):
    forged = '1999999999999POST/v1/orders{"client_order_id":"x","size":100000}'
    for block in (
        {"live": {"base_url": "http://collector.example"}},
        {"live": {"base_url": "https://collector.example"}},
        {"live": {"base_url": "https://polymarket.us.evil.example"}},
        {"auth": {"template": forged.replace("{", "{{").replace("}", "}}")}},
        {"auth": {"template": "{timestamp}{method}{path}{body}{body}"}},
        {"auth": {"template": "{timestamp}{method}{path}"}},
        {"auth": {"template": "{timestamp}{method}{path}{body}{other}"}},
    ):
        with pytest.raises(BadRequest, match="polymarket_us"):
            set_settings(conn, {"market_source_config": {"polymarket_us": block}}, "owner")
    set_settings(conn, {"market_source_config": {"polymarket_us": {
        "live": {"base_url": "https://sandbox.polymarket.us/"}, "auth": {"template": "{method}|{path}|{timestamp}|{body}"},
    }}}, "owner")
    assert base_url_problem("https://api.polymarket.us") is None and base_url_problem("https://x.example", "x.example") is None
    assert template_problem("{timestamp}{method}{path}{body}") is None and template_problem("{timestamp}{method}{path}{body}" + "-" * 9)
    # the gateway refuses the same at signing time, whatever the settings row holds
    creds = Credentials(key=KEY, secret=SEED, passphrase="PASS-xyz")
    http = http_answering(200, '{"balance": "10.00", "buying_power": "10.00"}')
    gw = LiveGateway(creds, {"live": {"base_url": "http://collector.example"}}, http=http)
    with pytest.raises(SourceError, match="refused"):
        gw.balance()
    with pytest.raises(ValueError, match="signing refused"):
        LiveGateway(creds, {"auth": {"template": forged.replace("{", "{{").replace("}", "}}")}}, http=http).balance()
    assert http.calls == [], "nothing was sent either way"
    # exchange.env may name another https host (a sandbox); the database may not
    gw = LiveGateway(creds, {"live": {"base_url": "https://collector.example"}}, http=http, base_url="https://sandbox.example")
    gw.balance()
    assert http.calls[-1]["url"] == "https://sandbox.example/v1/balance"
    with pytest.raises(SourceError, match="refused"):
        LiveGateway(creds, {}, http=http, base_url="http://sandbox.example").balance()


# ------------------------------ MEDIUM: a clock skew pauses placements only

def test_skew_seen_on_any_live_answer_pauses_and_resumes(pool, conn, gw):
    enable_live(conn)
    loop = live_loop(pool, gw, clock=lambda: NOW)
    loop.run_due(NOW)
    assert loop.live_paused is None and not kill.is_killed(conn)
    lv = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME)
    resting = approved_order(conn, lv, size=1)
    loop.run_task("executor", at(1))
    assert order_row(conn, resting["id"])["status"] == "open"
    gw.set_skew_ms(60_000)  # measured on the next non-probe answer
    loop.run_task("executor", at(2))
    assert loop.live_paused == "clock_skew" and loop.executor.live_blocked == "clock_skew" and kill.is_killed(conn)
    assert live_state(conn)["auto_kill_reasons"] == ["clock_skew"] and live_state(conn)["clock_skew_ms"] == 60_000
    loop.run_task("executor", at(3))
    assert order_row(conn, resting["id"])["status"] == "cancelled" and gw.remote == {}, "the kill's cancel still goes out while paused"
    gw.set_skew_ms(200)
    loop.run_task("live_fills", at(4))
    assert loop.live_paused is None and loop.executor.live_blocked is None, "the first in-range answer clears the pause"
    assert live_state(conn)["clock_skew_ms"] == 200 and len(audit_rows(conn, "auto_kill")) == 1


def test_gateway_skew_guard_is_wired_and_refuses_places_only(monkeypatch):
    from host.exchange.main import build_live_gateway

    creds = Credentials(key=KEY, secret=SEED)
    gw = build_live_gateway({}, creds, RateLimiter(now=NOW), max_skew_ms=30_000)
    assert isinstance(gw, LiveGateway) and gw.max_skew_ms == 30_000
    http = http_answering(200, '{"orders": []}', {"Date": "Sat, 03 Oct 2026 14:00:00 GMT"})
    gw.http = http
    gw.clock = lambda: datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
    gw.open_orders()
    assert gw.last_skew_ms == -3_600_000
    with pytest.raises(AuthError, match="placing refused"):
        gw.place({"id": "o", "client_request_id": "c", "market_ref": "m", "price": 0.5, "size": 1, "mode": "live"})
    assert gw.cancel({"exchange_order_id": "ex-1", "client_request_id": "c"}) is True and len(http.calls) == 2


# --------------------------------------- MEDIUM: demotions halt live assignments

def test_backtest_demotion_and_retirement_halt_live_assignments(conn, live):
    row = approved_order(conn, live, size=1)
    set_settings(conn, {"thresholds_backtest": {"min_bets": 10**6, "min_roi": 0.5, "max_drawdown": 0.0}}, "owner")
    recompute_all(conn)
    assert conn.execute("SELECT status FROM models WHERE id = %s", (live.model["id"],)).fetchone()["status"] == "candidate"
    assert assignment_row(conn, live.assignment["id"])["status"] == "halted"
    assert order_row(conn, row["id"])["status"] == "cancelled" and bankroll_of(conn, live.assignment)["reserved_cents"] == 0
    assert audit_rows(conn, "eligibility_changed")[-1]["after"] == {"status": "candidate"}
    halted = audit_rows(conn, "assignment_halted")[-1]
    assert halted["after"]["reason"] == "lineage no longer live_eligible"
    # retire: every active assignment of the lineage, approved rows cancelled at once, live ones cancel_requested
    other = trade_setup(conn, mode="live", model_status="live_eligible", game_id="2026_05_DAL_PHI")
    opened = approved_order(conn, other, size=1)
    Executor(PaperGateway(), FakeLiveGateway(clock=lambda: NOW)).tick(conn, NOW)
    waiting = approved_order(conn, other, size=1)
    retire(conn, other.model["id"], "retired", "owner")
    assert assignment_row(conn, other.assignment["id"])["status"] == "halted"
    assert order_row(conn, opened["id"])["status"] == "cancel_requested" and order_row(conn, waiting["id"])["status"] == "cancelled"
    assert audit_rows(conn, "model_retired")[-1]["after"]["assignments_halted"] == [str(other.assignment["id"])]


# ----------------------------------------------- MEDIUM: redaction everywhere

def test_unknown_shape_and_429_messages_are_redacted(conn):
    creds = Credentials(key=KEY, secret=SEED, passphrase="PASS-xyz")
    gw = LiveGateway(creds, {}, http=http_answering(200, '{"account": {"api_key": "%s", "pp": "PASS-xyz", "usd": "12.00"}}' % KEY))
    out = live_sync.auth_check(conn, gw, NOW, True)
    state = live_state(conn)
    for text in (out["error"], state["last_auth_error"], gw.probe_account()["payload"]):
        assert KEY not in text and "PASS-xyz" not in text and "***1234" in text, text
    gw = LiveGateway(creds, {}, http=http_answering(429, '{"error": "slow down %s PASS-xyz"}' % KEY))
    with pytest.raises(RateLimited) as exc:
        gw.balance()
    assert KEY not in str(exc.value) and "PASS-xyz" not in str(exc.value) and "***1234" in str(exc.value)
    assert base64.b64encode(SEED).decode() not in str(exc.value)


# ------------------------------------ MEDIUM: the direct cancel-all turns live off

def test_direct_cancel_all_cancels_approved_rows_and_turns_live_off(conn, gw, live):
    resting = approved_order(conn, live, size=1)
    Executor(PaperGateway(), gw).tick(conn, NOW)
    waiting = approved_order(conn, live, size=1)
    out = live_sync.cancel_all_direct(conn, gw, NOW, lambda s: None, "cli")
    assert out["live_off"]["was_on"] and out["approved_cancelled"] == 1 and not live_enabled(conn)
    assert order_row(conn, resting["id"])["status"] == "cancelled" and order_row(conn, waiting["id"])["status"] == "cancelled"
    assert assignment_row(conn, live.assignment["id"])["status"] == "halted"
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []
    Executor(PaperGateway(), gw).tick(conn, at(1))
    assert gw.calls["place"] == 1 and gw.remote == {}, "a restart submits nothing"
    assert audit_rows(conn, "live_off")[-1]["after"]["reason"] == "cancel-all --direct"


def test_direct_cancel_all_keeps_an_in_flight_place_cancellable(conn, gw, live):
    row = approved_order(conn, live, size=1)
    ex = Executor(PaperGateway(), gw)
    order = orders.set_status(conn, row["id"], "submitting", "exchange", expected=("approved",), submitted_at=NOW, gtd_at=at(900))
    out = live_sync.cancel_all_direct(conn, gw, NOW, lambda s: None, "cli")
    assert out["left_for_exchange"] == [str(row["id"])] and order_row(conn, row["id"])["status"] == "cancel_requested"
    ex._place(conn, order)  # the place returns after the direct run
    assert gw.calls["cancel"] == 1 and gw.remote == {}, "the order that reached the exchange is cancelled at once"
    ex.tick(conn, at(1))
    assert order_row(conn, row["id"])["status"] == "cancelled" and gw.calls["cancel"] == 1, "confirmed without a second cancel"
    assert live_sync.audit_open_orders(conn, gw, at(2))["unknown"] == [] and not kill.is_killed(conn)
    # a row closed in the database while its place is in flight is cancelled on the exchange too
    enable_live(conn)
    conn.execute("UPDATE assignments SET status = 'active' WHERE id = %s", (live.assignment["id"],))
    second = approved_order(conn, live, size=1)
    order = orders.set_status(conn, second["id"], "submitting", "exchange", expected=("approved",), submitted_at=at(3), gtd_at=at(900))
    orders.set_status(conn, second["id"], "cancelled", "test", expected=("submitting",))
    ex._place(conn, order)
    assert gw.calls["cancel"] == 2 and gw.remote == {}


# ------------------------------------- MEDIUM: buying power counts spent cash

def test_buying_power_subtracts_fills_since_the_probe(conn, gw, live):
    auth_state(conn, buying_power_cents=400)
    first = approved_order(conn, live, size=5)
    assert first["cost_cents"] == 266
    Executor(PaperGateway(), gw).tick(conn, NOW)
    gw.add_fill(first["client_request_id"], 0.52, 5, fee_cents=6, fill_id="f1")
    assert live_sync.poll_fills(conn, gw, NOW) == 1 and order_row(conn, first["id"])["status"] == "filled"
    assert bankroll_of(conn, live.assignment)["reserved_cents"] == 0, "the fill left the reservation: nothing reserved now"
    assert approve(conn, live, size=5)["reason"] == "buying_power", "266 spent since the probe + 266 > 400"
    assert approve(conn, live, size=1)["status"] == "approved", "54 + 266 <= 400"
    auth_state(conn, buying_power_cents=400, balance_checked_at=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert approve(conn, live, size=5)["status"] == "approved", "a fresh probe already reflects the fill: 266 + 54 reserved <= 400"


# ---------------------------------------------- LOW: the live approval lock race

def test_live_assignment_cannot_slip_past_live_off(pool, test_db_url):
    with pool.connection() as c:
        insert_game(c, LIVE_GAME)
        model = insert_model(c, status="live_eligible")
        insert_market(c, LIVE_GAME)
        enable_live(c)
    a = psycopg.connect(test_db_url, row_factory=dict_row)
    b = psycopg.connect(test_db_url, row_factory=dict_row)
    created = assignments.create_assignment(a, LIVE_GAME, model["id"], "live", 5000, "owner")  # not committed yet
    result: dict[str, Any] = {}

    def live_off() -> None:
        result.update(kill.live_off(b, "owner", "owner"))
        b.commit()

    worker = threading.Thread(target=live_off)
    worker.start()
    time.sleep(0.5)
    assert not result, "live-off waits for the live assignment in flight"
    a.commit()
    worker.join(timeout=10)
    a.close()
    b.close()
    with pool.connection() as c:
        assert c.execute("SELECT status FROM assignments WHERE id = %s", (created["id"],)).fetchone()["status"] == "halted"
        assert not live_enabled(c) and result["assignments_halted"] == [str(created["id"])]


# ----------------------------------------------- LOW: the local limiter wait

def test_executor_leaves_a_row_approved_when_no_order_token_is_due(conn, live):
    creds = Credentials(key=KEY, secret=SEED)
    limiter = RateLimiter({"orders_per_s": 0.2}, now=NOW)
    http = http_answering(200, '{"order_id": "ex-a"}')
    http = _numbered(http)
    gw = LiveGateway(creds, {}, limiter=limiter, http=http, clock=lambda: NOW, sleep=lambda s: None)
    first, second = approved_order(conn, live, size=1), approved_order(conn, live, size=1)
    ex = Executor(PaperGateway(), gw)
    assert ex.tick(conn, NOW)["submitted"] == 1 and len(http.calls) == 1
    assert order_row(conn, first["id"])["status"] == "open" and order_row(conn, second["id"])["status"] == "approved"
    assert ex.tick(conn, at(1))["submitted"] == 0 and order_row(conn, second["id"])["status"] == "approved", "waits for the token, never submitting"
    assert ex.tick(conn, at(5))["submitted"] == 1 and order_row(conn, second["id"])["status"] == "open"


# --------------------------------------------- gateway: parsing and config

def test_empty_listings_and_fill_shapes():
    live = live_config_with_defaults(None)["live"]
    for payload in ([], {}, {"orders": None}, {"data": None}, {"data": {"orders": None}}, None):
        assert live_parse.records(payload, live) == [], payload
    assert live_parse.records({"orders": "none"}, live) is None and live_parse.records(42, live) is None
    creds = Credentials(key=KEY, secret=SEED)
    for text in ("", "null", "{}", '{"orders": null}'):
        assert LiveGateway(creds, {}, http=http_answering(200, text)).open_orders() == [], text
    with pytest.raises(SourceError, match="unknown shape"):
        LiveGateway(creds, {}, http=http_answering(200, "<html>502</html>")).open_orders()
    fill = live_parse.fill_dict({"id": "f1", "size": "2", "fee_cents": 3, "price": "0.5"}, live)
    assert fill["size"] == 2 and fill["fee_cents"] == 3, "fee_cents is cents, not dollars"
    assert live_parse.fill_dict({"id": "f1", "size": "2", "fee": "0.03"}, live)["fee_cents"] == 3
    with pytest.raises(SourceError, match="fractional"):
        live_parse.fill_dict({"id": "f1", "size": "2.5"}, live)
    with pytest.raises(SourceError, match="fractional"):
        LiveGateway(creds, {}, http=http_answering(200, '{"fills": [{"id": "f1", "size": 2.5, "price": 0.5}]}')).fills(None)


def test_request_fields_merge_and_server_time_field():
    fields = live_config_with_defaults({"live": {"request_fields": {"price": "limit_price", "expires_at": None}}})["live"]["request_fields"]
    assert fields["price"] == "limit_price" and fields["client_order_id"] == "client_order_id" and fields["expires_at"] is None
    creds = Credentials(key=KEY, secret=SEED)
    http = http_answering(200, '{"order_id": "ex-1"}')
    order = {"id": "o1", "client_request_id": "c1", "market_ref": "m", "price": 0.5, "size": 1, "gtd_at": NOW, "mode": "live"}
    LiveGateway(creds, {"live": {"request_fields": {"price": "limit_price", "expires_at": None}}}, http=http).place(order)
    assert json.loads(http.calls[-1]["body"]) == {"client_order_id": "c1", "market_id": "m", "side": "BUY", "limit_price": 0.5, "size": 1, "time_in_force": "GTD"}
    with pytest.raises(NotConfigured, match="price"):
        LiveGateway(creds, {"live": {"request_fields": {"price": None}}}, http=http).place(order)
    # an ordinary "timestamp" field in the balance payload never becomes the server time
    http = http_answering(200, '{"balance": "100.00", "buying_power": "90.00", "timestamp": "2026-10-03T13:00:00Z"}')
    gw = LiveGateway(creds, {}, http=http, clock=lambda: datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc))
    assert gw.balance()["server_time"] is None and gw.last_skew_ms == 1000


# ------------------------------------------- operability: credentials, smoke

def test_malformed_secret_is_named_not_hidden(pool, conn, monkeypatch, capsys):
    from host.exchange import cli
    from host.exchange import main as exchange_main

    monkeypatch.setenv("POLYMARKET_US_API_KEY", KEY)
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", "not-a-seed!!")
    with pytest.raises(CredentialsError) as exc:
        exchange_main.load_credentials()
    assert "not-a-seed" not in str(exc.value) and "POLYMARKET_US_API_SECRET" in str(exc.value)
    monkeypatch.setenv("DATABASE_URL", pool.conninfo)
    monkeypatch.setenv("FLEET_DEV", "1")
    assert cli.main(["probe-account"]) == 1
    err = capsys.readouterr().err
    assert "secret malformed" in err and "POLYMARKET_US_API_SECRET" in err and "not-a-seed" not in err
    loop = live_loop(pool, FakeLiveGateway(clock=lambda: NOW), clock=lambda: NOW)

    def bad() -> Any:
        raise CredentialsError("POLYMARKET_US_API_SECRET decodes to 9 bytes, expected a 32-byte Ed25519 seed (or 64-byte secret key)")

    loop.load_credentials = bad  # type: ignore[method-assign]
    results = loop.run_due(NOW)
    assert results["auth"]["credentials_present"] is False and "startup_reconcile" not in results
    state = live_state(conn)
    assert state["credentials_present"] is False and state["last_auth_error"].startswith("credentials malformed: POLYMARKET_US_API_SECRET decodes to 9 bytes")
    loop.run_task("auth", at(1))
    assert live_state(conn)["last_auth_error"].startswith("credentials malformed"), "the probe keeps the message"


def smoke_phrase(conn) -> str:
    return "SMOKE " + datetime.now(timezone.utc).astimezone(owner_tz(conn)).date().isoformat()


def test_smoke_cancels_its_row_when_never_confirmed(pool, conn):
    enable_live(conn)
    set_setting(conn, "market_source", "polymarket_us")
    insert_game(conn, GAME_ID)
    market = insert_market(conn, GAME_ID, platform="polymarket_us")
    insert_snapshot(conn, market["id"], bid=0.50, ask=0.52, liquidity_usd_cents=300_000)
    result = smoke.run_smoke(pool, smoke_phrase(conn), sleep=lambda s: None)  # exchange service stopped, --drive forgotten
    row = order_row(conn, result["order_id"])
    assert result["status"] == "cancelled" and row["status"] == "cancelled"
    assert order_events(conn, row["id"]) == ["approved", "cancelled"]
    assert conn.execute("SELECT detail FROM order_events WHERE order_id = %s ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()["detail"] == {"reason": "smoke not confirmed in time"}
    assert Executor(PaperGateway(), FakeLiveGateway()).tick(conn, NOW)["submitted"] == 0, "a later exchange start places nothing"


def test_live_orders_stay_on_the_live_platform(pool, conn):
    enable_live(conn)
    insert_game(conn, GAME_ID)
    clob = insert_market(conn, GAME_ID, platform="polymarket_clob")
    insert_snapshot(conn, clob["id"], liquidity_usd_cents=900_000)
    stale = insert_market(conn, GAME_ID, platform="polymarket_us", side="away")
    insert_snapshot(conn, stale["id"], liquidity_usd_cents=800_000, age_s=120)
    fresh = insert_market(conn, GAME_ID, platform="polymarket_us")
    insert_snapshot(conn, fresh["id"], liquidity_usd_cents=100_000)
    set_setting(conn, "market_source", "polymarket_us")
    assert smoke.pick_market(conn)["id"] == fresh["id"], "not the more liquid CLOB market, not the stale one"
    with pytest.raises(Conflict, match="polymarket_clob"):
        smoke.pick_market(conn, clob["id"])
    set_setting(conn, "market_source", "polymarket_clob")
    with pytest.raises(Conflict, match="price source only"):
        smoke.pick_market(conn)
    # approvals and assignments: a live setup on sim markets while the source is polymarket_us
    set_setting(conn, "market_source", "sim")
    live = trade_setup(conn, mode="live", model_status="live_eligible", game_id=LIVE_GAME)
    assert approve(conn, live, size=1)["status"] == "approved"
    set_setting(conn, "market_source", "polymarket_us")
    assert approve(conn, live, size=1)["reason"] == "mode"
    model = insert_model(conn, status="live_eligible")
    insert_game(conn, "2026_05_DAL_PHI")
    with pytest.raises(Conflict, match="live platform"):
        assignments.create_assignment(conn, "2026_05_DAL_PHI", model["id"], "live", 1000, "owner")
    set_setting(conn, "market_source", "sim")
    insert_market(conn, "2026_05_DAL_PHI")
    assert assignments.create_assignment(conn, "2026_05_DAL_PHI", model["id"], "live", 1000, "owner")["status"] == "active"


# ------------------------------------------------- operability: the dashboard

def test_exchange_box_counts_pending_cancels_and_names_the_direct_command(client, conn, gw, live):
    a, b = approved_order(conn, live, size=1), approved_order(conn, live, size=1)
    Executor(PaperGateway(), gw).tick(conn, NOW)
    orders.cancel_order(conn, b["id"], "owner", "owner cancel")
    conn.execute("UPDATE exchange_state SET heartbeat_at = now()")
    exchange = client.get("/trading").text.split('id="exchange"')[1].split('id="ledger"')[0]
    assert '<dd class="c-live-orders">1 open, 1 cancel pending</dd>' in exchange and "c-direct-cancel" not in exchange
    conn.execute("UPDATE exchange_state SET heartbeat_at = now() - interval '2 minutes'")
    exchange = client.get("/fragments/trading").text.split('id="exchange"')[1].split('id="ledger"')[0]
    assert 'class="c-direct-cancel"' in exchange and "cancel-all --direct" in exchange and "Press KILL first" in exchange
    # live P&L stays on the top bar while real money is still in play with live off
    kill.live_off(conn, "owner", "owner")
    topbar = client.get("/fragments/topbar").text
    assert '<span class="pill paper">PAPER</span>' in topbar and "live today $0.00" in topbar
    conn.execute("UPDATE orders SET status = 'cancelled' WHERE mode = 'live'")
    conn.execute("UPDATE assignments SET status = 'settled' WHERE mode = 'live'")
    conn.execute("UPDATE bankrolls SET reserved_cents = 0, open_cost_cents = 0 WHERE mode = 'live'")
    assert "live today" not in client.get("/fragments/topbar").text


def test_settings_shows_a_remedy_per_auto_kill_reason(client, conn):
    enable_live(conn)
    kill.auto_kill(conn, "clock_skew", {"skew_ms": 48_213, "limit_ms": 30_000})
    html = client.get("/settings").text
    assert 'data-remedy="clock_skew"' in html and "wsl --shutdown" in html and "cancel-all --direct" in html
    assert html.count("Recovery: ") == 1 and html.count('data-remedy="clock_skew"') == 2, "the kill card and the live group"
    auth_state(conn, credentials_present=False)
    assert "exchange.env missing or malformed (see the last auth error)" in client.get("/settings").text


# ------------------------------------------------------ operability: secrets

def test_secret_files_are_ignored_by_git_and_docker():
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    assert "exchange.env" in ignore and "*.env" in ignore and "!.env.example" in ignore
    docker = (ROOT / ".dockerignore").read_text().splitlines()
    assert "exchange.env" in docker and "*.env" in docker
    import shutil
    import subprocess

    if shutil.which("git") and (ROOT / ".git").exists():
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False).stdout.split()
        assert [p for p in tracked if p.endswith(".env")] == [] and ".env.example" in tracked
        check = subprocess.run(["git", "check-ignore", "-q", "exchange.env"], cwd=ROOT, check=False)
        assert check.returncode == 0, "exchange.env must be ignored"
