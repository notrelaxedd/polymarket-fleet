"""The authenticated Polymarket US gateway (docs/LIVE.md, "Live gateway").

Everything about the real API is UNVERIFIED: paths, methods, request and response
field names come from `market_source_config.polymarket_us.live`, the signing rules from
`.auth` (defaults in polymarket_us.LIVE_DEFAULTS and signing.AUTH_DEFAULTS). Responses
are parsed defensively (live_parse); an unknown shape raises SourceError with the
truncated payload in the message (and at DEBUG in the log), 401/403 raise AuthError,
429 raises RateLimited (after telling the limiter), 5xx and transport failures raise
SourceError, a timeout raises GatewayTimeout (a SourceError and a TimeoutError). Every
call takes a token from the fleet-wide limiter by category when one is given: `orders`
for place, `cancels` for cancel, `account` for open_orders, fills and balance. The
`Date` header of every answer (or the body field named by `auth.server_time_field`)
updates `last_skew_ms`; with `max_skew_ms` set, a skew over it refuses to sign a place
(cancels, listings and fills keep going so a kill can still reach the exchange). The
destination is pinned by live_policy: https and a polymarket.us host, or the
`POLYMARKET_US_LIVE_BASE_URL` override from exchange.env. Keys are never logged or
returned: every message built from a response body goes through `_redact`.
"""
from __future__ import annotations

import email.utils
import json
import logging
import socket
import time
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode

from host.exchange.adapters import live_parse as lp
from host.exchange.adapters.base import NotConfigured, OrderGateway, RateLimited, SourceError, parse_time, truncate, utcnow
from host.exchange.adapters.live_http import GatewayTimeout, Http, header, urllib_http
from host.exchange.adapters.live_policy import base_url_problem, env_base_url, env_override_host
from host.exchange.adapters.polymarket_us import LIVE_DEFAULTS, live_config_with_defaults, pick
from host.exchange.adapters.signing import sign_headers, timestamp_now
from host.exchange.credentials import Credentials

__all__ = ["AuthError", "GatewayTimeout", "LiveGateway", "live_config_with_defaults", "urllib_http"]

log = logging.getLogger(__name__)

NAME = "polymarket_us"
PAYLOAD_LIMIT = 64 * 1024
REQUIRED_REQUEST_FIELDS = ("client_order_id", "market_id", "side", "price", "size", "time_in_force")


def urlsplit_host(url: str) -> str | None:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname


class AuthError(SourceError):
    """The exchange refused the credentials or the signature (401 or 403)."""


