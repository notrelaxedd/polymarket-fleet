"""View models for the machines, workloads and approvals pages (docs/workloads-design.md
section 10). Read-only SQL on the 0010 tables plus the display text ("flash 16 GB",
"4 GB RAM", the online dot, the placement refusals per machine and workload). Secret
values are never selected: the secrets list reads names, scope and update times only.
"""
from __future__ import annotations

from datetime import tzinfo
from typing import Any

import psycopg

from host import web
from host.scheduling import online_after
from host.workloads.manifest import Manifest

STALE_AFTER_SECONDS = 60
LOG_LINES_MACHINE = 200
LOG_LINES_WORKLOAD = 50
DONE_LIMIT = 50
DISK_TYPES = ("ssd", "hdd", "flash", "unknown")
RUNNING_JOBS = ("queued", "leased", "cancel_requested")
DONE_JOBS = ("succeeded", "failed", "cancelled")

MACHINE_SQL = """
SELECT m.id, m.name, m.hostname, m.remote_ip, m.agent_version, m.docker_version, m.docker_ok, m.arch,
       m.cpu_count, m.cpu_pct, m.ram_total_mb, m.ram_used_mb, m.disk_type_detected, m.disk_type_override,
       m.disk_size_mb, m.disk_free_mb, m.low_disk, m.native_polymarket, m.pinned, m.pinned_reason,
       m.enabled, m.last_heartbeat_at, w.name AS worker_name,
       EXTRACT(EPOCH FROM (now() - m.last_heartbeat_at))::bigint AS age,
       a.workload AS a_workload, a.state AS a_state, a.epoch AS a_epoch, a.draining_to AS a_draining_to,
       a.draining_to_set AS a_draining_to_set, a.restarts AS a_restarts, a.last_error AS a_error,
       a.cpu_pct AS a_cpu_pct, a.mem_mb AS a_mem_mb, a.run_manifest AS a_run_manifest,
       a.run_image_digest AS a_run_image_digest
  FROM machines m
  LEFT JOIN workload_assignments a ON a.machine_id = m.id
  LEFT JOIN workers w ON w.id = m.polymarket_worker_id
 ORDER BY m.name, m.id
"""


# ---------------------------------------------------------------- display text


def size_label(mb: int | None) -> str:
    """Megabytes as "512 MB" or whole gigabytes ("4 GB"); "?" when unknown."""
    if mb is None:
        return "?"
    return f"{int(mb)} MB" if mb < 1024 else f"{int(mb / 1024 + 0.5)} GB"


def effective_disk_type(machine: dict[str, Any]) -> str:
    """The override when set, else the detected type, else "unknown"."""
    return machine.get("disk_type_override") or machine.get("disk_type_detected") or "unknown"


def disk_label(machine: dict[str, Any]) -> str:
    """"flash 16 GB", or "flash disk" when the size is not reported."""
    kind = effective_disk_type(machine)
    size = machine.get("disk_size_mb")
    return f"{kind} {size_label(size)}" if size else f"{kind} disk"


def ram_label(machine: dict[str, Any]) -> str:
    """"4 GB RAM"."""
    total = machine.get("ram_total_mb")
    return f"{size_label(total)} RAM" if total else "RAM unknown"


def online_dot(age: int | None, threshold: int) -> str:
    """Status dot: online below online_after_seconds, stale below 60 s, offline otherwise."""
    if age is None:
        return "offline"
    if age < threshold:
        return "online"
    return "stale" if age < max(STALE_AFTER_SECONDS, threshold) else "offline"


def state_chip(workload: str | None, state: str | None) -> tuple[str, str]:
    """(chip tone, word) for what runs on a machine; the word is always there."""
    if not workload:
        return "muted", "idle"
    return {
        "running": ("ok", "running"), "starting": ("warn", "starting"), "pending": ("warn", "pending"),
        "draining": ("warn", "draining"), "failed": ("bad", "failed"),
    }.get(state or "", ("muted", state or "stopped"))


def short_text(value: Any, limit: int = 120) -> str:
    """Any payload value as one trimmed line."""
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _placement_check() -> Any:
    """check_placement, imported lazily. The ImportError fallback (no refusals shown) only
    exists until the host modules land; after the merge the import always succeeds."""
    try:
        from host.workloads.placement import check_placement
    except ImportError:
        return None
    return check_placement


# ---------------------------------------------------------------- machines


def _workload_models(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT name, manifest, image_digest, image_size_mb, enabled FROM workloads ORDER BY name"
    ).fetchall()
    for row in rows:
        try:
            row["parsed"] = Manifest.from_json(row["manifest"])
        except (KeyError, TypeError, ValueError):
            row["parsed"] = None
    return rows


