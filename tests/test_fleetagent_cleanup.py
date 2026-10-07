"""cleanup.py: old fleet images, prunes, exited containers and the low-disk guard."""

from __future__ import annotations

from fleetagent import cleanup
from tests.test_fleetagent_fakes import FakeDocker

D1, D2, D3 = ("sha256:" + c * 64 for c in "123")


def _cleaner(fake: FakeDocker, **kw) -> cleanup.Cleaner:
    return cleanup.Cleaner(fake.docker(), **kw)


def _repos(fake: FakeDocker) -> set[str]:
    return {f"{i['Repository']}:{i['Tag']}" for i in fake.images}


def test_is_fleet_repo() -> None:
    for yes in ("fleet/hello", "host.ts.net:5000/fleet/hello", "localhost:5000/fleet/polymarket"):
        assert cleanup.is_fleet_repo(yes)
    for no in ("python", "fleetx/hello", "myfleet/hello", "library/fleet", "registry", "reg/notfleet/x"):
        assert not cleanup.is_fleet_repo(no)


def test_keeps_the_digests_the_host_names_and_removes_other_fleet_images() -> None:
    fake = FakeDocker()
    fake.add_image("h/fleet/hello", digest=D1)
    fake.add_image("h/fleet/hello-old", digest=D2, size="40MB")
    fake.add_image("h/fleet/polymarket", digest=D3, size="60MB")
    fake.add_image("python", "3.13-slim", digest="sha256:" + "9" * 64)
    c = _cleaner(fake)
    c.run([D1, D3])
    assert _repos(fake) == {"h/fleet/hello:<none>", "h/fleet/polymarket:<none>", "python:3.13-slim"}
    assert (c.report.images_removed, c.report.bytes_freed) == (1, 40_000_000 + 1_500_000 + 2_500)


def test_an_image_id_in_keep_also_protects_and_in_use_images_survive() -> None:
    fake = FakeDocker()
    keep_id = fake.add_image("h/fleet/a", digest=D1)
    fake.add_image("h/fleet/b", digest=D2)
    fake.add_image("h/fleet/c", digest=D3)
    docker = fake.docker()
    docker.run(["--name", "x", "--label", "fleet.workload=w", "--label", "fleet.epoch=1", "h/fleet/c@" + D3])
    c = _cleaner(fake, prune_images=False, prune_builder=False)
    c.run([keep_id])
    assert _repos(fake) == {"h/fleet/a:<none>", "h/fleet/c:<none>"}  # b removed; c is in use so rmi refused
    assert c.report.images_removed == 1


def test_protect_argument_keeps_images_of_live_containers() -> None:
    fake = FakeDocker()
    fake.add_image("h/fleet/a", digest=D1)
    c = _cleaner(fake)
    c.run([], protect=[D1])
    assert len(fake.images) == 1


def test_without_a_keep_list_no_image_is_removed_but_prunes_still_run() -> None:
    fake = FakeDocker()
    fake.add_image("h/fleet/a", digest=D1)
    c = _cleaner(fake)
    c.run(None)
    assert len(fake.images) == 1 and fake.prune_calls.count("image") == 1 and fake.prune_calls.count("builder") == 1


def test_the_same_image_under_two_tags_counts_once() -> None:
    fake = FakeDocker()
    iid = fake.add_image("h/fleet/a", "1", digest=D1, image_id="sha256:" + "7" * 64)
    fake.add_image("h/fleet/a", "2", digest=D1, image_id=iid)
    c = _cleaner(fake, prune_images=False, prune_builder=False)
    c.run([])
    assert fake.images == [] and c.report.images_removed == 1


def test_exited_fleet_containers_are_removed_but_not_the_desired_one() -> None:
    fake = FakeDocker()
    docker = fake.docker()
    gone = docker.run(["--name", "a", "--label", "fleet.workload=w", "--label", "fleet.epoch=1", "img"])
    mine = docker.run(["--name", "b", "--label", "fleet.workload=w", "--label", "fleet.epoch=2", "img"])
    live = docker.run(["--name", "c", "--label", "fleet.workload=v", "--label", "fleet.epoch=1", "img"])
    other = docker.run(["--name", "d", "img"])  # not a fleet container
    for cid in (gone, mine, other):
        fake.exit(cid, 0)
    _cleaner(fake).run(None, desired=("w", 2))
    assert fake.container(gone) is None and fake.container(mine) and fake.container(live) and fake.container(other)


def test_hourly_schedule() -> None:
    clock = [0.0]
    c = _cleaner(FakeDocker(), clock=lambda: clock[0])
    assert c.due()
    c.run([])
    assert not c.due()
    clock[0] = 3599.0
    assert not c.due()
    clock[0] = 3600.0
    assert c.due()


def test_threshold_is_the_larger_of_1024_mb_and_ten_percent() -> None:
    assert cleanup.low_disk_threshold_mb(None) == 1024 and cleanup.low_disk_threshold_mb(8000) == 1024
    assert cleanup.low_disk_threshold_mb(100_000) == 10_000
    assert cleanup.is_low(1023, 15_000) and not cleanup.is_low(1500, 15_000) and cleanup.is_low(1500, 30_000)
    assert not cleanup.is_low(None, 1000)


def test_guard_prunes_first_and_reports_low_disk_only_when_still_low() -> None:
    fake = FakeDocker()
    fake.add_image("h/fleet/old", digest=D2)
    free = [500]
    c = _cleaner(fake)
    assert c.guard(lambda: (free[0], 16_000), [D1]) is False
    assert c.report.low_disk is True and fake.prune_calls and fake.images == []
    assert c.guard(lambda: (5000, 16_000), [D1]) is True and c.report.low_disk is False
    # pruning that frees enough lets the pull go ahead
    calls = iter([(500, 16_000), (3000, 16_000)])
    assert _cleaner(fake).guard(lambda: next(calls), [D1]) is True


def test_acknowledge_subtracts_only_what_was_sent() -> None:
    c = _cleaner(FakeDocker())
    c.report.images_removed, c.report.bytes_freed = 2, 100
    sent = c.snapshot()
    c.report.images_removed, c.report.bytes_freed = 3, 150  # more work done meanwhile
    c.acknowledge(sent)
    assert (c.report.images_removed, c.report.bytes_freed) == (1, 50)
