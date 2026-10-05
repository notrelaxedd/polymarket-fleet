"""fleet.sim.ingame (the in-game search and its validation) and fleet.worker.pbp_cache
(the worker's ETag cache of the pbp feed, read lazily). Synthetic plays and a local fake
HTTP server; no database.

The feed body here is built from the documented shape (contract section 3: gzip JSON
lines, Content-Type application/x-ndjson+gzip, no Content-Encoding); it is unverified
against the real host route until host/api/data_pbp.py is wired and probed.
"""
from __future__ import annotations

import gzip
import inspect
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from fleet.sim.control import JobStopped
from fleet.sim.ingame import in_fit_set, run_ingame_search, search_from_params
from fleet.worker import pbp_cache
from tests.test_ingame_wp import synthetic_rows

TRAIN = [2016, 2019]
VALIDATION = [2020, 2021]
SEASONS = (2016, 2017, 2018, 2019, 2020, 2021)


@pytest.fixture(scope="module")
def rows() -> list[dict[str, Any]]:
    return synthetic_rows(7200, "search", seasons=SEASONS, vegas_noise=0.8)


def _search(rows: list[dict[str, Any]], checkpoint: dict[str, Any] | None = None, n: Any = 4,
            emits: list[dict[str, Any]] | None = None, stop_after: int | None = None) -> dict[str, Any]:
    out = emits if emits is not None else []

    def emit(cp: dict[str, Any], progress: float) -> None:
        assert 0.0 < progress <= 1.0
        out.append(json.loads(json.dumps(cp)))

    def should_stop() -> bool:
        return stop_after is not None and len(out) >= stop_after

    return run_ingame_search(lambda: iter(rows), n, 7, TRAIN, VALIDATION, emit, should_stop, checkpoint,
                             top_k=3, train_fraction=0.5)


def test_search_result_shape(rows: list[dict[str, Any]]) -> None:
    result = _search(rows)
    assert result["evaluated"] == 4 and len(result["top"]) == 3 and len(result["models"]) == 3
    assert result["train_seasons"] == TRAIN and result["validation_seasons"] == VALIDATION
    fit_games = {r["game_id"] for r in rows if r["season"] <= 2019 and in_fit_set(r["game_id"], 0.5)}
    assert 0 < len(fit_games) < len({r["game_id"] for r in rows if r["season"] <= 2019})
    assert result["n_fit_plays"] == sum(1 for r in rows if r["game_id"] in fit_games)
    selection = [e["selection"]["log_loss"] for e in result["top"]]
    assert selection == sorted(selection)
    for model in result["models"]:
        assert model["family"] == "ingame_wp" and set(model["params"]) == {"l2", "time_scale", "fp_scale"}
        assert model["artifact"]["train_seasons"] == [2016, 2017, 2018, 2019]
        val = model["validation"]
        assert val["n_plays"] == sum(1 for r in rows if r["season"] >= 2020)
        assert val["seasons"] == [2020, 2021] and val["beats_baseline"] is (val["log_loss"] <= val["vegas_log_loss"])
        assert val["beats_baseline"] is True  # the plays follow the feature map, vegas_wp is the truth plus noise
        assert len(val["calibration"]) == 10 and set(val["by_period"]) == {"1", "2", "3", "4", "5"}
        assert set(val["by_score_bucket"]) == {"<=-9", "-8..-1", "0", "1..8", ">=9"}
        assert model["summary"].count(". ") == 2
    assert "artifact" not in result["top"][0]


