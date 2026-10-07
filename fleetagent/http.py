"""Tiny JSON HTTP client over urllib (stdlib only, self-contained).

HttpError: the server answered with a non-2xx status (carries status and detail).
HttpConnectionError: no usable answer (refused, timeout, DNS, reset, bad JSON).
Proxies from the environment are ignored on purpose: the agent talks to the host
over the tailnet. Response bodies are never logged (/start carries secrets).
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 4.0


class HttpError(Exception):
    """The server answered with a non-2xx status."""

    def __init__(self, status: int, detail: str, url: str = "") -> None:
        super().__init__(f"HTTP {status} from {url}: {detail}")
        self.status = status
        self.detail = detail
        self.url = url


class HttpConnectionError(Exception):
    """No usable answer: connection refused, timeout, DNS failure, reset."""


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _detail_from_body(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip()[:200]
    if isinstance(data, dict) and "detail" in data:
        return str(data["detail"])[:200]
    return text.strip()[:200]


def request_bytes(method: str, url: str, body: Any = None, token: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Perform one request and return the raw response body."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": "fleet-agent"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        raise HttpError(exc.code, _detail_from_body(raw), url) from None
    except (urllib.error.URLError, http.client.HTTPException, socket.timeout, OSError) as exc:
        raise HttpConnectionError(f"{method} {url}: {exc}") from None


def request_json(method: str, url: str, body: Any = None, token: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """Perform one request and decode the JSON body (an empty body decodes to None)."""
    raw = request_bytes(method, url, body=body, token=token, timeout=timeout)
    if not raw.strip():
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise HttpConnectionError(f"{method} {url}: invalid JSON in response: {exc}") from None


def get_json(url: str, token: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    return request_json("GET", url, token=token, timeout=timeout)


def post_json(url: str, body: Any, token: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    return request_json("POST", url, body=body, token=token, timeout=timeout)


def get_bytes(url: str, token: str | None = None, timeout: float = 60.0) -> bytes:
    """GET a binary document (the agent tarball)."""
    return request_bytes("GET", url, token=token, timeout=timeout)
