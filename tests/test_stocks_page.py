"""The /stocks page and the Stocks settings group (step 9, contract section 8): the page
renders empty and full, it is owner only, every action is a plain form that works with
JavaScript off, the Settings group round-trips dollars and cents, Home shows a Stocks
row when a stock assignment is halted or the broker has warnings, and nothing carries
an em-dash or an en-dash. Rows are inserted straight into the tables."""
from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from host.api.app import create_app
from host.settings_forms import GROUPS, parse_group
from tests.conftest import flash_cookie
from tests.pagecheck import page

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
DASHES = (chr(0x2014), chr(0x2013))
BT = {"sharpe": 1.25, "max_drawdown": 0.12, "cagr": 0.11, "trades": 80, "first_day": "2017-01-03",
      "last_day": "2023-12-29", "benchmark": {"cagr": 0.09, "sharpe": 0.7}}


def now(conn) -> datetime:
    return conn.execute("SELECT now() AS n").fetchone()["n"]


def add_symbol(conn, symbol: str, closes: list[float], error: str | None = None) -> None:
    """An instrument with one daily bar per past weekday, newest last."""
    conn.execute("INSERT INTO instruments (symbol, tradable, fetched_at, last_error) VALUES (%s, true, now(), %s)",
                 (symbol, error))
    day = now(conn).astimezone(NY).date()
    days = []
    while len(days) < len(closes):
        day -= timedelta(days=1)
        if day.weekday() < 5:
            days.append(day)
    for d, close in zip(reversed(days), closes):
        conn.execute(
            "INSERT INTO stock_bars (symbol, timeframe, ts, open, high, low, close, volume, feed)"
            " VALUES (%s, '1Day', %s, %s, %s, %s, %s, 1000, 'sip')",
            (symbol, datetime.combine(d, dtime(0, 0), tzinfo=NY), close, close, close, close),
        )
    conn.execute("UPDATE instruments SET bars_count = %s, bars_through = (SELECT max(ts) FROM stock_bars WHERE symbol = %s)"
                 " WHERE symbol = %s", (len(closes), symbol, symbol))


def set_broker(conn, **fields: Any) -> None:
    t = now(conn)
    close = t + timedelta(hours=2)
    values = {"environment": "paper", "keys_present": True, "account_status": "ACTIVE", "equity_cents": 10_000_000,
              "cash_cents": 9_000_000, "buying_power_cents": 18_000_000, "market_open": True,
              "session_date": close.astimezone(NY).date(), "next_open": close + timedelta(hours=17),
              "next_close": close, "checked_at": t, "last_error": None, "warnings": Jsonb([])}
    values.update(fields)
    conn.execute("UPDATE stock_broker_state SET " + ", ".join(f"{k} = %s" for k in values) + " WHERE id = 1",
                 list(values.values()))


def add_model(conn, status: str = "paper_ok", family: str = "momentum", validated: bool = True) -> int:
    params = {"lookback": 126, "skip": 5, "top_k": 3, "tag": uuid.uuid4().hex[:6]}
    row = conn.execute(
        "INSERT INTO stock_models (family, params, params_hash, status, backtest_metrics, validation_metrics, summary)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'Buys the strongest names.') RETURNING id",
        (family, Jsonb(params), uuid.uuid4().hex[:16], status, Jsonb(BT),
         Jsonb({"sharpe": 0.8}) if validated else None),
    ).fetchone()
    conn.execute("UPDATE stock_models SET lineage_id = id WHERE id = %s", (row["id"],))
    return int(row["id"])


def add_assignment(conn, model_id: int, status: str = "active", mode: str = "paper", cash: int = 700_000,
                   bankroll: int = 1_000_000) -> int:
    row = conn.execute(
        "INSERT INTO stock_assignments (model_id, mode, symbols, bankroll_cents, cash_cents, reserved_cents, status,"
        " halt_reason, created_by) VALUES (%s, %s, %s, %s, %s, 0, %s, %s, 'owner') RETURNING id",
        (model_id, mode, ["SPY", "QQQ"], bankroll, cash, status, "owner halt" if status == "halted" else None),
    ).fetchone()
    return int(row["id"])


