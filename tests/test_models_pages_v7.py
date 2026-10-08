"""Step 7 (docs/UI.md "Models", "Model detail"): the Models list as one row per lineage
with a headline number by rank basis and a "..." menu, the unranked lineages folded,
and the model page as three stats, the gate verdict in words and folded disclosures."""
from __future__ import annotations

from typing import Any

from host.api.model_view import backtest_misses, paper_misses
from host.api.models_view import headline, meta_line
from host.api.robustness import stress_reading
from host.eligibility import DEFAULT_THRESHOLDS
from host.paper_gate import DEFAULT_PAPER_THRESHOLDS
from tests.conftest import (
    backtest_metrics, flash_cookie, insert_game, insert_model, insert_paper_bet, insert_validated_model, make_assignment,
    model_row, set_setting, stress_metrics, validation_metrics,
)
from tests.pagecheck import page

DETAIL_KEYS = ["model-summary", "model-robustness", "model-backtest", "model-snapshot", "model-paper", "model-assignments", "model-history"]


def _entry(**kw: Any) -> dict[str, Any]:
    base = {"rank_mode": "validation", "validation": None, "paper": {}, "snapshot": None, "metrics": {}, "paper_ci": None}
    return {**base, **kw}


# ------------------------------------------------------------------ view helpers (no database)

def test_headline_follows_the_rank_basis() -> None:
    validation = {"n_bets": 130, "roi": 0.062, "ci": {"roi": [-0.012, 0.094]}, "market_p": 0.012}
    paper = _entry(rank_mode="paper", validation=validation, paper={"games": 5, "bets": 30, "pnl_cents": 2795, "avg_clv": 0.006},
                   paper_ci={"ci": [-0.002, 0.014]})
    assert headline(paper)["text"] == "CLV +0.6%" and meta_line(paper) == "5 games · 30 bets · +$27.95 · range -0.2% to +1.4%"
    snap = _entry(rank_mode="snapshot", validation=validation, snapshot={"n_games": 60, "n_bets": 30, "avg_clv": 0.02, "clv_ci": None})
    assert headline(snap)["text"] == "CLV +2.0%" and meta_line(snap) == "60 games · 30 bets replayed"
    held_out = _entry(validation=validation)
    assert headline(held_out)["text"] == "ROI +6.2%" and meta_line(held_out) == "130 held-out bets · range -1.2% to +9.4% · p = 0.012"
    unjudged = _entry(rank_mode="paper", paper={"avg_clv": 0.05}, metrics={"n_bets": 0, "roi": 0.0}, unranked_reason="not validated")
    assert headline(unjudged)["text"] == "ROI -", "a lineage the held-out era has not judged never leads with its paper CLV"
    assert "search-era" in headline(unjudged)["title"] and meta_line(unjudged) == "not validated · search era 0 bets"


def test_gate_misses_in_words() -> None:
    stats = {"games": 5, "bets": 30, "days": 3.5, "avg_clv": 0.01, "pnl_cents": 500}
    missing = paper_misses(stats, {"n_bets": 30, "ci": [-0.002, 0.02]}, DEFAULT_PAPER_THRESHOLDS)
    assert missing == ["10 paper games (5 so far)", "40 paper bets (30 so far)", "21 days on paper (3 so far)", "a CLV interval above zero"]
    done = {"games": 12, "bets": 50, "days": 30.0, "avg_clv": 0.01, "pnl_cents": 500}
    assert paper_misses(done, {"n_bets": 50, "ci": [0.001, 0.02]}, DEFAULT_PAPER_THRESHOLDS) == []
    assert backtest_misses({"validation_metrics": None}, DEFAULT_THRESHOLDS) is None
    weak = {"validation_metrics": validation_metrics(n_bets=30, roi=0.01, ci_roi=(-0.02, 0.05), market_p=0.3, flags=["overfit"])}
    assert backtest_misses(weak, DEFAULT_THRESHOLDS) == [
        "50 bets (30 so far)", "an ROI of at least +2.0% (now +1.0%)", "an ROI range starting at +0.0% or more (now -2.0%)",
        "a market test of p 0.10 or less (now p = 0.300)", "no Overfit flag",
    ]


# ------------------------------------------------------------------ the Models list

