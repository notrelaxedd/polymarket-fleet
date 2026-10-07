"""Container reconciliation: make the machine match the host's desired (epoch, workload, run).

Design section 6 step 3:
  * a fleet container that is not desired (other workload or older epoch) is stopped
    (`docker stop -t <stop_timeout_s>`), removed, and its scratch and secret files wiped;
  * a desired workload that is not running is started (fleetagent.starter);
  * a desired container that exited is restarted 3 s later unless its exit code is in
    no_restart_exit_codes (then it is reported failed), like systemd Restart=always,
    RestartSec=3, RestartPreventExitStatus=78;
  * running containers are adopted by their labels after an agent restart;
  * nothing here runs when the host is unreachable (the supervisor only reconciles after
    an answer), and nothing is started or stopped while the native fleet-worker is active.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Sequence

from fleetagent import cleanup, runargs, secretfiles, specs, workdirs
from fleetagent.docker import ContainerInfo, Docker, DockerError
from fleetagent.logship import LogShipper
from fleetagent.starter import StartMixin
from fleetagent.state import (
    DEFAULT_STOP_TIMEOUT, RESTART_DELAY, STATS_INTERVAL, Desired, Managed, StartFn,
)

log = logging.getLogger("fleetagent.reconcile")

__all__ = ["Desired", "Managed", "Reconciler"]


class Reconciler(StartMixin):
    def __init__(
        self,
        docker: Docker,
        cleaner: cleanup.Cleaner,
        logs: LogShipper,
        collector: specs.SpecsCollector,
        start_fn: StartFn,
        *,
        run_dir: str,
        data_dir: str,
        clock: Callable[[], float] = time.monotonic,
        note: Callable[[str], None] | None = None,
        restart_delay: float = RESTART_DELAY,
        stats_interval: float = STATS_INTERVAL,
        background_pull: bool = True,
        root: bool | None = None,
        chown: Callable[[str, int, int], None] = os.chown,
        labels: Sequence[str] = ("fleet.workload",),
    ) -> None:
        self.docker = docker
        self.labels = list(labels)
        self.cleaner = cleaner
        self.logs = logs
        self.collector = collector
        self.start_fn = start_fn
        self.run_dir = run_dir
        self.data_dir = data_dir
        self._clock = clock
        self._note = note or (lambda msg: None)
        self.restart_delay = restart_delay
        self.stats_interval = stats_interval
        self.background_pull = background_pull
        self.root = secretfiles.is_root() if root is None else root
        self._chown = chown
        self.managed: Managed | None = None
        self.acked_epoch = 0
        self.seen: list[ContainerInfo] = []
        self.started_this_tick = False
        self._images: dict[str, str] = {}
        self._stop_timeouts: dict[str, int] = {}
        self._pull: dict[str, Any] | None = None
        self._start_retry_at = 0.0

    # ----------------------------------------------------------------- helpers

    def _say(self, message: str) -> None:
        log.info("%s", message)
        self._note(message)

    def _record(self, want: tuple[str, int]) -> Managed:
        if self.managed is None or (self.managed.workload, self.managed.epoch) != want:
            self.managed = Managed(want[0], want[1])
        return self.managed

    def _remember_secrets(self, c: ContainerInfo) -> None:
        """Rebuild the redaction list of an adopted container from its secret files and run token."""
        if not c.workload:
            return
        values = list(secretfiles.read_values(self.run_dir, c.workload).values())
        token = c.env_value(runargs.TOKEN_ENV)
        if token:
            values.append(token)
        known = self.logs.secrets.get(c.workload, [])
        self.logs.set_secrets(c.workload, list(dict.fromkeys(known + values)))

    def busy(self) -> bool:
        """True while a start or pull is in progress (no self-update then)."""
        return self.started_this_tick or (self._pull is not None and self._pull["thread"].is_alive())

    def log_targets(self) -> list[tuple[str, str]]:
        return [(c.id, c.workload or "") for c in self.seen]

    def protected_images(self) -> list[str]:
        return [c.image_id for c in self.seen if c.image_id]

    def container_block(self) -> dict[str, Any] | None:
        return self.managed.block() if self.managed else None

    def restart_pending(self) -> bool:
        return self.managed is not None and self.managed.state == "exited"

    # ---------------------------------------------------------------- adoption

    def adopt(self) -> None:
        """Pick up fleet containers that are already there (agent restart)."""
        try:
            containers = self.docker.ps(self.labels)
        except DockerError as exc:
            log.warning("cannot list containers: %s", exc)
            return
        self.seen = containers
        for c in containers:
            self._remember_secrets(c)
        running = [c for c in containers if c.running and c.workload and c.epoch is not None]
        if running:
            best = max(running, key=lambda c: c.created)
            self._mark_running(self._record((best.workload or "", best.epoch or 0)), best)
            log.info("adopted container %s (%s epoch %s)", best.name, best.workload, best.epoch)

    def _mark_running(self, m: Managed, c: ContainerInfo) -> None:
        m.state, m.container_id, m.exit_code, m.error, m.restart_at = "running", c.id, None, None, None
        m.started_at = c.started_at
        m.image_digest = runargs.image_digest(c.image) or m.image_digest
        self.acked_epoch = max(self.acked_epoch, m.epoch)
        if self._clock() - m.stats_at >= self.stats_interval:
            m.stats_at = self._clock()
            stats = self.docker.stats(c.id)
            if stats:
                m.cpu_pct, m.mem_mb = float(stats["cpu_pct"]), int(stats["mem_mb"])

    # ---------------------------------------------------------------- removal

    def _remove(self, c: ContainerInfo, keep_files: bool = False) -> bool:
        """Stop (when running), drain its logs, remove it, wipe scratch and secrets. False when it stays."""
        w = c.workload or ""
        try:
            if c.running:
                timeout = c.stop_timeout or self._stop_timeouts.get(w) or DEFAULT_STOP_TIMEOUT
                self.docker.stop(c.id, timeout)
            self.logs.drain(c.id, w)
            self.docker.rm(c.id)
        except DockerError as exc:
            log.warning("cannot remove container %s: %s", c.name, exc)
            return False
        self._say(f"removed container {c.name}")
        if w and not keep_files:
            workdirs.wipe_dir(workdirs.scratch_dir(self.data_dir, w), self.docker, self._images.get(w))
            secretfiles.remove_secrets(self.run_dir, w)
        return True

    # --------------------------------------------------------------- reconcile

    def reconcile(self, desired: Desired | None, native: str | None) -> None:
        """One pass. `native` is the native fleet-worker state; only "inactive" and "absent" allow actions."""
        self.started_this_tick = False
        try:
            containers = self.docker.ps(self.labels)
        except DockerError as exc:
            log.warning("cannot list containers: %s", exc)
            return
        self.seen = containers
        self.logs.prune([c.id for c in containers])
        want = desired.want if desired else None
        if desired and desired.run and desired.workload:
            self._stop_timeouts[desired.workload] = int(desired.run.get("stop_timeout_s") or DEFAULT_STOP_TIMEOUT)
            self._images[desired.workload] = str(desired.run.get("image") or "")
        if native not in ("inactive", "absent"):
            self._observe_only(containers)
            return
        match: ContainerInfo | None = None
        strays: list[ContainerInfo] = []
        for c in sorted(containers, key=lambda x: x.created, reverse=True):
            if want and (c.workload, c.epoch) == want and match is None:
                match = c
            else:
                strays.append(c)
        clean = True
        removed: set[str] = set()
        for c in strays:
            keep_files = match is not None and want is not None and c.workload == want[0]
            if self._remove(c, keep_files):
                removed.add(c.id)
            else:
                clean = False
        if removed:
            # A removed container no longer protects its image: cleanup may free it right away.
            self.seen = [c for c in self.seen if c.id not in removed]
            self.cleaner.request()
        if want is None:
            self.managed = None
            self._pull = None
            if clean and desired is not None and desired.epoch is not None:
                self.acked_epoch = max(self.acked_epoch, int(desired.epoch))
            return
        assert desired is not None and desired.run is not None
        m = self._record(want)
        if match is not None and match.running:
            self._mark_running(m, match)
            self._remember_secrets(match)
        elif match is not None:
            self._handle_exit(m, match, desired)
        elif self._clock() >= self._start_retry_at:
            self._start(m, desired)

    def _observe_only(self, containers: list[ContainerInfo]) -> None:
        """Native fleet-worker active (or unknown): report what is there, touch nothing."""
        running = [c for c in containers if c.running]
        if not running:
            self.managed = None
            return
        best = max(running, key=lambda c: c.created)
        self._mark_running(self._record((best.workload or "", best.epoch or 0)), best)

    def _handle_exit(self, m: Managed, c: ContainerInfo, desired: Desired) -> None:
        code = c.exit_code if c.exit_code is not None else 1
        m.container_id, m.exit_code = c.id, code
        no_restart = [int(x) for x in (desired.run or {}).get("no_restart_exit_codes") or []]
        if code in no_restart:
            if m.state != "failed":
                self._say(f"container {c.name} exited with code {code}; not restarting")
            m.state, m.error, m.restart_at = "failed", f"exit code {code}: not restarted", None
            return
        if m.restart_at is None:
            m.state = "exited"
            m.error = "killed by the kernel (out of memory)" if c.oom_killed else None
            m.restart_at = self._clock() + self.restart_delay
            self._say(f"container {c.name} exited with code {code}; restarting in {self.restart_delay:g} s")
        if self._clock() < m.restart_at:
            return
        if not self._remove(c):
            return
        m.restarts += 1
        m.restart_at = None
        self._start(m, desired)

    # ------------------------------------------------------------- housekeeping

    def hourly(self, desired: Desired | None) -> None:
        """The hourly cleanup (not while a pull runs)."""
        if not self.cleaner.due() or self.busy():
            return
        self.cleaner.run(desired.keep_images if desired else None, self.protected_images(), desired.want if desired else None)

    def poll_exit(self) -> bool:
        """Cheap check between heartbeats: True when the managed container is no longer running."""
        m = self.managed
        if m is None or m.container_id is None or m.state != "running":
            return False
        try:
            info = self.docker.inspect(m.container_id)
        except DockerError:
            return False
        return info is None or not info.running
