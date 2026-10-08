"""Operator commands for stocks on Alpaca (contract section 6), registered in
host/exchange/cli.py:

stock-smoke --confirm "STOCK SMOKE YYYY-MM-DD" [--hold S]
    one 1-share SPY market-on-close buy on the keys' environment, cancelled after
    --hold seconds (10). It proves the keys, placement and cancellation without any
    assignment or bankroll. Refused under kill, on live keys while live trading is off,
    and from 15:45 New York (a cls order cannot be cancelled after 15:50). Its
    client_order_id starts with "fleet-smoke-", which the reconciliation knows as ours
    for 15 minutes (older, it is an unknown order: live auto-kills). A place that times
    out or fails is looked up by client_order_id (never placed again); a cancel that
    fails is retried (1, 2, 4, 8 s); the result is audited on every path, and anything
    but "canceled" at Alpaca exits 1 with "check the account now".
stock-cancel-all [--direct]
    without --direct: the database cancel (approved rows cancelled with the release,
    the rest cancel_requested for the exchange process). With --direct, the fallback
    for "exchange process down with orders resting": first the live gate (live keys:
    host.kill.live_off; paper: the paper stock orders cancelled and paper assignments
    halted), then every open order on the Alpaca account is cancelled with retries
    (1, 2, 4, 8 s), then our rows are closed from what Alpaca says (fills first). A row
    whose place may still be in flight stays cancel_requested for the exchange process.

The keys come from exchange.env as in the exchange process; they are never printed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, time as dtime, timedelta
from typing import Any, Callable

import psycopg

from host import db, kill
from host.api.serialize import jsonable
from host.config import Config
from host.errors import BadRequest, Conflict
from host.events import add_audit
from host.exchange import alpaca_credentials, stock_tasks
from host.exchange.adapters.base import utcnow
from host.exchange.alpaca_trading import AlpacaRejected
from host.exchange.stock_broker import NEW_YORK, SMOKE_PREFIX, parse_ts
from host.exchange.stock_executor import REFUSED
from host.exchange.stock_rows import ACTIVE, apply_remote, set_status
from host.settings import get_setting
from host.stocks import orders as stock_orders
from host.trading.positions import owner_tz

PHRASE_PREFIX = "STOCK SMOKE "
SMOKE_SYMBOL = "SPY"
SMOKE_CUTOFF = dtime(15, 45)
SMOKE_BEFORE_CLOSE = timedelta(minutes=15)
DIRECT_RETRIES = (1.0, 2.0, 4.0, 8.0)
DIRECT_REASON = "stock-cancel-all --direct"


def expected_phrase(conn: psycopg.Connection, now: datetime) -> str:
    return PHRASE_PREFIX + now.astimezone(owner_tz(conn)).date().isoformat()


def smoke_problem(conn: psycopg.Connection, client: Any, confirm: str, now: datetime, clock: dict[str, Any]) -> None:
    """400 on a wrong phrase; 409 under kill, on live keys with live off, or too late."""
    phrase = expected_phrase(conn, now)
    if confirm != phrase:
        raise BadRequest(f'confirmation must be exactly "{phrase}"')
    if kill.is_killed(conn):
        raise Conflict("the kill switch is on")
    if client.environment == "live" and get_setting(conn, "live_enabled", False) is not True:
        raise Conflict("these are live keys and live trading is off (enable it first)")
    close = parse_ts(clock.get("next_close"))
    late = clock.get("is_open") and close is not None and now >= close - SMOKE_BEFORE_CLOSE
    if now.astimezone(NEW_YORK).time() >= SMOKE_CUTOFF or late:
        raise Conflict("too late: from 15:45 New York a market-on-close order may not be cancellable in time")


def run_stock_smoke(conn: psycopg.Connection, client: Any, confirm: str, hold_s: float, now: datetime,
                    sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Place, hold and cancel the smoke order; {"status", "exchange_order_id", "timeline"}."""
    smoke_problem(conn, client, confirm, now, client.clock())
    client_id = f"{SMOKE_PREFIX}{uuid.uuid4().hex[:20]}"
    add_audit(conn, "stock_smoke", client_id, "cli", None, {"environment": client.environment, "symbol": SMOKE_SYMBOL},
              confirmation_text=confirm)
    conn.commit()
    timeline: list[dict[str, Any]] = []
    eid: str | None = None
    status: str | None = "unknown"
    try:
        eid = _smoke_place(client, client_id, timeline, sleep)
        if eid is None:
            status = "rejected" if timeline[-1]["step"] == "rejected" else "unknown"
            return {"client_order_id": client_id, "exchange_order_id": None, "status": status, "timeline": timeline}
        sleep(hold_s)
        _smoke_cancel(client, eid, timeline, sleep)
        for delay in (0.0, 0.5, 1.0, 2.0):
            sleep(delay)
            try:
                status = (client.order(eid) or {}).get("status")
            except Exception as exc:  # noqa: BLE001 - asked again, reported
                status = "unknown"
                timeline.append({"ts": utcnow(), "step": "status read failed", "detail": str(exc)[:200]})
            if status in ("canceled", "filled", "expired", "rejected"):
                break
        timeline.append({"ts": utcnow(), "step": f"status at Alpaca: {status}", "detail": eid})
        return {"client_order_id": client_id, "exchange_order_id": eid, "status": status, "timeline": timeline}
    finally:
        add_audit(conn, "stock_smoke_result", client_id, "cli", None, {"status": status, "exchange_order_id": eid})
        conn.commit()


