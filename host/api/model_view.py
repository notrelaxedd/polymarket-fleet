"""Display shaping for the model detail page (docs/UI.md "Model detail"): the three
stats at the top, the gate verdict in words and the lineage's assignments. Read-only;
the rules themselves stay in host.eligibility and host.paper_gate, this module only
says which of them a lineage still misses."""
from __future__ import annotations

from typing import Any

import psycopg

from host.eligibility import DEFAULT_THRESHOLDS, ci_low, gate_limits, gate_metrics, model_flags
from host.paper_gate import DEFAULT_PAPER_THRESHOLDS, paper_stats
from host.settings import get_setting
from host.web import pct1, pvalue, signed_money, signed_pct

ASSIGNMENT_STATE = {"active": "ok", "halted": "warn", "settled": "muted", "cancelled": "muted"}


def _limits(conn: psycopg.Connection, key: str, defaults: dict[str, Any]) -> dict[str, Any]:
    """A thresholds setting with its defaults (a plain read: a page view takes no lock)."""
    value = get_setting(conn, key)
    out = dict(defaults)
    if isinstance(value, dict):
        out.update({k: v for k, v in value.items() if k in out and v is not None})
    return out


def _num(value: Any) -> float | None:
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) else float(value)


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _range(pair: Any) -> str | None:
    if isinstance(pair, (list, tuple)) and len(pair) == 2 and all(_num(v) is not None for v in pair):
        return f"{signed_pct(pair[0])} to {signed_pct(pair[1])}"
    return None


def backtest_misses(model: dict[str, Any], limits: dict[str, Any]) -> list[str] | None:
    """What the judged era still misses of the paper gate, or None when nothing judged it."""
    metrics = gate_metrics(model, limits)
    if not isinstance(metrics, dict):
        return None
    rules = gate_limits(limits)
    out: list[str] = []
    bets, roi, drawdown = (_num(metrics.get(k)) for k in ("n_bets", "roi", "max_drawdown"))
    if bets is None or bets < float(rules["min_bets"]):
        out.append(f"{int(rules['min_bets'])} bets ({int(bets or 0)} so far)")
    if roi is None or roi < float(rules["min_roi"]):
        out.append(f"an ROI of at least {signed_pct(rules['min_roi'])} (now {signed_pct(roi)})")
    if drawdown is None or drawdown > float(rules["max_drawdown"]):
        out.append(f"a drawdown of at most {pct1(rules['max_drawdown'])} (now {pct1(drawdown)})")
    if rules.get("min_roi_ci_low") is not None:
        low = ci_low(metrics, "roi")
        if low is None or low < float(rules["min_roi_ci_low"]):
            out.append(f"an ROI range starting at {signed_pct(rules['min_roi_ci_low'])} or more (now {signed_pct(low)})")
    if rules.get("max_market_p") is not None:
        p = _num(metrics.get("market_p"))
        if p is None or p > float(rules["max_market_p"]):
            out.append(f"a market test of p {float(rules['max_market_p']):.2f} or less (now {pvalue(p)})")
    forbidden = [f for f in model_flags(metrics, model.get("stress_metrics")) if f in (rules.get("forbid_flags") or [])]
    if forbidden:
        out.append("no " + _join([f.replace("_", "-") for f in forbidden]) + " flag")
    return out


def paper_misses(stats: dict[str, Any], ci: dict[str, Any] | None, limits: dict[str, Any]) -> list[str]:
    """What the pooled paper record still misses of the live gate."""
    out: list[str] = []
    if stats["games"] < int(limits["min_games"]):
        out.append(f"{int(limits['min_games'])} paper games ({stats['games']} so far)")
    if stats["bets"] < int(limits["min_bets"]):
        out.append(f"{int(limits['min_bets'])} paper bets ({stats['bets']} so far)")
    if stats["days"] < float(limits["min_days"]):
        out.append(f"{int(float(limits['min_days']))} days on paper ({int(stats['days'])} so far)")
    clv = stats.get("avg_clv")
    if clv is None or float(clv) < float(limits["min_clv"]):
        out.append(f"an average CLV of at least {signed_pct(limits['min_clv'])} (now {signed_pct(clv)})")
    if stats["pnl_cents"] < int(limits["min_pnl_cents"]):
        out.append(f"a paper profit (now {signed_money(stats['pnl_cents'])})")
    if limits.get("clv_ci_excludes_zero"):
        bounds = (ci or {}).get("ci")
        if not bounds or int((ci or {}).get("n_bets") or 0) < int(limits["min_bets"]) or float(bounds[0]) <= 0.0:
            out.append("a CLV interval above zero")
    return out


