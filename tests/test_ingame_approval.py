"""In-game approval (host/trading/ingame.py, docs/INGAME.md, step 6C contract section
10): every reason code at its boundary and in its order, the kickoff exemption only
for in-game requests, paper-only, the lag suspension for buys but not sells, the
in-game max bet, the stored flag and state at entry, the API fields, and the executor
and paper fill exemptions (GTD, no kickoff cancel, fills after kickoff, kill)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg

from host import kill
from host.exchange import paper
from host.exchange.executor import Executor
from host.trading import ingame, ledger, orders
from host.trading.limits import CHECKS, REASONS, approve_order
from tests.conftest import (
    TradeSetup, bankroll_of, insert_model, insert_snapshot, order_events, order_row, set_setting, trade_setup,
    worker_row,
)

STATE = {"status": "in", "period": 3, "clock_seconds": 600, "home_score": 17, "away_score": 14, "possession": "home",
         "down": 2, "distance": 7, "yardline_100": 45, "home_timeouts": 3, "away_timeouts": 2}


def ingame_setup(conn: psycopg.Connection, mode: str = "paper", enabled: bool = True, **kw: Any) -> TradeSetup:
    """A trade setup whose game kicked off an hour ago, an ingame_wp model on the
    assignment and trade_ingame on (no game state yet)."""
    s = trade_setup(conn, mode=mode, **kw)
    conn.execute("UPDATE games SET kickoff_at = now() - interval '1 hour' WHERE game_id = %s", (s.game["game_id"],))
    model = insert_model(conn, family="ingame_wp", status="paper_ok", params={"l2": 1.0, "time_scale": 1.0, "fp_scale": 1.0},
                         artifact={"coef": [0.0]})
    conn.execute("UPDATE assignments SET ingame_model_id = %s, trade_ingame = %s WHERE id = %s",
                 (model["id"], enabled, s.assignment["id"]))
    return s


def put_state(conn: psycopg.Connection, game_id: str, age_s: float = 1.0, **fields: Any) -> None:
    """One game_state situation row `age_s` seconds old (database clock)."""
    row = {**STATE, **fields}
    conn.execute(
        """
        INSERT INTO game_state (game_id, ts, source, status, period, clock_seconds, home_score, away_score, possession,
                                down, distance, yardline_100, home_timeouts, away_timeouts)
        VALUES (%s, now() - make_interval(secs => %s), 'espn_summary', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (game_id, age_s, row["status"], row["period"], row["clock_seconds"], row["home_score"], row["away_score"],
         row["possession"], row["down"], row["distance"], row["yardline_100"], row["home_timeouts"], row["away_timeouts"]),
    )


def put_change(conn: psycopg.Connection, game_id: str, ago_s: float, key: str = "score:17-14", lag: float | None = None) -> None:
    """A feed_lag event first seen `ago_s` seconds ago (measured when `lag` is given)."""
    conn.execute(
        """
        INSERT INTO feed_lag (game_id, event_kind, event_key, source, feed_seen_at, market_moved_at, lag_s)
        VALUES (%s, 'score', %s, 'espn_summary', now() - make_interval(secs => %s),
                CASE WHEN %s::real IS NULL THEN NULL ELSE now() - make_interval(secs => %s + %s) END, %s)
        """,
        (game_id, key, ago_s, lag, ago_s, lag or 0, lag),
    )


def ask(conn: psycopg.Connection, s: TradeSetup, size: int = 2, ingame_flag: bool = True, **kw: Any) -> dict[str, Any]:
    """An order request through approve_order (the API's dispatch path)."""
    return approve_order(conn, worker_row(conn, s.worker.id), s.body(size=size, ingame=ingame_flag, **kw))


def hold(conn: psycopg.Connection, s: TradeSetup, size: int = 4) -> None:
    """A filled in-game buy of `size` contracts (paper)."""
    decision = ask(conn, s, size=size)
    assert decision["status"] == "approved", decision
    orders.set_status(conn, decision["order_id"], "open", "test", None, expected=("approved",))
    row = order_row(conn, decision["order_id"])
    orders.record_fill(conn, row["id"], 0.52, size, int(row["fee_cents_est"]), "paper", "test", snapshot_id=row["snapshot_id"])


def detail(conn: psycopg.Connection, order_id: Any) -> dict[str, Any]:
    return conn.execute("SELECT detail FROM order_events WHERE order_id = %s ORDER BY id LIMIT 1", (order_id,)).fetchone()["detail"]