def _smoke_place(client: Any, client_id: str, timeline: list[dict[str, Any]], sleep: Callable[[float], None]) -> str | None:
    """Place the smoke order; on a timeout or failure look it up by client_order_id
    (never placed again). The exchange order id, None when it does not exist."""
    try:
        eid = client.place({"id": client_id, "symbol": SMOKE_SYMBOL, "qty": 1, "side": "buy"})
        timeline.append({"ts": utcnow(), "step": "placed", "detail": eid})
        return eid
    except AlpacaRejected as exc:
        timeline.append({"ts": utcnow(), "step": "rejected", "detail": str(exc)[:200]})
        return None
    except Exception as exc:  # noqa: BLE001 - the order may exist: looked up below
        timeline.append({"ts": utcnow(), "step": "place failed, looking it up", "detail": str(exc)[:200]})
    for delay in (0.0,) + DIRECT_RETRIES:
        sleep(delay)
        try:
            remote = client.order_by_client_id(client_id)
        except Exception as exc:  # noqa: BLE001 - asked again
            timeline.append({"ts": utcnow(), "step": "lookup failed", "detail": str(exc)[:200]})
            continue
        if remote is not None:
            timeline.append({"ts": utcnow(), "step": "found at Alpaca", "detail": remote.get("id")})
            return str(remote.get("id"))
    timeline.append({"ts": utcnow(), "step": "not found at Alpaca after the place failed", "detail": client_id})
    return None


def _smoke_cancel(client: Any, eid: str, timeline: list[dict[str, Any]], sleep: Callable[[float], None]) -> None:
    for delay in (0.0,) + DIRECT_RETRIES:
        if delay:
            sleep(delay)
        try:
            accepted = client.cancel(eid)
        except Exception as exc:  # noqa: BLE001 - retried
            timeline.append({"ts": utcnow(), "step": "cancel failed", "detail": str(exc)[:200]})
            continue
        timeline.append({"ts": utcnow(), "step": "cancel accepted" if accepted else "cancel refused", "detail": eid})
        return