class LiveGateway(OrderGateway):
    name = NAME

    def __init__(
        self,
        creds: Credentials,
        config: dict[str, Any] | None,
        limiter: Any = None,
        http: Http | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_skew_ms: int | None = None,
        base_url: str | None = None,
    ) -> None:
        self.creds = creds
        self.config = live_config_with_defaults(config)
        self.auth = self.config["auth"]
        self.live = self.config["live"]
        self.limiter = limiter
        self.http: Http = http or urllib_http
        self.clock = clock or utcnow
        self.sleep = sleep
        self.last_skew_ms: int | None = None
        self.last_server_time: datetime | None = None
        self.max_skew_ms: int | None = max_skew_ms
        self.last_status: int | None = None
        self.last_payload: str | None = None
        # The env override (exchange.env) wins over the database; both are checked.
        override = base_url if base_url is not None else env_base_url()
        self.base_url = str(override or self.live["base_url"]).rstrip("/")
        self.base_url_problem = base_url_problem(self.base_url, env_override_host() if base_url is None else urlsplit_host(base_url))

    # --------------------------------------------------------------- plumbing

    def endpoint(self, name: str) -> tuple[str, str, dict[str, Any]]:
        """(method, path, extras) for a configured endpoint; "METHOD /path" accepted."""
        spec = self.live.get(name)
        if isinstance(spec, str):
            method, _, path = spec.strip().partition(" ")
            spec = {"method": method, "path": path.strip()}
        if not isinstance(spec, dict) or not spec.get("path"):
            raise SourceError(f"live endpoint {name!r} is not configured")
        default = LIVE_DEFAULTS.get(name) if isinstance(LIVE_DEFAULTS.get(name), dict) else {}
        return str(spec.get("method") or default.get("method") or "GET").upper(), str(spec["path"]), spec

    def _take(self, category: str) -> None:
        """One limiter token, waiting up to `limiter_wait_s` for it; RateLimited beyond."""
        if self.limiter is None:
            return
        now = self.clock()
        if self.limiter.take(category, now=now):
            return
        wait = float(self.limiter.wait_seconds(category, now=now))
        if wait > float(self.live.get("limiter_wait_s") or 0):
            raise RateLimited(f"local {category} rate limit: next token in {wait:.1f}s")
        self.sleep(wait)
        self.limiter.take(category, now=self.clock())

    def _redact(self, text: str | None) -> str | None:
        if text is None:
            return None
        for secret in (self.creds.key, self.creds.passphrase):
            if secret:
                text = text.replace(secret, "***" + self.creds.key_hint)
        return text

    def _note_skew(self, server: datetime | None) -> None:
        if server is not None:
            self.last_server_time = server
            self.last_skew_ms = int(round((server - self.clock()).total_seconds() * 1000))

    def _request(
        self, category: str, name: str, path_params: dict[str, str] | None = None, query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None, tolerate: tuple[int, ...] = (),
    ) -> tuple[int, Any]:
        """One signed call: (status, parsed JSON or None). Raises on 401/403 (AuthError),
        429 (RateLimited), any other status >= 400 not in `tolerate` (SourceError).
        Nothing is signed for a destination live_policy refuses, and no place is
        signed while the measured clock skew is over `max_skew_ms`."""
        if self.base_url_problem:
            raise SourceError(f"live base_url {self.base_url!r} refused: {self.base_url_problem}")
        if category == "orders" and self.max_skew_ms is not None and self.last_skew_ms is not None and abs(self.last_skew_ms) > self.max_skew_ms:
            raise AuthError(f"clock skew {self.last_skew_ms} ms is over the limit of {self.max_skew_ms} ms; placing refused")
        self._take(category)
        method, path, _ = self.endpoint(name)
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", str(value))
        if query:
            path = f"{path}{'&' if '?' in path else '?'}{urlencode(query)}"
        payload = b"" if body is None else json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
        headers = {"Accept": "application/json", "User-Agent": "polymarket-fleet/0.5"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(sign_headers(self.creds, method, path, payload, timestamp_now(self.auth, self.clock), self.auth))
        url = self.base_url + path
        try:
            status, response_headers, text = self.http(method, url, headers, payload if body is not None else None, float(self.live.get("timeout_s") or 10.0))
        except SourceError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise GatewayTimeout(f"{method} {path} timed out") from exc
        except (OSError, ValueError) as exc:
            raise SourceError(f"{method} {path} failed: {exc}") from exc
        self.last_status, self.last_payload = int(status), truncate(self._redact(text), PAYLOAD_LIMIT)
        date = header(response_headers, "Date")
        if date:
            try:
                self._note_skew(email.utils.parsedate_to_datetime(date))
            except (TypeError, ValueError):
                log.debug("unparseable Date header %r", date)
        if status in (401, 403):
            raise AuthError(f"{method} {path} answered {status}: {truncate(self._redact(text), 256)}")
        if status == 429:
            if self.limiter is not None and hasattr(self.limiter, "on_429"):
                self.limiter.on_429(category, now=self.clock())
            raise RateLimited(f"{method} {path} answered 429 (rate limited): {truncate(self._redact(text), 256)}")
        if status >= 400 and status not in tolerate:
            raise SourceError(f"{method} {path} answered {status}: {truncate(self._redact(text), 512)}")
        if not text or not text.strip():
            return int(status), None  # an empty body: nothing listed (records() reads None as [])
        try:
            return int(status), json.loads(text)
        except ValueError:
            raise self._unknown(name, None) from None

    def _unknown(self, what: str, payload: Any) -> SourceError:
        """SourceError for a payload we cannot read; the raw text (truncated) when it
        was not JSON at all."""
        if payload is None:
            payload = self.last_payload or ""
        text = truncate(self._redact(json.dumps(payload, default=str) if not isinstance(payload, str) else payload), 512)
        log.debug("polymarket_us %s payload has an unknown shape: %s", what, text)
        return SourceError(f"{what} payload has an unknown shape: {text}")

    # ------------------------------------------------------------------ calls

    def client_id(self, order: dict[str, Any]) -> str:
        """The id the exchange echoes as client_order_id: the row's `client_id_field`
        (client_request_id by default, what the executor reconciles on), else its id."""
        value = order.get(str(self.live.get("client_id_field") or "client_request_id"))
        return str(value if value is not None else order.get("id"))

    def place(self, order: dict[str, Any]) -> str:
        """POST the order (client_order_id, side_buy or for a sell side_sell, GTD until
        gtd_at); the exchange order id. The order needs `market_ref`."""
        if not order.get("market_ref"):
            raise SourceError(f"order {order.get('id')} has no market_ref to place against")
        fields = self.live["request_fields"]
        missing = [n for n in REQUIRED_REQUEST_FIELDS if not isinstance(fields.get(n), str) or not fields.get(n)]
        if missing:
            raise NotConfigured(f"live.request_fields {', '.join(missing)} not configured (null or missing)")
        side_key = "side_sell" if order.get("side") == "sell" else "side_buy"
        if not isinstance(self.live.get(side_key), str) or not self.live[side_key]:
            raise NotConfigured(f"live.{side_key} not configured (null or missing)")
        price = lp.number(order.get("price"))
        body: dict[str, Any] = {
            fields["client_order_id"]: self.client_id(order),
            fields["market_id"]: str(order["market_ref"]),
            fields["side"]: self.live[side_key],
            fields["price"]: round(price, 4) if price is not None else None,
            fields["size"]: int(order["size"]),
            fields["time_in_force"]: self.live["time_in_force"],
        }
        gtd_at = parse_time(order.get("gtd_at"))
        if gtd_at is not None and fields.get("expires_at"):
            body[fields["expires_at"]] = lp.iso(gtd_at)
        _, payload = self._request("orders", "place", body=body)
        record = lp.unwrap(payload, self.live)
        exchange_id = lp.field(record, self.live, "order_id") if isinstance(record, dict) else None
        if exchange_id is None or isinstance(exchange_id, (dict, list)):
            raise self._unknown("place", payload)
        return str(exchange_id)

    def cancel(self, order: dict[str, Any]) -> bool:
        """DELETE by exchange id (found by client id among the open orders when the
        row has none); True once the exchange no longer lists the order."""
        exchange_id = order.get("exchange_order_id")
        if not exchange_id:
            remote = self._find_open(self.client_id(order))
            if remote is None:
                return True
            exchange_id = remote["exchange_order_id"]
        status, _ = self._request("cancels", "cancel", path_params={"order_id": str(exchange_id)}, tolerate=(404,))
        if status == 404:
            return self._find_open(self.client_id(order), exchange_id=str(exchange_id)) is None
        return True

    def _find_open(self, client_id: str, exchange_id: str | None = None) -> dict[str, Any] | None:
        for remote in self.open_orders():
            if remote["client_order_id"] == client_id or (exchange_id and remote["exchange_order_id"] == exchange_id):
                return remote
        return None

    def open_orders(self) -> list[dict[str, Any]]:
        _, payload = self._request("account", "open")
        records = lp.records(payload, self.live)
        if records is None:
            raise self._unknown("open orders", payload)
        return [lp.order_dict(r, self.live) for r in records if lp.field(r, self.live, "order_id") is not None]

    def get_order(self, exchange_order_id: str) -> dict[str, Any] | None:
        """GET one order by exchange id; None when the exchange answers 404."""
        status, payload = self._request("account", "order", path_params={"order_id": exchange_order_id}, tolerate=(404,))
        if status == 404:
            return None
        record = lp.unwrap(payload, self.live)
        if not isinstance(record, dict) or lp.field(record, self.live, "order_id") is None:
            raise self._unknown("order", payload)
        return lp.order_dict(record, self.live)

    def fills(self, since: datetime | None) -> list[dict[str, Any]]:
        _, _, spec = self.endpoint("fills")
        _, payload = self._request("account", "fills", query=lp.since_query(since, spec) or None)
        records = lp.records(payload, self.live)
        if records is None:
            raise self._unknown("fills", payload)
        out = []
        for record in records:
            fill = lp.fill_dict(record, self.live)
            if fill is None:
                log.debug("polymarket_us fill without id or size skipped: %s", truncate(json.dumps(record, default=str), 512))
                continue
            out.append(fill)
        return out

    def balance(self) -> dict[str, Any]:
        _, payload = self._request("account", "balance")
        record = lp.unwrap(payload, self.live)
        if not isinstance(record, dict):
            raise self._unknown("balance", payload)
        balance = lp.cents(lp.field(record, self.live, "balance"), self.live)
        buying_power = lp.cents(lp.field(record, self.live, "buying_power"), self.live)
        if balance is None and buying_power is None:
            raise self._unknown("balance", payload)
        # Only a field the owner named carries server time; an ordinary "timestamp"
        # or "time" field in the payload must not overwrite the Date header's skew.
        time_field = self.auth.get("server_time_field")
        server_time = parse_time(pick(record, [str(time_field)])) if time_field else None
        self._note_skew(server_time)
        return {
            "balance_cents": balance if balance is not None else buying_power,
            "buying_power_cents": buying_power if buying_power is not None else balance,
            "server_time": None if server_time is None else lp.iso(server_time),
        }

    def cancel_all(self) -> int:
        """The configured cancel-all endpoint, or list-open-then-cancel-each; the
        number of orders cancelled."""
        if self.live.get("cancel_all"):
            before = self.open_orders()
            _, payload = self._request("cancels", "cancel_all")
            record = lp.unwrap(payload, self.live)
            count = lp.number(lp.field(record, self.live, "cancelled_count")) if isinstance(record, dict) else None
            listed = lp.records(payload, self.live)
            return int(count) if count is not None else len(listed) if listed is not None else len(before)
        done = 0
        for remote in self.open_orders():
            try:
                done += int(self.cancel({"exchange_order_id": remote["exchange_order_id"], "id": remote["client_order_id"]}))
            except SourceError as exc:
                log.warning("cancel_all: cancel of %s failed: %s", remote["exchange_order_id"], exc)
        return done

    def probe_account(self) -> dict[str, Any]:
        """The balance call's status and raw payload (truncated, key redacted) for the
        owner; never raises."""
        out: dict[str, Any] = {"status": None, "payload": None, "key_hint": self.creds.key_hint, "error": None}
        self.last_status = self.last_payload = None
        try:
            self._request("account", "balance")
        except Exception as exc:  # noqa: BLE001 - the owner wants the error text
            out["error"] = self._redact(str(exc))
        out["status"], out["payload"] = self.last_status, self.last_payload
        return out
