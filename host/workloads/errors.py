"""Workload-specific errors; they subclass host.errors.QueueError so the app maps them to HTTP."""
from __future__ import annotations

from host.errors import QueueError


class Unplaceable(QueueError):
    """The workload does not fit the machine; `refusals` lists (code, message) pairs."""

    status = 422

    def __init__(self, refusals: list[tuple[str, str]]) -> None:
        super().__init__("placement refused: " + "; ".join(f"{code}: {msg}" for code, msg in refusals))
        self.refusals = refusals

    @property
    def codes(self) -> list[str]:
        return [code for code, _ in self.refusals]


class TooMany(QueueError):
    """A per-workload cap was hit (pending outbound actions)."""

    status = 429


class SecretsUnavailable(QueueError):
    """FLEET_SECRETS_KEY is not configured on the host."""

    status = 409
