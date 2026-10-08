"""The Alpaca trading client of the exchange process (contract section 6): orders,
positions, the account, the clock and the calendar on the trading host of the
credentials (paper-api for paper keys, api for live keys).

Same request, error and redaction style as host.exchange.alpaca_data: the key headers
of host.exchange.alpaca_credentials, every error text redacted. Errors:

- AlpacaAuthError: 401, or 403 on a GET (the keys are wrong or lack the permission).
- AlpacaRateLimited: 429. The client then backs off (Retry-After, else 2, 4, ... 60 s)
  and refuses every call without a request until the backoff is over.
- AlpacaRejected: a definite refusal of an order (4xx on POST /v2/orders, 403 included:
  Alpaca answers 403 for insufficient buying power). Nothing was created.
- AlpacaTimeout: no answer in time. For a POST the order may or may not exist: the
  caller looks it up by client_order_id and never resubmits blind.
- AlpacaTradingError: anything else (5xx, transport, a body that is not JSON).
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from typing import Any, Callable
from urllib.parse import quote, urlencode

from host.exchange.adapters.base import SourceError, truncate
from host.exchange.adapters.live_http import GatewayTimeout, Http, header, urllib_http
from host.exchange.alpaca_credentials import AlpacaCredentials

TIMEOUT_S = 10.0
BACKOFF_MIN_S = 2.0
BACKOFF_MAX_S = 60.0
ORDERS_LIMIT = 500


class AlpacaTradingError(SourceError):
    """Alpaca did not answer as expected (never carries a key: every text is redacted)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AlpacaAuthError(AlpacaTradingError):
    """401/403: the keys were refused."""


class AlpacaRateLimited(AlpacaTradingError):
    """429, or a call made during the backoff that followed one."""


class AlpacaRejected(AlpacaTradingError):
    """A definite refusal of an order: nothing was created at Alpaca."""


