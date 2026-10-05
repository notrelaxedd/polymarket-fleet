"""Quarterback-change and injury signals, team_stats attachment and features_of
(docs/ROBUSTNESS.md B2; step 6B contract section B)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fleet.sim.data import FIELDS, TEAM_STATS_MAX, attach_team_stats, features_of, load_games
from fleet.sim.signals import (SIGNAL_KEYS, decision_time, empty_signals, injury_signals, normalise_signals,
                               qb_changed_map)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "games_sample.csv"
START = datetime(2024, 9, 1, 17, 0, tzinfo=timezone.utc)


def g(game_id: str, kickoff: str, home: str, away: str, home_qb: str | None, away_qb: str | None) -> dict:
    return {"game_id": game_id, "kickoff_at": kickoff, "home_team": home, "away_team": away,
            "home_qb_id": home_qb, "away_qb_id": away_qb}


# quarterback change ------------------------------------------------------------


def test_qb_change_across_a_season_boundary_and_unknown_starters() -> None:
    rows = [
        g("2023_17_A_B", "2023-12-31T18:00:00+00:00", "A", "B", "qa1", "qb1"),
        g("2023_18_C_A", "2024-01-07T18:00:00+00:00", "C", "A", "qc1", "qa1"),
        # New season, new starter for A: a change across the boundary.
        g("2024_01_A_C", "2024-09-08T17:00:00+00:00", "A", "C", "qa2", "qc1"),
        # Unknown starter (an unplayed game): 0, and it does not reset the history.
        g("2024_02_B_A", "2024-09-15T17:00:00+00:00", "B", "A", "", None),
        g("2024_03_A_B", "2024-09-22T17:00:00+00:00", "A", "B", "qa2", "qb2"),
    ]
    flags = qb_changed_map(list(reversed(rows)))  # order of the input does not matter
    assert flags["2023_17_A_B"] == {"home_qb_changed": 0, "away_qb_changed": 0}, "first game of a team"
    assert flags["2023_18_C_A"] == {"home_qb_changed": 0, "away_qb_changed": 0}
    assert flags["2024_01_A_C"] == {"home_qb_changed": 1, "away_qb_changed": 0}
    assert flags["2024_02_B_A"] == {"home_qb_changed": 0, "away_qb_changed": 0}, "unknown starter reads 0"
    # A's last known starter is qa2 (week 1), B's is qb1 (2023): B changed, A did not.
    assert flags["2024_03_A_B"] == {"home_qb_changed": 0, "away_qb_changed": 1}


def test_csv_adapter_derives_qb_flags_with_relocated_teams(tmp_path: Path) -> None:
    header = "game_id,season,game_type,week,gameday,gametime,away_team,away_score,home_team,home_score,away_qb_id,home_qb_id"
    lines = [
        header,
        "2019_17_OAK_DEN,2019,REG,17,2019-12-29,16:25,OAK,15,DEN,16,q_carr,q_lock",
        "2020_01_LV_CAR,2020,REG,1,2020-09-13,13:00,LV,34,CAR,30,q_carr,q_bridg",
        "2020_02_NO_LV,2020,REG,2,2020-09-21,20:15,NO,24,LV,34,q_brees,q_mull",
    ]
    path = tmp_path / "games.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    games = {x["game_id"]: x for x in load_games(str(path))}
    assert games["2020_01_LV_CAR"]["signals"]["away_qb_changed"] == 0, "OAK -> LV keeps the starter history"
    assert games["2020_02_NO_LV"]["signals"]["home_qb_changed"] == 1
    assert games["2019_17_OAK_DEN"]["signals"] == empty_signals()
    for game in games.values():
        assert set(game["signals"]) == set(SIGNAL_KEYS)
        assert game["team_stats"] == {"home": [], "away": []}


def test_fixture_has_qb_changes() -> None:
    games = load_games(str(FIXTURE))
    changed = sum(x["signals"]["home_qb_changed"] + x["signals"]["away_qb_changed"] for x in games)
    assert 0.05 * 2 * len(games) < changed < 0.25 * 2 * len(games)
    assert all(x["signals"]["home_out_count"] == 0 for x in games), "no injuries in a games.csv"


# injuries ------------------------------------------------------------------------


def inj(player: str, status: str, position: str = "WR", modified: str | None = "2024-09-06T20:00:00+00:00") -> dict:
    return {"gsis_id": player, "full_name": player.upper(), "position": position, "report_status": status,
            "date_modified": modified}


def test_injury_out_counts_respect_status_qb_and_decision_time() -> None:
    kickoff = "2024-09-08T17:00:00+00:00"
    cutoff = decision_time(kickoff, 60)
    assert cutoff == datetime(2024, 9, 8, 16, 0, tzinfo=timezone.utc)
    home = [
        inj("p1", "Out"), inj("p2", "out", "QB"), inj("p3", "Questionable"), inj("p4", "Doubtful"),
        inj("p1", "Out"),  # the same player twice counts once
        inj("p5", "Out", modified="2024-09-08T16:30:00+00:00"),  # after the decision time: leakage
        inj("p6", "Out", modified=None),  # no date: cannot prove it was known
    ]
    away = [inj("q1", "Out", "RB"), inj("q2", "Out", "TE", modified="2024-09-08T15:59:59Z")]
    out = injury_signals(home, away, cutoff)
    assert out == {"home_out_qb": 1, "away_out_qb": 0, "home_out_count": 2, "away_out_count": 2}
    # Kickoff as the decision time (lead unknown) lets the 16:30 report in.
    later = injury_signals(home, away, decision_time(kickoff, None))
    assert later["home_out_count"] == 3
    assert injury_signals([], [], None) == {"home_out_qb": 0, "away_out_qb": 0, "home_out_count": 0,
                                            "away_out_count": 0}


def test_normalise_signals_defaults_and_bounds() -> None:
    assert normalise_signals(None) == empty_signals()
    assert normalise_signals({"home_qb_changed": True, "away_qb_changed": 3, "home_out_count": "4",
                              "away_out_count": -2, "home_out_qb": "x"}) == {
        "home_qb_changed": 1, "away_qb_changed": 1, "home_out_qb": 0, "away_out_qb": 0,
        "home_out_count": 4, "away_out_count": 0}


# team_stats and features_of ------------------------------------------------------


def _stat(game_id: str, team: str, kickoff: str, off: float, season: int = 2024, week: int = 1) -> dict:
    return {"game_id": game_id, "season": season, "week": week, "team": team, "kickoff_at": kickoff,
            "off_epa_per_play": off, "def_epa_per_play": -off, "pass_rate": 0.6, "plays": 60, "success_rate": 0.45}


def test_load_games_attaches_strictly_earlier_team_stats(tmp_path: Path) -> None:
    games = [
        {"game_id": f"2024_{w:02d}_KC_OAK", "season": 2024, "game_type": "REG", "week": w,
         "kickoff_at": (START + timedelta(days=7 * w)).isoformat().replace("+00:00", "Z"), "home_team": "KC",
         "away_team": "OAK", "home_score": 20, "away_score": 10, "signals": {"home_out_count": w}}
        for w in range(1, 21)
    ]
    stats = []
    for game in games:
        stats.append(_stat(game["game_id"], "KC", game["kickoff_at"], game["week"] / 100, week=game["week"]))
        stats.append(_stat(game["game_id"], "LV", game["kickoff_at"], -game["week"] / 100, week=game["week"]))
    stats.append({"game_id": "bad", "team": "KC"})  # unplaceable rows are dropped
    path = tmp_path / "games.json"
    path.write_text(json.dumps({"games": games, "count": len(games), "team_game_stats": stats}), encoding="utf-8")
    loaded = load_games(str(path))
    first, last = loaded[0], loaded[-1]
    assert first["team_stats"] == {"home": [], "away": []}, "no stats of the game itself or later"
    assert len(last["team_stats"]["home"]) == TEAM_STATS_MAX
    weeks = [r["week"] for r in last["team_stats"]["home"]]
    assert weeks == sorted(weeks) and weeks[-1] == 19, "oldest first, the game itself excluded"
    assert [r["team"] for r in last["team_stats"]["away"]] == ["LV"] * TEAM_STATS_MAX, "OAK normalised to LV"
    assert all(r["kickoff_at"] < last["kickoff_at"] for r in last["team_stats"]["home"])
    assert last["signals"]["home_out_count"] == 20
    assert "signals" in FIELDS
    # A plain list (or a cache without team_game_stats) still loads, with empty stats.
    path.write_text(json.dumps(games), encoding="utf-8")
    assert all(x["team_stats"] == {"home": [], "away": []} for x in load_games(str(path)))


def test_attach_team_stats_excludes_same_kickoff() -> None:
    game = {"game_id": "g2", "kickoff_at": "2024-09-08T17:00:00+00:00", "home_team": "A", "away_team": "B"}
    rows = [_stat("g1", "A", "2024-09-08T17:00:00+00:00", 0.1), _stat("g0", "A", "2024-09-01T17:00:00+00:00", 0.2)]
    attach_team_stats([game], rows)
    assert [r["game_id"] for r in game["team_stats"]["home"]] == ["g0"]


def test_features_of_adds_signals_and_team_stats_with_safe_defaults() -> None:
    bare = features_of({"home_rest": 7, "season": 2024})
    assert bare["signals"] == empty_signals()
    assert bare["team_stats"] == {"home": [], "away": []}
    assert bare["home_rest"] == 7 and bare["wind"] is None
    full = features_of({"signals": {"home_qb_changed": 1}, "team_stats": {"home": [{"x": 1}, "junk"], "away": None}})
    assert full["signals"]["home_qb_changed"] == 1
    assert full["team_stats"] == {"home": [{"x": 1}], "away": []}