def add_order(conn, assignment_id: int, status: str, reason: str | None = None, side: str = "buy", qty: int = 3) -> str:
    oid = uuid.uuid4()
    session = now(conn).astimezone(NY).date()
    conn.execute(
        "INSERT INTO stock_orders (id, assignment_id, mode, session_date, client_request_id, symbol, side, qty,"
        " ref_price_cents, status, reason, rationale) VALUES (%s, %s, 'paper', %s, %s, 'SPY', %s, %s, 45000, %s, %s,"
        " 'momentum rank 2/20, w 0.25, target 12 held 8')",
        (oid, assignment_id, session, uuid.uuid4().hex[:24], side, qty, status, reason),
    )
    return str(oid)


@pytest.fixture
def full(conn) -> dict[str, Any]:
    """A paper broker with one warning, two symbols with bars (one failing its last
    fetch), four models, an active and a halted assignment, a position, orders, a mark."""
    add_symbol(conn, "SPY", [440.0, 450.0])
    add_symbol(conn, "QQQ", [370.0, 380.0], error="HTTP 500 from data.alpaca.markets")
    set_broker(conn, warnings=Jsonb([{"kind": "position_mismatch", "message": "SPY: ours 10, Alpaca 9", "ts": "x"}]))
    ok, live = add_model(conn), add_model(conn, "live_eligible", "trend")
    cand, gone = add_model(conn, "candidate", "meanrev", validated=False), add_model(conn, "retired", "buyhold")
    active, halted = add_assignment(conn, ok), add_assignment(conn, live, "halted")
    conn.execute("INSERT INTO stock_positions (assignment_id, symbol, qty, cost_cents) VALUES (%s, 'SPY', 10, 440000)",
                 (active,))
    day = now(conn).astimezone(NY).date() - timedelta(days=1)
    conn.execute("INSERT INTO stock_marks (assignment_id, session_date, equity_cents, positions_cents)"
                 " VALUES (%s, %s, 1190000, 440000)", (active, day))
    rejected = add_order(conn, active, "rejected", "max_order")
    filled = add_order(conn, active, "open")
    return {"ok": ok, "live": live, "candidate": cand, "retired": gone, "active": active, "halted": halted,
            "rejected": rejected, "open": filled}


def no_dashes(text: str) -> None:
    for dash in DASHES:
        assert dash not in text


# ------------------------------------------------------------------ the page


def test_page_renders_empty(client):
    r = client.get("/stocks")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    p = page(r.text)
    assert p.one("main").attr("data-page") == "stocks"
    assert p.one('[data-nav="trading"]').is_current, "Stocks lives under Trading in the nav"
    assert p.one('[data-subnav="stocks"]').is_current and not p.one('[data-subnav="nfl"]').is_current
    assert "What this page is" in p.one("details.intro").text
    for stat in ("broker", "equity", "stocks-today", "market", "open-orders"):
        assert p.has(f'[data-stat="{stat}"]'), stat
    assert p.one('[data-stat="broker"] .stat-value').text == "no keys"
    assert p.one("[data-broker-problem]").text.startswith("the exchange process has no Alpaca keys")
    for card in ("broker", "stock-assignments", "stock-models"):
        assert p.card(card)
    assert "No stock assignments yet" in p.card("stock-assignments").text
    assert "No stock models yet" in p.card("stock-models").text
    for key in ("stocks-positions", "stocks-orders", "stocks-feed", "stocks-assign", "stocks-search"):
        assert not p.one(f'details[data-key="{key}"]').is_open, key
    feed = p.listing("stock-feed")
    assert len(feed.select('[data-row="stock-feed"]')) == 20, "every followed symbol shows, with no bars yet"
    assert p.form("stock-assign").one('button[type="submit"]').disabled, "nothing to assign without a paper_ok model"
    no_dashes(r.text)
    frag = client.get("/fragments/stocks")
    assert frag.status_code == 200 and "<html" not in frag.text and page(frag.text).has('[data-card="broker"]')


