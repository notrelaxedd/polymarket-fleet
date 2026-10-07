"""Starting a workload container: image, run token, secret files, scratch, `docker run`.

A mixin of fleetagent.reconcile.Reconciler (it uses the reconciler's docker, cleaner,
logs, run_dir, data_dir and clock). Order per design section 6 step 3: low-disk check,
pull by digest, POST /start, secret files (0400), fresh scratch, `docker run -d`; the
epoch is acknowledged only when the run succeeded.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from fleetagent import http, runargs, secretfiles, workdirs
from fleetagent.docker import DockerError
from fleetagent.state import PULL_RETRY_SECONDS, START_RETRY_SECONDS, Desired, Managed

log = logging.getLogger("fleetagent.starter")


class StartMixin:
    # Provided by Reconciler (declared for type checkers).
    docker: Any
    cleaner: Any
    logs: Any
    collector: Any
    start_fn: Any
    run_dir: str
    data_dir: str
    root: bool
    background_pull: bool
    acked_epoch: int
    seen: list[Any]
    started_this_tick: bool
    _clock: Any
    _chown: Any
    _pull: dict[str, Any] | None
    _start_retry_at: float

    def _say(self, message: str) -> None: ...  # pragma: no cover
    def protected_images(self) -> list[str]: ...  # pragma: no cover

    def _fail(self, m: Managed, state: str, message: str, retry: float = START_RETRY_SECONDS) -> None:
        if m.error != message:
            self._say(message)
        m.state, m.error = state, message
        self._start_retry_at = self._clock() + retry

    def _measure(self) -> tuple[int | None, int | None]:
        root = self.collector.docker_root
        if not root:
            return None, None
        return self.collector.free_mb(root), self.collector.disk(root).size_mb

    def _ensure_image(self, m: Managed, desired: Desired, ref: str) -> bool:
        """True when the image is available locally; starts or finishes a pull otherwise."""
        try:
            if self.docker.image_present(ref):
                self._pull = None
                return True
        except DockerError as exc:
            self._fail(m, "starting", f"docker image check failed: {exc}")
            return False
        task = self._pull
        if task is not None and task["ref"] == ref:
            if task["thread"].is_alive():
                m.state, m.error = "starting", None
                return False
            self._pull = None
            if task["error"]:
                self._fail(m, "starting", f"pull failed: {task['error']}", PULL_RETRY_SECONDS)
                return False
            return True
        if not self.cleaner.guard(self._measure, desired.keep_images, self.protected_images(), desired.want):
            self._fail(m, "failed", "low_disk: not enough free space to pull the image", PULL_RETRY_SECONDS)
            return False
        self._say(f"pulling {ref}")
        task = {"ref": ref, "error": None}

        def pull() -> None:
            try:
                self.docker.pull(ref)
            except DockerError as exc:
                task["error"] = str(exc)

        thread = threading.Thread(target=pull, name="fleet-pull", daemon=True)
        task["thread"] = thread
        thread.start()
        if not self.background_pull:
            thread.join()
            if task["error"]:
                self._fail(m, "starting", f"pull failed: {task['error']}", PULL_RETRY_SECONDS)
                return False
            return True
        self._pull = task
        m.state, m.error = "starting", None
        return False

    def _start(self, m: Managed, desired: Desired) -> None:
        run = desired.run or {}
        workload, epoch, ref = m.workload, m.epoch, str(run.get("image") or "")
        m.state, m.container_id = "starting", None
        if not ref:
            self._fail(m, "failed", "the host sent a run block without an image")
            return
        if not self._ensure_image(m, desired, ref):
            return
        try:
            token, secrets = self.start_fn(epoch)
        except http.HttpError as exc:
            self._fail(m, "starting", f"start refused ({exc.status}): {exc.detail}")
            return
        except http.HttpConnectionError as exc:
            self._fail(m, "starting", f"start request failed: {exc}")
            return
        uid = runargs.run_uid(run)
        self.logs.set_secrets(workload, list(secrets.values()) + [token])
        try:
            sdir = secretfiles.write_secrets(self.run_dir, workload, secrets, uid, root=self.root, chown=self._chown)
            scratch = workdirs.prepare_scratch(self.data_dir, workload, uid, self.root, self._chown, self.docker, ref)
            state = workdirs.prepare_state(self.data_dir, workload, uid, self.root, self._chown) if run.get("state_volume") else None
        except (OSError, ValueError) as exc:
            secretfiles.remove_secrets(self.run_dir, workload)
            self._fail(m, "failed", f"cannot prepare directories: {exc}")
            return
        args = runargs.build_run_args(
            workload, epoch, run, scratch=scratch, state=state, secrets=sdir, group_add=secretfiles.container_group(self.root)
        )
        try:
            cid = self.docker.run(args, env={**os.environ, runargs.TOKEN_ENV: token})
        except DockerError as exc:
            self._cleanup_failed_run(workload, epoch)
            self._fail(m, "starting", f"docker run failed: {exc}")
            return
        m.state, m.container_id, m.error, m.exit_code, m.restart_at = "running", cid, None, None, None
        m.image_digest = runargs.image_digest(ref)
        m.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        m.stats_at = float("-inf")
        self.acked_epoch = max(self.acked_epoch, epoch)
        self.started_this_tick = True
        self._start_retry_at = 0.0
        started = self.docker.inspect(cid)
        if started is not None:
            self.seen.append(started)  # its logs ship from the next heartbeat on
        self._say(f"started {runargs.container_name(workload, epoch)} ({cid[:12]})")
        protect = self.protected_images() + ([m.image_digest] if m.image_digest else [])
        self.cleaner.run(desired.keep_images, protect, desired.want)

    def _cleanup_failed_run(self, workload: str, epoch: int) -> None:
        try:
            self.docker.rm(runargs.container_name(workload, epoch))
        except DockerError:
            pass
        secretfiles.remove_secrets(self.run_dir, workload)
