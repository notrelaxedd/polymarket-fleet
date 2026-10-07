"""The 3D fleet page at /fleet (host/api/dashboard_fleet3d.py): the built index.html and
its hashed assets, behind the same owner auth as every dashboard page; the card grid
moved to /fleet/list."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from host.api import dashboard_fleet3d
from host.api.app import DASHBOARD_HEADERS, create_app
from host.api.deps import require_owner
from tests.pagecheck import page

INDEX = (
    '<!doctype html><html lang="en"><head><title>Fleet control</title>'
    '<script type="module" crossorigin src="/fleet/assets/app-1a2b3c.js"></script>'
    '<link rel="stylesheet" crossorigin href="/fleet/assets/app-1a2b3c.css"></head>'
    '<body><div id="root"></div></body></html>'
)
OWNER = {"Tailscale-User-Login": "owner@example.com"}
IMMUTABLE = "private, max-age=31536000, immutable"


@pytest.fixture
def ui_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake Vite build in FLEET_UI_DIR, with a secret beside it that no URL may reach."""
    root = tmp_path / "fleet_ui"
    (root / "assets" / "fonts").mkdir(parents=True)
    (root / "index.html").write_text(INDEX)
    (root / "assets" / "app-1a2b3c.js").write_text("console.log('fleet');\n")
    (root / "assets" / "app-1a2b3c.css").write_text("body { margin: 0; }\n")
    (root / "assets" / "fonts" / "mono.woff2").write_bytes(b"wOF2fake")
    (root / "assets" / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    (root / "assets" / "dot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "secret.txt").write_text("not for the browser")
    monkeypatch.setenv("FLEET_UI_DIR", str(root))
    return root


def _strict(config):
    return dataclasses.replace(config, dev=False, owner_login="owner@example.com")


def test_fleet_serves_the_built_index_without_caching(client, ui_dir):
    r = client.get("/fleet")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.text == INDEX
    assert r.headers["cache-control"] == "no-store"
    for name, value in DASHBOARD_HEADERS:
        assert r.headers[name.decode()] == value.decode(), name
    r = client.get("/fleet/", follow_redirects=False)
    assert r.status_code in (307, 308) and r.headers["location"] == "/fleet"
    assert client.get("/fleet/").text == INDEX


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("app-1a2b3c.js", "text/javascript"), ("app-1a2b3c.css", "text/css"), ("fonts/mono.woff2", "font/woff2"),
        ("logo.svg", "image/svg+xml"), ("dot.png", "image/png"),
    ],
)
def test_assets_have_their_type_and_a_long_cache(client, ui_dir, name, content_type):
    r = client.get(f"/fleet/assets/{name}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].split(";")[0] == content_type
    assert r.headers["cache-control"] == IMMUTABLE
    assert r.content == (ui_dir / "assets" / name).read_bytes()
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "path",
    [
        "/fleet/assets/%2e%2e/index.html",
        "/fleet/assets/%2e%2e/%2e%2e/secret.txt",
        "/fleet/assets/fonts/%2e%2e/%2e%2e/%2e%2e/secret.txt",
        "/fleet/assets/%2E%2E%2Fsecret.txt",
        "/fleet/assets/..%2f..%2fsecret.txt",
        "/fleet/assets//etc/passwd",
        "/fleet/assets/%2Fetc%2Fpasswd",
        "/fleet/assets/",
        "/fleet/assets/fonts",
        "/fleet/assets/missing.js",
    ],
)
def test_asset_paths_cannot_leave_the_assets_folder(client, ui_dir, path):
    r = client.get(path)
    assert r.status_code == 404, (path, r.status_code)
    assert "not for the browser" not in r.text and "root:" not in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_asset_file_refuses_traversal(ui_dir, tmp_path):
    """The check itself, for the forms an HTTP client would normalise away."""
    assert dashboard_fleet3d.asset_file(ui_dir, "app-1a2b3c.js") == (ui_dir / "assets" / "app-1a2b3c.js").resolve()
    for name in ("../index.html", "../../secret.txt", "fonts/../../index.html", str(tmp_path / "secret.txt"),
                 "/etc/passwd", "", ".", "fonts", "fonts/"):
        assert dashboard_fleet3d.asset_file(ui_dir, name) is None, name
    (ui_dir / "assets" / "escape.txt").symlink_to(tmp_path / "secret.txt")
    assert dashboard_fleet3d.asset_file(ui_dir, "escape.txt") is None, "a symlink out of assets/"


