"""Step 6 Part C in-game phase of the end-to-end test (tests/test_e2e.py): the real agent,
host and exchange loop (sim market source) trading a game in progress, paper only.

A tiny in-game search (model_search, family ingame_wp) runs on the real worker against
the host's pbp feed (the nflverse fixture slice ingested through the CLI as the 2023
validation era, copied into 2019-2022 as the train era) and posts one ingame_wp lineage.
A shifted KC @ LV game that kicked off 20 minutes ago carries the ESPN event id of the
fixture tests/fixtures/espn_summary_in.json; a local fake ESPN (the settings point
espn_summary_url and scores_url at it) serves that payload with the situation the test
sets, so the exchange's gamestate task polls it for real and stores game_state and
feed_lag rows. The assignment has the in-game model and trade_ingame on.

Buying is gated by ingame_max_bet_cents (0 = the worker sizes nothing). In order: a
live in-game request is refused (409 at creation, ingame_paper_only at approval with the
assignment forced to live); the worker proposes an in-game buy that is approved with
its state_at_entry and paper-filled on a snapshot after kickoff; a feed outage makes the
state stale, a field goal opens the quiet period and the final two minutes the cutoff:
each time the worker abstains and a request on its lease gets the matching rejection;
a second in-game buy rests on a frozen book and KILL cancels it; ESPN goes final and
simulate-final settles: bets rows with ingame true, state_at_entry and no CLV, scored
for the in-game lineage (ingame_n_bets, ingame_pnl_cents).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

from fleet.models.ingame_wp import IngameWP
from fleet.sim.odds import devig
from fleet.worker import config as worker_config
from tests.e2e_ingame_feed import EVENT_ID, FIXTURES, FakeEspn, Situation
from tests.e2e_trading import ExchangeThread, SimClock, orders_of, rows, run_cli, shifted_game_csv

SOURCE_GAME = "2025_18_KC_LV"
GAME_ID = "2026_08_KC_LV"
BANKROLL_CENTS = 20_000
BET_CENTS = 300
SNAPSHOT_S = 6
SEARCH = {"family": "ingame_wp", "n": 2, "seed": 1, "top_k": 1, "train_seasons": [2019, 2022],
          "validation_seasons": [2023, None], "train_fraction": 0.5}
QUIET_S = 8
SETTINGS: dict[str, Any] = {
    "trade_tick_s": 1, "min_edge": 1.0, "snapshot_active_s": SNAPSHOT_S, "liquidity_floor_cents": 10_000,
    "fee_model": {"taker_rate": 0.0, "half_spread": 0.01}, "kelly_fraction": 0.5, "participation": 0.5,
    "ingame_tick_s": 1, "ingame_max_state_age_s": 30, "ingame_quiet_seconds": QUIET_S, "ingame_cutoff_seconds": 120,
    "ingame_dead_zone": 0.03, "ingame_min_edge": 0.05, "ingame_max_bet_cents": 0, "ingame_gtd_seconds": 600,
    "gamestate_poll_s": 3, "gamestate_max_rps": 10, "ingame_lag_min_events": 5,
}


def _db(host: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(host.database_url, autocommit=True, row_factory=dict_row) as conn:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def ingame_search(host: Any, worker_id: str, tmp_path: Path, wait_for: Callable[..., Any],
                  settled: Callable[..., Any]) -> dict[str, Any]:
    """pbp rows through the CLI, the tiny search on the worker; the new ingame_wp model row."""
    out = run_cli(["ingest-pbp-rows", "--season", "2023", "--file", str(FIXTURES / "pbp_rows_sample.csv.gz")])
    assert out.startswith("season 2023: ") and " in 2 games " in out, out
    _db(host, "INSERT INTO pbp_rows SELECT r.game_id || '_copy' || s, r.play_id, s, r.home_win, r.score_diff,"
              " r.seconds_remaining, r.half, r.down, r.ydstogo, r.yardline_100, r.posteam_is_home, r.home_timeouts,"
              " r.away_timeouts, r.pregame_p_home, r.vegas_wp"
              " FROM pbp_rows r CROSS JOIN generate_series(2019, 2022) s WHERE r.season = 2023")
    job = host.post("/api/jobs", {"kind": "model_search", "params": SEARCH, "target": worker_id}, expect=201)
    done = wait_for(lambda: (j := host.job(job["id"]))["status"] in ("succeeded", "failed") and j, "in-game search",
                    timeout=60.0)
    assert done["status"] == "succeeded", done.get("error")
    found = _db(host, "SELECT * FROM models WHERE family = 'ingame_wp'")
    assert len(found) == 1 and found[0]["lineage_id"] == found[0]["id"], found
    model = found[0]
    api = host.get(f"/api/models/{model['id']}")
    assert api["status"] == "candidate", "under 10000 validation plays an ingame_wp lineage stays a candidate"
    assert model["validation_metrics"]["era"] == "validation" and model["backtest_metrics"]["era"] == "search"
    assert model["validation_metrics"]["n_plays"] > 0 and set(model["artifact"]["train_seasons"]) <= {2019, 2020, 2021, 2022}
    wait_for(settled(host, worker_id, "idle"), "worker idle after the in-game search")
    return model


def kicked_off_game(host: Any, tmp_path: Path) -> dict[str, Any]:
    """The KC @ LV fixture row as GAME_ID, kicked off 20 minutes ago, with the ESPN event id."""
    csv_path, _ = shifted_game_csv(tmp_path, SOURCE_GAME, GAME_ID, days_ahead=0)
    assert "1 inserted" in run_cli(["ingest-games", "--file", str(csv_path)])
    return _db(host, "UPDATE games SET kickoff_at = now() - interval '20 minutes',"
                     " gameday = ((now() - interval '20 minutes') AT TIME ZONE 'America/New_York')::date,"
                     " raw = raw || jsonb_build_object('espn', %s::text) WHERE game_id = %s RETURNING *",
               (EVENT_ID, GAME_ID))[0]


def latest_row(host: Any, table: str, key: str, value: Any) -> dict[str, Any] | None:
    found = _db(host, f"SELECT * FROM {table} WHERE {key} = %s ORDER BY ts DESC, id DESC LIMIT 1", (value,))
    return found[0] if found else None


def phase_ingame(host: Any, state_dir: str, worker_id: str, agent: Any, models: dict[str, Any], tmp_path: Path,
                 wait_for: Callable[..., Any], settled: Callable[..., Any]) -> None:
    started = time.monotonic()
    model = ingame_search(host, worker_id, tmp_path, wait_for, settled)
    game = kicked_off_game(host, tmp_path)
    espn = FakeEspn().start()
    urls = {"espn_summary_url": espn.url + "/summary?event={event_id}", "scores_url": espn.url + "/scoreboard"}
    saved = {k: host.get("/api/settings")[k] for k in [*SETTINGS, *urls]}
    host.post("/api/settings", {**SETTINGS, **urls})
    exchange = ExchangeThread(host.database_url, SimClock()).start()
    try:
        _run(host, state_dir, worker_id, agent, models["child"], model, game, espn, exchange, wait_for, settled)
    finally:
        exchange.close()
        espn.close()
        host.post("/api/settings", saved)
    assert time.monotonic() - started < 100.0, "the in-game phase stays inside the e2e budget"


class Trader:
    """The worker's view and a request on its own lease (the host must refuse it)."""

    def __init__(self, host: Any, state_dir: str, agent: Any, aid: str, job_id: str) -> None:
        self.host, self.agent, self.aid, self.job_id = host, agent, aid, job_id
        self.token = worker_config.load_conf(state_dir)["worker_token"]

    def skip(self) -> Any:
        """Why the worker's last in-game pass for the assignment proposed nothing (None: it could)."""
        return self.agent.agent.trade.ingame.last.get(self.aid, {"skip": "not run"})["skip"]

    def request(self, market_id: str, tag: str) -> dict[str, Any]:
        snap = latest_row(self.host, "price_snapshots", "market_id", market_id)
        body = {"client_request_id": f"e2e-ingame-{tag}-{uuid.uuid4().hex[:8]}", "job_id": self.job_id,
                "lease_token": self.host.job(self.job_id)["lease_token"], "assignment_id": self.aid,
                "market_id": market_id, "snapshot_id": snap["id"], "price": float(snap["ask"]), "size": 1,
                "rationale": f"e2e {tag}", "ingame": True, "gtd_seconds": 60}
        resp = self.host.client.post("/api/v1/orders/request", json=body, headers={"Authorization": f"Bearer {self.token}"})
        assert resp.status_code == 200, resp.text
        return resp.json()

    def refused(self, market_id: str, reason: str, worker_skip: str | None, wait_for: Callable[..., Any],
                worker_first: bool = False) -> None:
        """The worker abstains (`worker_skip`, first when the request needs it) and a
        request on its lease is rejected with `reason`."""

        def abstains() -> None:
            if worker_skip is not None:
                wait_for(lambda: self.skip() == worker_skip, f"the worker abstaining ({worker_skip})", timeout=12.0)

        if worker_first:
            abstains()
        answer = self.request(market_id, reason)
        assert answer["status"] == "rejected" and answer["reason"] == reason, answer
        assert self.host.get(f"/api/orders/{answer['order_id']}")["reject_reason"] == reason
        if not worker_first:
            abstains()


