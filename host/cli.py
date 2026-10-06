"""Operator CLI that talks to the database directly (no HTTP)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Sequence

from host import auth, cli_ingame, db, kill, leaderboard, nflverse, queue, views
from host.api.owner import install_command
from host.api.serialize import jsonable
from host.config import Config
from host.errors import QueueError
from host.money import cents_to_dollars, dollars_to_cents
from host.settings import get_int_setting, get_setting
from host.trading import assignments, ledger
from host.trading import views as trading_views

ROLETEST_SLEEP_SECONDS = 120
ROLETEST_LIMIT_SECONDS = 10.0
ROLETEST_POLL_SECONDS = 0.2


def print_table(rows: list[dict[str, Any]], columns: Sequence[str]) -> None:
    """Render rows as a fixed-width text table."""
    cells = [[_cell(row.get(col)) for col in columns] for row in rows]
    widths = [max([len(col)] + [len(r[i]) for r in cells]) for i, col in enumerate(columns)]
    print("  ".join(col.ljust(widths[i]) for i, col in enumerate(columns)))
    for r in cells:
        print("  ".join(r[i].ljust(widths[i]) for i in range(len(columns))))


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(jsonable(value))


def cmd_migrate(config: Config, _: argparse.Namespace) -> None:
    applied = db.migrate(config.database_url)
    print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))


def cmd_enroll_token(config: Config, _: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        token, expires_at = auth.create_enroll_token(conn)
    print(f"token: {token}")
    print(f"expires_at: {jsonable(expires_at)}")
    print(install_command(config.public_url, token))


def cmd_workers(config: Config, _: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        rows = views.fleet_workers(conn)
    for row in rows:
        row["jobs"] = ",".join(j["id"][:8] for j in row["current_jobs"]) or "-"
    cols = ["id", "name", "online", "desired_role", "reported_role", "role_epoch", "acked_epoch",
            "auto_role", "enabled", "cpu_pct", "ram_used_mb", "code_version", "jobs"]
    print_table(rows, cols)


def cmd_jobs(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        rows = views.list_jobs(conn, args.status, args.limit)
    cols = ["id", "kind", "role", "status", "progress", "target_worker_id", "lease_worker_id",
            "expiries", "created_at"]
    print_table(rows, cols)


def cmd_role(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        worker = queue.set_role(conn, args.worker, args.role, actor="cli")
    print(f"{worker['id']} desired_role={worker['desired_role']} role_epoch={worker['role_epoch']}")


def cmd_send_job(config: Config, args: argparse.Namespace) -> None:
    params = json.loads(args.params) if args.params else {}
    with db.connect(config.database_url) as conn:
        result = queue.create_job(conn, args.kind, params, args.target, args.idempotency_key, actor="cli")
    job = result.job
    note = " (waiting for an idle worker)" if result.waiting_for_idle_worker else ""
    verb = "created" if result.created else "existing"
    print(f"{verb} job {job['id']} kind={job['kind']} status={job['status']} target={job['target_worker_id']}{note}")


def cmd_cancel(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        job = queue.cancel_job(conn, args.job, actor="cli")
    print(f"job {job['id']} status={job['status']}")


def cmd_kill(config: Config, _: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        changed = kill.set_kill(conn, actor="cli")
    print("kill_switch=true" + ("" if changed else " (already set)"))


def cmd_kill_reset(config: Config, args: argparse.Namespace) -> None:
    confirm = kill.RESET_CONFIRMATION if args.yes else input(f"Type {kill.RESET_CONFIRMATION} to clear the kill switch: ")
    with db.connect(config.database_url) as conn:
        kill.reset_kill(conn, "cli", confirm)
    print("kill_switch=false")


def _wait_for_ack(config: Config, worker_id: str, timeout: float) -> dict[str, Any] | None:
    """Poll until the worker has acked its desired role; the row on ack, None on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        with db.connect(config.database_url, autocommit=True) as conn:
            row = queue.get_worker(conn, worker_id)
        if row["acked_epoch"] == row["role_epoch"] and row["reported_role"] == row["desired_role"]:
            return row
        if time.monotonic() >= deadline:
            return None
        time.sleep(ROLETEST_POLL_SECONDS)


