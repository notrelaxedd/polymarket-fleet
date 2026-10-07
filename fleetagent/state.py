"""Plain records shared by the reconciler: what the host wants and what the agent reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

RESTART_DELAY = 3.0
START_RETRY_SECONDS = 5.0
PULL_RETRY_SECONDS = 30.0
STATS_INTERVAL = 15.0
DEFAULT_STOP_TIMEOUT = 15
StartFn = Callable[[int], tuple[str, dict[str, str]]]


@dataclass
class Desired:
    """What the heartbeat answer asks for. `run` is None when nothing should run."""

    epoch: int | None
    workload: str | None
    run: dict[str, Any] | None
    keep_images: list[str] | None

    @property
    def want(self) -> tuple[str, int] | None:
        if self.workload and self.run and self.epoch is not None:
            return (self.workload, int(self.epoch))
        return None


@dataclass
class Managed:
    """The agent's record of the desired container, reported as the heartbeat `container` block."""

    workload: str
    epoch: int
    state: str = "starting"  # starting | running | exited | failed
    container_id: str | None = None
    image_digest: str | None = None
    exit_code: int | None = None
    restarts: int = 0
    started_at: str | None = None
    error: str | None = None
    cpu_pct: float | None = None
    mem_mb: int | None = None
    restart_at: float | None = None
    stats_at: float = float("-inf")

    def block(self) -> dict[str, Any]:
        return {
            "workload": self.workload, "epoch": self.epoch, "state": self.state, "container_id": self.container_id,
            "image_digest": self.image_digest, "exit_code": self.exit_code, "restarts": self.restarts,
            "cpu_pct": self.cpu_pct, "mem_mb": self.mem_mb, "started_at": self.started_at, "error": self.error,
        }
