"""Step 6 Part B paper sell of the end-to-end test (tests/e2e_signals.py runs it): the
real agent in the trade role, the real exchange loop on the sim source, the real
approvals, paper fills and settlement.

A "market-shy" model (the trained model's lineage copied with its blend set to half the
market's log-odds and no Elo term, inserted straight into the models table like the
other phases force statuses and metrics) is assigned to a shifted fixture game whose
home side is a 0.36 underdog. The model values home above the ask and buys it twice
(one order at a time, min_edge opening and closing the window). The moneylines then
move so the sim reprices home to about 0.69: the market overshoots the model (0.60),
so the worker proposes a SELL at the bid; buys stay blocked by a liquidity floor no
book meets, which sells skip by design. The snapshot poller is held (a long cadence)
while the participation setting sizes the sell to half the position and then lets
each later snapshot fill half the order: the paper simulator fills it partially, then
fully, and the ledger replays clean after each fill. simulate-final (home wins) then
writes a sold row and pro-rata buy rows for the contracts still held, whose P&L adds
up to the bankroll's realized change.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fleet.models.base import params_hash
from fleet.sim.odds import expit, logit
from host.exchange.adapters.sim import home_mid
from host.trading.ledger import replay_problems
from tests.e2e_trading import ExchangeThread, SimClock, orders_of, rows, run_cli, set_min_edge, shifted_game_csv

SOURCE_GAME = "2025_03_GB_CLE"
SELL_GAME = "2026_07_GB_CLE"
BANKROLL_CENTS = 20_000
BEFORE_ML = (170, -200)  # home (CLE) a 0.36 underdog
AFTER_ML = (-250, 210)  # the market reprices home to about 0.69
SHY_BLEND = {"a": 0.0, "b": 0.5, "c": 0.0}
SNAPSHOT_S = 6
FREEZE_S = 300
FILL_TIMEOUT = SNAPSHOT_S + 8.0
SETTINGS = {
    "trade_tick_s": 1, "snapshot_active_s": SNAPSHOT_S, "liquidity_floor_cents": 10_000, "min_edge": 1.0,
    "fee_model": {"taker_rate": 0.02, "half_spread": 0.01}, "kelly_fraction": 0.5, "max_bet_cents": 400,
    "participation": 0.5,
}
NO_BOOK_MEETS = 10**11  # a liquidity floor above every book: buys stop, sells do not check it


def _db(host: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(host.database_url, autocommit=True, row_factory=dict_row) as conn:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def ledger_clean(host: Any) -> None:
    with psycopg.connect(host.database_url, row_factory=dict_row) as conn:
        assert replay_problems(conn) == [], replay_problems(conn)


def set_moneylines(host: Any, home: int, away: int) -> None:
    _db(host, "UPDATE games SET home_moneyline = %s, away_moneyline = %s WHERE game_id = %s", (home, away, SELL_GAME))


def market_shy_model(host: Any, child_id: str) -> str:
    """A new lineage: the trained model's params and ratings, blend a=0, b=0.5, c=0."""
    child = _db(host, "SELECT params, artifact, trained_through FROM models WHERE id = %s", (child_id,))[0]
    params = dict(child["params"], min_edge=0.01)
    artifact = dict(child["artifact"], blend=SHY_BLEND)
    model_id = str(uuid.uuid4())
    _db(host,
        "INSERT INTO models (id, lineage_id, family, params, params_hash, artifact, trained_through, summary)"
        " VALUES (%s, %s, 'elo_blend', %s, %s, %s, %s, %s)",
        (model_id, model_id, Jsonb(params), params_hash(params), Jsonb(artifact), Jsonb(child["trained_through"]),
         "A market-shy test blend: half the market's log-odds, no Elo term."))
    return model_id


def quiet_minute(clock: SimClock) -> int:
    """A sim minute whose random walk is (near) zero, so the book sits at the moneylines."""
    flat = {"game_id": SELL_GAME, "home_moneyline": None, "away_moneyline": None}
    return next(k for k in range(240) if abs(home_mid(flat, clock.at(k)) - 0.5) < 0.006)


