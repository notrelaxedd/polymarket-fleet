"""The /trading page for sells and positions (docs/TRADING.md "Selling", docs/DASHBOARD.md
"Trading page"): a `sell` chip on sell orders and fills with their realized P&L, and a
Positions table per assignment with size, average cost, current bid and unrealized
P&L at the bid, in dollars. Orders and fills are inserted straight into the tables
(the 0008 columns), so these tests do not depend on the approval or fill paths."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import psycopg

from tests.conftest import GAME_ID, insert_market, insert_snapshot, make_assignment, trade_setup
from tests.pagecheck import Node, page


def _order(
    conn: psycopg.Connection, setup: Any, market: dict[str, Any], side: str, price: float, size: int,
    status: str = "filled", filled: int | None = None, age_s: int = 0, assignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An order row of either side (a sell reserves nothing: cost 0)."""
    filled = (size if status == "filled" else 0) if filled is None else filled
    cost = 0 if side == "sell" else int(round(price * size * 100))
    return conn.execute(
        """
        INSERT INTO orders (client_request_id, assignment_id, worker_id, job_id, market_id, mode, price, size, cost_cents,
                            status, filled_size, avg_fill_price, my_p, market_p, edge, rationale, side, created_at)
        VALUES (%s, %s, %s, %s, %s, 'paper', %s, %s, %s, %s, %s, %s, 0.5, 0.5, 0.02, %s, %s,
                now() - make_interval(secs => %s))
        RETURNING *
        """,
        (uuid.uuid4().hex, (assignment or setup.assignment)["id"], setup.worker.id, setup.job["id"], market["id"], price, size,
         cost, status, filled, price if filled else None, f"{side} rationale", side, age_s),
    ).fetchone()


def _fill(conn: psycopg.Connection, order: dict[str, Any], price: float, size: int, fee: int, basis: int, age_s: int = 0) -> dict[str, Any]:
    return conn.execute(
        """
        INSERT INTO fills (order_id, ts, price, size, fee_cents, mode, basis_cents)
        VALUES (%s, now() - make_interval(secs => %s), %s, %s, %s, 'paper', %s) RETURNING *
        """,
        (order["id"], age_s, price, size, fee, basis),
    ).fetchone()


def _live(client: Any) -> Node:
    """The live region of /trading (everything the 5 s refresh replaces)."""
    return page(client.get("/trading").text).one("#trading-live")


def _pnl(node: Node) -> str:
    """The signed money of the one P&L figure in node, with its state class."""
    figure = node.one(".pnl")
    state = "pos" if figure.has_class("pnl-pos") else "neg" if figure.has_class("pnl-neg") else "zero"
    return f"{state} {figure.text}"


def _book(conn: psycopg.Connection) -> dict[str, Any]:
    """A held home position partly sold, an open sell, and a big losing away position.

    home: buy 30 @ 0.40 (basis $12.00), sell 10 @ 0.60 fee $0.24 removing basis $4.00
    (realized +$1.76), held 20 at avg 0.40, latest bid 0.55 -> unrealized +$2.75 (value $11.00 - sale fee $0.25 - basis $8.00).
    away: buy 5,000 @ 0.50 (basis $2,500.00), latest bid 0.20 -> unrealized -$1,540.00 (sale fee $40.00).
    """
    setup = trade_setup(conn)
    home = setup.market
    buy = _order(conn, setup, home, "buy", 0.40, 30, age_s=300)
    buy_fill = _fill(conn, buy, 0.40, 30, 50, 1200, age_s=300)
    sold = _order(conn, setup, home, "sell", 0.60, 10, age_s=200)
    sell_fill = _fill(conn, sold, 0.60, 10, 24, 400, age_s=200)
    resting = _order(conn, setup, home, "sell", 0.62, 5, status="open", age_s=100)
    insert_snapshot(conn, home["id"], bid=0.55, ask=0.57)
    away = insert_market(conn, GAME_ID, side="away")
    big = _order(conn, setup, away, "buy", 0.50, 5000, age_s=250)
    _fill(conn, big, 0.50, 5000, 0, 250_000, age_s=250)
    insert_snapshot(conn, away["id"], bid=0.20, ask=0.24)
    return {"setup": setup, "home": home, "away": away, "buy": buy, "buy_fill": buy_fill, "sold": sold,
            "sell_fill": sell_fill, "resting": resting, "big": big}