def _state_shows(host: Any, sit: Situation) -> Callable[[], Any]:
    want = sit.state()

    def check() -> Any:
        row = latest_row(host, "game_state", "game_id", GAME_ID)
        return row if row is not None and {k: row[k] for k in want} == want else False

    return check


def _new_buy(host: Any, aid: str, seen: set[str]) -> Callable[[], Any]:
    def check() -> Any:
        found = [o for o in orders_of(host, aid) if o["id"] not in seen and o["status"] in ("open", "partial", "filled")]
        return found[0] if found else False

    return check


def _run(host: Any, state_dir: str, worker_id: str, agent: Any, pregame_model_id: str, model: dict[str, Any],
         game: dict[str, Any], espn: FakeEspn, exchange: ExchangeThread, wait_for: Callable[..., Any],
         settled: Callable[..., Any]) -> None:
    markets = wait_for(lambda: (ms := host.get(f"/api/markets?game_id={GAME_ID}")) and len(ms) == 2
                       and all(m["mapping_confirmed"] and m["snapshot_age_s"] is not None for m in ms) and ms,
                       "two sim markets for the game in progress")
    side_of = {m["id"]: m["side"] for m in markets}
    home_id = next(m for m, s in side_of.items() if s == "home")
    ingame_id = str(model["id"])
    body = {"game_id": GAME_ID, "model_id": pregame_model_id, "bankroll_cents": BANKROLL_CENTS,
            "ingame_model_id": ingame_id, "trade_ingame": True}
    live = host.client.post("/api/assignments", json=dict(body, mode="live"))
    assert live.status_code == 409 and "paper-only" in live.text, live.text
    created = host.post("/api/assignments", body, expect=201)
    aid, job_id = created["id"], created["job_id"]
    assert created["trade_ingame"] is True and str(created["ingame_model_id"]) == ingame_id
    host.set_role(worker_id, "trade")
    wait_for(settled(host, worker_id, "trade"), "worker in trade for the game in progress")
    wait_for(lambda: host.job(job_id)["status"] == "leased", "in-game trade job claimed")
    trader = Trader(host, state_dir, agent, aid, job_id)

    # The exchange polls the fake ESPN; the parsed situation is the state the worker reads.
    sit = Situation()
    stored = wait_for(_state_shows(host, sit), "the polled game state", timeout=15.0)
    assert stored["source"] == "espn_summary" and f"/summary?event={EVENT_ID}" in espn.paths
    wait_for(lambda: trader.skip() is None, "the worker's in-game pass on a usable state")
    assert "Q2 10:00" in host.client.get("/trading").text

    # Live in-game orders: refused at creation above, and at approval (assignment forced to live).
    _db(host, "UPDATE assignments SET mode = 'live' WHERE id = %s", (aid,))
    try:
        trader.refused(home_id, "ingame_paper_only", None, wait_for)
    finally:
        _db(host, "UPDATE assignments SET mode = 'paper' WHERE id = %s", (aid,))

    # An in-game buy: approved with the state at entry, paper-filled on a later snapshot.
    host.post("/api/settings", {"ingame_max_bet_cents": BET_CENTS})
    first = wait_for(_new_buy(host, aid, set()), "an in-game buy approved", timeout=15.0)
    host.post("/api/settings", {"ingame_max_bet_cents": 0})
    p_home = IngameWP.from_json(model["params"], model["artifact"]).predict(
        sit.state(), devig(game["home_moneyline"], game["away_moneyline"]))
    p_side = p_home if side_of[first["market_id"]] == "home" else 1.0 - p_home
    assert first["ingame"] is True and first["mode"] == "paper" and first["rationale"].startswith("in-game: my ")
    assert abs(float(first["my_p"]) - p_side) < 1e-4 and float(first["edge"]) >= SETTINGS["ingame_min_edge"]
    assert 0 < first["cost_cents"] <= BET_CENTS and first["client_request_id"].startswith("ingame-")
    approval = host.get(f"/api/orders/{first['id']}")["events"][0]["detail"]
    assert approval["state_at_entry"] == {"period": 2, "clock_seconds": 600, "home_score": 21, "away_score": 0,
                                          "possession": "home"} and approval["gtd_seconds"] == 600, approval
    wait_for(lambda: host.get(f"/api/orders/{first['id']}")["status"] == "filled", "the in-game buy filled",
             timeout=SNAPSHOT_S + 8.0)
    filled = _db(host, "SELECT o.gtd_at, o.created_at, s.ts FROM orders o JOIN fills f ON f.order_id = o.id"
                       " JOIN price_snapshots s ON s.id = f.snapshot_id WHERE o.id = %s", (first["id"],))
    assert filled and all(r["ts"] > game["kickoff_at"] and r["ts"] > r["created_at"] for r in filled)
    assert (filled[0]["gtd_at"] - filled[0]["created_at"]).total_seconds() > 500, "GTD ingame_gtd_seconds, no kickoff cap"
    assert "ledger ok" in run_cli(["ledger-check"])

    # A feed outage: the state goes stale.
    host.post("/api/settings", {"ingame_max_state_age_s": 5})
    espn.failing = True
    trader.refused(home_id, "ingame_stale", "stale", wait_for, worker_first=True)
    espn.failing = False
    host.post("/api/settings", {"ingame_max_state_age_s": 30})
    wait_for(lambda: trader.skip() is None, "a fresh state again", timeout=10.0)

    # A field goal in Q3: a score event in feed_lag opens the quiet period.
    sit = replace(sit, period=3, clock=420, away=3)
    espn.situation = sit
    wait_for(_state_shows(host, sit), "the field goal polled", timeout=10.0)
    events = _db(host, "SELECT * FROM feed_lag WHERE game_id = %s", (GAME_ID,))
    assert [(e["event_kind"], e["event_key"], e["source"]) for e in events] == [("score", "score:21-3", "espn_summary")]
    trader.refused(home_id, "ingame_quiet", "quiet", wait_for)

    # After the quiet period a second buy rests on a frozen book; KILL cancels it.
    wait_for(lambda: trader.skip() is None, "the quiet period over", timeout=QUIET_S + 5.0)
    second = _resting_buy(host, aid, {first["id"]}, wait_for)
    _kill_and_resume(host, aid, second)

    # The final two minutes: inside the cutoff.
    sit = replace(sit, period=4, clock=90)
    espn.situation = sit
    wait_for(_state_shows(host, sit), "the two-minute state polled", timeout=10.0)
    trader.refused(home_id, "ingame_cutoff", "cutoff", wait_for, worker_first=True)

    # ESPN goes final: polling stops; simulate-final settles the in-game bets.
    sit = replace(sit, clock=0, final=True)
    espn.situation = sit
    wait_for(_state_shows(host, sit), "the final state polled", timeout=10.0)
    asked = espn.paths.count(f"/summary?event={EVENT_ID}")
    wait_for(lambda: trader.skip() == "not_in_play", "the worker sees the final state")
    time.sleep(SETTINGS["gamestate_poll_s"] + 1.5)
    assert espn.paths.count(f"/summary?event={EVENT_ID}") == asked, "a final game is not polled again"
    summary = json.loads(run_cli(["simulate-final", GAME_ID, "--home", "21", "--away", "3"]))
    assert summary["winner"] == "home" and summary["assignments"] == 1, summary
    _check_settlement(host, aid, job_id, model, pregame_model_id, side_of, first, summary)
    wait_for(lambda: host.worker(worker_id)["current_jobs"] == [], "in-game trade job dropped by the worker")
    host.set_role(worker_id, "idle")
    wait_for(settled(host, worker_id, "idle"), "worker idle after the in-game phase")