def test_page_renders_full(client, conn, full):
    r = client.get("/stocks")
    assert r.status_code == 200
    no_dashes(r.text)
    p = page(r.text)
    assert p.one('[data-stat="broker"] .stat-value').text == "PAPER"
    assert p.one('[data-env="paper"]').text == "PAPER"
    assert p.one('[data-stat="equity"] .stat-value').text == "$100,000.00"
    assert p.one('[data-stat="market"] .stat-value').text == "open"
    assert p.one('[data-stat="open-orders"] .stat-value').text == "1"
    assert not p.has("[data-broker-problem]")
    warning = p.one('[data-warning="position_mismatch"]')
    assert "SPY: ours 10, Alpaca 9" in warning.text and p.has('[data-chip="broker-warnings"]')
    # assignments: equity = cash 7000 + 10 SPY at the last close 450 = 11500; today vs the mark 11900
    row = p.row("stock-assignment", full["active"])
    assert row.one(".row-value").text == "$11,500.00"
    meta = row.select(".row-meta")[0].text
    assert "today -$400.00" in meta and "total +15.0%" in meta
    assert "SPY, QQQ" in row.text and row.one('[data-chip="active"]').text == "active"
    assert [a.attrs["data-action"] for a in row.select("[data-action]")] == ["halt", "close"]
    halted = p.row("stock-assignment", full["halted"])
    assert halted.one('[data-chip="halted"]') and [a.attrs["data-action"] for a in halted.select("[data-action]")] == [
        "resume", "close"]
    # models: retired folded away, the others with their numbers and menus
    ids = [n.attrs["data-id"] for n in p.listing("stock-models").select('[data-row="stock-model"]')]
    assert set(ids) == {str(full["ok"]), str(full["live"]), str(full["candidate"])}
    assert [n.attrs["data-id"] for n in p.listing("stock-retired").select("[data-row]")] == [str(full["retired"])]
    m = p.row("stock-model", full["ok"])
    assert "Sharpe 1.25" in m.text and "drawdown 12.0%" in m.text and "CAGR +11.0% vs SPY +9.0%" in m.text
    assert m.one('[data-chip="validated"]') and m.one('[data-chip="paper_ok"]').text == "paper ok"
    assert [a.attrs["data-action"] for a in m.select("[data-action]")] == ["validate", "backtest", "assign", "retire"]
    assert m.one('[data-action="assign"]').target == f"/stocks?model={full['ok']}#assign"
    cand = p.row("stock-model", full["candidate"])
    assert cand.one('[data-chip="unvalidated"]') and not cand.has('[data-action="assign"]')
    assert not p.row("stock-model", full["retired"]).has("details.menu")
    # positions, orders with reason codes, feed
    pos = p.row("stock-position", f"{full['active']}-SPY")
    assert "10 SPY" in pos.text and pos.one(".row-value").text == "+$100.00"
    rejected = p.row("stock-order", full["rejected"])
    assert rejected.one('[data-reason="max_order"]').text == "max_order: over max order"
    assert "momentum rank 2/20" in rejected.text and rejected.one('[data-chip="rejected"]')
    assert p.one('details[data-key="stocks-feed"]').is_open, "a failing symbol opens the feed group"
    qqq = p.row("stock-feed", "QQQ")
    assert qqq.one('[data-chip="feed-error"]') and "HTTP 500" in qqq.text and "2 bars" in qqq.text
    # the create form lists the tradable models and symbols
    options = [o.attrs["value"] for o in p.form("stock-assign").select('select[name="model_id"] option')]
    assert sorted(options) == sorted([str(full["ok"]), str(full["live"])])
    assert p.input("symbols").attr("value") == "SPY, QQQ"
    assert p.input("bankroll").attr("value") == "10000.00"
    opened = page(client.get(f"/stocks?model={full['ok']}").text)
    assert opened.one('details[data-key="stocks-assign"]').is_open
    assert opened.one(f'select[name="model_id"] option[value="{full["ok"]}"]').has_attr("selected")


