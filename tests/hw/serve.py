"""A real uvicorn server for browser tools and tests: FLEET_DEV=1 on a free loopback port,
no background loop, pointed at whatever database URL it is given."""
from __future__ import annotations

import socket
import threading
import time
from typing import Any

import httpx
import uvicorn

from host.api.app import create_app
from host.config import Config


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    """uvicorn in a thread; `with Server(url) as s:` gives s.url once /healthz answers."""

    def __init__(self, database_url: str) -> None:
        port = free_port()
        self.url = f"http://127.0.0.1:{port}"
        cfg = Config.from_env({"FLEET_DEV": "1", "FLEET_PUBLIC_URL": self.url, "DATABASE_URL": database_url})
        self.server = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, name="hw-uvicorn", daemon=True)

    def __enter__(self) -> "Server":
        self.thread.start()
        deadline = time.monotonic() + 15
        with httpx.Client(base_url=self.url, trust_env=False, timeout=2.0) as client:
            while time.monotonic() < deadline:
                try:
                    if client.get("/healthz").status_code == 200:
                        return self
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise RuntimeError("host did not come up")

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(10)
