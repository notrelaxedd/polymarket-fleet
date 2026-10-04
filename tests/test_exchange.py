"""Exchange process: mapping confidence, snapshot cadence and liquidity, closing price
freeze, retention and bars, the executor outbox (kill race, GTD expiry, cancel retry),
the rate limiter and the heartbeat. The helpers here are shared with
test_paper_fills, test_settlement and test_scores."""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from host import kill
from host.exchange import executor, mapping, ratelimit, retention, snapshots, state
from host.exchange.adapters.base import Book, MarketInfo, NotConfigured, OrderGateway, PaperGateway
from host.nflverse import EASTERN as _EASTERN
from host.exchange.adapters.sim import SimSource
from host.exchange.main import ExchangeLoop, run_once
from host.exchange.paper import fee_per_contract
from host.trading import ledger, orders
from tests.conftest import backtest_metrics, insert_model

NOW = datetime.now(timezone.utc).replace(microsecond=0)
# Two days out at 13:00 Eastern, so the mapping tests' +1 h / +2 h starts stay on the
# same Eastern gameday whatever the hour the suite runs at (02:00 to 04:00 UTC used
# to cross the Eastern midnight).
KICKOFF = (NOW + timedelta(days=2)).astimezone(_EASTERN).replace(hour=13, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


# ---------------------------------------------------------------------- helpers

def make_game(
    conn: psycopg.Connection, game_id: str = "2026_05_KC_LV", home: str = "LV", away: str = "KC",
    kickoff: datetime = KICKOFF, status: str = "scheduled", home_score: int | None = None, away_score: int | None = None,
    home_ml: int | None = 150, away_ml: int | None = -175,
) -> dict[str, Any]:
    from host.nflverse import EASTERN

    gameday = kickoff.astimezone(EASTERN).date()
    return dict(conn.execute(
        """
        INSERT INTO games (game_id, season, game_type, week, gameday, gametime, kickoff_at, home_team, away_team,
                           home_score, away_score, home_moneyline, away_moneyline, status, raw)
        VALUES (%s, 2026, 'REG', 5, %s, '16:15', %s, %s, %s, %s, %s, %s, %s, %s, '{}') RETURNING *
        """,
        (game_id, gameday, kickoff, home, away, home_score, away_score, home_ml, away_ml, status),
    ).fetchone())


def make_model(conn: psycopg.Connection, status: str = "paper_ok", **kw: Any) -> dict[str, Any]:
    kw.setdefault("params", {"k": 24.0, "hfa": 55.0, "mov_scale": 1, "seed": uuid.uuid4().hex[:6]})
    return insert_model(conn, metrics=backtest_metrics(), status=status, **kw)


def make_assignment(
    conn: psycopg.Connection, game_id: str, model: dict[str, Any], mode: str = "paper", bankroll_cents: int = 10_000,
    status: str = "active", job_status: str | None = "queued", worker_id: str | None = None,
) -> dict[str, Any]:
    """An assignment row, its funded bankroll and (optionally) its trade job."""
    job_id = None
    if job_status is not None:
        lease = job_status in ("leased", "cancel_requested")
        job = conn.execute(
            """
            INSERT INTO jobs (kind, role, status, params, max_expiries, idempotency_key, lease_worker_id, lease_token, lease_expires_at)
            VALUES ('trade', 'trade', %s, %s, NULL, %s, %s, %s, %s) RETURNING id
            """,
            (job_status, Jsonb({}), f"assignment:{uuid.uuid4()}", worker_id if lease else None,
             uuid.uuid4() if lease else None, NOW + timedelta(seconds=30) if lease else None),
        ).fetchone()
        job_id = job["id"]
    row = conn.execute(
        """
        INSERT INTO assignments (game_id, model_id, lineage_id, mode, job_id, status)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (game_id, model["id"], model["lineage_id"], mode, job_id, status),
    ).fetchone()
    if job_id is not None:
        conn.execute("UPDATE jobs SET params = %s WHERE id = %s", (Jsonb({"assignment_id": str(row["id"])}), job_id))
    bank = ledger.create_bankroll(conn, row["id"], mode, bankroll_cents)
    return {**dict(row), "bankroll": bank}


def make_market(
    conn: psycopg.Connection, game_id: str | None, side: str | None, platform: str = "sim", ref: str | None = None,
    confirmed: bool = True, title: str | None = None,
) -> dict[str, Any]:
    ref = ref or f"{platform}:{game_id}:{side}:{uuid.uuid4().hex[:6]}"
    return dict(conn.execute(
        """
        INSERT INTO markets (platform, market_ref, title, game_id, side, mapping_confirmed, mapping_confidence)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (platform, ref, title or f"{side} YES {game_id}", game_id, side, confirmed, 1.0 if confirmed else 0.5),
    ).fetchone())


