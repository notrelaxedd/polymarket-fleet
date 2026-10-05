"""Step 6C model review regressions.

1. Kickoffs: nflverse gives a kickoff to the receiving team at the kicking team's 35
   (yardline_100 35); the live ESPN feed gives it to the kicking team at its own 35
   (start.team, yardsToEndzone 65). host/pbp_rows stores kickoffs in the live convention,
   so the same kickoff is the same model state and probability on both paths.
2. Identity: a repeated ingame_wp search with the same params hits the existing root;
   the posted artifact must replace the stored one together with its metrics and
   summary (host/models._existing_root), so the gate never judges coefficients by the
   metrics of another fit.
"""
from __future__ import annotations

import copy
import csv
import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from fleet.models.ingame_wp import IngameWP, feature_vector, state_from_row
from fleet.sim.ingame import search_from_params
from fleet.worker.posts import MODEL_FIELDS
from host import models as host_models
from host import pbp_rows
from host.exchange.gamestate_parse import STATE_KEYS, parse_summary
from tests.conftest import lease_job, model_row
from tests.test_ingame_wp import synthetic_rows

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PBP = FIXTURES / "pbp_rows_sample.csv.gz"
OT_SUMMARY = FIXTURES / "espn_summary_overtime.json"
# The reviewed model: field-position coefficient 0.657 with fp_scale 1.2; every other
# coefficient non-zero too, so any feature that differs moves the probability.
PARAMS = {"l2": 0.02, "time_scale": 0.74, "fp_scale": 1.2}
COEF = [0.05, 0.12, 0.9, 0.3, 0.657, -0.1, -0.05, 0.08, 0.2, 0.4]


def _model() -> IngameWP:
    return IngameWP.from_json(PARAMS, {"coef": COEF, "n_train": 1, "train_seasons": [2023]})


def _records() -> dict[tuple[str, str], dict[str, str]]:
    with gzip.open(PBP, "rt", newline="") as fh:
        return {(r["game_id"], r["play_id"]): r for r in csv.DictReader(fh)}


def _row(record: dict[str, str], score_before: tuple[int, int] = (0, 0)) -> dict[str, Any]:
    return pbp_rows.map_record(record, score_before, None)


def _live_kickoff(payload: dict[str, Any]) -> dict[str, Any]:
    plays = parse_summary(json.dumps(payload))
    kick = next(p for p in plays if p["play_text"] and " kicks " in p["play_text"])
    return {k: kick[k] for k in STATE_KEYS}


def _same(live: dict[str, Any], train: dict[str, Any], pregame: float | None) -> None:
    """The live and training states agree on everything the model reads."""
    for key in ("status", "period", "clock_seconds", "possession", "down", "distance", "yardline_100",
                "home_timeouts", "away_timeouts"):
        assert live[key] == train[key], key
    assert live["home_score"] - live["away_score"] == train["home_score"] - train["away_score"]
    ts, fp = PARAMS["time_scale"], PARAMS["fp_scale"]
    assert feature_vector(live, pregame, ts, fp) == feature_vector(train, pregame, ts, fp)
    model = _model()
    assert model.predict(live, pregame) == model.predict(train, pregame)


def test_real_nflverse_kickoffs_are_stored_as_the_kicking_team() -> None:
    records = _records()
    # DET at KC opener: KC (home) kicks to DET; nflverse says posteam DET, yardline 35.
    opener = records[("2023_01_DET_KC", "40")]
    assert (opener["posteam"], opener["yardline_100"]) == ("DET", "35")
    row = _row(opener)
    assert (row["posteam_is_home"], row["yardline_100"]) == (True, 65)
    # NYJ (home) kicks off overtime to BUF; and BUF's late return after the NYJ field goal.
    for play in ("3902", "3548"):
        row = _row(records[("2023_01_BUF_NYJ", play)])
        assert (row["posteam_is_home"], row["yardline_100"]) == (True, 65), play
    # A safety free kick from the kicking team's 20 (nflverse: receiver at 20).
    free_kick = dict(opener, yardline_100="20")
    assert _row(free_kick)["yardline_100"] == 80
    # Scrimmage plays are untouched.
    scrimmage = records[("2023_01_DET_KC", "56")]
    assert _row(scrimmage)["yardline_100"] == int(float(scrimmage["yardline_100"])) == 75
    assert _row(scrimmage)["posteam_is_home"] is False


