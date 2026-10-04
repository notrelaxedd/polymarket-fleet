"""Live reconciliation tasks of the exchange loop (docs/LIVE.md "Authentication probe
and auto-kill", "Executor live path"): the auth probe, the fills poll, the open-order
audit, the startup reconciliation, the reconciliation of `submitting` rows and the
direct cancel-all. Every function takes the gateway it should talk to and `now`; the
auto-kill triggers go through `host.kill.auto_kill` and fire only while the kill
switch is off (a persisting condition kills again after RESUME, nothing spams the
audit log while killed).

Money rule: a live row is closed (its reservation released) only after its fills were
read. When the fills call fails (`fetch_fills` returns None) every closing path leaves
the row active for that pass and reports the error; a fill that still turns up for a
row already closed auto-kills `late_fill`, because the ledger can no longer book it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

import psycopg

from host import kill
from host.errors import Conflict
from host.events import add_audit
from host.exchange import state
from host.exchange.adapters.base import OrderGateway
from host.settings import get_int_setting, get_setting
from host.trading import orders

log = logging.getLogger(__name__)

ACTOR = "exchange"
FILLS_LOOKBACK = timedelta(seconds=60)
CLOSED_TAIL = timedelta(seconds=120)
LIVE_ACTIVE = ("submitting", "open", "partial", "cancel_requested")
DIRECT_RETRIES = (1.0, 2.0, 4.0, 8.0)
DEFAULT_SKEW_LIMIT_MS = 30_000
DIRECT_REASON = "cancel-all --direct"


# ----------------------------------------------------------------- helpers

def _int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def skew_limit_ms(conn: psycopg.Connection) -> int:
    """settings.auto_kill.clock_skew_ms (30 000 when unset or malformed)."""
    limits = get_setting(conn, "auto_kill", None) or {}
    return _int(limits.get("clock_skew_ms") if isinstance(limits, dict) else None) or DEFAULT_SKEW_LIMIT_MS


def remote_ids(remote: dict[str, Any]) -> tuple[str | None, str | None]:
    """(client id, exchange id) of a remote order or fill dict, tolerant of the
    older key names."""
    client = remote.get("client_order_id") or remote.get("client_request_id")
    exchange = remote.get("exchange_order_id") or remote.get("id") or remote.get("order_id")
    return _text(client), _text(exchange)


def index_remote(remote: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Remote orders by client id (a list, so duplicates are visible) and by exchange id."""
    by_client: dict[str, list[dict[str, Any]]] = {}
    by_exchange: dict[str, dict[str, Any]] = {}
    for item in remote:
        client, exchange = remote_ids(item)
        if client is not None:
            by_client.setdefault(client, []).append(item)
        if exchange is not None:
            by_exchange[exchange] = item
    return by_client, by_exchange


def listed(row: dict[str, Any], remote: list[dict[str, Any]]) -> bool:
    """Is this order row still among the remote open orders (by either id)?"""
    by_client, by_exchange = index_remote(remote)
    if row["client_request_id"] in by_client:
        return True
    return row.get("exchange_order_id") is not None and str(row["exchange_order_id"]) in by_exchange


def active_live_orders(conn: psycopg.Connection, statuses: tuple[str, ...] = LIVE_ACTIVE, for_update: bool = False) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM orders WHERE mode = 'live' AND status = ANY(%s) ORDER BY created_at" + (" FOR UPDATE SKIP LOCKED" if for_update else ""),
        (list(statuses),),
    ).fetchall()
    return [dict(r) for r in rows]


def recently_closed_live_orders(conn: psycopg.Connection, now: datetime) -> list[dict[str, Any]]:
    """Live rows that became terminal within CLOSED_TAIL: the fills poll keeps
    looking for their last fills a while after the last active order is gone."""
    rows = conn.execute(
        "SELECT * FROM orders WHERE mode = 'live' AND status = ANY(%s) AND updated_at >= %s ORDER BY created_at",
        (list(orders.TERMINAL_STATUSES), now - CLOSED_TAIL),
    ).fetchall()
    return [dict(r) for r in rows]