def _resting_buy(host: Any, aid: str, seen: set[str], wait_for: Callable[..., Any]) -> dict[str, Any]:
    """Hold the snapshot poller, let the worker buy once: the order cites the frozen book and rests."""
    host.post("/api/settings", {"snapshot_active_s": 300})
    time.sleep(1.5)
    host.post("/api/settings", {"ingame_max_bet_cents": BET_CENTS})
    order = wait_for(_new_buy(host, aid, seen), "a second in-game buy", timeout=15.0)
    host.post("/api/settings", {"ingame_max_bet_cents": 0})
    frozen = latest_row(host, "price_snapshots", "market_id", order["market_id"])
    assert order["status"] == "open" and order["ingame"] is True and order["snapshot_id"] == frozen["id"], order
    time.sleep(1.5)  # the fills task runs every second: nothing to fill against
    current = host.get(f"/api/orders/{order['id']}")
    assert current["status"] == "open" and current["filled_size"] == 0, current
    return order


def _kill_and_resume(host: Any, aid: str, order: dict[str, Any]) -> None:
    host.form("/kill")
    assert host.get("/api/orders?status=active") == [], "no active order survives the kill"
    killed = host.get(f"/api/orders/{order['id']}")
    assert killed["status"] == "cancelled" and killed["filled_size"] == 0
    assert [e["to_status"] for e in killed["events"]] == ["approved", "submitting", "open", "cancelled"]
    audit = host.get("/api/audit?limit=5")
    assert audit[0]["action"] == "kill_cancel_all" and audit[0]["after"]["orders_cancelled"] == [order["id"]]
    assert host.get(f"/api/assignments/{aid}")["status"] == "halted"
    host.form("/kill/reset", {"confirm": "RESUME"})
    host.form("/assignments/activate-paper")
    assert host.get(f"/api/assignments/{aid}")["status"] == "active"
    host.post("/api/settings", {"snapshot_active_s": SNAPSHOT_S})
    assert "ledger ok" in run_cli(["ledger-check"])


