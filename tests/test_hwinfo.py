"""fleet.common.hwinfo against fake /sys and /proc trees built in tmp_path."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fleet.common import hwinfo

PCI = "devices/pci0000:00/0000:00:17.0/ata1/host0/target0:0:0/0:0:0:0/block"
USB = "devices/pci0000:00/0000:00:14.0/usb2/2-1/2-1:1.0/host6/target6:0:0/6:0:0:0/block"
NVME = "devices/pci0000:00/0000:00:1d.0/0000:3d:00.0/nvme/nvme0/block"
MMC = "devices/platform/fe340000.mmc/mmc_host/mmc0/mmc0:0001/block"
VIRTUAL = "devices/virtual/block"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(target, link.parent))


def add_disk(sys_root: Path, name: str, devnum: str, parent: str = PCI, parts: dict[str, str] | None = None,
             rotational: str | None = "0", removable: str = "0", stat: str | None = None) -> Path:
    """A whole disk under sys_root/parent with partitions {name: devnum}, linked from
    /sys/dev/block and /sys/class/block like the kernel does."""
    disk = sys_root / parent / name
    disk.mkdir(parents=True)
    _write(disk / "removable", removable + "\n")
    if rotational is not None:
        _write(disk / "queue" / "rotational", rotational + "\n")
    if stat is not None:
        _write(disk / "stat", stat)
    _link(sys_root / "dev" / "block" / devnum, disk)
    _link(sys_root / "class" / "block" / name, disk)
    for part, part_devnum in (parts or {}).items():
        _write(disk / part / "partition", "1\n")
        _link(sys_root / "dev" / "block" / part_devnum, disk / part)
        _link(sys_root / "class" / "block" / part, disk / part)
    return disk


def mountinfo(proc_root: Path, *lines: str) -> None:
    _write(proc_root / "self" / "mountinfo", "".join(line + "\n" for line in lines))


def root_line(devnum: str, fstype: str, source: str, mount_id: int = 22) -> str:
    return f"{mount_id} 1 {devnum} / / rw,relatime shared:1 - {fstype} {source} rw"


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    sys_root, proc_root = tmp_path / "sys", tmp_path / "proc"
    sys_root.mkdir()
    proc_root.mkdir()
    return sys_root, proc_root


def _boot(roots: tuple[Path, Path]) -> str | None:
    sys_root, proc_root = roots
    return hwinfo.boot_disk(str(sys_root), str(proc_root))


# ------------------------------------------------------------------ temperature


def _sensor(sys_root: Path, hwmon: str, name: str, **temps: str) -> None:
    base = sys_root / "class" / "hwmon" / hwmon
    _write(base / "name", name + "\n")
    for key, value in temps.items():
        _write(base / f"{key}_input", value + "\n")
        _write(base / f"{key}_label", "Core\n")


def test_temp_prefers_the_hottest_cpu_sensor_over_a_hotter_disk(roots) -> None:
    sys_root, _ = roots
    _sensor(sys_root, "hwmon0", "nvme", temp1="70850")
    _sensor(sys_root, "hwmon1", "coretemp", temp1="52000", temp2="61500", temp3="200000", temp4="-5000", temp5="garbage")
    assert hwinfo.temp_c(str(sys_root)) == 61.5


def test_temp_uses_any_hwmon_sensor_without_a_cpu_one(roots) -> None:
    sys_root, _ = roots
    _sensor(sys_root, "hwmon0", "drivetemp", temp1="38000")
    _sensor(sys_root, "hwmon1", "nvme", temp1="44123")
    assert hwinfo.temp_c(str(sys_root)) == 44.1


def test_temp_falls_back_to_thermal_zones(roots) -> None:
    sys_root, _ = roots
    thermal = sys_root / "class" / "thermal"
    _write(thermal / "thermal_zone0" / "temp", "48000\n")
    _write(thermal / "thermal_zone1" / "temp", "0\n")
    _write(thermal / "thermal_zone2" / "temp", "nope\n")
    _write(thermal / "cooling_device0" / "temp", "90000\n")
    assert hwinfo.temp_c(str(sys_root)) == 48.0


def test_temp_ignores_implausible_values_and_reports_none_without_sensors(roots) -> None:
    sys_root, _ = roots
    assert hwinfo.temp_c(str(sys_root)) is None
    _sensor(sys_root, "hwmon0", "coretemp", temp1="0", temp2="127000", temp3="")
    _write(sys_root / "class" / "thermal" / "thermal_zone0" / "temp", "-273150\n")
    assert hwinfo.temp_c(str(sys_root)) is None


# -------------------------------------------------------------------- boot disk


def test_root_on_a_sata_partition_is_its_disk(roots) -> None:
    sys_root, proc_root = roots
    disk = add_disk(sys_root, "sda", "8:0", parts={"sda1": "8:1", "sda2": "8:2"}, rotational="1")
    mountinfo(proc_root, "21 26 0:20 / /sys rw - sysfs sysfs rw", root_line("8:2", "ext4", "/dev/sda2"), "30 22 8:1 / /boot/efi rw - vfat /dev/sda1 rw")
    found = _boot(roots)
    assert found == str(disk.resolve())
    assert hwinfo.boot_media(found) == "hdd"


def test_root_on_nvme_partition(roots) -> None:
    sys_root, proc_root = roots
    disk = add_disk(sys_root, "nvme0n1", "259:0", parent=NVME, parts={"nvme0n1p1": "259:1"}, rotational="0")
    mountinfo(proc_root, root_line("259:1", "ext4", "/dev/nvme0n1p1"))
    found = _boot(roots)
    assert found == str(disk.resolve())
    assert hwinfo.boot_media(found) == "ssd"


def test_root_on_device_mapper_follows_its_slave_to_the_disk(roots) -> None:
    sys_root, proc_root = roots
    disk = add_disk(sys_root, "sdb", "8:16", parts={"sdb1": "8:17", "sdb3": "8:19"}, rotational="0")
    dm = sys_root / VIRTUAL / "dm-0"
    dm.mkdir(parents=True)
    _link(sys_root / "dev" / "block" / "253:0", dm)
    _link(sys_root / "class" / "block" / "dm-0", dm)
    _link(dm / "slaves" / "sdb3", disk / "sdb3")
    mountinfo(proc_root, root_line("253:0", "ext4", "/dev/mapper/vg-root"))
    assert _boot(roots) == str(disk.resolve())


def test_btrfs_anonymous_devnum_falls_back_to_the_mount_source(roots) -> None:
    sys_root, proc_root = roots
    disk = add_disk(sys_root, "sdc", "8:32", parts={"sdc3": "8:35"})
    mountinfo(proc_root, "22 1 0:31 /@ / rw,relatime shared:1 - btrfs /dev/sdc3 rw,subvol=/@")
    assert _boot(roots) == str(disk.resolve())


def test_the_last_mount_on_root_wins(roots) -> None:
    sys_root, proc_root = roots
    add_disk(sys_root, "sda", "8:0", parts={"sda1": "8:1"})
    second = add_disk(sys_root, "sdb", "8:16", parts={"sdb1": "8:17"})
    mountinfo(proc_root, root_line("8:1", "ext4", "/dev/sda1", 1), root_line("8:17", "ext4", "/dev/sdb1", 2))
    assert _boot(roots) == str(second.resolve())


@pytest.mark.parametrize("lines", [
    (),
    ("22 1 0:30 / / rw - overlay overlay rw,lowerdir=/a",),
    ("22 1 0:30 / / rw - zfs rpool/ROOT/debian rw",),
    ("garbage line", "1 2 3"),
    (root_line("8:2", "ext4", "/dev/sda2"),),  # nothing in sysfs for it
])
def test_unknown_root_device_is_none(roots, lines) -> None:
    _, proc_root = roots
    if lines:
        mountinfo(proc_root, *lines)
    found = _boot(roots)
    assert found is None
    assert hwinfo.boot_media(found) == "unknown"
    assert hwinfo.gb_written(found) is None and hwinfo.wear_pct(found) is None


# ------------------------------------------------------------------- boot media


def test_usb_disk_is_flash_even_when_it_claims_to_rotate(roots) -> None:
    sys_root, _ = roots
    assert hwinfo.boot_media(str(add_disk(sys_root, "sda", "8:0", parent=USB, rotational="1"))) == "flash"


def test_removable_disk_is_flash(roots) -> None:
    sys_root, _ = roots
    assert hwinfo.boot_media(str(add_disk(sys_root, "sdb", "8:16", rotational="0", removable="1"))) == "flash"


def test_mmc_is_flash(roots) -> None:
    sys_root, _ = roots
    assert hwinfo.boot_media(str(add_disk(sys_root, "mmcblk0", "179:0", parent=MMC, rotational="0"))) == "flash"


@pytest.mark.parametrize("rotational,expected", [("0", "ssd"), ("1", "hdd"), ("x", "unknown"), (None, "unknown")])
def test_rotational_flag(roots, rotational, expected) -> None:
    sys_root, _ = roots
    assert hwinfo.boot_media(str(add_disk(sys_root, "sda", "8:0", rotational=rotational))) == expected


def test_loop_device_is_unknown(roots) -> None:
    sys_root, _ = roots
    assert hwinfo.boot_media(str(add_disk(sys_root, "loop0", "7:0", parent=VIRTUAL, rotational="0"))) == "unknown"


# ------------------------------------------------------------------ gb written


def test_gb_written_reads_sectors_written_from_stat(roots) -> None:
    sys_root, _ = roots
    stat = "   12021     3105  1093846    6152    41000    21000  4000000   61000        0   40000   70000\n"
    assert hwinfo.gb_written(str(add_disk(sys_root, "sda", "8:0", stat=stat))) == 2.05


@pytest.mark.parametrize("stat", [None, "", "1 2 3", "a b c d e f g h i j k"])
def test_gb_written_garbage_is_none(roots, stat) -> None:
    sys_root, _ = roots
    assert hwinfo.gb_written(str(add_disk(sys_root, "sda", "8:0", stat=stat))) is None


# ------------------------------------------------------------------------ wear


def _wear_file(tmp_path: Path, payload: object) -> str:
    path = tmp_path / "wear.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return str(path)


def test_wear_from_the_smart_file(roots, tmp_path) -> None:
    sys_root, _ = roots
    disk = str(add_disk(sys_root, "sda", "8:0"))
    assert hwinfo.wear_pct(disk, _wear_file(tmp_path, {"at": 1, "devices": {"sda": {"wear_pct": 12.5}, "sdb": {"wear_pct": 90}}})) == 12.5
    assert hwinfo.wear_pct(disk, _wear_file(tmp_path, {"devices": {"sda": {"wear_pct": 130}}})) == 100.0, "NVMe can report past 100"
    assert hwinfo.wear_pct(disk, _wear_file(tmp_path, {"devices": {"sdb": {"wear_pct": 3}}})) is None
    assert hwinfo.wear_pct(disk, str(tmp_path / "missing.json")) is None


@pytest.mark.parametrize("payload", ["{not json", "[1, 2]", {"devices": [1]}, {"devices": {"sda": 5}}, {"devices": {"sda": {"wear_pct": True}}},
                                     {"devices": {"sda": {"wear_pct": "7"}}}, {"devices": {"sda": {"wear_pct": -1}}}, {"devices": {"sda": {"wear_pct": 5000}}}])
def test_wear_file_garbage_is_none(roots, tmp_path, payload) -> None:
    sys_root, _ = roots
    assert hwinfo.wear_pct(str(add_disk(sys_root, "sda", "8:0")), _wear_file(tmp_path, payload)) is None


@pytest.mark.parametrize("life_time,expected", [("0x02 0x03", 30.0), ("0x01 0x01", 10.0), ("0x0B 0x01", 100.0), ("0x00 0x00", None), ("zz 0x01", None), ("", None)])
def test_emmc_life_time(roots, tmp_path, life_time, expected) -> None:
    sys_root, _ = roots
    disk = add_disk(sys_root, "mmcblk0", "179:0", parent=MMC)
    _write(disk / "device" / "life_time", life_time + "\n")
    assert hwinfo.wear_pct(str(disk), str(tmp_path / "missing.json")) == expected


def test_smart_file_wins_over_emmc_and_emmc_fills_in_when_absent(roots, tmp_path) -> None:
    sys_root, _ = roots
    disk = add_disk(sys_root, "mmcblk0", "179:0", parent=MMC)
    _write(disk / "device" / "life_time", "0x04 0x02\n")
    assert hwinfo.wear_pct(str(disk), _wear_file(tmp_path, {"devices": {"mmcblk0": {"wear_pct": 7}}})) == 7.0
    assert hwinfo.wear_pct(str(disk), _wear_file(tmp_path, {"devices": {}})) == 40.0