# ------------------------------------------------------------------ approval


def test_ingame_buy_after_kickoff_is_approved_flagged_and_records_the_state(conn):
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    decision = ask(conn, s, gtd_seconds=45)
    assert decision["status"] == "approved", decision
    row = order_row(conn, decision["order_id"])
    assert row["ingame"] is True and row["side"] == "buy" and row["cost_cents"] > 0
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == row["cost_cents"], "a buy reserves as before"
    event = detail(conn, row["id"])
    assert event["ingame"] is True and event["gtd_seconds"] == 60 and event["gtd_seconds_requested"] == 45
    assert event["state_at_entry"] == {"period": 3, "clock_seconds": 600, "home_score": 17, "away_score": 14,
                                       "possession": "home"}
    assert ledger.replay_problems(conn) == []


def test_a_pregame_request_after_kickoff_is_still_rejected_as_kickoff(conn):
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    decision = ask(conn, s, ingame_flag=False)
    assert decision["reason"] == "kickoff"
    assert order_row(conn, decision["order_id"])["ingame"] is False
    assert ask(conn, s, ingame_flag=False, order_side="sell")["reason"] == "kickoff", "pre-game sells too"


def test_kill_lease_assignment_and_market_come_first(conn):
    s = ingame_setup(conn, enabled=False)
    set_setting(conn, "kill_switch", True)
    assert ask(conn, s)["reason"] == "killed", "kill before every in-game check"
    assert ask(conn, s, order_side="sell")["reason"] == "killed"
    set_setting(conn, "kill_switch", False)
    assert ask(conn, s, lease_token="00000000-0000-0000-0000-000000000000")["reason"] == "lease"
    conn.execute("UPDATE assignments SET status = 'halted' WHERE id = %s", (s.assignment["id"],))
    assert ask(conn, s)["reason"] == "assignment"
    conn.execute("UPDATE assignments SET status = 'active' WHERE id = %s", (s.assignment["id"],))
    conn.execute("UPDATE markets SET mapping_confirmed = false WHERE id = %s", (s.market["id"],))
    assert ask(conn, s)["reason"] == "market"
    conn.execute("UPDATE markets SET mapping_confirmed = true WHERE id = %s", (s.market["id"],))
    assert ask(conn, s)["reason"] == "ingame_disabled"


def test_ingame_disabled_without_the_flag_or_the_model(conn):
    s = ingame_setup(conn, enabled=False)
    put_state(conn, s.game["game_id"])
    assert ask(conn, s)["reason"] == "ingame_disabled"
    conn.execute("UPDATE assignments SET trade_ingame = true, ingame_model_id = NULL WHERE id = %s", (s.assignment["id"],))
    assert ask(conn, s)["reason"] == "ingame_disabled"
    assert ask(conn, s, order_side="sell")["reason"] == "ingame_disabled"


def test_live_ingame_is_rejected_as_paper_only(conn):
    s = ingame_setup(conn, mode="live", model_status="live_eligible")
    put_state(conn, s.game["game_id"])
    assert ask(conn, s)["reason"] == "ingame_paper_only"
    assert ask(conn, s, order_side="sell")["reason"] == "ingame_paper_only"
    conn.execute("DELETE FROM game_state")
    assert ask(conn, s)["reason"] == "ingame_paper_only", "paper-only comes before the state checks"


def test_stale_exactly_at_the_max_age_passes_and_beyond_it_rejects(conn):
    s = ingame_setup(conn)
    assert ask(conn, s)["reason"] == "ingame_stale", "no game state"
    with conn.transaction():  # one transaction: now() is the same for the row and the approval
        put_state(conn, s.game["game_id"], age_s=30.0)
        assert ask(conn, s)["status"] == "approved", "age exactly ingame_max_state_age_s"
    with conn.transaction():
        put_state(conn, s.game["game_id"], age_s=30.001)
        assert ask(conn, s)["reason"] == "ingame_stale"
    set_setting(conn, "ingame_max_state_age_s", 10)
    with conn.transaction():
        put_state(conn, s.game["game_id"], age_s=10.0)
        assert ask(conn, s)["status"] == "approved"


