"""Order approval: every limit of docs/TRADING.md "Approval", in that order, one transaction.

`approve_order` takes the per-mode advisory lock (shared with the kill switch), locks
the bankroll row, runs the checks and writes an `orders` row either `approved` (with
the ledger reservation) or `rejected` (with the stable reason code and an
order_events row). The cost is computed here from settings.fee_model; whatever limit
or cost fields the worker sent are ignored. A request with `order_side` "sell" is
decided by host.trading.sells.approve_sell (docs/TRADING.md "Selling"); every buy
check below is unchanged. A request with `ingame` true runs host.trading.ingame's
checks instead (the in-game checks replace `kickoff`; docs/INGAME.md), and so does any
request on a game in play when trade_pregame_only is false (ingame.route): after
kickoff nothing is approved outside the in-game rules.
"""
from __future__ import annotations

import math
from typing import Any, Callable

import psycopg

from host import kill
from host.errors import BadRequest
from host.events import add_audit
from host.leases import as_uuid
from host.settings import get_setting, get_settings
from host.trading import ledger, orders
from host.trading.positions import exposure_cents, losses_today, positions, server_now

__all__ = ["approve_order", "approve_smoke", "losses_today", "positions", "order_cost_cents", "fee_per_contract", "REASONS"]

REASONS = (
    "duplicate", "killed", "lease", "assignment", "market", "kickoff", "mode", "stale_book", "liquidity",
    "participation", "price_band", "max_bet", "bankroll", "daily_loss", "exposure", "buying_power",
    "no_position", "sell_exceeds_position", "open_sell_exists", "ingame_disabled", "ingame_paper_only", "ingame_stale",
    "ingame_quiet", "ingame_cutoff", "ingame_lag_suspended",
)
ORDER_SIDES = ("buy", "sell")
ACTIVE_LIST = "', '".join(orders.ACTIVE_STATUSES)
MAX_SIZE = 1_000_000


def fee_per_contract(price: float, fee_model: dict[str, Any] | None) -> float:
    """Taker fee per $1 contract: taker_rate * price * (1 - price)."""
    rate = float((fee_model or {}).get("taker_rate", 0.0) or 0.0)
    return rate * price * (1 - price)


def order_cost_cents(price: float, size: int, fee_model: dict[str, Any] | None) -> tuple[int, int]:
    """(cost, fee estimate) in cents: cost = size * (price + fee) * 100, rounded half up."""
    fee = fee_per_contract(price, fee_model)
    cost = int(math.floor(size * (price + fee) * 100 + 0.5))
    return cost, max(0, cost - int(math.floor(size * price * 100 + 0.5)))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)


def _parse(body: dict[str, Any]) -> dict[str, Any]:
    """The request fields the host uses; 400 on malformed money or ids."""
    crid = body.get("client_request_id")
    if not isinstance(crid, str) or not crid or len(crid) > 64:
        raise BadRequest("client_request_id must be a string of at most 64 characters")
    price = _number(body.get("price"))
    if price is None or price < 0 or price > 1:
        raise BadRequest("price must be a number between 0 and 1")
    size = body.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1 or size > MAX_SIZE:
        raise BadRequest(f"size must be a whole number of contracts between 1 and {MAX_SIZE}")
    snapshot_id = body.get("snapshot_id")
    if isinstance(snapshot_id, bool) or not isinstance(snapshot_id, int):
        snapshot_id = None
    rationale = body.get("rationale")
    order_side = body.get("order_side")
    order_side = "buy" if order_side is None else order_side
    if order_side not in ORDER_SIDES:
        raise BadRequest("order_side must be buy or sell")
    return {
        "client_request_id": crid,
        "job_id": as_uuid(body.get("job_id")),
        "lease_token": as_uuid(body.get("lease_token")),
        "assignment_id": as_uuid(body.get("assignment_id")),
        "market_id": as_uuid(body.get("market_id")),
        "snapshot_id": snapshot_id,
        "price": round(price, 4),
        "size": size,
        "my_p": _number(body.get("my_p")),
        "market_p": _number(body.get("market_p")),
        "edge": _number(body.get("edge")),
        "rationale": str(rationale)[:512] if rationale is not None else None,
        "order_side": order_side,
        "ingame": body.get("ingame") is True,
        "gtd_seconds": body.get("gtd_seconds") if type(body.get("gtd_seconds")) is int else None,
    }


