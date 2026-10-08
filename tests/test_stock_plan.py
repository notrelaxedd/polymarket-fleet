"""Order planning (fleet/worker/stock_plan.py): the host's limits, the backtest's
rebalance-on-change rule, the cash cut, SPY freshness and live == backtest. No database."""

from __future__ import annotations

import copy
from fleet.stocks import backtest
from fleet.stocks.data import Bar
from fleet.stocks.families import Decision, StockModel, make_model
from fleet.worker.stock_plan import plan, plan_orders
from tests.test_stock_worker import BARS, DEFAULT_LIMITS, MOMENTUM, PREVIOUS, SESSION, UNIVERSE, _state, _through


def test_a_stale_spy_holds_the_decision_back() -> None:
    from fleet.worker.stock_trade import bars_end

    bars = _through(SESSION)
    assert bars_end(bars, UNIVERSE) == PREVIOUS
    lagging = dict(bars, SPY=bars["SPY"][:-1])  # SPY sets the anchors and the trend filter
    assert bars_end(lagging, UNIVERSE) < PREVIOUS
    assert bars_end({s: r for s, r in bars.items() if s != "SPY"}, UNIVERSE) == PREVIOUS, "no SPY in the data: symbols only"


def test_buys_are_cut_to_the_cash_the_host_would_reserve() -> None:
    from decimal import Decimal

    from fleet.worker.stock_plan import fit_buys_to_cash, reservation_cents

    buys = [{"symbol": "A", "qty": 10, "ref_price_cents": 10_000, "rationale": "r"},
            {"symbol": "B", "qty": 10, "ref_price_cents": 10_000, "rationale": "r"}]
    out = fit_buys_to_cash(buys, 150_000, Decimal("0.05"))  # 10 shares reserve 105,000; 4 more fit in 45,000
    assert [(o["symbol"], o["qty"]) for o in out] == [("A", 10), ("B", 4)]
    assert sum(reservation_cents(o["qty"], o["ref_price_cents"], Decimal("0.05")) for o in out) <= 150_000
    assert out[1]["rationale"].endswith("cut to cash 4") and fit_buys_to_cash(buys, 10_000, Decimal("0.05")) == []
    state = _state(positions={}, settings={"stock_price_band": 0.05})
    spent = sum(reservation_cents(o["qty"], o["ref_price_cents"], Decimal("0.05"))
                for o in plan_orders(state, _through(SESSION), "j1") if o["side"] == "buy")
    assert 0 < spent <= state["assignment"]["cash_cents"], "a fully invested target is never refused with cash"


class Fixed(StockModel):
    """The same weights every day, or a different set from `switch` on."""

    family = "fixed"

    def __init__(self, weights: dict[str, float], switch: str | None = None, then: dict[str, float] | None = None) -> None:
        super().__init__({})
        self.w, self.switch, self.then = weights, switch, then or {}

    def decide(self, hist: dict[str, list[Bar]], day: str) -> Decision:
        return Decision(dict(self.then if self.switch and day >= self.switch else self.w))