def test_search_never_fits_on_the_validation_era(rows: list[dict[str, Any]]) -> None:
    """Scrambling the validation-era outcomes (and vegas_wp) changes no fitted coefficient
    and no ranking, only the validation numbers."""
    base = _search(rows)
    scrambled = [dict(r, home_win=1.0 - r["home_win"], vegas_wp=0.5) if r["season"] >= 2020 else r for r in rows]
    other = _search(scrambled)
    assert [m["artifact"] for m in other["models"]] == [m["artifact"] for m in base["models"]]
    assert [m["params"] for m in other["models"]] == [m["params"] for m in base["models"]]
    assert [e["selection"] for e in other["top"]] == [e["selection"] for e in base["top"]]
    for a, b in zip(base["models"], other["models"]):
        assert a["validation"]["log_loss"] != b["validation"]["log_loss"]
        assert a["validation"]["n_plays"] == b["validation"]["n_plays"]


def test_eras_must_not_overlap(rows: list[dict[str, Any]]) -> None:
    for train, validation in (([2016, 2020], [2020, 2021]), ([2016, None], [2020, 2021]), ([2016, 2019], [None, 2021])):
        with pytest.raises(ValueError):
            run_ingame_search(lambda: iter(rows), 1, 1, train, validation, lambda c, p: None, lambda: False)


def test_checkpoint_resume_gives_the_same_result(rows: list[dict[str, Any]]) -> None:
    full = _search(rows)
    emits: list[dict[str, Any]] = []
    with pytest.raises(JobStopped):
        _search(rows, emits=emits, stop_after=2)
    assert [cp["evaluated"] for cp in emits] == [1, 2]
    resumed = _search(rows, checkpoint=emits[-1])
    assert resumed == full
    finished: list[dict[str, Any]] = []
    _search(rows, emits=finished)
    assert _search(rows, checkpoint=finished[-1]) == full


def test_params_grid_and_job_params(rows: list[dict[str, Any]]) -> None:
    grid = [{"l2": 0.02, "time_scale": 1.0, "fp_scale": 1.0}, {"l2": 5.0, "time_scale": 1.5, "fp_scale": 0.6}]
    result = _search(rows, n=grid)
    assert sorted(m["params"]["l2"] for m in result["models"]) == [0.02, 5.0]
    via_params = search_from_params({"family": "ingame_wp", "grid": grid, "seed": 7, "train_seasons": TRAIN,
                                     "validation_seasons": VALIDATION, "top_k": 3, "train_fraction": 0.5},
                                    lambda: iter(rows), lambda c, p: None, lambda: False)
    assert via_params == result
    with pytest.raises(ValueError):
        search_from_params({"family": "elo_blend"}, lambda: iter(rows), lambda c, p: None, lambda: False)


# pbp_cache -----------------------------------------------------------------------------


def _gzip_lines(rows: list[dict[str, Any]]) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as out:
        out.write(("\n".join(json.dumps(r) for r in rows) + "\n").encode("utf-8"))
    return buffer.getvalue()