def maybe_auto_kill(conn: psycopg.Connection, reason: str, detail: dict[str, Any]) -> bool:
    """auto_kill unless the fleet is already killed (then the condition is logged only)."""
    if kill.is_killed(conn):
        log.warning("auto-kill condition %s while already killed: %s", reason, detail)
        return False
    log.error("AUTO-KILL %s: %s", reason, detail)
    kill.auto_kill(conn, reason, detail)
    return True


def fills_since(rows: list[dict[str, Any]], now: datetime) -> datetime:
    """The earliest submission among `rows` minus the lookback (now minus it when none)."""
    stamps = [r["submitted_at"] for r in rows if r.get("submitted_at") is not None]
    return (min(stamps) if stamps else now) - FILLS_LOOKBACK


def fetch_fills(gateway: OrderGateway, since: datetime) -> list[dict[str, Any]] | None:
    """fills(since) as a list of dicts; None (logged) when the call failed, which no
    caller may read as "no fills"."""
    try:
        return [f for f in gateway.fills(since) if isinstance(f, dict)]
    except Exception as exc:  # noqa: BLE001 - the next pass retries
        log.warning("fills(since=%s) failed: %s", since, exc)
        return None


def _order_for_fill(conn: psycopg.Connection, fill: dict[str, Any]) -> dict[str, Any] | None:
    client, exchange = remote_ids(fill)
    if client is not None:
        row = conn.execute("SELECT * FROM orders WHERE client_request_id = %s AND mode = 'live'", (client,)).fetchone()
        if row is not None:
            return dict(row)
    if exchange is not None:
        row = conn.execute("SELECT * FROM orders WHERE exchange_order_id = %s AND mode = 'live'", (exchange,)).fetchone()
        if row is not None:
            return dict(row)
    return None


def record_fills(
    conn: psycopg.Connection, fills: list[dict[str, Any]], only: set[str] | None = None, actor: str = ACTOR,
) -> dict[str, Any]:
    """Record remote fills through orders.record_fill (idempotent on exchange_fill_id).
    `only` restricts to those order ids. Returns {"recorded", "unknown": [fills whose
    order we do not have], "late": [fill ids the ledger could not book]}. A fill the
    ledger cannot book (its order is already closed, or the size exceeds what was
    open) is real money the books do not show: auto-kill `late_fill`."""
    recorded, unknown, late = 0, [], []
    for fill in fills:
        fill_id = _text(fill.get("exchange_fill_id") or fill.get("id"))
        row = _order_for_fill(conn, fill)
        if row is None:
            unknown.append(fill)
            continue
        if only is not None and str(row["id"]) not in only:
            continue
        if fill_id is not None and conn.execute("SELECT 1 FROM fills WHERE exchange_fill_id = %s", (fill_id,)).fetchone():
            continue
        size, price = _int(fill.get("size")), fill.get("price")
        if not size or size <= 0 or price is None:
            log.warning("fill %s without size or price: %s", fill_id, fill)
            continue
        try:
            orders.record_fill(conn, row["id"], float(price), size, _int(fill.get("fee_cents")) or 0, "live", actor, exchange_fill_id=fill_id)
            recorded += 1
        except Conflict as exc:
            log.error("fill %s cannot be applied to order %s: %s", fill_id, row["id"], exc)
            late.append(fill_id)
            maybe_auto_kill(conn, "late_fill", {
                "order_id": str(row["id"]), "status": row["status"], "fill_id": fill_id, "size": size,
                "price": _text(price), "error": str(exc)[:200],
            })
    return {"recorded": recorded, "unknown": unknown, "late": late}


# ------------------------------------------------------------------- auth

