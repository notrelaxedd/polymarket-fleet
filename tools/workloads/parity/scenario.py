"""The paper scenario both modes run, step by step (see tools/workloads/paper_parity.py).

Everything that feeds the worker is fixed before the worker starts: the settings, the
games (the nflverse fixture plus three future games on fixed dates), a root elo_blend
model with fixed params and the frozen sim minute. The worker then trains the model
(a real `train` job through the real runner), trades three paper assignments until it
has nothing left to propose, and the games are settled with fixed scores.
"""
from __future__ import annotations

import contextlib
import csv
import io
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from fleet.models.base import params_hash
from host import cli, db
from host.exchange.adapters.sim import home_mid
from host.exchange.settle import simulate_final

from parity.hostapp import FrozenClock, HostApp, wait_for
from parity.workers import Worker, read_json

FIXTURE_GAMES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "games_sample.csv"
SETTINGS: dict[str, Any] = {
    "heartbeat_seconds": 1, "trade_tick_s": 1, "min_edge": 0.0, "kelly_fraction": 1.0, "snapshot_active_s": 2,
    "liquidity_floor_cents": 10_000, "fee_model": {"taker_rate": 0.0, "half_spread": 0.01},
}
ROOT_PARAMS = {"k": 24.0, "hfa": 55.0, "mov_scale": 1}
TRAIN_THROUGH = {"season": 2021, "week": 10}
# (fixture game, future game id, bankroll cents, max bet cents or None, final home, final away)
GAMES = (
    ("2025_02_BUF_NYJ", "2026_05_BUF_NYJ", 20_000, 150, 17, 27),
    ("2025_02_NE_MIA", "2026_05_NE_MIA", 15_000, None, 24, 20),
    ("2025_02_SF_NO", "2026_05_SF_NO", 10_000, 250, 21, 21),
)
OPEN_STATUSES = ("approved", "submitting", "open", "partial", "cancel_requested")


@dataclass(frozen=True)
class Plan:
    """What both modes share: the future gameday and the frozen sim minute."""

    gameday: date
    clock: FrozenClock
    ticks: int
    quiet_ticks: int
    tick_timeout: float


