"""specs.py, diskinfo.py and procinfo.py against fake /proc and /sys trees."""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from fleetagent import diskinfo, procinfo, specs
from tests.test_fleetagent_fakes import FakeDocker, FakeSys
from tests.test_fleetagent_harness import FakeSystemctl

MB = 1024 * 1024
SECTORS_16GB = 16_000_000_000 // 512


def _collector(sys_root, major: int, minor: int, free_mb: int = 4000, docker: FakeDocker | None = None, **kw) -> specs.SpecsCollector:
    docker = docker or FakeDocker()
    return specs.SpecsCollector(
        docker.docker(), proc=kw.get("proc"), sys_root=str(sys_root),
        stat_dev=lambda path: os.makedev(major, minor),
        statvfs=lambda path: SimpleNamespace(f_bavail=free_mb, f_frsize=MB),
    )


def _disk_of(tmp_path, build) -> diskinfo.DiskInfo:
    fsys = FakeSys(str(tmp_path))
    major, minor = build(fsys)
    return diskinfo.disk_info_for_device(major, minor, str(tmp_path))


def test_usb_stick_is_flash_by_its_bus_path_even_when_removable_is_0(tmp_path) -> None:
    def build(f: FakeSys):
        d = f.disk("sda", "pci0000:00/0000:00:14.0/usb2/2-1/2-1:1.0/host6/target6:0:0/6:0:0:0", SECTORS_16GB, removable=0, rotational=0)
        return f.link(8, 1, f.partition(d, "sda1", SECTORS_16GB - 2048))

    info = _disk_of(tmp_path, build)
    assert (info.type, info.name) == ("flash", "sda")
    assert info.size_mb == SECTORS_16GB * 512 // MB


def test_removable_flag_means_flash(tmp_path) -> None:
    def build(f: FakeSys):
        d = f.disk("sdb", "pci0000:00/ata3/host2/target2:0:0/2:0:0:0", SECTORS_16GB, removable=1, rotational=1)
        return f.link(8, 17, f.partition(d, "sdb1", 1000))

    assert _disk_of(tmp_path, build).type == "flash"


def test_sd_card_is_flash_by_the_mmcblk_name(tmp_path) -> None:
    def build(f: FakeSys):
        d = f.disk("mmcblk0", "platform/soc/mmc_host/mmc0/mmc0:aaaa", SECTORS_16GB, removable=0, rotational=0)
        return f.link(179, 2, f.partition(d, "mmcblk0p2", 1000))

    info = _disk_of(tmp_path, build)
    assert (info.type, info.name) == ("flash", "mmcblk0")


def test_sata_ssd_is_ssd(tmp_path) -> None:
    def build(f: FakeSys):
        d = f.disk("sda", "pci0000:00/0000:00:1f.2/ata1/host0/target0:0:0/0:0:0:0", 500_000_000, rotational=0)
        return f.link(8, 2, f.partition(d, "sda2", 400_000_000))

    assert _disk_of(tmp_path, build).type == "ssd"


