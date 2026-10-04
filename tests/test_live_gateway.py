"""The live gateway (docs/LIVE.md): credentials loading, Ed25519 signing, request
building against the configured paths, response parsing from fixture JSON, the
status-to-exception mapping and probe redaction. No database, no network: every test
injects a fake transport."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from nacl.signing import SigningKey, VerifyKey

from host.exchange.adapters.base import RateLimited, SourceError
from host.exchange.adapters.polymarket_us import live_config_with_defaults
from host.exchange.adapters.polymarket_us_live import AuthError, GatewayTimeout, LiveGateway
from host.exchange.adapters.signing import AUTH_DEFAULTS, message_for, sign_headers, timestamp_now
from host.exchange.credentials import Credentials, CredentialsError, load
from host.exchange.ratelimit import RateLimiter

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
SEED = bytes(range(32))
KEY = "pmus-live-key-7Q2Z"
ORDER_ID = "0b1d2e3f-4a5b-6c7d-8e9f-a0b1c2d3e4f5"
# The signature of the spec'd message under the fixed seed, pinned so a change to the
# template, the encoding or the library shows up here first.
KNOWN_SIGNATURE = "nQhbX1wjmnIkA2vPpQVJ/8+dxxpUIn48s1Htvo5Ss18jKWzNqKTcQbVZCgqyGVvF0ioxVIHj1NyNcIlqHGGbDQ=="


def fixture(name: str) -> str:
    return (FIXTURES / f"polymarket_us_live_{name}.json").read_text()


def creds(passphrase: str | None = None) -> Credentials:
    return Credentials(key=KEY, secret=SEED, passphrase=passphrase)


class FakeHttp:
    """Records every request; answers from a queue of (status, headers, text) or a
    callable per call, the last answer repeating."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body, "timeout": timeout})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(answer):
            return answer()
        if isinstance(answer, Exception):
            raise answer
        status, text = answer[0], answer[-1]
        headers_out = answer[1] if len(answer) == 3 else {"Date": "Sat, 03 Oct 2026 15:00:01 GMT"}
        return status, headers_out, text

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]


def gateway(http: FakeHttp, config: dict[str, Any] | None = None, limiter: RateLimiter | None = None, passphrase: str | None = None, clock=None) -> LiveGateway:
    return LiveGateway(creds(passphrase), config or {}, limiter=limiter, http=http, clock=clock or (lambda: NOW), sleep=lambda s: None)


ROW_ID = "9f8e7d6c-5b4a-3928-1706-f5e4d3c2b1a0"


def order(**extra: Any) -> dict[str, Any]:
    """An orders row as the executor hands it over, plus the market_ref it must attach."""
    row = {"id": ROW_ID, "client_request_id": ORDER_ID, "market_ref": "pmus-kc-lv-kc", "price": "0.5500", "size": 3, "gtd_at": NOW + timedelta(minutes=15), "exchange_order_id": None, "mode": "live"}
    row.update(extra)
    return row


# ---------------------------------------------------------------------- signing

def test_signing_is_deterministic_for_a_known_seed():
    """Assumption documented here: message = timestamp + METHOD + path + body, signed
    with Ed25519 over the UTF-8 bytes, base64. The expected value is computed with
    pynacl from that message and pinned as a literal."""
    timestamp, method, path, body = "1790000000000", "post", "/v1/orders", b'{"a":1}'
    message = message_for(method, path, body, timestamp, AUTH_DEFAULTS)
    assert message == b'1790000000000POST/v1/orders{"a":1}'
    expected = base64.b64encode(SigningKey(SEED).sign(message).signature).decode("ascii")
    assert expected == KNOWN_SIGNATURE
    first = sign_headers(creds(), method, path, body, timestamp, AUTH_DEFAULTS)
    second = sign_headers(creds(), method, path, body, timestamp, AUTH_DEFAULTS)
    assert first["X-PM-Signature"] == second["X-PM-Signature"] == expected
    VerifyKey(bytes(SigningKey(SEED).verify_key)).verify(message, base64.b64decode(first["X-PM-Signature"]))
    other = sign_headers(Credentials(KEY, bytes(32)), method, path, body, timestamp, AUTH_DEFAULTS)
    assert other["X-PM-Signature"] != expected, "a different seed gives a different signature"
    custom = {"template": "{method}|{path}|{timestamp}|{body}", "encoding": "hex"}
    assert message_for(method, path, body, timestamp, custom) == b'POST|/v1/orders|1790000000000|{"a":1}'
    hex_sig = sign_headers(creds(), method, path, body, timestamp, custom)["X-PM-Signature"]
    assert hex_sig == SigningKey(SEED).sign(message_for(method, path, body, timestamp, custom)).signature.hex()