def book(bid: float, ask: float, size: float = 200, levels: int = 3, when: datetime = NOW) -> Book:
    bids = [[round(bid - 0.01 * i, 4), size] for i in range(levels)]
    asks = [[round(ask + 0.01 * i, 4), size] for i in range(levels)]
    return Book(bids=bids, asks=asks, fetched_at=when)


def snap(conn: psycopg.Connection, market_id: Any, bid: float, ask: float, when: datetime = NOW, size: float = 200, levels: int = 3) -> dict[str, Any]:
    return snapshots.record_snapshot(conn, market_id, book(bid, ask, size, levels, when), when)


def order_cost_cents(price: float, size: int, fee_model: dict[str, Any] | None = None) -> int:
    return int(math.ceil(size * (price + fee_per_contract(price, fee_model)) * 100))


def make_order(
    conn: psycopg.Connection, assignment: dict[str, Any], market: dict[str, Any], price: float, size: int,
    status: str = "approved", snapshot_id: int | None = None, worker_id: str | None = None,
    submitted_at: datetime | None = None, gtd_at: datetime | None = None, my_p: float | None = None,
    market_p: float | None = None, edge: float | None = None,
) -> dict[str, Any]:
    """An order in `status` with its reservation posted (as approval would)."""
    cost = order_cost_cents(price, size)
    row = conn.execute(
        """
        INSERT INTO orders (client_request_id, assignment_id, worker_id, job_id, market_id, mode, price, size, cost_cents,
                            fee_cents_est, snapshot_id, status, submitted_at, gtd_at, my_p, market_p, edge, rationale)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'test') RETURNING *
        """,
        (uuid.uuid4().hex[:32], assignment["id"], worker_id, assignment.get("job_id"), market["id"], assignment["mode"],
         price, size, cost, cost - int(round(price * size * 100)), snapshot_id, status, submitted_at, gtd_at, my_p, market_p, edge),
    ).fetchone()
    ledger.reserve(conn, assignment["bankroll"]["id"], cost, row["id"])
    orders.add_order_event(conn, row["id"], None, status, "test")
    return dict(row)


def bankroll(conn: psycopg.Connection, assignment: dict[str, Any]) -> dict[str, Any]:
    return ledger.bankroll_for_assignment(conn, assignment["id"])


def order(conn: psycopg.Connection, order_id: Any) -> dict[str, Any]:
    return orders.get_order(conn, order_id)


def events(conn: psycopg.Connection, order_id: Any) -> list[tuple[str | None, str | None]]:
    rows = conn.execute("SELECT from_status, to_status FROM order_events WHERE order_id = %s ORDER BY id", (order_id,)).fetchall()
    return [(r["from_status"], r["to_status"]) for r in rows]


def info(home: str, away: str, side: str | None, kickoff: datetime | None, ref: str = "pm:1", platform: str = "polymarket_us") -> MarketInfo:
    return MarketInfo(platform, ref, "ev", f"{away} at {home} [{side}]", home, away, side, kickoff, 0.01, 1, {"ref": ref})


# ---------------------------------------------------------------------- mapping