def cmd_roletest(config: Config, args: argparse.Namespace) -> None:
    """Send a long sleep job to the worker, then flip it to train and time the ack.

    Both timestamps are host side: audit_log.ts of the role change and the
    workers.last_heartbeat_at written by the heartbeat that acked it. The sleep job
    is cancelled on every exit path (timeout, limit, Ctrl-C) so a worker that comes
    back later does not run a job nobody asked for.
    """
    with db.connect(config.database_url) as conn:
        result = queue.create_job(conn, "sleep", {"seconds": ROLETEST_SLEEP_SECONDS}, args.worker, None, "roletest")
    job_id = result.job["id"]
    print(f"sent sleep job {job_id} to {args.worker}; waiting for the role ack")
    try:
        if _wait_for_ack(config, args.worker, args.timeout) is None:
            raise QueueError(f"worker {args.worker} never acked the job's role within {args.timeout:g} s")
        with db.connect(config.database_url) as conn:
            worker = queue.set_role(conn, args.worker, "train", actor="roletest")
            requested = conn.execute(
                "SELECT ts FROM audit_log WHERE action = 'set_role' AND entity = %s ORDER BY id DESC LIMIT 1",
                (args.worker,),
            ).fetchone()["ts"]
        print(f"set role train (epoch {worker['role_epoch']}); waiting for the ack")
        acked = _wait_for_ack(config, args.worker, args.timeout)
        if acked is None:
            raise QueueError(f"worker {args.worker} never acked role train within {args.timeout:g} s")
        seconds = (acked["last_heartbeat_at"] - requested).total_seconds()
        print(f"role ack latency: {seconds:.2f} s")
        if seconds > ROLETEST_LIMIT_SECONDS:
            raise QueueError(f"role ack took {seconds:.2f} s, over the {ROLETEST_LIMIT_SECONDS:.0f} s limit")
    finally:
        with db.connect(config.database_url) as conn:
            try:
                queue.cancel_job(conn, job_id, actor="roletest")
            except QueueError:
                pass


def cmd_ingest_games(config: Config, args: argparse.Namespace) -> None:
    """Fetch nflverse games.csv (or read --file) and upsert it into games."""
    with db.connect(config.database_url) as conn:
        url = str(get_setting(conn, "nflverse_url", nflverse.DEFAULT_URL))
        result = nflverse.ingest(conn, args.file, url)
        last = nflverse.last_complete_season(conn)
    print(f"ingested {result['rows']} rows from {result['source']}: {result['inserted']} inserted, "
          f"{result['changed']} changed; last complete season {last}")


def cmd_ingest_signals(config: Config, args: argparse.Namespace) -> None:
    """Load nflverse injuries or play-by-play: one season, every season (all) or a file."""
    from host import data_refresh

    for line in data_refresh.backfill(lambda: db.connect(config.database_url), args.kind, args.season, args.file):
        print(line)


def cmd_models(config: Config, _: argparse.Namespace) -> None:
    """The leaderboard as a table (ranked rows first, then unranked)."""
    with db.connect(config.database_url) as conn:
        board = leaderboard.leaderboard(conn)
    rows = []
    for entry in board["ranked"] + board["unranked"]:
        m = entry["metrics"]
        rows.append({
            "rank": entry.get("rank", "-"), "id": str(entry["id"])[:8], "status": entry["status"],
            "family": entry["family"], "params": entry["short_params"], "roi": m.get("roi"),
            "bets": m.get("n_bets"), "log_loss": m.get("log_loss"), "market": m.get("market_log_loss"),
            "drawdown": m.get("max_drawdown"), "seasons": f"{m['seasons'][0]}-{m['seasons'][-1]}" if m.get("seasons") else "-",
            "rows": entry["members"],
        })
    print_table(rows, ["rank", "id", "status", "family", "params", "roi", "bets", "log_loss", "market", "drawdown", "seasons", "rows"])


