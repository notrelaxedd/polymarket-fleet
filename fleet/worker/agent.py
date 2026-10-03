"""Worker agent: BOOT -> REGISTER -> ACTIVE (-> DRAINING -> ACTIVE) per docs/PROTOCOL.md.

The loop is driven by run_forever(); tick() sends one heartbeat and applies the
answer. Clock, sleep and the stop event are injectable so tests can run it fast.

Finished jobs: a /complete or /fail call that the host has not acknowledged stays in
pending_posts (persisted to the state dir), its job is kept in heartbeat jobs[] so the
lease stays alive, and it is re-keyed when a register hands the job back with a new
lease token. Posts are never dropped on re-register, self-update or shutdown.

Lease deadline: last_ok_at is the send time of the last acknowledged heartbeat.
Runners are killed once lease_seconds - heartbeat_seconds - http_timeout have passed
without an answer (checked on every miss and in the 0.1 s service loop), so a runner
never outlives its lease on the host.

Release reasons: every released[] entry and every /checkpoint release carries a
"reason" (drain, preempt, cancel, shutdown, oom; "stopped" for a runner that an
external SIGTERM ended while the agent was not shutting down).

Memory watchdog: the 0.1 s service loop asks fleet.worker.watchdog which runners
exceed watchdog_rss_fraction of the machine's RAM; those are stopped and released
with reason "oom" and the trip is counted in status.json.

Kill flag: mirrors settings.kill_switch and only concerns the trade role. Batch
roles keep claiming and running under kill; on_kill() is the step 4 hook.

Step 3: before a backtest, model_search or train runner starts, fleet.worker.context
refreshes the games cache and fetches the job's model into job["context"] (a failure
fails the job); a result with create_models becomes a post sequence (fleet.worker.posts)
that creates the models, posts backtest metrics and only then completes the job.
"""

from __future__ import annotations

import datetime as dt
import logging
import platform
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import fleet
from fleet.common import http, sysinfo
from fleet.worker import config, context, launch, posts, update
from fleet.worker.posts import PendingPost
from fleet.worker.runner import Runner
from fleet.worker.watchdog import MemoryWatchdog

log = logging.getLogger("fleet.agent")

BATCH_ROLES = ("backtest", "model_search", "train")
EXIT_CONF_MISSING = 78
EXIT_UPDATED = 75
REGISTER_BACKOFF = (1, 2, 4, 8, 16, 30)
UPDATE_RETRY_SECONDS = 60.0
MAX_CHAINED_RESPONSES = 3
SHUTDOWN_FLUSH_ATTEMPTS = 3
SHUTDOWN_FLUSH_DELAY = 1.0
OUT_OF_CYCLE_TIMEOUT = 2.0


@dataclass
class RunningJob:
    """A job this worker holds, with its runner child."""

    job: dict[str, Any]
    lease_token: str
    runner: Runner
    sent_seq: int = 0
    pending_seq: int | None = None

    @property
    def job_id(self) -> str:
        return str(self.job["id"])


@dataclass
class AgentOptions:
    """Knobs that tests override."""

    heartbeat_seconds: float | None = None
    http_timeout: float = 4.0
    drain_grace: float = 3.0
    python: str | None = None
    register_backoff: tuple[float, ...] = REGISTER_BACKOFF
    code_version: str | None = None
    shutdown_flush_delay: float = SHUTDOWN_FLUSH_DELAY
    watchdog_rss_fraction: float = 0.8
    ram_total_mb: int | None = None
    watchdog_interval: float = 1.0
    data_timeout: float = 15.0


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _parse_server_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "unknown error"


