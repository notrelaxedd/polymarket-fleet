"""Shared helpers and fixtures for the tests/test_wl_api_*.py files (no tests of its own)."""
from __future__ import annotations

import base64
import os
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from host.workloads import machines
from host.workloads.manifest import parse_manifest

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
GOOD_SPECS = {
    "cpu_pct": 4.0, "ram_used_mb": 900, "ram_total_mb": 3800, "cpu_count": 4, "arch": "x86_64",
    "disk_type": "ssd", "disk_size_mb": 100_000, "disk_free_mb": 60_000, "docker_root": "/var/lib/docker",
    "docker_ok": True, "docker_version": "26.1.5",
}


def manifest_data(name: str = "hello", **over: Any) -> dict[str, Any]:
    """A valid jobs-mode manifest with container and host-only secrets and outbound email/log."""
    data: dict[str, Any] = {
        "schema": 1, "name": name, "description": "Says hello", "image": f"fleet/{name}", "protocol": "workload-v1",
        "resources": {"min_ram_mb": 128, "min_disk_mb": 300, "write_heavy": False, "memory_max_mb": 256, "cpus": 1.0},
        "runtime": {"mode": "jobs", "job_kinds": ["hello"], "network": "bridge", "uid": 10001, "scratch_mb": 512,
                    "stop_timeout_s": 15, "no_restart_exit_codes": [78], "nice": 0},
        "secrets": {"container": ["HELLO_GREETING"], "host_only": ["SMTP_URL", "EMAIL_FROM"]},
        "outbound": {"actions": ["email", "log"]},
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return data


def polymarket_data() -> dict[str, Any]:
    return {
        "schema": 1, "name": "polymarket", "description": "Polymarket worker", "image": "fleet/polymarket",
        "protocol": "fleet-worker",
        "resources": {"min_ram_mb": 3000, "min_disk_mb": 2048, "write_heavy": False, "memory_max_pct": 85},
        "runtime": {"mode": "service", "network": "host", "uts_host": True, "state_volume": True, "nice": 5,
                    "stop_timeout_s": 15, "no_restart_exit_codes": [78]},
        "secrets": {"container": ["FLEET_ENROLL_TOKEN"]},
        "trading": {"can_trade": True},
    }


def add_workload(conn: psycopg.Connection, data: dict[str, Any], digest: str | None = DIGEST_A,
                 size_mb: int | None = 50, enabled: bool = True) -> dict[str, Any]:
    """Insert a workloads row straight from manifest data."""
    manifest = parse_manifest(data, data["name"])
    return conn.execute(
        "INSERT INTO workloads (name, manifest, image_repo, image_digest, image_size_mb, enabled)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
        (manifest.name, Jsonb(manifest.to_json()), manifest.image, digest, size_mb, enabled),
    ).fetchone()


def enroll(client: TestClient, conn: psycopg.Connection, name: str = "box1", boot_id: str | None = None,
           specs: dict[str, Any] | None = None, ip: str | None = None) -> tuple[str, str]:
    """Enroll a machine through the API; returns (machine_id, machine_token)."""
    token = machines.create_enroll_token(conn)["token"]
    body: dict[str, Any] = {"enroll_token": token, "name": name, "hostname": name, "agent_version": "a1b2c3d4e5f6",
                            "specs": GOOD_SPECS if specs is None else specs}
    if boot_id:
        body["boot_id"] = boot_id
    headers = {"X-Forwarded-For": ip} if ip else {}
    r = client.post("/api/v1/machines/register", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["machine_id"], r.json()["machine_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def beat(client: TestClient, machine_id: str, token: str, **body: Any):
    """POST a machine heartbeat."""
    return client.post(f"/api/v1/machines/{machine_id}/heartbeat", json=body, headers=auth(token))


def container(workload: str, epoch: int, state: str = "running", **extra: Any) -> dict[str, Any]:
    return {"workload": workload, "epoch": epoch, "state": state, "container_id": "c0ffee", "image_digest": DIGEST_A,
            "exit_code": None, "restarts": 0, "cpu_pct": 1.5, "mem_mb": 40, "started_at": "2026-10-06T20:00:00Z",
            "error": None, **extra}


def run_token_for(client: TestClient, machine_id: str, token: str, epoch: int) -> dict[str, Any]:
    """POST /start and return its JSON body."""
    r = client.post(f"/api/v1/machines/{machine_id}/start", json={"epoch": epoch}, headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def secrets_key(monkeypatch) -> str:
    """A configured FLEET_SECRETS_KEY."""
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("FLEET_SECRETS_KEY", key)
    return key


@pytest.fixture
def no_secrets_key(monkeypatch) -> None:
    monkeypatch.delenv("FLEET_SECRETS_KEY", raising=False)


@pytest.fixture(autouse=True)
def _registry_env(monkeypatch) -> None:
    monkeypatch.setenv("FLEET_REGISTRY", "reg.example.ts.net:5000")
    monkeypatch.delenv("FLEET_AGENT_DIR", raising=False)