def test_trading_header_links_to_stocks(client):
    p = page(client.get("/trading").text)
    link = p.one('[data-subnav="stocks"]')
    assert link.target == "/stocks" and p.one('[data-subnav="nfl"]').is_current


def test_owner_only(config):
    strict = dataclasses.replace(config, dev=False, owner_login="owner@example.com")
    bad = {"Tailscale-User-Login": "intruder@example.com"}
    with TestClient(create_app(strict)) as c:
        for path in ("/stocks", "/fragments/stocks"):
            assert c.get(path, headers=bad).status_code == 401, path
        for path in ("/stocks/assignments", "/stocks/assignments/1/halt", "/stocks/assignments/1/resume",
                     "/stocks/assignments/1/close", "/stocks/models/1/retire", "/stocks/jobs"):
            assert c.post(path, headers=bad, data={}).status_code == 401, path
        assert c.get("/stocks", headers={"Tailscale-User-Login": "owner@example.com"}).status_code == 200
        cross = {"Tailscale-User-Login": "owner@example.com", "Origin": "https://evil.example"}
        assert c.post("/stocks/jobs", headers=cross, data={"kind": "stock_search"}).status_code == 403


# ------------------------------------------------------------------ forms (no JavaScript)


def test_every_action_is_a_plain_form(client, conn, full):
    p = page(client.get("/stocks").text)
    for node in p.one("main").select("[data-action]"):
        if node.tag == "a":
            assert node.attrs["href"].startswith("/stocks?model="), node
            continue
        assert node.tag == "form" and node.attrs["method"] == "post" and node.target.startswith("/stocks/"), node
        assert node.has('button[type="submit"]'), node
    for name in ("stock-assign", "stock-search"):
        form = p.form(name)
        assert form.tag == "form" and form.attrs["method"] == "post" and form.has('button[type="submit"]')
    assert p.form("stock-search").target == "/stocks/jobs" and p.form("stock-assign").target == "/stocks/assignments"


