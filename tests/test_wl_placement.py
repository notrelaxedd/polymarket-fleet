"""Placement guardrail (docs/workloads-design.md section 4.1): which workload may run on which machine.

`check_placement` and `effective_disk_type` are pure; the last section goes through the owner API
(`POST /api/machines/{id}/assign`) and proves a refused placement changes nothing.

Assumptions where the contract is silent:
- An override of "unknown" counts as set (the override wins whenever it is not NULL).
- The refusal message of a RAM refusal names both numbers; a disk refusal names the free MB.
- The API 422 `detail` is the Unplaceable text, which lists every code.
"""
from __future__ import annotations

import pytest

from host.workloads.manifest import Manifest
from host.workloads.placement import DISK_TYPES, Refusal, check_placement, effective_disk_type
from tests.wl_helpers import (
    archive_manifest, assignment_of, audit_count, audit_for, demo_site_manifest, hello_manifest,
    insert_machine, insert_workload, load_real_manifest, machine_dict, machine_row, make_manifest,
    polymarket_manifest,
)

ALL_CODES = [
    "workload_disabled", "image_not_published", "docker_missing", "ram_too_small", "disk_too_small",
    "write_heavy_on_flash",
]


def codes(manifest: Manifest, machine: dict, **kw) -> list[str]:
    return [r.code for r in check_placement(manifest, machine, **kw)]


# ------------------------------------------------------------------ effective_disk_type


def test_disk_types_constant():
    assert tuple(DISK_TYPES) == ("ssd", "hdd", "flash", "unknown")


@pytest.mark.parametrize(
    "detected, override, expected",
    [
        ("flash", None, "flash"),
        ("ssd", None, "ssd"),
        ("hdd", None, "hdd"),
        ("unknown", None, "unknown"),
        (None, None, "unknown"),
        ("flash", "ssd", "ssd"),
        ("ssd", "flash", "flash"),
        ("unknown", "ssd", "ssd"),
        ("ssd", "unknown", "unknown"),
        ("flash", "hdd", "hdd"),
    ],
)
def test_effective_disk_type_override_wins(detected, override, expected):
    m = machine_dict(disk_type_detected=detected, disk_type_override=override)
    assert effective_disk_type(m) == expected


def test_effective_disk_type_missing_keys_are_unknown():
    assert effective_disk_type({}) == "unknown"
    assert effective_disk_type({"disk_type_override": None}) == "unknown"


# ------------------------------------------------------------------ result shape


def test_a_fitting_workload_returns_an_empty_list():
    result = check_placement(hello_manifest(), machine_dict(), image_size_mb=60)
    assert result == []
    assert isinstance(result, list)


def test_refusals_are_refusal_objects_with_a_code_and_a_message():
    result = check_placement(demo_site_manifest(), machine_dict())
    assert result and all(isinstance(r, Refusal) for r in result)
    for r in result:
        assert r.code in ALL_CODES
        assert isinstance(r.message, str) and len(r.message) > 8


def test_check_placement_does_not_modify_the_machine_row():
    m = machine_dict(disk_type_detected=None, ram_total_mb=None)
    before = dict(m)
    check_placement(archive_manifest(), m, image_size_mb=50)
    assert m == before


# ------------------------------------------------------------------ every code on its own


def test_workload_disabled():
    assert codes(hello_manifest(), machine_dict(), workload_enabled=False) == ["workload_disabled"]
    assert codes(hello_manifest(), machine_dict(), workload_enabled=True) == []


def test_image_not_published():
    assert codes(hello_manifest(), machine_dict(), image_published=False) == ["image_not_published"]


def test_docker_missing():
    assert codes(hello_manifest(), machine_dict(docker_ok=False)) == ["docker_missing"]


def test_ram_too_small_and_the_message_is_concrete():
    m = make_manifest("big", ram=8192)
    result = check_placement(m, machine_dict(ram_total_mb=3800))
    assert [r.code for r in result] == ["ram_too_small"]
    assert "8192" in result[0].message and "3800" in result[0].message


def test_disk_too_small():
    m = make_manifest("bulky", disk=5000)
    result = check_placement(m, machine_dict(disk_free_mb=4000))
    assert [r.code for r in result] == ["disk_too_small"]
    assert "4000" in result[0].message


def test_write_heavy_on_flash():
    assert codes(archive_manifest(), machine_dict(disk_type_detected="flash", ram_total_mb=8000)) == ["write_heavy_on_flash"]


