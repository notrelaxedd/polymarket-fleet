"""Server-rendered HTML: the Jinja2 environment, display filters, redirects and error pages.

Templates live in host/templates and static files in host/static, both resolved
relative to this package so they work from a checkout and from the installed
package inside the Docker image.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, pass_context

from host.money import cents_to_dollars, format_cents

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

STATUS_TEXT = {
    400: "Bad request",
    401: "Not signed in",
    403: "Forbidden",
    404: "Not found",
    409: "Conflict",
    413: "Too large",
    500: "Server error",
    502: "Upstream unavailable",
}
FLASH_COOKIE = "flash"
FLASH_MAX_AGE = 60
# Pages are personal and change every few seconds: never cache them (the enroll token
# page in particular must not come back from the back-forward cache).
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
STATUS_HELP = {
    401: "The dashboard is reachable through tailscale serve only, which adds your tailnet "
         "login to every request. That login is missing or is not the configured owner.",
    403: "The request came from a worker machine or carried an Origin the host does not allow.",
}


def pct(value: Any) -> str:
    """0.42 -> "42%"."""
    try:
        return f"{int(round(float(value or 0) * 100))}%"
    except (TypeError, ValueError):
        return "0%"


def pct1(value: Any) -> str:
    """0.004 -> "0.4%"; "-" when missing (a null drawdown is not 0%)."""
    if value is None or isinstance(value, bool):
        return "-"
    try:
        return f"{100 * float(value):.1f}%"
    except (TypeError, ValueError):
        return "-"


def gb(value: Any) -> str:
    """Megabytes -> gigabytes with one decimal."""
    try:
        return f"{float(value or 0) / 1024:.1f}"
    except (TypeError, ValueError):
        return "0.0"


def ago(seconds: Any) -> str:
    """Seconds -> "3 s ago" / "2 min ago" / "4 h ago" / "never"."""
    if seconds is None:
        return "never"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def zone(name: Any) -> tzinfo:
    """The owner's time zone from settings.tz; UTC when it is missing or unknown."""
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except (KeyError, ValueError, OSError):
            return timezone.utc
    return timezone.utc


def format_ts(value: Any, tz: tzinfo = timezone.utc) -> str:
    """Datetime -> "2026-10-02 23:20:34 EDT" in the given zone; anything else as text."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    return "" if value is None else str(value)


@pass_context
def ts(context: Any, value: Any) -> str:
    """Template filter: format_ts in the zone the page context carries as `tz`."""
    return format_ts(value, zone(context.get("tz")))


def pretty_json(value: Any) -> str:
    """Indented JSON for checkpoints, params and results."""
    if value is None:
        return ""
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def compact_json(value: Any) -> str:
    """One-line JSON for table cells (event details)."""
    if value is None:
        return ""
    return json.dumps(value, sort_keys=True, separators=(", ", ": "), default=str)


def short(value: Any, length: int = 8) -> str:
    """First characters of a uuid or hash."""
    return str(value or "")[:length]


def fixed(value: Any, digits: int = 3) -> str:
    """A metric with a fixed number of decimals; "-" when missing."""
    if value is None or isinstance(value, bool):
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def signed_pct(value: Any, digits: int = 1) -> str:
    """0.021 -> "+2.1%"; "-" when missing."""
    if value is None or isinstance(value, bool):
        return "-"
    try:
        return f"{100 * float(value):+.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def price(value: Any) -> str:
    """A contract price: 0.52 -> "0.52", 0.525 -> "0.525"; "-" when missing."""
    if value is None or isinstance(value, bool):
        return "-"
    try:
        text = f"{float(value):.4f}".rstrip("0")
    except (TypeError, ValueError):
        return "-"
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def pvalue(value: Any) -> str:
    """A p-value: "p < 0.001" below 0.001 (the smallest a 10 000 flip test can give is
    1/10001, which "0.000" would overstate), else "p = 0.012"; "-" when missing."""
    if value is None or isinstance(value, bool):
        return "-"
    try:
        p = float(value)
    except (TypeError, ValueError):
        return "-"
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def season_span(seasons: Any) -> str:
    """[2010, ..., 2025] -> "2010-2025"; "-" when empty."""
    if isinstance(seasons, (list, tuple)) and seasons:
        return f"{seasons[0]}-{seasons[-1]}" if len(seasons) > 1 else str(seasons[0])
    return "-"


def make_env() -> Environment:
    """Autoescaping environment with the dashboard filters."""
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True, trim_blocks=True, lstrip_blocks=True)
    env.filters.update(
        {"money": format_cents, "dollars": cents_to_dollars, "pct": pct, "pct1": pct1, "gb": gb, "ago": ago,
         "ts": ts, "pretty_json": pretty_json, "compact_json": compact_json, "short": short,
         "fixed": fixed, "signed_pct": signed_pct, "season_span": season_span, "price": price,
         "pvalue": pvalue}
    )
    return env


ENV = make_env()


def flash_from(request: Request) -> str | None:
    """The flash message a redirect left in the flash cookie (rendered escaped).

    It is a cookie rather than a query parameter so a link somebody sends the owner
    cannot put arbitrary text into the status line, and so a reload does not repeat it.
    """
    value = request.cookies.get(FLASH_COOKIE)
    return unquote(value)[:300] if value else None


def render(request: Request, template: str, status: int = 200, **context: Any) -> HTMLResponse:
    """Render a template with the request, path, flash and time zone in the context.

    Every HTML response is sent with Cache-Control: no-store. A flash shown on this
    page is consumed: the cookie is cleared with the response.
    """
    context.setdefault("request", request)
    context.setdefault("path", request.url.path)
    context.setdefault("tz", None)
    context.setdefault("flash", flash_from(request))
    html = ENV.get_template(template).render(**context)
    response = HTMLResponse(html, status_code=status, headers=NO_STORE)
    if context["flash"]:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def redirect(url: str, flash: str | None = None) -> RedirectResponse:
    """303 back to a page, optionally with a flash message in a short-lived cookie."""
    response = RedirectResponse(url, status_code=303, headers=NO_STORE)
    if flash:
        response.set_cookie(
            FLASH_COOKIE, quote(flash[:300]), max_age=FLASH_MAX_AGE, path="/", httponly=True, samesite="lax"
        )
    return response


def safe_next(value: str | None, default: str) -> str:
    """A local redirect target from a form field; anything else falls back to the default."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return default


def wants_html(request: Request) -> bool:
    """Dashboard paths get HTML errors; everything under /api stays JSON."""
    return not request.url.path.startswith("/api/")


def error_response(request: Request, status: int, detail: str) -> Response:
    """JSON for the API, a small HTML page for the dashboard."""
    if not wants_html(request):
        return JSONResponse({"detail": detail}, status_code=status)
    return render(
        request, "error.html", status=status, status_code=status, title=STATUS_TEXT.get(status, "Error"),
        detail=detail, help=STATUS_HELP.get(status),
    )