def test_models_list_rows_menu_and_stats(client, conn):
    best = insert_validated_model(conn, params={"k": 40.0, "hfa": 70.0, "mov_scale": 1}, status="live_eligible")
    other = insert_validated_model(conn, params={"k": 30.0, "hfa": 60.0, "mov_scale": 0}, validation=validation_metrics(n_bets=60, roi=0.01))
    retired = insert_model(conn, params={"k": 33.0}, status="retired", validation=validation_metrics())
    fresh = insert_model(conn, params={"k": 34.0}, metrics=backtest_metrics(n_bets=20, roi=0.5))
    html = client.get("/models").text
    p = page(html)
    assert p.page_name == "models" and p.one("h1").text == "Models" and p.one("details.intro").is_open
    assert p.one("details.intro").attr("data-key") == "intro-models" and not p.has("table"), "a list of rows, not a table"
    best_stat = p.stat("best")
    assert best_stat.attr("href") == f"/models/{best['id']}" and best_stat.one(".stat-value").text == "ROI +4.0%"
    assert p.stat("ranked").one(".stat-value").text == "2" and p.stat("unranked").one(".stat-value").text == "2"
    assert p.stat("live-eligible").one(".stat-value").text == "1"
    assert p.count("[data-sort-line]") == 1 and "validation ROI" in p.one("[data-sort-line]").text
    ranked = p.card("ranked")
    assert ranked.row_ids("model") == [str(best["id"]), str(other["id"])] and ranked.one(".count").text == "2"
    for model_id in (best["id"], other["id"]):
        row = ranked.row("model", model_id)
        main = row.one("a.row-main")
        assert main.target == f"/models/{model_id}" and row.one(".row-title").text.startswith("#")
        assert row.count(".row-value") == 1 and row.count("details.menu") == 1 and row.one("details.menu > summary").text == "..."
        assert all(chip.text for chip in row.select(".chip")), "every chip has a word"
        assert set(row.actions()) == {"open", "train", "validate", "assign", "retire"}
    first = ranked.row("model", best["id"])
    assert first.chip("live_eligible").text == "Live Eligible" and first.chip("live_eligible").has_class("chip-ok")
    assert "K 40 · HFA 70 · MOV on" in first.one(".row-title").text
    assert ranked.row("model", other["id"]).chip("candidate").has_class("chip-muted")
    unranked = p.card("unranked")
    assert unranked.tag == "details" and not unranked.is_open and unranked.one("summary .count").text == "2"
    assert unranked.one(".disclosure-title").text == "Unranked"
    old = unranked.row("model", retired["id"])
    assert old.chip("retired").text == "Retired" and old.one(".row-meta").text.startswith("Retired 120 held-out bets")
    assert old.actions() == ["open", "train"], "a retired lineage can only be trained or opened"
    new = unranked.row("model", fresh["id"])
    assert new.one(".row-meta").text == "Candidate not validated · search era 20 bets" and new.chip("unvalidated").text == "not validated"
    assert chr(0x2014) not in html


def test_retire_from_the_list_menu_returns_to_the_list(client, conn):
    model = insert_validated_model(conn, params={"k": 41.0})
    form = page(client.get("/models").text).row("model", model["id"]).action("retire")
    assert form.attr("method") == "post" and form.attr("data-confirm")
    data = {i.attr("name"): i.attr("value") for i in form.select("input")}
    r = client.post(form.target, data=data, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/models" and "retired" in flash_cookie(r)
    assert model_row(conn, model["id"])["status"] == "retired"
    p = page(client.get("/models").text)
    assert p.card("unranked").row("model", model["id"]) and not p.has('[data-card="ranked"]')


# ------------------------------------------------------------------ the model page

def test_model_page_stats_verdict_and_disclosures(client, conn):
    model = insert_validated_model(conn, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1}, status="paper_ok", summary="Steady.")
    html = client.get(f"/models/{model['id']}").text
    p = page(html)
    assert p.page_name == "model" and p.one("details.intro").attr("data-key") == "intro-model"
    head = p.card("model")
    assert [s.attr("data-stat") for s in head.select(".stat")] == ["edge", "backtest-roi", "paper"]
    assert p.prop("Backtest ROI") == "+4.0%" and "on 120 held-out bets" in p.stat("backtest-roi").text
    assert p.prop("Edge vs market") == "-" and p.prop("Paper") == "-" and "no paper games yet" in p.stat("paper").text
    verdict = head.one(".verdict")
    assert verdict.chip("paper_ok").text == "Paper OK" and verdict.attr("data-verdict") == "warn"
    assert verdict.one(".verdict-text").text == (
        "Not yet eligible for live: no paper games yet. It needs 10 paper games and 40 paper bets over 21 days, a paper "
        "profit, an average CLV of at least +0.0% and a CLV interval above zero.")
    assert head.one(".actions details.menu").has('[data-action="retire"]'), "the one-way Retire sits behind the menu"
    keys = [d.attr("data-key") for d in p.select("details.disclosure")]
    assert keys == DETAIL_KEYS, keys
    for d in p.select("details.disclosure"):
        assert not d.is_open and d.one("summary .disclosure-title").text and d.one(".disclosure-summary").text
    assert p.card("summary").one(".disclosure-summary").text == "Steady." and p.card("summary").one(".summary").text == "Steady."
    backtest = p.card("backtest")
    assert backtest.prop("ROI") == "+3.0% Profit per dollar staked.", "a one-sentence caption under the number"
    assert backtest.has(".caption") and p.card("robustness").has(".reading")
    readings = p.card("robustness").texts(".reading")
    assert any(r.startswith("Worst case spread+0.02: ROI +1.1% on 74 bets") for r in readings), readings
    assert any(r.startswith("Best in ") for r in readings) and any(r.startswith("Calibration slope 0.97") for r in readings)
    assert p.card("assignments").one(".count").text == "0" and "Not assigned to any game yet." in p.card("assignments").text
    assert p.card("history").has('[data-card="lineage"]') and p.card("history").has('[data-card="jobs"]')
    assert chr(0x2014) not in html


