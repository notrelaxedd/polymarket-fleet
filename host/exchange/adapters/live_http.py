"""The HTTP transport behind the live gateway: a urllib call with a timeout that
answers (status, response headers, body text) and raises SourceError on any transport
failure, GatewayTimeout (a SourceError and a TimeoutError) when the exchange did not
answer in time. Tests inject a callable of the same shape instead."""
from __future__ import annotations

import socket
from typing import Callable

from host.exchange.adapters.base import SourceError

MAX_BYTES = 8 * 1024 * 1024
Http = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, str], str]]


class GatewayTimeout(SourceError, TimeoutError):
    """The exchange did not answer in time: the order may or may not exist remotely."""


def urllib_http(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, dict[str, str], str]:
    """The default transport: (status, response headers, body text)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            return int(response.status), dict(response.headers.items()), raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_BYTES) if exc.fp else b""
        return int(exc.code), dict(exc.headers.items()) if exc.headers else {}, raw.decode("utf-8", "replace")
    except (socket.timeout, TimeoutError) as exc:
        raise GatewayTimeout(f"{method} {url} timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise GatewayTimeout(f"{method} {url} timed out after {timeout}s") from exc
        raise SourceError(f"{method} {url} failed: {exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise SourceError(f"{method} {url} failed: {exc}") from exc


def header(headers: dict[str, str] | None, name: str) -> str | None:
    """One response header, looked up without regard to case."""
    for key, value in (headers or {}).items():
        if key.lower() == name.lower():
            return value
    return None
