"""The Stocks settings group (step 9, contract section 8): every key parses, and the
Settings page round-trips dollars and cents and rejects bad input without saving.
Split from tests/test_stocks_page.py to keep both modules near 300 lines."""

from __future__ import annotations

from host.settings_forms import GROUPS, parse_group
from tests.conftest import flash_cookie
from tests.pagecheck import page

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

