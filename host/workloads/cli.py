"""Operator CLI for workloads (docs/workloads-design.md section 9); host.cli dispatches here."""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Sequence

import psycopg

from host import db
from host.api.serialize import jsonable
from host.config import Config
from host.errors import BadRequest, NotFound, QueueError
from host.events import add_audit
from host.workloads import assign, config as wl_config, machines, outbound, pinning, queue, registry
from host.workloads import secrets as wl_secrets

ACTOR = "cli"
MACHINE_ID_RE = re.compile(r"^m_[0-9a-f]{6}$")
COMMANDS = (
    "workloads-sync", "workload-image", "machine-enroll-token", "machines", "machine-assign", "pin", "unpin",
    "secret-set", "outbound", "wl-send-job",
)


def handles(argv: Sequence[str]) -> bool:
    """True when host.cli should hand `argv` to this module.

    `assign` already exists for Polymarket (`assign GAME MODEL`); `assign m_xxxxxx WORKLOAD`
    (a machine id first) is the workloads form. `machine-assign` is the unambiguous alias.
    """
    if not argv:
        return False
    if argv[0] in COMMANDS:
        return True
    return argv[0] == "assign" and len(argv) > 1 and bool(MACHINE_ID_RE.match(argv[1]))


def resolve_machine(conn: psycopg.Connection, ref: str) -> dict[str, Any]:
    """A machine by id, or by name when exactly one machine has it."""
    row = conn.execute("SELECT * FROM machines WHERE id = %s", (ref,)).fetchone()
    if row is not None:
        return row
    rows = conn.execute("SELECT * FROM machines WHERE name = %s", (ref,)).fetchall()
    if len(rows) > 1:
        raise BadRequest(f"machine name {ref!r} is ambiguous; use the id")
    if not rows:
        raise NotFound(f"unknown machine: {ref!r}")
    return rows[0]


def _table(rows: list[dict[str, Any]], columns: Sequence[str]) -> None:
    from host.cli import print_table

    print_table(rows, columns)