# ------------------------------------------------------------------ order, and "all that apply"


def test_every_refusal_is_reported_in_the_documented_order():
    m = make_manifest("everything", ram=8192, disk=20000, write_heavy=True)
    machine = machine_dict(docker_ok=False, ram_total_mb=1000, disk_free_mb=500, disk_type_detected="flash")
    assert codes(m, machine, image_size_mb=100, image_published=False, workload_enabled=False) == ALL_CODES


@pytest.mark.parametrize(
    "kw, machine_kw, manifest_kw, expected",
    [
        ({}, {"ram_total_mb": 100}, {"ram": 8192, "write_heavy": True}, ["ram_too_small", "write_heavy_on_flash"]),
        ({}, {"ram_total_mb": 100, "disk_free_mb": 10}, {"ram": 8192, "disk": 5000}, ["ram_too_small", "disk_too_small"]),
        ({}, {"docker_ok": False, "disk_free_mb": 10}, {"disk": 5000}, ["docker_missing", "disk_too_small"]),
        ({"image_published": False}, {"docker_ok": False}, {}, ["image_not_published", "docker_missing"]),
        ({"workload_enabled": False}, {"disk_free_mb": 10}, {"disk": 5000}, ["workload_disabled", "disk_too_small"]),
        ({}, {"disk_free_mb": 10}, {"disk": 5000, "write_heavy": True}, ["disk_too_small", "write_heavy_on_flash"]),
    ],
)
def test_pairs_keep_the_documented_order(kw, machine_kw, manifest_kw, expected):
    assert codes(make_manifest("pair", **manifest_kw), machine_dict(**machine_kw), **kw) == expected


def test_no_duplicate_codes():
    m = make_manifest("everything", ram=8192, disk=20000, write_heavy=True)
    result = codes(m, machine_dict(ram_total_mb=None, disk_free_mb=None, docker_ok=False, disk_type_detected=None))
    assert len(result) == len(set(result))


# ------------------------------------------------------------------ boundaries


def test_ram_exactly_the_minimum_fits_and_one_mb_less_refuses():
    m = make_manifest("edge", ram=3000)
    assert codes(m, machine_dict(ram_total_mb=3000)) == []
    assert codes(m, machine_dict(ram_total_mb=2999)) == ["ram_too_small"]
    assert codes(m, machine_dict(ram_total_mb=3001)) == []


def test_disk_exactly_min_plus_image_fits_and_one_mb_less_refuses():
    m = make_manifest("edge", disk=2048)
    assert codes(m, machine_dict(disk_free_mb=2048 + 400), image_size_mb=400) == []
    assert codes(m, machine_dict(disk_free_mb=2048 + 400 - 1), image_size_mb=400) == ["disk_too_small"]
    assert codes(m, machine_dict(disk_free_mb=2048 + 400 + 1), image_size_mb=400) == []


def test_unknown_image_size_counts_as_zero():
    m = make_manifest("edge", disk=2048)
    assert codes(m, machine_dict(disk_free_mb=2048), image_size_mb=None) == []
    assert codes(m, machine_dict(disk_free_mb=2047), image_size_mb=None) == ["disk_too_small"]
    assert codes(m, machine_dict(disk_free_mb=2048), image_size_mb=0) == []
    assert codes(m, machine_dict(disk_free_mb=2048), image_size_mb=1) == ["disk_too_small"]


def test_the_image_size_is_added_not_ignored():
    m = make_manifest("edge", disk=0)
    assert codes(m, machine_dict(disk_free_mb=500), image_size_mb=501) == ["disk_too_small"]
    assert codes(m, machine_dict(disk_free_mb=500), image_size_mb=500) == []


def test_big_numbers_do_not_overflow():
    m = make_manifest("edge", disk=10_485_760)
    assert codes(m, machine_dict(disk_free_mb=10_485_760 + 10**9), image_size_mb=10**9) == []
    assert codes(m, machine_dict(disk_free_mb=10_485_760 + 10**9 - 1), image_size_mb=10**9) == ["disk_too_small"]


# ------------------------------------------------------------------ NULL specs refuse


def test_null_ram_refuses_even_for_the_smallest_workload():
    assert codes(make_manifest("tiny", ram=16), machine_dict(ram_total_mb=None)) == ["ram_too_small"]


