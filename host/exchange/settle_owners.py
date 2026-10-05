"""Bets rows of a market both of an assignment's models traded (docs/TRADING.md "In-game
trading (step 6 Part C)", "Who owns a sold contract").

An assignment can hold contracts its pre-game model bought (orders.ingame false) and
contracts its in-game model bought (orders.ingame true) on the same market, and either
model's sell can close the other's contracts (the position is one average-cost
position on the exchange and in the ledger). Each model's paper record must carry the
result of what it bought, so a contract belongs to the model that bought it:

- the market's fills are walked in order, keeping each model's own sub-position
  (contracts and basis, at that model's own average cost);
- a sell closes its own model's contracts first, then the other model's;
- the sell's row keeps only its own model's part: cost = the own basis it closed,
  proceeds and fee pro rata by contracts (the rounding residual on its own part),
  pnl = own proceeds - own fee - own basis (zero when it closed only the other
  model's contracts);
- the part that closed the other model's contracts (its basis at that model's average
  cost, its proceeds and fee pro rata) is realized on that model's buy rows, pro rata
  by contracts over its buys since its sub-position was last flat: the row's cost and
  fee include it and its pnl includes those proceeds;
- what each model still holds is split over its own buys as for any order (basis by
  bought basis, payout by bought contracts).

So a model's rows add up to proceeds - fees - basis + payout of exactly the contracts it
bought. The ledger `settle` row still moves the pooled remaining basis and payout (the
average-cost position): the buy rows carry those as `settle_basis_cents` and
`settle_payout_cents` (a push pays the pooled basis back, split by each row's remaining
basis), so the rows' pnl still adds up to the ledger's realized pnl to the cent.
A market only one model traded never comes here (host.exchange.settle_sells).
"""
from __future__ import annotations

from typing import Any, Callable

import psycopg

from host.exchange.settle_sells import split
from host.trading import orders
from host.trading.positions import sell_basis_cents

FILLS_SQL = "SELECT order_id, price, size, fee_cents, basis_cents FROM fills WHERE order_id = ANY(%s) ORDER BY id"


def mixed(market_orders: list[dict[str, Any]]) -> bool:
    """Both models have filled orders on this market."""
    return len({bool(o.get("ingame")) for o in market_orders}) > 1


def _spread(amounts: tuple[int, int, int, int], lot: dict[str, list[int]],
            into: dict[str, list[int]]) -> None:
    """Add (contracts, proceeds, fee, basis) to the lot's buys pro rata by contracts."""
    ids = list(lot)
    weights = [lot[oid][0] for oid in ids]
    parts = [split(amount, weights) for amount in amounts]
    for i, oid in enumerate(ids):
        acc = into.setdefault(oid, [0, 0, 0, 0])
        for k in range(4):
            acc[k] += parts[k][i]