def latest(host: Any, market_id: str) -> dict[str, Any] | None:
    found = _db(host, "SELECT * FROM price_snapshots WHERE market_id = %s ORDER BY ts DESC, id DESC LIMIT 1", (market_id,))
    return found[0] if found else None


def new_order(host: Any, aid: str, seen: set[str], order_side: str) -> Callable[[], Any]:
    """Predicate: an order of the assignment not in `seen`, approved or further along."""

    def check() -> Any:
        for o in orders_of(host, aid):
            if o["id"] not in seen and o["order_side"] == order_side and o["status"] in ("open", "partial", "filled"):
                return o
            assert o["status"] != "rejected" or o["id"] in seen, f"rejected: {o['reject_reason']}"
        return False

    return check


def phase_sells(host: Any, state_dir: str, worker_id: str, models: dict[str, Any], tmp_path: Path,
                wait_for: Callable[..., Any], settled: Callable[..., Any]) -> None:
    started = time.monotonic()
    saved = {k: host.get("/api/settings")[k] for k in SETTINGS}
    csv_path, _ = shifted_game_csv(tmp_path, SOURCE_GAME, SELL_GAME, days_ahead=3)
    assert "1 inserted" in run_cli(["ingest-games", "--file", str(csv_path)])
    set_moneylines(host, *BEFORE_ML)
    model_id = market_shy_model(host, models["child"])
    host.post("/api/settings", SETTINGS)
    clock = SimClock()
    clock.minute = quiet_minute(clock)
    exchange = ExchangeThread(host.database_url, clock).start()
    try:
        _run(host, worker_id, model_id, exchange, wait_for, settled)
    finally:
        exchange.close()
        host.post("/api/settings", saved)
    assert time.monotonic() - started < 90.0, "the sell phase stays inside the e2e budget"


def _buys(host: Any, aid: str, home_id: str, wait_for: Callable[..., Any]) -> list[dict[str, Any]]:
    """Two buys of the home side, one window each; returns the filled buy orders."""
    seen: set[str] = set()
    for n in (1, 2):
        set_min_edge(host, 0.0)
        order = wait_for(new_order(host, aid, seen, "buy"), f"buy {n} approved", timeout=15.0)
        set_min_edge(host, 1.0)
        assert order["market_id"] == home_id and order["mode"] == "paper" and order["rationale"].startswith("my 0.")
        shy = expit(0.5 * logit(float(order["market_p"])))
        assert abs(float(order["my_p"]) - shy) < 1e-3, "the model is half the market's log-odds"
        wait_for(lambda: all(o["status"] not in ("approved", "submitting", "open", "partial")
                             for o in orders_of(host, aid)), f"buy {n} filled", timeout=FILL_TIMEOUT)
        ledger_clean(host)
        seen = {o["id"] for o in orders_of(host, aid)}
    buys = [o for o in orders_of(host, aid) if o["order_side"] == "buy" and o["status"] == "filled"]
    assert len(buys) >= 2, orders_of(host, aid)
    return buys