def test_orders_stay_inside_the_hosts_max_order_and_max_position() -> None:
    names = ["S02", "S03", "S04"]  # under $1,000 a share (S00 at $1,136.96 can never be bought under max_order)
    state = _state(assignment=dict(_state()["assignment"], cash_cents=1_000_000), positions={},
                   settings=dict(DEFAULT_LIMITS))
    out = plan(state, _through(SESSION), "j1", model=Fixed({s: 1 / 3 for s in names}))
    refs = state["decision"]["ref_prices_cents"]
    buys = [o for o in out.orders if o["side"] == "buy"]
    assert {o["symbol"] for o in buys} == set(names)
    for o in buys:
        assert o["qty"] * o["ref_price_cents"] <= 100_000, "max_order"
        assert (0 + o["qty"]) * refs[o["symbol"]] <= 250_000, "max_position"
        assert o["rationale"].endswith("capped by max_order/max_position")
    assert out.unfinished == set(names), "the max_order shortfall is bought at the next sessions"
    # half way there: held + open buys + qty stays within max_position
    pending = [{"symbol": names[0], "side": "buy", "qty": 2_200_00 // refs[names[0]], "filled_qty": 0, "status": "open"}]
    state = dict(state, positions={names[1]: 2_400_00 // refs[names[1]]}, open_orders=pending)
    for o in plan(state, _through(SESSION), "j1", model=Fixed({s: 1 / 3 for s in names})).orders:
        held = state["positions"].get(o["symbol"], 0) + sum(p["qty"] for p in pending if p["symbol"] == o["symbol"])
        assert o["qty"] * o["ref_price_cents"] <= 100_000
        assert (held + o["qty"]) * refs[o["symbol"]] <= 250_000
    # a sell is capped by max_order too, the rest sold at the next sessions
    big = {names[0]: 5_000_00 // refs[names[0]]}
    out = plan(dict(state, positions=big, open_orders=[]), _through(SESSION), "j1", model=Fixed({}))
    assert [(o["side"], o["qty"] * o["ref_price_cents"] <= 100_000) for o in out.orders] == [("sell", True)]
    assert out.unfinished == {names[0]}
    # without limits in the state nothing is capped (missing = no cap); 0 is a cap of 0
    free = plan(dict(state, positions={}, open_orders=[], settings={}), _through(SESSION), "j1",
                model=Fixed({s: 1 / 3 for s in names}))
    assert all("capped" not in o["rationale"] for o in free.orders)
    assert free.unfinished == {o["symbol"] for o in free.orders if "cut to cash" in o["rationale"]}
    none = plan(dict(state, positions={}, open_orders=[], settings={"stock_max_order_cents": 0}), _through(SESSION),
                "j1", model=Fixed({s: 1 / 3 for s in names}))
    assert none.orders == []


def test_unchanged_weights_let_positions_drift_like_the_backtest() -> None:
    a, b = "S02", "S03"
    refs = _state()["decision"]["ref_prices_cents"]
    drifted = {a: 300_000 // refs[a] + 3, b: 300_000 // refs[b] - 2}  # a few shares off the target each
    state = _state(assignment=dict(_state()["assignment"], cash_cents=0), positions=drifted)
    out = plan(state, _through(SESSION), "j1", model=Fixed({a: 0.5, b: 0.5}))
    assert not out.changed and out.orders == [], "no trade on drift alone"
    # an unfinished name still goes to its target, a missing or extra name still trades
    assert [o["symbol"] for o in plan(state, _through(SESSION), "j1", model=Fixed({a: 0.5, b: 0.5}),
                                      unfinished={a}).orders] == [a]
    extra = dict(drifted, S05=5)
    assert [(o["symbol"], o["side"], o["qty"]) for o in plan(dict(state, positions=extra), _through(SESSION), "j1",
                                                              model=Fixed({a: 0.5, b: 0.5})).orders] == [("S05", "sell", 5)]
    # changed weights rebalance every name to its target
    moved = plan(state, _through(SESSION), "j1", model=Fixed({a: 0.5, b: 0.5}, switch=SESSION, then={a: 0.6, b: 0.4}))
    assert moved.changed and moved.orders and {o["symbol"] for o in moved.orders} | moved.unfinished == {a, b}


# -------------------------------------------------------- live == backtest


class Spy(StockModel):
    family = "spy"

    def __init__(self, inner: StockModel) -> None:
        super().__init__({})
        self.inner, self.seen = inner, {}

    def decide(self, hist: dict[str, list[Bar]], day: str) -> Decision:
        self.seen[day] = copy.deepcopy(hist)
        return self.inner.decide(hist, day)


def test_the_live_decision_sees_exactly_what_the_backtest_saw() -> None:
    inner = make_model("momentum", MOMENTUM, UNIVERSE)
    in_backtest = Spy(inner)
    backtest.run(in_backtest, BARS, UNIVERSE, 2024, 2024, 5.0, lambda: False)
    live = Spy(inner)
    plan_orders(_state(), _through(SESSION), "j1", model=live)
    assert live.seen[SESSION] == in_backtest.seen[SESSION]
    assert inner.weights(live.seen[SESSION], SESSION) == inner.weights(in_backtest.seen[SESSION], SESSION)