def test_headers_and_timestamp_formats():
    headers = sign_headers(creds(), "GET", "/v1/balance", b"", "1790000000000", AUTH_DEFAULTS)
    assert set(headers) == {"X-PM-Access-Key", "X-PM-Signature", "X-PM-Timestamp"}
    assert headers["X-PM-Access-Key"] == KEY and headers["X-PM-Timestamp"] == "1790000000000"
    with_pass = sign_headers(creds("hunter2"), "GET", "/v1/balance", b"", "1790000000000", AUTH_DEFAULTS)
    assert with_pass["X-PM-Passphrase"] == "hunter2"
    assert with_pass["X-PM-Signature"] == headers["X-PM-Signature"], "the passphrase is a header, not part of the message"
    renamed = {"headers": {"key": "PM-KEY", "signature": "PM-SIG", "timestamp": "PM-TS", "passphrase": "PM-PASS"}}
    assert set(sign_headers(creds("x"), "GET", "/p", b"", "1", renamed)) == {"PM-KEY", "PM-SIG", "PM-TS", "PM-PASS"}
    clock = lambda: datetime(2026, 10, 3, 15, 0, 0, 250_000, tzinfo=timezone.utc)  # noqa: E731
    assert timestamp_now(AUTH_DEFAULTS, clock) == str(int(clock().timestamp() * 1000)) == "1791039600250"
    assert timestamp_now({"timestamp": "s"}, clock) == "1791039600"
    assert timestamp_now({}, clock) == "1791039600250", "an empty auth block means milliseconds"
    with pytest.raises(ValueError):
        timestamp_now({"timestamp": "ns"}, clock)
    with pytest.raises(ValueError):
        sign_headers(creds(), "GET", "/p", b"", "1", {"encoding": "base32"})


def test_body_bytes_are_what_is_signed():
    http = FakeHttp((200, fixture("place")))
    gw = gateway(http, passphrase="pp")
    gw.place(order())
    call = http.last
    body = call["body"]
    assert body == json.dumps(json.loads(body), separators=(",", ":")).encode(), "compact JSON, no spaces"
    assert call["headers"]["Content-Type"] == "application/json"
    expected = sign_headers(creds("pp"), "POST", "/v1/orders", body, call["headers"]["X-PM-Timestamp"], AUTH_DEFAULTS)
    assert call["headers"]["X-PM-Signature"] == expected["X-PM-Signature"]
    assert call["headers"]["X-PM-Passphrase"] == "pp"
    wrong = sign_headers(creds("pp"), "POST", "/v1/orders", body + b" ", call["headers"]["X-PM-Timestamp"], AUTH_DEFAULTS)
    assert wrong["X-PM-Signature"] != expected["X-PM-Signature"]
    # a GET signs the empty body and the path including its query string
    http = FakeHttp((200, fixture("fills")))
    gw = gateway(http)
    gw.fills(NOW - timedelta(minutes=1))
    call = http.last
    assert call["body"] is None and call["url"].endswith("/v1/fills?since=2026-10-03T14%3A59%3A00.000Z")
    path = call["url"].split("https://api.polymarket.us", 1)[1]
    expected = sign_headers(creds(), "GET", path, b"", call["headers"]["X-PM-Timestamp"], AUTH_DEFAULTS)
    assert call["headers"]["X-PM-Signature"] == expected["X-PM-Signature"]
    assert "Content-Type" not in call["headers"]


# --------------------------------------------------------------- request shape