def test_mapping_confidence_rules(conn):
    make_game(conn)
    exact = mapping.match(conn, info("LV", "KC", "away", KICKOFF), NOW, 8)
    assert exact == {"game_id": "2026_05_KC_LV", "side": "away", "confidence": 1.0, "confirmed": True}
    swapped = mapping.match(conn, info("Kansas City Chiefs", "Las Vegas Raiders", "home", KICKOFF + timedelta(hours=2)), NOW, 8)
    assert swapped["game_id"] == "2026_05_KC_LV" and swapped["side"] == "away" and swapped["confirmed"], "the YES team decides the side"
    near = mapping.match(conn, info("LV", "KC", "away", KICKOFF + timedelta(hours=30)), NOW, 8)
    assert near["game_id"] == "2026_05_KC_LV" and near["confidence"] == 0.8 and not near["confirmed"], "within 36 h but another gameday"
    far = mapping.match(conn, info("LV", "KC", "away", KICKOFF + timedelta(hours=72)), NOW, 8)
    assert far["game_id"] is None and far["confidence"] == 0.0
    no_kick = mapping.match(conn, info("LV", "KC", "away", None), NOW, 8)
    assert no_kick["game_id"] == "2026_05_KC_LV" and no_kick["confidence"] == 0.5 and not no_kick["confirmed"]
    no_side = mapping.match(conn, info("LV", "KC", None, KICKOFF), NOW, 8)
    assert no_side["game_id"] == "2026_05_KC_LV" and no_side["side"] is None and no_side["confidence"] == 0.5 and not no_side["confirmed"]
    assert mapping.match(conn, info("LV", "Toronto", "home", KICKOFF), NOW, 8)["game_id"] is None
    assert mapping.match(conn, info("LV", "LV", "home", KICKOFF), NOW, 8)["game_id"] is None
    make_game(conn, "2026_05_KC_LV_b", "LV", "KC", KICKOFF + timedelta(hours=1))
    assert mapping.match(conn, info("LV", "KC", "away", KICKOFF), NOW, 8)["game_id"] is None, "two candidates on one gameday: unmatched"


def test_upsert_never_changes_a_confirmed_mapping(conn):
    make_game(conn)
    make_game(conn, "2026_05_SF_SEA", "SEA", "SF", KICKOFF)
    first = mapping.upsert_market(conn, info("LV", "KC", "away", KICKOFF), mapping.match(conn, info("LV", "KC", "away", KICKOFF), NOW, 8))
    assert first["mapping_confirmed"] and first["game_id"] == "2026_05_KC_LV" and first["side"] == "away"
    # The feed now says the market is about another game: title and raw refresh, the mapping stays.
    again = mapping.upsert_market(conn, info("SEA", "SF", "home", KICKOFF, ref="pm:1"), {"game_id": "2026_05_SF_SEA", "side": "home", "confidence": 1.0, "confirmed": True})
    assert again["id"] == first["id"] and again["game_id"] == "2026_05_KC_LV" and again["side"] == "away" and again["mapping_confirmed"]
    assert again["title"].startswith("SF at SEA") and again["raw"] == {"ref": "pm:1"}
    # An unconfirmed row follows the feed until the owner links it.
    weak = mapping.upsert_market(conn, info("LV", "KC", None, KICKOFF, ref="pm:2"), mapping.match(conn, info("LV", "KC", None, KICKOFF), NOW, 8))
    assert not weak["mapping_confirmed"] and weak["side"] is None
    linked = mapping.link_market(conn, weak["id"], "2026_05_KC_LV", "home", "owner")
    assert linked["mapping_confirmed"] and linked["side"] == "home" and linked["mapping_confidence"] == 1.0
    better = mapping.upsert_market(conn, info("LV", "KC", "away", KICKOFF, ref="pm:2"), mapping.match(conn, info("LV", "KC", "away", KICKOFF), NOW, 8))
    assert better["side"] == "home", "the owner's link wins over discovery"
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'market_linked'").fetchone()["n"] == 1
    with pytest.raises(Exception):
        mapping.link_market(conn, weak["id"], "2026_05_KC_LV", "sideways", "owner")


def test_discover_with_sim_confirms_two_markets_per_game(conn):
    make_game(conn)
    make_game(conn, "2026_09_SF_SEA", "SEA", "SF", NOW + timedelta(days=30))
    counts = mapping.discover(conn, SimSource(clock=lambda: NOW), NOW)
    assert counts == {"listed": 2, "confirmed": 2, "unmatched": 0, "upserted": 2}
    rows = conn.execute("SELECT market_ref, game_id, side, mapping_confirmed, mapping_confidence FROM markets ORDER BY market_ref").fetchall()
    assert [(r["game_id"], r["side"], r["mapping_confirmed"], r["mapping_confidence"]) for r in rows] == [("2026_05_KC_LV", "away", True, 1.0), ("2026_05_KC_LV", "home", True, 1.0)]
    assert mapping.discover(conn, SimSource(clock=lambda: NOW), NOW)["upserted"] == 2
    assert conn.execute("SELECT count(*) AS n FROM markets").fetchone()["n"] == 2, "rediscovery upserts, never duplicates"


