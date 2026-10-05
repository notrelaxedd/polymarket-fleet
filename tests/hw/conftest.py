"""Keep the hardware tools out of pytest collection (they need a real fleet or a
browser), and the browser fixtures the Playwright tests share: `browser` (Chromium,
skipped when playwright or its build under PLAYWRIGHT_BROWSERS_PATH is missing; never
downloads one) and `server` (a real uvicorn on the per-test database)."""
from __future__ import annotations

import os
from typing import Any, Iterator

import pytest

from tests.hw.serve import Server

collect_ignore = ["screenshots.py"]
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright is not installed")
    with sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure means no browser here
            pytest.skip(f"no Chromium for Playwright: {exc}")
        yield chromium
        chromium.close()


@pytest.fixture
def server(test_db_url: str) -> Iterator[Server]:
    with Server(test_db_url) as live:
        yield live
