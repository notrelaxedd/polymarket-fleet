"""What the database may and may not decide about the signed live calls (docs/LIVE.md
"Live gateway"). Settings are writable by the owner API and by anything holding the
shared database role, so neither the destination of a signed request nor the bytes
the exchange process signs may come from them unchecked:

- `base_url` must be https and its host on the allowlist (`polymarket.us` and its
  subdomains); another host is only possible through `POLYMARKET_US_LIVE_BASE_URL`
  in exchange.env, which only the exchange process reads.
- the signing `template` must hold each of {timestamp}, {method}, {path} and {body}
  exactly once and nothing else but a few separator characters, so the signed message
  can never be an attacker-chosen order body.

Stdlib only: host.settings validates writes with it and the signing module enforces it.
"""
from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlsplit

ALLOWED_HOSTS = ("polymarket.us",)
BASE_URL_ENV = "POLYMARKET_US_LIVE_BASE_URL"
PLACEHOLDERS = ("{timestamp}", "{method}", "{path}", "{body}")
SEPARATORS_RE = re.compile(r"^[\s|:;,.\-_/#+]*$")
MAX_SEPARATORS = 8


def host_allowed(host: str, extra: str | None = None) -> bool:
    """`polymarket.us`, any of its subdomains, or the host of `extra` (the env override)."""
    host = host.lower().rstrip(".")
    if extra and host == extra.lower():
        return True
    return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_HOSTS)


def base_url_problem(url: Any, override: str | None = None) -> str | None:
    """Why `url` may not receive signed requests (None when it may)."""
    if not isinstance(url, str) or not url.strip():
        return "must be an https URL"
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        return "must use https"
    if not parts.hostname:
        return "must name a host"
    if parts.username or parts.password:
        return "must not carry credentials"
    if parts.query or parts.fragment:
        return "must not carry a query or fragment"
    if not host_allowed(parts.hostname, override):
        return f"host {parts.hostname!r} is not an allowed exchange host (polymarket.us or a subdomain)"
    return None


def env_base_url(env: Any = None) -> str | None:
    """The host-level override from exchange.env, validated for scheme; None when unset."""
    source = os.environ if env is None else env
    value = (source.get(BASE_URL_ENV) or "").strip()
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        return None
    return value


def env_override_host(env: Any = None) -> str | None:
    value = env_base_url(env)
    return urlsplit(value).hostname if value else None


def template_problem(template: Any) -> str | None:
    """Why `template` may not be signed (None when it may): every placeholder exactly
    once, no other placeholders or braces, at most a few separator characters."""
    if not isinstance(template, str) or not template:
        return "must be a string"
    rest = template
    for name in PLACEHOLDERS:
        if rest.count(name) != 1:
            return f"must contain {name} exactly once"
        rest = rest.replace(name, "")
    if "{" in rest or "}" in rest:
        return "must not contain other braces or placeholders"
    if len(rest) > MAX_SEPARATORS or not SEPARATORS_RE.match(rest):
        return "may only add a few separator characters between the placeholders"
    return None


def polymarket_us_problems(block: Any, override: str | None = None) -> list[str]:
    """The settings-side check of `market_source_config.polymarket_us`."""
    problems: list[str] = []
    if not isinstance(block, dict):
        return problems
    live = block.get("live")
    if isinstance(live, dict) and "base_url" in live:
        error = base_url_problem(live.get("base_url"), override)
        if error:
            problems.append(f"polymarket_us.live.base_url {error}")
    auth = block.get("auth")
    if isinstance(auth, dict) and auth.get("template") is not None:
        error = template_problem(auth.get("template"))
        if error:
            problems.append(f"polymarket_us.auth.template {error}")
    return problems
