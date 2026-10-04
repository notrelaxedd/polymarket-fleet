"""Exchange operator commands (no HTTP): python -m host.exchange.cli <command>.

simulate-final <game_id> --home N --away M   set a final (sim source or FLEET_DEV) and settle
probe                                        the configured source's raw markets payload
run-once                                     every exchange task once
exchange-state                               the heartbeat row
exchange-smoke --confirm "SMOKE YYYY-MM-DD"  the smoke order (docs/LIVE.md), [--market ID] [--hold S]
cancel-all --direct                          list and cancel the open orders on the exchange, close the rows
probe-account                                the balance call's status and raw payload (key redacted)
auth-check                                   one auth probe written to exchange_state

The live commands load the credentials from the environment (exchange.env) the way
the exchange process does; the key and secret are never printed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Sequence

from host import db
from host.api.serialize import jsonable
from host.config import Config
from host.errors import QueueError
from host.exchange import live_sync, probe, settle, smoke, state
from host.exchange.adapters.base import utcnow


def _print(value: Any) -> None:
    print(json.dumps(jsonable(value), indent=2, default=str))


def print_table(rows: list[dict[str, Any]], columns: Sequence[str]) -> None:
    """Fixed-width columns, header first (empty rows print only the header)."""
    cells = [[str(r.get(c, "") if r.get(c) is not None else "") for c in columns] for r in rows]
    widths = [max([len(c)] + [len(row[i]) for row in cells]) for i, c in enumerate(columns)]
    print("  ".join(c.ljust(widths[i]) for i, c in enumerate(columns)))
    for row in cells:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def cmd_simulate_final(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        result = settle.simulate_final(conn, args.game_id, args.home, args.away, "cli")
    _print(result)


def cmd_probe(config: Config, _: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        result = probe.probe_markets(conn)
    _print(result)


def cmd_run_once(config: Config, _: argparse.Namespace) -> None:
    from host.exchange.main import run_once

    pool = db.make_pool(config.database_url, max_size=2)
    try:
        _print(run_once(pool))
    finally:
        pool.close()


def cmd_exchange_state(config: Config, _: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        _print(state.read_state(conn))


def build_gateway(config: Config) -> tuple[Any, Any]:
    """(live gateway, credentials) from the environment and the settings row."""
    from host.exchange.main import live_gateway_from_settings

    with db.connect(config.database_url) as conn:
        return live_gateway_from_settings(conn)


def cmd_exchange_smoke(config: Config, args: argparse.Namespace) -> None:
    gateway, creds = build_gateway(config)
    if creds is None:
        print("error: no credentials in the environment (POLYMARKET_US_API_KEY / _SECRET)", file=sys.stderr)
        raise SystemExit(1)
    pool = db.make_pool(config.database_url, max_size=2)
    try:
        result = smoke.run_smoke(pool, args.confirm, args.market, args.hold, gateway=gateway if args.drive else None)
    finally:
        pool.close()
    print(f"smoke order {result['order_id']}: {result['status']} price={result['price']} size={result['size']}"
          f" exchange_order_id={result['exchange_order_id']}")
    print_table([{"ts": e["ts"], "status": e["to_status"], "actor": e["actor"], "detail": json.dumps(jsonable(e["detail"]))} for e in result["timeline"]],
                ["ts", "status", "actor", "detail"])
    if result["status"] != "cancelled":
        raise SystemExit(1)


def cmd_cancel_all(config: Config, args: argparse.Namespace) -> None:
    if not args.direct:
        from host import kill

        with db.connect(config.database_url) as conn:
            result = kill.cancel_all(conn, "cli", args.mode)
        print(f"cancelled={result['cancelled']} requested={result['requested']}")
        return
    gateway, creds = build_gateway(config)
    if creds is None:
        print("error: no credentials in the environment (POLYMARKET_US_API_KEY / _SECRET)", file=sys.stderr)
        raise SystemExit(1)
    with db.connect(config.database_url) as conn:
        result = live_sync.cancel_all_direct(conn, gateway, utcnow(), time.sleep, "cli")
    print_table(result["remote"], ["exchange_order_id", "client_order_id", "order_id", "cancelled", "attempts", "row_status", "error"])
    print(f"remote={len(result['remote'])} cancelled={sum(1 for r in result['remote'] if r['cancelled'])} "
          f"rows_closed={len(result['rows_closed'])} still_open={result['still_open']}")


def cmd_probe_account(config: Config, _: argparse.Namespace) -> None:
    gateway, creds = build_gateway(config)
    if creds is None:
        _print({"status": None, "payload": None, "key_present": False, "key_hint": None, "error": "no credentials in the environment"})
        return
    try:
        result = gateway.probe_account()
    except Exception as exc:  # noqa: BLE001 - the owner wants the error text
        result = {"status": None, "payload": None, "key_hint": getattr(creds, "key_hint", None), "error": str(exc)}
    _print({"key_present": True, **result})


def cmd_auth_check(config: Config, _: argparse.Namespace) -> None:
    gateway, creds = build_gateway(config)
    with db.connect(config.database_url) as conn:
        result = live_sync.auth_check(conn, gateway, utcnow(), creds is not None)
    _print(result)


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
    p = sub.add_parser("exchange-smoke", help='the smoke order: --confirm "SMOKE YYYY-MM-DD"')
    p.add_argument("--confirm", required=True)
    p.add_argument("--market", default=None, help="market id (default: the most liquid live-tradable market)")
    p.add_argument("--hold", type=int, default=None, help="seconds to hold before cancelling (default: smoke_hold_seconds)")
    p.add_argument("--drive", action="store_true", help="submit and cancel from this process (exchange service stopped)")
    p.set_defaults(func=cmd_exchange_smoke)
    p = sub.add_parser("cancel-all", help="cancel every active order (database), or --direct on the exchange")
    p.add_argument("--mode", choices=("paper", "live"), default=None)
    p.add_argument("--direct", action="store_true", help="list and cancel on the exchange with the credentials")
    p.set_defaults(func=cmd_cancel_all)
    sub.add_parser("probe-account", help="the balance call's status and raw payload (key redacted)").set_defaults(func=cmd_probe_account)
    sub.add_parser("auth-check", help="one auth probe written to exchange_state").set_defaults(func=cmd_auth_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.from_env()
    try:
        args.func(config, args)
    except QueueError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
