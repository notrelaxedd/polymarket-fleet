"""Screenshots and layout checks for the workloads pages. A dev tool, not a pytest test.

    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers .venv/bin/python tests/hw/shoot_workloads.py [OUT_DIR]

It seeds a throwaway database with tests/hw/seed_workloads.py (four workloads, five
machines in every state, jobs, logs, secrets, pending and done approvals), serves the app
with FLEET_DEV=1 and captures /fleet, /machines, /workloads, three workload pages,
/outbound, a machine's logs and the unpin page at 390x844 and 1280x800 (dark, the one scheme),
running the docs/UI.md assertions of tests/hw/ui_checks.py on each (no horizontal scroll,
h1 and a stat in the first screen, rows at most 88 px, 44 px targets, whole chips, every
"..." menu item on top). It also posts the enroll token form once. Exits 1 on any problem.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")

from tests.hw import seed_workloads  # noqa: E402
from tests.hw.seed_shots import drop_database, fresh_database  # noqa: E402
from tests.hw.serve import Server  # noqa: E402
from tests.hw.ui_checks import check_page  # noqa: E402

DEFAULT_OUT = Path(os.environ.get("SCREENSHOT_DIR", "/tmp/screenshots"))
VIEWPORTS = {"390": (390, 844), "1280": (1280, 800)}


def pages(ids: dict[str, str]) -> list[tuple[str, str]]:
    return [
        ("fleet", "/fleet/list"), ("machines", "/machines"), ("workloads", "/workloads"), ("workload-hello", "/workloads/hello"),
        ("workload-demo", "/workloads/demo-site"), ("workload-polymarket", "/workloads/polymarket"), ("outbound", "/outbound"),
        ("machine-logs", f"/machines/{ids['pi1']}/logs"), ("unpin", f"/machines/{ids['pi2']}/unpin"),
    ]


def capture_all(server_url: str, database_url: str, ids: dict[str, str], out: Path) -> list[str]:
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    written: list[str] = []
    with httpx.Client(base_url=server_url, trust_env=False) as client:
        resp = client.post("/machine-enroll-token", headers={"Origin": server_url})
        if resp.status_code != 200 or "install-agent.sh" not in resp.text:
            problems.append(f"enroll token page: {resp.status_code}")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, path in pages(ids):
            for width, (w, h) in VIEWPORTS.items():
                for scheme in ("dark",):  # one scheme: the site is dark only
                    context = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme)
                    page = context.new_page()
                    seed_workloads.touch(database_url)
                    page.goto(server_url + path, wait_until="networkidle")
                    label = f"{name}-{width}-{scheme}"
                    target = out / f"{label}.png"
                    page.screenshot(path=str(target), full_page=True)
                    written.append(str(target))
                    problems.extend(check_page(page, label, phone=width == "390"))
                    context.close()
        browser.close()
    for line in problems:
        print("PROBLEM", line)
    if problems:
        raise SystemExit(1)
    return written


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    database_url = fresh_database()
    try:
        ids = seed_workloads.seed(database_url)
        with Server(database_url) as server:
            for path in capture_all(server.url, database_url, ids, out):
                print(path)
    finally:
        drop_database(database_url)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
