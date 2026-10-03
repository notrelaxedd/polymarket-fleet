"""Host entry point: migrate, start the loop thread, serve the API."""
from __future__ import annotations

import logging

import uvicorn

from host import db
from host.api.app import create_app
from host.config import Config
from host.loop import LoopThread

log = logging.getLogger("host.main")


def main() -> None:
    """python -m host.main"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    applied = db.migrate(config.database_url)
    log.info("migrations applied: %s", applied or "none")
    app = create_app(config)
    loop_pool = db.make_pool(config.database_url, min_size=1, max_size=2)
    loop = LoopThread(loop_pool, config.loop_seconds)
    loop.start()
    log.info("serving on %s (public url %s, dev=%s)", config.bind, config.public_url, config.dev)
    try:
        uvicorn.run(app, host=config.bind_host, port=config.bind_port, log_level="info")
    finally:
        loop.stop()
        loop_pool.close()


if __name__ == "__main__":
    main()