def test_null_free_disk_refuses_even_when_nothing_is_needed():
    m = make_manifest("tiny", disk=0)
    assert codes(m, machine_dict(disk_free_mb=None), image_size_mb=None) == ["disk_too_small"]
    assert codes(m, machine_dict(disk_free_mb=None), image_size_mb=0) == ["disk_too_small"]


def test_a_machine_that_never_reported_specs_is_refused_by_both_checks():
    bare = machine_dict(ram_total_mb=None, disk_free_mb=None, docker_ok=False, disk_type_detected="unknown")
    assert codes(hello_manifest(), bare, image_size_mb=60) == ["docker_missing", "ram_too_small", "disk_too_small"]


def test_missing_keys_behave_like_null():
    assert codes(hello_manifest(), {"docker_ok": True}) == ["ram_too_small", "disk_too_small"]


def test_null_disk_size_does_not_matter_only_free_space_does():
    assert codes(hello_manifest(), machine_dict(disk_size_mb=None), image_size_mb=60) == []


# ------------------------------------------------------------------ write_heavy and disk type


def archive_on(**machine_kw) -> list[str]:
    return codes(archive_manifest(), machine_dict(ram_total_mb=8000, disk_free_mb=50000, **machine_kw))


def test_write_heavy_is_refused_on_flash_and_on_unknown():
    assert archive_on(disk_type_detected="flash") == ["write_heavy_on_flash"]
    assert archive_on(disk_type_detected="unknown") == ["write_heavy_on_flash"]
    assert archive_on(disk_type_detected=None) == ["write_heavy_on_flash"]


def test_write_heavy_is_allowed_on_ssd_and_hdd():
    assert archive_on(disk_type_detected="ssd") == []
    assert archive_on(disk_type_detected="hdd") == []


def test_override_that_corrects_flash_to_ssd_lets_a_write_heavy_workload_in():
    assert archive_on(disk_type_detected="flash", disk_type_override="ssd") == []
    assert archive_on(disk_type_detected="unknown", disk_type_override="ssd") == []
    assert archive_on(disk_type_detected="flash", disk_type_override="hdd") == []


def test_override_that_marks_an_ssd_as_flash_refuses_a_write_heavy_workload():
    assert archive_on(disk_type_detected="ssd", disk_type_override="flash") == ["write_heavy_on_flash"]
    assert archive_on(disk_type_detected="hdd", disk_type_override="unknown") == ["write_heavy_on_flash"]


def test_a_workload_that_is_not_write_heavy_ignores_the_disk_type():
    for detected in ("flash", "unknown", "ssd", "hdd", None):
        assert codes(hello_manifest(), machine_dict(disk_type_detected=detected), image_size_mb=60) == []
    assert codes(hello_manifest(), machine_dict(disk_type_detected="ssd", disk_type_override="flash"), image_size_mb=60) == []


# ------------------------------------------------------------------ the realistic fleet box


FLEET_BOX = dict(ram_total_mb=3800, disk_free_mb=9000, disk_size_mb=15000, disk_type_detected="flash")


def test_the_4gb_flash_box_takes_hello_and_polymarket():
    assert codes(hello_manifest(), machine_dict(**FLEET_BOX), image_size_mb=60) == []
    assert codes(polymarket_manifest(), machine_dict(**FLEET_BOX), image_size_mb=150) == []


def test_the_4gb_flash_box_refuses_a_write_heavy_2gb_archive():
    result = check_placement(archive_manifest(), machine_dict(**FLEET_BOX), image_size_mb=200)
    assert [r.code for r in result] == ["write_heavy_on_flash"]


def test_the_4gb_flash_box_refuses_an_8gb_demo_site():
    result = check_placement(demo_site_manifest(), machine_dict(**FLEET_BOX), image_size_mb=1200)
    assert [r.code for r in result] == ["ram_too_small"]
    assert "8192" in result[0].message


def test_a_3gb_box_just_below_polymarket_is_refused():
    assert codes(polymarket_manifest(), machine_dict(**{**FLEET_BOX, "ram_total_mb": 2999}), image_size_mb=150) == ["ram_too_small"]


def test_the_real_workload_folders_on_the_realistic_box():
    """Runs against workloads/<name>/workload.toml when the folders exist (they do after the merge)."""
    hello = load_real_manifest("hello")
    polymarket = load_real_manifest("polymarket")
    box = machine_dict(**FLEET_BOX)
    assert codes(hello, box, image_size_mb=100) == []
    assert polymarket.resources.min_ram_mb == 3000 and polymarket.resources.min_disk_mb == 2048
    assert not polymarket.resources.write_heavy
    assert codes(polymarket, box, image_size_mb=300) == []
    assert codes(polymarket, machine_dict(**{**FLEET_BOX, "ram_total_mb": 2999}), image_size_mb=300) == ["ram_too_small"]


