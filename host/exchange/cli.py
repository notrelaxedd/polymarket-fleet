"""Exchange operator commands (no HTTP): python -m host.exchange.cli <command>.

simulate-final <game_id> --home N --away M   set a final (sim source or FLEET_DEV) and settle
probe                                        the configured source's raw markets payload
run-once                                     every exchange task once
exchange-state                               the heartbeat row
exchange-smoke --confirm "SMOKE YYYY-MM-DD"  the smoke order (docs/LIVE.md), [--market ID] [--hold S]
cancel-all --direct                          live off, then list and cancel the open orders on the exchange, close the rows
probe-account                                the balance call's status and raw payload (key redacted)
auth-check                                   one auth probe written to exchange_state
probe-gamestate --event ID [--yahoo]         one game-state request: status, payload, parsed ([--url U])
probe-alpaca [--asset-class N] [--get PATH]  read-only Alpaca checks: keys, account, assets, quotes ([--raw])
ingest-stock-bars [--symbol S]               fetch the daily stock bars now (all stock_symbols, or the ones named)
stock-bars-status                            one line per symbol: bars stored, newest bar, last fetch, last error

The live commands load the credentials from the environment (exchange.env) the way
the exchange process does; the key and secret are never printed. A malformed secret
exits 1 with the loader's secret-free message instead of "no credentials".
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
    """(live gateway, credentials) from the environment and the settings row; exit 1
    naming the problem when the secret is present but malformed."""
    from host.exchange.credentials import CredentialsError
    from host.exchange.main import live_gateway_from_settings

    try:
        with db.connect(config.database_url) as conn:
            return live_gateway_from_settings(conn)
    except CredentialsError as exc:
        print(f"error: secret malformed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def cmd_exchange_smoke(config: Config, args: argparse.Namespace) -> None:
    gateway, creds = build_gateway(config)
    if creds is None:
        print("error: no credentials in the environment (POLYMARKET_US_API_KEY / _SECRET)", file=sys.stderr)
        raise SystemExit(1)
    if args.drive:
        # Driving from this process: measure the clock skew on a probe first, so the
        # gateway's skew guard applies to the smoke placement as it does in the loop.
        try:
            gateway.balance()
        except Exception as exc:  # noqa: BLE001 - the owner wants the error text
            print(f"error: auth probe failed before the smoke order: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
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
    off = result["live_off"]
    print(f"live off (was {'on' if off['was_on'] else 'off'}): assignments halted={len(off['assignments_halted'])} "
          f"approved rows cancelled={result['approved_cancelled']}")
    print_table(result["remote"], ["exchange_order_id", "client_order_id", "order_id", "cancelled", "attempts", "row_status", "error"])
    print(f"remote={len(result['remote'])} cancelled={sum(1 for r in result['remote'] if r['cancelled'])} "
          f"rows_closed={len(result['rows_closed'])} still_open={result['still_open']}")
    if result["left_for_exchange"]:
        print(f"left cancel_requested for the exchange process (never seen on the exchange): {', '.join(result['left_for_exchange'])}")
    if result["error"]:
        print(f"error: {result['error']}", file=sys.stderr)
        raise SystemExit(1)


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


def cmd_probe_gamestate(config: Config, args: argparse.Namespace) -> None:
    """Never raises: without a database the default ESPN template is used."""
    try:
        with db.connect(config.database_url) as conn:
            result = probe.probe_gamestate(conn, args.event, args.yahoo, args.url)
    except Exception as exc:  # noqa: BLE001 - the probe must work without the database
        result = probe.probe_gamestate(None, args.event, args.yahoo, args.url)
        result["database"] = f"not reachable ({exc.__class__.__name__}); settings defaults used"
    print_probe_gamestate(result)


def cmd_probe_alpaca(_: Config, args: argparse.Namespace) -> None:
    """Needs no database: reads ALPACA_* from the environment (exchange.env). Exits 1
    when the keys are missing, ALPACA_BASE_URL is not an Alpaca host, or the account
    call does not answer 200."""
    from host.exchange import alpaca_credentials, alpaca_probe

    try:
        creds = alpaca_credentials.load()
    except alpaca_credentials.AlpacaConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    if creds is None:
        print(f"error: no Alpaca keys in the environment ({alpaca_credentials.KEY_VAR} / "
              f"{alpaca_credentials.SECRET_VAR} in exchange.env)", file=sys.stderr)
        raise SystemExit(1)
    try:
        result = alpaca_probe.run(creds, args.asset_class, args.get, args.raw)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    _print(result)
    if result["account"]["status"] != 200:
        raise SystemExit(1)


def cmd_ingest_stock_bars(config: Config, args: argparse.Namespace) -> None:
    from host.exchange import stock_bars

    feed = stock_bars.StockFeed()
    with db.connect(config.database_url) as conn:
        result = feed.run(conn, utcnow(), force=True, only=args.symbol or None)
    _print(result)
    if result.get("error") or result.get("skipped"):
        raise SystemExit(1)


def cmd_stock_bars_status(config: Config, _: argparse.Namespace) -> None:
    from host.exchange import stock_bars

    with db.connect(config.database_url) as conn:
        rows = stock_bars.status(conn)
    print_table(rows, ["symbol", "bars_count", "bars_through", "fetched_at", "tradable", "last_error"])


def print_probe_gamestate(result: dict[str, Any]) -> None:
    for key in ("source", "event_id", "game_id", "url", "status", "error", "database"):
        if key in result:
            print(f"{key}: {result[key] if result[key] is not None else '-'}")
    print("--- payload (first 64 KiB) ---")
    print(result.get("payload") or "")
    print("--- parsed ---")
    parsed = result.get("parsed")
    print(parsed if isinstance(parsed, str) else json.dumps(jsonable(parsed), indent=2, default=str))


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
    p = sub.add_parser("probe-gamestate", help="one game-state request: HTTP status, payload and what the parser extracted")
    p.add_argument("--event", required=True, help="the ESPN event id (games.raw->>'espn')")
    p.add_argument("--yahoo", action="store_true", help="probe yahoo_pbp_url instead of the ESPN summary")
    p.add_argument("--url", default=None, help="a URL template to probe instead of the setting ({event_id} is filled in)")
    p.set_defaults(func=cmd_probe_gamestate)
    p = sub.add_parser("probe-alpaca", help="read-only Alpaca checks: keys, account, assets, event contracts, quotes")
    p.add_argument("--asset-class", action="append", default=[], help="another asset class name to try for event contracts")
    p.add_argument("--get", action="append", default=[], help="another GET path (/v2/... trading, data:/v2/... market data)")
    p.add_argument("--raw", action="store_true", help="print up to 64 KiB of each answer instead of 2 KiB")
    p.set_defaults(func=cmd_probe_alpaca)
    p = sub.add_parser("ingest-stock-bars", help="fetch the daily stock bars from Alpaca now")
    p.add_argument("--symbol", action="append", default=[], help="only this symbol (repeatable); default every stock_symbols entry")
    p.set_defaults(func=cmd_ingest_stock_bars)
    sub.add_parser("stock-bars-status", help="the daily bar feed per symbol").set_defaults(func=cmd_stock_bars_status)
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