def test_stale_when_the_state_does_not_show_a_game_in_progress(conn):
    s = ingame_setup(conn)
    game = s.game["game_id"]
    for fields in ({"status": "half"}, {"status": "final"}, {"status": "pre"}, {"clock_seconds": None}, {"period": None}):
        conn.execute("DELETE FROM game_state")
        put_state(conn, game, **fields)
        assert ask(conn, s)["reason"] == "ingame_stale", fields
        assert ask(conn, s, order_side="sell")["reason"] == "ingame_stale", fields
    conn.execute("DELETE FROM game_state")
    put_state(conn, game)
    conn.execute("UPDATE games SET status = 'final' WHERE game_id = %s", (game,))
    assert ask(conn, s)["reason"] == "ingame_stale", "a final game"


def test_quiet_exactly_at_the_quiet_seconds_passes(conn):
    s = ingame_setup(conn)
    game = s.game["game_id"]
    with conn.transaction():
        put_state(conn, game)
        put_change(conn, game, 19.999, key="score:17-14")
        assert ask(conn, s)["reason"] == "ingame_quiet"
        assert ask(conn, s, order_side="sell")["reason"] == "ingame_quiet", "sells wait for the quiet period too"
    with conn.transaction():
        conn.execute("DELETE FROM feed_lag")
        put_change(conn, game, 20.0, key="possession:3:home:17-14:0")
        put_state(conn, game)
        assert ask(conn, s)["status"] == "approved", "exactly ingame_quiet_seconds after the change"


def test_cutoff_exactly_at_the_cutoff_rejects(conn):
    s = ingame_setup(conn, bankroll_cents=100_000)
    set_setting(conn, "ingame_max_bet_cents", 100_000)
    set_setting(conn, "max_bet_cents", 100_000)
    game = s.game["game_id"]
    cases = [
        ({"period": 4, "clock_seconds": 120}, "ingame_cutoff"),
        ({"period": 4, "clock_seconds": 121}, None),
        ({"period": 3, "clock_seconds": 0}, None),
        ({"period": 5, "clock_seconds": 120}, "ingame_cutoff"),
        ({"period": 5, "clock_seconds": 121}, None),
        ({"period": 6, "clock_seconds": 30}, "ingame_cutoff"),
    ]
    for fields, reason in cases:
        conn.execute("DELETE FROM game_state")
        put_state(conn, game, **fields)
        decision = ask(conn, s, size=1)
        assert decision["reason"] == reason, (fields, decision)
    assert ingame.seconds_remaining({"period": 1, "clock_seconds": 900}) == 3600
    assert ingame.seconds_remaining({"period": 4, "clock_seconds": 0}) == 0
    assert ingame.seconds_remaining({"period": 5, "clock_seconds": 600}) == 600
    assert ingame.seconds_remaining({"period": None, "clock_seconds": 600}) is None


def test_lag_suspension_rejects_buys_but_not_sells(conn):
    s = ingame_setup(conn)
    game = s.game["game_id"]
    put_state(conn, game)
    hold(conn, s, 4)
    for i in range(4):
        put_change(conn, game, 600 + i, key=f"old:{i}", lag=30.0)
    assert ask(conn, s)["status"] == "approved", "four measured events: not enough data, not suspended"
    put_change(conn, game, 700, key="old:4", lag=30.0)
    assert ask(conn, s)["reason"] == "ingame_lag_suspended", "five events, median 30 s > 20 s"
    decision = ask(conn, s, size=2, order_side="sell", price=0.50)
    assert decision["status"] == "approved", decision
    assert order_row(conn, decision["order_id"])["ingame"] is True
    conn.execute("DELETE FROM game_state")
    put_state(conn, game, period=4, clock_seconds=60)
    assert ask(conn, s)["reason"] == "ingame_cutoff", "the cutoff comes before the lag"


def test_max_bet_also_against_ingame_max_bet_cents(conn):
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    set_setting(conn, "ingame_max_bet_cents", 100)
    assert ask(conn, s, size=2)["reason"] == "max_bet", "2 x 0.52 plus fee > 100 cents"
    assert ask(conn, s, size=1)["status"] == "approved"
    assert ask(conn, s, size=1)["reason"] == "max_bet", "open in-game cost counts toward the cap"
    set_setting(conn, "ingame_max_bet_cents", 100_000)
    set_setting(conn, "max_bet_cents", 150)
    assert ask(conn, s, size=2)["reason"] == "max_bet", "the global max bet still applies"


def test_the_usual_checks_follow_the_ingame_ones(conn):
    s = ingame_setup(conn, bankroll_cents=50)
    put_state(conn, s.game["game_id"])
    assert ask(conn, s, size=1)["reason"] == "bankroll"
    stale = insert_snapshot(conn, s.market["id"], age_s=120)
    assert ask(conn, s, size=1, snapshot_id=stale["id"])["reason"] == "stale_book"
    assert ask(conn, s, size=1, order_side="sell", price=0.50)["reason"] == "no_position"