def test_model_page_verdicts_by_status(client, conn):
    plain = insert_model(conn, params={"k": 50.0}, metrics=backtest_metrics(n_bets=10, roi=0.0))
    text = page(client.get(f"/models/{plain['id']}").text).one(".verdict-text").text
    assert text == "Not cleared for paper trading: validate it on the held-out seasons first."
    weak = insert_model(conn, params={"k": 51.0}, validation=validation_metrics(n_bets=30, roi=0.03), stress=stress_metrics())
    text = page(client.get(f"/models/{weak['id']}").text).one(".verdict-text").text
    assert text == "Not cleared for paper trading: needs 50 bets (30 so far)."
    live = insert_validated_model(conn, params={"k": 52.0}, status="live_eligible")
    p = page(client.get(f"/models/{live['id']}").text)
    assert p.one(".verdict").attr("data-verdict") == "ok" and p.one(".verdict-text").text.startswith("Eligible for live")
    gone = insert_validated_model(conn, params={"k": 53.0}, status="retired")
    p = page(client.get(f"/models/{gone['id']}").text)
    assert p.one(".verdict-text").text.startswith("Retired") and not p.has('[data-action="retire"]')


def test_model_page_lists_the_lineage_assignments(client, conn):
    model = insert_validated_model(conn, params={"k": 60.0}, status="paper_ok")
    insert_game(conn, "2026_06_NE_NYJ", home="NYJ", away="NE", week=6)
    assignment = make_assignment(conn, "2026_06_NE_NYJ", model["id"], "paper")
    p = page(client.get(f"/models/{model['id']}").text)
    card = p.card("assignments")
    row = card.row("assignment", assignment["id"])
    assert card.one(".count").text == "1" and row.one(".row-title").text == "NE @ NYJ"
    assert row.one(".row-meta").text.startswith("2026 week 6 · paper") and row.chip("active").has_class("chip-ok")
    assert row.one(".row-value").text == "$0.00" and row.one("a.row-main").target == "/trading"


# ------------------------------------------------------------------ step 7 review: the words match the rules

def _scores(conn, model, game_id: str, n_bets: int = 4, pnl: int = 100, clv: float = 0.01) -> None:
    conn.execute(
        "INSERT INTO model_scores (model_id, game_id, mode, lineage_id, n_bets, stake_cents, pnl_cents, avg_clv) "
        "VALUES (%s, %s, 'paper', %s, %s, %s, %s, %s)",
        (model["id"], game_id, model["lineage_id"], n_bets, n_bets * 1000, pnl, clv),
    )


def test_paper_games_are_counted_once_like_the_gate(client, conn):
    """Two models of one lineage trading the same 4 games make 4 games everywhere the
    page counts them, the number the verdict's "(4 so far)" uses."""
    root = insert_validated_model(conn, status="paper_ok", params={"k": 20.0, "hfa": 50.0, "mov_scale": 1})
    child = insert_model(conn, parent=root, params={"k": 20.0, "hfa": 50.0, "mov_scale": 1, "seed": "c"},
                         trained_through=[2025, 18])
    for i in range(4):
        game_id = f"2026_0{i + 1}_KC_LV"
        insert_game(conn, game_id, kickoff_in_s=86400 * (10 + i))
        insert_paper_bet(conn, root, game_id, 0.01, pnl_cents=100)
        _scores(conn, root, game_id)
        _scores(conn, child, game_id)
    p = page(client.get(f"/models/{root['id']}").text)
    assert p.stat("paper").one(".stat-note").text == "4 games · 32 bets"
    assert "10 paper games (4 so far)" in p.one(".verdict-text").text
    assert p.card("paper").one(".disclosure-summary").text.startswith("4 games · 32 bets · +$8.00")
    assert p.card("paper").prop("paper record").startswith("4 games · 32 bets · +$8.00"), "signed like the stat"
    row = page(client.get("/models").text).row("model", root["id"])
    assert row.chip("rank-paper") and "4 games · 32 bets · +$8.00" in row.one(".row-meta").text


