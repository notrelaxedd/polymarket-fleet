"""`probe-alpaca`: read-only checks of the Alpaca keys and of what the account can
trade (docs/ALPACA.md, "The probe"). Every request is a GET; nothing is ordered,
written or stored. The output is meant to be pasted back, so the key and the secret
are redacted from every text, and the account's id and number are never printed.

Checks, in order: the account (keys valid, paper or live, buying power, flags), the
clock (market open, clock skew from the Date header), one stock asset, the crypto
asset list, an options contract sample, the event-contract guesses (Alpaca's asset
class name for Kalshi event contracts is not verified yet, so several are tried and
`--asset-class` adds more), any extra `--get` paths, and one free IEX stock quote and
one crypto quote from the market data host.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable
from urllib.parse import quote

from host.exchange.adapters.base import SourceError, truncate
from host.exchange.adapters.live_http import Http, header, urllib_http
from host.exchange.alpaca_credentials import DATA_URL, AlpacaCredentials

TIMEOUT_S = 10.0
SNIPPET = 2 * 1024
RAW_LIMIT = 64 * 1024
EVENT_GUESSES = ("event_contract", "prediction_market", "event", "kalshi")
ACCOUNT_FIELDS = (
    "status", "crypto_status", "currency", "cash", "buying_power", "equity", "pattern_day_trader",
    "daytrade_count", "trading_blocked", "account_blocked", "transfers_blocked", "shorting_enabled",
    "multiplier", "options_trading_level", "options_approved_level",
)
PRIVATE_FIELDS = ("id", "account_number", "admin_configurations", "user_configurations")
CLOCK_FIELDS = ("timestamp", "is_open", "next_open", "next_close")
KNOWN_CLASSES = ("us_equity", "crypto", "us_option")


def _call(creds: AlpacaCredentials, http: Http, base: str, path: str) -> dict[str, Any]:
    """One GET: {"url", "status", "date", "data" (parsed JSON or None), "text", "error"}."""
    url = base + path
    out: dict[str, Any] = {"url": url, "status": None, "date": None, "data": None, "text": None, "error": None}
    try:
        status, headers, text = http("GET", url, creds.headers(), None, TIMEOUT_S)
    except SourceError as exc:
        out["error"] = creds.redact(str(exc))
        return out
    out.update(status=status, date=header(headers, "Date"), text=creds.redact(text))
    try:
        out["data"] = json.loads(text) if text else None
    except ValueError:
        out["error"] = "the answer is not JSON (or was cut off)"
    if status != 200 and not out["error"]:
        out["error"] = f"answered {status}"
    return out


def _snippet(result: dict[str, Any], raw: bool) -> str | None:
    return truncate(result.get("text"), RAW_LIMIT if raw else SNIPPET)


def _records(data: Any) -> list[dict[str, Any]] | None:
    """A listing's records: a bare list, or the first list value of a wrapper object."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return None


def _listing(result: dict[str, Any], raw: bool, new_only: bool = False) -> dict[str, Any]:
    """Status, count, sample symbols and the first record's field names of a listing.
    With `new_only`, records of a class already known (stocks, crypto, options) are left
    out of the count, so an endpoint that ignores an unknown asset class and answers with
    stocks is never reported as event contracts."""
    out = {"url": result["url"], "status": result["status"], "error": result["error"]}
    records = _records(result["data"]) if result["status"] == 200 else None
    if records is not None and new_only:
        known = [r for r in records if r.get("class") in KNOWN_CLASSES]
        records = [r for r in records if r.get("class") not in KNOWN_CLASSES]
        if known:
            out["known_class_records_ignored"] = len(known)
    if records is not None:
        out["count"] = len(records)
        out["sample"] = [r.get("symbol") or r.get("name") or r.get("id") for r in records[:10]]
        out["fields"] = sorted(records[0]) if records else []
        if raw:
            out["payload"] = _snippet(result, raw)
    else:
        out["payload"] = _snippet(result, raw)
    return out


def _account(result: dict[str, Any], raw: bool) -> dict[str, Any]:
    out = {"url": result["url"], "status": result["status"], "error": result["error"]}
    data = result["data"]
    if result["status"] == 200 and isinstance(data, dict):
        out["summary"] = {k: data.get(k) for k in ACCOUNT_FIELDS if k in data}
        out["fields"] = sorted(k for k in data if k not in PRIVATE_FIELDS)
    else:
        out["payload"] = _snippet(result, raw)
    return out


