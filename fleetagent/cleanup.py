"""Disk hygiene for machines that boot from small flash drives (design section 6).

After every start and hourly: remove images of fleet repositories (a repository under
`fleet/` or containing "/fleet/") whose digest is not in keep_images, `docker image
prune -f`, `docker builder prune -af`, and remove exited containers labelled
fleet.workload (except the desired one, whose exit code the supervisor still needs).
Low-disk guard: free space below max(1024 MB, 10% of the disk) prunes first; when
still low the pull is refused and the heartbeat reports low_disk.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Collection, Sequence

from fleetagent.docker import Docker, DockerError

log = logging.getLogger("fleetagent.cleanup")

LOW_DISK_MIN_MB = 1024
LOW_DISK_FRACTION = 0.10
INTERVAL_SECONDS = 3600.0


def is_fleet_repo(repository: str) -> bool:
    return repository.startswith("fleet/") or "/fleet/" in repository


def low_disk_threshold_mb(disk_size_mb: int | None) -> int:
    """max(1024 MB, 10% of the disk)."""
    return max(LOW_DISK_MIN_MB, int((disk_size_mb or 0) * LOW_DISK_FRACTION))


def is_low(free_mb: int | None, disk_size_mb: int | None) -> bool:
    return free_mb is not None and free_mb < low_disk_threshold_mb(disk_size_mb)


@dataclass
class Report:
    images_removed: int = 0
    bytes_freed: int = 0
    low_disk: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        return {"images_removed": self.images_removed, "bytes_freed": self.bytes_freed, "low_disk": self.low_disk}


class Cleaner:
    def __init__(
        self,
        docker: Docker,
        clock: Callable[[], float] = time.monotonic,
        interval: float = INTERVAL_SECONDS,
        prune_images: bool = True,
        prune_builder: bool = True,
        labels: Sequence[str] = ("fleet.workload",),
        repo_filter: Callable[[str], bool] | None = None,
    ) -> None:
        self.docker = docker
        self.labels = list(labels)
        self.repo_filter = repo_filter or is_fleet_repo
        self._clock = clock
        self.interval = interval
        self.prune_images = prune_images
        self.prune_builder = prune_builder
        self.report = Report()
        self._last_run: float | None = None

    def due(self) -> bool:
        return self._last_run is None or self._clock() - self._last_run >= self.interval

    def run(
        self,
        keep_images: Collection[str] | None,
        protect: Collection[str] = (),
        desired: tuple[str, int] | None = None,
    ) -> None:
        """One cleanup pass. keep_images None (the host did not say) removes no image.
        `protect` are extra digests or image ids never removed (images of live containers).
        `desired` is the (workload, epoch) whose container is kept even when exited."""
        self._last_run = self._clock()
        if keep_images is not None:
            self._remove_old_images(set(keep_images) | set(protect))
        for step, enabled in ((self.docker.image_prune, self.prune_images), (self.docker.builder_prune, self.prune_builder)):
            if not enabled:
                continue
            try:
                self.report.bytes_freed += step()
            except DockerError as exc:
                log.warning("prune failed: %s", exc)
        self._remove_exited(desired)

    def _remove_old_images(self, keep: set[str]) -> None:
        try:
            images = self.docker.images()
        except DockerError as exc:
            log.warning("cannot list images: %s", exc)
            return
        remaining: dict[str, int] = {}
        for img in images:
            remaining[img.id] = remaining.get(img.id, 0) + 1
        removed_ids: set[str] = set()
        for img in images:
            ref = img.ref
            if ref is None or not self.repo_filter(img.repository) or img.digest in keep or img.id in keep:
                continue
            try:
                self.docker.rmi(ref)
            except DockerError as exc:
                log.debug("not removing %s: %s", ref, exc)
                continue
            remaining[img.id] -= 1
            if remaining[img.id] <= 0 and img.id not in removed_ids:
                removed_ids.add(img.id)
                self.report.images_removed += 1
                self.report.bytes_freed += img.size_bytes
                log.info("removed old image %s", ref)

    def _remove_exited(self, desired: tuple[str, int] | None) -> None:
        try:
            containers = self.docker.ps(self.labels)
        except DockerError as exc:
            log.warning("cannot list containers: %s", exc)
            return
        for c in containers:
            if c.running or (c.workload, c.epoch) == desired:
                continue
            try:
                self.docker.rm(c.id)
            except DockerError as exc:
                log.warning("cannot remove exited container %s: %s", c.name, exc)

    def guard(
        self,
        measure: Callable[[], tuple[int | None, int | None]],
        keep_images: Collection[str] | None,
        protect: Collection[str] = (),
        desired: tuple[str, int] | None = None,
    ) -> bool:
        """True when a pull may go ahead. `measure` returns (free_mb, disk_size_mb). When free
        space is low, prune first and measure again; still low means refuse and report low_disk."""
        free, size = measure()
        if not is_low(free, size):
            self.report.low_disk = False
            return True
        log.warning("low disk (%s MB free of %s MB): pruning before the pull", free, size)
        self.run(keep_images, protect, desired)
        free, size = measure()
        self.report.low_disk = is_low(free, size)
        if self.report.low_disk:
            log.error("still low on disk after pruning (%s MB free): refusing to pull", free)
        return not self.report.low_disk

    def snapshot(self) -> Report:
        return Report(self.report.images_removed, self.report.bytes_freed, self.report.low_disk)

    def acknowledge(self, sent: Report) -> None:
        """The host got `sent`: subtract it, keeping anything counted since."""
        self.report.images_removed -= sent.images_removed
        self.report.bytes_freed -= sent.bytes_freed
