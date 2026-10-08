"""Step 4 phase of the end-to-end test (tests/test_e2e.py): the paper trading flow on
the real host, the real agent in the trade role and the real exchange loop (sim
market source) running in a thread.

A scheduled game two days ahead goes in through the ingest CLI (the fixture's BUF @
NYJ row shifted to 2026 week 5, both moneylines kept), the exchange discovers its two
sim markets and records snapshots, the owner assigns the model trained in the step 3
phase, the worker claims the trade job, proposes, the host approves and the paper
simulator fills; a lowered daily-loss limit (a host-only limit: the worker sizes
inside max_bet itself) forces a rejection with its reason; KILL cancels
every paper order inside the kill transaction and halts the assignment; RESUME and
"Activate all paper" bring it back; a role change away from trade cancels the open
order before the job is handed back, the next trade role reclaims it; simulate-final
settles everything: bets with CLV, a model score, the trade job succeeded, P&L on
/api/pnl, the paper columns on the leaderboard and a full /trading page.

The sim book is deterministic per (game, minute). The exchange thread's SimSource
gets a clock the test moves on purpose (the snapshot timestamps keep the real clock),
so an order rests exactly when the test wants one open: the test steps the sim to a
minute whose away ask is higher than the order's price.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

from fleet.worker import config as worker_config
from host import cli, db
from host.api.serialize import jsonable
from host.exchange.adapters.sim import SimSource, home_mid
from host.exchange.main import ExchangeLoop
from host.nflverse import EASTERN
from tests.conftest import FIXTURE_GAMES
from tests.pagecheck import page, shows_pnl

SOURCE_GAME = "2025_02_BUF_NYJ"
GAME_ID = "2026_05_BUF_NYJ"
SNAPSHOT_S = 6
BANKROLL_CENTS = 20_000
LOW_DAILY_LOSS = 1
TRADE_SETTINGS = {
    "trade_tick_s": 1,
    "min_edge": 0.0,
    "snapshot_active_s": SNAPSHOT_S,
    "liquidity_floor_cents": 10_000,
    "fee_model": {"taker_rate": 0.0, "half_spread": 0.01},
}
FILL_TIMEOUT = SNAPSHOT_S + 8.0
MID_STEP = 0.015


# ------------------------------------------------------------------ sim clock


class SimClock:
    """A clock for the SimSource that only moves when the test says so. It starts at the
    real minute (the sim's lookahead check reads it) and steps forward by whole minutes."""

    def __init__(self) -> None:
        self.base = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        self.minute = 0

    def __call__(self) -> datetime:
        return self.base + timedelta(minutes=self.minute)

    def at(self, minute: int) -> datetime:
        return self.base + timedelta(minutes=minute)


def pick_minutes(clock: SimClock, game: dict[str, Any]) -> list[int]:
    """Three sim minutes [m0, m1, m2] with a strictly falling home mid (each at least
    MID_STEP below the previous one), so stepping the clock forward lifts the away ask
    above any resting away order."""
    mids = [(home_mid(game, clock.at(k)), k) for k in range(60)]
    start_mid, start = max(mids)
    chosen = [start]
    current = start_mid
    for k in range(start + 1, 600):
        mid = home_mid(game, clock.at(k))
        if mid <= current - MID_STEP:
            chosen.append(k)
            current = mid
            if len(chosen) == 3:
                return chosen
    raise AssertionError(f"no falling sim minutes for {game['game_id']}: {chosen}")


class ExchangeThread:
    """The real exchange loop (python -m host.exchange.main) in a thread, sim source.
    With `gateway` (step 5) the loop's live gateway is that object and its credentials
    "load" (tests/fake_gateway.py), so the live tasks run."""

    def __init__(self, database_url: str, clock: SimClock, gateway: Any = None) -> None:
        self.pool = db.make_pool(database_url, min_size=1, max_size=4)
        if gateway is None:
            self.loop = ExchangeLoop(self.pool)
        else:
            from tests.fake_gateway import live_loop

            self.loop = live_loop(self.pool, gateway)
        self.loop.source = SimSource(clock=clock)
        self.loop.source_name = "sim"
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.loop.run_forever, args=(self.stop,), name="e2e-exchange", daemon=True)

    def start(self) -> "ExchangeThread":
        self.thread.start()
        return self

    def close(self) -> None:
        self.stop.set()
        self.thread.join(10.0)
        self.pool.close()


