"""A scripted live gateway for the step 5 tests (docs/LIVE.md): the remote order store,
a fills queue, a balance payload, call counters and failure injection. Returns the
plain dicts the real LiveGateway returns (open_orders, fills, balance)."""
from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from host.exchange.adapters.base import OrderGateway, RateLimited, SourceError
from host.exchange.main import ExchangeLoop


class FakeAuthError(SourceError):
    """What the real gateway raises on 401/403 (its AuthError is also a SourceError)."""


class FakeCredentials:
    """Stands in for host.exchange.credentials.Credentials in the loop (never printed)."""

    key_hint = "ab12"

    def __repr__(self) -> str:
        return "Credentials(key=***ab12, secret=<redacted>)"


class FakeLiveGateway(OrderGateway):
    """Scripted behaviours: `place_mode` is "ok" (accepted, id returned), "timeout"
    (accepted by the exchange, TimeoutError raised to the caller), "lost" (TimeoutError,
    nothing placed), "auth" (FakeAuthError) or "429" (RateLimited). `fail_next(method,
    exc)` queues one exception for the next call of that method. `cancel_results`
    scripts cancel outcomes (bool, exception or "ack_only": ok answered, order still
    listed) ahead of the default True. `log` is every call in order."""

    name = "polymarket_us"

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.remote: dict[str, dict[str, Any]] = {}
        self.fill_queue: list[dict[str, Any]] = []
        self.balance_payload: dict[str, Any] = {"balance_cents": 100_000, "buying_power_cents": 100_000, "server_time": None}
        self.last_skew_ms: int = 0
        self.calls: Counter[str] = Counter()
        self.log: list[str] = []
        self.placed: list[str] = []
        self.cancelled: list[str] = []
        self.place_mode = "ok"
        self.cancel_results: list[Any] = []
        self._fail: dict[str, list[BaseException]] = defaultdict(list)
        self._ids = itertools.count(1)
        self._fill_ids = itertools.count(1)

    # ------------------------------------------------------------- scripting

    def fail_next(self, method: str, exc: BaseException) -> None:
        self._fail[method].append(exc)

    def set_skew_ms(self, ms: int) -> None:
        self.last_skew_ms = int(ms)

    def add_remote_order(
        self, client_id: str | None = None, exchange_order_id: str | None = None, market_ref: str = "m-1",
        price: float = 0.5, size: int = 1, filled_size: int = 0, status: str = "open",
    ) -> dict[str, Any]:
        """An order resting on the exchange (ours when `client_id` matches a row)."""
        exchange_order_id = exchange_order_id or f"ex-{next(self._ids)}"
        row = {
            "exchange_order_id": exchange_order_id, "client_order_id": client_id, "market_ref": market_ref,
            "price": price, "size": size, "filled_size": filled_size, "status": status,
        }
        self.remote[exchange_order_id] = row
        return row

    def add_fill(
        self, client_id: str | None, price: float, size: int, fee_cents: int = 0, exchange_order_id: str | None = None,
        fill_id: str | None = None, ts: datetime | None = None,
    ) -> dict[str, Any]:
        """A fill the next fills() call returns (also bumps the remote order's filled_size)."""
        if exchange_order_id is None:
            exchange_order_id = next((eid for eid, o in self.remote.items() if o["client_order_id"] == client_id), None)
        fill = {
            "exchange_fill_id": fill_id or f"fill-{next(self._fill_ids)}", "exchange_order_id": exchange_order_id,
            "client_order_id": client_id, "price": price, "size": size, "fee_cents": fee_cents, "ts": ts or self.clock(),
        }
        self.fill_queue.append(fill)
        remote = self.remote.get(exchange_order_id or "")
        if remote is not None:
            remote["filled_size"] = int(remote["filled_size"]) + size
            if remote["filled_size"] >= int(remote["size"]):
                self.remote.pop(exchange_order_id, None)
        return fill

    def _check(self, method: str) -> None:
        self.calls[method] += 1
        self.log.append(method)
        queued = self._fail.get(method)
        if queued:
            raise queued.pop(0)

    # ---------------------------------------------------------- OrderGateway

    def place(self, order: dict[str, Any]) -> str:
        self._check("place")
        client_id = order["client_request_id"]
        self.placed.append(client_id)
        if self.place_mode == "auth":
            raise FakeAuthError("place answered 401")
        if self.place_mode == "429":
            raise RateLimited("place answered 429")
        if self.place_mode == "lost":
            raise TimeoutError("place timed out (nothing reached the exchange)")
        row = self.add_remote_order(client_id, market_ref=str(order.get("market_id")), price=float(order["price"]), size=int(order["size"]))
        if self.place_mode == "timeout":
            raise TimeoutError("place timed out (the exchange accepted it)")
        return row["exchange_order_id"]

    def cancel(self, order: dict[str, Any]) -> bool:
        self._check("cancel")
        if self.cancel_results:
            outcome = self.cancel_results.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == "ack_only":
                return True  # the exchange said ok but still lists the order
            if not outcome:
                return False
        eid = order.get("exchange_order_id")
        if eid is None:
            eid = next((e for e, o in self.remote.items() if o["client_order_id"] == order.get("client_request_id")), None)
        self.cancelled.append(str(eid))
        self.remote.pop(str(eid), None)
        return True

    def open_orders(self) -> list[dict[str, Any]]:
        self._check("open_orders")
        return [dict(o) for o in self.remote.values()]

    def fills(self, since: datetime | None) -> list[dict[str, Any]]:
        self._check("fills")
        return [dict(f) for f in self.fill_queue if since is None or f["ts"] >= since]

    def balance(self) -> dict[str, Any]:
        self._check("balance")
        return dict(self.balance_payload)

    def cancel_all(self) -> int:
        self._check("cancel_all")
        n = len(self.remote)
        self.cancelled.extend(self.remote)
        self.remote.clear()
        return n

    def probe_account(self) -> dict[str, Any]:
        self._check("probe_account")
        return {"status": 200, "payload": '{"balance": "1000.00"}', "key_hint": "ab12", "error": None}


def live_loop(pool: Any, gateway: FakeLiveGateway | None = None, clock: Callable[[], datetime] | None = None) -> ExchangeLoop:
    """An ExchangeLoop whose live gateway is `gateway` and whose credentials "load"."""
    gateway = gateway or FakeLiveGateway(clock)
    loop = ExchangeLoop(pool, clock=clock or (lambda: datetime.now(timezone.utc)), gateway_factory=lambda config, creds: gateway)
    loop.load_credentials = lambda: FakeCredentials()  # type: ignore[method-assign]
    return loop