def _stored_decision(conn: psycopg.Connection, crid: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT id, status, reject_reason FROM orders WHERE client_request_id = %s", (crid,)).fetchone()
    if row is None:
        return None
    rejected = row["status"] == "rejected"
    return {"status": "rejected" if rejected else "approved", "order_id": str(row["id"]),
            "reason": row["reject_reason"] if rejected else None, "duplicate": True}


def _one(conn: psycopg.Connection, sql: str, key: Any) -> dict[str, Any] | None:
    if key is None:
        return None
    row = conn.execute(sql, (key,)).fetchone()
    return dict(row) if row is not None else None


def _load(conn: psycopg.Connection, worker: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """Everything the checks look at, read once.

    Lock order: the per-mode advisory lock (taken by the caller), the bankroll row
    FOR UPDATE, then the job row FOR SHARE. The job is read last and shared-locked so
    a release that is committing (heartbeat released[], the reaper, the drain
    handshake) is waited for and its queued row is what the lease check sees. A halt
    or a settlement takes the same advisory lock first, so the assignment row read
    here is never stale either.
    """
    settings = get_settings(conn)
    assignment = _one(conn, "SELECT * FROM assignments WHERE id = %s", req["assignment_id"])
    market = _one(conn, "SELECT * FROM markets WHERE id = %s", req["market_id"])
    cited = _one(conn, "SELECT * FROM price_snapshots WHERE id = %s", req["snapshot_id"])
    latest = None
    if market is not None:
        latest = _one(conn, "SELECT * FROM price_snapshots WHERE market_id = %s ORDER BY ts DESC, id DESC LIMIT 1", market["id"])
    cost, fee = order_cost_cents(req["price"], req["size"], settings.get("fee_model"))
    ctx: dict[str, Any] = {
        "worker": worker, "req": req, "settings": settings, "now": server_now(conn),
        "assignment": assignment, "market": market, "cited": cited, "latest": latest,
        "mode": assignment["mode"] if assignment is not None else "paper", "cost": cost, "fee": fee,
        "game": None, "model": None, "bankroll": None, "job": None,
    }
    if assignment is not None:
        ctx["game"] = _one(conn, "SELECT * FROM games WHERE game_id = %s", assignment["game_id"])
        ctx["model"] = _one(conn, "SELECT * FROM models WHERE id = %s", assignment["model_id"])
        ctx["bankroll"] = _one(conn, "SELECT * FROM bankrolls WHERE assignment_id = %s FOR UPDATE", assignment["id"])
    ctx["job"] = _one(conn, "SELECT * FROM jobs WHERE id = %s FOR SHARE", req["job_id"])
    return ctx


def _check_killed(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    return kill.is_killed(conn)


def _check_lease(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    job, req, worker = ctx["job"], ctx["req"], ctx["worker"]
    return (
        job is None or job["kind"] != "trade" or job["status"] != "leased"
        or job["lease_worker_id"] != worker["id"] or req["lease_token"] is None
        or job["lease_token"] != req["lease_token"] or bool(job["preempt_requested"])
        or job["lease_expires_at"] is None or job["lease_expires_at"] <= ctx["now"]
        or worker["desired_role"] != "trade"
    )


def _check_assignment(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    a = ctx["assignment"]
    return a is None or a["status"] != "active" or ctx["bankroll"] is None or a["job_id"] != ctx["job"]["id"]


def _check_market(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    m = ctx["market"]
    return (
        m is None or m["game_id"] != ctx["assignment"]["game_id"] or not m["mapping_confirmed"]
        or m["status"] != "open"
    )


def _check_kickoff(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    """A pre-game request needs a game that has not kicked off. Whatever
    trade_pregame_only says: with it off, a request on a game in play never reaches
    this list but host.trading.ingame's (ingame.route)."""
    game = ctx["game"]
    if game is None or game["status"] == "final":
        return True
    return game["kickoff_at"] is not None and game["kickoff_at"] <= ctx["now"]


def _check_mode(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    """Live needs the switch, a live_eligible lineage, auth_ok and a market on the
    live platform (the current market source; never the price-only CLOB source)."""
    if ctx["mode"] != "live":
        return False
    from host.trading.live import market_platform_problem

    state = conn.execute("SELECT auth_ok FROM exchange_state WHERE id").fetchone()
    model = ctx["model"]
    market = ctx["market"]
    return (
        ctx["settings"].get("live_enabled") is not True or model is None or model["status"] != "live_eligible"
        or state is None or not state["auth_ok"]
        or market is None or market_platform_problem(conn, market["platform"]) is not None
    )


def _check_book(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    """The cited snapshot must be this market's and no older than book_max_age_s.
    Being the latest does not spare it: when the poller stalls, the newest book is
    still a dead book and nothing may be approved against it."""
    cited, latest = ctx["cited"], ctx["latest"]
    if cited is None or latest is None or cited["market_id"] != ctx["market"]["id"]:
        return True
    max_age = float(ctx["settings"].get("book_max_age_s", 60) or 0)
    return (ctx["now"] - cited["ts"]).total_seconds() > max_age


def _check_liquidity(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    floor = int(ctx["settings"].get("liquidity_floor_cents", 0) or 0)
    return any(int(s["liquidity_usd_cents"] or 0) < floor for s in (ctx["cited"], ctx["latest"]))


def _level(entry: Any) -> tuple[float, float] | None:
    """One book level as (price, size) from [price, size] or {"price", "size"}."""
    try:
        if isinstance(entry, dict):
            return float(entry["price"]), float(entry["size"])
        return float(entry[0]), float(entry[1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def depth_at_or_better(levels: Any, price: float) -> float:
    """Contracts offered at or below `price` across ask levels."""
    total = 0.0
    for entry in levels if isinstance(levels, list) else []:
        level = _level(entry)
        if level is not None and level[0] <= price + 1e-9:
            total += max(level[1], 0.0)
    return total


def _check_participation(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    share = float(ctx["settings"].get("participation", 0.5) or 0.0)
    depth = depth_at_or_better(ctx["cited"]["ask_depth"], ctx["req"]["price"])
    return ctx["req"]["size"] > share * depth + 1e-9


def _check_price_band(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    price = ctx["req"]["price"]
    if price < 0.01 - 1e-9 or price > 0.99 + 1e-9:
        return True
    tick = float(ctx["market"]["tick"] or 0.01)
    if tick > 0 and abs(price / tick - round(price / tick)) > 1e-6:
        return True
    ask = ctx["latest"]["ask"] if ctx["latest"]["ask"] is not None else ctx["cited"]["ask"]
    return ask is not None and price > float(ask) + 0.05 + 1e-9


def _check_max_bet(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    limit = int(ctx["settings"].get("max_bet_cents", 0) or 0)
    for own in (ctx["assignment"]["max_bet_cents"], ctx.get("extra_max_bet_cents")):
        if own is not None:
            limit = min(limit, int(own))
    open_cost = conn.execute(
        f"""
        SELECT COALESCE(SUM(cost_cents), 0) AS s FROM orders
         WHERE assignment_id = %s AND market_id = %s AND status IN ('{ACTIVE_LIST}')
        """,
        (ctx["assignment"]["id"], ctx["market"]["id"]),
    ).fetchone()["s"]
    return ctx["cost"] + int(open_cost) > limit


def _check_bankroll(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    return ctx["cost"] > int(ctx["bankroll"]["available_cents"])


def _trip_live(conn: psycopg.Connection, losses: int, limit: int) -> None:
    """The live daily-loss trip: kill.live_off (live_enabled off, live assignments halted
    with their orders cancelled, audit live_off) plus one daily_loss_trip audit row.
    Runs once; a later request finds live_enabled false."""
    if get_setting(conn, "live_enabled", False) is not True:
        return
    result = kill.live_off(conn, "host", "daily_loss")
    add_audit(
        conn, "daily_loss_trip", "live", "host", {"live_enabled": True},
        {"live_enabled": False, "losses_cents": losses, "max_daily_loss_cents": limit,
         "assignments_halted": result["assignments_halted"]},
    )


def _check_daily_loss(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    limits = ctx["settings"].get("max_daily_loss_cents") or {}
    limit = int(limits.get(ctx["mode"], 0) or 0)
    today = losses_today(conn, ctx["mode"], ctx["now"])
    ctx["losses"] = today
    if today["losses_cents"] >= limit:
        if ctx["mode"] == "live":
            _trip_live(conn, today["losses_cents"], limit)
        return True
    return today["losses_cents"] + today["reserved_cents"] + ctx["cost"] > limit


def _check_exposure(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    limits = ctx["settings"].get("max_exposure_cents") or {}
    limit = int(limits.get(ctx["mode"], 0) or 0)
    if limit <= 0:
        return False
    return exposure_cents(conn, ctx["mode"]) + ctx["cost"] > limit


def reserved_live_cents(conn: psycopg.Connection) -> int:
    """Cents reserved by live approvals not yet filled or released (all live bankrolls)."""
    row = conn.execute("SELECT COALESCE(SUM(reserved_cents), 0) AS s FROM bankrolls WHERE mode = 'live'").fetchone()
    return int(row["s"])


def live_spent_since(conn: psycopg.Connection, since: Any) -> int:
    """Cents live fills took out of reservations (cost plus fees) after `since`: cash
    the exchange already debited that the last probed buying power does not show."""
    row = conn.execute(
        "SELECT COALESCE(SUM(-d_reserved), 0) AS s FROM ledger WHERE mode = 'live' AND kind = 'fill' AND ts > %s", (since,)
    ).fetchone()
    return int(row["s"])


def buying_power_short(conn: psycopg.Connection, cost: int, now: Any, settings: dict[str, Any]) -> bool:
    """docs/LIVE.md "Live approvals": the exchange's buying power must be fresher than
    buying_power_max_age_s and cover `cost` plus every live reservation plus what
    live fills spent since the probe; a missing or stale figure rejects."""
    state = conn.execute("SELECT buying_power_cents, balance_checked_at FROM exchange_state WHERE id").fetchone()
    if state is None or state["buying_power_cents"] is None or state["balance_checked_at"] is None:
        return True
    max_age = float(settings.get("buying_power_max_age_s", 300) or 300)
    if (now - state["balance_checked_at"]).total_seconds() > max_age:
        return True
    spent = live_spent_since(conn, state["balance_checked_at"])
    return cost + reserved_live_cents(conn) + spent > int(state["buying_power_cents"])


def _check_buying_power(conn: psycopg.Connection, ctx: dict[str, Any]) -> bool:
    if ctx["mode"] != "live":
        return False
    return buying_power_short(conn, ctx["cost"], ctx["now"], ctx["settings"])


CHECKS: tuple[tuple[str, Callable[[psycopg.Connection, dict[str, Any]], bool]], ...] = (
    ("killed", _check_killed), ("lease", _check_lease), ("assignment", _check_assignment),
    ("market", _check_market), ("kickoff", _check_kickoff), ("mode", _check_mode), ("stale_book", _check_book),
    ("liquidity", _check_liquidity), ("participation", _check_participation), ("price_band", _check_price_band),
    ("max_bet", _check_max_bet), ("bankroll", _check_bankroll), ("daily_loss", _check_daily_loss),
    ("exposure", _check_exposure), ("buying_power", _check_buying_power),
)


def _insert(conn: psycopg.Connection, ctx: dict[str, Any], status: str, reason: str | None) -> dict[str, Any]:
    req, a, job = ctx["req"], ctx["assignment"], ctx["job"]
    cited = ctx["cited"]
    row = conn.execute(
        """
        INSERT INTO orders (client_request_id, assignment_id, worker_id, job_id, market_id, mode, price, size,
                            cost_cents, fee_cents_est, snapshot_id, status, reject_reason, my_p, market_p, edge, rationale,
                            side, ingame)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (
            req["client_request_id"], a["id"] if a else None, ctx["worker"]["id"], job["id"] if job else None,
            ctx["market"]["id"], ctx["mode"], req["price"], req["size"], ctx["cost"], ctx["fee"],
            cited["id"] if cited else None, status, reason, req["my_p"], req["market_p"], req["edge"], req["rationale"],
            req.get("order_side", "buy"), bool(req.get("ingame")),
        ),
    ).fetchone()
    detail = {"reason": reason} if reason else {"cost_cents": ctx["cost"], "fee_cents_est": ctx["fee"]}
    if req.get("order_side") == "sell":
        detail["order_side"] = "sell"
    if req.get("ingame") or ctx.get("in_play"):
        from host.trading.ingame import entry_state, gtd_seconds

        detail.update(state_at_entry=entry_state(ctx.get("game_state")),
                      gtd_seconds=gtd_seconds(ctx["settings"]), gtd_seconds_requested=req.get("gtd_seconds"))
        detail["ingame" if req.get("ingame") else "in_play"] = True
    orders.add_order_event(conn, row["id"], None, status, ctx["worker"]["id"], detail)
    return dict(row)


def _smoke_problem(conn: psycopg.Connection, ctx: dict[str, Any]) -> str | None:
    """The smoke checks in order: kill, price band, max bet, auth (live switch,
    auth_ok and a market on the live platform), buying power. None when all pass."""
    from host.trading.live import market_platform_problem

    settings = ctx["settings"]
    state = conn.execute("SELECT auth_ok FROM exchange_state WHERE id").fetchone()
    checks = (
        ("killed", lambda: kill.is_killed(conn)),
        ("price_band", lambda: _check_price_band(conn, ctx)),
        ("max_bet", lambda: ctx["cost"] > int(settings.get("max_bet_cents", 0) or 0)),
        ("mode", lambda: settings.get("live_enabled") is not True or state is None or not state["auth_ok"]
         or market_platform_problem(conn, ctx["market"].get("platform")) is not None),
        ("buying_power", lambda: buying_power_short(conn, ctx["cost"], ctx["now"], settings)),
    )
    return next((name for name, check in checks if check()), None)


def approve_smoke(conn: psycopg.Connection, market: dict[str, Any], price: float, size: int, actor: str) -> dict[str, Any]:
    """Approve or reject the smoke order (docs/LIVE.md "Smoke order"): an `orders` row
    `kind='smoke'`, `mode='live'`, no assignment, no worker, no reservation. Under the
    live approval lock. Returns {"status", "order_id", "reason"} like approve_order."""
    import uuid

    kill.approval_lock(conn, "live")
    settings = get_settings(conn)
    latest = _one(conn, "SELECT * FROM price_snapshots WHERE market_id = %s ORDER BY ts DESC, id DESC LIMIT 1", market["id"])
    if latest is None:
        raise BadRequest("the market has no price snapshot yet")
    cost, fee = order_cost_cents(price, size, settings.get("fee_model"))
    ctx = {"settings": settings, "now": server_now(conn), "market": market, "cited": latest, "latest": latest,
           "req": {"price": round(float(price), 4), "size": int(size)}, "mode": "live", "cost": cost, "fee": fee}
    reason = _smoke_problem(conn, ctx)
    status = "rejected" if reason else "approved"
    row = conn.execute(
        """
        INSERT INTO orders (client_request_id, kind, market_id, mode, price, size, cost_cents, fee_cents_est,
                            snapshot_id, status, reject_reason, rationale)
        VALUES (%s, 'smoke', %s, 'live', %s, %s, %s, %s, %s, %s, %s, 'smoke order') RETURNING *
        """,
        ("smoke-" + uuid.uuid4().hex[:24], market["id"], ctx["req"]["price"], size, cost, fee, latest["id"], status, reason),
    ).fetchone()
    orders.add_order_event(conn, row["id"], None, status, actor, {"reason": reason} if reason else {"cost_cents": cost, "smoke": True})
    return {"status": status, "order_id": str(row["id"]), "reason": reason}


def approve_order(conn: psycopg.Connection, worker: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Decide one order request: {"status": "approved"|"rejected", "order_id", "reason"}.
    A sell (`order_side` "sell") goes to host.trading.sells.approve_sell."""
    if body.get("order_side") == "sell":
        from host.trading.sells import approve_sell  # sells builds on this module

        return approve_sell(conn, worker, body)
    req = _parse(body)
    stored = _stored_decision(conn, req["client_request_id"])
    if stored is not None:
        return stored
    first = _one(conn, "SELECT mode FROM assignments WHERE id = %s", req["assignment_id"])
    kill.approval_lock(conn, first["mode"] if first is not None else "paper")
    stored = _stored_decision(conn, req["client_request_id"])
    if stored is not None:
        return stored
    ctx = _load(conn, worker, req)
    from host.trading import ingame  # ingame builds on this module

    checks = ingame.route(conn, ctx, ingame.BUY_CHECKS) or CHECKS
    reason = next((name for name, check in checks if check(conn, ctx)), None)
    if ctx["market"] is None:
        # orders.market_id is NOT NULL: a request for an unknown market cannot be stored.
        return {"status": "rejected", "order_id": None, "reason": reason or "market"}
    if reason is not None:
        row = _insert(conn, ctx, "rejected", reason)
        return {"status": "rejected", "order_id": str(row["id"]), "reason": reason}
    row = _insert(conn, ctx, "approved", None)
    ledger.reserve(conn, ctx["bankroll"]["id"], ctx["cost"], row["id"])
    return {"status": "approved", "order_id": str(row["id"]), "reason": None}