# -------------------------------------------------------------------- snapshots

def test_snapshot_liquidity_within_five_cents_and_mirror(conn):
    make_game(conn)
    market = make_market(conn, "2026_05_KC_LV", "home")
    b = Book(bids=[[0.50, 100], [0.46, 100], [0.45, 100], [0.44, 1000]], asks=[[0.52, 100], [0.57, 100], [0.58, 1000]], fetched_at=NOW)
    assert snapshots.liquidity_usd_cents(b) == int(round((0.50 + 0.46 + 0.45 + 0.52 + 0.57) * 100 * 100))
    row = snapshots.record_snapshot(conn, market["id"], b, NOW)
    assert (float(row["bid"]), float(row["ask"]), float(row["mid"])) == (0.50, 0.52, 0.51)
    assert row["bid_depth"] == [[0.5, 100], [0.46, 100], [0.45, 100], [0.44, 1000]] and len(row["ask_depth"]) == 3
    m = conn.execute("SELECT * FROM markets WHERE id = %s", (market["id"],)).fetchone()
    assert (float(m["best_bid"]), float(m["best_ask"]), m["liquidity_usd_cents"], m["last_snapshot_at"]) == (0.50, 0.52, row["liquidity_usd_cents"], NOW)
    assert snapshots.latest_snapshot(conn, market["id"])["id"] == row["id"]
    assert snapshots.latest_snapshot(conn, uuid.uuid4()) is None
    empty = snapshots.record_snapshot(conn, market["id"], Book([], [], NOW), NOW + timedelta(seconds=1))
    assert empty["mid"] is None and empty["liquidity_usd_cents"] == 0


def test_snapshot_cadence_active_two_seconds_idle_thirty(conn):
    make_game(conn)
    make_game(conn, "2026_05_SF_SEA", "SEA", "SF", KICKOFF)
    make_game(conn, "2026_09_DAL_PHI", "PHI", "DAL", NOW + timedelta(days=30))
    active = make_market(conn, "2026_05_KC_LV", "home", ref="sim:2026_05_KC_LV:home")
    idle = make_market(conn, "2026_05_SF_SEA", "home", ref="sim:2026_05_SF_SEA:home")
    make_market(conn, "2026_09_DAL_PHI", "home", ref="sim:2026_09_DAL_PHI:home")
    unmatched = make_market(conn, None, None, confirmed=False)
    make_assignment(conn, "2026_05_KC_LV", make_model(conn))
    due = {m["id"] for m in snapshots.due_markets(conn, NOW, 2, 30, 8)}
    assert due == {active["id"], idle["id"]}, "never snapshotted: both due; the far game and unmatched markets are not"
    assert unmatched["id"] not in due
    source = SimSource(conn.execute("SELECT * FROM games").fetchall(), clock=lambda: NOW)
    counts = snapshots.poll(conn, source, None, NOW)
    assert counts["stored"] == 2
    assert snapshots.due_markets(conn, NOW + timedelta(seconds=1), 2, 30, 8) == []
    assert [m["id"] for m in snapshots.due_markets(conn, NOW + timedelta(seconds=2), 2, 30, 8)] == [active["id"]]
    assert {m["id"] for m in snapshots.due_markets(conn, NOW + timedelta(seconds=30), 2, 30, 8)} == {active["id"], idle["id"]}
    limiter = ratelimit.RateLimiter({"market_data_per_s": 1}, now=NOW)
    counts = snapshots.poll(conn, source, limiter, NOW + timedelta(seconds=31))
    assert counts["stored"] == 1 and counts["throttled"] == 1, "one market-data token per fetch"
    conn.execute("UPDATE markets SET status = 'resolved' WHERE id = %s", (active["id"],))
    assert active["id"] not in {m["id"] for m in snapshots.due_markets(conn, NOW + timedelta(minutes=5), 2, 30, 8)}