def test_request_building_for_every_call_uses_config_paths():
    config = {
        "live": {
            "base_url": "https://sandbox.polymarket.us/",
            "place": "POST /api/orders/new",
            "cancel": {"method": "POST", "path": "/api/orders/{order_id}/cancel"},
            "open": {"path": "/api/orders?status=open"},
            "fills": {"path": "/api/trades", "since_param": "from", "since_format": "ms"},
            "balance": "GET /api/account",
            "request_fields": {"client_order_id": "clientId", "market_id": "market", "side": "side", "price": "limitPrice", "size": "qty", "time_in_force": "tif", "expires_at": "goodTil"},
            "side_buy": "buy", "time_in_force": "GTD",
        },
        "auth": {"timestamp": "s", "headers": {"key": "X-Key"}},
    }
    http = FakeHttp((200, fixture("place")))
    gw = gateway(http, config)
    assert gw.place(order()) == "ord-7f3a9c"
    call = http.last
    assert (call["method"], call["url"], call["timeout"]) == ("POST", "https://sandbox.polymarket.us/api/orders/new", 10.0)
    assert json.loads(call["body"]) == {
        "clientId": ORDER_ID, "market": "pmus-kc-lv-kc", "side": "buy", "limitPrice": 0.55, "qty": 3, "tif": "GTD", "goodTil": "2026-10-03T15:15:00.000Z",
    }
    assert call["headers"]["X-Key"] == KEY and call["headers"]["X-PM-Timestamp"] == str(int(NOW.timestamp()))
    assert {"X-PM-Signature", "Accept", "User-Agent", "Content-Type"} <= set(call["headers"])
    assert call["headers"]["X-PM-Signature"] == sign_headers(creds(), "POST", "/api/orders/new", call["body"], call["headers"]["X-PM-Timestamp"], config["auth"])["X-PM-Signature"]

    http.answers = [(200, "{}")]
    assert gw.cancel(order(exchange_order_id="ord-7f3a9c")) is True
    assert (http.last["method"], http.last["url"], http.last["body"]) == ("POST", "https://sandbox.polymarket.us/api/orders/ord-7f3a9c/cancel", None)

    http.answers = [(200, fixture("open_orders"))]
    gw.open_orders()
    assert (http.last["method"], http.last["url"]) == ("GET", "https://sandbox.polymarket.us/api/orders?status=open")

    http.answers = [(200, fixture("fills"))]
    gw.fills(NOW)
    assert http.last["url"] == f"https://sandbox.polymarket.us/api/trades?from={int(NOW.timestamp() * 1000)}"
    gw.fills(None)
    assert http.last["url"] == "https://sandbox.polymarket.us/api/trades", "no since, no query"

    http.answers = [(200, fixture("balance"))]
    gw.balance()
    assert (http.last["method"], http.last["url"]) == ("GET", "https://sandbox.polymarket.us/api/account")
    for call in http.calls:
        assert "X-PM-Passphrase" not in call["headers"], "no passphrase, no header"

    defaults = live_config_with_defaults(None)["live"]
    assert defaults["base_url"] == "https://api.polymarket.us" and defaults["cancel_all"] is None
    assert (defaults["place"]["method"], defaults["place"]["path"]) == ("POST", "/v1/orders")
    assert (defaults["cancel"]["method"], defaults["cancel"]["path"]) == ("DELETE", "/v1/orders/{order_id}")
    assert defaults["open"]["path"] == "/v1/orders/open" and defaults["order"]["path"] == "/v1/order/{order_id}"
    assert defaults["fills"]["path"] == "/v1/fills" and defaults["balance"]["path"] == "/v1/balance"
    assert live_config_with_defaults({"live": {"response_fields": {"order_id": "uid"}}})["live"]["response_fields"]["order_id"] == ["uid"]
    with pytest.raises(SourceError):
        gateway(FakeHttp((200, "{}")), {"live": {"place": None}}).place(order())
    with pytest.raises(SourceError):
        gateway(FakeHttp((200, "{}"))).place(order(market_ref=None))
    # the client id the exchange echoes is client_request_id (what the executor
    # reconciles on) unless configured to be the row id
    http = FakeHttp((200, fixture("place")))
    gateway(http, {"live": {"client_id_field": "id"}}).place(order())
    assert json.loads(http.last["body"])["client_order_id"] == ROW_ID
    http = FakeHttp((200, fixture("place")))
    gateway(http).place({**order(), "client_request_id": None})
    assert json.loads(http.last["body"])["client_order_id"] == ROW_ID, "falls back to the row id"