def test_sell_chip_on_open_and_recent_sell_orders_with_realized_pnl(client, conn):
    b = _book(conn)
    live = _live(client)
    resting = live.card("open-orders").row("order", b["resting"]["id"])
    assert resting.one("[data-side]").attr("data-side") == "sell" and resting.chip("sell").text == "sell"
    assert "sell 5 @ 0.62" in resting.text and "$0.00" not in resting.text, "a sell shows no reservation cost"
    assert resting.action("cancel").target == f"/orders/{b['resting']['id']}/cancel", "an open sell keeps its Cancel button"
    recent = live.card("orders")
    sold = recent.row("order", b["sold"]["id"])
    assert sold.chip("sell").text == "sell" and "sell 10 @ 0.60 (10 filled avg 0.60)" in sold.text
    assert "realized +$1.76" in sold.text and _pnl(sold) == "pos +$1.76"
    bought = recent.row("order", b["buy"]["id"])
    assert bought.one("[data-side]").attr("data-side") == "buy" and "sell" not in bought.chips()
    assert "30 @ 0.40" in bought.text and "$12.00" in bought.text and "realized" not in bought.text
    rest_recent = recent.row("order", b["resting"]["id"])
    assert "sell" in rest_recent.chips() and "realized" not in rest_recent.text, "an unfilled sell has no realized P&L yet"


def test_sell_fill_shows_chip_realized_pnl_and_basis(client, conn):
    b = _book(conn)
    fills = _live(client).card("fills")
    sold = fills.row("fill", b["sell_fill"]["id"])
    assert sold.attr("data-side") == "sell" and sold.chip("sell").text == "sell"
    assert "sold 10 @ 0.60" in sold.text and "$0.24" in sold.text and "basis $4.00" in sold.text
    assert _pnl(sold) == "pos +$1.76"
    bought = fills.row("fill", b["buy_fill"]["id"])
    assert "sell" not in bought.chips() and "30 @ 0.40" in bought.text and not bought.has(".pnl")
    assert "realized" in fills.text.lower(), "the realized column is labelled"


def test_positions_table_per_assignment_values_at_the_bid(client, conn):
    b = _book(conn)
    idle = make_assignment(conn, GAME_ID)
    positions = _live(client).card("positions")
    held = {n.attr("data-assignment") for n in positions.select("[data-assignment]")}
    assert held == {str(b["setup"].assignment["id"])}, "only the assignment holding contracts gets a table"
    assert str(idle["id"]) not in positions.html and "KC @ LV" in positions.text
    assert "1 assignment holding" in positions.text
    home = positions.row("position", b["home"]["id"])
    assert "size 20" in home.text and "avg cost 0.40 · basis $8.00" in home.text and "bid 0.55" in home.text
    assert _pnl(home) == "pos +$2.75" and "home wins" in home.text
    assert home.select(".row-meta")[-1].text == "unrealized, net of sale fee", "the value's meaning is visible, not only a tooltip"
    away = positions.row("position", b["away"]["id"])
    assert "size 5000" in away.text and "$2,500.00" in away.text and "bid 0.20" in away.text
    assert _pnl(away) == "neg -$1,540.00", "money as $1,234.56 with the sign, net of the sale fee"
    total = positions.one(".positions-total")
    assert total.text == "unrealized (net of sale fee) -$1,537.25" and _pnl(total) == "neg -$1,537.25"
    assert "unrealized net of fee -$1,537.25" in positions.one(".disclosure-summary").text


def test_position_sold_out_or_resolved_disappears(client, conn):
    b = _book(conn)
    rest = _order(conn, b["setup"], b["home"], "sell", 0.55, 20, age_s=50)
    _fill(conn, rest, 0.55, 20, 20, 800, age_s=50)
    conn.execute("UPDATE markets SET status = 'resolved', resolved_yes = false WHERE id = %s", (b["away"]["id"],))
    positions = _live(client).card("positions")
    assert not positions.rows("position") and "No open positions." in positions.text and "0 assignments holding" in positions.text


def test_position_without_any_bid_is_not_valued(conn):
    from host.trading.views_positions import assignment_positions, value_at_bid
    from host.trading.assignments import list_assignments

    setup = trade_setup(conn)
    bare = insert_market(conn, GAME_ID, side="away")
    order = _order(conn, setup, bare, "buy", 0.30, 7)
    _fill(conn, order, 0.30, 7, 0, 210)
    held = assignment_positions(conn, list_assignments(conn))
    assert len(held) == 1 and held[0]["unpriced"] == 1 and held[0]["unrealized_cents"] == 0
    row = held[0]["rows"][0]
    assert row["bid"] is None and row["unrealized_cents"] is None and row["size"] == 7 and row["basis_cents"] == 210
    valued = value_at_bid({"size": 3, "basis_cents": 100}, None)
    assert valued["value_cents"] is None and valued["unrealized_cents"] is None
    priced = value_at_bid({"size": 20, "basis_cents": 800}, Decimal("0.55"), {"taker_rate": 0.05})
    assert priced["value_cents"] == 1100 and priced["sell_fee_cents"] == 25 and priced["unrealized_cents"] == 275
    assert value_at_bid({"size": 20, "basis_cents": 800}, Decimal("0.55"), {"taker_rate": 0.0})["unrealized_cents"] == 300