def test_closing_price_is_frozen_from_the_last_pre_kickoff_snapshot(conn):
    kickoff = NOW - timedelta(hours=1)
    make_game(conn, kickoff=kickoff)
    make_game(conn, "2026_05_SF_SEA", "SEA", "SF", kickoff)
    market = make_market(conn, "2026_05_KC_LV", "home")
    fallback = make_market(conn, "2026_05_SF_SEA", "home")
    snap(conn, market["id"], 0.40, 0.42, kickoff - timedelta(minutes=10))
    snap(conn, market["id"], 0.44, 0.46, kickoff - timedelta(seconds=1))
    snap(conn, market["id"], 0.60, 0.62, kickoff)
    snap(conn, market["id"], 0.70, 0.72, kickoff + timedelta(minutes=5))
    snap(conn, fallback["id"], 0.30, 0.32, kickoff + timedelta(minutes=1))
    assert snapshots.freeze_closing_prices(conn, kickoff - timedelta(hours=2)) == 0, "nothing before kickoff"
    assert snapshots.freeze_closing_prices(conn, NOW) == 2
    rows = {r["market_ref"]: r for r in conn.execute("SELECT market_ref, closing_price FROM markets").fetchall()}
    assert float(rows[market["market_ref"]]["closing_price"]) == 0.45, "mid of the last snapshot strictly before kickoff"
    assert float(rows[fallback["market_ref"]]["closing_price"]) == 0.31, "fallback: the last snapshot"
    snap(conn, market["id"], 0.10, 0.12, NOW)
    assert snapshots.freeze_closing_prices(conn, NOW) == 0
    assert float(conn.execute("SELECT closing_price FROM markets WHERE id = %s", (market["id"],)).fetchone()["closing_price"]) == 0.45
    bare = make_market(conn, "2026_05_KC_LV", "away")
    assert snapshots.freeze_closing_prices(conn, NOW) == 0 and conn.execute("SELECT closing_price FROM markets WHERE id = %s", (bare["id"],)).fetchone()["closing_price"] is None


# -------------------------------------------------------------------- retention

def test_retention_rolls_up_bars_and_deletes_raw_rows_idempotently(conn):
    kickoff = NOW - timedelta(days=20)
    make_game(conn, kickoff=kickoff, status="final", home_score=20, away_score=10)
    market = make_market(conn, "2026_05_KC_LV", "home")
    minute = (kickoff - timedelta(hours=1)).replace(second=0, microsecond=0)
    ids = [snap(conn, market["id"], b, b + 0.02, minute + timedelta(seconds=s))["id"] for s, b in ((0, 0.40), (20, 0.44), (40, 0.42))]
    ids.append(snap(conn, market["id"], 0.50, 0.52, minute + timedelta(minutes=1))["id"])
    recent = snap(conn, market["id"], 0.55, 0.57, NOW - timedelta(hours=1))
    assignment = make_assignment(conn, "2026_05_KC_LV", make_model(conn))
    cited = make_order(conn, assignment, market, 0.45, 10, snapshot_id=ids[2])
    result = retention.run(conn, NOW, days=14)
    assert result["bars"] == 2 and result["deleted"] == 3 and result["closing_frozen"] == 1
    left = {r["id"] for r in conn.execute("SELECT id FROM price_snapshots").fetchall()}
    assert left == {ids[2], recent["id"]}, "an order's cited snapshot and recent rows stay"
    bars = conn.execute("SELECT * FROM price_bars ORDER BY minute").fetchall()
    assert len(bars) == 2
    first, second = bars
    assert (float(first["open"]), float(first["high"]), float(first["low"]), float(first["close"]), first["n"]) == (0.41, 0.45, 0.41, 0.45, 2)
    assert (float(first["bid"]), float(first["ask"])) == (0.44, 0.46) and first["min_liquidity_usd_cents"] > 0
    assert (float(second["open"]), second["n"]) == (0.51, 1)
    assert float(conn.execute("SELECT closing_price FROM markets WHERE id = %s", (market["id"],)).fetchone()["closing_price"]) == 0.51
    again = retention.run(conn, NOW, days=14)
    assert again["bars"] == 0 and again["deleted"] == 0
    assert conn.execute("SELECT n FROM price_bars ORDER BY minute").fetchall()[0]["n"] == 2, "idempotent: bars not double counted"
    assert order(conn, cited["id"])["snapshot_id"] == ids[2]