def test_limiter_tokens_by_category_and_waits_briefly():
    limiter = RateLimiter({"orders_per_s": 1, "cancels_per_s": 1, "account_per_s": 1}, now=NOW)
    http = FakeHttp((200, fixture("place")), (200, "{}"), (200, fixture("balance")), (200, fixture("fills")))
    gw = gateway(http, limiter=limiter)
    gw.place(order())
    assert limiter.buckets["orders"].tokens == pytest.approx(0.0)
    gw.cancel(order(exchange_order_id="x"))
    assert limiter.buckets["cancels"].tokens == pytest.approx(0.0)
    gw.balance()
    assert limiter.buckets["account"].tokens == pytest.approx(0.0)
    slept: list[float] = []
    gw.sleep = slept.append
    gw.fills(None)
    assert slept and slept[0] == pytest.approx(1.0), "the second account call within the second waits for the next token"
    gw.live["limiter_wait_s"] = 0.5
    with pytest.raises(RateLimited):
        gw.open_orders()
    assert len(http.calls) == 4, "a wait over the limit never reaches the transport"


# ----------------------------------------------------------------- responses

def test_responses_parsed_from_fixtures_and_malformed_payloads_raise_source_error():
    gw = gateway(FakeHttp((200, fixture("place"))))
    assert gw.place(order()) == "ord-7f3a9c"

    listed = gateway(FakeHttp((200, fixture("open_orders")))).open_orders()
    assert listed == [
        {"exchange_order_id": "ord-7f3a9c", "client_order_id": ORDER_ID, "market_ref": "pmus-kc-lv-kc", "price": 0.55, "size": 3, "filled_size": 1, "status": "PARTIALLY_FILLED"},
        {"exchange_order_id": "ord-b2c4d6", "client_order_id": "smoke-9a8b7c6d", "market_ref": "pmus-sf-sea-sea", "price": 0.12, "size": 1, "filled_size": 0, "status": "open"},
    ], "camelCase and snake_case both parse; a record without an id and a non-object are skipped"

    fills = gateway(FakeHttp((200, fixture("fills")))).fills(NOW)
    assert fills == [
        {"exchange_fill_id": "fill-0001", "exchange_order_id": "ord-7f3a9c", "client_order_id": ORDER_ID, "price": 0.55, "size": 1, "fee_cents": 1, "ts": datetime(2026, 10, 3, 15, 0, 30, 250_000, tzinfo=timezone.utc)},
        {"exchange_fill_id": "fill-0002", "exchange_order_id": "ord-0ther", "client_order_id": "unknown-client-id", "price": 0.61, "size": 2, "fee_cents": 0, "ts": datetime.fromtimestamp(1790000000, tz=timezone.utc)},
    ], "fees in dollars become cents; a fill without an id is skipped"

    gw = gateway(FakeHttp((200, fixture("balance"))))
    assert gw.balance() == {"balance_cents": 125050, "buying_power_cents": 98025, "server_time": None}
    assert gw.last_skew_ms == 1000, "without auth.server_time_field only the Date header measures the skew"
    gw = gateway(FakeHttp((200, fixture("balance"))), {"auth": {"server_time_field": "server_time"}})
    assert gw.balance()["server_time"] == "2026-10-03T15:00:02.500Z" and gw.last_skew_ms == 2500, "the named field wins over the Date header"
    cents = gateway(FakeHttp((200, '{"balance": 1250, "buying_power": 980}')), {"live": {"money_unit": "cents"}}).balance()
    assert (cents["balance_cents"], cents["buying_power_cents"]) == (1250, 980)
    only = gateway(FakeHttp((200, '{"available": "12.34"}'))).balance()
    assert (only["balance_cents"], only["buying_power_cents"]) == (1234, 1234), "one figure stands in for both"

    bad = json.loads(fixture("malformed"))
    for name, call in [("place_without_an_id", "place"), ("place_id_is_an_object", "place"), ("open_orders_not_a_list", "open_orders"), ("open_orders_scalar", "open_orders"), ("fills_wrong_key", "fills"), ("balance_without_money", "balance"), ("balance_is_a_list", "balance")]:
        gw = gateway(FakeHttp((200, json.dumps(bad[name]))))
        with pytest.raises(SourceError) as exc:
            getattr(gw, call)(order()) if call == "place" else getattr(gw, call)(NOW) if call == "fills" else getattr(gw, call)()
        assert "unknown shape" in str(exc.value) and json.dumps(bad[name])[:40] in str(exc.value), name
    for call in ("place", "open_orders", "fills", "balance"):
        gw = gateway(FakeHttp((200, bad["not_json"])))
        with pytest.raises(SourceError, match="unknown shape"):
            getattr(gw, call)(order()) if call == "place" else getattr(gw, call)(NOW) if call == "fills" else getattr(gw, call)()
    with pytest.raises(SourceError, match="502"):
        gateway(FakeHttp((502, bad["not_json"]))).balance()
    gw = gateway(FakeHttp((200, '{"order": {"status": "ACCEPTED"}}' + "x" * 2000)))
    with pytest.raises(SourceError) as exc:
        gw.place(order())
    assert "truncated" in str(exc.value) and len(str(exc.value)) < 700