def test_fixture_kickoff_is_the_same_state_live_and_in_training() -> None:
    """The overtime fixture's kickoff (KC kicks to home LV, tied 24-24, OT 10:00) through
    the live parser and the same kickoff as an nflverse record through map_record."""
    payload = json.loads(OT_SUMMARY.read_text(encoding="utf-8"))
    live = _live_kickoff(payload)
    assert (live["possession"], live["yardline_100"], live["period"], live["clock_seconds"]) == ("away", 65, 5, 600)
    record = {"game_id": "2026_05_KC_LV", "play_id": "501", "season": "2026", "home_team": "LV", "away_team": "KC",
              "posteam": "LV", "defteam": "KC", "return_team": "LV", "yardline_100": "35", "play_type": "kickoff",
              "game_seconds_remaining": "600", "quarter_seconds_remaining": "600", "game_half": "Overtime",
              "down": "", "ydstogo": "0", "timeout": "0", "home_timeouts_remaining": "2",
              "away_timeouts_remaining": "1", "home_score": "", "away_score": "", "vegas_home_wp": "0.5"}
    train = state_from_row(_row(record, (24, 24)))
    _same(live, train, 0.55)
    # Before the fix the receiver-at-35 encoding moved p by about 0.15 to 0.20 here.
    receiver = state_from_row(dict(_row(record, (24, 24)), posteam_is_home=True, yardline_100=35))
    assert abs(_model().predict(receiver, 0.55) - _model().predict(live, 0.55)) > 0.1


def test_real_overtime_kickoff_is_the_same_state_live_and_in_training() -> None:
    """The real 2023_01_BUF_NYJ overtime kickoff (NYJ kicks to BUF, 16-16, OT 10:00) from
    the nflverse fixture against the same play in ESPN's summary shape."""
    record = _records()[("2023_01_BUF_NYJ", "3902")]
    train = state_from_row(_row(record, (16, 16)))
    payload = copy.deepcopy(json.loads(OT_SUMMARY.read_text(encoding="utf-8")))
    competition = payload["header"]["competitions"][0]
    for entry in competition["competitors"]:
        code = "NYJ" if entry["homeAway"] == "home" else "BUF"
        entry["team"]["abbreviation"] = code
        entry["score"] = "16"
    home_id = next(e["id"] for e in competition["competitors"] if e["homeAway"] == "home")
    payload["situation"].update(homeTimeouts=2, awayTimeouts=2)
    kick = payload["drives"]["current"]["plays"][0]
    kick.update(text="(10:00) G.Zuerlein kicks 65 yards from NYJ 35 to end zone, Touchback.", homeScore=16, awayScore=16)
    kick["start"]["team"] = {"id": home_id}
    live = _live_kickoff(payload)
    assert (live["possession"], live["yardline_100"]) == ("home", 65)
    _same(live, train, 0.42)


@pytest.mark.parametrize("pregame", [None, 0.3, 0.7])
def test_regulation_kickoff_after_a_home_score(pregame: float | None) -> None:
    """Home leads 21-14 in Q3 10:00 and kicks: live (home at 65) equals training."""
    live = {"status": "in", "period": 3, "clock_seconds": 600, "home_score": 21, "away_score": 14,
            "possession": "home", "down": None, "distance": None, "yardline_100": 65,
            "home_timeouts": 2, "away_timeouts": 1}
    record = dict(_records()[("2023_01_DET_KC", "40")], home_team="KC", posteam="DET", game_half="Half2",
                  game_seconds_remaining="1500", quarter_seconds_remaining="600", home_timeouts_remaining="2",
                  away_timeouts_remaining="1")
    _same(live, state_from_row(_row(record, (21, 14))), pregame)


# --------------------------------------------------------------------- identity


