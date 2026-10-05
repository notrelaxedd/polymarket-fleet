"""The executor outbox (docs/TRADING.md "Executor and paper fills", docs/LIVE.md
"Executor live path").

Two gateways, chosen by the order's mode. Every tick: `approved -> submitting`
(committed) -> gateway `place` -> `open`; a place that times out leaves the row
`submitting` to be reconciled by client id (open orders, then fills, then expiry
after `submitting_grace_s`), never resubmitted blind. `cancel_requested` rows go to
the gateway with retries at 1, 2, 4, 8 s (then every 8 s) until `open_orders()` no
longer lists them, their last fills absorbed before the release (and never closed
while the fills call fails); `open`/`partial` rows past `gtd_at` expire: paper at
once with the release, live through the same cancel path (reason `gtd`), closed as
`expired` once the exchange no longer lists them. A pre-game order is bounded and
cancelled by kickoff whatever trade_pregame_only says; an order approved under the
in-game rules (`orders.ingame`, or an untagged one approved in play:
host.trading.orders.in_play_order) gets a GTD of `ingame_gtd_seconds` and is never
bounded or cancelled by kickoff. Every tick also switches trade_ingame off where the
in-game model's lineage is retired (host.trading.assignments_ingame.turn_off_retired). Nothing is ever submitted while the
kill switch is on: the kill flag is read FOR SHARE in the transaction that marks a
row `submitting`, so a kill in flight is waited for and an approved row found under
kill is cancelled with release. While `live_blocked` is set (clock skew over the
limit) no live order is placed; cancels, listings and fills keep going so a kill's
cancels still reach the exchange.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import psycopg

from host.errors import Conflict
from host.exchange import live_sync
from host.exchange.adapters.base import NotConfigured, OrderGateway, PaperGateway, utcnow
from host.exchange.ratelimit import RateLimiter
from host.settings import get_int_setting
from host.trading import assignments_ingame, orders

log = logging.getLogger(__name__)

ACTOR = "exchange"
RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
RECONCILE_AFTER = timedelta(seconds=5)


def killed(conn: psycopg.Connection) -> bool:
    """The kill flag, read FOR SHARE so a kill transaction in flight is waited for."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'kill_switch' FOR SHARE").fetchone()
    return row is not None and row["value"] is True


def kickoff_of(conn: psycopg.Connection, order: dict[str, Any]) -> datetime | None:
    """The kickoff of the order's game (None for a smoke order without an assignment)."""
    if order.get("assignment_id") is None:
        return None
    row = conn.execute(
        "SELECT g.kickoff_at FROM assignments a JOIN games g ON g.game_id = a.game_id WHERE a.id = %s",
        (order["assignment_id"],),
    ).fetchone()
    return None if row is None else row["kickoff_at"]


def _commit(conn: psycopg.Connection) -> None:
    if not conn.autocommit:
        conn.commit()


def with_market_ref(conn: psycopg.Connection, order: dict[str, Any]) -> dict[str, Any]:
    """The order dict plus `market_ref` (the exchange's market id): what a live
    gateway places against. The gateway has no database access of its own."""
    if order.get("market_ref"):
        return order
    row = conn.execute("SELECT market_ref FROM markets WHERE id = %s", (order["market_id"],)).fetchone()
    return {**order, "market_ref": row["market_ref"] if row else None}


def takes_own_tokens(gateway: OrderGateway) -> bool:
    """A gateway holding the fleet limiter takes its own token per call (the real
    LiveGateway); the executor then takes none, so nothing is counted twice."""
    return getattr(gateway, "limiter", None) is not None


