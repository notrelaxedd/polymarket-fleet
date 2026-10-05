"""Step 6 Part A phase of the end-to-end test (tests/test_e2e.py): a small model search
with a held-out validation era runs on the real agent through a two-process pool and
creates models that carry validation and stress metrics; the same search with one
process gives the same models and the same numbers; a validate job refills an older
lineage on the new era; the Models page shows the validation columns, the chips and
the Robustness section; the stricter gates promote and demote on the stored metrics.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from host.eligibility import DEFAULT_THRESHOLDS
from tests.conftest import stress_metrics, validation_metrics
from tests.e2e_models import finished, send

ERAS = {"backtest_seasons": [2016, 2019], "validation_seasons": [2020, 2021]}
SEARCH = {"family": "elo_blend", "n": 4, "seed": 6, "seasons": [2016, 2019], "top_k": 2}
CI_KEYS = {"roi", "avg_clv", "max_drawdown", "hit_rate", "avg_edge"}
PRICE_NAMES = ["spread+0.01", "spread+0.02", "fee x1.5"]
REGIMES = {"favourite", "underdog", "home", "away", "divisional", "non_divisional", "primetime", "day",
           "cold_or_windy", "other_weather"}
REGIME_KEYS = {"n_games", "n_bets", "roi", "pnl_cents", "mean_ll_gain"}
VALIDATION_EXTRAS = {"ci", "mean_ll_gain", "market_p", "brier_decomposition", "calib_slope", "calib_intercept",
                     "shrunk_roi", "era", "flags"}
SEARCH_ERA_FALLBACK = {"min_bets": 0, "min_roi": -1.0, "max_drawdown": 1.0, "require_validation": False}


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def check_validation_shape(vm: dict[str, Any], seasons: list[int]) -> None:
    """The validation_metrics object of docs/ROBUSTNESS.md A2 (the backtest shape plus the extras)."""
    assert VALIDATION_EXTRAS <= set(vm), sorted(vm)
    assert vm["era"] == "validation" and vm["seasons"] == seasons and len(vm["per_season"]) == len(seasons)
    assert set(vm["ci"]) == CI_KEYS and all(len(vm["ci"][k]) == 2 for k in CI_KEYS)
    assert vm["ci"]["roi"][0] <= vm["roi"] <= vm["ci"]["roi"][1] or vm["n_bets"] == 0
    assert vm["ci"]["avg_clv"] == [0.0, 0.0], "closing-line fills carry no CLV"
    assert 0.0 <= vm["market_p"] <= 1.0 and isinstance(vm["mean_ll_gain"], float)
    assert set(vm["brier_decomposition"]) == {"reliability", "resolution", "uncertainty"}
    assert set(vm["flags"]) <= {"overfit"} and len(vm["calibration"]) == 10
    assert abs(vm["shrunk_roi"] - vm["roi"] * vm["n_bets"] / (vm["n_bets"] + 100)) < 1e-9


def check_stress_shape(sm: dict[str, Any], seed: int) -> None:
    """The stress_metrics object of docs/ROBUSTNESS.md A3."""
    assert set(sm) == {"prices", "neighbourhood", "regimes", "flags", "seed"} and sm["seed"] == seed
    assert [p["name"] for p in sm["prices"]] == PRICE_NAMES
    assert all(set(p) == {"name", "n_bets", "roi", "log_loss", "mean_ll_gain"} for p in sm["prices"])
    nb = sm["neighbourhood"]
    assert nb["n"] == 10 and set(nb) == {"n", "shrunk_roi_median", "shrunk_roi_p10", "ll_gain_median", "ll_gain_p10"}
    assert nb["shrunk_roi_p10"] <= nb["shrunk_roi_median"] and nb["ll_gain_p10"] <= nb["ll_gain_median"]
    assert set(sm["regimes"]) == REGIMES and all(set(r) == REGIME_KEYS for r in sm["regimes"].values())
    for a, b in (("favourite", "underdog"), ("home", "away"), ("divisional", "non_divisional"), ("primetime", "day"),
                 ("cold_or_windy", "other_weather")):
        assert sm["regimes"][a]["n_games"] + sm["regimes"][b]["n_games"] == sm["regimes"]["home"]["n_games"] + sm["regimes"]["away"]["n_games"]
    assert set(sm["flags"]) <= {"fragile", "regime_dependent"}


def validated_search(host: Any, worker_id: str, workers: int, wait_for: Callable[..., Any],
                     settled: Callable[..., Any]) -> dict[str, Any]:
    """The four-candidate search with `workers` processes: the host copies the eras
    and the pool size in, the job succeeds and its result carries the validation."""
    host.post("/api/settings", {"search_workers": workers})
    job = send(host, "model_search", SEARCH, "any_idle")
    params = job["params"]
    assert params["validation_seasons"] == ERAS["validation_seasons"] and params["workers"] == workers, params
    assert params["backtest_seasons"] == ERAS["backtest_seasons"] and params["seasons"] == SEARCH["seasons"]
    wait_for(settled(host, worker_id, "model_search"), f"agent in model_search (workers {workers})")
    done = wait_for(finished(host, job["id"]), f"validated search done (workers {workers})", timeout=90.0)
    result = done["result"]
    assert result["evaluated"] == 4 and result["seasons"] == [2019] and result["validation_seasons"] == [2020, 2021]
    assert len(result["top"]) == 2 and len(result["validated"]) == 2 and len(result["created_models"]) == 2
    assert [v["index"] for v in result["validated"]] == [t["index"] for t in result["top"]]
    assert "validation_note" not in result and done["progress"] == 1
    wait_for(settled(host, worker_id, "idle"), "worker idle after the validated search")
    return done


def search_phase(host: Any, worker_id: str, wait_for: Callable[..., Any], settled: Callable[..., Any]) -> list[str]:
    """Pooled (2 workers) then serial (1 worker): the pooled search creates two
    validated models, the serial one finds the same models with the same numbers."""
    pooled = validated_search(host, worker_id, 2, wait_for, settled)
    result = pooled["result"]
    created = result["created_models"]
    assert all(m["created"] is True and m["lineage_id"] == m["id"] for m in created), created
    assert host.events(pooled["id"]).count("model_created") == 2
    ids = [m["id"] for m in created]
    for mid, top, validated in zip(ids, result["top"], result["validated"]):
        model = host.get(f"/api/models/{mid}")
        assert model["params"] == top["params"] and model["backtest_metrics"] == top["metrics"]
        assert model["validation_metrics"] == validated["validation_metrics"], "the search stores what it validated"
        assert model["stress_metrics"] == validated["stress_metrics"]
        check_validation_shape(model["validation_metrics"], [2020, 2021])
        check_stress_shape(model["stress_metrics"], seed=SEARCH["seed"])
        search_era = model["backtest_metrics"]
        assert search_era["era"] == "search" and set(search_era["ci"]) == CI_KEYS and 0 <= search_era["market_p"] <= 1
        assert search_era["seasons"] == [2019] and "shrunk_roi" in search_era and "mean_ll_gain" in search_era
        assert model["validated"] is True and model["validation"]["ci"]["roi"] == model["validation_metrics"]["ci"]["roi"]
        assert model["flags"] == model["validation_metrics"]["flags"] + [
            f for f in model["stress_metrics"]["flags"] if f not in model["validation_metrics"]["flags"]]
        assert set(model["flag_meanings"]) == set(model["flags"])

    serial = validated_search(host, worker_id, 1, wait_for, settled)
    again = serial["result"]
    assert [m["id"] for m in again["created_models"]] == ids and all(m["created"] is False for m in again["created_models"])
    assert host.events(serial["id"]).count("model_exists") == 2
    assert _dumps(again["top"]) == _dumps(result["top"]), "one process and two give the same search-era numbers"
    assert _dumps(again["validated"]) == _dumps(result["validated"]), "and the same validation and stress numbers"
    assert again["seasons"] == result["seasons"] and again["validation_seasons"] == result["validation_seasons"]

    board = host.get("/api/models")
    ranked = {m["id"]: m for m in board["ranked"]}
    assert set(ids) <= set(ranked), (ids, [m["id"] for m in board["unranked"]])
    for mid in ids:
        entry = ranked[mid]
        assert entry["validated"] is True and entry["rank_mode"] == "validation" and entry["rank"] >= 1
        assert entry["validation"]["ci"]["roi"] == entry["validation"]["ci"]["roi"] and "market_p" in entry["validation"]
        assert entry["score"] == entry["validation"]["shrunk_roi"] and entry["search_score"] is not None
    page = host.client.get("/models").text
    assert '<span class="k">validation ROI</span>' in page and '<span class="k">beats market</span>' in page
    for mid in ids:
        row = _row(page, mid)
        assert 'href="/jobs?validate_model=' in row and ("chip chip-beats" in row or "no <span" in row)
        if host.get(f"/api/models/{mid}")["validation_metrics"]["n_bets"]:
            assert '<span class="range">' in row, "the ROI interval is printed next to the validation ROI"
    return ids


def _row(page: str, model_id: str) -> str:
    start = page.index(f'data-model="{model_id}"')
    return page[start:page.index("</tr>", start)]


def validate_phase(host: Any, worker_id: str, child_id: str, root_id: str,
                   wait_for: Callable[..., Any], settled: Callable[..., Any]) -> None:
    """A validate job on the trained child: the whole lineage gets the new era's
    validation and stress metrics, the job and model pages show the Robustness section."""
    before = host.get(f"/api/models/{child_id}")
    assert before["validation_metrics"]["seasons"] == [2022, 2023, 2024, 2025], "the step 3 search validated on the default era"
    job = send(host, "validate", {"model_id": child_id}, worker_id)
    params = job["params"]
    assert job["role"] == "backtest" and params["seed"] == 1 and params["validation_seasons"] == [2020, 2021], job
    assert params["model_id"] == child_id and params["workers"] == 1
    wait_for(settled(host, worker_id, "backtest"), "worker in backtest for the validate job")
    done = wait_for(finished(host, job["id"]), "validate done", timeout=60.0)
    result = done["result"]
    assert set(result) == {"validation_metrics", "stress_metrics"}
    vm, sm = result["validation_metrics"], result["stress_metrics"]
    check_validation_shape(vm, [2020, 2021])
    check_stress_shape(sm, seed=1)
    assert vm["per_season"][0]["season"] == 2020
    events = host.events(job["id"])
    assert "model_validation" in events and events[-1] == "succeeded", events
    for mid in (child_id, root_id):
        stored = host.get(f"/api/models/{mid}")
        assert stored["validation_metrics"] == vm and stored["stress_metrics"] == sm, "the lineage shares the validation"
        assert stored["validation"]["seasons"] == [2020, 2021]
    page = host.client.get(f"/models/{child_id}").text
    assert 'id="robustness"' in page and "validation era 2020-2021" in page and 'class="ci-line"' in page
    assert "spread+0.02" in page and "primetime" in page.lower()
    detail = host.client.get(f"/jobs/{job['id']}").text
    assert 'id="robustness"' in detail, "the validate job's result renders like the model's section"
    form = host.client.get(f"/jobs?validate_model={child_id}").text
    assert 'id="validate" open' in form and f'value="{child_id}"' in form and 'name="validate_seed"' in form
    wait_for(settled(host, worker_id, "idle"), "worker idle after the validate job")


def set_lineage_metrics(host: Any, lineage_id: str, validation: dict[str, Any], stress: dict[str, Any]) -> None:
    """Force the stored validation and stress metrics of a lineage (every row)."""
    with psycopg.connect(host.database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE models SET validation_metrics = %s, stress_metrics = %s, updated_at = now() WHERE lineage_id = %s",
            (Jsonb(validation), Jsonb(stress), lineage_id),
        )


def gates_phase(host: Any, child_id: str, root_id: str) -> None:
    """The A4 gates on the trained lineage: forced metrics that clear every rule
    give paper_ok, a forbidden flag or a failed interval or market rule demotes,
    and with require_validation off the search era is judged as before."""

    def statuses() -> set[str]:
        return {host.get(f"/api/models/{mid}")["status"] for mid in (child_id, root_id)}

    def recompute(thresholds: dict[str, Any]) -> None:
        host.post("/api/settings", {"thresholds_backtest": thresholds})

    assert host.get("/api/settings")["thresholds_backtest"] == DEFAULT_THRESHOLDS
    set_lineage_metrics(host, root_id, validation_metrics(), stress_metrics())
    recompute(DEFAULT_THRESHOLDS)
    assert statuses() == {"paper_ok"}, "metrics that clear every rule promote the whole lineage"
    page = host.client.get("/models").text
    assert 'class="badge st-paper_ok">paper ok</span>' in _row(page, root_id)

    set_lineage_metrics(host, root_id, validation_metrics(flags=["overfit"]), stress_metrics(flags=["regime_dependent"]))
    recompute(DEFAULT_THRESHOLDS)
    assert statuses() == {"candidate"}, "a forbidden flag demotes"
    row = _row(host.client.get("/models").text, root_id)
    assert 'class="chip chip-flag chip-overfit"' in row and 'class="chip chip-flag chip-regime_dependent"' in row
    detail = host.get(f"/api/models/{child_id}")
    assert detail["flags"] == ["overfit", "regime_dependent"] and detail["status"] == "candidate"
    assert "the search era looked better than the held-out era" in host.client.get(f"/models/{child_id}").text

    set_lineage_metrics(host, root_id, validation_metrics(), stress_metrics(flags=["regime_dependent"]))
    recompute(DEFAULT_THRESHOLDS)
    assert statuses() == {"paper_ok"}, "regime_dependent is shown but not forbidden by default"
    recompute(dict(DEFAULT_THRESHOLDS, forbid_flags=["regime_dependent"]))
    assert statuses() == {"candidate"}, "a flag added to forbid_flags demotes on the next recompute"

    set_lineage_metrics(host, root_id, validation_metrics(ci_roi=(0.01, 0.07), market_p=0.02), stress_metrics())
    recompute(dict(DEFAULT_THRESHOLDS, min_roi_ci_low=0.01))
    assert statuses() == {"paper_ok"}, "the ROI 5th percentile on the boundary passes"
    recompute(dict(DEFAULT_THRESHOLDS, min_roi_ci_low=0.011))
    assert statuses() == {"candidate"}, "below min_roi_ci_low the interval rule fails"
    recompute(dict(DEFAULT_THRESHOLDS, max_market_p=0.01))
    assert statuses() == {"candidate"}, "a market p above max_market_p fails"
    recompute(dict(DEFAULT_THRESHOLDS, max_market_p=0.02))
    assert statuses() == {"paper_ok"}, "max_market_p is inclusive"

    set_lineage_metrics(host, root_id, validation_metrics(flags=["overfit"]), stress_metrics(flags=["fragile"]))
    recompute(DEFAULT_THRESHOLDS)
    assert statuses() == {"candidate"}
    recompute(dict(DEFAULT_THRESHOLDS, **SEARCH_ERA_FALLBACK))
    assert statuses() == {"paper_ok"}, "with require_validation off the search era is judged and flags are ignored"

    set_lineage_metrics(host, root_id, validation_metrics(), stress_metrics())
    recompute(DEFAULT_THRESHOLDS)
    assert statuses() == {"paper_ok"}
    assert host.get("/api/settings")["thresholds_backtest"] == DEFAULT_THRESHOLDS


def phase_validation(host: Any, worker_id: str, models: dict[str, Any], wait_for: Callable[..., Any],
                     settled: Callable[..., Any]) -> list[str]:
    """Returns the ids of the two validated search models; the eras and the pool
    size are put back afterwards."""
    before = host.get("/api/settings")
    assert before["validation_seasons"] == [2022, None] and before["search_workers"] == "auto"
    host.post("/api/settings", ERAS)
    settings_page = host.client.get("/settings").text
    for name in ("seasons_first", "seasons_last", "validation_first", "validation_last", "search_workers"):
        assert f'name="{name}"' in settings_page, name
    assert 'id="thresholds"' in settings_page and 'value="2020"' in settings_page
    ids = search_phase(host, worker_id, wait_for, settled)
    validate_phase(host, worker_id, models["child"], models["roots"][0], wait_for, settled)
    gates_phase(host, models["child"], models["roots"][0])
    host.post("/api/settings", {key: before[key] for key in ("backtest_seasons", "validation_seasons", "search_workers")})
    return ids