def auth_check(
    conn: psycopg.Connection, gateway: OrderGateway, now: datetime, creds_present: bool, creds_error: str | None = None,
) -> dict[str, Any]:
    """The auth probe: balance() -> exchange_state (auth_ok, balances, skew, failures).
    Auto-kill on `auto_kill.auth_failures` consecutive failures and on a clock skew
    over `auto_kill.clock_skew_ms`; `skew_over_limit` tells the loop to stop placing.
    `creds_error` is the loader's (secret-free) message for a malformed secret."""
    limits = get_setting(conn, "auto_kill", None) or {}
    max_failures = _int(limits.get("auth_failures")) or 3
    skew_limit = skew_limit_ms(conn)
    out: dict[str, Any] = {"auth_ok": False, "credentials_present": creds_present, "skew_ms": None, "skew_over_limit": False,
                           "auto_killed": None, "error": None, "failures": 0}
    if not creds_present:
        state.set_credentials_present(conn, False, creds_error)
        out["error"] = creds_error
        return out
    try:
        payload = gateway.balance()
    except Exception as exc:  # noqa: BLE001 - any failure counts
        failures = state.record_auth_failure(conn, now, str(exc) or exc.__class__.__name__)
        out.update({"error": f"auth: {exc}", "failures": failures})
        if failures >= max_failures and maybe_auto_kill(conn, "auth_failures", {"failures": failures, "last_error": str(exc)[:200]}):
            out["auto_killed"] = "auth_failures"
        return out
    skew = _int(getattr(gateway, "last_skew_ms", None))
    payload = payload if isinstance(payload, dict) else {}
    state.record_auth_ok(conn, now, _int(payload.get("balance_cents")), _int(payload.get("buying_power_cents")), skew)
    out.update({"auth_ok": True, "skew_ms": skew, "balance_cents": _int(payload.get("balance_cents")),
                "buying_power_cents": _int(payload.get("buying_power_cents"))})
    if skew is not None and abs(skew) > skew_limit:
        out["skew_over_limit"] = True
        out["error"] = f"clock skew {skew} ms over the {skew_limit} ms limit"
        if maybe_auto_kill(conn, "clock_skew", {"skew_ms": skew, "limit_ms": skew_limit}):
            out["auto_killed"] = "clock_skew"
    return out


# ------------------------------------------------------------------ fills

def poll_fills(conn: psycopg.Connection, gateway: OrderGateway, now: datetime) -> int:
    """fills(since the earliest submission - 60 s) -> record_fill, while live orders
    are active and for CLOSED_TAIL after the last one closed (a fill that lands as
    the row closes is still seen); a fill for an order we do not know auto-kills
    `unknown_fill`. Returns the number of fills recorded."""
    rows = active_live_orders(conn) or recently_closed_live_orders(conn, now)
    if not rows:
        return 0
    fills = fetch_fills(gateway, fills_since(rows, now))
    if fills is None:
        return 0
    result = record_fills(conn, fills)
    if result["unknown"]:
        first = result["unknown"][0]
        maybe_auto_kill(conn, "unknown_fill", {"fill": {k: _text(v) for k, v in first.items()}, "count": len(result["unknown"])})
    return int(result["recorded"])


# -------------------------------------------------------------- open orders

def closed_status(row: dict[str, Any], now: datetime) -> str:
    """`expired` when the row's gtd_at has passed, else `cancelled`."""
    return "expired" if row.get("gtd_at") is not None and row["gtd_at"] <= now else "cancelled"


def confirm_gone(conn: psycopg.Connection, order_id: Any, now: datetime, actor: str, detail: dict[str, Any] | None = None) -> str:
    """A cancel_requested row the exchange no longer lists, fills absorbed: release
    the rest and close it as expired (past gtd) or cancelled."""
    current = orders.get_order(conn, order_id, for_update=True)
    if current["status"] != "cancel_requested":
        return current["status"]
    to_status = closed_status(current, now)
    orders.confirm_cancelled(conn, order_id, actor, detail, to_status=to_status)
    return to_status


def close_missing(conn: psycopg.Connection, row: dict[str, Any], now: datetime, actor: str = ACTOR) -> str:
    """An order of ours the exchange no longer lists, fills already absorbed: filled
    stays filled; cancel_requested -> cancelled (expired past gtd); past gtd ->
    expired; else cancelled (the exchange dropped it). The unfilled reservation is
    released."""
    current = orders.get_order(conn, row["id"], for_update=True)
    if current["status"] not in LIVE_ACTIVE:
        return current["status"]
    if current["status"] == "cancel_requested":
        return confirm_gone(conn, current["id"], now, actor, {"reason": "not listed on the exchange"})
    orders.release_unfilled(conn, current, note="missing on the exchange")
    to_status = closed_status(current, now)
    orders.set_status(conn, current["id"], to_status, actor, {"reason": "not listed on the exchange"}, expected=LIVE_ACTIVE)
    return to_status