# --------------------------------------------------------------------- executor

class FlakyGateway(OrderGateway):
    """Live-shaped gateway for the outbox tests: scripted place/cancel outcomes."""

    name = "flaky"

    def __init__(self, place=None, cancel_results=None, remote=None):
        self.place_fn = place
        self.cancel_results = list(cancel_results or [])
        self.remote = remote or []
        self.placed: list[str] = []
        self.cancels = 0

    def place(self, order):
        self.placed.append(order["client_request_id"])
        if self.place_fn is not None:
            return self.place_fn(order)
        return "ex-" + order["client_request_id"]

    def cancel(self, order):
        self.cancels += 1
        outcome = self.cancel_results.pop(0) if self.cancel_results else True
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def open_orders(self):
        return self.remote

    def fills(self, since):
        return []  # a cancel is confirmed only once the fills were read


def _setup(conn, mode="paper", bankroll=10_000):
    make_game(conn)
    market = make_market(conn, "2026_05_KC_LV", "home")
    assignment = make_assignment(conn, "2026_05_KC_LV", make_model(conn), mode=mode, bankroll_cents=bankroll)
    return market, assignment


def test_outbox_moves_approved_to_open_through_submitting(conn):
    market, assignment = _setup(conn)
    o = make_order(conn, assignment, market, 0.50, 10)
    counts = executor.run_once(conn, PaperGateway(), NOW)
    assert counts["submitted"] == 1
    row = order(conn, o["id"])
    assert row["status"] == "open" and row["exchange_order_id"] == "paper:" + o["client_request_id"]
    assert row["submitted_at"] == NOW and row["gtd_at"] == NOW + timedelta(seconds=900)
    assert events(conn, o["id"]) == [(None, "approved"), ("approved", "submitting"), ("submitting", "open")]
    assert executor.run_once(conn, PaperGateway(), NOW)["submitted"] == 0
    bank = bankroll(conn, assignment)
    assert bank["reserved_cents"] == o["cost_cents"] and ledger.replay_problems(conn) == []


def test_outbox_timeout_leaves_submitting_and_reconciles_by_client_id(conn):
    market, assignment = _setup(conn, mode="live")
    o = make_order(conn, assignment, market, 0.50, 10)

    def boom(_):
        raise TimeoutError("gateway timed out")

    gateway = FlakyGateway(place=boom)
    ex = executor.Executor(gateway)
    ex.tick(conn, NOW)
    assert order(conn, o["id"])["status"] == "submitting" and gateway.placed == [o["client_request_id"]]
    ex.tick(conn, NOW + timedelta(seconds=1))
    assert gateway.placed == [o["client_request_id"]], "never resubmitted blind"
    ex.tick(conn, NOW + timedelta(seconds=10))
    assert order(conn, o["id"])["status"] == "submitting", "not on the exchange yet: stays submitting"
    gateway.remote = [{"client_order_id": o["client_request_id"], "id": "ex-77"}]
    ex.tick(conn, NOW + timedelta(seconds=11))
    row = order(conn, o["id"])
    assert row["status"] == "open" and row["exchange_order_id"] == "ex-77"
    assert ("submitting", "open") in events(conn, o["id"])


def test_executor_never_submits_while_killed_and_cancels_approved_with_release(conn):
    market, assignment = _setup(conn)
    o = make_order(conn, assignment, market, 0.50, 10)
    # The flag alone (the kill transaction's own order sweep is tested in test_kill_switch).
    conn.execute("UPDATE settings SET value = 'true' WHERE key = 'kill_switch'")
    assert kill.is_killed(conn)
    gateway = FlakyGateway()
    counts = executor.Executor(gateway).tick(conn, NOW)
    assert counts["submitted"] == 0 and counts["cancelled_under_kill"] == 1 and gateway.placed == []
    row = order(conn, o["id"])
    assert row["status"] == "cancelled"
    bank = bankroll(conn, assignment)
    assert bank["reserved_cents"] == 0 and bank["available_cents"] == 10_000 and ledger.replay_problems(conn) == []
    assert events(conn, o["id"])[-1] == ("approved", "cancelled")