def _run(host: Any, worker_id: str, model_id: str, exchange: ExchangeThread, wait_for: Callable[..., Any],
         settled: Callable[..., Any]) -> None:
    markets = wait_for(lambda: (ms := host.get(f"/api/markets?game_id={SELL_GAME}")) and len(ms) == 2
                       and all(m["mapping_confirmed"] and m["snapshot_age_s"] is not None for m in ms) and ms,
                       "two sim markets for the sell game")
    home_id = next(m["id"] for m in markets if m["side"] == "home")
    away_id = next(m["id"] for m in markets if m["side"] == "away")
    created = host.post("/api/assignments", {"game_id": SELL_GAME, "model_id": model_id, "bankroll_cents": BANKROLL_CENTS},
                        expect=201)
    aid, job_id = created["id"], created["job_id"]
    host.set_role(worker_id, "trade")
    wait_for(settled(host, worker_id, "trade"), "worker in trade for the sell")
    wait_for(lambda: host.job(job_id)["status"] == "leased", "sell trade job claimed")

    buys = _buys(host, aid, home_id, wait_for)
    position = host.get(f"/api/assignments/{aid}")["positions"]
    held = sum(int(o["filled_size"]) for o in buys)
    assert len(position) == 1 and position[0]["market_id"] == home_id and position[0]["size"] == held and held >= 4
    basis = int(position[0]["basis_cents"])

    # The market overshoots: home reprices to about 0.69, above the model's 0.60.
    host.post("/api/settings", {"liquidity_floor_cents": NO_BOOK_MEETS})
    set_moneylines(host, *AFTER_ML)
    exchange.loop.last_run.pop("discover", None)  # rediscover now: the sim reads the new lines
    wait_for(lambda: (h := latest(host, home_id)) and float(h["bid"]) >= 0.6
             and (a := latest(host, away_id)) and float(a["ask"]) <= 0.4, "the sim reprices home", timeout=SNAPSHOT_S + 8.0)
    host.post("/api/settings", {"snapshot_active_s": FREEZE_S})  # hold the poller: the next book waits for the test
    time.sleep(1.5)
    frozen = latest(host, home_id)
    bid, top = float(frozen["bid"]), float(frozen["bid_depth"][0][1])
    assert float(frozen["bid_depth"][0][0]) == bid and bid >= 0.6
    size = held // 2
    host.post("/api/settings", {"participation": (size + 0.5) / top, "min_edge": 0.0})
    sell = wait_for(new_order(host, aid, {o["id"] for o in buys}, "sell"), "a sell approved", timeout=15.0)
    set_min_edge(host, 1.0)
    assert sell["market_id"] == home_id and sell["size"] == size and float(sell["price"]) == bid, sell
    assert sell["snapshot_id"] == frozen["id"] and sell["status"] == "open" and sell["cost_cents"] == 0
    assert sell["rationale"].startswith("sell: my 0.") and float(sell["my_p"]) < bid and sell["edge"] > 0
    assert abs(float(sell["my_p"]) - expit(0.5 * logit(float(sell["market_p"])))) < 1e-3

    # Each later snapshot now fills about half of the order: partial, then full.
    first = (size + 1) // 2
    host.post("/api/settings", {"participation": (first + 0.5) / top, "snapshot_active_s": SNAPSHOT_S})
    part = wait_for(lambda: (o := host.get(f"/api/orders/{sell['id']}"))["filled_size"] > 0 and o, "the sell partly filled",
                    timeout=FILL_TIMEOUT)
    assert part["status"] == "partial" and part["filled_size"] == first, "one snapshot fills only part of it"
    ledger_clean(host)
    wait_for(lambda: host.get(f"/api/orders/{sell['id']}")["status"] == "filled", "the sell filled", timeout=FILL_TIMEOUT)
    ledger_clean(host)
    fills = _db(host, "SELECT * FROM fills WHERE order_id = %s ORDER BY id", (sell["id"],))
    assert [f["size"] for f in fills] == [first, size - first] and all(f["exchange_fill_id"].endswith(":b0") for f in fills)
    assert all(float(f["price"]) == bid for f in fills)
    expect_basis, left, left_basis = [], held, basis
    for f in fills:
        part_basis = left_basis if f["size"] >= left else (2 * left_basis * f["size"] + left) // (2 * left)
        expect_basis.append(part_basis)
        left, left_basis = left - f["size"], left_basis - part_basis
    assert [int(f["basis_cents"]) for f in fills] == expect_basis, "basis removed at the average cost"
    ledger = _db(host, "SELECT * FROM ledger WHERE kind = 'sell' AND ref_id = %s ORDER BY id", (str(sell["id"]),))
    assert len(ledger) == 2
    for f, row in zip(fills, ledger):
        proceeds = round(float(f["price"]) * f["size"] * 100)
        assert row["d_open"] == -f["basis_cents"] and row["d_available"] == proceeds - f["fee_cents"]
        assert row["d_realized"] == proceeds - f["fee_cents"] - f["basis_cents"] and row["d_reserved"] == 0
    sold_basis, sold_realized = sum(expect_basis), sum(r["d_realized"] for r in ledger)
    now = host.get(f"/api/assignments/{aid}")
    assert now["positions"][0]["size"] == held - size and now["positions"][0]["basis_cents"] == basis - sold_basis
    buy_fees = sum(int(f["fee_cents"]) for o in buys
                   for f in _db(host, "SELECT fee_cents FROM fills WHERE order_id = %s", (o["id"],)))
    assert now["bankroll"]["realized_pnl_cents"] == sold_realized - buy_fees, "realized: the sale less the buy fees"
    page = host.client.get("/trading").text
    assert '<span class="chip chip-sell">sell</span>' in page and f'data-assignment="{aid}"' in page and sell["id"] in page

    # simulate-final, home wins: a sold row and pro-rata buy rows for what is still held.
    summary = json.loads(run_cli(["simulate-final", SELL_GAME, "--home", "27", "--away", "17"]))
    assert summary["winner"] == "home" and summary["bets"] == len(buys), "the summary counts buys, like n_bets"
    total = _check_bets(host, aid, model_id, sell, sold_basis, sold_realized, held - size)
    assert summary["pnl_cents"] == total, "the sale's P&L is part of the game's"
    wait_for(lambda: host.worker(worker_id)["current_jobs"] == [], "sell trade job dropped by the worker")
    assert host.job(job_id)["status"] == "succeeded"
    host.set_role(worker_id, "idle")
    wait_for(settled(host, worker_id, "idle"), "worker idle after the sell")


