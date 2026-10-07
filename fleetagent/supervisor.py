"""The machine supervisor: BOOT -> REGISTER -> ACTIVE (design section 6).

Every `heartbeat_seconds` (a fixed monotonic schedule, so a slow tick never drifts it)
it sends specs, the native fleet-worker state, the container status, shipped logs and the
cleanup report, then reconciles the machine with the answer (fleetagent.reconcile).
Between heartbeats it polls the managed container once a second so a crashed workload is
restarted 3 s later. Losing the host never stops a container; stopping the agent
never stops one either (systemd KillMode=process).

Clock, sleep and the stop event are injectable so tests run it fast.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import fleetagent
from fleetagent import cleanup, config, http, specs, update
from fleetagent.docker import Docker
from fleetagent.logship import LogShipper
from fleetagent.reconcile import Desired, Reconciler
from fleetagent.registration import RegistrationMixin

log = logging.getLogger("fleetagent.supervisor")

EXIT_CONF_MISSING = 78
EXIT_UPDATED = 75
REGISTER_BACKOFF = (1, 2, 4, 8, 16, 30)
UPDATE_RETRY_SECONDS = 60.0


@dataclass
class Options:
    """Knobs that tests override."""

    heartbeat_seconds: float | None = None
    http_timeout: float = 4.0
    register_backoff: tuple[float, ...] = REGISTER_BACKOFF
    agent_version: str | None = None
    restart_delay: float = 3.0
    stats_interval: float = 15.0
    background_pull: bool = True
    poll_interval: float = 1.0
    self_update: bool = True
    cleanup_interval: float = cleanup.INTERVAL_SECONDS
    prune_images: bool = True
    prune_builder: bool = True
    labels: tuple[str, ...] = ("fleet.workload",)  # which containers are ours (tests on a shared daemon narrow it)
    repo_filter: Callable[[str], bool] | None = None  # which image repositories cleanup may remove


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Supervisor(RegistrationMixin):
    def __init__(
        self,
        state_dir: str | None = None,
        options: Options | None = None,
        docker: Docker | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] | None = None,
        stop: threading.Event | None = None,
        systemctl_runner: Callable[..., Any] = subprocess.run,
        root: bool | None = None,
        chown: Callable[[str, int, int], None] = os.chown,
        collector: specs.SpecsCollector | None = None,
    ) -> None:
        self.state_dir = state_dir or config.state_dir()
        self.options = options or Options()
        self.stop = stop or threading.Event()
        self._clock = clock
        self._sleep = sleep or (lambda seconds: self.stop.wait(seconds))
        self._systemctl = systemctl_runner
        self.docker = docker or Docker(binary=config.docker_binary())
        self.collector = collector or specs.SpecsCollector(self.docker)
        self.cleaner = cleanup.Cleaner(
            self.docker, clock, self.options.cleanup_interval, self.options.prune_images, self.options.prune_builder,
            self.options.labels, self.options.repo_filter,
        )
        self.logs = LogShipper(self.docker, self.state_dir)
        self.reconciler = Reconciler(
            self.docker, self.cleaner, self.logs, self.collector, self._call_start,
            run_dir=config.run_dir(), data_dir=config.data_dir(), clock=clock, note=self._note,
            restart_delay=self.options.restart_delay, stats_interval=self.options.stats_interval,
            background_pull=self.options.background_pull, root=root, chown=chown, labels=self.options.labels,
        )
        self.agent_version = self.options.agent_version or fleetagent.__version__
        self.heartbeat_seconds = self.options.heartbeat_seconds or 5.0
        self.conf: dict[str, Any] | None = None
        self.state = "BOOT"
        self.native: str | None = None
        self.desired: Desired | None = None
        self.host_agent_version: str | None = None
        self.exit_code: int | None = None
        self.heartbeat_count = 0
        self.misses = 0
        self.last_error: str | None = None
        self.last_specs: dict[str, Any] = {}
        self._update_failed_at: float | None = None

    # ------------------------------------------------------------------ helpers

    def _note(self, message: str) -> None:
        self.logs.add_agent_line(message, utcnow_iso())

    # ---------------------------------------------------------------- lifecycle

    def run_forever(self) -> int:
        if not self.boot():
            return EXIT_CONF_MISSING
        try:
            while not self.stop.is_set():
                if not self.register_with_backoff():
                    break
                code = self.active_loop()
                if code is not None:
                    return code
            return 0
        finally:
            self.shutdown()

    def run_once(self) -> int:
        """One pass for scripts: register, one heartbeat and reconcile. 0 when the host answered."""
        if not self.boot():
            return EXIT_CONF_MISSING
        try:
            if not self.register_once():
                return 1
            return 0 if self.tick() and self.misses == 0 else 1
        finally:
            self.shutdown()

    def boot(self) -> bool:
        self.state = "BOOT"
        try:
            self.conf = config.load_conf(self.state_dir)
        except config.ConfMissing as exc:
            log.error("agent.conf missing or unusable (%s); run enroll first", exc)
            return False
        log.info("machine %s, host %s, agent %s", self.conf["machine_id"], self.conf["host_url"], self.agent_version)
        self.reconciler.adopt()
        return True

    def shutdown(self) -> None:
        """Containers keep running; only the status file is refreshed."""
        self._write_status()

    # ---------------------------------------------------------------- main loop

    def active_loop(self) -> int | None:
        """Heartbeat on a fixed monotonic schedule. An exit code, or None to go back to REGISTER."""
        self.state = "ACTIVE"
        next_at = self._clock()
        next_poll = next_at + self.options.poll_interval
        while not self.stop.is_set():
            now = self._clock()
            if now >= next_at:
                next_at += self.heartbeat_seconds
                if next_at <= now:
                    next_at = now + self.heartbeat_seconds
                if not self.tick():
                    return None
                if self.exit_code is not None:
                    return self.exit_code
                continue
            if now >= next_poll:
                next_poll = now + self.options.poll_interval
                self.service()
            self._sleep(max(0.0, min(0.1, next_at - self._clock())))
        return None

    def service(self) -> None:
        """Between heartbeats: notice an exited container and restart it when due."""
        if self.desired is None or self.native not in ("inactive", "absent"):
            return
        if self.reconciler.restart_pending() or self.reconciler.poll_exit():
            self._reconcile()

    def _reconcile(self) -> None:
        try:
            self.reconciler.reconcile(self.desired, self.native)
        except Exception:
            log.exception("reconcile failed")
            self.last_error = "reconcile failed (see the agent log)"

    def tick(self) -> bool:
        """One heartbeat and its reconcile. False when the agent must re-register."""
        payload, batch, report = self.build_heartbeat()
        assert self.conf is not None
        try:
            resp = http.post_json(
                f"{self.conf['host_url']}/api/v1/machines/{self.conf['machine_id']}/heartbeat",
                payload, token=self.conf["machine_token"], timeout=self.options.http_timeout,
            )
        except http.HttpError as exc:
            self.last_error = str(exc)
            if exc.status in (401, 404):
                log.error("heartbeat refused (%s), re-registering", exc.status)
                self._write_status()
                return False
            return self._on_miss(str(exc))
        except http.HttpConnectionError as exc:
            return self._on_miss(str(exc))
        if not isinstance(resp, dict):
            return self._on_miss("bad heartbeat response")
        self.misses = 0
        self.last_error = None
        self.heartbeat_count += 1
        batch.commit()
        self.cleaner.acknowledge(report)
        self._on_response(resp)
        self._write_status()
        return True

    def _on_miss(self, reason: str) -> bool:
        self.misses += 1
        self.last_error = reason
        log.warning("heartbeat failed (%d): %s (containers keep running)", self.misses, reason)
        self._write_status()
        return True

    # ---------------------------------------------------------------- heartbeat

    def _probe_native(self) -> None:
        value = specs.native_polymarket(self._systemctl)
        if value is not None and value != self.native:
            log.info("native fleet-worker: %s", value)
        if value is not None:
            self.native = value

    def build_heartbeat(self) -> tuple[dict[str, Any], Any, cleanup.Report]:
        self._probe_native()
        specs_now = self.collector.collect()
        self.last_specs = specs_now
        free, size = specs_now.get("disk_free_mb"), specs_now.get("disk_size_mb")
        if free is not None:
            self.cleaner.report.low_disk = cleanup.is_low(free, size)
        batch = self.logs.collect(self.reconciler.log_targets())
        report = self.cleaner.snapshot()
        payload: dict[str, Any] = {
            "specs": specs_now,
            "acked_epoch": self.reconciler.acked_epoch,
            "container": self.reconciler.container_block(),
            "logs": batch.entries,
            "cleanup": report.as_dict(),
        }
        if self.native is not None:
            payload["native_polymarket"] = self.native
        return payload, batch, report

    def _on_response(self, resp: dict[str, Any]) -> None:
        self._apply_common(resp)
        if "epoch" in resp and "workload" in resp and "run" in resp:
            keep = resp.get("keep_images")
            self.desired = Desired(
                epoch=resp.get("epoch"), workload=resp.get("workload"),
                run=resp.get("run") if isinstance(resp.get("run"), dict) else None,
                keep_images=[str(k) for k in keep] if isinstance(keep, list) else None,
            )
            self._reconcile()
            if self.native in ("inactive", "absent"):
                try:
                    self.reconciler.hourly(self.desired)
                except Exception:
                    log.exception("cleanup failed")
        self._maybe_self_update()

    # -------------------------------------------------------------- self-update

    def _maybe_self_update(self) -> None:
        if not self.options.self_update or not self.host_agent_version or self.host_agent_version == self.agent_version:
            return
        if self.reconciler.busy() or self.exit_code is not None:
            return
        now = self._clock()
        if self._update_failed_at is not None and now - self._update_failed_at < UPDATE_RETRY_SECONDS:
            return
        assert self.conf is not None
        log.info("host agent %s differs from running %s: self-updating", self.host_agent_version, self.agent_version)
        try:
            version = update.self_update(self.conf["host_url"], config.app_dir(self.state_dir), self.agent_version)
        except (update.UpdateError, OSError) as exc:
            log.error("self-update failed: %s", exc)
            self._update_failed_at = now
            self.last_error = f"self-update failed: {exc}"
            return
        if version is None:
            self._update_failed_at = now
            return
        log.info("updated to %s; exiting 75 for restart (containers keep running)", version)
        self.exit_code = EXIT_UPDATED

    # ------------------------------------------------------------------- status

    def _write_status(self) -> None:
        config.save_status(self.state_dir, {
            "at": utcnow_iso(),
            "state": self.state,
            "agent_version": self.agent_version,
            "host_agent_version": self.host_agent_version,
            "heartbeat_seconds": self.heartbeat_seconds,
            "heartbeat_count": self.heartbeat_count,
            "misses": self.misses,
            "native_polymarket": self.native,
            "acked_epoch": self.reconciler.acked_epoch,
            "container": self.reconciler.container_block(),
            "low_disk": self.cleaner.report.low_disk,
            "last_error": self.last_error,
        })
