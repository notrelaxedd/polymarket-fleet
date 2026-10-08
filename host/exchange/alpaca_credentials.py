"""Alpaca credentials for the exchange process only (docs/ALPACA.md, "Credentials").

`load()` reads `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` and the optional
`ALPACA_BASE_URL` (default: the paper trading host) from exchange.env. The values are
never logged, stored or returned: `repr()` shows the key's last 4 characters only, and
`redact()` replaces the key and the secret in any text before it is printed.

Requests may only go to Alpaca's own hosts: the base URL must be https on
`paper-api.alpaca.markets` (paper) or `api.alpaca.markets` (live); a trailing `/v2`
is accepted and dropped, because every request path carries its own version.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

KEY_VAR = "ALPACA_API_KEY_ID"
SECRET_VAR = "ALPACA_API_SECRET_KEY"
BASE_URL_VAR = "ALPACA_BASE_URL"
PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
TRADING_HOSTS = {"paper-api.alpaca.markets": "paper", "api.alpaca.markets": "live"}


class AlpacaConfigError(ValueError):
    """ALPACA_BASE_URL is set but not an allowed Alpaca trading URL (never carries a key)."""


@dataclass(repr=False, eq=False)
class AlpacaCredentials:
    """The key id, the secret, the trading base URL and whether it is paper or live."""

    key_id: str
    secret: str = field(repr=False)
    base_url: str = PAPER_URL
    environment: str = "paper"
    key_hint: str = ""

    def __post_init__(self) -> None:
        if not self.key_hint:
            self.key_hint = hint_of(self.key_id)

    def __repr__(self) -> str:
        return f"AlpacaCredentials(key_hint={self.key_hint!r}, environment={self.environment!r})"

    __str__ = __repr__

    def headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret, "Accept": "application/json"}

    def redact(self, text: str | None) -> str | None:
        """`text` with the key id and the secret replaced by `***<hint>`."""
        if text is None:
            return None
        for value in (self.secret, self.key_id):
            if value:
                text = text.replace(value, f"***{self.key_hint}")
        return text


def hint_of(key: str) -> str:
    """The last 4 characters of the key, what probes may show."""
    return key[-4:] if key else ""


def normalize_base_url(value: str) -> tuple[str, str]:
    """(base URL without a trailing slash or /v2, "paper" or "live"); raises
    AlpacaConfigError naming the problem for anything that is not an Alpaca trading host."""
    raw = value.strip()
    parts = urlsplit(raw)
    if parts.scheme != "https":
        raise AlpacaConfigError(f"{BASE_URL_VAR} must use https")
    if parts.username or parts.password:
        raise AlpacaConfigError(f"{BASE_URL_VAR} must not carry credentials")
    if parts.query or parts.fragment:
        raise AlpacaConfigError(f"{BASE_URL_VAR} must not carry a query or fragment")
    host = (parts.hostname or "").lower().rstrip(".")
    if host not in TRADING_HOSTS or parts.port not in (None, 443):
        allowed = " or ".join(f"https://{h}" for h in TRADING_HOSTS)
        raise AlpacaConfigError(f"{BASE_URL_VAR} host {host!r} is not an Alpaca trading host ({allowed})")
    path = parts.path.rstrip("/")
    if path.endswith("/v2"):
        path = path[: -len("/v2")]
    if path:
        raise AlpacaConfigError(f"{BASE_URL_VAR} must be the bare host (optionally ending in /v2), not {parts.path!r}")
    return f"https://{host}", TRADING_HOSTS[host]


def load(env: Mapping[str, str] | None = None) -> AlpacaCredentials | None:
    """The credentials from the environment, or None when the key id or the secret is
    missing (or blank). A set but bad ALPACA_BASE_URL raises AlpacaConfigError."""
    source = os.environ if env is None else env
    key_id = (source.get(KEY_VAR) or "").strip()
    secret = (source.get(SECRET_VAR) or "").strip()
    if not key_id or not secret:
        return None
    base_text = (source.get(BASE_URL_VAR) or "").strip()
    base_url, environment = normalize_base_url(base_text) if base_text else (PAPER_URL, "paper")
    return AlpacaCredentials(key_id=key_id, secret=secret, base_url=base_url, environment=environment)