def _clock(result: dict[str, Any], now: datetime) -> dict[str, Any]:
    out = {"url": result["url"], "status": result["status"], "error": result["error"]}
    data = result["data"]
    if isinstance(data, dict):
        out["summary"] = {k: data.get(k) for k in CLOCK_FIELDS if k in data}
    if result["date"]:
        try:
            server = parsedate_to_datetime(result["date"])
            out["clock_skew_ms"] = int((server - now).total_seconds() * 1000)
        except (TypeError, ValueError):
            pass
    return out


def _plain(result: dict[str, Any], raw: bool) -> dict[str, Any]:
    return {"url": result["url"], "status": result["status"], "error": result["error"], "payload": _snippet(result, raw)}


def _verdict(creds: AlpacaCredentials, account: dict[str, Any], events: dict[str, Any]) -> dict[str, str]:
    status = account["status"]
    if status == 200:
        keys = f"ok ({creds.environment} account, key ***{creds.key_hint})"
    elif status in (401, 403):
        keys = (f"rejected ({status}): regenerate the pair in the Alpaca dashboard and check that "
                f"ALPACA_BASE_URL matches it (paper keys only work on paper-api.alpaca.markets)")
    else:
        keys = f"could not check: {account['error']}"
    found = [name for name, r in events.items() if r.get("count")]
    if found:
        contracts = f"found under {', '.join(found)}: step 8 (NFL on Alpaca) can start"
    else:
        contracts = ("not found under the guessed names: ask Alpaca support which asset class or endpoint "
                     "serves Kalshi event contracts for this account, then rerun with --asset-class NAME or --get PATH")
    return {"keys": keys, "event_contracts": contracts}


def _path(extra: str) -> tuple[str, str]:
    """(host, path) of a --get value: "/v2/..." on the trading host, "data:/v..." on the
    market data host; anything with a scheme, a host or '..' is refused."""
    on_data = extra.startswith("data:")
    path = extra[len("data:"):] if on_data else extra
    if not path.startswith("/") or "://" in path or ".." in path or path.startswith("//"):
        raise ValueError(f"--get needs a path like /v2/assets or data:/v2/stocks/AAPL/bars, not {extra!r}")
    return ("data" if on_data else "trading"), path


def run(creds: AlpacaCredentials, asset_classes: Iterable[str] = (), gets: Iterable[str] = (), raw: bool = False,
        http: Http | None = None, now: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Every check above; never raises for a network or HTTP problem (each check records its own)."""
    http = http or urllib_http
    clock_now = now or (lambda: datetime.now(timezone.utc))
    base = creds.base_url

    def get(path: str, host: str = "trading") -> dict[str, Any]:
        return _call(creds, http, DATA_URL if host == "data" else base, path)

    extras = [_path(g) for g in gets]
    account = _account(get("/v2/account"), raw)
    started = clock_now()
    clock = _clock(get("/v2/clock"), started)
    stock = _plain(get("/v2/assets/AAPL"), raw)
    crypto = _listing(get("/v2/assets?status=active&asset_class=crypto"), raw)
    options = _listing(get("/v2/options/contracts?underlying_symbols=SPY&limit=3"), raw)
    names = list(dict.fromkeys([*EVENT_GUESSES, *asset_classes]))
    events = {n: _listing(get(f"/v2/assets?status=active&asset_class={quote(n, safe='')}"), raw, True) for n in names}
    # The --get answers are shown for reading, never counted as event contracts: a path
    # can answer with anything (bars, orders), so only the asset class lookups decide.
    extra = {f"{h}:{p}" if h == "data" else p: _listing(get(p, h), raw, True) for h, p in extras}
    data = {
        "stock_quote_iex": _plain(get("/v2/stocks/AAPL/quotes/latest?feed=iex", "data"), raw),
        "crypto_quote": _plain(get("/v1beta3/crypto/us/latest/quotes?symbols=BTC%2FUSD", "data"), raw),
    }
    return {
        "verdict": _verdict(creds, account, events),
        "key_present": True, "key_hint": creds.key_hint, "environment": creds.environment, "base_url": base,
        "account": account, "clock": clock, "stock_asset": stock, "crypto_assets": crypto,
        "options_contracts": options, "event_contract_guesses": events, "extra": extra, "market_data": data,
    }
