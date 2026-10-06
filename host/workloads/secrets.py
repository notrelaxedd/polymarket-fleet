"""Workload secrets, encrypted in Postgres with a NaCl SecretBox under FLEET_SECRETS_KEY.

Values are write-only: no owner route returns one, no audit row or log line contains
one. Only `secrets_for_machine` (container scope, current epoch) and `host_only_secrets`
(host senders) decrypt. The plaintext is bound to its (workload, name) so a ciphertext
swapped between rows fails to decrypt.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from typing import Any

import psycopg

from host.errors import BadRequest, Conflict, NotFound
from host.events import add_audit
from host.workloads import config
from host.workloads.errors import SecretsUnavailable
from host.workloads.registry import get_workload, manifest_of

MAX_VALUE_BYTES = 16 * 1024

log = logging.getLogger(__name__)


def secret_box():
    """A SecretBox from env FLEET_SECRETS_KEY (base64 of 32 bytes); None when it is unset."""
    raw = config.secrets_key()
    if not raw:
        return None
    from nacl.secret import SecretBox

    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise SecretsUnavailable("FLEET_SECRETS_KEY is not valid base64") from None
    if len(key) != SecretBox.KEY_SIZE:
        raise SecretsUnavailable("FLEET_SECRETS_KEY must decode to 32 bytes")
    return SecretBox(key)


def _need_box():
    box = secret_box()
    if box is None:
        raise SecretsUnavailable("FLEET_SECRETS_KEY is not configured on the host (put it in secrets.env)")
    return box


def _bind(workload: str, name: str) -> bytes:
    return f"{workload}/{name}\n".encode()


def _decrypt(box, workload: str, row: dict[str, Any]) -> str:
    from nacl.exceptions import CryptoError

    try:
        plain = box.decrypt(bytes(row["ciphertext"]), bytes(row["nonce"]))
    except CryptoError:
        raise SecretsUnavailable(f"secret {row['name']} cannot be decrypted (was FLEET_SECRETS_KEY changed?)") from None
    prefix = _bind(workload, row["name"])
    if not plain.startswith(prefix):
        raise SecretsUnavailable(f"secret {row['name']} does not belong to {workload}")
    return plain[len(prefix):].decode("utf-8")


def set_secret(
    conn: psycopg.Connection, workload: str, name: str, value: str, actor: str | None, ip: str | None
) -> dict[str, Any]:
    """Store (or replace) one secret; returns {name, scope, updated_at} (never the value)."""
    manifest = manifest_of(get_workload(conn, workload))
    if name in manifest.container_secrets:
        scope = "container"
    elif name in manifest.host_only_secrets:
        scope = "host_only"
    else:
        raise BadRequest(f"workload {workload!r} does not declare a secret named {name!r}")
    if not isinstance(value, str) or value == "":
        raise BadRequest("value must be a non-empty string")
    data = value.encode("utf-8")
    if len(data) > MAX_VALUE_BYTES:
        raise BadRequest(f"value larger than {MAX_VALUE_BYTES} bytes")
    box = _need_box()
    sealed = box.encrypt(_bind(workload, name) + data)
    row = conn.execute(
        """
        INSERT INTO workload_secrets (workload, name, scope, nonce, ciphertext, updated_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (workload, name) DO UPDATE SET scope = EXCLUDED.scope, nonce = EXCLUDED.nonce,
          ciphertext = EXCLUDED.ciphertext, updated_by = EXCLUDED.updated_by, updated_at = now()
        RETURNING name, scope, updated_at
        """,
        (workload, name, scope, bytes(sealed.nonce), bytes(sealed.ciphertext), actor),
    ).fetchone()
    add_audit(conn, "secret_set", f"{workload}/{name}", actor, None, {"scope": scope}, ip)
    return row


def delete_secret(conn: psycopg.Connection, workload: str, name: str, actor: str | None, ip: str | None) -> None:
    """Remove one stored secret; 404 when it is not set. Audited (no value)."""
    get_workload(conn, workload)
    row = conn.execute(
        "DELETE FROM workload_secrets WHERE workload = %s AND name = %s RETURNING scope", (workload, name)
    ).fetchone()
    if row is None:
        raise NotFound(f"secret {name!r} is not set for {workload!r}")
    add_audit(conn, "secret_delete", f"{workload}/{name}", actor, {"scope": row["scope"]}, None, ip)


def list_secret_names(conn: psycopg.Connection, workload: str) -> list[dict[str, Any]]:
    """Declared and stored secret names: [{name, scope, declared, set, updated_at}]."""
    manifest = manifest_of(get_workload(conn, workload))
    stored = {
        r["name"]: r
        for r in conn.execute("SELECT name, scope, updated_at FROM workload_secrets WHERE workload = %s", (workload,))
    }
    out: list[dict[str, Any]] = []
    declared = [(n, "container") for n in manifest.container_secrets] + [(n, "host_only") for n in manifest.host_only_secrets]
    for name, scope in declared:
        row = stored.pop(name, None)
        out.append({"name": name, "scope": scope, "declared": True, "set": row is not None,
                    "updated_at": row["updated_at"] if row else None})
    for name, row in sorted(stored.items()):
        out.append({"name": name, "scope": row["scope"], "declared": False, "set": True, "updated_at": row["updated_at"]})
    return out


def secrets_for_machine(conn: psycopg.Connection, machine_id: str, epoch: int) -> dict[str, str]:
    """Container-scoped secrets of the workload assigned to the machine at `epoch`.

    409 when the epoch is stale or nothing is assigned. Host-only secrets are never
    included. No key configured: SecretsUnavailable when the workload declares any.
    """
    a = conn.execute(
        "SELECT workload, epoch FROM workload_assignments WHERE machine_id = %s", (machine_id,)
    ).fetchone()
    if a is None or a["workload"] is None or a["epoch"] != epoch:
        raise Conflict("epoch is not current or nothing is assigned")
    workload = a["workload"]
    manifest = manifest_of(get_workload(conn, workload))
    declared = manifest.container_secrets
    if not declared:
        return {}
    rows = conn.execute(
        "SELECT name, nonce, ciphertext FROM workload_secrets"
        " WHERE workload = %s AND scope = 'container' AND name = ANY(%s)",
        (workload, list(declared)),
    ).fetchall()
    if not rows:
        return {}  # nothing stored: no key needed (a polymarket restart must never wait on it)
    if manifest.protocol == "fleet-worker":
        # The polymarket worker needs its enroll token only for a first start (no worker.conf
        # yet); a lost or changed FLEET_SECRETS_KEY must not keep a trading container down.
        try:
            box = secret_box()
        except SecretsUnavailable:
            box = None
        out: dict[str, str] = {}
        for r in rows:
            try:
                if box is None:
                    raise SecretsUnavailable("FLEET_SECRETS_KEY is not configured")
                out[r["name"]] = _decrypt(box, workload, r)
            except SecretsUnavailable as exc:
                log.warning("start of %s on %s without secret %s: %s", workload, machine_id, r["name"], exc.message)
        return out
    box = _need_box()
    return {r["name"]: _decrypt(box, workload, r) for r in rows}


def host_only_secrets(conn: psycopg.Connection, workload: str) -> dict[str, str]:
    """Host-only secrets of a workload, for the host's outbound senders only."""
    rows = conn.execute(
        "SELECT name, nonce, ciphertext FROM workload_secrets WHERE workload = %s AND scope = 'host_only'",
        (workload,),
    ).fetchall()
    if not rows:
        return {}
    box = _need_box()
    return {r["name"]: _decrypt(box, workload, r) for r in rows}


def secrets_version(conn: psycopg.Connection, workload: str) -> str:
    """A short digest that changes whenever a container secret of the workload is set or deleted."""
    rows = conn.execute(
        "SELECT name, updated_at FROM workload_secrets WHERE workload = %s AND scope = 'container' ORDER BY name",
        (workload,),
    ).fetchall()
    digest = hashlib.sha256()
    for r in rows:
        digest.update(f"{r['name']}={r['updated_at'].isoformat()}\n".encode())
    return digest.hexdigest()[:16]


def all_secret_values(conn: psycopg.Connection, workload: str) -> list[str]:
    """Every stored secret value of a workload (both scopes), for scrubbing text the host
    stores; empty when the key is missing or a value cannot be decrypted (best effort)."""
    try:
        box = secret_box()
    except SecretsUnavailable:
        return []
    if box is None:
        return []
    rows = conn.execute("SELECT name, nonce, ciphertext FROM workload_secrets WHERE workload = %s", (workload,)).fetchall()
    values: list[str] = []
    for r in rows:
        try:
            values.append(_decrypt(box, workload, r))
        except SecretsUnavailable:
            continue
    return [v for v in values if len(v) >= 4]


def scrub(text: str, values: list[str]) -> str:
    """Replace every value in text with [redacted], longest first."""
    for v in sorted(values, key=len, reverse=True):
        text = text.replace(v, "[redacted]")
    return text
