"""Exchange credentials for the exchange process only (docs/LIVE.md, "Credentials").

`load()` reads `POLYMARKET_US_API_KEY`, `POLYMARKET_US_API_SECRET` (the Ed25519 private
key seed, base64 or hex, auto-detected; a 64-byte secret key is accepted by taking its
first 32 bytes) and the optional `POLYMARKET_US_PASSPHRASE` once at start. The values
are never logged, stored or returned: `repr()` shows the key's last 4 characters only.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass, field
from typing import Mapping

KEY_VAR = "POLYMARKET_US_API_KEY"
SECRET_VAR = "POLYMARKET_US_API_SECRET"
PASSPHRASE_VAR = "POLYMARKET_US_PASSPHRASE"
SEED_BYTES = 32
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


class CredentialsError(ValueError):
    """The secret is present but not a usable Ed25519 seed (the message never carries it)."""


@dataclass(repr=False, eq=False)
class Credentials:
    """The API key, the 32-byte Ed25519 seed and the optional passphrase."""

    key: str
    secret: bytes = field(repr=False)
    passphrase: str | None = None
    key_hint: str = ""

    def __post_init__(self) -> None:
        if not self.key_hint:
            self.key_hint = hint_of(self.key)

    def __repr__(self) -> str:
        return f"Credentials(key_hint={self.key_hint!r})"

    __str__ = __repr__


def hint_of(key: str) -> str:
    """The last 4 characters of the key, what probes and the dashboard may show."""
    return key[-4:] if key else ""


def decode_secret(text: str) -> bytes:
    """The 32-byte seed from a hex or base64 string (64 or 128 hex characters are
    read as hex, anything else as standard or URL-safe base64, padding optional)."""
    raw = text.strip()
    if not raw:
        raise CredentialsError(f"{SECRET_VAR} is empty")
    data: bytes | None = None
    if len(raw) in (2 * SEED_BYTES, 4 * SEED_BYTES) and HEX_RE.match(raw):
        data = bytes.fromhex(raw)
    else:
        padded = raw + "=" * (-len(raw) % 4)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                data = decoder(padded, validate=True) if decoder is base64.b64decode else decoder(padded)
                break
            except (binascii.Error, ValueError):
                continue
    if data is None:
        raise CredentialsError(f"{SECRET_VAR} is neither hex nor base64")
    if len(data) == 2 * SEED_BYTES:
        data = data[:SEED_BYTES]
    if len(data) != SEED_BYTES:
        raise CredentialsError(f"{SECRET_VAR} decodes to {len(data)} bytes, expected a 32-byte Ed25519 seed (or 64-byte secret key)")
    return data


def load(env: Mapping[str, str] | None = None) -> Credentials | None:
    """The credentials from the environment, or None when the key or the secret is
    missing (or blank). A present but malformed secret raises CredentialsError."""
    source = os.environ if env is None else env
    key = (source.get(KEY_VAR) or "").strip()
    secret_text = source.get(SECRET_VAR) or ""
    if not key or not secret_text.strip():
        return None
    passphrase = source.get(PASSPHRASE_VAR)
    passphrase = passphrase if passphrase else None
    return Credentials(key=key, secret=decode_secret(secret_text), passphrase=passphrase, key_hint=hint_of(key))