def audit_open_orders(conn: psycopg.Connection, gateway: OrderGateway, now: datetime) -> dict[str, Any]:
    """Remote open orders against our active live set. Unknown remote orders are
    cancelled and auto-kill `unknown_order`; our open/partial/cancel_requested rows
    missing remotely are closed from their fills (close_missing), or left for the
    next pass when the fills call failed."""
    out: dict[str, Any] = {"remote": 0, "unknown": [], "closed": {}, "fills": 0, "auto_killed": None, "error": None}
    try:
        remote = [o for o in gateway.open_orders() if isinstance(o, dict)]
    except Exception as exc:  # noqa: BLE001 - reported, retried next pass
        out["error"] = f"open_orders: {exc}"
        return out
    out["remote"] = len(remote)
    ours = active_live_orders(conn)
    by_client, by_exchange = index_remote(remote)
    known_clients = {r["client_request_id"] for r in ours}
    known_exchange = {str(r["exchange_order_id"]) for r in ours if r.get("exchange_order_id") is not None}
    for item in remote:
        client, exchange = remote_ids(item)
        if client in known_clients or exchange in known_exchange:
            continue
        out["unknown"].append({"client_order_id": client, "exchange_order_id": exchange})
        try:
            gateway.cancel({"exchange_order_id": exchange, "client_request_id": client, "id": None, "mode": "live"})
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel of unknown remote order %s failed: %s", exchange, exc)
    if out["unknown"] and maybe_auto_kill(conn, "unknown_order", {"orders": out["unknown"][:10], "count": len(out["unknown"])}):
        out["auto_killed"] = "unknown_order"
    missing = [r for r in ours if r["status"] != "submitting" and r["client_request_id"] not in by_client
               and str(r.get("exchange_order_id")) not in by_exchange]
    if missing:
        fills = fetch_fills(gateway, fills_since(missing, now))
        if fills is None:
            out["error"] = f"fills unavailable: {len(missing)} missing live order(s) left open for the next pass"
        else:
            out["fills"] = record_fills(conn, fills, only={str(r["id"]) for r in missing})["recorded"]
            for row in missing:
                out["closed"][str(row["id"])] = close_missing(conn, row, now)
    state.set_open_orders_checked(conn, now)
    return out


# --------------------------------------------------------------- submitting

def reconcile_submitting(
    conn: psycopg.Connection, gateway: OrderGateway, rows: list[dict[str, Any]], now: datetime, actor: str = ACTOR,
    grace_s: int | None = None,
) -> dict[str, Any]:
    """Live rows stuck in `submitting`: open_orders() by client id -> open (the exchange
    id adopted); else fills by client id -> recorded; else after submitting_grace_s ->
    expired with the release (never while the fills call fails). Two remote orders
    for one client id auto-kill `ambiguous_reconciliation`. Never resubmits."""
    out: dict[str, Any] = {"opened": 0, "filled": 0, "expired": 0, "ambiguous": [], "error": None}
    if not rows:
        return out
    grace = grace_s if grace_s is not None else get_int_setting(conn, "submitting_grace_s", 60)
    try:
        remote = [o for o in gateway.open_orders() if isinstance(o, dict)]
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"open_orders: {exc}"
        return out
    by_client, _ = index_remote(remote)
    missing = []
    for row in rows:
        found = by_client.get(row["client_request_id"], [])
        if len(found) > 1:
            out["ambiguous"].append(row["client_request_id"])
            continue
        if found:
            _, exchange_id = remote_ids(found[0])
            orders.set_status(conn, row["id"], "open", actor, {"reconciled": True, "exchange_order_id": exchange_id},
                              expected=("submitting",), exchange_order_id=exchange_id)
            out["opened"] += 1
        else:
            missing.append(row)
    if out["ambiguous"]:
        maybe_auto_kill(conn, "ambiguous_reconciliation", {"client_ids": out["ambiguous"][:10]})
    if missing:
        fills = fetch_fills(gateway, fills_since(missing, now))
        if fills is None:
            out["error"] = f"fills unavailable: {len(missing)} submitting live order(s) kept for the next pass"
            return out
        out["filled"] = record_fills(conn, fills, only={str(r["id"]) for r in missing})["recorded"]
        for row in missing:
            current = orders.get_order(conn, row["id"])
            if current["status"] != "submitting" or row["submitted_at"] is None or row["submitted_at"] > now - timedelta(seconds=grace):
                continue
            orders.release_unfilled(conn, current, note="submitting grace expired")
            orders.set_status(conn, row["id"], "expired", actor, {"reason": "never seen on the exchange", "grace_s": grace}, expected=("submitting",))
            out["expired"] += 1
    return out