def gate_verdict(conn: psycopg.Connection, model: dict[str, Any]) -> dict[str, str]:
    """{"state", "text"}: where the lineage stands on the way to live trading, in words."""
    status = model.get("status")
    if status == "retired":
        return {"state": "muted", "text": "Retired: it no longer ranks or trades, and a retired lineage cannot come back."}
    if status == "live_eligible":
        return {"state": "ok", "text": "Eligible for live: it passed the held-out seasons and the paper record."}
    if status == "paper_ok":
        limits = _limits(conn, "thresholds_paper", DEFAULT_PAPER_THRESHOLDS)
        stats = paper_stats(conn, model["lineage_id"])
        if not stats["games"]:
            return {"state": "warn", "text": (
                f"Not yet eligible for live: no paper games yet. It needs {int(limits['min_games'])} paper games and "
                f"{int(limits['min_bets'])} paper bets over {int(float(limits['min_days']))} days, a profit"
                + (" and a CLV interval above zero." if limits.get("clv_ci_excludes_zero") else ".")
            )}
        missing = paper_misses(stats, model.get("paper_ci"), limits)
        if not missing:
            return {"state": "ok", "text": "Meets every paper rule: it becomes eligible for live at the next settlement."}
        return {"state": "warn", "text": f"Not yet eligible for live: needs {_join(missing)}."}
    missing_bt = backtest_misses(model, _limits(conn, "thresholds_backtest", DEFAULT_THRESHOLDS))
    if missing_bt is None:
        return {"state": "muted", "text": "Not cleared for paper trading: validate it on the held-out seasons first."}
    if not missing_bt:
        return {"state": "ok", "text": "Meets every rule for paper trading: its status updates on the next recompute."}
    return {"state": "warn", "text": f"Not cleared for paper trading: needs {_join(missing_bt)}."}


def detail_stats(model: dict[str, Any]) -> list[dict[str, Any]]:
    """The three stats at the top: edge vs market (CLV), backtest ROI, paper record."""
    paper, snap, validation = model.get("paper") or {}, model.get("snapshot") or {}, model.get("validation")
    if paper.get("avg_clv") is not None:
        rng = _range((model.get("paper_ci") or {}).get("ci"))
        edge = {"value": f"CLV {signed_pct(paper['avg_clv'])}", "note": f"paper, 90% range {rng}" if rng else f"paper, {paper.get('bets', 0)} bets"}
    elif snap.get("avg_clv") is not None:
        rng = _range(snap.get("clv_ci"))
        edge = {"value": f"CLV {signed_pct(snap['avg_clv'])}", "note": f"replay, 90% range {rng}" if rng else "snapshot replay"}
    else:
        edge = {"value": "-", "note": "no CLV yet: paper trade it or replay it"}
    if validation:
        bets = validation.get("n_bets") or 0
        roi = {"value": signed_pct(validation.get("roi")) if bets else "-", "note": f"on {bets} held-out bets"}
    else:
        metrics = model.get("backtest_metrics") if isinstance(model.get("backtest_metrics"), dict) else {}
        bets = metrics.get("n_bets") or 0
        roi = {"value": signed_pct(metrics.get("roi")) if bets else "-", "note": f"on {bets} bets, search era only"}
    games = paper.get("games") or 0
    record = {"value": signed_money(paper.get("pnl_cents")) if games else "-",
              "note": f"{games} games · {paper.get('bets') or 0} bets" if games else "no paper games yet"}
    return [
        {"name": "edge", "label": "Edge vs market", **edge},
        {"name": "backtest-roi", "label": "Backtest ROI", **roi},
        {"name": "paper", "label": "Paper", **record},
    ]


def lineage_assignments(conn: psycopg.Connection, lineage_id: Any, limit: int = 20) -> dict[str, Any]:
    """The lineage's most recent assignments (newest kickoff first) and their total count;
    (step 6C) an ingame_wp lineage's are the ones naming one of its models as their
    in-game model (`ingame` true on those rows)."""
    where = "(a.lineage_id = %s OR a.ingame_model_id IN (SELECT id FROM models WHERE lineage_id = %s))"
    rows = conn.execute(
        f"""
        SELECT a.id, a.model_id, a.mode, a.status, a.created_at, a.lineage_id <> %s AS ingame, g.season, g.week,
               g.home_team, g.away_team, g.kickoff_at, b.realized_pnl_cents, b.available_cents
          FROM assignments a JOIN games g ON g.game_id = a.game_id
          LEFT JOIN bankrolls b ON b.assignment_id = a.id
         WHERE {where} ORDER BY g.kickoff_at DESC NULLS LAST, a.created_at DESC LIMIT %s
        """,
        (lineage_id, lineage_id, lineage_id, limit),
    ).fetchall()
    total = conn.execute(f"SELECT count(*) AS n FROM assignments a WHERE {where}", (lineage_id, lineage_id)).fetchone()["n"]
    rows_out = [
        {**dict(r), "game": f"{r['away_team']} @ {r['home_team']}", "when": f"{r['season']} week {r['week']}"
         + (" · in-game" if r["ingame"] else ""), "state": ASSIGNMENT_STATE.get(r["status"], "muted")}
        for r in rows
    ]
    return {"rows": rows_out, "total": int(total)}
