"""Talking to the host's register and /start routes (a mixin of fleetagent.supervisor.Supervisor).

register: the agent presents machine_id and its current machine token; the host rotates
the token and keeps the previous one, so a register whose answer was lost (the conf
still holds the old token) is accepted again. The new token is written to agent.conf
before anything else uses it.
"""

from __future__ import annotations

import logging
from typing import Any

from fleetagent import config, http, launch, procinfo

log = logging.getLogger("fleetagent.registration")


class RegistrationMixin:
    # Provided by Supervisor (declared for type checkers).
    conf: dict[str, Any] | None
    state_dir: str
    state: str
    options: Any
    stop: Any
    collector: Any
    agent_version: str
    heartbeat_seconds: float
    host_agent_version: str | None
    misses: int
    last_error: str | None
    _sleep: Any

    def _write_status(self) -> None: ...  # pragma: no cover

    def _call_start(self, epoch: int) -> tuple[str, dict[str, str]]:
        """POST /start: the run token and secrets for the container of `epoch`. Never logged."""
        assert self.conf is not None
        resp = http.post_json(
            f"{self.conf['host_url']}/api/v1/machines/{self.conf['machine_id']}/start",
            {"epoch": epoch}, token=self.conf["machine_token"], timeout=15.0,
        )
        if not isinstance(resp, dict) or not resp.get("run_token"):
            raise http.HttpConnectionError("bad /start response")
        secrets = resp.get("secrets") or {}
        if not isinstance(secrets, dict):
            raise http.HttpConnectionError("bad /start secrets")
        return str(resp["run_token"]), {str(k): str(v) for k, v in secrets.items()}

    def register_payload(self) -> dict[str, Any]:
        assert self.conf is not None
        body: dict[str, Any] = {
            "machine_id": self.conf["machine_id"],
            "machine_token": self.conf["machine_token"],
            "hostname": procinfo.hostname(),
            "boot_id": procinfo.boot_id(),
            "agent_version": self.agent_version,
            "specs": self.collector.collect(),
        }
        if self.conf.get("name"):
            body["name"] = self.conf["name"]
        return body

    def register_once(self) -> bool:
        assert self.conf is not None
        self.state = "REGISTER"
        try:
            resp = http.post_json(
                self.conf["host_url"] + "/api/v1/machines/register", self.register_payload(), timeout=self.options.http_timeout
            )
        except http.HttpError as exc:
            self.last_error = str(exc)
            log.warning("registration failed: %s", exc)
            return False
        except http.HttpConnectionError as exc:
            self.last_error = str(exc)
            log.warning("host unreachable during register: %s", exc)
            return False
        if not isinstance(resp, dict) or not resp.get("machine_token"):
            self.last_error = "bad register response"
            log.error(self.last_error)
            return False
        self.conf["machine_token"] = str(resp["machine_token"])
        if resp.get("machine_id"):
            self.conf["machine_id"] = str(resp["machine_id"])
        config.save_conf(self.state_dir, self.conf)
        launch.clear_pending(config.app_dir(self.state_dir))
        self._apply_common(resp)
        self.misses = 0
        self.last_error = None
        log.info("registered as %s", self.conf["machine_id"])
        self._write_status()
        return True

    def register_with_backoff(self) -> bool:
        """Retry registration with 1,2,4..30 s backoff until it succeeds or stop is set."""
        attempt = 0
        while not self.stop.is_set():
            if self.register_once():
                return True
            delay = self.options.register_backoff[min(attempt, len(self.options.register_backoff) - 1)]
            attempt += 1
            self._write_status()
            self._sleep(delay)
        return False

    def _apply_common(self, resp: dict[str, Any]) -> None:
        seconds = resp.get("heartbeat_seconds")
        if self.options.heartbeat_seconds is None and isinstance(seconds, (int, float)) and 0.1 <= seconds <= 300:
            self.heartbeat_seconds = float(seconds)
        if resp.get("agent_version"):
            self.host_agent_version = str(resp["agent_version"])