def cmd_sync(config: Config, _: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        result = registry.sync_from_dir(conn, wl_config.workloads_dir())
        add_audit(conn, "workloads_sync", None, ACTOR, None, {"synced": result["synced"], "errors": sorted(result["errors"])})
    print("synced: " + (", ".join(result["synced"]) or "nothing"))
    for name, problems in result["errors"].items():
        print(f"error in {name}: " + "; ".join(problems))


def cmd_image(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        row = registry.set_image(conn, args.name, args.digest, args.size_mb)
        add_audit(conn, "workload_image", args.name, ACTOR, None, {"digest": args.digest, "size_mb": args.size_mb})
    print(f"{row['name']} image {row['image_digest']} ({row['image_size_mb'] or '?'} MB)")


def cmd_enroll_token(config: Config, _: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        result = machines.create_enroll_token(conn)
        add_audit(conn, "machine_enroll_token", None, ACTOR, None, {"expires_at": result["expires_at"].isoformat()})
    url = config.public_url
    print(f"token: {result['token']}")
    print(f"expires_at: {jsonable(result['expires_at'])}")
    print(f"curl -fsSL {url}/install-agent.sh | sudo bash -s -- {url} {result['token']}")


def cmd_machines(config: Config, _: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.name, m.enabled, m.pinned, m.native_polymarket AS native, m.ram_total_mb AS ram_mb,
                   m.disk_free_mb, COALESCE(m.disk_type_override, m.disk_type_detected) AS disk,
                   m.last_heartbeat_at, a.workload, a.epoch, a.acked_epoch, a.state
              FROM machines m LEFT JOIN workload_assignments a ON a.machine_id = m.id ORDER BY m.registered_at, m.id
            """
        ).fetchall()
    _table(rows, ["id", "name", "enabled", "pinned", "native", "ram_mb", "disk_free_mb", "disk", "workload",
                  "epoch", "acked_epoch", "state", "last_heartbeat_at"])


def cmd_assign(config: Config, args: argparse.Namespace) -> None:
    target = None if args.workload.lower() in ("none", "null", "-") else args.workload
    with db.connect(config.database_url) as conn:
        machine = resolve_machine(conn, args.machine)
        row = assign.assign(conn, machine["id"], target, ACTOR, None)
    print(f"{machine['id']} workload={row['workload'] or 'none'} epoch={row['epoch']} state={row['state']}"
          + (f" draining_to={row['draining_to'] or 'none'}" if row["state"] == "draining" else ""))


def cmd_pin(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        machine = resolve_machine(conn, args.machine)
        row = pinning.pin(conn, machine["id"], args.reason, ACTOR, None)
    print(f"{row['id']} pinned: {row['pinned_reason']}")


def cmd_unpin(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        machine = resolve_machine(conn, args.machine)
        phrase = f"UNPIN {machine['name']}"
        confirm = args.confirm if args.confirm is not None else input(f"Type {phrase} to unpin {machine['id']}: ")
        pinning.unpin(conn, machine["id"], confirm, ACTOR, None)
    print(f"{machine['id']} unpinned")


def cmd_secret_set(config: Config, args: argparse.Namespace) -> None:
    value = sys.stdin.read()
    if value.endswith("\n"):
        value = value[:-1]
    with db.connect(config.database_url) as conn:
        row = wl_secrets.set_secret(conn, args.workload, args.name, value, ACTOR, None)
    print(f"{args.workload}/{row['name']} set ({row['scope']})")


def _summary(row: dict[str, Any]) -> str:
    payload = row["payload"] or {}
    if row["kind"] == "email":
        return f"email to {payload.get('to')}: {payload.get('subject')}"
    return row["kind"] + " " + json.dumps(payload, sort_keys=True)[:80]


def cmd_outbound(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        if args.approve:
            row = outbound.approve(conn, args.approve, ACTOR, None)
            print(f"action {row['id']} approved")
            return
        if args.reject:
            row = outbound.reject(conn, args.reject, ACTOR, None, args.reason or "")
            print(f"action {row['id']} rejected")
            return
        rows = conn.execute(
            "SELECT * FROM outbound_actions WHERE status = %s ORDER BY created_at LIMIT 200", (args.status,)
        ).fetchall()
    for row in rows:
        row["summary"] = _summary(row)
    _table(rows, ["id", "workload", "kind", "status", "created_at", "summary"])


def cmd_send_job(config: Config, args: argparse.Namespace) -> None:
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as exc:
        raise BadRequest(f"--params is not valid JSON: {exc}") from None
    with db.connect(config.database_url) as conn:
        target = resolve_machine(conn, args.target)["id"] if args.target else None
        job = queue.create_job(conn, workload=args.workload, kind=args.kind, params=params, target=target,
                               idempotency_key=args.idempotency_key)
        if job["_created"]:
            add_audit(conn, "workload_job_create", str(job["id"]), ACTOR, None,
                      {"workload": args.workload, "kind": args.kind, "target": target})
    verb = "created" if job["_created"] else "existing"
    print(f"{verb} job {job['id']} workload={job['workload']} kind={job['kind']} status={job['status']} target={target or '-'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m host.cli", description="fleet workloads operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("workloads-sync", help="read workloads/*/workload.toml into the database").set_defaults(func=cmd_sync)
    p = sub.add_parser("workload-image", help="record a published image digest")
    p.add_argument("name")
    p.add_argument("digest", help="sha256:<64 hex>")
    p.add_argument("--size-mb", type=int, default=None)
    p.set_defaults(func=cmd_image)
    sub.add_parser("machine-enroll-token", help="mint a single-use machine enroll token").set_defaults(func=cmd_enroll_token)
    sub.add_parser("machines", help="list machines and what they run").set_defaults(func=cmd_machines)
    for name in ("assign", "machine-assign"):
        p = sub.add_parser(name, help="assign a workload to a machine (or none)")
        p.add_argument("machine", help="machine id or name")
        p.add_argument("workload", help="workload name or none")
        p.set_defaults(func=cmd_assign)
    p = sub.add_parser("pin", help="pin a machine so it cannot be reassigned")
    p.add_argument("machine")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_pin)
    p = sub.add_parser("unpin", help="unpin a machine (prompts for the typed phrase)")
    p.add_argument("machine")
    p.add_argument("--confirm", default=None, help="the phrase, instead of the prompt")
    p.set_defaults(func=cmd_unpin)
    p = sub.add_parser("secret-set", help="store a workload secret (value from stdin)")
    p.add_argument("workload")
    p.add_argument("name")
    p.set_defaults(func=cmd_secret_set)
    p = sub.add_parser("outbound", help="list pending outbound actions, or approve or reject one")
    p.add_argument("--status", default="pending")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--approve", metavar="ID", default=None)
    g.add_argument("--reject", metavar="ID", default=None)
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_outbound)
    p = sub.add_parser("wl-send-job", help="queue a workload job")
    p.add_argument("workload")
    p.add_argument("kind")
    p.add_argument("--params", default=None, help="JSON object")
    p.add_argument("--target", default=None, help="machine id or name")
    p.add_argument("--idempotency-key", default=None)
    p.set_defaults(func=cmd_send_job)
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
