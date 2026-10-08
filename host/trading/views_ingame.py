"""In-game view shaping for the /trading page (docs/DASHBOARD.md "Trading page",
contract section 12): per assignment the live score and clock with the state age (or
"state stale"), the in-game model's probability next to the market mid, the in-game
flag of fills, and the "In-game feed" block (per-source lag and the suspension state).

Read-only. The game state comes from host.exchange.gamestate.latest_state and the lag
from host.exchange.feedlag.lag_status, the same calls the trade state payload uses, so
the page shows what the worker and the approval see.
"""
from __future__ import annotations

from typing import Any

import psycopg

from fleet.models.ingame_wp import IngameWP
from fleet.sim.odds import devig
from host.exchange.feedlag import lag_status
from host.exchange.gamestate import latest_state
from host.labels import label
from host.leaderboard import short_params
from host.settings import get_setting
from host.trading.ingame import model_retired
from host.web import ago

INGAME_FAMILY = "ingame_wp"
SOURCE_NAMES = {"espn_summary": "ESPN", "espn_scoreboard": "ESPN scoreboard", "yahoo": "Yahoo"}
DEFAULT_MAX_STATE_AGE_S = 30.0
DEFAULT_LAG_MIN_EVENTS = 5
REASON_TEXT = {
    "ingame_disabled": "in-game trading is off for this assignment", "ingame_paper_only": "in-game orders are paper only",
    "ingame_stale": "game state too old", "ingame_quiet": "too soon after a score or possession change",
    "ingame_cutoff": "inside the end-of-game cutoff", "ingame_lag_suspended": "feed lag suspends in-game buys",
}


def _number(conn: psycopg.Connection, key: str, default: float) -> float:
    try:
        return float(get_setting(conn, key, default))
    except (TypeError, ValueError):
        return default


