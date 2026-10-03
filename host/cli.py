"""Operator CLI that talks to the database directly (no HTTP)."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from host import auth, db, queue, views
from host.api.owner import install_command
from host.api.serialize import jsonable
from host.config import Config
from host.errors import QueueError


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
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