def _split(total: int, weights: list[int]) -> list[int]:
    parts = [total * w // sum(weights) for w in weights[:-1]]
    return parts + [total - sum(parts)]


def _check_bets(host: Any, aid: str, model_id: str, sell: dict[str, Any], sold_basis: int, sold_realized: int,
                still_held: int) -> int:
    """The settled rows against the fills and the ledger; returns the total P&L."""
    bets = {b["order_id"]: b for b in rows(host, "SELECT * FROM bets WHERE assignment_id = %s", (aid,))}
    sold = bets.pop(sell["id"])
    assert sold["order_side"] == "sell" and sold["result"] == "sold" and sold["stake_cents"] == 0 and sold["clv"] is None
    assert sold["cost_cents"] == sold_basis and sold["pnl_cents"] == sold_realized
    buys = _db(host, "SELECT id FROM orders WHERE assignment_id = %s AND side = 'buy' AND filled_size > 0"
               " ORDER BY created_at, id", (aid,))
    totals = [_db(host, "SELECT sum(size) AS size, sum(basis_cents) AS basis, sum(fee_cents) AS fee FROM fills"
                  " WHERE order_id = %s", (o["id"],))[0] for o in buys]
    bought = [int(t["basis"]) for t in totals]
    basis_parts = _split(sum(bought) - sold_basis, bought)
    payout_parts = _split(still_held * 100, [int(t["size"]) for t in totals])
    assert set(bets) == {str(o["id"]) for o in buys}
    for order, t, part, payout in zip(buys, totals, basis_parts, payout_parts):
        bet = bets[str(order["id"])]
        assert bet["order_side"] == "buy" and bet["result"] == "win" and bet["clv"] is not None
        assert bet["cost_cents"] == part and bet["stake_cents"] == int(t["basis"]) + int(t["fee"])
        assert bet["pnl_cents"] == payout - part - int(t["fee"]), "the buy row covers its share of what is held"
    total = sold["pnl_cents"] + sum(b["pnl_cents"] for b in bets.values())
    done = host.get(f"/api/assignments/{aid}")
    assert done["status"] == "settled" and done["positions"] == []
    assert done["bankroll"]["realized_pnl_cents"] == total and done["bankroll"]["available_cents"] == BANKROLL_CENTS + total
    assert done["bankroll"]["open_cost_cents"] == 0 and done["bankroll"]["reserved_cents"] == 0
    score = rows(host, "SELECT * FROM model_scores WHERE game_id = %s", (SELL_GAME,))
    assert len(score) == 1 and score[0]["model_id"] == model_id and score[0]["pnl_cents"] == total
    assert score[0]["n_bets"] == len(buys), "n_bets counts buys"
    ledger_clean(host)
    return total