def _minsec(seconds: Any) -> str:
    s = max(0, int(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


def _period(period: Any) -> str:
    p = int(period or 1)
    if p >= 5:
        return "OT" if p == 5 else f"OT{p - 4}"
    return f"Q{p}"


def clock_label(state: dict[str, Any]) -> str:
    """"Q3 4:12 · 17-14" (away-home, as the row's "away @ home" heading) or Half,
    End Q1, Final, Pre-game with the score."""
    status = state.get("status")
    if status == "pre":
        when = "Pre-game"
    elif status == "half":
        when = "Half"
    elif status == "end_period":
        when = f"End {_period(state.get('period'))}"
    elif status == "final":
        when = "Final"
    else:
        when = f"{_period(state.get('period'))} {_minsec(state.get('clock_seconds'))}"
    away, home = state.get("away_score"), state.get("home_score")
    if away is None or home is None:
        return when
    return f"{when} · {int(away)}-{int(home)}"


def ingame_models(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """ingame_wp models of non-retired lineages for the in-game selects, newest first."""
    rows = conn.execute(
        """
        SELECT m.id, m.family, m.params, m.status, m.artifact IS NOT NULL AS trained FROM models m
         WHERE m.family = %s
           AND NOT EXISTS (SELECT 1 FROM models r WHERE r.lineage_id = m.lineage_id AND r.status = 'retired')
         ORDER BY m.created_at DESC, m.id LIMIT 200
        """,
        (INGAME_FAMILY,),
    ).fetchall()
    return [
        {"id": str(r["id"]), "label": f"{INGAME_FAMILY} · {short_params(r['family'], r['params'])} · {label(r['status'], 'model')}"
                                     f"{'' if r['trained'] else ' · untrained'} · {str(r['id'])[:8]}"}
        for r in rows
    ]


def pregame_p_home(conn: psycopg.Connection, game_id: str) -> float | None:
    """The devigged closing moneyline of the game, else the frozen closing price of its
    home market (1 - the away market's)."""
    game = conn.execute("SELECT home_moneyline, away_moneyline FROM games WHERE game_id = %s", (game_id,)).fetchone()
    p = devig(game["home_moneyline"], game["away_moneyline"]) if game else None
    if p is not None:
        return float(p)
    row = conn.execute(
        """
        SELECT side, closing_price FROM markets
         WHERE game_id = %s AND mapping_confirmed AND closing_price IS NOT NULL ORDER BY side = 'home' DESC LIMIT 1
        """,
        (game_id,),
    ).fetchone()
    if row is None:
        return None
    price = float(row["closing_price"])
    return price if row["side"] == "home" else 1.0 - price


def home_mid(markets: list[dict[str, Any]], game_id: str) -> float | None:
    """The home market's mid (or 1 - the away market's) from the confirmed markets."""
    for side in ("home", "away"):
        for m in markets:
            if m.get("game_id") == game_id and m.get("side") == side and m.get("best_bid") is not None and m.get("best_ask") is not None:
                mid = (float(m["best_bid"]) + float(m["best_ask"])) / 2
                return mid if side == "home" else 1.0 - mid
    return None


def _load_model(conn: psycopg.Connection, model_id: Any, cache: dict[str, Any]) -> dict[str, Any] | None:
    key = str(model_id)
    if key not in cache:
        row = conn.execute("SELECT id, family, params, artifact FROM models WHERE id = %s", (model_id,)).fetchone()
        model = None
        if row is not None and row["family"] == INGAME_FAMILY and isinstance(row["artifact"], dict):
            try:
                model = IngameWP.from_json(row["params"] or {}, row["artifact"])
            except (ValueError, TypeError, KeyError):
                model = None
        cache[key] = None if row is None else {"row": dict(row), "model": model}
    return cache[key]


def assignment_ingame(conn: psycopg.Connection, a: dict[str, Any], markets: list[dict[str, Any]],
                      max_age: float, cache: dict[str, Any]) -> dict[str, Any]:
    """One assignment's in-game cell: switch, model, game state label, model p vs mid."""
    flags = a if "trade_ingame" in a else conn.execute(
        "SELECT ingame_model_id, trade_ingame FROM assignments WHERE id = %s", (a["id"],)
    ).fetchone()
    model_id = flags["ingame_model_id"] if flags else None
    loaded = _load_model(conn, model_id, cache) if model_id else None
    out: dict[str, Any] = {
        "model_id": str(model_id) if model_id else None,
        "model_label": short_params(INGAME_FAMILY, loaded["row"]["params"]) if loaded else None,
        "trade_ingame": bool(flags and flags["trade_ingame"]),
        "enabled": bool(flags and flags["trade_ingame"] and model_id) and not model_retired(conn, model_id),
        "state": None, "label": None, "line": None, "age_s": None, "stale": False, "source": None,
        "p_home": None, "mid_home": home_mid(markets, a["game_id"]), "pregame_p_home": None,
    }
    latest = latest_state(conn, a["game_id"])
    if latest is not None:
        state = latest["state"]
        out.update(state=state, label=clock_label(state), age_s=latest["age_s"], source=latest["source"],
                   stale=float(latest["age_s"]) > max_age and state.get("status") != "final", line=f"{clock_label(state)} · {ago(latest['age_s'])}")
        if loaded and loaded["model"] is not None and state.get("status") in ("in", "half", "end_period"):
            out["pregame_p_home"] = pregame_p_home(conn, a["game_id"])
            out["p_home"] = loaded["model"].predict(state, out["pregame_p_home"])
    out["visible"] = latest is not None or bool(model_id) or out["trade_ingame"]
    return out


def lag_text(name: str, summary: dict[str, Any], min_events: int) -> str:
    """"ESPN: median 6 s behind the market over 12 events" or "ESPN: not enough data"."""
    n = int(summary.get("n") or 0)
    median = summary.get("median_lag_s")
    if n < min_events or median is None:
        return f"{name}: not enough data ({n} of {min_events} events measured)"
    direction = "behind" if median >= 0 else "ahead of"
    return f"{name}: median {abs(float(median)):.0f} s {direction} the market over {n} event{'' if n == 1 else 's'}"


def lag_short(name: str, summary: dict[str, Any], min_events: int) -> str:
    """"ESPN 6 s behind" (or "ESPN 3 of 5 events" below the minimum) for the closed group's summary."""
    n = int(summary.get("n") or 0)
    median = summary.get("median_lag_s")
    if n < min_events or median is None:
        return f"{name} {n} of {min_events} events"
    return f"{name} {abs(float(median)):.0f} s {'behind' if median >= 0 else 'ahead'}"


def feed_block(conn: psycopg.Connection) -> dict[str, Any]:
    """The "In-game feed" block: one line per source plus the suspension state."""
    lag = lag_status(conn)
    min_events = int(_number(conn, "ingame_lag_min_events", DEFAULT_LAG_MIN_EVENTS))
    max_lag = _number(conn, "ingame_max_lag_s", 20.0)
    sources = []
    order = list(SOURCE_NAMES)  # ESPN, then its scoreboard fallback, then Yahoo; unknown names last
    by_source = lag.get("by_source") or {}
    for source, summary in sorted(by_source.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else len(order), kv[0])):
        name = SOURCE_NAMES.get(source, source)
        sources.append({"source": source, "name": name, "text": lag_text(name, summary, min_events),
                        "short": lag_short(name, summary, min_events), "suspended": bool(summary.get("suspended"))})
    configured = get_setting(conn, "gamestate_sources", ["espn"])
    events = conn.execute(
        """
        SELECT DISTINCT g.raw->>'espn' AS event_id, g.away_team, g.home_team, g.kickoff_at FROM games g
          JOIN assignments a ON a.game_id = g.game_id AND a.status IN ('active', 'halted')
         WHERE g.status <> 'final' AND g.raw->>'espn' IS NOT NULL ORDER BY g.kickoff_at LIMIT 32
        """
    ).fetchall()
    return {
        "suspended": bool(lag.get("suspended")), "median_lag_s": lag.get("median_lag_s"), "n": int(lag.get("n") or 0),
        "enough": int(lag.get("n") or 0) >= min_events, "min_events": min_events, "max_lag_s": max_lag,
        "sources": sources, "configured": configured if isinstance(configured, list) else ["espn"],
        "probe_events": [{"event_id": r["event_id"], "label": f"{r['away_team']} @ {r['home_team']}"} for r in events],
    }


def annotate_fills(conn: psycopg.Connection, fills: list[dict[str, Any]]) -> None:
    """Set fill["ingame"] from its order when the fill query did not carry it."""
    missing = [f["order_id"] for f in fills if "ingame" not in f]
    if not missing:
        return
    rows = conn.execute("SELECT id, ingame FROM orders WHERE id = ANY(%s)", (missing,)).fetchall()
    flags = {r["id"]: bool(r["ingame"]) for r in rows}
    for f in fills:
        f.setdefault("ingame", flags.get(f["order_id"], False))


def ingame_live(conn: psycopg.Connection, ctx: dict[str, Any]) -> dict[str, Any]:
    """Annotate the live region's assignments and fills; return the feed block."""
    max_age = _number(conn, "ingame_max_state_age_s", DEFAULT_MAX_STATE_AGE_S)
    cache: dict[str, Any] = {}
    markets = ctx.get("markets") or []
    for a in ctx.get("assignments") or []:
        a["ingame"] = assignment_ingame(conn, a, markets, max_age, cache)
    annotate_fills(conn, ctx.get("fills") or [])
    return {"ingame_feed": feed_block(conn), "ingame_models": ingame_models(conn), "ingame_max_age_s": max_age}