def walk(conn: psycopg.Connection, market_orders: list[dict[str, Any]]) -> dict[str, Any]:
    """The per-model walk over the market's fills: each model's remaining contracts and
    basis and current lot, each sell's own part and each buy's part closed by the
    other model's sells, all as [contracts, proceeds, fee, basis]."""
    owner = {str(o["id"]): bool(o.get("ingame")) for o in market_orders}
    sides = {str(o["id"]): o.get("side") for o in market_orders}
    held, basis = {False: 0, True: 0}, {False: 0, True: 0}
    lot: dict[bool, dict[str, list[int]]] = {False: {}, True: {}}
    own: dict[str, list[int]] = {}
    cross: dict[str, list[int]] = {}
    for r in conn.execute(FILLS_SQL, ([o["id"] for o in market_orders],)).fetchall():
        oid, size, mine_owner = str(r["order_id"]), int(r["size"]), owner[str(r["order_id"])]
        if sides.get(oid) != "sell":
            cost = int(r["basis_cents"]) if r["basis_cents"] is not None else orders.fill_cost_cents(r["price"], size)
            held[mine_owner] += size
            basis[mine_owner] += cost
            entry = lot[mine_owner].setdefault(oid, [0, 0])
            entry[0] += size
            entry[1] += cost
            continue
        other_owner = not mine_owner
        other = min(max(0, size - held[mine_owner]), held[other_owner])
        mine = size - other
        # The seller's own part takes the rounding residual (split puts it on the last weight).
        proceeds = split(orders.fill_cost_cents(r["price"], size), [other, mine])[::-1]
        fee = split(int(r["fee_cents"]), [other, mine])[::-1]
        closed = {}
        for who, n in ((mine_owner, mine), (other_owner, other)):
            closed[who] = sell_basis_cents(held[who], basis[who], n)
            held[who], basis[who] = max(0, held[who] - n), basis[who] - closed[who]
        acc = own.setdefault(oid, [0, 0, 0, 0])
        for k, v in enumerate((mine, proceeds[0], fee[0], closed[mine_owner])):
            acc[k] += v
        if other:
            _spread((other, proceeds[1], fee[1], closed[other_owner]), lot[other_owner], cross)
        for who in (False, True):
            if held[who] <= 0:
                lot[who] = {}
    return {"held": held, "basis": basis, "lot": lot, "own": own, "cross": cross, "owner": owner}


def owner_bets(
    conn: psycopg.Connection, market_orders: list[dict[str, Any]], totals: dict[str, dict[str, Any]],
    result: str, pooled: tuple[int, int], buy_row: Callable[..., dict[str, Any]],
    sell_row: Callable[..., dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """{order id: bets row} of a market both models traded. `pooled` is (remaining
    basis, contracts held) of the whole position; `buy_row(order, totals, basis,
    payout)` and `sell_row(order, totals)` build the plain rows this adjusts."""
    w = walk(conn, market_orders)
    rows: dict[str, dict[str, Any]] = {}
    remaining: dict[str, int] = {}
    model_payout: dict[str, int] = {}
    for who in (False, True):
        buys = [o for o in market_orders if o.get("side") != "sell" and w["owner"][str(o["id"])] is who]
        weights = [w["lot"][who].get(str(o["id"]), [0, 0]) for o in buys]
        basis_parts = split(w["basis"][who], [x[1] for x in weights])
        payout_parts = split(w["held"][who] * 100, [x[0] for x in weights]) if result == "win" else [0] * len(buys)
        for order, basis, payout in zip(buys, basis_parts, payout_parts):
            remaining[str(order["id"])], model_payout[str(order["id"])] = basis, payout
    ids = list(remaining)
    basis_left, held_total = pooled
    # The ledger moves the pooled numbers; each row's share of them, so the rows add up.
    settle_basis = split(basis_left, [remaining[oid] for oid in ids])
    if result == "win":
        settle_payout = split(held_total * 100, [model_payout[oid] for oid in ids])
    else:
        settle_payout = list(settle_basis) if result == "push" else [0] * len(ids)
    by_id = {str(o["id"]): o for o in market_orders}
    for oid, s_basis, s_payout in zip(ids, settle_basis, settle_payout):
        c = w["cross"].get(oid, [0, 0, 0, 0])
        row = buy_row(by_id[oid], totals[oid], remaining[oid] + c[3], s_payout)
        row.update(settle_basis_cents=s_basis, settle_payout_cents=s_payout, fee_cents=row["fee_cents"] + c[2])
        row["pnl_cents"] = s_payout + c[1] - row["cost_cents"] - row["fee_cents"]
        rows[oid] = row
    for order in market_orders:
        oid = str(order["id"])
        if order.get("side") == "sell":
            _, proceeds, fee, basis = w["own"].get(oid, [0, 0, 0, 0])
            row = sell_row(order, totals[oid])
            row.update(cost_cents=basis, fee_cents=fee, proceeds_cents=proceeds, pnl_cents=proceeds - fee - basis)
            rows[oid] = row
    return rows