def startup_reconcile(conn: psycopg.Connection, gateway: OrderGateway, now: datetime) -> dict[str, Any]:
    """Before the first submission: reconcile every `submitting` live row, then the
    open-order audit. Returns both results."""
    rows = active_live_orders(conn, ("submitting",), for_update=True)
    reconciled = reconcile_submitting(conn, gateway, rows, now)
    audit = audit_open_orders(conn, gateway, now)
    error = reconciled.get("error") or audit.get("error")
    return {"reconciled": reconciled, "audit": audit, "error": error}


# ------------------------------------------------------------ cancel-all

def cancel_all_direct(
    conn: psycopg.Connection, gateway: OrderGateway, now: datetime, sleep: Callable[[float], None], actor: str = "cli",
) -> dict[str, Any]:
    """The manual fallback for "exchange process down with live orders resting":
    first `kill.live_off` (live off, live assignments halted, `approved` live rows
    cancelled with their release so a restart cannot submit them, the rest
    `cancel_requested`), then list the open orders on the exchange and cancel each
    with retry (1, 2, 4, 8 s, then give up on that order and report it), then close
    the rows the exchange no longer lists (fills absorbed first; nothing closes when
    the fills call fails). A `submitting` row never seen on the exchange stays
    `cancel_requested` for the exchange process to confirm, since its place may
    still be in flight."""
    approved = len(active_live_orders(conn, ("approved",)))
    off = kill.live_off(conn, actor, DIRECT_REASON)
    never_seen = {str(r["id"]) for r in active_live_orders(conn) if r["status"] == "cancel_requested" and r.get("exchange_order_id") is None}
    remote = [o for o in gateway.open_orders() if isinstance(o, dict)]
    results: list[dict[str, Any]] = []
    for item in remote:
        client, exchange = remote_ids(item)
        row = _order_for_fill(conn, item)
        if row is not None:
            never_seen.discard(str(row["id"]))
        entry = {"exchange_order_id": exchange, "client_order_id": client, "order_id": str(row["id"]) if row else None,
                 "cancelled": False, "attempts": 0, "row_status": row["status"] if row else "unknown"}
        for delay in (0.0,) + DIRECT_RETRIES:
            if delay:
                sleep(delay)
            entry["attempts"] += 1
            try:
                if gateway.cancel({"exchange_order_id": exchange, "client_request_id": client, "id": None, "mode": "live"}):
                    entry["cancelled"] = True
                    break
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc)[:200]
        results.append(entry)
    confirmed = [o for o in gateway.open_orders() if isinstance(o, dict)]
    rows = [r for r in active_live_orders(conn, for_update=True) if not listed(r, confirmed) and str(r["id"]) not in never_seen]
    closed: dict[str, str] = {}
    error = None
    fills = fetch_fills(gateway, fills_since(rows, now)) if rows else []
    if fills is None:
        error = f"fills unavailable: {len(rows)} row(s) left cancel_requested for the exchange process"
    else:
        if rows:
            record_fills(conn, fills, only={str(r["id"]) for r in rows})
        for row in rows:
            closed[str(row["id"])] = confirm_gone(conn, row["id"], now, actor, {"reason": DIRECT_REASON})
    for entry in results:
        if entry["order_id"] in closed:
            entry["row_status"] = closed[entry["order_id"]]
    summary = {"direct": True, "remote": len(remote), "cancelled": sum(e["cancelled"] for e in results), "rows_closed": len(closed),
               "live_was_on": off["was_on"], "assignments_halted": len(off["assignments_halted"]),
               "approved_cancelled": approved, "left_for_exchange": len(never_seen), "error": error}
    add_audit(conn, "cancel_all", "live", actor, None, summary)
    return {"remote": results, "rows_closed": closed, "still_open": len(confirmed), "live_off": off, "approved_cancelled": approved,
            "left_for_exchange": sorted(never_seen), "error": error}
