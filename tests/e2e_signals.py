"""Step 6 Part B phase of the end-to-end test (tests/test_e2e.py): snapshot replay,
the signals feed, an epa_blend search and a paper sell, on the real host, the real
agent and (for the sell) the real exchange loop on the sim source.

Snapshot replay: confirmed "sim" markets with minute bars around the decision time
(kickoff minus decision_minutes_before_kickoff) are written straight into markets,
price_bars and price_snapshots for a few played 2025 fixture games, with a frozen
closing price above every entry (a constructed rising close), plus three games the
replay must not score (bars only outside the 30-minute window or after the decision
time, an unconfirmed mapping, another platform). A snapshot backtest of the trained
model is first sent with allow_sim_prices off: the worker refuses the sim platform
(the job fails, no prices file is fetched, nothing is stored); with it on, the job
replays 2025 through the latest season in games, the lineage gets snapshot_metrics
(price_source "snapshots", the exact CLV of the constructed prices) while its
backtest_metrics and status stay, and the Models page shows the snapshot group.

Signals and epa_blend: team_game_stats rows and injury reports go in directly; the
games feed carries the stats and the leak-free Out signals; an epa_blend search on
the fixture creates validated models. The paper sell lives in tests/e2e_sells.py.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fleet.worker import config as worker_config
from tests.e2e_models import finished, send
from tests.e2e_sells import phase_sells

PLATFORM = "sim"
CLOSE = 0.58
LIQUIDITY = 5_000_000
DECISION_MINUTES = 60
# (minutes after the decision time, mid, bid, ask): a rising market; the last bar whose
# minute closed by the decision time is the one the replay buys at, the one after it is
# lookahead.
RISING = [(-25, 0.395, 0.39, 0.40), (-15, 0.405, 0.40, 0.41), (-5, 0.415, 0.41, 0.42), (5, 0.50, 0.49, 0.51)]
LATE_ONLY = [(-45, 0.415, 0.41, 0.42), (5, 0.415, 0.41, 0.42)]
DEPTH = {"bid": 0.40, "ask": 0.41, "mid": 0.405, "bid_depth": [[0.40, 300.0]], "ask_depth": [[0.41, 30.0], [0.42, 400.0]]}
N_SCORED = 5
EXPECTED_CLV = ((N_SCORED - 1) * (CLOSE - 0.42) + (CLOSE - DEPTH["ask"])) / N_SCORED
EPA_SEARCH = {"family": "epa_blend", "n": 2, "seed": 5, "seasons": [2021, 2021], "top_k": 2}
EPA_KEYS = {"window", "shrink", "l2", "min_edge", "kelly_fraction"}


def _conn(host: Any) -> psycopg.Connection:
    return psycopg.connect(host.database_url, autocommit=True, row_factory=dict_row)


def _market(conn: psycopg.Connection, game: dict[str, Any], side: str, platform: str = PLATFORM,
            confirmed: bool = True) -> Any:
    team = game["home_team"] if side == "home" else game["away_team"]
    return conn.execute(
        "INSERT INTO markets (platform, market_ref, title, game_id, side, mapping_confirmed, status, closing_price)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'closed', %s) RETURNING id",
        (platform, f"replay:{game['game_id']}:{side}", f"{team} to win (recorded)", game["game_id"], side,
         confirmed, CLOSE),
    ).fetchone()["id"]


def _bars(conn: psycopg.Connection, market_id: Any, decision: datetime, bars: list[tuple[int, float, float, float]]) -> None:
    for offset, mid, bid, ask in bars:
        conn.execute(
            "INSERT INTO price_bars (market_id, minute, open, high, low, close, bid, ask, min_liquidity_usd_cents, n)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)",
            (market_id, decision + timedelta(minutes=offset), mid, mid, mid, mid, bid, ask, LIQUIDITY),
        )


def seed_recorded_prices(host: Any) -> list[str]:
    """Recorded prices for eight played 2025 games; returns the five the replay scores."""
    with _conn(host) as conn:
        games = conn.execute(
            "SELECT game_id, kickoff_at, home_team, away_team FROM games WHERE season = 2025 AND home_score IS NOT NULL"
            " ORDER BY kickoff_at, game_id LIMIT 8"
        ).fetchall()
        assert len(games) == 8
        for index, game in enumerate(games):
            decision = game["kickoff_at"] - timedelta(minutes=DECISION_MINUTES)
            for side in ("home", "away"):
                platform = "polymarket_us" if index == 7 else PLATFORM
                market_id = _market(conn, game, side, platform, confirmed=index != 6)
                _bars(conn, market_id, decision, LATE_ONLY if index == 5 else RISING)
                if index == 0:  # a depth snapshot one minute before the decision: the fill walks it
                    conn.execute(
                        "INSERT INTO price_snapshots (market_id, ts, bid, ask, mid, bid_depth, ask_depth, liquidity_usd_cents)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (market_id, decision - timedelta(minutes=1), DEPTH["bid"], DEPTH["ask"], DEPTH["mid"],
                         Jsonb(DEPTH["bid_depth"]), Jsonb(DEPTH["ask_depth"]), LIQUIDITY),
                    )
    return [g["game_id"] for g in games[:N_SCORED]]


def expected_replay(host: Any) -> dict[str, Any]:
    """The seasons a snapshot replay of [2025, null] covers (2025 through the latest
    season in games, kept when a game has a confirmed sim market) and the played games
    it leaves unscored for lack of prices."""
    with _conn(host) as conn:
        latest = conn.execute("SELECT max(season) AS s FROM games").fetchone()["s"]
        seasons = [r["season"] for r in conn.execute(
            "SELECT DISTINCT g.season FROM markets m JOIN games g ON g.game_id = m.game_id WHERE m.platform = %s"
            " AND m.mapping_confirmed AND m.side IN ('home', 'away') AND g.season BETWEEN 2025 AND %s ORDER BY 1",
            (PLATFORM, latest),
        ).fetchall()]
        played = conn.execute(
            "SELECT count(*) AS n FROM games WHERE season = ANY(%s) AND home_score IS NOT NULL AND away_score IS NOT NULL",
            (seasons,),
        ).fetchone()["n"]
    return {"latest": latest, "seasons": seasons, "unscored": played - N_SCORED}


def _snapshot_job(host: Any, worker_id: str, model_id: str) -> dict[str, Any]:
    job = send(host, "backtest", {"model_id": model_id, "price_source": "snapshots", "seasons": [2025, None]}, worker_id)
    params = job["params"]
    assert params["price_source"] == "snapshots" and params["price_platform"] == PLATFORM, params
    assert params["decision_minutes_before_kickoff"] == DECISION_MINUTES and params["participation"] == 0.5
    return job


def phase_replay(host: Any, state_dir: str, worker_id: str, models: dict[str, Any], wait_for: Callable[..., Any],
                 settled: Callable[..., Any]) -> str:
    """Returns the id of the refused (failed) job."""
    child, root = models["child"], models["roots"][0]
    host.post("/api/settings", {"participation": 0.5, "decision_minutes_before_kickoff": DECISION_MINUTES})
    before = {mid: host.get(f"/api/models/{mid}") for mid in (child, root)}
    assert all(m["snapshot_metrics"] is None for m in before.values())
    seed_recorded_prices(host)
    cache = worker_config.prices_cache_path(state_dir, PLATFORM)

    # allow_sim_prices off (the default): the Jobs page warns, the worker refuses.
    assert host.get("/api/settings")["allow_sim_prices"] is False
    assert 'class="error small replay-sim"' in host.client.get("/jobs").text
    refused = _snapshot_job(host, worker_id, child)
    assert refused["params"]["allow_sim_prices"] is False
    failed = wait_for(lambda: (j := host.job(refused["id"]))["status"] == "failed" and j, "sim prices refused", timeout=30.0)
    assert "allow_sim_prices" in (failed["error"] or ""), failed["error"]
    assert not os.path.exists(cache), "no sim prices were fetched"
    assert all(host.get(f"/api/models/{mid}")["snapshot_metrics"] is None for mid in (child, root))
    assert "model_snapshot_backtest" not in host.events(refused["id"])
    wait_for(settled(host, worker_id, "idle"), "worker idle after the refused replay")

    # allow_sim_prices on: replayed through the latest season, stored apart.
    host.post("/api/settings", {"allow_sim_prices": True})
    assert 'class="error small replay-sim"' not in host.client.get("/jobs").text
    expect = expected_replay(host)
    job = _snapshot_job(host, worker_id, child)
    assert job["params"]["allow_sim_prices"] is True and job["params"]["seasons"] == [2025, expect["latest"]]
    done = wait_for(finished(host, job["id"]), "snapshot replay done", timeout=60.0)
    result = done["result"]
    assert result["price_source"] == "snapshots" and result["platform"] == PLATFORM
    assert result["seasons"] == expect["seasons"] and expect["seasons"][0] == 2025, (result["seasons"], expect)
    assert result["n_games"] == N_SCORED and result["n_bets"] == N_SCORED, "every constructed game bets once"
    assert result["n_unscored_no_prices"] == expect["unscored"], (result["n_unscored_no_prices"], expect)
    assert sum(r["n_unscored_no_prices"] for r in result["per_season"]) == expect["unscored"]
    assert abs(result["avg_clv"] - EXPECTED_CLV) < 1e-6, (result["avg_clv"], EXPECTED_CLV)
    low, high = result["ci"]["avg_clv"]
    assert CLOSE - 0.42 - 1e-6 <= low <= high <= CLOSE - DEPTH["ask"] + 1e-6, "every CLV is positive: the close rose"
    with open(cache, encoding="utf-8") as fh:
        fetched = json.load(fh)["markets"]
    assert fetched and {m["platform"] for m in fetched} == {PLATFORM}, "the worker fetched the sim prices only"
    assert "model_snapshot_backtest" in host.events(job["id"]) and "model_backtest" not in host.events(job["id"])
    for mid in (child, root):
        stored = host.get(f"/api/models/{mid}")
        assert stored["snapshot_metrics"] == result, "the whole lineage carries the replay"
        assert stored["backtest_metrics"] == before[mid]["backtest_metrics"], "closing-line numbers untouched"
        assert stored["status"] == before[mid]["status"]
    board = host.get("/api/models")
    entry = next(m for m in board["ranked"] + board["unranked"] if m["id"] == root)
    snap = entry["snapshot"]
    assert snap["n_games"] == N_SCORED and snap["n_bets"] == N_SCORED and snap["clv_estimated"] is False
    assert abs(snap["avg_clv"] - EXPECTED_CLV) < 1e-6 and entry["rank_mode"] != "snapshot", "below 30 bets: no snapshot rank"
    page = host.client.get("/models").text
    row = page[page.index(f'data-model="{root}"'):]
    row = row[:row.index("</tr>")]
    assert f'<span class="k">snapshot</span> {N_SCORED} games &middot; {N_SCORED} bets' in row and "snapshot-ci" in row
    detail = host.client.get(f"/models/{child}").text
    assert 'id="snapshot"' in detail and "Replayed on recorded <strong>sim</strong> prices" in detail
    wait_for(settled(host, worker_id, "idle"), "worker idle after the replay")
    host.post("/api/settings", {"allow_sim_prices": False})
    return refused["id"]


def seed_signals(host: Any) -> dict[str, Any]:
    """team_game_stats for six 2021 games and two injury reports on the first one: an
    Out quarterback filed before the decision time (counts) and an Out player filed
    after it (must not)."""
    with _conn(host) as conn:
        games = conn.execute(
            "SELECT game_id, season, week, game_type, kickoff_at, home_team, away_team FROM games"
            " WHERE season = 2021 ORDER BY kickoff_at, game_id LIMIT 6"
        ).fetchall()
        for i, game in enumerate(games):
            for j, team in enumerate((game["home_team"], game["away_team"])):
                conn.execute(
                    "INSERT INTO team_game_stats (game_id, team, season, week, kickoff_at, off_epa_per_play,"
                    " def_epa_per_play, pass_rate, plays, success_rate) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (game["game_id"], team, game["season"], game["week"], game["kickoff_at"], 0.02 * (i - 2) + 0.05 * j,
                     -0.01 * i, 0.55 + 0.01 * i, 60 + i, 0.42 + 0.01 * j),
                )
        first = games[0]
        decision = first["kickoff_at"] - timedelta(minutes=DECISION_MINUTES)
        for team, gsis, position, modified in ((first["home_team"], "00-e2e-qb", "QB", decision - timedelta(days=2)),
                                               (first["away_team"], "00-e2e-wr", "WR", decision + timedelta(minutes=10))):
            conn.execute(
                "INSERT INTO injuries (season, game_type, week, team, gsis_id, full_name, position, report_status,"
                " date_modified) VALUES (%s, %s, %s, %s, %s, %s, %s, 'Out', %s)",
                (first["season"], first["game_type"], first["week"], team, gsis, f"E2E {position}", position, modified),
            )
    return {"games": [g["game_id"] for g in games], "first": first["game_id"]}


def phase_epa(host: Any, state_dir: str, worker_id: str, wait_for: Callable[..., Any], settled: Callable[..., Any]) -> list[str]:
    seeded = seed_signals(host)
    token = worker_config.load_conf(state_dir)["worker_token"]
    feed = host.client.get("/api/v1/data/games", headers={"Authorization": f"Bearer {token}"}).json()
    stats = [r for r in feed["team_game_stats"] if r["game_id"] in seeded["games"]]
    assert len(stats) == 12 and set(stats[0]) == {"game_id", "season", "week", "team", "kickoff_at", "off_epa_per_play",
                                                  "def_epa_per_play", "pass_rate", "plays", "success_rate"}
    signals = next(g for g in feed["games"] if g["game_id"] == seeded["first"])["signals"]
    assert signals["home_out_qb"] == 1 and signals["home_out_count"] == 1, signals
    assert signals["away_out_qb"] == 0 and signals["away_out_count"] == 0, "a report filed after the decision time never counts"

    job = send(host, "model_search", EPA_SEARCH, "any_idle")
    assert job["params"]["family"] == "epa_blend" and job["params"]["validation_seasons"][0] == 2022
    wait_for(settled(host, worker_id, "model_search"), "agent in model_search for epa_blend")
    done = wait_for(finished(host, job["id"]), "epa_blend search done", timeout=90.0)
    result = done["result"]
    assert result["evaluated"] == 2 and result["seasons"] == [2021] and len(result["validated"]) == 2
    created = result["created_models"]
    assert len(created) == 2 and all(m["created"] is True for m in created), created
    ids = [m["id"] for m in created]
    board = host.get("/api/models")
    listed = {m["id"]: m for m in board["ranked"] + board["unranked"]}
    page = host.client.get("/models").text
    for mid in ids:
        model = host.get(f"/api/models/{mid}")
        assert model["family"] == "epa_blend" and set(model["params"]) == EPA_KEYS, model["params"]
        assert model["validation_metrics"]["seasons"] == result["validation_seasons"] and model["summary"].startswith("EPA blend (")
        assert listed[mid]["short_params"].startswith("window ") and listed[mid]["short_params"] in page
    wait_for(settled(host, worker_id, "idle"), "worker idle after the epa_blend search")
    return ids


def phase_signals(host: Any, state_dir: str, worker_id: str, agent: Any, models: dict[str, Any], tmp_path: Path,
                  wait_for: Callable[..., Any], settled: Callable[..., Any]) -> str:
    """Runs the three parts; returns the id of the refused snapshot job (the one job
    of the run that ends failed, on purpose)."""
    refused = phase_replay(host, state_dir, worker_id, models, wait_for, settled)
    phase_epa(host, state_dir, worker_id, wait_for, settled)
    phase_sells(host, state_dir, worker_id, models, tmp_path, wait_for, settled)
    return refused