def cmd_assign(config: Config, args: argparse.Namespace) -> None:
    """Create an assignment (bankroll and max bet in dollars)."""
    with db.connect(config.database_url) as conn:
        bankroll = get_int_setting(conn, "default_bankroll_cents", 10_000)
        if args.bankroll:
            bankroll = dollars_to_cents(args.bankroll, "bankroll")
        max_bet = dollars_to_cents(args.max_bet, "max bet") if args.max_bet else None
        row = assignments.create_assignment(conn, args.game_id, args.model_id, args.mode, bankroll, "cli", max_bet)
    print(f"assignment {row['id']} game={row['game_id']} model={str(row['model_id'])[:8]} mode={row['mode']} "
          f"bankroll=${cents_to_dollars(row['bankroll']['available_cents'])} job={row['job_id']}")


def cmd_assignments(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        rows = assignments.list_assignments(conn, args.status)
    flat = []
    for r in rows:
        b = r["bankroll"]
        flat.append({
            "id": str(r["id"])[:8], "game": r["game_id"], "model": str(r["model_id"])[:8], "mode": r["mode"],
            "status": r["status"], "available": cents_to_dollars(b["available_cents"]),
            "reserved": cents_to_dollars(b["reserved_cents"]), "open": cents_to_dollars(b["open_cost_cents"]),
            "realized": cents_to_dollars(b["realized_pnl_cents"]), "open_orders": r["open_orders"],
            "job": r["job"]["status"], "kickoff": r["game"]["kickoff_at"],
        })
    print_table(flat, ["id", "game", "model", "mode", "status", "available", "reserved", "open", "realized",
                       "open_orders", "job", "kickoff"])


def cmd_orders(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        rows = trading_views.list_orders(conn, args.status, args.limit)
    for r in rows:
        r["id"] = str(r["id"])[:8]
        r["cost"] = cents_to_dollars(r["cost_cents"])
        r["market"] = f"{r['game_id']} {r['side']}"
    print_table(rows, ["id", "mode", "status", "reject_reason", "market", "price", "size", "filled_size", "cost",
                       "worker_id", "created_at"])


def cmd_cancel_all(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        result = kill.cancel_all(conn, "cli", args.mode)
    print(f"cancelled={result['cancelled']} requested={result['requested']}")


def cmd_ledger_check(config: Config, _: argparse.Namespace) -> None:
    """Replay every bankroll's ledger against its cached columns; exit 1 on a mismatch."""
    with db.connect(config.database_url, autocommit=True) as conn:
        n = conn.execute("SELECT count(*) AS n FROM bankrolls").fetchone()["n"]
        problems = ledger.replay_problems(conn)
    for problem in problems:
        print(problem)
    if problems:
        raise QueueError(f"ledger replay found {len(problems)} problem(s) across {n} bankroll(s)")
    print(f"ledger ok: {n} bankroll(s) replay to their cached columns")


def cmd_exchange_state(config: Config, _: argparse.Namespace) -> None:
    with db.connect(config.database_url, autocommit=True) as conn:
        state = trading_views.exchange_state(conn)
    print(json.dumps(jsonable(state), indent=2, sort_keys=True))


def cmd_simulate_final(config: Config, args: argparse.Namespace) -> None:
    """Set a final score for testing (market_source sim or FLEET_DEV), then settle."""
    try:
        from host.exchange.settle import simulate_final
    except ImportError:
        raise QueueError("exchange module not available") from None
    with db.connect(config.database_url) as conn:
        result = simulate_final(conn, args.game_id, args.home, args.away, "cli")
    print(json.dumps(jsonable(result), sort_keys=True))


def cmd_run_loop(config: Config, _: argparse.Namespace) -> None:
    pool = db.make_pool(config.database_url, max_size=2)
    try:
        from host.loop import run_once

        print(json.dumps(run_once(pool)))
    finally:
        pool.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m host.cli", description="fleet host operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("enroll-token", help="mint a single-use enroll token").set_defaults(func=cmd_enroll_token)
    sub.add_parser("workers", help="list workers").set_defaults(func=cmd_workers)
    p = sub.add_parser("jobs", help="list jobs, newest first")
    p.add_argument("--status", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_jobs)
    p = sub.add_parser("role", help="set a worker's desired role")
    p.add_argument("worker")
    p.add_argument("role")
    p.set_defaults(func=cmd_role)
    p = sub.add_parser("send-job", help="create a job")
    p.add_argument("kind")
    p.add_argument("--target", default=None, help="any_idle or a worker id")
    p.add_argument("--params", default=None, help="JSON object")
    p.add_argument("--idempotency-key", default=None)
    p.set_defaults(func=cmd_send_job)
    p = sub.add_parser("cancel", help="cancel a job")
    p.add_argument("job")
    p.set_defaults(func=cmd_cancel)
    sub.add_parser("run-loop", help="one reaper + dispatcher iteration").set_defaults(func=cmd_run_loop)
    p = sub.add_parser("ingest-games", help="fetch nflverse games.csv (or read a file) into the games table")
    p.add_argument("--file", default=None, help="a local games.csv instead of the download")
    p.set_defaults(func=cmd_ingest_games)
    for kind, name in (("injuries", "ingest-injuries"), ("pbp", "ingest-pbp")):
        p = sub.add_parser(name, help=f"load nflverse {kind} for a season, every season (all) or a file")
        p.add_argument("--season", default=None, help="a season year, or all for the full backfill")
        p.add_argument("--file", default=None, help="a local file instead of the download")
        p.set_defaults(func=cmd_ingest_signals, kind=kind)
    sub.add_parser("models", help="the model leaderboard").set_defaults(func=cmd_models)
    sub.add_parser("kill", help="raise the kill switch (trade role stops)").set_defaults(func=cmd_kill)
    p = sub.add_parser("kill-reset", help="clear the kill switch (prompts for RESUME)")
    p.add_argument("--yes", action="store_true", help="skip the RESUME prompt")
    p.set_defaults(func=cmd_kill_reset)
    p = sub.add_parser("roletest", help="measure a worker's role-switch latency")
    p.add_argument("worker")
    p.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for each ack")
    p.set_defaults(func=cmd_roletest)
    p = sub.add_parser("assign", help="create an assignment: game, model, mode, bankroll dollars")
    p.add_argument("game_id")
    p.add_argument("model_id")
    p.add_argument("--mode", default="paper", choices=("paper", "live"))
    p.add_argument("--bankroll", default=None, help="dollars (default: default_bankroll_cents)")
    p.add_argument("--max-bet", default=None, help="dollars, lowers the global max bet for this assignment")
    p.set_defaults(func=cmd_assign)
    p = sub.add_parser("assignments", help="list assignments")
    p.add_argument("--status", default=None)
    p.set_defaults(func=cmd_assignments)
    p = sub.add_parser("orders", help="list orders, newest first")
    p.add_argument("--status", default=None, help="one status, or active")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_orders)
    p = sub.add_parser("cancel-all", help="cancel every active order without a kill")
    p.add_argument("--mode", default=None, choices=("paper", "live"))
    p.set_defaults(func=cmd_cancel_all)
    sub.add_parser("ledger-check", help="replay every bankroll's ledger").set_defaults(func=cmd_ledger_check)
    sub.add_parser("exchange-state", help="the exchange process state").set_defaults(func=cmd_exchange_state)
    p = sub.add_parser("simulate-final", help="set a final score for a game (sim source or FLEET_DEV)")
    p.add_argument("game_id")
    p.add_argument("--home", type=int, required=True)
    p.add_argument("--away", type=int, required=True)
    p.set_defaults(func=cmd_simulate_final)
    cli_ingame.add_parsers(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from host.workloads import cli as workloads_cli

    argv = list(sys.argv[1:] if argv is None else argv)
    if workloads_cli.handles(argv):  # workloads, machines, pin, secret-set, ... (docs/workloads-design.md section 9)
        return workloads_cli.main(argv)
    args = build_parser().parse_args(argv)
    config = Config.from_env()
    try:
        args.func(config, args)
    except QueueError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
