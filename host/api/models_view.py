"""Display shaping for the Models list (docs/UI.md "Models"): one headline number per
lineage chosen by its rank basis, a one-line meta, the status chip state and the
stats at the top of the page. The ranking itself stays in host.leaderboard; this
module only turns its entries into words and short numbers."""
from __future__ import annotations

from typing import Any

from host.leaderboard import PAPER_RANK_BETS, PAPER_RANK_GAMES
from host.web import pvalue, signed_money, signed_pct

STATUS_STATE = {"live_eligible": "ok", "paper_ok": "ok", "candidate": "muted", "retired": "muted"}
FLAG_STATE = {"overfit": "bad", "fragile": "warn", "regime_dependent": "warn"}
FLAG_TITLES = {
    "overfit": "overfit: the search era looked better than the held-out era",
    "fragile": "fragile: worse prices or a parameter nudge remove the edge",
    "regime_dependent": "regime-dependent: one regime holds the profit",
}
BASIS_TITLES = {"paper": "ranked on paper CLV", "snapshot": "ranked on snapshot replay CLV"}
SORT_LINE = (
    f"Best first: paper CLV after {PAPER_RANK_GAMES} paper games and {PAPER_RANK_BETS} paper bets, then snapshot CLV after "
    "30 bets replayed on recorded prices, else validation ROI; few bets are shrunk toward zero."
)


def status_state(status: Any) -> str:
    """The chip state of a lineage status: ok, warn, bad or muted."""
    return STATUS_STATE.get(str(status), "muted")


def status_word(status: Any) -> str:
    return str(status or "unknown").replace("_", " ")


def _range(pair: Any) -> str | None:
    if isinstance(pair, (list, tuple)) and len(pair) == 2 and all(isinstance(v, (int, float)) for v in pair):
        return f"{signed_pct(pair[0])} to {signed_pct(pair[1])}"
    return None


def headline(entry: dict[str, Any]) -> dict[str, str]:
    """{"text": "CLV +0.6%", "title": ...}: paper or snapshot CLV for a lineage ranked on
    it, the validation ROI otherwise; a lineage the held-out era has not judged shows
    its search-era ROI and says so in the title."""
    mode = entry.get("rank_mode")
    validation = entry.get("validation")
    paper, snap = entry.get("paper") or {}, entry.get("snapshot") or {}
    if validation and mode == "paper" and paper.get("avg_clv") is not None:
        return {"text": f"CLV {signed_pct(paper['avg_clv'])}", "title": "average paper CLV: the closing price minus the price paid"}
    if validation and mode == "snapshot" and snap.get("avg_clv") is not None:
        return {"text": f"CLV {signed_pct(snap['avg_clv'])}", "title": "snapshot replay CLV: the closing price minus the price paid"}
    if validation:
        roi = signed_pct(validation.get("roi")) if validation.get("n_bets") else "-"
        return {"text": f"ROI {roi}", "title": "return on the held-out validation seasons"}
    metrics = entry.get("metrics") or {}
    roi = signed_pct(metrics.get("roi")) if metrics.get("n_bets") else "-"
    return {"text": f"ROI {roi}", "title": "search-era ROI only: not validated on held-out seasons yet"}


def meta_line(entry: dict[str, Any]) -> str:
    """The one grey line under a lineage's name: why it is unranked (when it is), then
    the record behind its headline."""
    reason = entry.get("unranked_reason")
    record = _record_line(entry)
    if not reason or reason == "retired" or record.startswith(reason):  # a retired row says so in its status chip
        return record
    return f"{reason} · {record}"


def _record_line(entry: dict[str, Any]) -> str:
    mode = entry.get("rank_mode")
    validation = entry.get("validation")
    paper, snap = entry.get("paper") or {}, entry.get("snapshot") or {}
    if validation and mode == "paper":
        parts = [f"{paper.get('games', 0)} games", f"{paper.get('bets', 0)} bets", signed_money(paper.get("pnl_cents"))]
        ci = (entry.get("paper_ci") or {}).get("ci")
        if _range(ci):
            parts.append(f"range {_range(ci)}")
        return " · ".join(parts)
    if validation and mode == "snapshot":
        parts = [f"{snap.get('n_games', 0)} games", f"{snap.get('n_bets', 0)} bets replayed"]
        if _range(snap.get("clv_ci")):
            parts.append(f"range {_range(snap.get('clv_ci'))}")
        return " · ".join(parts)
    if validation:
        parts = [f"{validation.get('n_bets') or 0} held-out bets"]
        if validation.get("n_bets") and _range((validation.get("ci") or {}).get("roi")):
            parts.append(f"range {_range(validation['ci']['roi'])}")
        if validation.get("market_p") is not None:
            parts.append(pvalue(validation["market_p"]))
        return " · ".join(parts)
    reason = entry.get("unranked_reason") or "not validated"
    bets = (entry.get("metrics") or {}).get("n_bets")
    return f"{reason} · search era {bets if bets is not None else '-'} bets"


def shape_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """The entry with a `view` dict for _model_row.html."""
    entry["view"] = {
        "headline": headline(entry),
        "meta": meta_line(entry),
        "status_state": status_state(entry.get("status")),
        "status_word": status_word(entry.get("status")),
        "basis_title": BASIS_TITLES.get(str(entry.get("rank_mode")), ""),
        "flags": [{"name": f, "state": FLAG_STATE.get(f, "warn"), "word": f.replace("_", "-"), "title": FLAG_TITLES.get(f, f)}
                  for f in entry.get("flags") or []],
    }
    return entry


def board_view(board: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """The leaderboard with a `view` per entry plus the page's stats and sort line."""
    ranked = [shape_entry(e) for e in board.get("ranked", [])]
    unranked = [shape_entry(e) for e in board.get("unranked", [])]
    best = ranked[0] if ranked else None
    every = ranked + unranked
    return {
        "ranked": ranked, "unranked": unranked, "best": best, "sort_line": SORT_LINE,
        "counts": {
            "ranked": len(ranked), "unranked": len(unranked),
            "live_eligible": sum(1 for e in every if e.get("status") == "live_eligible"),
            "paper_ok": sum(1 for e in every if e.get("status") == "paper_ok"),
        },
    }