class Agent:
    """The worker state machine."""

    def __init__(
        self,
        state_dir: str | None = None,
        options: AgentOptions | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self.state_dir = state_dir or config.state_dir()
        self.options = options or AgentOptions()
        self.stop = stop or threading.Event()
        self._clock = clock
        self._wall = wall
        self._sleep = sleep or (lambda seconds: self.stop.wait(seconds))
        self.heartbeat_seconds = self.options.heartbeat_seconds or 5.0
        self.lease_seconds = 30.0
        self.code_version = self.options.code_version or fleet.__version__
        self.conf: dict[str, Any] | None = None
        self.state = "BOOT"
        self.role = "idle"
        self.acked_epoch = 0
        self.desired_role = "idle"
        self.role_epoch = 0
        self.kill = False
        self.stopping = False
        self.running: dict[str, RunningJob] = {}
        self.pending_releases: list[dict[str, Any]] = []
        self.pending_posts: list[PendingPost] = []
        self.misses = 0
        self.degraded = False
        self.last_ok_at: float | None = None
        self.skew_ms: int | None = None
        self.host_code_version: str | None = None
        self.exit_code: int | None = None
        self.heartbeat_count = 0
        self.last_response: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._update_failed_at: float | None = None
        self._cpu = sysinfo.CpuMeter()
        self.watchdog = MemoryWatchdog(
            fraction=self.options.watchdog_rss_fraction,
            ram_total_mb=self.options.ram_total_mb,
            interval=self.options.watchdog_interval,
            clock=self._clock,
        )

    # ------------------------------------------------------------ lifecycle

    def run_forever(self) -> int:
        """Run until the stop event is set or an exit code is requested."""
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

    def boot(self) -> bool:
        """Load worker.conf and any posts left unsent by an earlier run. False when the conf is missing (exit 78)."""
        self.state = "BOOT"
        try:
            self.conf = config.load_conf(self.state_dir)
        except config.ConfMissing as exc:
            log.error("worker.conf missing or unusable (%s); run enroll first", exc)
            return False
        self.pending_posts = [PendingPost.from_dict(p) for p in config.load_pending_posts(self.state_dir)]
        if self.pending_posts:
            log.info("resending %d unsent post(s) from the previous run", len(self.pending_posts))
        log.info("worker %s, host %s, code %s", self.conf["worker_id"], self.conf["host_url"], self.code_version)
        return True

    def shutdown(self) -> None:
        """Complete finished runners, stop the rest and hand everything back (best effort)."""
        self.stopping = True
        if self.conf is None or self.state == "BOOT":
            return
        self.service_runners()
        if self.running:
            log.info("shutting down: draining %d runner(s)", len(self.running))
            for rj in self._stop_runners(list(self.running), self.options.drain_grace):
                self.pending_releases.append(self._release_entry(rj, "shutdown"))
        self.flush_posts(attempts=SHUTDOWN_FLUSH_ATTEMPTS, delay=self.options.shutdown_flush_delay)
        if self.pending_releases:
            try:
                self._post_heartbeat(self.build_heartbeat())
            except (http.HttpError, http.HttpConnectionError) as exc:
                log.warning("final heartbeat failed: %s", exc)
        self._save_pending_posts()

    # ------------------------------------------------------------- register

    def register_payload(self) -> dict[str, Any]:
        assert self.conf is not None
        return {
            "worker_id": self.conf["worker_id"],
            "worker_token": self.conf["worker_token"],
            "hostname": sysinfo.hostname(),
            "python_version": platform.python_version(),
            "code_version": self.code_version,
            "boot_id": sysinfo.boot_id(),
        }

    def register_once(self) -> bool:
        """One registration attempt. True on success."""
        assert self.conf is not None
        self.state = "REGISTER"
        sent_at = self._clock()
        try:
            resp = http.post_json(
                self.conf["host_url"] + "/api/v1/workers/register",
                self.register_payload(),
                timeout=self.options.http_timeout,
            )
        except http.HttpError as exc:
            self.last_error = str(exc)
            if exc.status == 401:
                log.error("registration refused (401): token rotated elsewhere or worker removed; retrying")
            else:
                log.warning("registration failed: %s", exc)
            return False
        except http.HttpConnectionError as exc:
            self.last_error = str(exc)
            log.warning("host unreachable during register: %s", exc)
            return False
        if not isinstance(resp, dict) or not resp.get("worker_token"):
            self.last_error = f"bad register response: {resp!r}"
            log.error(self.last_error)
            return False
        self._apply_register(resp, sent_at)
        return True

    def _apply_register(self, resp: dict[str, Any], sent_at: float) -> None:
        assert self.conf is not None
        self.conf["worker_token"] = str(resp["worker_token"])
        if resp.get("worker_id"):
            self.conf["worker_id"] = str(resp["worker_id"])
        config.save_conf(self.state_dir, self.conf)
        launch.clear_pending(config.app_dir(self.state_dir))
        self._apply_common_fields(resp)
        self.role = self.desired_role
        self.acked_epoch = self.role_epoch
        self.misses = 0
        self.degraded = False
        self.last_ok_at = sent_at
        self.last_error = None
        held = resp.get("held_jobs") or []
        log.info("registered as %s, role %s epoch %s, %d held job(s)", self.conf["worker_id"], self.role, self.acked_epoch, len(held))
        for job in held:
            self._start_job(job)
        self.flush_posts()
        self._write_status()

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

    # ------------------------------------------------------------ main loop

    def active_loop(self) -> int | None:
        """Heartbeat on a fixed monotonic schedule. Returns an exit code, or None
        when the agent must go back to REGISTER."""
        self.state = "ACTIVE"
        next_at = self._clock()
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
            if self.service_runners():
                self.flush_posts()
            if self.misses and self._lease_overdue():
                self._leave_for_register("lease deadline passed between heartbeats")
                return None
            self._sleep(max(0.0, min(0.1, next_at - self._clock())))
        return None

    def tick(self) -> bool:
        """Service runners, send one heartbeat, apply the answer, then retry queued posts.
        False when the agent must re-register."""
        self.service_runners()
        payload = self.build_heartbeat()
        sent_at = self._clock()
        try:
            resp = self._post_heartbeat(payload)
        except http.HttpError as exc:
            self.last_error = str(exc)
            if exc.status in (401, 404):
                log.error("heartbeat refused (%s), re-registering", exc.status)
                self._write_status()
                return self._leave_for_register("heartbeat refused")
            return self._on_miss(str(exc))
        except http.HttpConnectionError as exc:
            return self._on_miss(str(exc))
        self._on_heartbeat_ok(resp, sent_at)
        self.flush_posts()
        self._write_status()
        return True

    def _leave_for_register(self, reason: str) -> bool:
        """Kill runners and go back to REGISTER. Unsent posts and releases are kept and
        re-keyed when the register hands their jobs back."""
        if self.running:
            log.error("%s: killing %d runner(s), re-registering", reason, len(self.running))
        self._kill_all_runners()
        return False

    def _on_miss(self, reason: str) -> bool:
        self.misses += 1
        self.last_error = reason
        if self.misses >= 2 and not self.degraded:
            self.degraded = True
            log.warning("degraded: %d consecutive heartbeat failures (%s)", self.misses, reason)
        else:
            log.warning("heartbeat failed (%d): %s", self.misses, reason)
        self._write_status()
        if self._lease_overdue():
            return self._leave_for_register("no heartbeat answered for %.1fs (lease deadline %.1fs)" % (self._since_ok(), self.lease_deadline()))
        return True

    def lease_deadline(self) -> float:
        """Seconds after the last acknowledged heartbeat was sent at which runners must be
        dead: one period plus one timeout before the host lease can expire."""
        margin = self.lease_seconds - self.heartbeat_seconds - self.options.http_timeout
        return max(margin, self.lease_seconds / 2.0)

    def _since_ok(self) -> float:
        if self.last_ok_at is None:
            return 0.0
        return self._clock() - self.last_ok_at

    def _lease_overdue(self) -> bool:
        return self.last_ok_at is not None and self._since_ok() >= self.lease_deadline()

    # ------------------------------------------------------------ heartbeat

    def build_heartbeat(self) -> dict[str, Any]:
        jobs: list[dict[str, Any]] = []
        for rj in self.running.values():
            checkpoint, progress, seq = rj.runner.snapshot()
            entry: dict[str, Any] = {"id": rj.job_id, "lease_token": rj.lease_token, "progress": progress}
            if seq != rj.sent_seq and checkpoint is not None:
                entry["checkpoint"] = checkpoint
                rj.pending_seq = seq
            jobs.append(entry)
        for post in self.pending_posts:
            if post.job_id and post.job_id not in self.running:
                jobs.append({"id": post.job_id, "lease_token": post.body.get("lease_token"), "progress": post.progress})
        return {
            "cpu_pct": self._cpu.sample(),
            "ram_used_mb": sysinfo.ram_used_mb(),
            "ram_total_mb": sysinfo.ram_total_mb(),
            "reported_role": self.role,
            "acked_epoch": self.acked_epoch,
            "jobs": jobs,
            "released": list(self.pending_releases),
            "want_job": self.wants_job(),
            "code_version": self.code_version,
            "skew_ms": self.skew_ms,
        }

    def wants_job(self) -> bool:
        """Batch roles claim regardless of the kill flag (it only concerns trade)."""
        return self.role in BATCH_ROLES and not self.running and not self.stopping

    def on_kill(self) -> None:
        """Called when the kill flag turns on. A no-op for batch roles; step 4 makes
        a trade worker stop proposing and cancel its open orders here."""
        return None

    def _post_heartbeat(self, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        assert self.conf is not None
        resp = http.post_json(
            f"{self.conf['host_url']}/api/v1/workers/{self.conf['worker_id']}/heartbeat",
            payload,
            token=self.conf["worker_token"],
            timeout=timeout if timeout is not None else self.options.http_timeout,
        )
        if not isinstance(resp, dict):
            raise http.HttpConnectionError(f"bad heartbeat response: {resp!r}")
        self.heartbeat_count += 1
        self.pending_releases.clear()
        for rj in self.running.values():
            if rj.pending_seq is not None:
                rj.sent_seq = rj.pending_seq
                rj.pending_seq = None
        return resp

    def _on_heartbeat_ok(self, resp: dict[str, Any], sent_at: float) -> None:
        self.misses = 0
        if self.degraded:
            log.info("heartbeat recovered")
        self.degraded = False
        self.last_ok_at = sent_at
        self.last_error = None
        self.last_response = resp
        self._apply_common_fields(resp)
        self.handle_response(resp)

    def _apply_common_fields(self, resp: dict[str, Any]) -> None:
        if isinstance(resp.get("desired_role"), str):
            self.desired_role = resp["desired_role"]
        if isinstance(resp.get("role_epoch"), int):
            self.role_epoch = resp["role_epoch"]
        kill = bool(resp.get("kill", False))
        if kill and not self.kill:
            log.warning("kill switch is on (trade only; batch jobs keep running)")
            self.on_kill()
        elif self.kill and not kill:
            log.info("kill switch reset")
        self.kill = kill
        if resp.get("code_version"):
            self.host_code_version = str(resp["code_version"])
        hb = resp.get("heartbeat_seconds")
        if self.options.heartbeat_seconds is None and isinstance(hb, (int, float)) and hb > 0:
            self.heartbeat_seconds = float(hb)
        server = _parse_server_time(resp.get("server_time"))
        if server is not None:
            self.skew_ms = int(round((self._wall() - server) * 1000))

    def handle_response(self, resp: dict[str, Any], depth: int = 0) -> None:
        """Apply lost/claimed/preempt/role change/self-update from a heartbeat answer."""
        for job_id in resp.get("lost") or []:
            self._forget_lost(str(job_id))
        for job in resp.get("claimed") or []:
            self._start_job(job)
        cancelled = {str(j) for j in (resp.get("cancel") or [])}
        role_change = self.desired_role != self.role or self.role_epoch != self.acked_epoch
        preempt = [str(j) for j in (resp.get("preempt") or []) if str(j) in self.running]
        if preempt and not role_change:
            log.info("preempt requested for %s", ", ".join(preempt))
            for rj in self._stop_runners(preempt, self.options.drain_grace):
                self.pending_releases.append(self._release_entry(rj, "cancel" if rj.job_id in cancelled else "preempt"))
        if role_change:
            # A role change arrives with preempt[] for the other-role jobs; the drain
            # stops them all and the release reason names the cause (drain, or cancel).
            self.drain(cancelled)
            if depth < MAX_CHAINED_RESPONSES:
                self._out_of_cycle_heartbeat(depth + 1)
            return
        self._maybe_self_update()

    def _out_of_cycle_heartbeat(self, depth: int) -> None:
        """Report the new role right away: a short timeout and one immediate retry."""
        timeout = min(self.options.http_timeout, OUT_OF_CYCLE_TIMEOUT)
        for attempt in (1, 2):
            sent_at = self._clock()
            try:
                resp = self._post_heartbeat(self.build_heartbeat(), timeout=timeout)
                break
            except (http.HttpError, http.HttpConnectionError) as exc:
                if attempt == 1:
                    log.warning("out-of-cycle heartbeat failed: %s; retrying once", exc)
                    continue
                log.warning("out-of-cycle heartbeat failed again: %s (releases retried next cycle)", exc)
                return
        self.last_ok_at = sent_at
        self.last_response = resp
        self._apply_common_fields(resp)
        self.handle_response(resp, depth)

    # ---------------------------------------------------------------- drain

    def drain(self, cancelled: set[str] | None = None) -> None:
        """Stop every runner, release the jobs (reason drain, or cancel for the ids in
        cancelled), adopt the desired role and epoch."""
        self.state = "DRAINING"
        log.info("role change %s/%s -> %s/%s: draining %d runner(s)", self.role, self.acked_epoch, self.desired_role, self.role_epoch, len(self.running))
        for rj in self._stop_runners(list(self.running), self.options.drain_grace):
            entry = self._release_entry(rj, "cancel" if rj.job_id in (cancelled or ()) else "drain")
            if not self._release_now(entry):
                self.pending_releases.append(entry)
        self.role = self.desired_role
        self.acked_epoch = self.role_epoch
        self.state = "ACTIVE"

    def _release_now(self, entry: dict[str, Any]) -> bool:
        """POST /checkpoint release=true so the release is acknowledged before the switch.
        True when the host answered for good (2xx or 4xx); False when it must be carried
        in the next heartbeat's released[] (no answer or 5xx)."""
        assert self.conf is not None
        body = {
            "lease_token": entry["lease_token"],
            "checkpoint": entry["checkpoint"],
            "progress": entry["progress"],
            "release": True,
        }
        if isinstance(entry.get("reason"), str):
            body["reason"] = entry["reason"]
        try:
            http.post_json(
                f"{self.conf['host_url']}/api/v1/jobs/{entry['id']}/checkpoint",
                body,
                token=self.conf["worker_token"],
                timeout=self.options.http_timeout,
            )
        except http.HttpError as exc:
            if exc.status >= 500:
                log.warning("release of %s failed (%s); carried in the next heartbeat", entry["id"], exc.status)
                return False
            log.warning("release of %s refused (%s); job is no longer ours", entry["id"], exc.status)
            return True
        except http.HttpConnectionError as exc:
            log.warning("release of %s not delivered (%s); carried in the next heartbeat", entry["id"], exc)
            return False
        return True

    def _stop_runners(self, job_ids: list[str], grace: float) -> list[RunningJob]:
        """SIGTERM the given runners in parallel, SIGKILL survivors after grace."""
        stopped: list[RunningJob] = []
        targets = [self.running.pop(j) for j in job_ids if j in self.running]
        for rj in targets:
            rj.runner.terminate()
        deadline = time.monotonic() + grace
        for rj in targets:
            if not rj.runner.wait(max(0.0, deadline - time.monotonic())):
                log.warning("runner %s ignored SIGTERM, killing", rj.job_id)
                rj.runner.kill()
                rj.runner.wait(5.0)
            rj.runner.reap_group()
            stopped.append(rj)
        return stopped

    def _release_entry(self, rj: RunningJob, reason: str) -> dict[str, Any]:
        """A released[] entry (also the /checkpoint release body) with its reason."""
        checkpoint, progress, _ = rj.runner.snapshot()
        return {"id": rj.job_id, "lease_token": rj.lease_token, "progress": progress, "checkpoint": checkpoint, "reason": reason}

    # --------------------------------------------------------------- runners

    def _start_job(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("id", ""))
        token = str(job.get("lease_token", ""))
        if not job_id or not token:
            log.error("ignoring job without id/lease_token: %r", job)
            return
        if job_id in self.running:
            log.warning("job %s already running; replacing lease token", job_id)
            self.running[job_id].lease_token = token
            return
        if self._rekey_finished(job_id, token):
            return
        lease = job.get("lease_seconds")
        if isinstance(lease, (int, float)) and lease > 0:
            self.lease_seconds = float(lease)
        if context.needs_context(job.get("kind")):
            try:
                job = dict(job, context=self._build_context(job))
            except context.ContextError as exc:
                log.error("cannot build the context for job %s: %s", job_id, exc)
                self._queue_post("fail", job_id, token, {"error": f"context unavailable: {exc}"}, 0.0)
                return
        runner = Runner(job, python=self.options.python)
        try:
            runner.start()
        except OSError as exc:
            log.error("cannot start runner for %s: %s", job_id, exc)
            self._queue_post("fail", job_id, token, {"error": f"runner start failed: {exc}"}, 0.0)
            return
        self.running[job_id] = RunningJob(job=job, lease_token=token, runner=runner)
        log.info("started runner for %s job %s (pid %s, resume=%s)", job.get("kind"), job_id, runner.pid, job.get("checkpoint") is not None)

    def _build_context(self, job: dict[str, Any]) -> dict[str, Any]:
        """Games cache path and model for a batch job (fleet.worker.context)."""
        assert self.conf is not None
        return context.build_context(
            self.conf["host_url"], self.conf["worker_token"], self.state_dir, job,
            timeout=self.options.http_timeout, data_timeout=max(self.options.http_timeout, self.options.data_timeout),
        )

    def _rekey_finished(self, job_id: str, token: str) -> bool:
        """A job handed back that we already finished or released: carry the new lease
        token on the unsent post or release instead of running it again."""
        hit = False
        for post in self.pending_posts:
            if post.job_id == job_id:
                post.body["lease_token"] = token
                hit = True
        for entry in self.pending_releases:
            if entry.get("id") == job_id:
                entry["lease_token"] = token
                hit = True
        if hit:
            log.info("job %s handed back while its result is unsent; re-keyed, not restarted", job_id)
            self._save_pending_posts()
        return hit

    def _forget_lost(self, job_id: str) -> None:
        rj = self.running.pop(job_id, None)
        if rj is None:
            return
        log.warning("job %s lost (lease gone): killing runner", job_id)
        rj.runner.kill()
        rj.runner.wait(5.0)
        rj.runner.reap_group()

    def _kill_all_runners(self) -> None:
        for job_id in list(self.running):
            self._forget_lost(job_id)

    def service_runners(self) -> int:
        """Collect finished runners and queue their /complete or /fail call, then
        let the memory watchdog stop runners that outgrew the machine.
        Returns how many posts were queued."""
        queued = 0
        for rj in list(self.running.values()):
            outcome = rj.runner.outcome
            if outcome is None:
                continue
            del self.running[rj.job_id]
            checkpoint, progress, _ = rj.runner.snapshot()
            if outcome == "done":
                self._queue_complete(rj)
                queued += 1
            elif outcome == "error":
                self._queue_post("fail", rj.job_id, rj.lease_token, {"error": rj.runner.error or "unknown error"}, progress)
                queued += 1
            elif outcome == "stopped":
                log.warning("runner %s stopped by an external SIGTERM; releasing", rj.job_id)
                self.pending_releases.append(self._release_entry(rj, "shutdown" if self.stopping else "stopped"))
            else:
                code = rj.runner.poll()
                self._queue_post("fail", rj.job_id, rj.lease_token, {"error": f"runner exited with code {code}"}, progress)
                queued += 1
        self._watchdog_pass()
        return queued

    def _watchdog_pass(self) -> None:
        """Stop every runner the memory watchdog flags and release its job (reason oom)
        with the last checkpoint; the release rides the next heartbeat."""
        offenders = self.watchdog.over_limit(self.running)
        if not offenders:
            return
        for rj in self._stop_runners(offenders, self.options.drain_grace):
            self.watchdog.forget(rj.job_id)
            entry = self._release_entry(rj, "oom")
            log.warning("job %s released after the memory watchdog tripped (checkpoint kept: %s)", rj.job_id, entry["checkpoint"] is not None)
            self.pending_releases.append(entry)

    def _queue_complete(self, rj: RunningJob) -> None:
        """Queue the /complete of a finished runner behind its model posts, if any."""
        post = posts.complete_post(rj.job, rj.lease_token, rj.runner.result)
        if post.steps:
            log.info("job %s -> %d model post(s), then complete", rj.job_id, len(post.steps))
        else:
            log.info("job %s -> complete", rj.job_id)
        self.pending_posts.append(post)
        self._save_pending_posts()

    def _queue_post(self, kind: str, job_id: str, lease_token: str, extra: dict[str, Any], progress: float) -> None:
        body = {"lease_token": lease_token}
        body.update(extra)
        if kind == "fail":
            error = str(extra.get("error", ""))
            log.warning("job %s failed: %s", job_id, _last_line(error))
            if "\n" in error.strip():
                log.warning("job %s traceback:\n%s", job_id, error.rstrip())
        else:
            log.info("job %s -> %s", job_id, kind)
        self.pending_posts.append(PendingPost(path=f"/api/v1/jobs/{job_id}/{kind}", body=body, job_id=job_id, progress=progress))
        self._save_pending_posts()

    def flush_posts(self, attempts: int = 1, delay: float = 0.0) -> None:
        """Deliver queued /complete and /fail calls; keep the ones the host did not answer.
        With attempts > 1, retry the survivors after delay seconds (shutdown)."""
        if not self.pending_posts:
            return
        for attempt in range(attempts):
            if attempt and delay > 0:
                time.sleep(delay)
            self._flush_posts_once()
            if not self.pending_posts:
                break
        self._save_pending_posts()

    def _flush_posts_once(self) -> None:
        assert self.conf is not None
        self.pending_posts = posts.flush_once(
            self.pending_posts, self.conf["host_url"], self.conf["worker_token"],
            self.options.http_timeout, self._save_pending_posts,
        )

    def _save_pending_posts(self) -> None:
        config.save_pending_posts(self.state_dir, [p.to_dict() for p in self.pending_posts])

    # ----------------------------------------------------------- self-update

    def _maybe_self_update(self) -> None:
        if not self.host_code_version or self.host_code_version == self.code_version:
            return
        if self.running or self.pending_posts or self.pending_releases:
            return
        now = self._clock()
        if self._update_failed_at is not None and now - self._update_failed_at < UPDATE_RETRY_SECONDS:
            return
        if self.exit_code is not None:
            return
        assert self.conf is not None
        log.info("host code %s differs from running %s: self-updating", self.host_code_version, self.code_version)
        try:
            version = update.self_update(self.conf["host_url"], config.app_dir(self.state_dir), self.code_version)
        except (update.UpdateError, OSError) as exc:
            log.error("self-update failed: %s", exc)
            self._update_failed_at = now
            self.last_error = f"self-update failed: {exc}"
            return
        if version is None:
            log.warning("/dl/version still reports %s; not updating", self.code_version)
            self._update_failed_at = now
            return
        log.info("updated to %s; exiting 75 for restart", version)
        self.exit_code = EXIT_UPDATED

    # ---------------------------------------------------------------- status

    def _write_status(self) -> None:
        config.save_status(
            self.state_dir,
            {
                "at": _utcnow_iso(),
                "state": self.state,
                "role": self.role,
                "acked_epoch": self.acked_epoch,
                "desired_role": self.desired_role,
                "role_epoch": self.role_epoch,
                "degraded": self.degraded,
                "misses": self.misses,
                "code_version": self.code_version,
                "host_code_version": self.host_code_version,
                "heartbeat_seconds": self.heartbeat_seconds,
                "running": sorted(self.running),
                "kill": self.kill,
                "watchdog_trips": self.watchdog.trips,
                "pending_posts": [p.job_id for p in self.pending_posts],
                "heartbeat_count": self.heartbeat_count,
                "last_error": self.last_error,
                "last_response": self.last_response,
            },
        )