def update_available(assignment: dict[str, Any], workload_row: dict[str, Any]) -> bool:
    """The machine runs an older manifest or image than the synced one (host.workloads.updates)."""
    try:
        from host.workloads.updates import is_outdated
    except ImportError:  # only before the host modules land
        return False
    return is_outdated(assignment, workload_row)


def placement_options(machine: dict[str, Any], workloads: list[dict[str, Any]], selected: str | None) -> list[dict[str, Any]]:
    """The assign select: "none" plus each workload; a refused one is disabled with the
    reason in its label ("demo-site (needs 8192 MB RAM, machine has 3800 MB)"). The one
    already selected stays enabled (assigning it again is a no-op)."""
    check = _placement_check()
    options = [{"value": "none", "label": "none", "selected": selected is None, "disabled": False, "title": ""}]
    for wl in workloads:
        refusals: list[Any] = []
        if check is not None and wl["parsed"] is not None:
            refusals = check(
                wl["parsed"], machine, image_size_mb=wl["image_size_mb"],
                image_published=bool(wl["image_digest"]), workload_enabled=wl["enabled"],
            )
        is_selected = wl["name"] == selected
        blocked = bool(refusals) and not is_selected
        reasons = "; ".join(r.message for r in refusals)
        label = f"{wl['name']} ({refusals[0].message})" if blocked else wl["name"]
        options.append({"value": wl["name"], "label": label, "selected": is_selected, "disabled": blocked, "title": reasons if blocked else ""})
    return options


def _machine_card(row: dict[str, Any], threshold: int, workloads: list[dict[str, Any]]) -> dict[str, Any]:
    dot = online_dot(row["age"], threshold)
    workload = row["a_workload"]
    selected = (row["a_draining_to"] if row["a_draining_to_set"] else workload) if row["a_state"] == "draining" else workload
    tone, word = state_chip(workload, row["a_state"])
    native = row["native_polymarket"] == "active"
    pinned_word = f"pinned: {short_text(row['pinned_reason'], 40)}" if row["pinned_reason"] else "pinned"
    free = row["disk_free_mb"]
    current = next((w for w in workloads if w["name"] == workload), None)
    update = bool(current) and row["a_state"] != "draining" and update_available(
        {"workload": workload, "run_manifest": row["a_run_manifest"], "run_image_digest": row["a_run_image_digest"]}, current)
    return {
        "update_available": update,
        **row, "dot": dot, "online": dot == "online", "disk_label": disk_label(row), "ram_label": ram_label(row),
        "disk_type": effective_disk_type(row), "disk_total_label": size_label(row["disk_size_mb"]), "state_tone": tone, "state_word": word, "workload": workload,
        "native_active": native, "pinned_word": pinned_word, "locked": row["pinned"] or native,
        "lock_reason": "pinned: unpin it first" if row["pinned"] else ("native fleet-worker is running" if native else ""),
        "free_label": size_label(free) if free is not None else "?", "options": placement_options(row, workloads, selected),
        "meta": (f"{dot} {web.ago(row['age'])}" if row["age"] is not None else "never seen") + (f" · {workload}" if workload else ""),
    }


