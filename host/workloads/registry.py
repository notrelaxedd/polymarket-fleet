"""The workloads table: manifests synced from disk, image digests recorded at publish."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host.errors import BadRequest, NotFound
from host.events import add_audit
from host.workloads import config
from host.workloads.manifest import Manifest, ManifestError, discover, load_manifest

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def sync_from_dir(conn: psycopg.Connection, root: Path) -> dict[str, Any]:
    """Upsert every valid manifest under `root`; never deletes a workload.

    A changed image repository clears the recorded digest and size (they belonged to
    the old repository). Returns {"synced": [names], "errors": {folder: [problems]}}.
    """
    synced: list[str] = []
    errors: dict[str, list[str]] = {}
    for folder in discover(root):
        try:
            manifest = load_manifest(folder)
        except ManifestError as exc:
            errors[folder.name] = exc.problems
            continue
        frozen = _frozen_change(conn, manifest)
        if frozen:
            errors[folder.name] = [frozen]
            continue
        conn.execute(
            """
            INSERT INTO workloads (name, manifest, image_repo) VALUES (%(n)s, %(m)s, %(r)s)
            ON CONFLICT (name) DO UPDATE SET manifest = EXCLUDED.manifest,
              image_digest = CASE WHEN workloads.image_repo = EXCLUDED.image_repo
                                  THEN workloads.image_digest END,
              image_size_mb = CASE WHEN workloads.image_repo = EXCLUDED.image_repo
                                   THEN workloads.image_size_mb END,
              image_repo = EXCLUDED.image_repo, synced_at = now(), updated_at = now()
            """,
            {"n": manifest.name, "m": Jsonb(manifest.to_json()), "r": manifest.image},
        )
        synced.append(manifest.name)
    return {"synced": synced, "errors": errors}


def _run_shape(data: dict[str, Any]) -> dict[str, Any]:
    """The manifest fields that shape a running container (the heartbeat run block)."""
    res = data.get("resources") or {}
    return {
        "image": data.get("image"), "protocol": data.get("protocol"), "runtime": data.get("runtime"),
        "memory": (res.get("memory_max_mb"), res.get("memory_max_pct"), res.get("cpus")),
    }


def _frozen_change(conn: psycopg.Connection, manifest: Manifest) -> str | None:
    """A problem when the sync would change how an assigned workload's containers run.

    Such a change would reach running machines on their next restart (or, for the image
    repository, stop them at once), pinned live traders included, so it waits until the
    workload is assigned nowhere."""
    row = conn.execute("SELECT manifest FROM workloads WHERE name = %s", (manifest.name,)).fetchone()
    if row is None or _run_shape(row["manifest"]) == _run_shape(manifest.to_json()):
        return None
    n = conn.execute("SELECT count(*) AS n FROM workload_assignments WHERE workload = %s OR draining_to = %s",
                     (manifest.name, manifest.name)).fetchone()["n"]
    if not n:
        return None
    return (f"not synced: the image, protocol, runtime or memory/cpu limits changed while {manifest.name} "
            f"is assigned to {n} machine(s); assign those machines to none first")


def get_workload(conn: psycopg.Connection, name: str, for_update: bool = False) -> dict[str, Any]:
    """One workloads row; 404 when unknown."""
    sql = "SELECT * FROM workloads WHERE name = %s" + (" FOR UPDATE" if for_update else "")
    row = conn.execute(sql, (name,)).fetchone()
    if row is None:
        raise NotFound(f"unknown workload: {name!r}")
    return row


def manifest_of(row: dict[str, Any]) -> Manifest:
    """The parsed manifest stored on a workloads row."""
    return Manifest.from_json(row["manifest"])


def set_image(conn: psycopg.Connection, name: str, digest: str, size_mb: int | None = None) -> dict[str, Any]:
    """Record the published image digest (and size) of a workload."""
    if not DIGEST_RE.match(digest or ""):
        raise BadRequest("digest must look like sha256:<64 hex>")
    if size_mb is not None and (isinstance(size_mb, bool) or not isinstance(size_mb, int) or size_mb < 0):
        raise BadRequest("size_mb must be a non-negative integer")
    get_workload(conn, name)
    return conn.execute(
        "UPDATE workloads SET image_digest = %s, image_size_mb = %s, updated_at = now() WHERE name = %s RETURNING *",
        (digest, size_mb, name),
    ).fetchone()


def set_enabled(conn: psycopg.Connection, name: str, enabled: bool, actor: str | None, ip: str | None) -> dict[str, Any]:
    """Enable or disable a workload (a disabled one cannot be assigned); audited."""
    before = get_workload(conn, name, for_update=True)
    row = conn.execute(
        "UPDATE workloads SET enabled = %s, updated_at = now() WHERE name = %s RETURNING *", (bool(enabled), name)
    ).fetchone()
    add_audit(conn, "workload_enabled", name, actor, {"enabled": before["enabled"]}, {"enabled": row["enabled"]}, ip)
    return row


def image_ref(workload_row: dict[str, Any]) -> str | None:
    """`<registry>/<repo>@<digest>`, or None while no image is published."""
    if not workload_row.get("image_digest"):
        return None
    return f"{config.registry()}/{workload_row['image_repo']}@{workload_row['image_digest']}"