def _post(conn, worker, entry: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    job = lease_job(conn, worker, "model_search", {"family": "ingame_wp"})
    body = {k: entry.get(k) for k in MODEL_FIELDS}
    body["job_id"] = str(job["id"])
    return host_models.create_model(conn, body, worker.id)


def _validation(log_loss: float, beats: bool, seasons: list[int]) -> dict[str, Any]:
    return {"n_plays": 20000, "log_loss": log_loss, "vegas_log_loss": 0.5, "beats_baseline": beats,
            "seasons": seasons, "era": "validation"}


def _entry(coef: list[float], validation: dict[str, Any], n_train: int, summary: str) -> dict[str, Any]:
    return {"family": "ingame_wp", "params": dict(PARAMS), "trained_through": None, "parent_model_id": None,
            "artifact": {"coef": coef, "features": list(_model().to_json()["features"]), "n_train": n_train,
                         "train_seasons": [2018, 2019]},
            "backtest_metrics": {"era": "search", "n_fit_plays": n_train, "log_loss": 0.48},
            "validation_metrics": validation, "stress_metrics": None, "summary": summary}


def test_equal_params_new_artifact_replaces_artifact_with_its_metrics(conn, make_worker) -> None:
    worker = make_worker()
    first = _entry(COEF, _validation(0.49, True, [2022, 2023]), 24049, "first fit.")
    second = _entry([c * 0.5 for c in COEF], _validation(0.52, False, [2024]), 180431, "second fit.")
    row, created = _post(conn, worker, first)
    assert created and row["status"] == "paper_ok"
    again, created = _post(conn, worker, second)
    assert not created and again["id"] == row["id"]
    stored = model_row(conn, row["id"])
    assert stored["artifact"] == second["artifact"]
    assert stored["validation_metrics"] == second["validation_metrics"]
    assert stored["backtest_metrics"] == second["backtest_metrics"]
    assert stored["backtest_metrics"]["n_fit_plays"] == stored["artifact"]["n_train"]
    assert stored["summary"] == "second fit."
    assert stored["status"] == again["status"] == "candidate", "the gate follows the new fit"
    # A refit whose metrics happen to be equal still replaces the coefficients.
    third = dict(second, artifact=dict(second["artifact"], coef=[c * 0.25 for c in COEF]), summary=None)
    _post(conn, worker, third)
    stored = model_row(conn, row["id"])
    assert stored["artifact"] == third["artifact"] and stored["summary"] == "second fit."
    # The same artifact again with newer metrics: the metrics move, the artifact stays.
    fourth = dict(third, validation_metrics=_validation(0.47, True, [2024, 2025]))
    _post(conn, worker, fourth)
    stored = model_row(conn, row["id"])
    assert stored["artifact"] == third["artifact"]
    assert stored["validation_metrics"] == fourth["validation_metrics"] and stored["status"] == "paper_ok"


def test_repeated_search_with_equal_params_keeps_artifact_and_metrics_paired(conn, make_worker) -> None:
    """The reviewed scenario: two default-seed searches over different eras draw the same
    params; the second's coefficients and validation are stored together."""
    rows = synthetic_rows(3000, "identity", seasons=(2016, 2017, 2018, 2019, 2020, 2021), vegas_noise=0.8)
    a = search_from_params({"family": "ingame_wp", "n": 1, "train_seasons": [2016, 2017],
                            "validation_seasons": [2020, 2021]}, lambda: iter(rows), lambda c, f: None, lambda: False)
    b = search_from_params({"family": "ingame_wp", "n": 1, "train_seasons": [2016, 2019],
                            "validation_seasons": [2020, 2021], "train_fraction": 1},
                           lambda: iter(rows), lambda c, f: None, lambda: False)
    ea, eb = a["create_models"][0], b["create_models"][0]
    assert ea["params"] == eb["params"] and ea["artifact"]["coef"] != eb["artifact"]["coef"]
    worker = make_worker()
    row, _ = _post(conn, worker, ea)
    again, created = _post(conn, worker, eb)
    assert not created and again["id"] == row["id"]
    stored = model_row(conn, row["id"])
    assert stored["artifact"]["coef"] == eb["artifact"]["coef"]
    assert stored["validation_metrics"]["log_loss"] == eb["validation_metrics"]["log_loss"]
    assert stored["backtest_metrics"]["n_fit_plays"] == stored["artifact"]["n_train"]
    assert stored["summary"] == eb["summary"]