class AlpacaTimeout(AlpacaTradingError, TimeoutError):
    """No answer in time: a POST may or may not have created the order."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AlpacaTrading:
    """`AlpacaTrading(creds, http=None)`: one instance per exchange process, holding the
    rate-limit backoff between calls. `monotonic` is the backoff's clock (tests replace it)."""

    def __init__(self, creds: AlpacaCredentials, http: Http | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.creds = creds
        self.http = http or urllib_http
        self.monotonic = monotonic
        self.backoff_until = 0.0
        self.backoff_s = 0.0
        self.requests = 0

    @property
    def environment(self) -> str:
        return self.creds.environment

    def backoff_remaining(self) -> float:
        return max(0.0, self.backoff_until - self.monotonic())

    def _backoff(self, headers: dict[str, str]) -> float:
        retry = header(headers, "Retry-After")
        try:
            wait = float(retry) if retry is not None else 0.0
        except ValueError:
            wait = 0.0
        self.backoff_s = min(BACKOFF_MAX_S, max(BACKOFF_MIN_S, self.backoff_s * 2, wait))
        self.backoff_until = self.monotonic() + self.backoff_s
        return self.backoff_s

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        """(status, parsed JSON or None) for a 2xx or a 404/422 answer; raises otherwise."""
        what = f"{method} {path.split('?')[0]}"
        if self.backoff_remaining() > 0:
            raise AlpacaRateLimited(f"{what}: rate limited, backing off {self.backoff_remaining():.0f} s more", 429)
        headers = self.creds.headers()
        data = None
        if body is not None:
            headers = {**headers, "Content-Type": "application/json"}
            data = json.dumps(body).encode()
        self.requests += 1
        try:
            status, response_headers, text = self.http(method, self.creds.base_url + path, headers, data, TIMEOUT_S)
        except GatewayTimeout as exc:
            raise AlpacaTimeout(self.creds.redact(f"{what} timed out: {exc}") or what) from None
        except SourceError as exc:
            raise AlpacaTradingError(self.creds.redact(f"{what} failed: {exc}") or what) from None
        text = self.creds.redact(text) or ""
        if status == 429:
            wait = self._backoff(response_headers)
            raise AlpacaRateLimited(f"{what} answered 429 (rate limited), backing off {wait:.0f} s", 429)
        self.backoff_s = 0.0
        if status == 401 or (status == 403 and method == "GET"):
            raise AlpacaAuthError(f"{what} answered {status}: {truncate(text, 300)}", status)
        if status in (404, 422) or (status == 403 and method == "POST") or 200 <= status < 300:
            try:
                return status, json.loads(text) if text.strip() else None
            except ValueError:
                if 200 <= status < 300:
                    raise AlpacaTradingError(f"{what} answered a body that is not JSON", status) from None
                return status, {"message": truncate(text, 300)}
        raise AlpacaTradingError(f"{what} answered {status}: {truncate(text, 300)}", status)

    def _get(self, path: str) -> Any:
        status, data = self._request("GET", path)
        if not 200 <= status < 300:
            message = data.get("message") if isinstance(data, dict) else None
            raise AlpacaTradingError(f"GET {path.split('?')[0]} answered {status}: {truncate(str(message), 300)}", status)
        return data

    # ------------------------------------------------------------------ orders

    def place(self, order: dict[str, Any]) -> str:
        """POST /v2/orders: a whole-share market-on-close order whose client_order_id is
        the stock_orders id; the exchange order id."""
        body = {"symbol": str(order["symbol"]), "qty": str(int(order["qty"])), "side": str(order["side"]),
                "type": "market", "time_in_force": "cls", "client_order_id": str(order["id"])}
        status, data = self._request("POST", "/v2/orders", body)
        if not 200 <= status < 300:
            message = data.get("message") if isinstance(data, dict) else None
            raise AlpacaRejected(f"order refused ({status}): {truncate(str(message or data), 300)}", status)
        exchange_id = data.get("id") if isinstance(data, dict) else None
        if not exchange_id:
            raise AlpacaTradingError("POST /v2/orders answered without an order id", status)
        return str(exchange_id)

    def cancel(self, exchange_order_id: str) -> bool:
        """DELETE /v2/orders/{id}: True when Alpaca accepted the cancel, False when it
        answered 422 (the order cannot be cancelled: after 15:50 for cls, or already
        done). 404 raises."""
        status, data = self._request("DELETE", f"/v2/orders/{quote(str(exchange_order_id), safe='')}")
        if status == 422:
            return False
        if status == 404:
            raise AlpacaTradingError(f"cancel: order {exchange_order_id} not found at Alpaca", 404)
        return True

    def order(self, exchange_order_id: str) -> dict[str, Any] | None:
        """GET /v2/orders/{id}; None when Alpaca does not know it."""
        status, data = self._request("GET", f"/v2/orders/{quote(str(exchange_order_id), safe='')}")
        return data if 200 <= status < 300 and isinstance(data, dict) else None

    def order_by_client_id(self, client_id: str) -> dict[str, Any] | None:
        """GET /v2/orders:by_client_order_id; None when Alpaca does not know it."""
        status, data = self._request("GET", "/v2/orders:by_client_order_id?" + urlencode({"client_order_id": client_id}))
        return data if 200 <= status < 300 and isinstance(data, dict) else None

    def orders(self, status: str = "open", after: datetime | None = None) -> list[dict[str, Any]]:
        """GET /v2/orders (status open, closed or all; oldest first, at most 500)."""
        query: dict[str, Any] = {"status": status, "limit": ORDERS_LIMIT, "direction": "asc", "nested": "false"}
        if after is not None:
            query["after"] = _iso(after)
        data = self._get("/v2/orders?" + urlencode(query))
        if not isinstance(data, list):
            raise AlpacaTradingError("GET /v2/orders: unexpected answer (not a list)")
        return [o for o in data if isinstance(o, dict)]

    # ------------------------------------------------------- account and clock

    def positions(self) -> list[dict[str, Any]]:
        data = self._get("/v2/positions")
        if not isinstance(data, list):
            raise AlpacaTradingError("GET /v2/positions: unexpected answer (not a list)")
        return [p for p in data if isinstance(p, dict)]

    def account(self) -> dict[str, Any]:
        data = self._get("/v2/account")
        if not isinstance(data, dict):
            raise AlpacaTradingError("GET /v2/account: unexpected answer")
        return data

    def clock(self) -> dict[str, Any]:
        data = self._get("/v2/clock")
        if not isinstance(data, dict):
            raise AlpacaTradingError("GET /v2/clock: unexpected answer")
        return data

    def calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        """GET /v2/calendar: the trading days with their open and close (New York)."""
        data = self._get("/v2/calendar?" + urlencode({"start": start.isoformat(), "end": end.isoformat()}))
        if not isinstance(data, list):
            raise AlpacaTradingError("GET /v2/calendar: unexpected answer (not a list)")
        return [d for d in data if isinstance(d, dict)]
