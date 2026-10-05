"""The worker's games cache keeps the feed's team stats and a snapshot backtest fetches
the feed with its own injury cutoff (fleet/worker/context.py, docs/PROTOCOL.md)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from fleet.common import http
from fleet.sim.data import load_games
from fleet.worker import config, context, jobs

GAME = {"game_id": "2021_01_A_B", "season": 2021, "week": 1, "game_type": "REG",
        "kickoff_at": "2021-09-12T17:00:00Z", "home_team": "B", "away_team": "A",
        "home_score": 20, "away_score": 17, "home_moneyline": -150, "away_moneyline": 130}
LATER = dict(GAME, game_id="2021_02_A_B", week=2, kickoff_at="2021-09-19T17:00:00Z")
STAT = {"game_id": "2021_01_A_B", "season": 2021, "week": 1, "team": "B", "kickoff_at": "2021-09-12T17:00:00Z",
        "off_epa_per_play": 0.12, "def_epa_per_play": -0.03, "pass_rate": 0.55, "plays": 60, "success_rate": 0.47}
SNAPSHOT_JOB = {"kind": "backtest", "params": {"family": "elo_blend", "price_source": "snapshots",
                                               "decision_minutes_before_kickoff": 300}}


class FakeGet:
    """Stands in for http.get_json_etag; answers with the feed body for the minutes asked."""

    def __init__(self, stated: Any = "echo") -> None:
        self.urls: list[str] = []
        self.stated = stated

    def __call__(self, url: str, token: str | None = None, etag: str | None = None, timeout: float = 0) -> http.Response:
        if "/data/prices" in url:
            return http.Response(200, {"markets": [], "count": 0}, "p")
        self.urls.append(url)
        minutes = int(url.split("decision_minutes=")[1]) if "decision_minutes=" in url else 60
        if etag == f"e{minutes}":
            return http.Response(304, None, etag)
        body: dict[str, Any] = {"games": [GAME, LATER], "count": 2, "team_game_stats": [STAT]}
        stated = minutes if self.stated == "echo" else self.stated
        if stated is not None:
            body["decision_minutes_before_kickoff"] = stated
        return http.Response(200, json.loads(json.dumps(body)), f"e{minutes}")


@pytest.fixture()
def fake_get(monkeypatch: pytest.MonkeyPatch) -> FakeGet:
    fake = FakeGet()
    monkeypatch.setattr(context.http, "get_json_etag", fake)
    return fake


def test_the_cache_keeps_team_game_stats_so_load_games_attaches_them(fake_get: FakeGet, tmp_path: Any) -> None:
    path = context.refresh_games("http://h", "t", str(tmp_path), 1.0)
    assert path == config.games_cache_path(str(tmp_path)) and fake_get.urls == ["http://h/api/v1/data/games"]
    games = {g["game_id"]: g for g in load_games(path)}
    assert games["2021_02_A_B"]["team_stats"]["home"][0]["off_epa_per_play"] == pytest.approx(0.12)
    assert games["2021_01_A_B"]["team_stats"]["home"] == [], "only earlier kickoffs"


def test_a_snapshot_backtest_fetches_its_own_cutoff_into_its_own_cache(fake_get: FakeGet, tmp_path: Any) -> None:
    state = str(tmp_path)
    ctx = context.build_context("http://h", "t", state, SNAPSHOT_JOB, 1.0, 1.0)
    assert fake_get.urls[0] == "http://h/api/v1/data/games?decision_minutes=300"
    assert ctx["games_path"] == config.games_cache_path(state, 300) and ctx["games_path"].endswith("games.d300.json")
    assert ctx["games_minutes"] == 300
    with open(config.games_etag_path(state, 300), encoding="utf-8") as fh:
        assert fh.read().strip() == "e300"
    plain = context.build_context("http://h", "t", state, {"kind": "backtest", "params": {}}, 1.0, 1.0)
    assert plain["games_path"] == config.games_cache_path(state) != ctx["games_path"]
    assert "games_minutes" not in plain and fake_get.urls[-1] == "http://h/api/v1/data/games"
    context.build_context("http://h", "t", state, SNAPSHOT_JOB, 1.0, 1.0)
    assert fake_get.urls[-1].endswith("?decision_minutes=300"), "the d300 cache sends its own etag (304)"
    no_minutes = {"kind": "backtest", "params": {"price_source": "snapshots"}}
    assert context.build_context("http://h", "t", state, no_minutes, 1.0, 1.0)["games_minutes"] == 60


@pytest.mark.parametrize("stated", [None, 60])
def test_a_feed_with_another_or_no_stated_cutoff_fails_the_snapshot_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
                                                                       stated: Any) -> None:
    monkeypatch.setattr(context.http, "get_json_etag", FakeGet(stated=stated))
    with pytest.raises(context.ContextError, match="the job needs 300"):
        context.build_context("http://h", "t", str(tmp_path), SNAPSHOT_JOB, 1.0, 1.0)
    assert not (tmp_path / "cache" / "games.d300.json").exists()


def test_the_job_refuses_a_context_whose_feed_cutoff_differs(tmp_path: Any) -> None:
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"markets": [], "count": 0}), encoding="utf-8")
    params = dict(SNAPSHOT_JOB["params"], _context={"prices_path": str(prices), "games_minutes": 60})
    with pytest.raises(ValueError, match="cut injuries at 60 minutes"):
        jobs._replay(params)
    params["_context"]["games_minutes"] = 300
    assert jobs._replay(params).decision_minutes == 300