class Executor:
    """Holds the two gateways and the per-order cancel retry schedule between ticks.

    `Executor(paper, live)`: paper orders go to `paper` (a PaperGateway by default),
    live orders to `live`. With one gateway only (`Executor(gateway)`, the step 4
    form) it serves both modes."""

    def __init__(
        self, paper_gateway: OrderGateway | None = None, live_gateway: OrderGateway | None = None,
        limiter: RateLimiter | None = None, actor: str = ACTOR,
    ) -> None:
        self.paper_gateway = paper_gateway or PaperGateway()
        self.live_gateway = live_gateway if live_gateway is not None else self.paper_gateway
        self.limiter = limiter
        self.actor = actor
        self.retries: dict[str, tuple[int, float]] = {}
        self.sent: set[str] = set()  # live cancels acknowledged, awaiting open_orders() confirmation
        self.live_blocked: str | None = None

    @property
    def gateway(self) -> OrderGateway:
        """The live gateway (what the step 4 single-gateway executor exposed)."""
        return self.live_gateway

    def gateway_for(self, order: dict[str, Any]) -> OrderGateway:
        return self.live_gateway if order["mode"] == "live" else self.paper_gateway

    def tick(self, conn: psycopg.Connection, now: datetime | None = None) -> dict[str, int]:
        now = now or utcnow()
        counts = {"submitted": 0, "cancelled_under_kill": 0, "reconciled": 0, "kickoff": 0, "ingame_retired": 0,
                  "cancels": 0, "expired": 0}
        if killed(conn):
            counts["cancelled_under_kill"] = self.cancel_approved_under_kill(conn)
        else:
            counts["submitted"] = self.submit_approved(conn, now)
        _commit(conn)
        counts["reconciled"] = self.reconcile_submitting(conn, now)
        _commit(conn)
        counts["kickoff"] = self.cancel_at_kickoff(conn, now)
        _commit(conn)
        counts["ingame_retired"] = len(assignments_ingame.turn_off_retired(conn, self.actor))
        _commit(conn)
        counts["expired"] = self.expire_orders(conn, now)
        _commit(conn)
        counts["cancels"] = self.process_cancels(conn, now)
        _commit(conn)
        return counts

    # ------------------------------------------------------------------ submit

    def cancel_approved_under_kill(self, conn: psycopg.Connection) -> int:
        rows = conn.execute("SELECT id FROM orders WHERE status = 'approved' FOR UPDATE SKIP LOCKED").fetchall()
        for row in rows:
            orders.cancel_order(conn, row["id"], self.actor, "kill")
        return len(rows)

    def submit_approved(self, conn: psycopg.Connection, now: datetime) -> int:
        """Move approved rows through the outbox one transaction at a time. A pre-game
        order lives until `gtd_seconds` after submission or until kickoff, whichever
        comes first (whatever trade_pregame_only says: an order approved before kickoff
        never trades the game in play). An order approved under the in-game rules
        (orders.in_play_order) lives `ingame_gtd_seconds` after submission, kickoff
        aside (docs/INGAME.md)."""
        gtd = get_int_setting(conn, "gtd_seconds", 900)
        ingame_gtd = max(1, get_int_setting(conn, "ingame_gtd_seconds", 60))
        submitted = 0
        while True:
            if killed(conn):
                break
            row = conn.execute(
                """
                SELECT * FROM orders WHERE status = 'approved' AND (mode = 'paper' OR NOT %s)
                 ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
                """,
                (self.live_blocked is not None,),
            ).fetchone()
            if row is None:
                break
            if row["mode"] == "live" and not self._take(self.gateway_for(row), "orders", now):
                _commit(conn)
                break
            ingame = orders.in_play_order(conn, dict(row))
            gtd_at = now + timedelta(seconds=ingame_gtd if ingame else gtd)
            kickoff = kickoff_of(conn, row) if not ingame else None
            if kickoff is not None and kickoff < gtd_at:
                gtd_at = kickoff
            order = orders.set_status(
                conn, row["id"], "submitting", self.actor, {"gateway": self.gateway_for(row).name},
                expected=("approved",), submitted_at=now, gtd_at=gtd_at,
            )
            _commit(conn)
            self._place(conn, order)
            _commit(conn)
            submitted += 1
        return submitted

    def _take(self, gateway: OrderGateway, category: str, now: datetime) -> bool:
        """One limiter token for a live call. A gateway that takes its own tokens is
        asked whether one is due within its `limiter_wait_s` first, so an order is
        not marked submitting only to be refused by the local limiter."""
        if takes_own_tokens(gateway):
            wait = float(gateway.limiter.wait_seconds(category, now=now))  # type: ignore[attr-defined]
            allowed = float((getattr(gateway, "live", None) or {}).get("limiter_wait_s") or 0.0)
            return wait <= allowed
        if self.limiter is None:
            return True
        return self.limiter.take(category, now=now)

    def _place(self, conn: psycopg.Connection, order: dict[str, Any]) -> None:
        """Gateway place for one `submitting` row, then `open`; a kill that landed in
        between leaves the row cancelled (paper) or cancel_requested (live)."""
        gateway = self.gateway_for(order)
        if order["mode"] == "live":
            order = with_market_ref(conn, order)
        try:
            exchange_id = gateway.place(order)
        except NotConfigured as exc:
            log.error("order %s cannot be placed: %s", order["id"], exc)
            self._reject_by_exchange(conn, order, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - timeout or transport error: reconcile later
            log.warning("place of order %s failed, left submitting: %s", order["id"], exc)
            return
        try:
            orders.set_status(conn, order["id"], "open", self.actor, {"exchange_order_id": exchange_id}, expected=("submitting",), exchange_order_id=exchange_id)
        except Conflict:
            current = orders.get_order(conn, order["id"])
            log.warning("order %s became %s while being placed; cancelling on the exchange", order["id"], current["status"])
            conn.execute("UPDATE orders SET exchange_order_id = %s, updated_at = now() WHERE id = %s", (exchange_id, order["id"]))
            current["exchange_order_id"] = exchange_id
            self._cancel_after_race(conn, current)

    def _cancel_after_race(self, conn: psycopg.Connection, current: dict[str, Any]) -> None:
        """The row changed under a place in flight (a kill, cancel-all --direct): the
        order that reached the exchange is cancelled there right away whatever the
        row's status, since a row already closed in the database would otherwise
        leave it orphaned. A cancel_requested row is then confirmed like any other
        (at once on a paper gateway, through open_orders() on a live one)."""
        gateway = self.gateway_for(current)
        if current["status"] == "cancel_requested":
            if self._cancel_on_exchange(conn, current, utcnow()):
                if isinstance(gateway, PaperGateway):
                    orders.confirm_cancelled(conn, current["id"], self.actor, {"gateway": gateway.name})
                else:
                    self.sent.add(str(current["id"]))
            return
        if current["mode"] != "live":
            return
        try:
            gateway.cancel(current)
        except Exception as exc:  # noqa: BLE001 - the audit finds and cancels an orphan as a last resort
            log.warning("cancel of order %s after the place race failed: %s", current["id"], exc)

    def _reject_by_exchange(self, conn: psycopg.Connection, order: dict[str, Any], reason: str) -> None:
        current = orders.get_order(conn, order["id"], for_update=True)
        orders.release_unfilled(conn, current, note="rejected by exchange")
        orders.set_status(conn, order["id"], "rejected_by_exchange", self.actor, {"reason": reason}, expected=("submitting",), reject_reason=reason[:200])

    # --------------------------------------------------------------- reconcile

    def reconcile_submitting(self, conn: psycopg.Connection, now: datetime) -> int:
        """Rows stuck in `submitting` (a crash or a timed-out place): paper rows are
        placed on the paper gateway; live rows go through live_sync.reconcile_submitting
        (open orders, fills, expiry after the grace). Never resubmits."""
        rows = conn.execute(
            "SELECT * FROM orders WHERE status = 'submitting' AND submitted_at <= %s ORDER BY created_at FOR UPDATE SKIP LOCKED",
            (now - RECONCILE_AFTER,),
        ).fetchall()
        if not rows:
            return 0
        done, live_rows = 0, []
        for row in rows:
            gateway = self.gateway_for(row)
            if isinstance(gateway, PaperGateway):
                exchange_id = gateway.place(row)
                orders.set_status(conn, row["id"], "open", self.actor, {"reconciled": True, "exchange_order_id": exchange_id}, expected=("submitting",), exchange_order_id=exchange_id)
                done += 1
            else:
                live_rows.append(dict(row))
        if live_rows:
            result = live_sync.reconcile_submitting(conn, self.live_gateway, live_rows, now, self.actor)
            done += int(result["opened"]) + int(result["filled"]) + int(result["expired"])
        return done

    # ----------------------------------------------------------------- kickoff

    def cancel_at_kickoff(self, conn: psycopg.Connection, now: datetime) -> int:
        """Cancel every active pre-game order whose game has kicked off (actor exchange,
        reason kickoff), whatever trade_pregame_only says; orders approved under the
        in-game rules (orders.in_play_order) are left alone. Paper cancels at once with
        the release; live rows become cancel_requested for process_cancels."""
        rows = conn.execute(
            f"""
            SELECT o.id FROM orders o
              JOIN assignments a ON a.id = o.assignment_id
              JOIN games g ON g.game_id = a.game_id
             WHERE o.status IN ('approved', 'submitting', 'open', 'partial') AND NOT o.ingame
               AND NOT {orders.IN_PLAY_EVENT.format(alias="o")}
               AND g.kickoff_at IS NOT NULL AND g.kickoff_at <= %s
             ORDER BY o.created_at FOR UPDATE OF o SKIP LOCKED
            """,
            (now,),
        ).fetchall()
        for row in rows:
            orders.cancel_order(conn, row["id"], self.actor, "kickoff")
        return len(rows)

    # ----------------------------------------------------------------- cancels

    def _schedule_retry(self, key: str, attempts: int, now: datetime) -> None:
        delay = RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)]
        self.retries[key] = (attempts + 1, now.timestamp() + delay)

    def process_cancels(self, conn: psycopg.Connection, now: datetime) -> int:
        """Ask the gateway to cancel every due `cancel_requested` row; a live cancel
        counts as confirmed only once `open_orders()` no longer lists the order
        (one listing per tick), with its fills absorbed before the release."""
        rows = conn.execute("SELECT * FROM orders WHERE status = 'cancel_requested' ORDER BY created_at FOR UPDATE SKIP LOCKED").fetchall()
        done, verify = 0, []
        for row in rows:
            key = str(row["id"])
            if key in self.sent:
                verify.append(dict(row))  # acknowledged earlier (the place race): confirm, do not resend
                continue
            attempts, next_at = self.retries.get(key, (0, 0.0))
            if now.timestamp() < next_at:
                continue
            if not self._cancel_on_exchange(conn, row, now):
                self._schedule_retry(key, attempts, now)
            elif isinstance(self.gateway_for(row), PaperGateway):
                orders.confirm_cancelled(conn, row["id"], self.actor, {"gateway": self.gateway_for(row).name})
                self.retries.pop(key, None)
                done += 1
            else:
                verify.append(dict(row))
        if verify:
            done += self._confirm_live_cancels(conn, verify, now)
        return done

    def _confirm_live_cancels(self, conn: psycopg.Connection, rows: list[dict[str, Any]], now: datetime) -> int:
        """Rows the exchange no longer lists are closed (expired past gtd, else
        cancelled) once their last fills were read; when the listing or the fills
        call fails nothing closes and every row is retried on the schedule."""
        try:
            remote = [o for o in self.live_gateway.open_orders() if isinstance(o, dict)]
        except Exception as exc:  # noqa: BLE001 - unconfirmed: retried on the schedule
            log.warning("open_orders after cancel failed: %s", exc)
            remote = None
        gone = [r for r in rows if remote is not None and not live_sync.listed(r, remote)]
        gone_ids = {str(r["id"]) for r in gone}
        if gone:
            fills = live_sync.fetch_fills(self.live_gateway, live_sync.fills_since(gone, now))
            if fills is None:
                log.warning("fills unavailable: %d confirmed cancel(s) left open for the next pass", len(gone))
                gone_ids = set()
            else:
                live_sync.record_fills(conn, fills, only=gone_ids, actor=self.actor)
        for row in rows:
            key = str(row["id"])
            self.sent.discard(key)
            if key in gone_ids:
                live_sync.confirm_gone(conn, row["id"], now, self.actor, {"gateway": self.live_gateway.name})
                self.retries.pop(key, None)
            else:
                self._schedule_retry(key, self.retries.get(key, (0, 0.0))[0], now)
        return len(gone_ids)

    def _cancel_on_exchange(self, conn: psycopg.Connection, order: dict[str, Any], now: datetime) -> bool:
        """Send a cancel to the gateway for a cancel_requested row; True when it
        answered ok (the caller confirms through open_orders for live rows)."""
        if order["status"] != "cancel_requested":
            return True
        if order["mode"] == "live" and not self._take(self.gateway_for(order), "cancels", now):
            return False
        try:
            return bool(self.gateway_for(order).cancel(order))
        except Exception as exc:  # noqa: BLE001 - retried on the schedule
            log.warning("cancel of order %s failed: %s", order["id"], exc)
            return False

    # ------------------------------------------------------------------ expiry

    def expire_orders(self, conn: psycopg.Connection, now: datetime) -> int:
        """Paper rows past gtd_at expire at once with the release. A live row goes
        through the cancel path (cancel_requested, reason gtd): the exchange may
        have filled it in the last second, so it is closed as `expired` only once
        open_orders() no longer lists it and its fills were absorbed."""
        rows = conn.execute(
            "SELECT * FROM orders WHERE status IN ('open', 'partial') AND gtd_at IS NOT NULL AND gtd_at <= %s FOR UPDATE SKIP LOCKED",
            (now,),
        ).fetchall()
        expired = 0
        for row in rows:
            if row["mode"] == "live":
                orders.set_status(conn, row["id"], "cancel_requested", self.actor, {"reason": "gtd", "gtd_at": row["gtd_at"].isoformat()}, expected=("open", "partial"))
            else:
                orders.release_unfilled(conn, row, note="gtd expired")
                orders.set_status(conn, row["id"], "expired", self.actor, {"gtd_at": row["gtd_at"].isoformat()}, expected=("open", "partial"))
            expired += 1
        return expired


def run_once(conn: psycopg.Connection, gateway: OrderGateway | None = None, now: datetime | None = None) -> dict[str, int]:
    """One executor tick with a throwaway single-gateway Executor (tests and the CLI)."""
    return Executor(gateway).tick(conn, now)