def test_paper_pnl_rule_quotes_its_threshold(client, conn):
    limits = {**DEFAULT_PAPER_THRESHOLDS, "min_pnl_cents": 500}
    stats = {"games": 12, "bets": 50, "days": 30.0, "avg_clv": 0.01, "pnl_cents": 300}
    assert paper_misses(stats, {"n_bets": 50, "ci": [0.001, 0.02]}, limits) == ["a paper P&L of at least +$5.00 (now +$3.00)"]
    floor = {**limits, "min_pnl_cents": -5000}
    assert paper_misses({**stats, "pnl_cents": -6000}, {"n_bets": 50, "ci": [0.001, 0.02]}, floor) == [
        "a paper P&L of at least -$50.00 (now -$60.00)"], "a small loss allowed is not 'a profit'"
    set_setting(conn, "thresholds_paper", {**DEFAULT_PAPER_THRESHOLDS, "min_pnl_cents": 500, "clv_ci_excludes_zero": False})
    model = insert_validated_model(conn, status="paper_ok", params={"k": 21.0})
    text = page(client.get(f"/models/{model['id']}").text).one(".verdict-text").text
    assert text.endswith("over 21 days, a paper P&L of at least +$5.00 and an average CLV of at least +0.0%."), text


def test_live_eligible_verdict_names_the_gate_that_judged_it(client, conn):
    live = insert_validated_model(conn, params={"k": 54.0}, status="live_eligible")
    text = page(client.get(f"/models/{live['id']}").text).one(".verdict-text").text
    assert text == "Eligible for live: it passed the held-out seasons and the paper record."
    set_setting(conn, "thresholds_backtest", {**DEFAULT_THRESHOLDS, "require_validation": False})
    text = page(client.get(f"/models/{live['id']}").text).one(".verdict-text").text
    assert text == "Eligible for live: it passed the backtest gate and the paper record.", "no held-out claim off the search era"


def test_stress_reading_follows_the_fragile_rule(client, conn):
    base = {"n_bets": 120, "roi": 0.04}
    thin = [{"name": "spread+0.01", "n_bets": 80, "roi": 0.02}, {"name": "spread+0.02", "n_bets": 40, "roi": 0.011}]
    assert stress_reading(base, thin).endswith("; worse prices cut the bets to 40 of 120.")
    kept = [{"name": "spread+0.02", "n_bets": 74, "roi": 0.011}]
    assert stress_reading(base, kept).endswith("; the edge survives worse prices.")
    assert stress_reading(base, [{"name": "spread+0.02", "n_bets": 74, "roi": -0.01}]).endswith("; worse prices remove the edge.")
    flat = stress_reading({"n_bets": 120, "roi": -0.03}, [{"name": "spread+0.02", "n_bets": 100, "roi": 0.001}])
    assert flat.endswith("against -3.0% on 120 at base prices; there was no edge at base prices.")
    stress = stress_metrics(flags=["fragile"])
    stress["prices"][1]["n_bets"] = 40
    model = insert_validated_model(conn, params={"k": 24.0}, stress=stress)
    body = page(client.get(f"/models/{model['id']}").text).card("robustness")
    assert body.chip("fragile") and not any("survives" in r for r in body.texts(".reading")), "no 'survives' beside 'fragile'"
    caption = " ".join(body.texts(".caption"))
    assert "makes luck an unlikely explanation" in caption and "not luck" not in caption


def test_ranked_note_counts_every_lineage_cleared_for_paper(client, conn):
    insert_validated_model(conn, status="live_eligible", params={"k": 25.0})
    insert_validated_model(conn, status="live_eligible", params={"k": 26.0})
    insert_validated_model(conn, status="paper_ok", params={"k": 27.0})
    insert_validated_model(conn, params={"k": 28.0})
    p = page(client.get("/models").text)
    assert p.stat("ranked").one(".stat-value").text == "4" and p.stat("ranked").one(".stat-note").text == "3 cleared for paper"


def test_counts_read_one_game_one_bet(client, conn):
    root = insert_validated_model(conn, status="paper_ok", params={"k": 29.0}, validation=validation_metrics(n_bets=1))
    insert_game(conn)
    insert_paper_bet(conn, root, "2026_05_KC_LV", 0.02, pnl_cents=150)
    _scores(conn, root, "2026_05_KC_LV", n_bets=1, pnl=150, clv=0.02)
    p = page(client.get(f"/models/{root['id']}").text)
    assert p.stat("paper").one(".stat-note").text == "1 game · 1 bet" and "on 1 held-out bet" in p.stat("backtest-roi").text
    assert "Paper OK 1 held-out bet · range " in page(client.get("/models").text).row("model", root["id"]).one(".row-meta").text