class _Feed:
    """A fake host serving the pbp feed with ETag support."""

    def __init__(self, body: bytes, etag: str = '"7200-2021-abc"') -> None:
        self.body, self.etag, self.status = body, etag, 200
        self.requests: list[dict[str, Any]] = []
        feed = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                feed.requests.append({"path": self.path, "headers": dict(self.headers)})
                if feed.status != 200:
                    self.send_response(feed.status)
                    self.end_headers()
                    return
                if self.headers.get("If-None-Match") == feed.etag:
                    self.send_response(304)
                    self.send_header("ETag", feed.etag)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson+gzip")
                self.send_header("ETag", feed.etag)
                self.send_header("Content-Length", str(len(feed.body)))
                self.end_headers()
                self.wfile.write(feed.body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture()
def feed(rows: list[dict[str, Any]]) -> Iterator[_Feed]:
    server = _Feed(_gzip_lines(rows[:300]))
    yield server
    server.server.shutdown()
    server.server.server_close()


def test_refresh_uses_etag_and_iterates_lazily(feed: _Feed, tmp_path: Path, rows: list[dict[str, Any]]) -> None:
    path = pbp_cache.refresh_pbp(feed.url, "tok", str(tmp_path), (2016, 2021))
    assert path == str(tmp_path / "cache" / "pbp.jsonl.gz")
    first = feed.requests[0]
    assert first["path"] == "/api/v1/data/pbp?seasons=2016-2021"
    assert first["headers"]["Authorization"] == "Bearer tok" and "If-None-Match" not in first["headers"]
    stream = pbp_cache.iter_rows(path)
    assert inspect.isgenerator(stream) and next(stream) == rows[0]
    assert list(pbp_cache.iter_rows(path)) == rows[:300]
    assert all(r["season"] == 2017 for r in pbp_cache.iter_rows(path, (2017, 2017)))
    before = Path(path).read_bytes()
    assert pbp_cache.refresh_pbp(feed.url, "tok", str(tmp_path), (2016, 2021)) == path
    assert feed.requests[1]["headers"]["If-None-Match"] == feed.etag and Path(path).read_bytes() == before
    pbp_cache.refresh_pbp(feed.url, "tok", str(tmp_path), (2012, 2025))  # another selection: no If-None-Match
    assert "If-None-Match" not in feed.requests[2]["headers"]
    assert json.loads((tmp_path / "cache" / "pbp.etag").read_text()) == {"seasons": "2012-2025", "etag": feed.etag}


def test_refresh_falls_back_to_the_cache(feed: _Feed, tmp_path: Path) -> None:
    feed.status = 503
    with pytest.raises(pbp_cache.PbpCacheError):
        pbp_cache.refresh_pbp(feed.url, "tok", str(tmp_path))
    feed.status = 200
    path = pbp_cache.refresh_pbp(feed.url, "tok", str(tmp_path))
    good = Path(path).read_bytes()
    feed.status, feed.etag = 503, '"changed"'
    assert pbp_cache.refresh_pbp(feed.url, "tok", str(tmp_path)) == path and Path(path).read_bytes() == good
    feed.status, feed.body = 200, b'{"not": "gzip"}\n'  # a decompressed or broken body never replaces the cache
    assert pbp_cache.refresh_pbp(feed.url, "tok", str(tmp_path)) == path and Path(path).read_bytes() == good
    assert not (tmp_path / "cache" / "pbp.jsonl.gz.part").exists()


def test_injected_fetch_and_lazy_reader(tmp_path: Path, rows: list[dict[str, Any]]) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fetch(url: str, headers: dict[str, str], dest: str) -> tuple[int, str | None]:
        calls.append((url, headers))
        if headers.get("If-None-Match") == "e1":
            return 304, "e1"
        Path(dest).write_bytes(_gzip_lines(rows[:5]) + b"trailing garbage, not gzip")
        return 200, "e1"

    path = pbp_cache.refresh_pbp("http://host", "", str(tmp_path), fetch=fetch)
    assert pbp_cache.refresh_pbp("http://host", "", str(tmp_path), fetch=fetch) == path
    assert "Authorization" not in calls[0][1] and calls[1][1]["If-None-Match"] == "e1"
    stream = pbp_cache.iter_rows(path)
    assert [next(stream) for _ in range(5)] == rows[:5]  # read before the broken tail is reached
    with pytest.raises(Exception):
        list(stream)


def test_run_ingame_search_job_reads_the_cache(tmp_path: Path, rows: list[dict[str, Any]]) -> None:
    path = tmp_path / "pbp.jsonl.gz"
    path.write_bytes(_gzip_lines(rows))
    params = {"family": "ingame_wp", "n": 2, "seed": 7, "train_seasons": TRAIN, "validation_seasons": VALIDATION,
              "top_k": 3, "train_fraction": 0.5, "_context": {"pbp_path": str(path)}}
    result = pbp_cache.run_ingame_search_job(params, None, lambda c, p: None, lambda: False)
    assert result == _search(rows, n=2)
    with pytest.raises(ValueError):
        pbp_cache.run_ingame_search_job({"n": 1}, None, lambda c, p: None, lambda: False)