def test_latest_snapshot_bid_wins_over_the_market_row(conn):
    """The current bid is the latest snapshot's, even when the mirrored best bid differs."""
    from host.trading.views_positions import latest_bids

    setup = trade_setup(conn)
    insert_snapshot(conn, setup.market["id"], bid=0.47, ask=0.49)
    conn.execute("UPDATE markets SET best_bid = 0.10 WHERE id = %s", (setup.market["id"],))
    bids = latest_bids(conn, [setup.market["id"]])
    assert float(bids[setup.market["id"]]["bid"]) == 0.47
    conn.execute("DELETE FROM price_snapshots WHERE market_id = %s", (setup.market["id"],))
    assert float(latest_bids(conn, [setup.market["id"]])[setup.market["id"]]["bid"]) == 0.10
    assert latest_bids(conn, []) == {}


def test_fragment_refresh_carries_positions_and_sell_chips(client, conn):
    _book(conn)
    r = client.get("/fragments/trading")
    assert r.status_code == 200
    fragment = page(r.text)
    assert not fragment.has("html") and not fragment.has("#trading-live") and not fragment.has('[data-card="assign"]')
    assert fragment.card("positions").has(".pnl-pos") and "+$2.75" in fragment.card("positions").text
    assert fragment.count('[data-chip="sell"]') == 4, "open sell, two recent sells, one sell fill"
    assert "positions" in fragment.cards() and len(fragment.cards()) == 10, "positions is a card (step 4 had 8, step 6C adds the in-game feed)"
    assert _live(client).count('[data-card="positions"]') == 1


def test_empty_positions_state_in_words(client, conn):
    positions = _live(client).card("positions")
    assert "No open positions." in positions.text and not positions.rows("position")
    assert "No open positions." in page(client.get("/fragments/trading").text).card("positions").text


def test_owner_api_orders_and_fills_carry_order_side_and_realized(conn):
    from host.trading import views

    b = _book(conn)
    orders = {o["id"]: o for o in views.list_orders(conn, None, 50)}
    assert orders[b["sold"]["id"]]["order_side"] == "sell" and orders[b["sold"]["id"]]["realized_cents"] == 176
    assert orders[b["sold"]["id"]]["side"] == "home", "`side` stays the market's team side"
    assert orders[b["buy"]["id"]]["order_side"] == "buy" and orders[b["buy"]["id"]]["realized_cents"] is None
    assert orders[b["resting"]["id"]]["realized_cents"] is None
    fills = {f["id"]: f for f in views.list_fills(conn, 50)}
    assert fills[b["sell_fill"]["id"]]["realized_cents"] == 176 and fills[b["buy_fill"]["id"]]["realized_cents"] is None


def test_sell_rules_in_css_keep_the_aa_pairs_and_phone_layout(client):
    from tests.test_style import _schemes, contrast, declarations

    css = client.get("/static/style.css").text
    sell = declarations(".chip-sell", css=css)
    # colour means state (docs/UI.md): the sell chip is never a filled blue badge; an
    # outline in the link colour (6B review) or plain chip-muted are both fine
    assert "accent-fill" not in sell, "the sell chip is not drawn as a blue status badge"
    assert "var(--red-fg)" in declarations(".pnl-neg", css=css)
    for tokens in _schemes():
        if "var(--accent)" in sell:
            assert contrast(tokens["accent"], tokens["card"]) >= 4.5
        for bg in ("card", "live-bg"):
            assert contrast(tokens["red-fg"], tokens[bg]) >= 4.5
    assert "flex-basis: 100%" in declarations(".positions-total", media="max-width", css=css), "the position total wraps on a phone"


def test_sell_reject_reasons_in_words():
    from host.api.dashboard_trading import reason_text

    assert reason_text({"reject_reason": "no_position"}, {}) == "nothing held to sell"
    assert reason_text({"reject_reason": "sell_exceeds_position"}, {}) == "sell larger than the position"
    assert reason_text({"reject_reason": "open_sell_exists"}, {}) == "a sell is already open on this market"


def test_no_em_dash_on_the_page(client, conn):
    _book(conn)
    assert chr(0x2014) not in client.get("/trading").text