def future_games_csv(directory: Path, gameday: date) -> Path:
    with open(FIXTURE_GAMES, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        rows = {r["game_id"]: r for r in reader}
    path = directory / "future_games.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        for source, game_id, *_ in GAMES:
            row = dict(rows[source])
            row.update({"game_id": game_id, "season": "2026", "week": "5", "gameday": gameday.isoformat(),
                        "gametime": "13:00", "home_score": "", "away_score": "", "result": "", "total": "",
                        "overtime": "", "old_game_id": "", "gsis": "", "pfr": "", "espn": "", "ftn": ""})
            writer.writerow(row)
    return path


def pick_minute(clock_start: FrozenClock) -> FrozenClock:
    """The minute (of the next hour) where the first game's home mid is highest, so its
    away ask is low: the trained model then sees an edge there, as in tests/e2e_trading.py."""
    _, game_id, *_ = GAMES[0]
    with open(FIXTURE_GAMES, newline="", encoding="utf-8") as fh:
        source = next(r for r in csv.DictReader(fh) if r["game_id"] == GAMES[0][0])
    game = {"game_id": game_id, "home_moneyline": int(source["home_moneyline"]), "away_moneyline": int(source["away_moneyline"])}
    best = max(range(60), key=lambda k: (home_mid(game, clock_start.shifted(k)), -k))
    return FrozenClock(clock_start.shifted(best))


def run_cli(database_url: str, args: list[str]) -> str:
    os.environ["DATABASE_URL"] = database_url
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(args)
    if code != 0:
        raise RuntimeError(f"host.cli {args}: exit {code}: {out.getvalue()}")
    return out.getvalue()


def seed(host: HostApp, work: Path, plan: Plan) -> str:
    """Settings, games and the root model; returns the root model id."""
    host.post("/api/settings", SETTINGS)
    run_cli(host.database_url, ["ingest-games", "--file", str(FIXTURE_GAMES)])
    run_cli(host.database_url, ["ingest-games", "--file", str(future_games_csv(work, plan.gameday))])
    model_id = uuid.uuid4()
    with db.connect(host.database_url) as conn:
        conn.execute(
            "INSERT INTO models (id, lineage_id, family, params, params_hash, status)"
            " VALUES (%s, %s, 'elo_blend', %s, %s, 'candidate')",
            (model_id, model_id, Jsonb(ROOT_PARAMS), params_hash(ROOT_PARAMS)),
        )
    return str(model_id)


def enroll_and_start(host: HostApp, worker: Worker) -> str:
    token = host.post("/api/enroll-token")["token"]
    worker.prepare(host.url, host.code_version, token)
    worker.start()
    conf = wait_for(lambda: read_json(worker.state / "worker.conf"), "worker.conf", timeout=60.0)
    worker_id = str(conf["worker_id"])
    wait_for(lambda: settled(host, worker_id, "idle"), "worker online and idle", timeout=60.0)
    return worker_id


def settled(host: HostApp, worker_id: str, role: str) -> bool:
    w = host.worker(worker_id)
    return bool(w and w["online"] and w["reported_role"] == role and w["desired_role"] == role and not w["switching"])


def job_done(host: HostApp, job_id: str) -> Any:
    job = host.get(f"/api/jobs/{job_id}")
    if job["status"] in ("failed", "cancelled"):
        raise RuntimeError(f"job {job_id} {job['status']}: {job.get('error')}")
    return job if job["status"] == "succeeded" else False


def train(host: HostApp, worker_id: str, root_id: str) -> str:
    body = {"kind": "train", "params": {"model_id": root_id, "through": TRAIN_THROUGH}, "target": worker_id}
    job = host.post("/api/jobs", body, expect=201)
    done = wait_for(lambda: job_done(host, job["id"]), "train job", timeout=120.0)
    return str(done["result"]["created_models"][0]["id"])


def assign(host: HostApp, worker_id: str, model_id: str) -> list[str]:
    wait_for(lambda: len([m for m in host.get("/api/markets") if m["snapshot_age_s"] is not None]) >= 2 * len(GAMES),
             "sim markets with snapshots", timeout=60.0)
    ids = []
    for _, game_id, bankroll, max_bet, *_ in GAMES:
        body: dict[str, Any] = {"game_id": game_id, "model_id": model_id, "mode": "paper", "bankroll_cents": bankroll}
        if max_bet is not None:
            body["max_bet_cents"] = max_bet
        ids.append(host.post("/api/assignments", body, expect=201)["job_id"])
    host.post(f"/api/workers/{worker_id}/role", {"role": "trade"})
    wait_for(lambda: all(host.get(f"/api/jobs/{j}")["status"] == "leased" for j in ids), "trade jobs leased", timeout=60.0)
    return ids


def trade_until_quiet(host: HostApp, worker: Worker, plan: Plan) -> dict[str, Any]:
    """Run at least plan.ticks worker ticks, and on until plan.quiet_ticks ticks in a row
    proposed nothing while no order is open. Returns the tick log."""
    seen: dict[int, int] = {}
    start = None
    deadline = time.monotonic() + plan.tick_timeout
    while time.monotonic() < deadline:
        trade = (read_json(worker.state / "status.json") or {}).get("trade") or {}
        ticks, last = int(trade.get("ticks") or 0), trade.get("last_tick") or {}
        if ticks and start is None:
            start = ticks
        if ticks and ticks not in seen and "proposed" in last:
            seen[ticks] = int(last["proposed"])
        done = sorted(seen)
        quiet = done[-plan.quiet_ticks:] if len(done) >= plan.quiet_ticks else []
        open_orders = host.sql("SELECT count(*) AS n FROM orders WHERE status = ANY(%s)", (list(OPEN_STATUSES),))[0]["n"]
        if (start is not None and ticks - start + 1 >= plan.ticks and quiet and all(seen[t] == 0 for t in quiet)
                and quiet == list(range(quiet[0], quiet[0] + plan.quiet_ticks)) and open_orders == 0):
            return {"ticks_observed": len(seen), "proposals": sum(seen.values())}
        time.sleep(0.1)
    tail = sorted(seen.items())[-8:]
    raise TimeoutError(f"the worker did not go quiet within {plan.tick_timeout:.0f}s (last ticks, proposals: {tail})")


def settle(host: HostApp, worker_id: str, job_ids: list[str]) -> None:
    for _, game_id, *_, home, away in GAMES:
        with db.connect(host.database_url) as conn:
            simulate_final(conn, game_id, home, away, "parity")
    wait_for(lambda: all(job_done(host, j) for j in job_ids), "trade jobs succeeded", timeout=60.0)
    wait_for(lambda: (w := host.worker(worker_id)) and w["current_jobs"] == [], "worker dropped the trade jobs", timeout=30.0)
    host.post(f"/api/workers/{worker_id}/role", {"role": "idle"})
    wait_for(lambda: settled(host, worker_id, "idle"), "worker idle", timeout=30.0)


def run(host: HostApp, worker: Worker, work: Path, plan: Plan) -> dict[str, Any]:
    """The whole scenario on a started host; returns run facts for the report."""
    root_id = seed(host, work, plan)
    host.start_exchange()
    worker_id = enroll_and_start(host, worker)
    child_id = train(host, worker_id, root_id)
    job_ids = assign(host, worker_id, child_id)
    ticks = trade_until_quiet(host, worker, plan)
    settle(host, worker_id, job_ids)
    return {"worker_id": worker_id, **ticks}
