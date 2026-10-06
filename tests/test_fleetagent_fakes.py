"""Test doubles for the fleetagent tests: an in-memory docker CLI and fake sysfs trees.

FakeDocker is a `runner` for fleetagent.docker.Docker: it parses exactly the commands that
class issues and keeps containers, images and logs in memory, so no daemon is needed.
FakeSys builds /sys/dev/block style trees (USB stick, SD card, SATA SSD, rotating disk,
NVMe, LVM over a partition, an unknown device). The two sanity tests at the bottom keep
the doubles honest.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from typing import Any

from fleetagent.docker import Docker


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class FakeDocker:
    """Callable runner plus controls. `calls` records every command (argv without the binary)."""

    def __init__(self, root: str = "/var/lib/docker") -> None:
        self.root = root
        self.lock = threading.RLock()
        self.calls: list[list[str]] = []
        self.client_env: dict[str, dict[str, str]] = {}  # container id -> env of the `docker run` client
        self.images: list[dict[str, Any]] = []
        self.containers: dict[str, dict[str, Any]] = {}
        self.down = False
        self.fail_pull: str | None = None
        self.fail_run: str | None = None
        self.fail_stop = False
        self.pulls: list[str] = []
        self.stop_exit_code = 0
        self.pull_gate: threading.Event | None = None
        self.prune_calls: list[str] = []
        self._tick = 0
        self._seq = 0

    # ------------------------------------------------------------- controls

    def docker(self) -> Docker:
        return Docker(runner=self, binary="docker")

    def add_image(self, repo: str, tag: str = "<none>", digest: str | None = None, size: str = "10MB", image_id: str | None = None) -> str:
        digest = digest if digest is not None else "sha256:" + _sha(repo + tag)
        image_id = image_id or "sha256:" + _sha("id" + repo + tag + digest)
        self.images.append({"ID": image_id, "Repository": repo, "Tag": tag, "Digest": digest, "Size": size})
        return image_id

    def ref_present(self, ref: str) -> bool:
        return any(ref in self._refs(i) for i in self.images)

    @staticmethod
    def _refs(img: dict[str, Any]) -> list[str]:
        out = []
        if img["Tag"] != "<none>":
            out.append(f"{img['Repository']}:{img['Tag']}")
        if img["Digest"] not in ("", "<none>"):
            out.append(f"{img['Repository']}@{img['Digest']}")
        return out

    def container(self, name_or_id: str) -> dict[str, Any] | None:
        for cid, c in self.containers.items():
            if cid == name_or_id or c["name"] == name_or_id or cid.startswith(name_or_id):
                return c
        return None

    def running(self) -> list[dict[str, Any]]:
        return [c for c in self.containers.values() if c["running"]]

    def exit(self, name_or_id: str, code: int) -> None:
        c = self.container(name_or_id)
        assert c is not None, name_or_id
        c["running"], c["exit_code"] = False, code

    def emit(self, name_or_id: str, stream: str, text: str, ts: str | None = None) -> str:
        c = self.container(name_or_id)
        assert c is not None, name_or_id
        with self.lock:
            self._tick += 1
            stamp = ts or f"2026-10-06T20:{self._tick // 60 % 60:02d}:{self._tick % 60:02d}.000000000Z"
            c["logs"].append((stamp, stream, text))
        return stamp

    def calls_of(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]

    # --------------------------------------------------------------- runner

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        args = list(cmd[1:])
        if args[0] == "pull" and self.pull_gate is not None:
            self.pull_gate.wait(10.0)  # outside the lock: other commands keep working while a pull "downloads"
        with self.lock:
            self.calls.append(args)
            rc, out, err = self._dispatch(args, kwargs.get("env"))
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def _dispatch(self, a: list[str], env: dict[str, str] | None) -> tuple[int, str, str]:
        head = a[0]
        if self.down:
            return 1, "", "Cannot connect to the Docker daemon"
        handler = getattr(self, "_cmd_" + head.replace("-", "_"), None)
        if handler is None:
            return 1, "", f"unknown command {head}"
        return handler(a, env)

    def _cmd_info(self, a: list[str], env: Any) -> tuple[int, str, str]:
        return 0, json.dumps({"DockerRootDir": self.root, "ServerVersion": "26.1.5"}), ""

    def _cmd_image(self, a: list[str], env: Any) -> tuple[int, str, str]:
        if a[1] == "inspect":
            return (0, "sha256:x\n", "") if self.ref_present(a[-1]) else (1, "", "No such image")
        if a[1] == "prune":
            self.prune_calls.append("image")
            return 0, "Total reclaimed space: 1.5MB\n", ""
        return 1, "", "bad image command"

    def _cmd_pull(self, a: list[str], env: Any) -> tuple[int, str, str]:
        ref = a[-1]
        self.pulls.append(ref)
        if self.fail_pull:
            return 1, "", self.fail_pull
        repo, _, digest = ref.partition("@")
        self.add_image(repo, "<none>", digest or None, "50MB")
        return 0, ref + "\n", ""

    def _cmd_images(self, a: list[str], env: Any) -> tuple[int, str, str]:
        return 0, "".join(json.dumps(i) + "\n" for i in self.images), ""

    def _cmd_rmi(self, a: list[str], env: Any) -> tuple[int, str, str]:
        ref = a[1]
        hit = [i for i in self.images if ref in self._refs(i)]
        if not hit:
            return 1, "", "No such image"
        used = {c["image_id"] for c in self.containers.values()}
        if hit[0]["ID"] in used:
            return 1, "", "image is being used by a container"
        self.images.remove(hit[0])
        return 0, f"Untagged: {ref}\n", ""

    def _cmd_builder(self, a: list[str], env: Any) -> tuple[int, str, str]:
        self.prune_calls.append("builder")
        return 0, "ID\tRECLAIMABLE\nTotal:\t2.5kB\n", ""

    def _cmd_container(self, a: list[str], env: Any) -> tuple[int, str, str]:
        self.prune_calls.append("container")
        return 0, "", ""

    def _cmd_run(self, a: list[str], env: dict[str, str] | None) -> tuple[int, str, str]:
        if "--rm" in a:  # helper container (scratch wipe)
            return 0, "", ""
        if self.fail_run:
            return 125, "", self.fail_run
        rest = a[2:]
        name = rest[rest.index("--name") + 1]
        if any(c["name"] == name for c in self.containers.values()):
            return 125, "", f"Conflict. The container name /{name} is already in use"
        labels: dict[str, str] = {}
        envs: list[str] = []
        stop_timeout = None
        i = 0
        while i < len(rest) - 1:
            if rest[i] == "--label":
                k, _, v = rest[i + 1].partition("=")
                labels[k] = v
            elif rest[i] == "-e":
                item = rest[i + 1]
                envs.append(item if "=" in item else f"{item}={(env or {}).get(item, '')}")
            elif rest[i] == "--stop-timeout":
                stop_timeout = int(rest[i + 1])
            i += 1
        image = rest[-1]
        image_id = next((x["ID"] for x in self.images if image in self._refs(x)), "sha256:" + _sha(image))
        self._seq += 1
        cid = _sha(f"{name}{self._seq}")
        self.containers[cid] = {
            "id": cid, "name": name, "labels": labels, "env": envs, "image": image, "image_id": image_id, "running": True,
            "exit_code": 0, "args": rest, "created": f"2026-10-06T19:00:{self._seq:02d}.000000000Z", "logs": [],
            "stop_timeout": stop_timeout, "oom": False,
        }
        self.client_env[cid] = dict(env or {})
        return 0, cid + "\n", ""

    def _cmd_stop(self, a: list[str], env: Any) -> tuple[int, str, str]:
        c = self.container(a[-1])
        if c is None:
            return 1, "", "No such container"
        if self.fail_stop:
            return 1, "", "cannot stop"
        c["running"], c["exit_code"] = False, self.stop_exit_code
        return 0, a[-1] + "\n", ""

    def _cmd_rm(self, a: list[str], env: Any) -> tuple[int, str, str]:
        c = self.container(a[-1])
        if c is None:
            return 1, "", "No such container"
        del self.containers[c["id"]]
        return 0, a[-1] + "\n", ""

    def _cmd_ps(self, a: list[str], env: Any) -> tuple[int, str, str]:
        wanted = [a[i + 1].split("=", 1)[1] for i in range(len(a) - 1) if a[i] == "--filter" and a[i + 1].startswith("label=")]
        ids = []
        for c in sorted(self.containers.values(), key=lambda x: x["created"]):
            if "-a" not in a and not c["running"]:
                continue
            if all((w.split("=")[0] in c["labels"]) and ("=" not in w or c["labels"][w.split("=")[0]] == w.split("=", 1)[1]) for w in wanted):
                ids.append(c["id"])
        return 0, "".join(i + "\n" for i in ids), ""

    def _cmd_inspect(self, a: list[str], env: Any) -> tuple[int, str, str]:
        rows = []
        for ident in a[3:]:
            c = self.container(ident)
            if c is None:
                continue
            rows.append({
                "Id": c["id"], "Name": "/" + c["name"], "Created": c["created"], "Image": c["image_id"],
                "State": {"Status": "running" if c["running"] else "exited", "Running": c["running"], "ExitCode": c["exit_code"],
                          "StartedAt": c["created"], "FinishedAt": "", "Error": "", "OOMKilled": c["oom"]},
                "Config": {"Image": c["image"], "Labels": c["labels"], "Env": c["env"], "StopTimeout": c["stop_timeout"]},
            })
        return (0, json.dumps(rows), "") if rows else (1, "[]", "No such container")

    def _cmd_stats(self, a: list[str], env: Any) -> tuple[int, str, str]:
        if self.container(a[-1]) is None:
            return 1, "", "No such container"
        return 0, json.dumps({"CPUPerc": "1.50%", "MemUsage": "40MiB / 3.7GiB"}) + "\n", ""

    def _cmd_logs(self, a: list[str], env: Any) -> tuple[int, str, str]:
        c = self.container(a[-1])
        if c is None:
            return 1, "", "No such container"
        since = a[a.index("--since") + 1] if "--since" in a else None
        out: dict[str, list[str]] = {"stdout": [], "stderr": []}
        for ts, stream, text in c["logs"]:
            if since is None or ts >= since:
                out[stream].append(f"{ts} {text}")
        if "--tail" in a:
            keep = int(a[a.index("--tail") + 1])
            rows = [(ts, st, tx) for ts, st, tx in c["logs"] if since is None or ts >= since][-keep:]
            out = {"stdout": [f"{ts} {tx}" for ts, st, tx in rows if st == "stdout"], "stderr": [f"{ts} {tx}" for ts, st, tx in rows if st == "stderr"]}
        return 0, "".join(x + "\n" for x in out["stdout"]), "".join(x + "\n" for x in out["stderr"])


class FakeSys:
    """Builds a fake /sys block tree under `root` (devices/, dev/block/)."""

    def __init__(self, root: str) -> None:
        self.root = root
        os.makedirs(os.path.join(root, "dev", "block"), exist_ok=True)

    def _write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def disk(self, name: str, bus_path: str, sectors: int, removable: int = 0, rotational: int | None = 0) -> str:
        """A whole disk at devices/<bus_path>/block/<name>; returns its directory."""
        path = os.path.join(self.root, "devices", bus_path, "block", name)
        self._write(os.path.join(path, "size"), str(sectors))
        self._write(os.path.join(path, "removable"), str(removable))
        if rotational is not None:
            self._write(os.path.join(path, "queue", "rotational"), str(rotational))
        return path

    def partition(self, disk_dir: str, name: str, sectors: int) -> str:
        path = os.path.join(disk_dir, name)
        self._write(os.path.join(path, "size"), str(sectors))
        self._write(os.path.join(path, "partition"), "1")
        return path

    def link(self, major: int, minor: int, target: str) -> tuple[int, int]:
        """/sys/dev/block/<major>:<minor> -> target (a relative symlink, like the real one)."""
        node = os.path.join(self.root, "dev", "block", f"{major}:{minor}")
        os.symlink(os.path.relpath(target, os.path.dirname(node)), node)
        return major, minor

    def mapper(self, name: str, parents: list[str], sectors: int) -> str:
        """A device-mapper device at devices/virtual/block/<name> with slaves/ pointing at its parents."""
        path = os.path.join(self.root, "devices", "virtual", "block", name)
        self._write(os.path.join(path, "size"), str(sectors))
        for parent in parents:
            slave = os.path.join(path, "slaves", os.path.basename(parent))
            os.makedirs(os.path.dirname(slave), exist_ok=True)
            os.symlink(os.path.relpath(parent, os.path.dirname(slave)), slave)
        return path


# ---------------------------------------------------------------- sanity tests


def test_fake_docker_round_trip() -> None:
    fake = FakeDocker()
    docker = fake.docker()
    fake.add_image("reg/fleet/x", digest="sha256:" + "a" * 64)
    ref = "reg/fleet/x@sha256:" + "a" * 64
    assert docker.image_present(ref)
    cid = docker.run(["--name", "n1", "--label", "fleet.workload=x", "--label", "fleet.epoch=2", "-e", "T", ref], env={"T": "tok"})
    info = docker.inspect(cid)
    assert info is not None and info.running and info.workload == "x" and info.epoch == 2
    assert info.env_value("T") == "tok"
    fake.emit("n1", "stdout", "hello")
    assert [r[2] for r in docker.logs(cid)] == ["hello"]
    docker.stop(cid, 5)
    assert docker.ps(["fleet.workload"])[0].running is False
    docker.rm(cid)
    assert docker.ps(["fleet.workload"]) == []


def test_fake_sys_resolves_a_partition_to_its_disk(tmp_path) -> None:
    from fleetagent import diskinfo

    sys = FakeSys(str(tmp_path))
    disk = sys.disk("sda", "pci0000:00/0000:00:1f.2/ata1/host0/target0:0:0/0:0:0:0", 1_000_000, rotational=0)
    part = sys.partition(disk, "sda1", 900_000)
    major, minor = sys.link(8, 1, part)
    info = diskinfo.disk_info_for_device(major, minor, str(tmp_path))
    assert (info.type, info.name, info.size_mb) == ("ssd", "sda", 1_000_000 * 512 // (1024 * 1024))
