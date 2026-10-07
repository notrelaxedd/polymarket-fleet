"""Incremental log shipping: `docker logs --timestamps --since <cursor>` per container.

Per heartbeat at most 200 lines and 64 KiB go out, each line cut at 2048 characters; the
rest waits for the next heartbeat. Every known secret value (the container's secret
files and its run token) is replaced by "[redacted]" before a line is cut or shipped.
A cursor only moves when the heartbeat that carried the lines was acknowledged
(Batch.commit), so a failed heartbeat never loses logs. Lines of a container that is
about to be removed are drained into a carry buffer first.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from fleetagent import config
from fleetagent.docker import Docker, DockerError

log = logging.getLogger("fleetagent.logship")

MAX_LINES = 200
MAX_BYTES = 64 * 1024
MAX_LINE_CHARS = 2048
MAX_CARRY = 2000
INITIAL_TAIL = 1000
REDACTED = "[redacted]"
Cursor = tuple[str, int]  # (normalized timestamp of the last shipped line, lines shipped at that timestamp)


def redact(text: str, values: list[str]) -> str:
    """Replace every secret value (and each line of a multi-line value) with [redacted]."""
    needles: set[str] = set()
    for value in values:
        if not value:
            continue
        needles.add(value)
        if "\n" in value:
            needles.update(part for part in value.splitlines() if len(part.strip()) >= 6)
    for needle in sorted(needles, key=len, reverse=True):
        if needle in text:
            text = text.replace(needle, REDACTED)
    return text


def _iso(ts: str) -> str:
    """Docker's 9-digit timestamp trimmed to microseconds (what Postgres and fromisoformat read)."""
    return ts[:26] + "Z" if len(ts) >= 27 else ts


@dataclass
class Batch:
    """Lines picked for one heartbeat; commit() after the host acknowledged them."""

    entries: list[dict[str, str]] = field(default_factory=list)
    _cursors: dict[str, Cursor] = field(default_factory=dict)
    _carry_used: int = 0
    _owner: "LogShipper | None" = None

    def commit(self) -> None:
        if self._owner is not None:
            self._owner._commit(self)


class LogShipper:
    def __init__(self, docker: Docker, state_dir: str | None = None, max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> None:
        self.docker = docker
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self._path = os.path.join(state_dir, "logcursors.json") if state_dir else None
        self.cursors: dict[str, Cursor] = self._load()
        self.secrets: dict[str, list[str]] = {}
        self.carry: list[tuple[str, dict[str, str]]] = []  # (workload, entry), oldest first

    # ---------------------------------------------------------------- secrets

    def set_secrets(self, workload: str, values: list[str]) -> None:
        """The values to redact for a workload (secret file contents plus its run token)."""
        self.secrets[workload] = [v for v in values if v]

    def known_values(self) -> list[str]:
        return [v for vals in self.secrets.values() for v in vals]

    def forget(self, workload: str) -> None:
        """Drop a workload's values once its container is gone and its carried lines are shipped."""
        if not any(w == workload for w, _ in self.carry):
            self.secrets.pop(workload, None)

    # ---------------------------------------------------------------- cursors

    def _load(self) -> dict[str, Cursor]:
        if not self._path:
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return {}
        out: dict[str, Cursor] = {}
        if isinstance(raw, dict):
            for cid, cur in raw.items():
                if isinstance(cur, list) and len(cur) == 2 and isinstance(cur[0], str) and isinstance(cur[1], int):
                    out[str(cid)] = (cur[0], cur[1])
        return out

    def _save(self) -> None:
        if self._path:
            try:
                config.write_json_atomic(self._path, {k: list(v) for k, v in self.cursors.items()}, 0o600)
            except OSError:
                pass

    def prune(self, live_ids: list[str]) -> None:
        """Forget cursors of containers that no longer exist."""
        stale = [cid for cid in self.cursors if cid not in live_ids]
        for cid in stale:
            del self.cursors[cid]
        if stale:
            self._save()

    # ------------------------------------------------------------- collecting

    def _fresh(self, container_id: str, workload: str, cursor: Cursor | None) -> list[tuple[str, dict[str, str]]]:
        """Lines after `cursor`, redacted and cut: [(normalized ts, entry)]."""
        try:
            # no cursor: a new container (a few lines) or one adopted without a state file (maybe
            # megabytes); the last INITIAL_TAIL lines are enough either way
            rows = self.docker.logs(container_id, since=cursor[0] if cursor else None, tail=None if cursor else INITIAL_TAIL)
        except DockerError as exc:
            log.debug("logs for %s unavailable: %s", container_id[:12], exc)
            return []
        values = self.secrets.get(workload, []) + [v for w, vals in self.secrets.items() if w != workload for v in vals]
        skip = cursor[1] if cursor else 0
        out: list[tuple[str, dict[str, str]]] = []
        for ts, stream, text in rows:
            if cursor and ts < cursor[0]:
                continue
            if cursor and ts == cursor[0] and skip > 0:
                skip -= 1
                continue
            line = redact(text, values)[:MAX_LINE_CHARS]
            out.append((ts, {"ts": _iso(ts), "stream": stream, "line": line}))
        return out

    @staticmethod
    def _size(entry: dict[str, str]) -> int:
        return len(json.dumps(entry))

    def collect(self, containers: list[tuple[str, str]]) -> Batch:
        """The next batch: carried lines first, then each (container_id, workload) in order."""
        batch = Batch(_owner=self)
        used = 0

        def room(entry: dict[str, str]) -> bool:
            return len(batch.entries) < self.max_lines and used + self._size(entry) <= self.max_bytes

        for _, entry in self.carry:
            if not room(entry):
                break
            batch.entries.append(entry)
            used += self._size(entry)
            batch._carry_used += 1
        for cid, workload in containers:
            cursor = self.cursors.get(cid)
            taken: list[tuple[str, dict[str, str]]] = []
            for ts, entry in self._fresh(cid, workload, cursor):
                if not room(entry):
                    break
                batch.entries.append(entry)
                used += self._size(entry)
                taken.append((ts, entry))
            if taken:
                last = taken[-1][0]
                n = sum(1 for ts, _ in taken if ts == last)
                if cursor and cursor[0] == last:
                    n += cursor[1]
                batch._cursors[cid] = (last, n)
        return batch

    def _commit(self, batch: Batch) -> None:
        del self.carry[: batch._carry_used]
        self.cursors.update(batch._cursors)
        if batch._cursors:
            self._save()

    def drain(self, container_id: str, workload: str) -> None:
        """Move the container's unshipped lines into the carry buffer (call before removing it)."""
        cursor = self.cursors.get(container_id)
        fresh = self._fresh(container_id, workload, cursor)
        for _, entry in fresh:
            self.carry.append((workload, entry))
        del self.carry[: max(0, len(self.carry) - MAX_CARRY)]
        self.cursors.pop(container_id, None)
        self._save()

    def add_agent_line(self, line: str, ts_iso: str) -> None:
        """Queue a line written by the agent itself (stream "agent"), e.g. why a start failed."""
        text = redact(line, self.known_values())[:MAX_LINE_CHARS]
        self.carry.append(("", {"ts": ts_iso, "stream": "agent", "line": text}))
        del self.carry[: max(0, len(self.carry) - MAX_CARRY)]