def test_executor_racing_kill_cannot_open(conn):
    """The kill lands between `submitting` and `open`: the order ends cancelled and
    the exchange is told to cancel by client id instead of the row opening."""
    market, assignment = _setup(conn)
    o = make_order(conn, assignment, market, 0.50, 10)

    def place_then_kill(row):
        conn.execute(
            "UPDATE orders SET status = 'cancelled', updated_at = now() WHERE id = %s AND status = 'submitting'", (row["id"],)
        )
        orders.release_unfilled(conn, orders.get_order(conn, row["id"]), note="kill")
        orders.add_order_event(conn, row["id"], "submitting", "cancelled", "kill")
        return "ex-late"

    gateway = FlakyGateway(place=place_then_kill)
    executor.Executor(gateway).tick(conn, NOW)
    row = order(conn, o["id"])
    assert row["status"] == "cancelled" and row["exchange_order_id"] == "ex-late"
    assert ("submitting", "open") not in events(conn, o["id"])
    assert bankroll(conn, assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []


def test_live_cancel_requested_retries_one_two_four_eight_until_confirmed(conn):
    market, assignment = _setup(conn, mode="live")
    o = make_order(conn, assignment, market, 0.50, 10, status="cancel_requested", submitted_at=NOW)
    gateway = FlakyGateway(cancel_results=[RuntimeError("down"), False, RuntimeError("down"), RuntimeError("down"), True])
    ex = executor.Executor(gateway)
    ex.tick(conn, NOW)
    assert gateway.cancels == 1 and order(conn, o["id"])["status"] == "cancel_requested"
    ex.tick(conn, NOW + timedelta(seconds=0.5))
    assert gateway.cancels == 1, "waits 1 s before the second attempt"
    ex.tick(conn, NOW + timedelta(seconds=1))
    assert gateway.cancels == 2
    ex.tick(conn, NOW + timedelta(seconds=2))
    assert gateway.cancels == 2, "waits 2 s"
    ex.tick(conn, NOW + timedelta(seconds=3))
    assert gateway.cancels == 3
    ex.tick(conn, NOW + timedelta(seconds=6))
    assert gateway.cancels == 3, "waits 4 s"
    ex.tick(conn, NOW + timedelta(seconds=7))
    assert gateway.cancels == 4
    ex.tick(conn, NOW + timedelta(seconds=14))
    assert gateway.cancels == 4, "waits 8 s"
    ex.tick(conn, NOW + timedelta(seconds=15))
    assert gateway.cancels == 5 and order(conn, o["id"])["status"] == "cancelled"
    assert bankroll(conn, assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []
    assert events(conn, o["id"])[-1] == ("cancel_requested", "cancelled")


def test_gtd_expiry_releases_the_unfilled_part(conn):
    market, assignment = _setup(conn)
    o = make_order(conn, assignment, market, 0.50, 10, status="open", submitted_at=NOW - timedelta(minutes=20), gtd_at=NOW - timedelta(minutes=5))
    orders.record_fill(conn, o["id"], 0.50, 4, 5, "paper", "test")
    still = make_order(conn, assignment, market, 0.50, 10, status="open", submitted_at=NOW, gtd_at=NOW + timedelta(minutes=15))
    counts = executor.run_once(conn, PaperGateway(), NOW)
    assert counts["expired"] == 1
    assert order(conn, o["id"])["status"] == "expired" and order(conn, still["id"])["status"] == "open"
    bank = bankroll(conn, assignment)
    assert bank["reserved_cents"] == still["cost_cents"], "only the live reservation remains"
    assert bank["open_cost_cents"] == 200 and ledger.replay_problems(conn) == []
    assert events(conn, o["id"])[-1] == ("partial", "expired")


def test_live_without_a_gateway_is_rejected_by_exchange_with_release(conn):
    market, assignment = _setup(conn, mode="live")
    o = make_order(conn, assignment, market, 0.50, 10)
    executor.Executor(OrderGateway()).tick(conn, NOW)
    row = order(conn, o["id"])
    assert row["status"] == "rejected_by_exchange" and "not configured" in row["reject_reason"]
    assert bankroll(conn, assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []
    with pytest.raises(NotConfigured):
        OrderGateway().balance()


# ------------------------------------------------------------------ rate limiter

def test_rate_limiter_buckets_priority_and_429():
    rl = ratelimit.RateLimiter({"orders_per_s": 2, "cancels_per_s": 10, "market_data_per_s": 4, "account_per_s": 1}, now=0.0)
    assert rl.take("orders", now=0.0) and rl.take("orders", now=0.0) and not rl.take("orders", now=0.0)
    assert rl.wait_seconds("orders", now=0.0) == pytest.approx(0.5)
    assert not rl.take("orders", now=0.4) and rl.take("orders", now=0.5)
    assert sum(rl.take("market_data", now=1.0) for _ in range(10)) == 4
    assert rl.take("unknown", now=1.0), "unknown categories are never throttled"
    rl.on_429("market_data", now=2.0)
    assert rl.effective_rate("market_data", now=2.0) == 2.0 and rl.effective_rate("market_data", now=62.0) == 4.0
    assert sum(rl.take("market_data", now=2.0) for _ in range(10)) == 4, "what had refilled is still there"
    assert sum(rl.take("market_data", now=3.0) for _ in range(10)) == 2, "halved refill for 60 s"
    assert sum(rl.take("market_data", now=63.0) for _ in range(10)) == 4
    assert sum(rl.take("market_data", now=64.0) for _ in range(10)) == 4, "full rate again after 60 s"
    assert ratelimit.RateLimiter.ordered(["market_data", "fills", "orders", "cancels", "x"]) == ["cancels", "orders", "fills", "market_data", "x"]
    rl.update_limits({"orders_per_s": 100}, now=10.0)
    assert sum(rl.take("orders", now=20.0) for _ in range(150)) == 100
    assert ratelimit.RateLimiter({"orders_per_s": "bad"}).limits["orders_per_s"] == 5.0


# -------------------------------------------------------------- heartbeat, loop

def test_heartbeat_and_last_error(conn):
    assert state.read_state(conn, NOW)["alive"] is False
    state.heartbeat(conn, "sim", None, NOW)
    s = state.read_state(conn, NOW + timedelta(seconds=3))
    assert s["alive"] and s["heartbeat_age_s"] == 3 and s["market_source"] == "sim" and s["last_error"] is None
    state.heartbeat(conn, "polymarket_us", "markets request answered 503", NOW + timedelta(seconds=5))
    s = state.read_state(conn, NOW + timedelta(seconds=25))
    assert not s["alive"] and s["heartbeat_age_s"] == 20 and s["last_error"] == "markets request answered 503"
    state.heartbeat(conn, "polymarket_us", None, NOW + timedelta(seconds=30))
    assert state.read_state(conn)["last_error"] is None


def test_run_once_runs_every_task_and_survives_a_failing_one(pool, conn):
    make_game(conn)
    assignment = make_assignment(conn, "2026_05_KC_LV", make_model(conn))
    results = run_once(pool, NOW)
    assert set(results) >= {"heartbeat", "discover", "snapshots", "executor", "fills", "scores", "settle", "retention"}
    assert results["discover"]["confirmed"] == 2 and results["snapshots"]["stored"] == 2 and results["last_error"] is None
    assert state.read_state(conn, NOW)["heartbeat_age_s"] == 0
    market = conn.execute("SELECT * FROM markets WHERE side = 'home'").fetchone()
    o = make_order(conn, assignment, market, 0.99, 5)
    loop = ExchangeLoop(pool, clock=lambda: NOW + timedelta(seconds=5))

    def broken(connection, now):
        raise RuntimeError("scores feed exploded")

    loop.task_scores = broken  # type: ignore[method-assign]
    results = run_once(pool, NOW + timedelta(seconds=5), loop)
    assert results["scores"] is None and results["last_error"] == "scores: scores feed exploded"
    assert order(conn, o["id"])["status"] in ("open", "partial", "filled"), "the executor still ran"
    assert loop.due("executor", NOW + timedelta(seconds=5.3)) and not loop.due("heartbeat", NOW + timedelta(seconds=9))
    results = loop.run_due(NOW + timedelta(seconds=6))
    assert "heartbeat" not in results and "executor" in results
    results = loop.run_due(NOW + timedelta(seconds=11))
    assert "heartbeat" in results and "scores" not in results
    assert state.read_state(conn)["last_error"] == "scores: scores feed exploded", "the error sticks until scores succeed"