def _check_settlement(host: Any, aid: str, job_id: str, model: dict[str, Any], pregame_id: str,
                      side_of: dict[str, str], first: dict[str, Any], summary: dict[str, Any]) -> None:
    ingame_id = str(model["id"])
    bets = rows(host, "SELECT b.*, o.market_id FROM bets b JOIN orders o ON o.id = b.order_id"
                      " WHERE b.assignment_id = %s ORDER BY b.id", (aid,))
    filled = [o for o in orders_of(host, aid) if o["filled_size"] > 0]
    assert [b["order_id"] for b in bets] == [first["id"]] == [o["id"] for o in filled], (bets, filled)
    bet = bets[0]
    won = side_of[bet["market_id"]] == "home"
    assert bet["ingame"] is True and bet["clv"] is None and bet["result"] == ("win" if won else "loss")
    assert bet["model_id"] == ingame_id and bet["lineage_id"] == str(model["lineage_id"]), "scored for the in-game lineage"
    assert bet["state_at_entry"] == {"period": 2, "clock_seconds": 600, "home_score": 21, "away_score": 0,
                                     "possession": "home"}
    assert summary["bets"] == 1 and ingame_id in [x["lineage_id"] for x in summary["lineages"]], summary
    scores = {r["model_id"]: r for r in rows(host, "SELECT * FROM model_scores WHERE game_id = %s", (GAME_ID,))}
    assert set(scores) == {ingame_id, pregame_id}, scores
    assert scores[pregame_id]["n_bets"] == scores[pregame_id]["ingame_n_bets"] == 0, "the pre-game model placed nothing"
    score = scores[ingame_id]
    assert score["n_bets"] == score["ingame_n_bets"] == 1 and score["avg_clv"] is None
    assert score["pnl_cents"] == score["ingame_pnl_cents"] == bet["pnl_cents"]
    done = host.get(f"/api/assignments/{aid}")
    assert done["status"] == "settled" and done["positions"] == []
    assert done["bankroll"]["realized_pnl_cents"] == bet["pnl_cents"]
    assert done["bankroll"]["available_cents"] == BANKROLL_CENTS + bet["pnl_cents"]
    job = host.job(job_id)
    assert job["status"] == "succeeded" and job["result"]["ingame_n_bets"] == 1, job["result"]
    assert job["result"]["ingame_pnl_cents"] == bet["pnl_cents"]
    page = host.client.get("/trading").text
    assert '<span class="chip chip-ingame">in-game</span>' in page and first["id"] in page
    board = host.get("/api/models")
    entry = next(m for m in board["unranked"] if m["id"] == ingame_id)
    assert entry["is_ingame"] and entry["ingame"] == {"games": 1, "bets": 1, "pnl_cents": bet["pnl_cents"]}, entry
    assert entry["ingame_validation"] is not None and entry["status"] == "candidate"
    assert 'id="ingame"' in host.client.get("/models").text
    assert "ledger ok" in run_cli(["ledger-check"])
