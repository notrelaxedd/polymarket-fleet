"""The host side of the parity run: a fresh Postgres database, the host app (uvicorn
on 127.0.0.1:<free port>, FLEET_DEV=1), the host loop and the exchange loop on the sim
source with a frozen clock, all in this process (the way tests/test_e2e.py and
tests/e2e_trading.py run them)."""
from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import psycopg
import uvicorn
from psycopg.conninfo import make_conninfo

from host import db
from host.api.app import create_app
from host.config import Config
from host.exchange.adapters.sim import SimSource
from host.exchange.main import ExchangeLoop
from host.loop import LoopThread

log = logging.getLogger("parity.host")
LOOP_SECONDS = 0.5
POLL = 0.1


def wait_for(predicate: Callable[[], Any], what: str, timeout: float = 30.0, poll: float = POLL) -> Any:
    """Poll until predicate() is truthy; raise TimeoutError with the last value."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(poll)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}; last={last!r}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FrozenClock:
    """The SimSource clock: one fixed minute for the whole run, so every snapshot of a
    market carries the same book (host/exchange/adapters/sim.py seeds the book by
    game_id and minute). The snapshot rows keep the real clock."""

    def __init__(self, at: datetime) -> None:
        self.at = at.astimezone(timezone.utc).replace(second=0, microsecond=0)

    def __call__(self) -> datetime:
        return self.at

    def shifted(self, minutes: int) -> datetime:
        return self.at + timedelta(minutes=minutes)


def create_database(admin_url: str, label: str) -> str:
    """CREATE DATABASE polymarket_parity_<label>_<hex> and migrate it; returns its URL."""
    name = f"polymarket_parity_{label}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_conninfo(admin_url, dbname=name)
    db.migrate(url)
    return url


def drop_database(admin_url: str, url: str) -> None:
    name = psycopg.conninfo.conninfo_to_dict(url)["dbname"]
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@dataclass
class HostApp:
    """A running host: app, loop thread and exchange thread. `client` talks to it over TCP."""

    database_url: str
    clock: FrozenClock
    deploy_dir: Path
    url: str = ""
    code_version: str = ""
    client: httpx.Client = field(init=False)
    _threads: list[Any] = field(default_factory=list)
    _pools: list[Any] = field(default_factory=list)
    _server: Any = None
    _exchange_stop: threading.Event = field(default_factory=threading.Event)

    def start(self) -> "HostApp":
        port = free_port()
        self.url = f"http://127.0.0.1:{port}"
        cfg = Config.from_env({"FLEET_DEV": "1", "FLEET_PUBLIC_URL": self.url, "DATABASE_URL": self.database_url,
                               "FLEET_DEPLOY_DIR": str(self.deploy_dir)})
        app = create_app(cfg)
        self.code_version = app.state.bundle.code_version
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        server_thread = threading.Thread(target=self._server.run, name="parity-uvicorn", daemon=True)
        server_thread.start()
        self._threads.append(server_thread)
        self.client = httpx.Client(base_url=self.url, trust_env=False, timeout=10.0)
        wait_for(lambda: self._server.started and self._healthy(), "host to come up")
        loop_pool = db.make_pool(self.database_url, min_size=1, max_size=2)
        self._pools.append(loop_pool)
        loop = LoopThread(loop_pool, LOOP_SECONDS)
        loop.start()
        self._threads.append(loop)
        return self

    def start_exchange(self) -> None:
        """The real exchange loop (host/exchange/main.py) on the sim source and the frozen clock."""
        pool = db.make_pool(self.database_url, min_size=1, max_size=4)
        self._pools.append(pool)
        loop = ExchangeLoop(pool)
        loop.source = SimSource(clock=self.clock)
        loop.source_name = "sim"
        thread = threading.Thread(target=loop.run_forever, args=(self._exchange_stop,), name="parity-exchange", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _healthy(self) -> bool:
        try:
            return self.client.get("/healthz").status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self._exchange_stop.set()
        for thread in self._threads:
            if isinstance(thread, LoopThread):
                thread.stop()
        if self._server is not None:
            self._server.should_exit = True
        for thread in self._threads:
            thread.join(10.0)
        for pool in self._pools:
            pool.close()
        self.client.close()

    # ------------------------------------------------------------- API helpers

    def get(self, path: str) -> Any:
        resp = self.client.get(path)
        if resp.status_code != 200:
            raise RuntimeError(f"GET {path}: {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def post(self, path: str, body: dict[str, Any] | None = None, expect: int = 200) -> Any:
        resp = self.client.post(path, json=body)
        if resp.status_code != expect:
            raise RuntimeError(f"POST {path}: {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def worker(self, worker_id: str) -> dict[str, Any] | None:
        return next((w for w in self.get("/api/fleet")["workers"] if w["id"] == worker_id), None)

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with db.connect(self.database_url) as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]