def test_ui_dir_lookup(monkeypatch, tmp_path):
    monkeypatch.setenv("FLEET_UI_DIR", str(tmp_path))
    assert dashboard_fleet3d.ui_dir() == tmp_path
    monkeypatch.setenv("FLEET_UI_DIR", "")
    expected = dashboard_fleet3d.IMAGE_UI_DIR if dashboard_fleet3d.IMAGE_UI_DIR.is_dir() else dashboard_fleet3d.CHECKOUT_UI_DIR
    assert dashboard_fleet3d.ui_dir() == expected
    assert dashboard_fleet3d.CHECKOUT_UI_DIR.parts[-2:] == ("fleet-ui", "dist")


def test_missing_build_is_a_503_pointing_at_the_cards(client, tmp_path, monkeypatch):
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    monkeypatch.setenv("FLEET_UI_DIR", str(empty))
    r = client.get("/fleet")
    assert r.status_code == 503 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
    p = page(r.text)
    card = p.card("fleet-ui-missing")
    assert "npm ci && npm run build in fleet-ui" in card.text and "rebuild the Docker image" in card.text
    assert card.action("fleet-list").target == "/fleet/list" and "/static/style.css" in p.hrefs
    assert client.get("/fleet/assets/app.js").status_code == 404


def test_the_fleet3d_router_carries_require_owner():
    """Included from the dashboard router (which carries require_owner for every page) and
    carrying it itself, so the page stays behind the owner check even if it is ever
    registered on its own. The 401 and 403 tests below check the behaviour."""
    from host.api import dashboard

    for router in (dashboard.router, dashboard_fleet3d.router):
        assert any(d.dependency is require_owner for d in router.dependencies)
    assert {r.path for r in dashboard_fleet3d.router.routes} == {"/fleet", "/fleet/", "/fleet/assets/{name:path}"}


def test_fleet_needs_the_owner_login(config, ui_dir):
    with TestClient(create_app(_strict(config))) as c:
        for path in ("/fleet", "/fleet/", "/fleet/assets/app-1a2b3c.js", "/fleet/list"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 401, path
            assert "Fleet control" not in r.text and "console.log" not in r.text
            r = c.get(path, headers={"Tailscale-User-Login": "intruder@example.com"}, follow_redirects=False)
            assert r.status_code == 401, path
        assert c.get("/fleet", headers=OWNER).text == INDEX
        assert c.get("/fleet/assets/app-1a2b3c.js", headers=OWNER).status_code == 200


def test_fleet_refused_from_a_worker_machine(config, conn, make_worker, ui_dir):
    w = make_worker("box1")
    conn.execute("UPDATE workers SET remote_ip = '100.64.0.7' WHERE id = %s", (w.id,))
    with TestClient(create_app(_strict(config))) as c:
        from_worker = {**OWNER, "X-Forwarded-For": "100.64.0.7"}
        for path in ("/fleet", "/fleet/assets/app-1a2b3c.js", "/fleet/list"):
            r = c.get(path, headers=from_worker)
            assert r.status_code == 403 and w.id in r.text, path
        assert c.get("/fleet", headers={**OWNER, "X-Forwarded-For": "100.64.0.9"}).status_code == 200


def test_card_grid_lives_at_fleet_list(client, make_worker, ui_dir):
    w = make_worker("box1")
    r = client.get("/fleet/list")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    p = page(r.text)
    assert p.page_name == "fleet" and p.has(f'#fleet-grid [data-row="worker"][data-id="{w.id}"]')
    nav = p.nav("fleet")
    assert nav.target == "/fleet" and nav.is_current, "Fleet is current on the card view too"
    three_d = p.one('a[data-action="fleet-3d"]')
    assert three_d.target == "/fleet" and three_d.text == "3D view"
    assert client.get("/fragments/fleet").status_code == 200
    home = page(client.get("/").text)
    assert home.nav("fleet").target == "/fleet" and not home.nav("fleet").is_current
    r = client.post(f"/workers/{w.id}/role", data={"role": "backtest"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/fleet/list"
