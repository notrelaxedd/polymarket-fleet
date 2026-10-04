"""Exchange operator commands (no HTTP): python -m host.exchange.cli <command>.

simulate-final <game_id> --home N --away M   set a final (sim source or FLEET_DEV) and settle
probe                                        the configured source's raw markets payload
run-once                                     every exchange task once
exchange-state                               the heartbeat row
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from host import db
from host.api.serialize import jsonable
from host.config import Config
from host.errors import QueueError
from host.exchange import probe, settle, state


def cmd_simulate_final(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        result = settle.simulate_final(conn, args.game_id, args.home, args.away, "cli")
    print(json.dumps(jsonable(result), indent=2))


def cmd_probe(config: Config, _: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        result = probe.probe_markets(conn)
    print(json.dumps(jsonable(result), indent=2))


def cmd_run_once(config: Config, _: argparse.Namespace) -> None:
    from host.exchange.main import run_once

    pool = db.make_pool(config.database_url, max_size=2)
    try:
        print(json.dumps(jsonable(run_once(pool)), indent=2, default=str))
    finally:
        pool.close()


def cmd_exchange_state(config: Config, _: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        print(json.dumps(jsonable(state.read_state(conn)), indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m host.exchange.cli", description="fleet exchange operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("simulate-final", help="set a final for a game (sim source or FLEET_DEV) and settle it")
    p.add_argument("game_id")
    p.add_argument("--home", type=int, required=True)
    p.add_argument("--away", type=int, required=True)
    p.set_defaults(func=cmd_simulate_final)
    sub.add_parser("probe", help="raw markets payload of the configured source").set_defaults(func=cmd_probe)
    sub.add_parser("run-once", help="run every exchange task once").set_defaults(func=cmd_run_once)
    sub.add_parser("exchange-state", help="the exchange heartbeat row").set_defaults(func=cmd_exchange_state)
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
