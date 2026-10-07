"""secretfiles.py and logship.py unit tests."""

from __future__ import annotations

import json
import os
import stat

import pytest

from fleetagent import logship, secretfiles
from tests.test_fleetagent_fakes import FakeDocker


def _cid(fake: FakeDocker, name: str = "c1") -> str:
    return fake.docker().run(["--name", name, "--label", "fleet.workload=w", "--label", "fleet.epoch=1", "img"])


# ---------------------------------------------------------------- secretfiles


def test_secret_files_as_root_are_0400_owned_by_the_uid_and_the_dir_is_closed(tmp_path) -> None:
    chowns: list[tuple] = []
    d = secretfiles.write_secrets(str(tmp_path), "hello", {"A": "1", "B_2": "two\nlines"}, 10001, root=True, chown=lambda *a: chowns.append(a))
    assert d == str(tmp_path / "secrets" / "hello")
    for name, value in (("A", "1"), ("B_2", "two\nlines")):
        path = os.path.join(d, name)
        assert open(path).read() == value and stat.S_IMODE(os.stat(path).st_mode) == 0o400
        assert (path, 10001, 10001) in chowns
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o500 and (d, 10001, 10001) in chowns
    assert secretfiles.container_group(True) is None


def test_secret_files_without_root_are_group_readable_and_need_group_add(tmp_path) -> None:
    d = secretfiles.write_secrets(str(tmp_path), "hello", {"A": "1"}, 10001, root=False, chown=lambda *a: pytest.fail("no chown"))
    assert stat.S_IMODE(os.stat(os.path.join(d, "A")).st_mode) == 0o440
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o750
    assert secretfiles.container_group(False) == os.getegid()


def test_rewriting_replaces_the_files_and_remove_deletes_everything(tmp_path) -> None:
    secretfiles.write_secrets(str(tmp_path), "w", {"OLD": "x"}, 1, root=False)
    d = secretfiles.write_secrets(str(tmp_path), "w", {"NEW": "y"}, 1, root=True, chown=lambda *a: None)
    assert os.listdir(d) == ["NEW"]
    assert secretfiles.read_values(str(tmp_path), "w") == {"NEW": "y"}
    assert secretfiles.workloads_with_secrets(str(tmp_path)) == ["w"]
    secretfiles.remove_secrets(str(tmp_path), "w")  # works on a 0500 directory too
    assert not os.path.exists(d) and secretfiles.read_values(str(tmp_path), "w") == {}
    secretfiles.remove_secrets(str(tmp_path), "w")  # a no-op the second time


@pytest.mark.parametrize("bad", ["../evil", "a/b", "", "1ABC", "A B", "A" * 65, "a.b"])
def test_secret_names_cannot_escape_the_directory(tmp_path, bad) -> None:
    with pytest.raises(ValueError):
        secretfiles.write_secrets(str(tmp_path), "w", {bad: "x"}, 1, root=False)
    assert not (tmp_path / "evil").exists()


def test_workload_names_are_checked_too(tmp_path) -> None:
    with pytest.raises(ValueError):
        secretfiles.secrets_dir(str(tmp_path), "../x")


# --------------------------------------------------------------------- redact


def test_redact_replaces_every_value_longest_first_and_skips_empty() -> None:
    assert logship.redact("a secret-token-xyz and secret", ["secret", "secret-token-xyz", ""]) == "a [redacted] and [redacted]"
    assert logship.redact("nothing here", ["zzz"]) == "nothing here"
    assert logship.redact("line one of key and ---END---", ["line one of key\n---END---"]) == "[redacted] and [redacted]"
    assert logship.redact("tok tok", ["tok"]) == "[redacted] [redacted]"


# ------------------------------------------------------------------- shipping


def test_collect_commit_cursor_and_no_repeats() -> None:
    fake = FakeDocker()
    cid = _cid(fake)
    ship = logship.LogShipper(fake.docker())
    for i in range(5):
        fake.emit(cid, "stdout", f"l{i}")
    batch = ship.collect([(cid, "w")])
    assert [e["line"] for e in batch.entries] == [f"l{i}" for i in range(5)]
    assert [e["line"] for e in ship.collect([(cid, "w")]).entries] == [f"l{i}" for i in range(5)]  # not committed yet
    batch.commit()
    assert ship.collect([(cid, "w")]).entries == []
    fake.emit(cid, "stderr", "late")
    assert [(e["stream"], e["line"]) for e in ship.collect([(cid, "w")]).entries] == [("stderr", "late")]