# ------------------------------------------------------------------- helpers


def rows(host: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Read rows straight from the host's database (bets and scores have no API)."""
    with psycopg.connect(host.database_url, row_factory=dict_row) as conn:
        return [jsonable(dict(r)) for r in conn.execute(sql, params).fetchall()]


def shifted_game_csv(directory: Path, source_game: str = SOURCE_GAME, game_id: str = GAME_ID, days_ahead: int = 2) -> tuple[Path, dict[str, Any]]:
    """The fixture's BUF @ NYJ row (or `source_game`) shifted to 2026 (the week in
    `game_id`), `days_ahead` days ahead, unplayed, as `game_id`."""
    with open(FIXTURE_GAMES, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        source = next(r for r in reader if r["game_id"] == source_game)
    gameday = (datetime.now(EASTERN) + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    row = dict(source)
    row.update({"game_id": game_id, "season": "2026", "week": game_id.split("_")[1].lstrip("0"), "gameday": gameday, "gametime": "13:00",
                "home_score": "", "away_score": "", "result": "", "total": "", "overtime": "", "old_game_id": "",
                "gsis": "", "pfr": "", "espn": "", "ftn": ""})
    path = directory / f"shifted_{game_id}.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        writer.writerow(row)
    game = {"game_id": game_id, "home_team": row["home_team"], "away_team": row["away_team"],
            "home_moneyline": int(row["home_moneyline"]), "away_moneyline": int(row["away_moneyline"])}
    return path, game


def run_cli(args: list[str]) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(args) == 0, out.getvalue()
    return out.getvalue()


def orders_of(host: Any, assignment_id: str, status: str | None = None) -> list[dict[str, Any]]:
    query = f"/api/orders?assignment_id={assignment_id}&limit=200" + (f"&status={status}" if status else "")
    return host.get(query)


def order_in(host: Any, assignment_id: str, status: str, exclude: set[str] = frozenset()) -> Callable[[], Any]:
    """Predicate: the newest order of the assignment in `status` not in `exclude`."""

    def check() -> Any:
        for o in orders_of(host, assignment_id):
            if o["status"] == status and o["id"] not in exclude:
                return o
        return False

    return check


def order_status(host: Any, order_id: str) -> str:
    return host.get(f"/api/orders/{order_id}")["status"]


def newest_snapshot_id(host: Any, market_id: str) -> int | None:
    found = rows(host, "SELECT id FROM price_snapshots WHERE market_id = %s ORDER BY ts DESC, id DESC LIMIT 1", (market_id,))
    return int(found[0]["id"]) if found else None


def rest_open_order(host: Any, clock: SimClock, minute: int, order: dict[str, Any], wait_for: Callable[..., Any],
                    game_id: str = GAME_ID) -> None:
    """Step the sim to `minute` (a higher away ask) and see the next snapshot leave the
    open order unfilled: it rests on the book."""
    before = newest_snapshot_id(host, order["market_id"])
    clock.minute = minute
    wait_for(lambda: newest_snapshot_id(host, order["market_id"]) != before, "a snapshot at the new sim minute", timeout=SNAPSHOT_S + 5.0)
    time.sleep(1.5)  # the fills task runs every second; it must leave the order alone
    current = host.get(f"/api/orders/{order['id']}")
    assert current["status"] == "open" and current["filled_size"] == 0, current
    market = next(m for m in host.get(f"/api/markets?game_id={game_id}") if m["id"] == order["market_id"])
    assert float(market["best_ask"]) > float(order["price"]), "the ask moved above the resting order"


def set_min_edge(host: Any, value: float) -> None:
    """min_edge 1.0 pauses proposals (an owner knob) while the test arranges the next
    step; 0.0 lets the thin sim edge through again."""
    host.post("/api/settings", {"min_edge": value})


def trade_status(state_dir: str) -> dict[str, Any]:
    return (worker_config.load_status(state_dir) or {}).get("trade") or {}


# ------------------------------------------------------------------- the phase


def phase_trading(host: Any, state_dir: str, worker_id: str, agent: Any, models: dict[str, Any], tmp_path: Path,
                  wait_for: Callable[..., Any], settled: Callable[..., Any]) -> None:
    started = time.monotonic()
    model_id = models["child"]
    csv_path, game = shifted_game_csv(tmp_path)
    out = run_cli(["ingest-games", "--file", str(csv_path)])
    assert out.startswith("ingested 1 rows from ") and "1 inserted, 0 changed" in out, out
    host.post("/api/settings", TRADE_SETTINGS)
    settings = host.get("/api/settings")
    daily_loss, default_kelly = settings["max_daily_loss_cents"], float(settings["kelly_fraction"])
    clock = SimClock()
    minutes = pick_minutes(clock, game)
    clock.minute = minutes[0]
    exchange = ExchangeThread(host.database_url, clock).start()
    try:
        _run(host, state_dir, worker_id, agent, model_id, clock, minutes, daily_loss, default_kelly, wait_for, settled)
    finally:
        exchange.close()
    assert time.monotonic() - started < 120.0, "the trading phase stays well inside the e2e budget"


def _run(host: Any, state_dir: str, worker_id: str, agent: Any, model_id: str, clock: SimClock, minutes: list[int],
         daily_loss: dict[str, int], default_kelly: float, wait_for: Callable[..., Any], settled: Callable[..., Any]) -> None:
    # Discovery: two confirmed sim markets, each with a snapshot; the exchange heartbeats.
    markets = wait_for(
        lambda: (ms := host.get(f"/api/markets?game_id={GAME_ID}")) and len(ms) == 2
        and all(m["mapping_confirmed"] and m["snapshot_age_s"] is not None for m in ms) and ms,
        "two sim markets with snapshots",
    )
    assert {m["side"] for m in markets} == {"home", "away"} and all(m["platform"] == "sim" for m in markets)
    away = next(m for m in markets if m["side"] == "away")
    exchange_state = host.get("/api/exchange")
    assert exchange_state["down"] is False and exchange_state["market_source"] == "sim" and exchange_state["last_error"] is None
    assert not page(host.client.get("/fragments/topbar").text).has('[data-banner="exchange-down"]')

    # The owner assigns the trained model to the game (bankroll, trade job).
    created = host.post("/api/assignments", {"game_id": GAME_ID, "model_id": model_id, "bankroll_cents": BANKROLL_CENTS}, expect=201)
    aid, job_id = created["id"], created["job_id"]
    assert created["status"] == "active" and created["bankroll"]["available_cents"] == BANKROLL_CENTS
    assert host.job(job_id)["kind"] == "trade" and host.job(job_id)["status"] == "queued"
    assert GAME_ID in page(host.client.get("/trading").text).row("assignment", aid).text

    # The worker takes the trade role, claims the job, ticks and proposes; the host
    # approves (the away side carries the edge against the sim book) and the paper
    # simulator fills after the next snapshot.
    host.set_role(worker_id, "trade")
    wait_for(settled(host, worker_id, "trade"), "worker in the trade role")
    leased = wait_for(lambda: (j := host.job(job_id))["status"] == "leased" and j["lease_worker_id"] == worker_id and j, "trade job claimed")
    assert host.worker(worker_id)["current_jobs"][0]["id"] == job_id
    first = wait_for(order_in(host, aid, "open"), "first proposal approved and open", timeout=15.0)
    set_min_edge(host, 1.0)  # one order at a time: nothing new until the test asks
    assert first["market_id"] == away["id"] and first["worker_id"] == worker_id and first["mode"] == "paper"
    assert first["edge"] > 0 and first["rationale"].startswith("my 0.") and first["snapshot_id"]
    assert first["price"] == float(first["price"]) and first["size"] >= 1
    assert host.get(f"/api/orders/{first['id']}")["events"][-1]["to_status"] == "open"
    wait_for(lambda: order_status(host, first["id"]) == "filled", "first order filled by the paper simulator", timeout=FILL_TIMEOUT)
    fills = [f for f in host.get("/api/fills?limit=50") if f["order_id"] == first["id"]]
    assert fills and sum(f["size"] for f in fills) == first["size"] and all(float(f["price"]) <= float(first["price"]) for f in fills)
    assert all(f["snapshot_id"] > first["snapshot_id"] for f in fills), "fills come from snapshots after the submission"
    detail = host.get(f"/api/assignments/{aid}")
    assert detail["positions"] and detail["positions"][0]["market_id"] == away["id"] and detail["positions"][0]["size"] == first["size"]
    assert host.get(f"/api/assignments/{aid}")["bankroll"]["open_cost_cents"] > 0
    assert "ledger ok" in run_cli(["ledger-check"])

    # The Kelly stake is a target position: the filled first order sits at it, so the
    # owner doubles the Kelly fraction to make room for the orders that follow.
    host.post("/api/settings", {"kelly_fraction": 2 * default_kelly})
    # A lowered paper daily-loss limit (one the worker cannot see) rejects the next
    # proposal with its reason; the fleet max bet travels to the worker, which sizes
    # inside it instead of being rejected.
    host.post("/api/settings", {"max_daily_loss_cents": dict(daily_loss, paper=LOW_DAILY_LOSS), "min_edge": 0.0})
    rejected = wait_for(order_in(host, aid, "rejected"), "a proposal rejected for daily_loss", timeout=15.0)
    assert rejected["reject_reason"] == "daily_loss" and rejected["cost_cents"] > LOW_DAILY_LOSS
    assert host.get(f"/api/orders/{rejected['id']}")["events"][-1]["detail"] == {"reason": "daily_loss"}
    assert "Daily Loss: daily loss limit" in page(host.client.get("/trading").text).card("orders").row("order", rejected["id"]).text
    host.post("/api/settings", {"max_daily_loss_cents": daily_loss})

    # The next approval opens, the sim moves against it and it rests; KILL cancels it
    # inside the kill transaction and halts the assignment.
    second = wait_for(order_in(host, aid, "open", exclude={first["id"]}), "second order open", timeout=15.0)
    set_min_edge(host, 1.0)
    rest_open_order(host, clock, minutes[1], second, wait_for)
    t_kill = time.monotonic()
    resp = host.form("/kill")
    assert resp.headers["location"] == "/"
    assert host.get("/api/orders?status=active") == [], "no active order survives the kill"
    assert time.monotonic() - t_kill < 2.0
    killed = host.get(f"/api/orders/{second['id']}")
    assert killed["status"] == "cancelled" and killed["filled_size"] == 0
    assert [e["to_status"] for e in killed["events"]] == ["approved", "submitting", "open", "cancelled"]
    assignment = host.get(f"/api/assignments/{aid}")
    assert assignment["status"] == "halted" and assignment["bankroll"]["reserved_cents"] == 0
    audit = host.get("/api/audit?limit=5")
    assert audit[0]["action"] == "kill_cancel_all" and audit[1]["action"] == "kill"
    assert audit[0]["after"]["orders_cancelled"] == [second["id"]] and audit[0]["after"]["assignments_halted"] == [aid]
    assert host.job(job_id)["status"] == "leased", "a kill leaves the trade job with its worker"
    wait_for(lambda: (t := trade_status(state_dir)).get("last_tick", {}).get("kill") is True and t, "the worker's tick sees the kill")
    wait_for(lambda: agent.agent.last_response.get("kill") is True, "heartbeat reply carries kill")
    assert "ledger ok" in run_cli(["ledger-check"])

    # RESUME clears the flag only; "Activate all paper" brings the assignment back.
    host.form("/kill/reset", {"confirm": "RESUME"})
    assert host.get(f"/api/assignments/{aid}")["status"] == "halted"
    assert page(host.client.get("/trading").text).action("activate-all-paper").target == "/assignments/activate-paper"
    resp = host.form("/assignments/activate-paper")
    assert resp.headers["location"] == "/trading"
    assert host.get(f"/api/assignments/{aid}")["status"] == "active"
    wait_for(lambda: trade_status(state_dir).get("last_tick", {}).get("kill") is False, "the worker's tick sees the reset")
    set_min_edge(host, 0.0)

    # The worker proposes again; the order rests; switching the role away from trade
    # cancels it first (the release handshake) and hands the job back to the queue.
    third = wait_for(order_in(host, aid, "open", exclude={first["id"], second["id"]}), "third order open after the reset", timeout=15.0)
    set_min_edge(host, 1.0)
    rest_open_order(host, clock, minutes[2], third, wait_for)
    host.set_role(worker_id, "idle")
    wait_for(settled(host, worker_id, "idle"), "worker idle after trade")
    released = host.get(f"/api/orders/{third['id']}")
    assert released["status"] == "cancelled" and released["events"][-1]["detail"] == {"reason": "drain"}
    assert released["events"][-1]["actor"] == worker_id, "cancelled by the worker's own release call"
    queued = host.job(job_id)
    assert queued["status"] == "queued" and queued["lease_worker_id"] is None
    events = host.job(job_id)["events"]
    assert events[-1]["event"] == "released" and events[-1]["detail"] == {"status": "queued", "reason": "drain"}
    assert host.get(f"/api/assignments/{aid}")["status"] == "active"
    assert host.get("/api/orders?status=active") == []

    # Back in the trade role the worker reclaims the job and trades on; a fill lands.
    set_min_edge(host, 0.0)
    host.set_role(worker_id, "trade")
    wait_for(settled(host, worker_id, "trade"), "worker back in trade")
    wait_for(lambda: (j := host.job(job_id))["status"] == "leased" and j["lease_worker_id"] == worker_id, "trade job reclaimed")
    assert host.events(job_id).count("claimed") == 2
    fourth = wait_for(order_in(host, aid, "open", exclude={first["id"], second["id"], third["id"]}), "fourth order open", timeout=15.0)
    set_min_edge(host, 1.0)
    wait_for(lambda: order_status(host, fourth["id"]) == "filled", "fourth order filled", timeout=FILL_TIMEOUT)
    assert "ledger ok" in run_cli(["ledger-check"])

    # simulate-final: the away side wins; bets with CLV, a score, the job succeeded,
    # P&L everywhere and the leaderboard's paper columns.
    out = run_cli(["simulate-final", GAME_ID, "--home", "17", "--away", "27"])
    summary = json.loads(out)
    assert summary["winner"] == "away" and summary["assignments"] == 1 and summary["bets"] == 2, out
    assert [x["lineage_id"] for x in summary["lineages"]] == [models_root(host, model_id)]
    assert summary["lineages"][0]["status"] in ("candidate", "paper_ok"), "one game: no promotion to live_eligible"
    bets = rows(host, "SELECT * FROM bets WHERE assignment_id = %s ORDER BY id", (aid,))
    assert sorted(o["status"] for o in orders_of(host, aid)) == sorted(["filled", "cancelled", "cancelled", "filled"] + ["rejected"] * (len(orders_of(host, aid)) - 4))
    assert {b["order_id"] for b in bets} == {first["id"], fourth["id"]}
    for bet in bets:
        assert bet["result"] == "win" and bet["side"] == "away" and bet["game_id"] == GAME_ID and bet["worker_id"] == worker_id
        assert bet["closing_price"] is not None and bet["clv"] is not None and bet["pnl_cents"] > 0 and bet["mode"] == "paper"
        assert abs(float(bet["closing_price"]) - float(bet["entry_price"]) - bet["clv"]) < 1e-5, "clv = closing - entry"
    pnl_cents = sum(b["pnl_cents"] for b in bets)
    score = rows(host, "SELECT * FROM model_scores WHERE game_id = %s", (GAME_ID,))
    assert len(score) == 1 and score[0]["model_id"] == model_id and score[0]["n_bets"] == 2 and score[0]["pnl_cents"] == pnl_cents
    assert score[0]["avg_clv"] is not None and score[0]["mode"] == "paper"
    settled_row = host.get(f"/api/assignments/{aid}")
    assert settled_row["status"] == "settled" and settled_row["settled_at"] and settled_row["positions"] == []
    assert settled_row["bankroll"]["open_cost_cents"] == 0 and settled_row["bankroll"]["reserved_cents"] == 0
    assert settled_row["bankroll"]["realized_pnl_cents"] == pnl_cents
    assert settled_row["bankroll"]["available_cents"] == BANKROLL_CENTS + pnl_cents
    assert [a["id"] for a in host.get("/api/assignments?status=settled")] == [aid]
    job = host.job(job_id)
    assert job["status"] == "succeeded" and job["result"]["n_bets"] == 2 and job["result"]["pnl_cents"] == pnl_cents
    assert job["result"]["assignment_id"] == aid and host.events(job_id)[-1] == "succeeded"
    pnl = host.get("/api/pnl")
    assert pnl["today_cents"] == pnl_cents and pnl["all_time_cents"] == pnl_cents
    assert pnl["by_worker"][worker_id] == pnl_cents and pnl["by_mode"]["paper"] == {"today_cents": pnl_cents, "all_time_cents": pnl_cents}
    assert pnl["by_mode"]["live"] == {"today_cents": 0, "all_time_cents": 0}
    dollars = f"${pnl_cents // 100}.{pnl_cents % 100:02d}"
    assert shows_pnl(host.client, "paper", dollars), "today's paper P&L is on view"
    board = host.get("/api/models")
    entry = next(m for m in board["ranked"] + board["unranked"] if m["id"] == models_root(host, model_id))
    assert entry["paper"]["games"] == 1 and entry["paper"]["bets"] == 2 and entry["paper"]["pnl_cents"] == pnl_cents
    assert entry["paper"]["avg_clv"] is not None and entry["rank_mode"] == "validation", "one game: not yet ranked on paper (step 6A ranks by the validation era)"
    assert page(host.client.get("/models").text).row("model", entry["id"])
    assert dollars in page(host.client.get(f"/models/{entry['id']}").text).prop("Paper"), "the paper stat on the model page"
    record = page(host.client.get(f"/models/{model_id}").text).prop("paper record")
    assert record.startswith("1 game · 2 bets · ") and dollars in record, record
    trading = page(host.client.get("/trading").text)
    row = trading.row("assignment", aid)
    assert row.chip("settled").text == "Settled" and GAME_ID in row.text
    listed = set(trading.row_ids("order"))
    for order_id in (first["id"], second["id"], third["id"], fourth["id"], rejected["id"]):
        assert str(order_id) in listed, order_id
    assert "Daily Loss: daily loss limit" in trading.card("orders").row("order", rejected["id"]).text
    fills = trading.card("fills")
    assert fills.rows("fill") and "No fills yet." not in fills.text
    ledger = trading.card("ledger")
    assert ledger.chip("ledger-ok").text == "OK" and "Replay of 1 bankroll" in ledger.text
    exchange = trading.card("exchange")
    assert not exchange.has('[data-chip="exchange-down"]') and exchange.prop("source") == "Sim"
    assert host.client.get("/fragments/trading").status_code == 200
    assert "ledger ok" in run_cli(["ledger-check"])

    # The worker learns the job is gone and goes back to idle for the crash phase.
    wait_for(lambda: host.worker(worker_id)["current_jobs"] == [], "trade job dropped by the worker")
    host.set_role(worker_id, "idle")
    wait_for(settled(host, worker_id, "idle"), "worker idle after trading")


def models_root(host: Any, model_id: str) -> str:
    """The leaderboard lists lineages by their root model's id."""
    return host.get(f"/api/models/{model_id}")["lineage_id"]
