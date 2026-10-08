"""The systemd units and the root wear script that deploy/install_worker.sh writes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import types
from pathlib import Path

import pytest

from fleet.common import hwinfo

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "deploy" / "install_worker.sh"
WORKER_UNIT = REPO / "deploy" / "fleet-worker.service"


def _installer() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _units() -> dict[str, str]:
    """Every unit the installer writes, by file name."""
    units = {}
    for match in re.finditer(r"cat > (\S+) <<'UNITEOF'\n(.*?)UNITEOF\n", _installer(), re.S):
        target = match.group(1).strip('"')
        units["fleet-worker.service" if target == "$UNIT" else os.path.basename(target)] = match.group(2)
    return units


def _service_lines(text: str) -> list[str]:
    section = text.split("[Service]\n", 1)[1].split("\n[", 1)[0]
    return [line for line in section.splitlines() if line and not line.startswith("#")]


def _wear_module(tmp_path: Path) -> types.ModuleType:
    match = re.search(r"# BEGIN fleet-wear\n(.*?)# END fleet-wear\n", _installer(), re.S)
    assert match, "installer must carry the wear script between the markers"
    module = types.ModuleType("fleet_wear")
    module.__file__ = str(tmp_path / "wear.py")
    exec(compile(match.group(1), module.__file__, "exec"), module.__dict__)
    return module


# --------------------------------------------------------------------- units


def test_installer_parses() -> None:
    assert subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True).returncode == 0


def test_installer_writes_the_repo_worker_unit() -> None:
    installed = _units()["fleet-worker.service"]
    assert installed == WORKER_UNIT.read_text(encoding="utf-8")
    lines = _service_lines(installed)
    for line in ("Environment=FLEET_RUN_DIR=/run/fleet", "Environment=FLEET_REBOOT_TRIGGER=/run/fleet/reboot",
                 "RuntimeDirectory=fleet", "RuntimeDirectoryMode=0750", "RuntimeDirectoryPreserve=yes", "ProtectSystem=strict"):
        assert line in lines


def test_reboot_path_watches_the_trigger_the_worker_writes() -> None:
    units = _units()
    env = dict(line.split("=", 2)[1:] for line in _service_lines(units["fleet-worker.service"]) if line.startswith("Environment="))
    assert env["FLEET_REBOOT_TRIGGER"] == "/run/fleet/reboot"
    assert os.path.dirname(env["FLEET_REBOOT_TRIGGER"]) == env["FLEET_RUN_DIR"] == "/run/fleet"
    path_unit = units["fleet-reboot.path"]
    assert "PathExists=/run/fleet/reboot\n" in path_unit and "Unit=fleet-reboot.service\n" in path_unit
    service = _service_lines(units["fleet-reboot.service"])
    assert "Type=oneshot" in service and "RemainAfterExit=yes" in service, "one reboot per trigger, no re-trigger loop"
    assert any(line.startswith("ExecStart=") and line.endswith("systemctl reboot") for line in service)
    assert "[Install]" not in units["fleet-reboot.service"], "only the path unit starts it"
    text = _installer()
    assert text.index("mv -fT /run/fleet/reboot") < text.index("systemctl enable --now fleet-reboot.path"), \
        "a leftover trigger is moved aside before the path unit (re)starts"


def test_wear_units() -> None:
    units = _units()
    service = _service_lines(units["fleet-wear.service"])
    assert "ExecStart=/usr/bin/python3 /usr/local/lib/fleet/wear.py" in service
    assert "RuntimeDirectory=fleet-wear" in service and "RuntimeDirectoryPreserve=yes" in service
    assert os.path.dirname(hwinfo.WEAR_FILE) == "/run/fleet-wear"
    assert "OnUnitActiveSec=1h" in units["fleet-wear.timer"]


# ---------------------------------------------------------------- wear_from


NVME = {"device": {"type": "nvme"}, "nvme_smart_health_information_log": {"percentage_used": 3, "data_units_written": 123}}
ATA_STATS = {
    "rotation_rate": 0,
    "ata_device_statistics": {"pages": [
        {"number": 1, "table": [{"name": "Lifetime Power-On Resets", "value": 50}]},
        {"number": 7, "table": [{"name": "Percentage Used Endurance Indicator", "value": 8}]},
    ]},
    "ata_smart_attributes": {"table": [{"id": 177, "name": "Wear_Leveling_Count", "value": 99}]},
}


def _ata(attr_id: int, value: int, rotation: int = 0) -> dict:
    return {"rotation_rate": rotation, "ata_smart_attributes": {"table": [
        {"id": 9, "name": "Power_On_Hours", "value": 97},
        {"id": attr_id, "name": "x", "value": value},
    ]}}


HDD = {"rotation_rate": 7200, "ata_smart_attributes": {"table": [
    {"id": 1, "value": 200}, {"id": 9, "value": 50}, {"id": 177, "value": 100}, {"id": 202, "value": 100},
]}}


@pytest.mark.parametrize("data,expected", [
    (NVME, 3.0),
    ({"nvme_smart_health_information_log": {"percentage_used": 120}}, 120.0),
    (ATA_STATS, 8.0),
    (_ata(231, 93), 7.0),
    (_ata(177, 99), 1.0),
    (_ata(202, 80), 20.0),
    (_ata(233, 100), 0.0),
    (_ata(202, 200), None),
    (HDD, None),
    ({"smartctl": {"exit_status": 2}}, None),
    ({}, None),
    ([], None),
    ({"nvme_smart_health_information_log": {"percentage_used": True}}, None),
])
def test_wear_from(tmp_path: Path, data, expected) -> None:
    assert _wear_module(tmp_path).wear_from(data) == expected


def test_wear_main_writes_the_file_hwinfo_reads(tmp_path: Path, monkeypatch) -> None:
    module = _wear_module(tmp_path)
    sys_block = tmp_path / "sys-block"
    for dev in ("loop0", "nvme0n1", "sda", "sdb", "sr0", "dm-0"):
        (sys_block / dev).mkdir(parents=True)
    outputs = {"/dev/nvme0n1": json.dumps(NVME), "/dev/sda": json.dumps(HDD), "/dev/sdb": "not json"}
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=outputs[cmd[-1]], stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    out = tmp_path / "fleet-wear" / "wear.json"
    module.main(str(out), str(sys_block))
    assert [c[-1] for c in calls] == ["/dev/nvme0n1", "/dev/sda", "/dev/sdb"]
    assert all("standby" in c for c in calls), "never spin up a sleeping disk"
    assert json.loads(out.read_text())["devices"] == {"nvme0n1": {"wear_pct": 3.0}}
    assert oct(out.stat().st_mode & 0o777) == "0o644"
    assert sorted(os.listdir(out.parent)) == ["wear.json"]
    assert hwinfo.wear_pct("/sys/devices/x/block/nvme0n1", str(out)) == 3.0


def test_wear_main_without_smartctl_writes_an_empty_file(tmp_path: Path, monkeypatch) -> None:
    module = _wear_module(tmp_path)
    (tmp_path / "sys-block" / "sda").mkdir(parents=True)

    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(module.subprocess, "run", missing)
    out = tmp_path / "wear.json"
    module.main(str(out), str(tmp_path / "sys-block"))
    assert json.loads(out.read_text())["devices"] == {}