def test_lines_sharing_a_timestamp_across_a_cap_boundary_are_neither_lost_nor_repeated() -> None:
    fake = FakeDocker()
    cid = _cid(fake)
    ship = logship.LogShipper(fake.docker(), max_lines=3)
    for i in range(7):
        fake.emit(cid, "stdout", f"same-{i}", ts="2026-10-06T20:00:00.000000000Z")
    got: list[str] = []
    for _ in range(4):
        batch = ship.collect([(cid, "w")])
        got += [e["line"] for e in batch.entries]
        batch.commit()
    assert got == [f"same-{i}" for i in range(7)]


def test_secrets_are_redacted_for_every_workload_and_lines_are_cut_after_redaction() -> None:
    fake = FakeDocker()
    cid = _cid(fake)
    ship = logship.LogShipper(fake.docker())
    ship.set_secrets("w", ["hunter2-secret", "run-token-abc"])
    ship.set_secrets("other", ["other-secret"])
    fake.emit(cid, "stdout", "pw=hunter2-secret t=run-token-abc o=other-secret")
    fake.emit(cid, "stdout", "z" * 2040 + "hunter2-secret")  # the secret straddles the 2048 cut
    lines = [e["line"] for e in ship.collect([(cid, "w")]).entries]
    assert lines[0] == "pw=[redacted] t=[redacted] o=[redacted]"
    assert lines[1] == ("z" * 2040 + "[redacted]")[:2048] and "hunter" not in lines[1]
    assert all("secret" not in line for line in lines)


def test_the_byte_cap_stops_a_batch_and_the_rest_waits() -> None:
    fake = FakeDocker()
    cid = _cid(fake)
    ship = logship.LogShipper(fake.docker(), max_bytes=10_000)
    for i in range(10):
        fake.emit(cid, "stdout", f"{i}" + "q" * 2000)
    batch = ship.collect([(cid, "w")])
    assert 0 < len(batch.entries) < 10 and sum(len(json.dumps(e)) for e in batch.entries) <= 10_000
    batch.commit()
    assert ship.collect([(cid, "w")]).entries[0]["line"].startswith(str(len(batch.entries)))


def test_drain_carries_the_final_lines_of_a_container_that_is_going_away() -> None:
    fake = FakeDocker()
    cid = _cid(fake)
    ship = logship.LogShipper(fake.docker())
    ship.set_secrets("w", ["s3"])
    fake.emit(cid, "stdout", "bye s3")
    ship.drain(cid, "w")
    fake.docker().rm(cid)
    ship.add_agent_line("removed container c1 s3", "2026-10-06T20:00:00.000000Z")
    batch = ship.collect([])
    assert [(e["stream"], e["line"]) for e in batch.entries] == [("stdout", "bye [redacted]"), ("agent", "removed container c1 [redacted]")]
    assert ship.collect([]).entries == batch.entries  # still queued until committed
    batch.commit()
    assert ship.collect([]).entries == [] and ship.carry == []
    ship.forget("w")
    assert "w" not in ship.secrets


def test_cursors_persist_in_the_state_dir_and_stale_ones_are_pruned(tmp_path) -> None:
    fake = FakeDocker()
    cid = _cid(fake)
    fake.emit(cid, "stdout", "one")
    first = logship.LogShipper(fake.docker(), str(tmp_path))
    first.collect([(cid, "w")]).commit()
    assert oct(os.stat(tmp_path / "logcursors.json").st_mode & 0o777) == "0o600"
    second = logship.LogShipper(fake.docker(), str(tmp_path))
    assert second.collect([(cid, "w")]).entries == []
    second.prune([])
    assert second.cursors == {} and json.loads((tmp_path / "logcursors.json").read_text()) == {}


def test_a_container_that_vanished_yields_no_lines() -> None:
    ship = logship.LogShipper(FakeDocker().docker())
    assert ship.collect([("nope", "w")]).entries == []


def test_a_container_without_a_cursor_starts_from_its_last_thousand_lines() -> None:
    fake = FakeDocker()
    cid = _cid(fake)
    ship = logship.LogShipper(fake.docker(), max_lines=5000, max_bytes=10**7)
    for i in range(1500):
        fake.emit(cid, "stdout", f"n{i}")
    batch = ship.collect([(cid, "w")])
    assert [e["line"] for e in batch.entries][0] == "n500" and len(batch.entries) == 1000
    assert fake.calls_of("logs")[-1][-3:-1] == ["--tail", "1000"]
    batch.commit()
    fake.emit(cid, "stdout", "after")
    assert [e["line"] for e in ship.collect([(cid, "w")]).entries] == ["after"]
    assert "--tail" not in fake.calls_of("logs")[-1]
