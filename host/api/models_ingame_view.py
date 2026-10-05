"""Display shaping for the in-game (ingame_wp) parts of the Models pages (docs/UI.md
"Models", docs/INGAME.md "Scoring and dashboard"): the rows of the "In-game models"
group, the in-game line of a pre-game row whose lineage has in-game bets, and the
stats and verdict at the top of an ingame_wp model page. The numbers come from
host.leaderboard_ingame (`ingame`, `ingame_validation`, `ingame_reason`); this module
only turns them into words and short numbers."""
from __future__ import annotations

from typing import Any

from host.api.models_view import status_state, status_word
from host.ingame_eligibility import MIN_PLAYS
from host.web import fixed, num, season_span, signed_money

LL_TITLE = "log-loss gain over vegas_wp per held-out play: above zero beats nflverse's own in-game win probability"


def record_line(record: dict[str, Any] | None, lead: str = "in-game") -> str | None:
    """"in-game 3 bets · +$4.80" for a lineage with in-game bets, None without."""
    if not record or not record.get("bets"):
        return None
    bets = int(record["bets"])
    return f"{lead} {bets} bet{'' if bets == 1 else 's'} · {signed_money(record.get('pnl_cents'))}"


def gain_text(iv: dict[str, Any] | None) -> str:
    """"+0.008": the log-loss gain over vegas_wp, three decimals with its sign."""
    gain = (iv or {}).get("ll_gain")
    return "-" if gain is None else f"{float(gain):+.3f}"


def validation_line(iv: dict[str, Any] | None) -> str:
    """"41,812 plays 2022-2024 · log-loss 0.412 vs 0.420" or "not validated"."""
    if not iv:
        return "not validated"
    span = f" {season_span(iv.get('seasons'))}" if iv.get("seasons") else ""
    return f"{num(iv.get('n_plays'))} plays{span} · log-loss {fixed(iv.get('log_loss'))} vs {fixed(iv.get('vegas_log_loss'))}"


def ingame_row(entry: dict[str, Any]) -> dict[str, Any]:
    """An ingame_wp lineage with a `view` dict for the "In-game models" rows: the
    validation line, the reason (a second grey line, or the in-game paper record when
    the lineage has in-game bets, so the row keeps three lines at most), the gain."""
    iv = entry.get("ingame_validation")
    record = record_line(entry.get("ingame"), "in-game paper")
    entry["view"] = {
        "status_state": status_state(entry.get("status")),
        "status_word": status_word(entry.get("status")),
        "meta": validation_line(iv),
        "reason": None if record else entry.get("ingame_reason"),
        "record": record,
        "gain": gain_text(iv),
        "gain_title": LL_TITLE,
        "beats": bool(iv and iv.get("beats_baseline")),
    }
    return entry


def add_pregame_lines(entries: list[dict[str, Any]]) -> None:
    """The in-game line of a pre-game row whose lineage has in-game bets (ui.row's
    `ingame`); nothing for the others."""
    for entry in entries:
        entry.setdefault("view", {})["ingame"] = record_line(entry.get("ingame"))


def ingame_stats(model: dict[str, Any]) -> list[dict[str, Any]]:
    """The three stats at the top of an ingame_wp model page: log-loss against
    vegas_wp, the held-out plays, the in-game paper record."""
    iv = model.get("ingame_validation")
    record = model.get("ingame") or {}
    if iv:
        loss = {"value": fixed(iv.get("log_loss")), "note": f"vegas_wp {fixed(iv.get('vegas_log_loss'))} · gain {gain_text(iv)}"}
        plays = {"value": num(iv.get("n_plays")), "note": f"seasons {season_span(iv.get('seasons'))} · {num(MIN_PLAYS)} needed"}
    else:
        loss = {"value": "-", "note": "no held-out validation stored"}
        plays = {"value": "-", "note": f"{num(MIN_PLAYS)} needed for paper"}
    bets = int(record.get("bets") or 0)
    paper = {"value": signed_money(record.get("pnl_cents")) if bets else "-",
             "note": f"{record.get('games', 0)} games · {bets} bets" if bets else "no in-game bets yet"}
    return [
        {"name": "log-loss", "label": "Log-loss vs vegas_wp", **loss},
        {"name": "plays", "label": "Held-out plays", **plays},
        {"name": "ingame-paper", "label": "In-game paper", **paper},
    ]


def ingame_verdict(model: dict[str, Any]) -> dict[str, str]:
    """{"state", "text"}: the status rule in words; an in-game model never goes live."""
    status = model.get("status")
    iv = model.get("ingame_validation") or {}
    if status == "retired":
        return {"state": "muted", "text": "Retired: it no longer trades in-game, and a retired lineage cannot come back."}
    if status == "paper_ok":
        return {"state": "ok", "text": f"Cleared for in-game paper trading: it beats vegas_wp over {num(iv.get('n_plays'))} "
                                       "held-out plays. In-game orders never go live in this step."}
    reason = str(model.get("ingame_reason") or "not validated")
    return {"state": "warn" if iv else "muted",
            "text": f"Not cleared for in-game paper trading: {reason}. Paper ok needs a log-loss at or below vegas_wp "
                    f"over at least {num(MIN_PLAYS)} plays."}