def test_row_actions_post_and_redirect(client, conn, full):
    r = client.post(f"/stocks/assignments/{full['active']}/halt", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/stocks#assignments"
    assert flash_cookie(r) == f"stock assignment {full['active']} halted"
    assert conn.execute("SELECT status FROM stock_assignments WHERE id = %s", (full["active"],)).fetchone()["status"] == "halted"
    r = client.post(f"/stocks/assignments/{full['active']}/close", follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("close refused: "), "it still holds SPY"
    r = client.post("/stocks/assignments/999999/resume", follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("resume refused: ")
    r = client.post(f"/stocks/models/{full['candidate']}/retire", follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == f"stock model {full['candidate']} retired"
    assert conn.execute("SELECT status FROM stock_models WHERE id = %s", (full["candidate"],)).fetchone()["status"] == "retired"
    r = client.post("/stocks/jobs", data={"kind": "stock_backtest", "model_id": str(full["ok"])}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("stock_backtest job ")
    r = client.post("/stocks/jobs", data={"kind": "stock_validate", "model_id": "abc"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "validate refused: Model: choose a stock model"
    r = client.post("/stocks/jobs", data={"kind": "stock_trade"}, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("job refused: unknown kind")


def test_new_search_form(client, conn):
    form = {"kind": "stock_search", "n": "50", "seed": "7", "family_momentum": "true", "family_trend": "true"}
    r = client.post("/stocks/jobs", data=form, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r).startswith("stock_search job ")
    job = conn.execute("SELECT kind, role, params FROM jobs WHERE kind = 'stock_search'").fetchone()
    assert job["params"]["n"] == 50 and job["params"]["seed"] == 7 and job["params"]["families"] == ["momentum", "trend"]
    r = client.post("/stocks/jobs", data={**form, "n": "lots"})
    assert r.status_code == 400
    p = page(r.text)
    assert p.one('details[data-key="stocks-search"]').is_open
    assert p.form("stock-search").one(".inline-error").text == "Candidates must be a whole number"
    assert p.input("n").attr("value") == "lots" and not p.input("family_meanrev").has_attr("checked")
    r = client.post("/stocks/jobs", data={"kind": "stock_search", "n": "5", "seed": "1"})
    assert r.status_code == 400 and "tick at least one family" in r.text


def test_new_assignment_form(client, conn, full):
    r = client.post("/stocks/assignments", data={"model_id": str(full["ok"]), "mode": "paper", "bankroll": "1,500.25",
                                                "symbols": "spy qqq"}, follow_redirects=False)
    assert r.status_code == 303, r.text[-500:]
    row = conn.execute("SELECT * FROM stock_assignments ORDER BY id DESC LIMIT 1").fetchone()
    assert row["bankroll_cents"] == 150025 and row["symbols"] == ["SPY", "QQQ"] and row["mode"] == "paper"
    assert flash_cookie(r) == f"stock assignment {row['id']} created: paper on 2 symbols, bankroll $1,500.25"
    set_broker(conn, keys_present=False)
    r = client.post("/stocks/assignments", data={"model_id": str(full["ok"]), "mode": "paper", "bankroll": "100",
                                                "symbols": "SPY"})
    assert r.status_code == 400
    p = page(r.text)
    assert p.one('details[data-key="stocks-assign"]').is_open and "no Alpaca keys" in p.one(".inline-error").text
    assert p.input("bankroll").attr("value") == "100"
    r = client.post("/stocks/assignments", data={"model_id": "", "bankroll": "abc", "symbols": "SPY"})
    assert r.status_code == 400 and "Bankroll must be a dollar amount" in r.text


# ------------------------------------------------------------------ Settings: the Stocks group

STOCK_KEYS = {
    "stocks_enabled", "stock_symbols", "stock_history_start", "stock_history_feed", "stock_bars_hour",
    "stock_decision_lead_min", "stock_trade_tick_s", "stock_cost_bps", "stock_price_band", "stock_max_order_cents",
    "stock_max_position_cents", "stock_default_bankroll_cents", "stock_max_daily_loss_cents", "stock_max_assignments",
    "stock_backtest_years", "stock_validation_years", "thresholds_stock_backtest", "thresholds_stock_paper",
    "stock_broker_poll_s", "stock_orders_poll_s",
}
GOOD = {
    "stocks_enabled": "true", "stock_symbols": "spy, QQQ  brk.b", "stock_history_start": "2018-01-02",
    "stock_history_feed": "iex", "stock_bars_hour": "19", "stock_decision_lead_min": "25", "stock_trade_tick_s": "20",
    "stock_broker_poll_s": "15", "stock_orders_poll_s": "3", "stock_max_assignments": "4", "stock_cost_bps": "2.5",
    "stock_price_band": "0.04", "stock_max_order": "1,250.50", "stock_max_position": "3000",
    "stock_default_bankroll": "5000.05", "stock_max_daily_loss_paper": "800", "stock_max_daily_loss_live": "150.75",
    "stock_backtest_first": "2017", "stock_backtest_last": "", "stock_validation_first": "2023",
    "stock_validation_last": "", "stock_bt_min_sharpe": "0.6", "stock_bt_max_drawdown": "0.25",
    "stock_bt_min_trades": "40", "stock_bt_min_validation_sharpe": "0.1", "stock_paper_min_days": "15",
    "stock_paper_min_return": "-0.01", "stock_paper_max_drawdown": "0.2",
}


def test_the_stocks_group_parses_every_key():
    assert "stocks" in GROUPS
    out = parse_group("stocks", GOOD)
    assert set(out) == STOCK_KEYS
    assert out["stock_max_order_cents"] == 125050 and out["stock_default_bankroll_cents"] == 500005
    assert out["stock_max_daily_loss_cents"] == {"paper": 80000, "live": 15075}
    assert out["stock_symbols"] == ["SPY", "QQQ", "BRK.B"] and out["stocks_enabled"] is True
    assert out["stock_backtest_years"] == [2017, None] and out["stock_validation_years"] == [2023, None]
    assert out["thresholds_stock_backtest"] == {"min_sharpe": 0.6, "max_drawdown": 0.25, "min_trades": 40,
                                                "min_validation_sharpe": 0.1}
    assert out["thresholds_stock_paper"] == {"min_days": 15, "min_return": -0.01, "max_drawdown": 0.2}
    assert parse_group("stocks", {k: v for k, v in GOOD.items() if k != "stocks_enabled"})["stocks_enabled"] is False


def test_settings_page_stocks_group_round_trips(client, conn):
    p = page(client.get("/settings").text)
    form = p.form("stocks")
    assert form.target == "/settings/stocks" and form.attr("id") == "stocks"
    group = form.closest("details")
    assert group.attr("data-key") == "settings-stocks" and not group.is_open
    assert "max order $1,000.00" in group.one(".disclosure-summary").text
    assert p.field("stock_max_order").one("input").attr("value") == "1000.00"
    assert p.field("stock_max_daily_loss_live").one("input").attr("value") == "200.00"
    assert p.field("stock_default_bankroll").one("input").attr("value") == "10000.00"
    assert p.field("stock_validation_last").one("input").attr("value") == ""
    assert p.field("stock_bt_min_trades").one("input").attr("value") == "30"
    assert p.input("stocks_enabled").has_attr("checked")
    assert p.input("stock_symbols").attr("value").startswith("SPY, QQQ, IWM")
    r = client.post("/settings/stocks", data=GOOD, follow_redirects=False)
    assert r.status_code == 303 and flash_cookie(r) == "stocks settings saved"
    s = client.get("/api/settings").json()
    assert s["stock_max_order_cents"] == 125050 and s["stock_max_daily_loss_cents"] == {"paper": 80000, "live": 15075}
    assert s["stock_symbols"] == ["SPY", "QQQ", "BRK.B"] and s["stock_history_feed"] == "iex"
    again = page(client.get("/settings").text)
    assert again.field("stock_max_order").one("input").attr("value") == "1250.50"
    assert again.field("stock_max_daily_loss_live").one("input").attr("value") == "150.75"
    assert again.field("stock_price_band").one("input").attr("value") == "0.04"
    for bad, message in [
        ({**GOOD, "stock_max_assignments": "99"}, "stock_max_assignments must be an integer between 0 and 50"),
        ({**GOOD, "stock_max_order": "lots"}, "Max order must be a dollar amount"),
        ({**GOOD, "stock_bars_hour": "9.5"}, "Bars refresh hour must be a whole number"),
        ({**GOOD, "stock_symbols": "SPY SPY"}, "stock_symbols must not repeat a symbol"),
    ]:
        r = client.post("/settings/stocks", data=bad, follow_redirects=False)
        assert r.status_code == 400, bad
        rejected = page(r.text)
        assert rejected.form("stocks").closest("details").is_open and message in rejected.form("stocks").text
        assert rejected.one("[data-errors]").one("a").target == "#stocks"
    assert client.get("/api/settings").json()["stock_max_assignments"] == 4, "nothing saved on a rejected form"


# ------------------------------------------------------------------ Home


def test_home_needs_attention_stocks_row(client, conn):
    assert not page(client.get("/fragments/home").text).has('[data-row="attention"][data-id="stocks"]')
    halted = add_assignment(conn, add_model(conn), "halted")
    row = page(client.get("/").text).row("attention", "stocks")
    assert row.one(".row-main").target == "/stocks" and "1 assignment halted" in row.text
    conn.execute("UPDATE stock_assignments SET status = 'closed' WHERE id = %s", (halted,))
    set_broker(conn, warnings=Jsonb([{"kind": "unknown_order", "message": "an order that is not ours"}]))
    row = page(client.get("/fragments/home").text).row("attention", "stocks")
    assert "1 broker warning" in row.text and "an order that is not ours" in row.text


def test_no_dashes_in_my_files():
    files = ["host/api/dashboard_stocks.py", "host/stocks/views.py", "host/settings_forms_stocks.py",
             "host/templates/stocks.html", "host/templates/_settings_stocks.html", "tests/test_stocks_page.py"]
    files += [str(p.relative_to(ROOT)) for p in (ROOT / "host/templates").glob("_stocks*.html")]
    for name in files:
        no_dashes((ROOT / name).read_text(encoding="utf-8"))