def cancel_all_direct(conn: psycopg.Connection, client: Any, now: datetime, sleep: Callable[[float], None],
                      actor: str = "cli") -> dict[str, Any]:
    """See the module doc; the summary printed by the command."""
    mode = client.environment
    if mode == "live":
        gate = kill.live_off(conn, actor, DIRECT_REASON)
    else:
        kill.approval_lock(conn, mode)
        gate = kill.stock_effects(conn, actor, DIRECT_REASON, mode)
    conn.commit()
    results: list[dict[str, Any]] = []
    for item in client.orders("open"):
        entry = {"exchange_order_id": item.get("id"), "client_order_id": item.get("client_order_id"),
                 "symbol": item.get("symbol"), "cancelled": False, "attempts": 0, "error": None}
        for delay in (0.0,) + DIRECT_RETRIES:
            if delay:
                sleep(delay)
            entry["attempts"] += 1
            try:
                if client.cancel(str(item.get("id"))):
                    entry["cancelled"] = True
                    break
                entry["error"] = REFUSED
                break
            except Exception as exc:  # noqa: BLE001 - reported per order
                entry["error"] = str(exc)[:200]
        results.append(entry)
    rows = conn.execute("SELECT * FROM stock_orders WHERE mode = %s AND status = ANY(%s) ORDER BY created_at",
                        (mode, list(ACTIVE))).fetchall()
    refused = {str(e["exchange_order_id"]) for e in results if e["error"] == REFUSED}
    closed: dict[str, str] = {}
    left: list[str] = []
    for row in rows:
        remote = client.order_by_client_id(str(row["id"]))
        if remote is None:
            left.append(str(row["id"]))
            continue
        if row["exchange_order_id"] is None:
            conn.execute("UPDATE stock_orders SET exchange_order_id = %s WHERE id = %s", (str(remote.get("id")), row["id"]))
        status = apply_remote(conn, row["id"], remote, now)
        if status == "cancel_requested" and str(remote.get("id")) in refused:
            current = conn.execute("SELECT filled_qty FROM stock_orders WHERE id = %s", (row["id"],)).fetchone()
            set_status(conn, row["id"], "partial" if current["filled_qty"] else "open", ("cancel_requested",),
                       {"reason": REFUSED}, reason=REFUSED)
        closed[str(row["id"])] = conn.execute("SELECT status FROM stock_orders WHERE id = %s", (row["id"],)).fetchone()["status"]
        conn.commit()
    summary = {"direct": True, "environment": mode, "remote": len(results), "cancelled": sum(e["cancelled"] for e in results),
               "rows": closed, "left_for_exchange": left}
    add_audit(conn, "cancel_all", f"stocks:{mode}", actor, None, summary)
    return {**summary, "remote_orders": results, "gate": gate}


# ------------------------------------------------------------------ commands

def _print(value: Any) -> None:
    print(json.dumps(jsonable(value), indent=2, default=str))


def _client() -> Any:
    try:
        client = stock_tasks.default_client()
    except alpaca_credentials.AlpacaConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    if client is None:
        print(f"error: no Alpaca keys in the environment ({alpaca_credentials.KEY_VAR} / "
              f"{alpaca_credentials.SECRET_VAR} in exchange.env)", file=sys.stderr)
        raise SystemExit(1)
    return client


def cmd_stock_smoke(config: Config, args: argparse.Namespace) -> None:
    client = _client()
    with db.connect(config.database_url) as conn:
        result = run_stock_smoke(conn, client, args.confirm, args.hold, utcnow())
    _print(result)
    if result["status"] != "canceled":
        print(f"error: the smoke order is {result['status']} at Alpaca, not canceled: check the account now", file=sys.stderr)
        raise SystemExit(1)


def cmd_stock_cancel_all(config: Config, args: argparse.Namespace) -> None:
    if not args.direct:
        with db.connect(config.database_url) as conn:
            kill.approval_locks(conn)
            result = stock_orders.cancel_orders(conn, "cli", "stock-cancel-all")
            add_audit(conn, "cancel_all", "stocks", "cli", None, {"cancelled": len(result["cancelled"]),
                                                                  "requested": len(result["requested"])})
        print(f"cancelled={len(result['cancelled'])} requested={len(result['requested'])}")
        return
    client = _client()
    with db.connect(config.database_url) as conn:
        result = cancel_all_direct(conn, client, utcnow(), time.sleep)
    _print({k: v for k, v in result.items() if k != "gate"})
    if any(e["error"] for e in result["remote_orders"]) or result["left_for_exchange"]:
        raise SystemExit(1)


def add_commands(sub: Any) -> None:
    p = sub.add_parser("stock-smoke", help='one 1-share SPY cls buy, cancelled: --confirm "STOCK SMOKE YYYY-MM-DD"')
    p.add_argument("--confirm", required=True)
    p.add_argument("--hold", type=float, default=10.0, help="seconds before the cancel (default 10)")
    p.set_defaults(func=cmd_stock_smoke)
    p = sub.add_parser("stock-cancel-all", help="cancel every active stock order (database), or --direct at Alpaca")
    p.add_argument("--direct", action="store_true", help="cancel every open order on the Alpaca account and close our rows")
    p.set_defaults(func=cmd_stock_cancel_all)
