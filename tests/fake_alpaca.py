"""An in-memory Alpaca trading host for the stock exchange tests: an `Http` callable
(method, url, headers, body, timeout) -> (status, headers, text), so the real
AlpacaTrading client is exercised.

It keeps orders (market-on-close only, filled by `run_close()` at the settable
`close_prices`, or partly by `fill()`), positions, the account's cash, a clock and a
calendar derived from `now` (weekdays 9:30 to 16:00 New York, `half_days` close at
13:00, `holidays` closed, `extra_days` open), refuses a cls order and its cancel from 15:50 to the close,
and can time out (`timeout_next` "before" or "after" the order is created) or answer
a forced status (`force[path_prefix] = status`).
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

from host.exchange import alpaca_credentials as ac
from host.exchange.adapters.live_http import GatewayTimeout
from host.exchange.alpaca_trading import AlpacaTrading

NY = ZoneInfo("America/New_York")
KEY, SECRET = "PKFAKEKEY0000000000ABCD", "fake-secret-never-printed-987654321"
OPEN_STATES = ("new", "accepted", "pending_new", "partially_filled", "pending_cancel")


def ny(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, dtime(hour, minute), tzinfo=NY)


class FakeAlpaca:
    def __init__(self, now: datetime | None = None, environment: str = "paper", cash: str = "100000") -> None:
        self.now = now or datetime.now(timezone.utc)
        self.environment = environment
        self.cash = Decimal(cash)
        self.positions: dict[str, Decimal] = {}
        self.current_prices: dict[str, Decimal] = {}
        self.close_prices: dict[str, Decimal] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.half_days: set[date] = set()
        self.holidays: set[date] = set()
        self.extra_days: set[date] = set()  # weekend days that trade anyway (tests pinned to the real date)
        self.timeout_next: str | None = None
        self.force: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []

    # ------------------------------------------------------------------ helpers

    def client(self) -> AlpacaTrading:
        base = ac.LIVE_URL if self.environment == "live" else ac.PAPER_URL
        creds = ac.load({ac.KEY_VAR: KEY, ac.SECRET_VAR: SECRET, ac.BASE_URL_VAR: base})
        return AlpacaTrading(creds, http=self)

    def trading_day(self, day: date) -> bool:
        return day in self.extra_days or (day.weekday() < 5 and day not in self.holidays)

    def close_of(self, day: date) -> datetime:
        return ny(day, 13) if day in self.half_days else ny(day, 16)

    def after_cutoff(self) -> bool:
        local = self.now.astimezone(NY)
        day = local.date()
        return self.trading_day(day) and self.close_of(day) - timedelta(minutes=10) <= self.now < self.close_of(day)

    def clock(self) -> dict[str, Any]:
        local = self.now.astimezone(NY)
        day = local.date()
        is_open = self.trading_day(day) and ny(day, 9, 30) <= self.now < self.close_of(day)
        d = day if self.trading_day(day) and self.now < self.close_of(day) else day + timedelta(days=1)
        while not self.trading_day(d):
            d += timedelta(days=1)
        o = day if self.trading_day(day) and self.now < ny(day, 9, 30) else day + timedelta(days=1)
        while not self.trading_day(o):
            o += timedelta(days=1)
        return {"timestamp": local.isoformat(), "is_open": is_open, "next_open": ny(o, 9, 30).isoformat(),
                "next_close": self.close_of(d).isoformat()}

    def by_client(self, client_id: str) -> dict[str, Any] | None:
        return next((o for o in self.orders.values() if o["client_order_id"] == client_id), None)

    def place_direct(self, symbol: str, qty: int, side: str = "buy", client_id: str | None = None) -> dict[str, Any]:
        """An order placed by someone else (the owner by hand): not ours."""
        return self._create({"symbol": symbol, "qty": str(qty), "side": side, "type": "market", "time_in_force": "cls",
                             "client_order_id": client_id or uuid.uuid4().hex})

    def _create(self, body: dict[str, Any]) -> dict[str, Any]:
        order = {"id": str(uuid.uuid4()), "client_order_id": body["client_order_id"], "symbol": body["symbol"],
                 "qty": body["qty"], "side": body["side"], "type": body["type"], "time_in_force": body["time_in_force"],
                 "status": "accepted", "filled_qty": "0", "filled_avg_price": None, "filled_at": None,
                 "created_at": self.now.isoformat()}
        self.orders[order["id"]] = order
        return order

    def fill(self, order_id: str, qty: int, price: str) -> None:
        """Fill `qty` more shares of an open order at `price` (cumulative average kept)."""
        o = self.orders[order_id]
        before = Decimal(o["filled_qty"])
        avg = Decimal(o["filled_avg_price"] or "0")
        total = before + qty
        o["filled_avg_price"] = str((avg * before + Decimal(price) * qty) / total)
        o["filled_qty"] = str(int(total))
        o["filled_at"] = self.now.isoformat()
        o["status"] = "filled" if total == Decimal(o["qty"]) else "partially_filled"
        sign = 1 if o["side"] == "buy" else -1
        self.positions[o["symbol"]] = self.positions.get(o["symbol"], Decimal(0)) + sign * qty
        self.cash -= sign * qty * Decimal(price)

    def run_close(self) -> int:
        """Fill every open cls order at its symbol's close price."""
        n = 0
        for o in self.orders.values():
            if o["status"] in OPEN_STATES and o["status"] != "pending_cancel":
                left = int(Decimal(o["qty"]) - Decimal(o["filled_qty"]))
                self.fill(o["id"], left, str(self.close_prices[o["symbol"]]))
                n += 1
        return n

    # --------------------------------------------------------------------- http

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        assert headers["APCA-API-KEY-ID"] == KEY and headers["APCA-API-SECRET-KEY"] == SECRET
        parts = urlsplit(url)
        path, query = unquote(parts.path), {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append((method, path))
        for prefix, status in self.force.items():
            if path.startswith(prefix):
                return status, {"Retry-After": "3"} if status == 429 else {}, json.dumps({"message": f"forced {status} {KEY}"})
        if method == "POST" and path == "/v2/orders":
            return self._post(json.loads(body or b"{}"))
        if method == "DELETE" and path.startswith("/v2/orders/"):
            return self._delete(path.rsplit("/", 1)[1])
        if path == "/v2/orders:by_client_order_id":
            o = self.by_client(query.get("client_order_id", ""))
            return (200, {}, json.dumps(o)) if o else (404, {}, '{"message": "order not found"}')
        if path.startswith("/v2/orders/"):
            o = self.orders.get(path.rsplit("/", 1)[1])
            return (200, {}, json.dumps(o)) if o else (404, {}, '{"message": "order not found"}')
        if path == "/v2/orders":
            return 200, {}, json.dumps(self._list(query))
        if path == "/v2/positions":
            return 200, {}, json.dumps([{"symbol": s, "qty": str(q), "current_price": str(self.current_prices.get(s, "0"))}
                                        for s, q in self.positions.items() if q != 0])
        if path == "/v2/account":
            value = sum((q * self.current_prices.get(s, Decimal(0)) for s, q in self.positions.items()), Decimal(0))
            return 200, {}, json.dumps({"status": "ACTIVE", "cash": str(self.cash), "equity": str(self.cash + value),
                                        "buying_power": str(self.cash * 2), "pattern_day_trader": False, "daytrade_count": 0})
        if path == "/v2/clock":
            return 200, {}, json.dumps(self.clock())
        if path == "/v2/calendar":
            return 200, {}, json.dumps(self._calendar(date.fromisoformat(query["start"]), date.fromisoformat(query["end"])))
        return 404, {}, '{"message": "not found"}'

    def _post(self, body: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        if self.timeout_next == "before":
            self.timeout_next = None
            raise GatewayTimeout("POST timed out")
        assert body["type"] == "market" and body["time_in_force"] == "cls" and body["qty"].isdigit()
        if self.after_cutoff():
            return 422, {}, '{"message": "cls orders are not accepted after 15:50"}'
        if self.by_client(body["client_order_id"]):
            return 422, {}, '{"message": "client_order_id must be unique"}'
        if body["side"] == "buy" and Decimal(body["qty"]) * self.close_prices.get(body["symbol"], Decimal(0)) > self.cash * 2:
            return 403, {}, '{"message": "insufficient buying power"}'
        order = self._create(body)
        if self.timeout_next == "after":
            self.timeout_next = None
            raise GatewayTimeout("POST timed out")
        return 200, {}, json.dumps(order)

    def _delete(self, order_id: str) -> tuple[int, dict[str, str], str]:
        o = self.orders.get(order_id)
        if o is None:
            return 404, {}, '{"message": "order not found"}'
        if o["status"] not in OPEN_STATES or (o["time_in_force"] == "cls" and self.after_cutoff()):
            return 422, {}, '{"message": "order cannot be cancelled"}'
        o["status"] = "canceled"
        return 204, {}, ""

    def _list(self, query: dict[str, str]) -> list[dict[str, Any]]:
        status = query.get("status", "open")
        out = [o for o in self.orders.values()
               if status == "all" or (status == "open") == (o["status"] in OPEN_STATES)]
        after = query.get("after")
        if after:
            cut = datetime.fromisoformat(after.replace("Z", "+00:00"))
            out = [o for o in out if datetime.fromisoformat(o["created_at"]) > cut]
        return out[: int(query.get("limit", 500))]

    def _calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        out, d = [], start
        while d <= end:
            if self.trading_day(d):
                out.append({"date": d.isoformat(), "open": "09:30", "close": "13:00" if d in self.half_days else "16:00"})
            d += timedelta(days=1)
        return out
