"""Ed25519 request signing for the Polymarket US gateway (docs/LIVE.md, "Signing").

UNVERIFIED against the real API: the message template, the timestamp unit, the
signature encoding and the header names all come from
`settings.market_source_config.polymarket_us.auth` with the defaults below.

    message   = template.format(timestamp=..., method=..., path=..., body=...)
    signature = Ed25519(seed).sign(message UTF-8), base64 (or hex)

`path` includes the query string, `method` is upper-cased, `body` is the exact bytes
sent (empty for GET). The same seed and message always give the same signature.
"""
from __future__ import annotations

import base64
from datetime import datetime
from typing import Any, Callable

from nacl.signing import SigningKey

from host.exchange.credentials import Credentials

AUTH_DEFAULTS: dict[str, Any] = {
    "template": "{timestamp}{method}{path}{body}",
    "timestamp": "ms",
    "encoding": "base64",
    "headers": {
        "key": "X-PM-Access-Key",
        "signature": "X-PM-Signature",
        "timestamp": "X-PM-Timestamp",
        "passphrase": "X-PM-Passphrase",
    },
    "server_time_field": None,
}


def _auth(auth_config: dict[str, Any] | None, key: str) -> Any:
    value = (auth_config or {}).get(key)
    return AUTH_DEFAULTS[key] if value is None else value


def header_names(auth_config: dict[str, Any] | None) -> dict[str, str]:
    """The configured header names merged over the defaults."""
    names = dict(AUTH_DEFAULTS["headers"])
    configured = (auth_config or {}).get("headers")
    if isinstance(configured, dict):
        names.update({k: str(v) for k, v in configured.items() if v})
    return names


def timestamp_now(auth_config: dict[str, Any] | None, clock: Callable[[], datetime]) -> str:
    """The current time as the API wants it: integer milliseconds (`"ms"`, default)
    or integer seconds (`"s"`) since the epoch, as a string."""
    unit = str(_auth(auth_config, "timestamp")).lower()
    seconds = clock().timestamp()
    if unit in ("s", "sec", "seconds"):
        return str(int(seconds))
    if unit in ("ms", "millis", "milliseconds"):
        return str(int(seconds * 1000))
    raise ValueError(f"auth.timestamp must be 'ms' or 's', not {unit!r}")


def message_for(method: str, path: str, body: bytes, timestamp: str, auth_config: dict[str, Any] | None) -> bytes:
    """The UTF-8 bytes that get signed."""
    template = str(_auth(auth_config, "template"))
    text = template.format(timestamp=timestamp, method=method.upper(), path=path, body=body.decode("utf-8"))
    return text.encode("utf-8")


def encode_signature(signature: bytes, auth_config: dict[str, Any] | None) -> str:
    encoding = str(_auth(auth_config, "encoding")).lower()
    if encoding == "base64":
        return base64.b64encode(signature).decode("ascii")
    if encoding in ("base64url", "urlsafe"):
        return base64.urlsafe_b64encode(signature).decode("ascii")
    if encoding == "hex":
        return signature.hex()
    raise ValueError(f"auth.encoding must be base64, base64url or hex, not {encoding!r}")


def sign(creds: Credentials, message: bytes) -> bytes:
    """The raw 64-byte Ed25519 signature of `message` under the credentials' seed."""
    return SigningKey(creds.secret).sign(message).signature


def sign_headers(
    creds: Credentials, method: str, path: str, body: bytes, timestamp: str, auth_config: dict[str, Any] | None
) -> dict[str, str]:
    """The authentication headers for one request: key, signature, timestamp and the
    passphrase header only when a passphrase is set."""
    names = header_names(auth_config)
    signature = sign(creds, message_for(method, path, body, timestamp, auth_config))
    headers = {
        names["key"]: creds.key,
        names["signature"]: encode_signature(signature, auth_config),
        names["timestamp"]: timestamp,
    }
    if creds.passphrase:
        headers[names["passphrase"]] = creds.passphrase
    return headers