def test_a_database_row_works_as_the_machine_argument(conn):
    """The machine argument is a `machines` row dict: bigint columns, NULLs and all."""
    m = insert_machine(conn, "rowbox")
    row = machine_row(conn, m.id)
    assert codes(hello_manifest(), row, image_size_mb=60) == []
    assert codes(demo_site_manifest(), row, image_size_mb=1200) == ["ram_too_small"]
    assert effective_disk_type(row) == "flash"
    nulls = machine_row(conn, insert_machine(conn, "nullbox", ram_total_mb=None, disk_free_mb=None, docker_ok=False,
                                             disk_type="unknown").id)
    assert codes(hello_manifest(), nulls) == ["docker_missing", "ram_too_small", "disk_too_small"]


# ------------------------------------------------------------------ the owner API


@pytest.fixture
def fleet(conn):
    """The published workloads and one realistic 4 GB flash box assigned nothing."""
    insert_workload(conn, hello_manifest(), size_mb=60)
    insert_workload(conn, polymarket_manifest(), size_mb=150)
    insert_workload(conn, archive_manifest(), size_mb=200)
    insert_workload(conn, demo_site_manifest(), size_mb=1200)
    return insert_machine(conn, "fleetbox", ram_total_mb=3800, disk_free_mb=9000, disk_size_mb=15000, disk_type="flash")


def assign(client, machine, workload, **kw):
    return client.post(f"/api/machines/{machine.id}/assign", json={"workload": workload}, **kw)


def test_api_assign_refusal_is_422_lists_every_code_and_changes_nothing(client, conn, fleet):
    before = assignment_of(conn, fleet.id)
    audits = audit_count(conn)
    r = assign(client, fleet, "demo-site")
    assert r.status_code == 422, r.text
    assert "ram_too_small" in r.json()["detail"]
    r = assign(client, fleet, "archive")
    assert r.status_code == 422 and "write_heavy_on_flash" in r.json()["detail"]
    after = assignment_of(conn, fleet.id)
    assert (after["workload"], after["epoch"], after["state"], after["run_token_hash"]) == (
        before["workload"], before["epoch"], before["state"], before["run_token_hash"])
    assert audit_count(conn) == audits
    assert audit_for(conn, action="workload_assign") == []


def test_api_assign_lists_all_codes_at_once(client, conn):
    insert_workload(conn, make_manifest("giant", ram=8192, disk=60000, write_heavy=True), size_mb=500)
    m = insert_machine(conn, "poorbox", ram_total_mb=1000, disk_free_mb=2000, docker_ok=False, disk_type="flash")
    r = assign(client, m, "giant")
    assert r.status_code == 422
    detail = r.json()["detail"]
    for code in ("docker_missing", "ram_too_small", "disk_too_small", "write_heavy_on_flash"):
        assert code in detail, detail
    assert detail.index("docker_missing") < detail.index("ram_too_small") < detail.index("disk_too_small") < detail.index("write_heavy_on_flash")
    assert assignment_of(conn, m.id)["epoch"] == 1


def test_api_assign_a_fitting_workload_moves_the_epoch_and_audits(client, conn, fleet):
    r = assign(client, fleet, "hello")
    assert r.status_code == 200, r.text
    a = assignment_of(conn, fleet.id)
    assert (a["workload"], a["epoch"], a["state"]) == ("hello", 2, "pending")
    rows = audit_for(conn, action="workload_assign", entity=fleet.id)
    assert len(rows) == 1
    assert assign(client, fleet, "polymarket").status_code == 200
    assert assignment_of(conn, fleet.id)["epoch"] == 3


def test_api_assign_checks_the_image_size_from_the_workload_row_at_the_boundary(client, conn):
    """free disk == min_disk + image size fits, one MB less is refused (the API must pass the stored size)."""
    insert_workload(conn, make_manifest("sized", disk=2048), size_mb=1000)
    fits = insert_machine(conn, "exact", disk_free_mb=3048)
    short = insert_machine(conn, "short", disk_free_mb=3047)
    assert assign(client, short, "sized").status_code == 422
    assert "disk_too_small" in assign(client, short, "sized").json()["detail"]
    assert assignment_of(conn, short.id)["workload"] is None
    assert assign(client, fits, "sized").status_code == 200


