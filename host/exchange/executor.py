"""The executor outbox (docs/TRADING.md, "Executor and paper fills").

Every tick: `approved -> submitting` (committed) -> gateway `place` -> `open`; a place
that times out leaves the row `submitting` to be reconciled by client id, never
resubmitted blind. `cancel_requested` rows go to the gateway with retries at 1, 2, 4,
8 s (then every 8 s) until confirmed; `open`/`partial` rows past `gtd_at` expire with
the ledger release. Nothing is ever submitted while the kill switch is on: the kill
flag is read FOR SHARE in the transaction that marks a row `submitting`, so a kill in
flight is waited for and an approved row found under kill is cancelled with release.
Every transition goes through host.trading.orders.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import psycopg

from host.errors import Conflict
from host.exchange.adapters.base import NotConfigured, OrderGateway, PaperGateway, utcnow
from host.exchange.ratelimit import RateLimiter
from host.settings import get_int_setting, get_setting
from host.trading import orders

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


class Executor:
    """Holds the gateway and the per-order cancel retry schedule between ticks."""

    def __init__(self, gateway: OrderGateway | None = None, limiter: RateLimiter | None = None, actor: str = ACTOR) -> None:
        self.gateway = gateway or PaperGateway()
        self.limiter = limiter
        self.actor = actor
        self.retries: dict[str, tuple[int, float]] = {}

    def tick(self, conn: psycopg.Connection, now: datetime | None = None) -> dict[str, int]:
        now = now or utcnow()
        counts = {"submitted": 0, "cancelled_under_kill": 0, "reconciled": 0, "kickoff": 0, "cancels": 0, "expired": 0}
        if killed(conn):
            counts["cancelled_under_kill"] = self.cancel_approved_under_kill(conn)
        else:
            counts["submitted"] = self.submit_approved(conn, now)
        _commit(conn)
        counts["reconciled"] = self.reconcile_submitting(conn, now)
        _commit(conn)
        counts["kickoff"] = self.cancel_at_kickoff(conn, now)
        _commit(conn)
        counts["cancels"] = self.process_cancels(conn, now)
        _commit(conn)
        counts["expired"] = self.expire_orders(conn, now)
        _commit(conn)
        return counts

    # ------------------------------------------------------------------ submit

    def cancel_approved_under_kill(self, conn: psycopg.Connection) -> int:
        rows = conn.execute("SELECT id FROM orders WHERE status = 'approved' FOR UPDATE SKIP LOCKED").fetchall()
        for row in rows:
            orders.cancel_order(conn, row["id"], self.actor, "kill")
        return len(rows)

    def submit_approved(self, conn: psycopg.Connection, now: datetime) -> int:
        """Move approved rows through the outbox one transaction at a time. The order
        lives until `gtd_seconds` after submission, or until kickoff when
        `trade_pregame_only` is on, whichever comes first."""
        gtd = get_int_setting(conn, "gtd_seconds", 900)
        pregame = get_setting(conn, "trade_pregame_only", True) is not False
        submitted = 0
        while True:
            if killed(conn):
                break
            row = conn.execute(
                "SELECT * FROM orders WHERE status = 'approved' ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"
            ).fetchone()
            if row is None:
                break
            if row["mode"] == "live" and self.limiter is not None and not self.limiter.take("orders", now=now):
                _commit(conn)
                break
            gtd_at = now + timedelta(seconds=gtd)
            kickoff = kickoff_of(conn, row) if pregame else None
            if kickoff is not None and kickoff < gtd_at:
                gtd_at = kickoff
            order = orders.set_status(
                conn, row["id"], "submitting", self.actor, {"gateway": self.gateway.name},
                expected=("approved",), submitted_at=now, gtd_at=gtd_at,
            )
            _commit(conn)
            self._place(conn, order)
            _commit(conn)
            submitted += 1
        return submitted

    def _place(self, conn: psycopg.Connection, order: dict[str, Any]) -> None:
        """Gateway place for one `submitting` row, then `open`; a kill that landed in
        between leaves the row cancelled (paper) or cancel_requested (live)."""
        try:
            exchange_id = self.gateway.place(order)
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
            self._cancel_on_exchange(conn, current, now=utcnow())

    def _reject_by_exchange(self, conn: psycopg.Connection, order: dict[str, Any], reason: str) -> None:
        current = orders.get_order(conn, order["id"], for_update=True)
        orders.release_unfilled(conn, current, note="rejected by exchange")
        orders.set_status(conn, order["id"], "rejected_by_exchange", self.actor, {"reason": reason}, expected=("submitting",), reject_reason=reason[:200])

    # --------------------------------------------------------------- reconcile

    def reconcile_submitting(self, conn: psycopg.Connection, now: datetime) -> int:
        """Rows stuck in `submitting` (a crash or a timed-out place): look them up on
        the exchange by client id and mark `open` when found; never resubmit."""
        rows = conn.execute(
            "SELECT * FROM orders WHERE status = 'submitting' AND submitted_at <= %s FOR UPDATE SKIP LOCKED",
            (now - RECONCILE_AFTER,),
        ).fetchall()
        if not rows:
            return 0
        try:
            remote = {o.get("client_order_id") or o.get("client_request_id"): o for o in self.gateway.open_orders()}
        except Exception as exc:  # noqa: BLE001
            log.warning("reconcile: open_orders failed: %s", exc)
            return 0
        done = 0
        for row in rows:
            if isinstance(self.gateway, PaperGateway):
                exchange_id = self.gateway.place(row)
            else:
                found = remote.get(row["client_request_id"])
                if found is None:
                    continue
                exchange_id = str(found.get("id") or found.get("order_id") or "")
            orders.set_status(conn, row["id"], "open", self.actor, {"reconciled": True, "exchange_order_id": exchange_id}, expected=("submitting",), exchange_order_id=exchange_id)
            done += 1
        return done

    # ----------------------------------------------------------------- kickoff

    def cancel_at_kickoff(self, conn: psycopg.Connection, now: datetime) -> int:
        """With trade_pregame_only: cancel every active order whose game has kicked
        off (actor exchange, reason kickoff), the way a sports book pulls resting
        orders at game start. Paper cancels at once with the release; live rows
        become cancel_requested for process_cancels."""
        if get_setting(conn, "trade_pregame_only", True) is False:
            return 0
        rows = conn.execute(
            """
            SELECT o.id FROM orders o
              JOIN assignments a ON a.id = o.assignment_id
              JOIN games g ON g.game_id = a.game_id
             WHERE o.status IN ('approved', 'submitting', 'open', 'partial')
               AND g.kickoff_at IS NOT NULL AND g.kickoff_at <= %s
             ORDER BY o.created_at FOR UPDATE OF o SKIP LOCKED
            """,
            (now,),
        ).fetchall()
        for row in rows:
            orders.cancel_order(conn, row["id"], self.actor, "kickoff")
        return len(rows)

    # ----------------------------------------------------------------- cancels

    def process_cancels(self, conn: psycopg.Connection, now: datetime) -> int:
        rows = conn.execute("SELECT * FROM orders WHERE status = 'cancel_requested' FOR UPDATE SKIP LOCKED").fetchall()
        done = 0
        for row in rows:
            key = str(row["id"])
            attempts, next_at = self.retries.get(key, (0, 0.0))
            if now.timestamp() < next_at:
                continue
            if self._cancel_on_exchange(conn, row, now):
                self.retries.pop(key, None)
                done += 1
            else:
                delay = RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)]
                self.retries[key] = (attempts + 1, now.timestamp() + delay)
        return done

    def _cancel_on_exchange(self, conn: psycopg.Connection, order: dict[str, Any], now: datetime) -> bool:
        """Confirm a cancel with the gateway; paper is immediate. True when confirmed."""
        if order["status"] != "cancel_requested":
            return True
        if order["mode"] == "live" and self.limiter is not None and not self.limiter.take("cancels", now=now):
            return False
        try:
            ok = self.gateway.cancel(order)
        except Exception as exc:  # noqa: BLE001 - retried on the schedule
            log.warning("cancel of order %s failed: %s", order["id"], exc)
            return False
        if not ok:
            return False
        orders.confirm_cancelled(conn, order["id"], self.actor, {"gateway": self.gateway.name})
        return True

    # ------------------------------------------------------------------ expiry

    def expire_orders(self, conn: psycopg.Connection, now: datetime) -> int:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status IN ('open', 'partial') AND gtd_at IS NOT NULL AND gtd_at <= %s FOR UPDATE SKIP LOCKED",
            (now,),
        ).fetchall()
        expired = 0
        for row in rows:
            if row["mode"] == "live":
                try:
                    if not self.gateway.cancel(row):
                        continue
                except Exception as exc:  # noqa: BLE001
                    log.warning("expiry cancel of order %s failed: %s", row["id"], exc)
                    continue
            orders.release_unfilled(conn, row, note="gtd expired")
            orders.set_status(conn, row["id"], "expired", self.actor, {"gtd_at": row["gtd_at"].isoformat()}, expected=("open", "partial"))
            expired += 1
        return expired


def run_once(conn: psycopg.Connection, gateway: OrderGateway | None = None, now: datetime | None = None) -> dict[str, int]:
    """One executor tick with a throwaway Executor (tests and the CLI)."""
    return Executor(gateway).tick(conn, now)