def machine_cards(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """One dict per machine for /machines: display text, assignment, assign-select options."""
    threshold = online_after(conn)
    workloads = _workload_models(conn)
    return [_machine_card(row, threshold, workloads) for row in conn.execute(MACHINE_SQL).fetchall()]


def machine_card(conn: psycopg.Connection, machine_id: str) -> dict[str, Any] | None:
    return next((m for m in machine_cards(conn) if m["id"] == machine_id), None)


def pending_approvals(conn: psycopg.Connection) -> int:
    return conn.execute("SELECT count(*) AS n FROM outbound_actions WHERE status = 'pending'").fetchone()["n"]


def machine_stats(cards: list[dict[str, Any]], pending: int) -> dict[str, int]:
    return {
        "online": sum(1 for c in cards if c["online"]), "total": len(cards),
        "running": sum(1 for c in cards if c["workload"] and c["a_state"] == "running"),
        "pinned": sum(1 for c in cards if c["pinned"]), "approvals": pending,
    }


def log_lines(conn: psycopg.Connection, machine_id: str, tz: tzinfo, workload: str | None = None, limit: int = LOG_LINES_MACHINE) -> list[dict[str, Any]]:
    """The newest `limit` lines, oldest first (newest last)."""
    sql = "SELECT ts, stream, line FROM machine_logs WHERE machine_id = %s" + (" AND workload = %s" if workload else "")
    args: tuple[Any, ...] = (machine_id, workload, limit) if workload else (machine_id, limit)
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT %s", args).fetchall()
    return [{"time": r["ts"].astimezone(tz).strftime("%H:%M:%S"), "stream": r["stream"], "line": r["line"]} for r in reversed(rows)]


# ---------------------------------------------------------------- workloads


def workload_list(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every workload with its running and queued counts (/workloads)."""
    return conn.execute(
        """
        SELECT w.name, w.manifest, w.image_digest, w.image_size_mb, w.enabled, w.synced_at,
               (SELECT count(*) FROM workload_assignments a WHERE a.workload = w.name) AS assigned,
               (SELECT count(*) FROM workload_assignments a WHERE a.workload = w.name AND a.state = 'running') AS running,
               (SELECT count(*) FROM workload_jobs j WHERE j.workload = w.name AND j.status = 'queued') AS queued
          FROM workloads w ORDER BY w.name
        """
    ).fetchall()


def manifest_lines(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """The manifest summary as (label, text) pairs for the workload page."""
    res, rt = manifest.get("resources", {}), manifest.get("runtime", {})
    actions = manifest.get("outbound_actions") or []
    cap = (f"{res['memory_max_mb']} MB" if res.get("memory_max_mb") else
           f"{res['memory_max_pct']}% of RAM" if res.get("memory_max_pct") else "none")
    secrets = f"{len(manifest.get('container_secrets') or [])} container, {len(manifest.get('host_only_secrets') or [])} host only"
    return [
        ("Mode", rt.get("mode", "?") + (": " + ", ".join(rt["job_kinds"]) if rt.get("job_kinds") else "")),
        ("Protocol", str(manifest.get("protocol", "?"))),
        ("Needs", f"{size_label(res.get('min_ram_mb'))} RAM, {size_label(res.get('min_disk_mb'))} free disk"),
        ("Write heavy", "yes: refused on flash and unknown disks" if res.get("write_heavy") else "no"),
        ("Memory cap", cap), ("CPUs", str(res.get("cpus") or "no cap")), ("Network", str(rt.get("network", "bridge"))),
        ("Outbound", (", ".join(actions) + ": every send waits for your approval") if actions else "none"),
        ("Secrets", secrets), ("Trading", "can place orders" if manifest.get("can_trade") else "no"),
    ]


def workload_detail(conn: psycopg.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT name, manifest, image_repo, image_digest, image_size_mb, enabled, synced_at FROM workloads WHERE name = %s", (name,)
    ).fetchone()
    if row is None:
        return None
    return {**row, "summary": manifest_lines(row["manifest"]), "description": row["manifest"].get("description", ""),
            "kinds": list(row["manifest"].get("runtime", {}).get("job_kinds") or []),
            "is_jobs": row["manifest"].get("runtime", {}).get("mode") == "jobs"}


def workload_machines(conn: psycopg.Connection, name: str) -> list[dict[str, Any]]:
    """Machines whose assignment is this workload, with the container state."""
    rows = conn.execute(
        """
        SELECT m.id, m.name, m.enabled, m.pinned, a.workload, a.state, a.epoch, a.restarts, a.last_error, a.cpu_pct,
               a.mem_mb, a.image_digest_running, a.run_manifest, a.run_image_digest,
               EXTRACT(EPOCH FROM (now() - a.started_at))::bigint AS up
          FROM workload_assignments a JOIN machines m ON m.id = a.machine_id
         WHERE a.workload = %s ORDER BY m.name
        """, (name,),
    ).fetchall()
    wl = conn.execute("SELECT name, manifest, image_digest FROM workloads WHERE name = %s", (name,)).fetchone()
    for r in rows:
        r["tone"], r["word"] = state_chip(name, r["state"])
        r["update_available"] = bool(wl) and r["state"] != "draining" and update_available(r, wl)
    return rows


def workload_jobs(conn: psycopg.Connection, name: str) -> dict[str, list[dict[str, Any]]]:
    """Running (queued, leased, cancelling) and Done (last 50) jobs of a workload."""
    cols = """
        SELECT j.id, j.kind, j.status, j.progress, j.params, j.result, j.error, j.target_machine_id,
               m.name AS machine_name, EXTRACT(EPOCH FROM (now() - j.created_at))::bigint AS age,
               EXTRACT(EPOCH FROM (now() - j.finished_at))::bigint AS finished_age
          FROM workload_jobs j LEFT JOIN machines m ON m.id = COALESCE(j.lease_machine_id, j.target_machine_id)
         WHERE j.workload = %s AND j.status = ANY(%s)
    """
    running = conn.execute(cols + " ORDER BY j.created_at LIMIT 100", (name, list(RUNNING_JOBS))).fetchall()
    done = conn.execute(cols + " ORDER BY j.finished_at DESC NULLS LAST LIMIT %s", (name, list(DONE_JOBS), DONE_LIMIT)).fetchall()
    for job in running + done:
        job["tone"], job["word"] = {
            "queued": ("muted", "queued"), "leased": ("ok", "running"), "cancel_requested": ("warn", "cancelling"),
            "succeeded": ("ok", "succeeded"), "failed": ("bad", "failed"), "cancelled": ("muted", "cancelled"),
        }[job["status"]]
        job["detail"] = short_text(job["error"] or job["result"] or job["params"], 140)
        job["cancellable"] = job["status"] in ("queued", "leased")
    return {"running": running, "done": done}


def workload_logs(conn: psycopg.Connection, name: str, machines: list[dict[str, Any]], tz: tzinfo) -> list[dict[str, Any]]:
    """The last 50 lines of this workload per machine running it."""
    return [{"machine": m, "lines": log_lines(conn, m["id"], tz, name, LOG_LINES_WORKLOAD)} for m in machines]


def secret_rows(conn: psycopg.Connection, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Declared secrets (and stored ones the manifest no longer declares): name, scope,
    whether a value is set, when. Values are never read here."""
    stored = {r["name"]: r for r in conn.execute(
        "SELECT name, scope, updated_at, updated_by FROM workload_secrets WHERE workload = %s", (manifest["name"],)).fetchall()}
    declared = [(n, "container") for n in manifest.get("container_secrets") or []] + [(n, "host_only") for n in manifest.get("host_only_secrets") or []]
    rows = [{"name": n, "scope": scope, "declared": True, "set": n in stored, "updated_at": stored[n]["updated_at"] if n in stored else None}
            for n, scope in declared]
    rows += [{"name": n, "scope": r["scope"], "declared": False, "set": True, "updated_at": r["updated_at"]}
             for n, r in sorted(stored.items()) if n not in {d[0] for d in declared}]
    return rows


# ---------------------------------------------------------------- outbound approvals


def outbound_summary(kind: str, payload: Any) -> str:
    """One line for a row: "email to x@y: subject"."""
    data = payload if isinstance(payload, dict) else {}
    if kind == "email":
        return short_text(f"email to {data.get('to', '?')}: {data.get('subject', '')}")
    detail = data.get("message") or data.get("text") or ""
    return short_text(f"{kind}: {detail}" if detail else kind)


_OUTBOUND_COLS = """
    SELECT o.id, o.workload, o.kind, o.payload, o.status, o.error, o.decided_by, m.name AS machine_name,
           EXTRACT(EPOCH FROM (now() - o.created_at))::bigint AS age,
           EXTRACT(EPOCH FROM (now() - o.decided_at))::bigint AS decided_age
      FROM outbound_actions o LEFT JOIN machines m ON m.id = o.machine_id
"""
_DONE_TONE = {"sent": "ok", "approved": "warn", "sending": "warn", "rejected": "muted", "expired": "muted", "failed": "bad"}


def _with_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in rows:
        r["summary"] = outbound_summary(r["kind"], r["payload"])
        r["tone"] = _DONE_TONE.get(r["status"], "muted")
    return rows


def outbound_pending(conn: psycopg.Connection, workload: str | None = None) -> list[dict[str, Any]]:
    where = " WHERE o.status = 'pending'" + (" AND o.workload = %s" if workload else "")
    return _with_summary(conn.execute(_OUTBOUND_COLS + where + " ORDER BY o.created_at", (workload,) if workload else ()).fetchall())


def outbound_done(conn: psycopg.Connection) -> list[dict[str, Any]]:
    sql = _OUTBOUND_COLS + " WHERE o.status <> 'pending' ORDER BY COALESCE(o.decided_at, o.created_at) DESC LIMIT %s"
    return _with_summary(conn.execute(sql, (DONE_LIMIT,)).fetchall())


def outbound_counts(conn: psycopg.Connection) -> dict[str, int]:
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, count(*) AS n FROM outbound_actions GROUP BY status").fetchall()}
    return {"pending": counts.get("pending", 0), "sent": counts.get("sent", 0), "rejected": counts.get("rejected", 0),
            "failed": counts.get("failed", 0) + counts.get("expired", 0)}