def test_api_assign_refuses_an_unpublished_and_a_disabled_workload(client, conn):
    insert_workload(conn, make_manifest("unpub"), published=False)
    insert_workload(conn, make_manifest("off"), enabled=False)
    m = insert_machine(conn, "box")
    r = assign(client, m, "unpub")
    assert r.status_code == 422 and "image_not_published" in r.json()["detail"]
    r = assign(client, m, "off")
    assert r.status_code == 422 and "workload_disabled" in r.json()["detail"]
    assert assignment_of(conn, m.id)["workload"] is None and assignment_of(conn, m.id)["epoch"] == 1


def test_api_assign_refuses_a_machine_without_docker(client, conn):
    insert_workload(conn, make_manifest("tiny"))
    m = insert_machine(conn, "nodocker", docker_ok=False)
    r = assign(client, m, "tiny")
    assert r.status_code == 422 and "docker_missing" in r.json()["detail"]


def test_api_assign_a_machine_that_never_reported_specs_is_refused(client, conn):
    insert_workload(conn, make_manifest("tiny"))
    m = insert_machine(conn, "silent", ram_total_mb=None, disk_free_mb=None, docker_ok=False)
    r = assign(client, m, "tiny")
    assert r.status_code == 422
    assert "ram_too_small" in r.json()["detail"] and "disk_too_small" in r.json()["detail"]


def test_api_unknown_machine_and_unknown_workload_are_404(client, conn, fleet):
    assert client.post("/api/machines/m_nope00/assign", json={"workload": "hello"}).status_code == 404
    assert assign(client, fleet, "no-such-workload").status_code == 404
    assert assignment_of(conn, fleet.id)["epoch"] == 1


def test_api_assigning_nothing_is_not_subject_to_placement(client, conn):
    """workload null stops the machine; a box that fits nothing can still be emptied."""
    insert_workload(conn, make_manifest("tiny"))
    m = insert_machine(conn, "weak", ram_total_mb=None, disk_free_mb=None, docker_ok=False, workload="tiny", epoch=3, state="running")
    r = assign(client, m, None)
    assert r.status_code == 200, r.text
    a = assignment_of(conn, m.id)
    assert (a["workload"], a["epoch"], a["state"]) == (None, 4, "stopped")


def test_api_disk_type_override_changes_placement_both_ways(client, conn, fleet):
    assert assign(client, fleet, "archive").status_code == 422
    r = client.post(f"/api/machines/{fleet.id}/disk-type", json={"disk_type": "ssd"})
    assert r.status_code == 200, r.text
    assert machine_row(conn, fleet.id)["disk_type_override"] == "ssd"
    assert machine_row(conn, fleet.id)["disk_type_detected"] == "flash", "the override never replaces the detected value"
    assert assign(client, fleet, "archive").status_code == 200
    # Marking it flash again refuses a re-assignment of a write-heavy workload on another box.
    other = insert_machine(conn, "ssdbox", disk_type="ssd")
    assert assign(client, other, "archive").status_code == 200
    r = client.post(f"/api/machines/{other.id}/disk-type", json={"disk_type": "flash"})
    assert r.status_code == 200
    third = insert_machine(conn, "ssdbox2", disk_type="ssd", disk_override="flash")
    r = assign(client, third, "archive")
    assert r.status_code == 422 and "write_heavy_on_flash" in r.json()["detail"]
    # NULL clears the override and the detected type applies again.
    assert client.post(f"/api/machines/{third.id}/disk-type", json={"disk_type": None}).status_code == 200
    assert machine_row(conn, third.id)["disk_type_override"] is None
    assert assign(client, third, "archive").status_code == 200


def test_api_disk_type_rejects_garbage(client, conn, fleet):
    for bad in ("nvme", "", 7, ["flash"]):
        r = client.post(f"/api/machines/{fleet.id}/disk-type", json={"disk_type": bad})
        assert r.status_code == 400, (bad, r.text)
    assert machine_row(conn, fleet.id)["disk_type_override"] is None


def test_api_machine_list_shows_the_refusals_per_workload(client, conn, fleet):
    r = client.get("/api/machines")
    assert r.status_code == 200
    text = r.text
    # The fleet box is flash with 3800 MB: demo-site is refused for RAM, archive for write-heavy.
    assert "ram_too_small" in text and "write_heavy_on_flash" in text
    assert "flash" in text
    assert fleet.id in text