def test_rotating_hdd_is_hdd(tmp_path) -> None:
    def build(f: FakeSys):
        d = f.disk("sda", "pci0000:00/0000:00:1f.2/ata2/host1/target1:0:0/1:0:0:0", 1_000_000_000, rotational=1)
        return f.link(8, 1, f.partition(d, "sda1", 900_000_000))

    info = _disk_of(tmp_path, build)
    assert (info.type, info.size_mb) == ("hdd", 1_000_000_000 * 512 // MB)


def test_nvme_is_ssd(tmp_path) -> None:
    def build(f: FakeSys):
        d = f.disk("nvme0n1", "pci0000:00/0000:00:1d.0/0000:3d:00.0/nvme/nvme0", 1_000_000, rotational=0)
        return f.link(259, 2, f.partition(d, "nvme0n1p2", 900_000))

    info = _disk_of(tmp_path, build)
    assert (info.type, info.name) == ("ssd", "nvme0n1")


def test_a_whole_disk_without_partitions_works(tmp_path) -> None:
    info = _disk_of(tmp_path, lambda f: f.link(8, 0, f.disk("sda", "pci0000:00/ata1/host0/target0:0:0/0:0:0:0", 2048, rotational=1)))
    assert (info.type, info.name) == ("hdd", "sda")


def test_lvm_maps_to_its_parent_disk(tmp_path) -> None:
    def build(f: FakeSys):
        d = f.disk("sda", "pci0000:00/0000:00:1f.2/ata1/host0/target0:0:0/0:0:0:0", 1_000_000_000, rotational=1)
        part = f.partition(d, "sda2", 900_000_000)
        return f.link(253, 0, f.mapper("dm-0", [part], 100_000_000))

    info = _disk_of(tmp_path, build)
    assert (info.type, info.name, info.size_mb) == ("hdd", "sda", 1_000_000_000 * 512 // MB)


def test_lvm_over_a_usb_stick_is_flash_and_over_two_disks_takes_the_cautious_type(tmp_path) -> None:
    def build(f: FakeSys):
        usb = f.disk("sdc", "pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0/host7/target7:0:0/7:0:0:0", 1000, rotational=0)
        ssd = f.disk("sda", "pci0000:00/ata1/host0/target0:0:0/0:0:0:0", 1000, rotational=0)
        return f.link(253, 1, f.mapper("dm-1", [f.partition(ssd, "sda1", 900), f.partition(usb, "sdc1", 900)], 1800))

    assert _disk_of(tmp_path, build).type == "flash"


def test_unknown_when_rotational_is_missing_or_the_device_is_not_in_sysfs(tmp_path) -> None:
    def build(f: FakeSys):
        d = f.disk("vda", "pci0000:00/0000:00:04.0/virtio1", 2048, rotational=None)
        return f.link(252, 1, f.partition(d, "vda1", 1000))

    assert _disk_of(tmp_path, build).type == "unknown"
    info = diskinfo.disk_info_for_device(0, 44, str(tmp_path))  # a btrfs style anonymous device
    assert (info.type, info.size_mb, info.name) == ("unknown", None, None)


def test_collect_reports_disk_size_free_and_docker_facts(tmp_path) -> None:
    fsys = FakeSys(str(tmp_path / "sys"))
    d = fsys.disk("sda", "pci0000:00/0000:00:14.0/usb2/2-1/2-1:1.0/host6/target6:0:0/6:0:0:0", SECTORS_16GB, rotational=0)
    major, minor = fsys.link(8, 1, fsys.partition(d, "sda1", 1000))
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal: 3891200 kB\nMemAvailable: 1000000 kB\n")
    (proc / "cpuinfo").write_text("processor : 0\n\nprocessor : 1\n\nprocessor : 2\n\nprocessor : 3\n")
    out = _collector(tmp_path / "sys", major, minor, free_mb=9000, proc=str(proc)).collect()
    assert out["disk_type"] == "flash" and out["disk_size_mb"] == SECTORS_16GB * 512 // MB
    assert out["disk_free_mb"] == 9000 and out["docker_root"] == "/var/lib/docker" and out["docker_ok"] is True
    assert out["docker_version"] == "26.1.5" and out["ram_total_mb"] == 3800 and out["ram_used_mb"] == 2823
    assert out["cpu_count"] == 4 and out["arch"] and out["cpu_pct"] == 0.0


def test_docker_root_that_does_not_exist_falls_back_to_its_nearest_parent(tmp_path) -> None:
    docker = FakeDocker(root=str(tmp_path / "not" / "there" / "docker"))
    seen: list[str] = []
    fsys = FakeSys(str(tmp_path / "sys"))
    major, minor = fsys.link(8, 1, fsys.disk("sda", "pci0000:00/ata1/host0/target0:0:0/0:0:0:0", 2048, rotational=0))
    coll = specs.SpecsCollector(
        docker.docker(), proc=str(tmp_path), sys_root=str(tmp_path / "sys"),
        stat_dev=lambda p: (seen.append(p), os.makedev(major, minor))[1],
        statvfs=lambda p: (seen.append(p), SimpleNamespace(f_bavail=5, f_frsize=MB))[1],
    )
    out = coll.collect()
    assert out["disk_type"] == "ssd" and out["disk_free_mb"] == 5
    assert all(os.path.exists(p) for p in seen)


def test_docker_down_reports_docker_ok_false_and_keeps_the_last_known_root(tmp_path) -> None:
    docker = FakeDocker()
    fsys = FakeSys(str(tmp_path / "sys"))
    major, minor = fsys.link(8, 1, fsys.disk("sda", "pci0000:00/ata1/host0/target0:0:0/0:0:0:0", 2048, rotational=1))
    coll = _collector(tmp_path / "sys", major, minor, docker=docker)
    assert coll.collect()["docker_ok"] is True
    docker.down = True
    out = coll.collect()
    assert out["docker_ok"] is False and out["docker_root"] == "/var/lib/docker" and out["disk_type"] == "hdd"
    fresh = _collector(tmp_path / "sys", major, minor, docker=docker).collect()
    assert fresh["docker_ok"] is False and fresh["docker_root"] is None and fresh["disk_type"] == "unknown"


# ------------------------------------------------------------- native probe


@pytest.mark.parametrize("state,expected", [("active", "active"), ("inactive", "inactive"), ("absent", "absent")])
def test_native_polymarket_from_systemctl(state, expected) -> None:
    runner = FakeSystemctl(state)
    assert specs.native_polymarket(runner, "systemctl") == expected
    assert runner.calls[0] == ["is-active", "fleet-worker"]


def test_native_polymarket_activating_counts_as_active_and_garbage_is_unknown() -> None:
    def activating(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 3, "activating\n", "")

    assert specs.native_polymarket(activating, "systemctl") == "active"

    def deactivating(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 3, "deactivating\n", "")

    assert specs.native_polymarket(deactivating, "systemctl") == "active", "a stopping worker still owns the machine"

    def garbage(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "Failed to connect to bus: No such file or directory\n")

    assert specs.native_polymarket(garbage, "systemctl") is None

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    assert specs.native_polymarket(missing, "systemctl") == "absent"


def test_native_polymarket_uses_the_systemctl_override(monkeypatch, tmp_path) -> None:
    script = tmp_path / "fake-systemctl"
    script.write_text('#!/bin/sh\nif [ "$1" = is-active ]; then echo active; exit 0; fi\necho enabled\n')
    script.chmod(0o755)
    monkeypatch.setenv("FLEET_AGENT_SYSTEMCTL", str(script))
    assert specs.native_polymarket() == "active"


# ----------------------------------------------------------------- procinfo


def test_procinfo_reads_a_fake_proc(tmp_path) -> None:
    (tmp_path / "meminfo").write_text("MemTotal: 4096000 kB\nMemFree: 100 kB\nMemAvailable: 1024000 kB\n")
    (tmp_path / "cpuinfo").write_text("processor\t: 0\nprocessor\t: 1\n")
    (tmp_path / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    (tmp_path / "sys" / "kernel" / "random").mkdir(parents=True)
    (tmp_path / "sys" / "kernel" / "random" / "boot_id").write_text("abc-123\n")
    assert procinfo.ram_total_mb(str(tmp_path)) == 4000 and procinfo.ram_used_mb(str(tmp_path)) == 3000
    assert procinfo.cpu_count(str(tmp_path)) == 2 and procinfo.boot_id(str(tmp_path)) == "abc-123"
    meter = procinfo.CpuMeter(str(tmp_path))
    assert meter.sample() == 0.0
    (tmp_path / "stat").write_text("cpu  200 0 200 900 0 0 0 0 0 0\n")  # +100 user +100 system +100 idle
    assert meter.sample() == 66.7
    assert procinfo.ram_total_mb(str(tmp_path / "missing")) is None and procinfo.boot_id(str(tmp_path / "missing")) is None


def test_specs_command_prints_json(tmp_path, monkeypatch, capsys) -> None:
    from fleetagent.__main__ import main

    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal: 2048000 kB\nMemAvailable: 1048000 kB\n")
    stub = tmp_path / "docker"
    stub.write_text(f'#!/bin/sh\necho \'{{"DockerRootDir": "{tmp_path}", "ServerVersion": "9.9"}}\'\n')
    stub.chmod(0o755)
    monkeypatch.setenv("FLEET_AGENT_DOCKER", str(stub))
    monkeypatch.setenv("FLEET_AGENT_PROC_ROOT", str(proc))
    monkeypatch.setenv("FLEET_AGENT_SYS_ROOT", str(tmp_path / "nosys"))
    monkeypatch.setenv("FLEET_AGENT_SYSTEMCTL", str(tmp_path / "no-systemctl"))
    assert main(["specs"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ram_total_mb"] == 2000 and out["docker_version"] == "9.9" and out["disk_type"] == "unknown"
    assert out["native_polymarket"] == "absent"