def test_reason_order_and_codes():
    names = [n for n, _ in ingame.BUY_CHECKS]
    assert names[:10] == ["killed", "lease", "assignment", "market", "ingame_disabled", "ingame_paper_only",
                          "ingame_stale", "ingame_quiet", "ingame_cutoff", "ingame_lag_suspended"]
    assert "kickoff" not in names and names[10:] == [n for n, _ in CHECKS if n not in ("killed", "lease", "assignment", "market", "kickoff")]
    sells = [n for n, _ in ingame.SELL_CHECKS]
    assert "ingame_lag_suspended" not in sells and "kickoff" not in sells and "ingame_cutoff" in sells
    assert set(ingame.REASONS) <= set(REASONS)


def test_ingame_requests_are_idempotent(conn):
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    body = s.body(size=2, ingame=True)
    worker = worker_row(conn, s.worker.id)
    first = approve_order(conn, worker, body)
    assert approve_order(conn, worker, body) == dict(first, duplicate=True)


# ------------------------------------------------------------------ API


def test_api_accepts_ingame_and_gtd_seconds(client, conn):
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    r = client.post("/api/v1/orders/request", json=s.body(size=2, ingame=True, gtd_seconds=30), headers=s.worker.headers)
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    assert order_row(conn, r.json()["order_id"])["ingame"] is True
    r = client.post("/api/v1/orders/request", json=s.body(size=2), headers=s.worker.headers)
    assert r.json()["reason"] == "kickoff", "ingame defaults to false"
    r = client.post("/api/v1/orders/request", json=s.body(size=2, ingame=True, gtd_seconds=0), headers=s.worker.headers)
    assert r.status_code == 400, "a GTD below one second is a bad request"


# ------------------------------------------------------------------ executor and paper fills


def test_executor_gives_ingame_orders_their_gtd_and_never_cancels_them_at_kickoff(conn):
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    o = order_row(conn, ask(conn, s, size=2)["order_id"])
    pre = trade_setup(conn, game_id="2026_05_SF_SEA")
    pregame = order_row(conn, ask(conn, pre, size=2, ingame_flag=False)["order_id"])
    conn.execute("UPDATE games SET kickoff_at = now() - interval '1 minute' WHERE game_id = %s", (pre.game["game_id"],))
    counts = Executor().tick(conn)
    assert counts["kickoff"] == 1 and order_row(conn, pregame["id"])["status"] == "cancelled", "a pre-game order still goes"
    row = order_row(conn, o["id"])
    assert row["status"] == "open", "the in-game order is not cancelled at kickoff"
    assert row["gtd_at"] == row["submitted_at"] + timedelta(seconds=60), "GTD is ingame_gtd_seconds, not kickoff"
    assert Executor().tick(conn, row["gtd_at"] - timedelta(seconds=1))["expired"] == 0
    assert Executor().tick(conn, row["gtd_at"])["expired"] == 1
    assert order_row(conn, o["id"])["status"] == "expired"
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == 0, "the expiry released the reservation"
    assert ledger.replay_problems(conn) == []


def test_paper_ingame_orders_fill_on_snapshots_after_kickoff(conn):
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    o = order_row(conn, ask(conn, s, size=2)["order_id"])
    Executor().tick(conn)
    assert paper.kickoff_bound(conn, order_row(conn, o["id"])) is None
    insert_snapshot(conn, s.market["id"], bid=0.48, ask=0.50)
    assert paper.process(conn) >= 1
    row = order_row(conn, o["id"])
    assert row["filled_size"] == 2 and row["status"] == "filled"
    assert ledger.replay_problems(conn) == []


def test_kill_cancels_open_ingame_orders(conn):
    s = ingame_setup(conn)
    put_state(conn, s.game["game_id"])
    o = order_row(conn, ask(conn, s, size=2)["order_id"])
    Executor().tick(conn)
    assert order_row(conn, o["id"])["status"] == "open"
    kill.set_kill(conn, "owner")
    assert order_row(conn, o["id"])["status"] == "cancelled"
    assert order_events(conn, o["id"])[-1] == "cancelled"
    assert bankroll_of(conn, s.assignment)["reserved_cents"] == 0
    assert ask(conn, s)["reason"] == "killed"
