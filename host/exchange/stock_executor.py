"""The stock order outbox of the exchange process (contract section 6), modelled on the
NFL executor (host/exchange/executor.py).

Every tick: `approved -> submitting` (committed) -> place at Alpaca -> `open` with the
exchange order id; sells of a batch go before its buys. A place that times out (or
answers 5xx) stays `submitting` and is reconciled by client_order_id (the order id):
found -> open, not found after SUBMIT_GRACE -> expired with the release; it is never
resubmitted blind. A definite refusal (4xx) -> rejected_by_exchange with the release; a
429 puts the row back to approved for the next tick. Nothing is submitted while the
kill switch is on (the flag is read FOR SHARE, so a kill in flight is waited for), nor
while the broker check is stale (the row waits for a good check). An approved row is
cancelled with the release when it is found under kill, of another environment, of
another session than the broker's, past Alpaca's market-on-close cutoff (next_close -
10 minutes, by the last broker check even when it is stale: a cls order sent later
would be refused or queued for the next session's close), or when an order of the
opposite side in the same symbol is active on the same account (one Alpaca account is
shared by the assignments of a mode, and Alpaca refuses it as a potential wash trade).

`cancel_requested` rows are cancelled at Alpaca: accepted -> the order is read back and
closed from what Alpaca says (fills first); refused (Alpaca's 422, a cls order after
15:50) -> back to open (partial when it has fills) with reason "cancel refused (after
15:50)", and it fills at the close like any open order (not while Alpaca shows the
order pending_cancel: a second cancel of it is refused too). The poll (every
stock_orders_poll_s) reads our open, partial and cancel_requested orders at Alpaca:
filled_qty and filled_avg_price changes are booked through stock_fills.book_fill
(fill id "<exchange_order_id>:<cumulative filled_qty>"), canceled / expired / rejected
close the row as cancelled / expired / rejected_by_exchange with the release.

After submit, every step touches only rows of the keys' environment (mode ==
client.environment): rows of the other mode belong to another Alpaca account and wait,
untouched, for its keys (the broker reconciliation warns about them). Rows are locked
assignment first, then the order, the lock order of the approval and the kill.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import psycopg

from host.exchange.alpaca_trading import AlpacaAuthError, AlpacaRateLimited, AlpacaRejected
from host.exchange.stock_rows import ACTIVE, ACTOR, apply_remote, close, lock_pair, set_status
from host.stocks import market
from host.stocks import orders as stock_orders

log = logging.getLogger(__name__)

RECONCILE_AFTER = timedelta(seconds=5)
SUBMIT_GRACE = timedelta(seconds=60)
RESEND_AFTER = timedelta(seconds=30)
RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
MAX_SINGLE_LOOKUPS = 20
PLACE_CUTOFF = timedelta(minutes=10)  # Alpaca refuses cls orders from 15:50 (close - 10 min)
REFUSED = "cancel refused (after 15:50)"


def _commit(conn: psycopg.Connection) -> None:
    if not conn.autocommit:
        conn.commit()


def killed(conn: psycopg.Connection) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key = 'kill_switch' FOR SHARE").fetchone()
    return row is not None and row["value"] is True


class StockExecutor:
    """Holds the cancel retry schedule and the cancels already sent between ticks."""

    def __init__(self) -> None:
        self.retries: dict[str, tuple[int, float]] = {}
        self.sent: dict[str, datetime] = {}

    def tick(self, conn: psycopg.Connection, client: Any, now: datetime, poll: bool = True) -> dict[str, Any]:
        counts: dict[str, Any] = {"submitted": 0, "cancelled_under_kill": 0, "reconciled": 0, "cancels": 0, "polled": 0,
                                  "error": None}
        errors: list[str] = []
        if killed(conn):
            counts["cancelled_under_kill"] = len(stock_orders.cancel_orders(conn, ACTOR, "kill", approved_only=True)["cancelled"])
        else:
            counts["submitted"] = self.submit_approved(conn, client, now)
        _commit(conn)
        for name, step in (("reconciled", self.reconcile_submitting), ("cancels", self.process_cancels)) + (
                (("polled", self.poll),) if poll else ()):
            try:
                counts[name] = step(conn, client, now)
            except Exception as exc:  # noqa: BLE001 - one failing step must not stop the others
                if not conn.autocommit:
                    conn.rollback()
                log.warning("stock executor %s failed: %s", name, exc)
                errors.append(f"{name}: {exc}")
            _commit(conn)
        counts["error"] = "; ".join(errors)[:500] or None
        return counts

    # ------------------------------------------------------------------ submit

    def _stale(self, conn: psycopg.Connection, row: dict[str, Any], client: Any, broker: dict[str, Any] | None,
               now: datetime) -> str | None:
        """Why an approved row can never be placed (None when it can, or may later)."""
        if row["mode"] != client.environment:
            return f"the Alpaca keys are {client.environment} keys, the order is {row['mode']}"
        if broker is None or broker.get("next_close") is None or broker.get("session_date") is None:
            return "no broker check"
        if broker["session_date"] != row["session_date"]:
            return "its session is over"
        if now > broker["next_close"] - PLACE_CUTOFF:
            return "past the market-on-close cutoff (15:50)"
        other = conn.execute(
            "SELECT assignment_id, side FROM stock_orders WHERE mode = %s AND symbol = %s AND side <> %s AND status = ANY(%s)"
            " AND id <> %s ORDER BY created_at LIMIT 1",
            (row["mode"], row["symbol"], row["side"], list(ACTIVE), row["id"])).fetchone()
        if other is not None:
            return (f"wash: a {other['side']} order of assignment {other['assignment_id']} in {row['symbol']} is active"
                    " on the shared Alpaca account")
        return None

    def submit_approved(self, conn: psycopg.Connection, client: Any, now: datetime) -> int:
        submitted = 0
        found = conn.execute("SELECT session_date, next_close, checked_at FROM stock_broker_state WHERE id = 1").fetchone()
        broker = dict(found) if found is not None else None
        fresh = broker is not None and not market.broker_stale(broker, now, market.max_broker_age_s(conn))
        while not killed(conn) and client.backoff_remaining() <= 0:
            row = conn.execute(
                "SELECT * FROM stock_orders WHERE status = 'approved' ORDER BY created_at, side = 'buy', id"
                " LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
            if row is None:
                break
            why = self._stale(conn, dict(row), client, broker, now)
            if why:
                _commit(conn)
                close(conn, row["id"], "cancelled", why)
                _commit(conn)
                continue
            if not fresh:
                break  # a stale broker check: the row waits for a good one (the cutoff above ends the wait)
            order = set_status(conn, row["id"], "submitting", ("approved",), {"environment": client.environment})
            _commit(conn)
            if order is not None:
                self._place(conn, client, order)
                submitted += 1
            _commit(conn)
        return submitted

    def _place(self, conn: psycopg.Connection, client: Any, order: dict[str, Any]) -> None:
        try:
            eid = client.place(order)
        except AlpacaRateLimited as exc:
            set_status(conn, order["id"], "approved", ("submitting",), {"retry": str(exc)})
            return
        except (AlpacaRejected, AlpacaAuthError) as exc:
            close(conn, order["id"], "rejected_by_exchange", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - timeout or 5xx: reconciled by client id, never resubmitted
            log.warning("place of stock order %s left submitting: %s", order["id"], exc)
            return
        if set_status(conn, order["id"], "open", ("submitting",), {"exchange_order_id": eid}, exchange_order_id=eid) is None:
            conn.execute("UPDATE stock_orders SET exchange_order_id = %s, updated_at = now() WHERE id = %s", (eid, order["id"]))

    # --------------------------------------------------------------- reconcile

    def reconcile_submitting(self, conn: psycopg.Connection, client: Any, now: datetime) -> int:
        """Rows whose place may or may not have reached Alpaca (`submitting`, or
        cancel_requested without an exchange id), looked up by client_order_id."""
        rows = conn.execute(
            """
            SELECT o.*, (SELECT max(e.ts) FROM stock_order_events e WHERE e.order_id = o.id AND e.to_status = 'submitting') AS sent_at
              FROM stock_orders o
             WHERE o.mode = %s AND (o.status = 'submitting' OR (o.status = 'cancel_requested' AND o.exchange_order_id IS NULL))
             ORDER BY o.created_at
            """, (client.environment,)).fetchall()
        done = 0
        for row in rows:
            sent = row["sent_at"] or row["updated_at"]
            if sent > now - RECONCILE_AFTER:
                continue
            remote = client.order_by_client_id(str(row["id"]))
            if remote is not None:
                eid = str(remote.get("id"))
                lock_pair(conn, row["id"])
                if set_status(conn, row["id"], "open", ("submitting",), {"reconciled": True, "exchange_order_id": eid},
                              exchange_order_id=eid) is None:
                    conn.execute("UPDATE stock_orders SET exchange_order_id = %s WHERE id = %s", (eid, row["id"]))
                apply_remote(conn, row["id"], remote, now)
                done += 1
            elif sent <= now - SUBMIT_GRACE:
                to_status = "expired" if row["status"] == "submitting" else "cancelled"
                close(conn, row["id"], to_status, "never seen at Alpaca", {"grace_s": SUBMIT_GRACE.seconds})
                done += 1
            _commit(conn)
        return done

    # ----------------------------------------------------------------- cancels

    def process_cancels(self, conn: psycopg.Connection, client: Any, now: datetime) -> int:
        rows = conn.execute("SELECT * FROM stock_orders WHERE status = 'cancel_requested' AND exchange_order_id IS NOT NULL"
                            " AND mode = %s ORDER BY created_at", (client.environment,)).fetchall()
        done = 0
        for row in rows:
            key = str(row["id"])
            sent = self.sent.get(key)
            attempts, next_at = self.retries.get(key, (0, 0.0))
            if (sent is not None and now - sent < RESEND_AFTER) or now.timestamp() < next_at:
                continue
            try:
                accepted = client.cancel(row["exchange_order_id"])
                remote = client.order(row["exchange_order_id"])
            except Exception as exc:  # noqa: BLE001 - retried on the schedule
                log.warning("cancel of stock order %s failed: %s", key, exc)
                self.retries[key] = (attempts + 1, now.timestamp() + RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)])
                continue
            self.retries.pop(key, None)
            self.sent[key] = now
            status = apply_remote(conn, row["id"], remote, now) if remote is not None else row["status"]
            if not accepted and status == "cancel_requested" and (remote or {}).get("status") != "pending_cancel":
                current = conn.execute("SELECT filled_qty FROM stock_orders WHERE id = %s", (row["id"],)).fetchone()
                set_status(conn, row["id"], "partial" if current["filled_qty"] else "open", ("cancel_requested",),
                           {"reason": REFUSED}, reason=REFUSED)
                self.sent.pop(key, None)
            done += 1
            _commit(conn)
        for key in [k for k, t in self.sent.items() if now - t > RESEND_AFTER * 10]:
            self.sent.pop(key, None)
        return done

    # -------------------------------------------------------------------- poll

    def poll(self, conn: psycopg.Connection, client: Any, now: datetime) -> int:
        """Our orders at Alpaca: fills booked, closed orders closed."""
        rows = conn.execute("SELECT * FROM stock_orders WHERE status IN ('open', 'partial', 'cancel_requested')"
                            " AND exchange_order_id IS NOT NULL AND mode = %s ORDER BY created_at",
                            (client.environment,)).fetchall()
        if not rows:
            return 0
        listed = client.orders("all", min(r["created_at"] for r in rows) - timedelta(seconds=60))
        by_id = {str(o.get("id")): o for o in listed}
        lookups = 0
        for row in rows:
            remote = by_id.get(str(row["exchange_order_id"]))
            if remote is None and lookups < MAX_SINGLE_LOOKUPS:
                lookups += 1
                remote = client.order(row["exchange_order_id"])
            if remote is not None:
                apply_remote(conn, row["id"], remote, now)
                _commit(conn)
        return len(rows)