def test_401_raises_auth_error_429_raises_rate_limited():
    with pytest.raises(AuthError, match="401"):
        gateway(FakeHttp((401, '{"error": "invalid signature"}'))).balance()
    with pytest.raises(AuthError, match="403"):
        gateway(FakeHttp((403, "forbidden"))).open_orders()
    assert issubclass(AuthError, SourceError)
    limiter = RateLimiter(now=NOW)
    gw = gateway(FakeHttp((429, '{"error": "slow down"}')), limiter=limiter)
    with pytest.raises(RateLimited):
        gw.place(order())
    assert limiter.buckets["orders"].halved_until > NOW.timestamp(), "the limiter learns about the 429"
    assert limiter.buckets["account"].halved_until == 0.0
    with pytest.raises(SourceError, match="503"):
        gateway(FakeHttp((503, "upstream"))).fills(None)
    with pytest.raises(GatewayTimeout):
        gateway(FakeHttp(TimeoutError("read timed out"))).place(order())
    assert issubclass(GatewayTimeout, SourceError) and issubclass(GatewayTimeout, TimeoutError)
    with pytest.raises(SourceError):
        gateway(FakeHttp(OSError("connection reset"))).place(order())
    http = FakeHttp((200, {"date": "Sat, 03 Oct 2026 14:59:30 GMT"}, fixture("balance").replace("server_time", "when")))
    gw = gateway(http)
    gw.balance()
    assert gw.last_skew_ms == -30_000, "the Date header (any case) sets the skew when no server-time field is present"
    gw.max_skew_ms = 20_000
    with pytest.raises(AuthError, match="skew"):
        gw.place(order())
    assert len(http.calls) == 1, "over the skew limit no place is signed or sent"
    gw.balance()
    assert len(http.calls) == 2, "cancels, listings and the probe keep going, so a kill can still reach the exchange"


def test_cancel_paths_and_cancel_all():
    # 404 on cancel: confirmed only once the open list no longer shows the order
    http = FakeHttp((404, '{"error": "not found"}'), (200, fixture("open_orders")))
    gw = gateway(http)
    assert gw.cancel(order(exchange_order_id="ord-7f3a9c")) is False
    http = FakeHttp((404, '{"error": "not found"}'), (200, '{"orders": []}'))
    assert gateway(http).cancel(order(exchange_order_id="ord-7f3a9c")) is True
    # no exchange id yet (kill during submitting): found by client id, then cancelled
    http = FakeHttp((200, fixture("open_orders")), (204, ""))
    assert gateway(http).cancel(order()) is True
    assert http.last["url"].endswith("/v1/orders/ord-7f3a9c") and http.last["method"] == "DELETE"
    http = FakeHttp((200, '{"orders": []}'))
    assert gateway(http).cancel(order()) is True and len(http.calls) == 1, "not on the exchange: nothing to cancel"
    # cancel_all without a configured path: list, then one DELETE per open order
    http = FakeHttp((200, fixture("open_orders")), (200, "{}"), (500, "boom"))
    gw = gateway(http)
    assert gw.cancel_all() == 1
    assert [c["method"] for c in http.calls] == ["GET", "DELETE", "DELETE"]
    # with a configured path: one call, the count from the payload
    http = FakeHttp((200, fixture("open_orders")), (200, '{"cancelled": 2}'))
    gw = gateway(http, {"live": {"cancel_all": "DELETE /v1/orders"}})
    assert gw.cancel_all() == 2 and http.last["method"] == "DELETE" and http.last["url"].endswith("/v1/orders")
    http = FakeHttp((200, '{"order": {"order_id": "ord-1", "status": "FILLED", "size": 2, "filled_size": 2}}'))
    gw = gateway(http)
    assert gw.get_order("ord-1")["status"] == "FILLED" and http.last["url"].endswith("/v1/order/ord-1")
    assert gateway(FakeHttp((404, ""))).get_order("ord-x") is None


