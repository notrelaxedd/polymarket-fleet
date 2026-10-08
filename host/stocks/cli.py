"""Operator CLI commands for stocks on Alpaca (contract section 2B), wired into host.cli:
stock-models, stock-assign, stock-assignments, stock-orders."""
from __future__ import annotations

import argparse
from typing import Any

from host import db
from host.config import Config
from host.money import cents_to_dollars, dollars_to_cents
from host.settings import get_int_setting, get_setting
from host.stocks import assignments, models, orders


def _table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    from host.cli import print_table  # deferred: host.cli imports this module

    print_table(rows, columns)


def _metric(metrics: Any, key: str) -> Any:
    return metrics.get(key) if isinstance(metrics, dict) else None


def cmd_stock_models(config: Config, args: argparse.Namespace) -> None:
    """Stock models, best backtest sharpe first."""
    with db.connect(config.database_url) as conn:
        rows = models.list_models(conn, args.status, args.limit)
    flat = []
    for r in rows:
        bt, val = r["backtest_metrics"], r["validation_metrics"]
        flat.append({"id": r["id"], "status": r["status"], "family": r["family"], "params": r["params"],
                     "sharpe": _metric(bt, "sharpe"), "drawdown": _metric(bt, "max_drawdown"), "cagr": _metric(bt, "cagr"),
                     "trades": _metric(bt, "trades"), "val_sharpe": _metric(val, "sharpe")})
    _table(flat, ["id", "status", "family", "sharpe", "drawdown", "cagr", "trades", "val_sharpe", "params"])


def cmd_stock_assign(config: Config, args: argparse.Namespace) -> None:
    """Create a stock assignment (bankroll in dollars, symbols comma separated)."""
    with db.connect(config.database_url) as conn:
        bankroll = get_int_setting(conn, "stock_default_bankroll_cents", 1_000_000)
        if args.bankroll:
            bankroll = dollars_to_cents(args.bankroll, "bankroll")
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else \
            list(get_setting(conn, "stock_symbols", ["SPY"]))
        row = assignments.create_assignment(conn, args.model_id, args.mode, bankroll, symbols, "cli")
    print(f"stock assignment {row['id']} model={row['model_id']} mode={row['mode']} symbols={','.join(row['symbols'])} "
          f"bankroll=${cents_to_dollars(row['bankroll_cents'])} job={row['job_id']}")


def cmd_stock_assignments(config: Config, args: argparse.Namespace) -> None:
    with db.connect(config.database_url) as conn:
        rows = assignments.list_assignments(conn, args.status)
    flat = [{"id": r["id"], "model": r["model_id"], "family": r["family"], "mode": r["mode"], "status": r["status"],
             "symbols": ",".join(r["symbols"]), "bankroll": cents_to_dollars(r["bankroll_cents"]),
             "cash": cents_to_dollars(r["cash_cents"]), "reserved": cents_to_dollars(r["reserved_cents"]),
             "realized": cents_to_dollars(r["realized_cents"]), "open_orders": r["open_orders"],
             "decided": r["last_decision_date"], "job": r["job_status"], "halt_reason": r["halt_reason"]} for r in rows]
    _table(flat, ["id", "model", "family", "mode", "status", "symbols", "bankroll", "cash", "reserved", "realized",
                  "open_orders", "decided", "job", "halt_reason"])


def cmd_stock_orders(config: Config, args: argparse.Namespace) -> None:
    status = args.status
    with db.connect(config.database_url) as conn:
        rows = conn.execute(
            f"""
            SELECT id, assignment_id, mode, session_date, symbol, side, qty, filled_qty, ref_price_cents, reserved_cents,
                   status, reason, created_at FROM stock_orders
             WHERE (%(status)s::text IS NULL OR status = %(status)s
                    OR (%(status)s = 'active' AND status IN ('{orders.ACTIVE_LIST}')))
             ORDER BY created_at DESC LIMIT %(limit)s
            """,
            {"status": status, "limit": max(1, min(args.limit, 1000))},
        ).fetchall()
    flat = [dict(r, id=str(r["id"])[:8], ref=cents_to_dollars(r["ref_price_cents"])) for r in rows]
    _table(flat, ["id", "assignment_id", "mode", "session_date", "symbol", "side", "qty", "filled_qty", "ref", "status",
                  "reason", "created_at"])


def add_parsers(sub: Any) -> None:
    """Register the stock subcommands on host.cli's subparsers."""
    p = sub.add_parser("stock-models", help="list stock models, best backtest sharpe first")
    p.add_argument("--status", default=None, choices=("candidate", "paper_ok", "live_eligible", "retired"))
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_stock_models)
    p = sub.add_parser("stock-assign", help="create a stock assignment: model id, mode, bankroll dollars, symbols")
    p.add_argument("model_id", type=int)
    p.add_argument("--mode", default="paper", choices=assignments.MODES)
    p.add_argument("--bankroll", default=None, help="dollars (default: stock_default_bankroll_cents)")
    p.add_argument("--symbols", default=None, help="comma separated (default: the stock_symbols setting)")
    p.set_defaults(func=cmd_stock_assign)
    p = sub.add_parser("stock-assignments", help="list stock assignments")
    p.add_argument("--status", default=None, choices=("active", "halted", "closed"))
    p.set_defaults(func=cmd_stock_assignments)
    p = sub.add_parser("stock-orders", help="list stock orders, newest first")
    p.add_argument("--status", default=None, help="one status, or active")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_stock_orders)
