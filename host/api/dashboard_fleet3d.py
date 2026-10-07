"""The 3D fleet control page at /fleet (docs/DASHBOARD.md "Fleet `/fleet`").

The page is a React + three.js build from fleet-ui/ (Vite, base '/fleet/'): one
index.html and hashed files under assets/. This module only serves those files; the
page itself talks to the owner JSON API (docs/PROTOCOL.md "Fleet UI additions"). The router is
included from host.api.dashboard, so every route here sits behind require_owner like
the rest of the dashboard: no owner login, no page and no assets.

Where the build lives, first match wins:
- FLEET_UI_DIR, when set (tests, unusual layouts);
- host/fleet_ui/, where the Docker image copies the build;
- <repo>/fleet-ui/dist, a local `npm run build` in a checkout.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from host import web
from host.api.deps import require_owner
from host.errors import NotFound

router = APIRouter(tags=["dashboard-fleet3d"], dependencies=[Depends(require_owner)])

UI_DIR_ENV = "FLEET_UI_DIR"
IMAGE_UI_DIR = web.PACKAGE_DIR / "fleet_ui"
CHECKOUT_UI_DIR = web.PACKAGE_DIR.parent / "fleet-ui" / "dist"

# Asset names carry a content hash, so a browser may keep them for good; "private"
# keeps shared caches (there should be none on a tailnet) from storing owner-only files.
ASSET_CACHE = "private, max-age=31536000, immutable"
CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
}

NOT_BUILT = web.ENV.from_string(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>503 3D page not built</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<main class="page" data-page="error">
  <h1>503 3D page not built</h1>
  <section class="card confirm" data-card="fleet-ui-missing">
    <p>The 3D fleet page has not been built: there is no index.html in {{ where }}.</p>
    <p class="help">Run npm ci &amp;&amp; npm run build in fleet-ui, or rebuild the Docker image
      (docker compose build host). The card view works without it.</p>
    <p class="actions"><a class="btn primary" href="/fleet/list" data-action="fleet-list">Open the card view</a>
      <a class="btn" href="/">Back to the dashboard</a></p>
  </section>
</main>
</body>
</html>
"""
)


def ui_dir() -> Path:
    """The folder holding the built index.html and assets/ (see the module docstring).

    Read on every request, so a build that lands while the host runs is picked up.
    """
    configured = os.environ.get(UI_DIR_ENV, "").strip()
    if configured:
        return Path(configured)
    if IMAGE_UI_DIR.is_dir():
        return IMAGE_UI_DIR
    return CHECKOUT_UI_DIR


def asset_file(root: Path, name: str) -> Path | None:
    """The file `name` under root/assets, or None when it is missing or would leave
    that folder (../, an absolute path, a symlink out): resolved, then checked."""
    assets = (root / "assets").resolve()
    try:
        candidate = (assets / name).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not candidate.is_relative_to(assets) or candidate == assets or not candidate.is_file():
        return None
    return candidate


def content_type(path: Path) -> str:
    """The media type by extension; anything unknown is served as opaque bytes (and
    X-Content-Type-Options: nosniff keeps the browser from guessing)."""
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


@router.get("/fleet", response_class=HTMLResponse)
def fleet3d_page() -> Response:
    """The built index.html, never cached (a rebuild changes the asset names it
    references); a 503 page pointing at the card view when there is no build."""
    root = ui_dir()
    index = root / "index.html"
    try:
        html = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        body = NOT_BUILT.render(where=str(root))
        return HTMLResponse(body, status_code=503, headers=web.NO_STORE)
    return HTMLResponse(html, headers=web.NO_STORE)


@router.get("/fleet/", include_in_schema=False)
def fleet3d_slash() -> Response:
    """/fleet/ is the same page: send the browser to /fleet."""
    return RedirectResponse("/fleet", status_code=308, headers=web.NO_STORE)


@router.get("/fleet/assets/{name:path}")
def fleet3d_asset(name: str) -> Response:
    """One hashed build file (JavaScript, CSS, fonts, images), cached for a year."""
    path = asset_file(ui_dir(), name)
    if path is None:
        raise NotFound("no such file")
    return FileResponse(path, media_type=content_type(path), headers={"Cache-Control": ASSET_CACHE})