# ---------------------------------------------------------------------- probe

def test_probe_redacts_the_key():
    echo = json.dumps({"api_key": KEY, "balance": "10.00", "passphrase": "hunter2"})
    gw = gateway(FakeHttp((200, echo)), passphrase="hunter2")
    out = gw.probe_account()
    assert out["status"] == 200 and out["error"] is None and out["key_hint"] == "7Q2Z"
    assert KEY not in out["payload"] and "hunter2" not in out["payload"] and "***7Q2Z" in out["payload"]
    assert base64.b64encode(SEED).decode() not in json.dumps(out) and SEED.hex() not in json.dumps(out)
    failed = gateway(FakeHttp((401, f"bad key {KEY}"))).probe_account()
    assert failed["status"] == 401 and KEY not in failed["error"] and KEY not in failed["payload"]
    down = gateway(FakeHttp(OSError("connection refused"))).probe_account()
    assert down == {"status": None, "payload": None, "key_hint": "7Q2Z", "error": "GET /v1/balance failed: connection refused"}
    big = gateway(FakeHttp((200, "{" + '"x": "' + "y" * 70_000 + '"}'))).probe_account()
    assert len(big["payload"]) < 66_000 and "truncated" in big["payload"]
    assert repr(gw.creds) == "Credentials(key_hint='7Q2Z')" and KEY not in str(gw.creds) and "secret" not in repr(gw.creds)


# ---------------------------------------------------------------- credentials

def test_credentials_loader_formats():
    signing_key = SigningKey(SEED)
    seed_b64 = base64.b64encode(SEED).decode()
    full_b64 = base64.b64encode(bytes(signing_key) + bytes(signing_key.verify_key)).decode()
    for secret in (seed_b64, seed_b64.rstrip("="), base64.urlsafe_b64encode(SEED).decode(), SEED.hex(), SEED.hex().upper(), full_b64, (bytes(signing_key) + bytes(signing_key.verify_key)).hex(), f"  {seed_b64}\n"):
        loaded = load({"POLYMARKET_US_API_KEY": KEY, "POLYMARKET_US_API_SECRET": secret})
        assert loaded is not None and loaded.secret == SEED, secret
        assert loaded.key == KEY and loaded.key_hint == "7Q2Z" and loaded.passphrase is None
        assert repr(loaded) == "Credentials(key_hint='7Q2Z')" and secret not in repr(loaded)
    loaded = load({"POLYMARKET_US_API_KEY": KEY, "POLYMARKET_US_API_SECRET": seed_b64, "POLYMARKET_US_PASSPHRASE": "hunter2"})
    assert loaded.passphrase == "hunter2" and "hunter2" not in repr(loaded)
    assert load({"POLYMARKET_US_API_KEY": KEY, "POLYMARKET_US_API_SECRET": seed_b64, "POLYMARKET_US_PASSPHRASE": ""}).passphrase is None
    assert load({}) is None
    assert load({"POLYMARKET_US_API_KEY": KEY}) is None
    assert load({"POLYMARKET_US_API_SECRET": seed_b64}) is None
    assert load({"POLYMARKET_US_API_KEY": "  ", "POLYMARKET_US_API_SECRET": seed_b64}) is None
    for bad in ("abc", base64.b64encode(b"short").decode(), "zz" * 32, base64.b64encode(bytes(33)).decode(), "!!!!"):
        with pytest.raises(CredentialsError) as exc:
            load({"POLYMARKET_US_API_KEY": KEY, "POLYMARKET_US_API_SECRET": bad})
        assert bad not in str(exc.value)
    assert isinstance(CredentialsError("x"), ValueError)
    with_env = load()
    assert with_env is None or isinstance(with_env, Credentials), "the real environment never breaks the loader"
