"""The recorded prices feed for snapshot replay backtests (docs/ROBUSTNESS.md B1):
the body and ETag of GET /api/v1/data/prices.

Body: {"markets": [{"market_id", "game_id", "side", "platform", "confirmed",
"closing_price", "kickoff_at", "bars": [[minute, bid, ask, close,
min_liquidity_usd_cents], ...], "depth": [[ts, bid_depth, ask_depth], ...]}], "count"}.

- Markets: confirmed mappings with a side, of games whose kickoff_at >= since (every
  game when since is absent). platform names one platform; without it every platform
  except "sim" is served, so simulated prices only reach a worker that asks for them.
- Bars and depth lie inside [kickoff - 6 h, kickoff). Bars are the 1-minute price_bars
  merged with bars built the same way (last mid, bid and ask; least liquidity) from the
  raw snapshots not rolled up yet (the retention window); where a minute has both, the
  raw snapshot's close, bid and ask win (they are the later ones) and the liquidity is
  the least of the two. Depth is the raw snapshots thinned to the last one per minute.
- ETag: price_bars count and max minute, max price_snapshots id, max markets
  updated_at, the games stamp (kickoff moves the window) and a hash of the query.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import psycopg

from host import nflverse
from host.errors import BadRequest
from host.signals import iso

WINDOW = "6 hours"
SIM_PLATFORM = "sim"
MAX_PLATFORM_LEN = 64


def parse_since(value: str | None) -> datetime | None:
    """An ISO date or datetime (naive means UTC) as an aware datetime; 400 when bad."""
    text = (value or "").strip()
    if not text:
        return None
    if "T" in text:  # an unencoded "+00:00" offset arrives as a space
        text = text.replace(" ", "+")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise BadRequest(f"since is not an ISO date: {value!r}") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def check_platform(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > MAX_PLATFORM_LEN or not all(c.isalnum() or c in "_-." for c in text):
        raise BadRequest("platform is not a platform name")
    return text


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def _stamp(value: Any) -> str:
    return "0" if value is None else f"{value.timestamp():.6f}"


def prices_etag(conn: psycopg.Connection, since: datetime | None, platform: str | None) -> str:
    """One string that changes whenever the served data or the query can change."""
    bars = conn.execute("SELECT count(*) AS n, max(minute) AS t FROM price_bars").fetchone()
    snap = conn.execute("SELECT max(id) AS id FROM price_snapshots").fetchone()["id"]
    mk = conn.execute("SELECT max(updated_at) AS t FROM markets").fetchone()["t"]
    query = hashlib.sha256(f"{since.isoformat() if since else ''}|{platform or ''}".encode()).hexdigest()[:10]
    return f"{bars['n']}-{_stamp(bars['t'])}-{snap or 0}-{_stamp(mk)}-{nflverse.games_etag(conn)}-{query}"


def _markets(conn: psycopg.Connection, since: datetime | None, platform: str | None) -> list[dict[str, Any]]:
    where = ["m.mapping_confirmed", "m.side IN ('home', 'away')"]
    args: list[Any] = []
    if since is not None:
        where.append("g.kickoff_at >= %s")
        args.append(since)
    if platform is None:
        where.append("m.platform <> %s")
        args.append(SIM_PLATFORM)
    else:
        where.append("m.platform = %s")
        args.append(platform)
    return conn.execute(
        "SELECT m.id, m.game_id, m.side, m.platform, m.mapping_confirmed, m.closing_price, g.kickoff_at"
        " FROM markets m JOIN games g ON g.game_id = m.game_id WHERE " + " AND ".join(where)
        + " ORDER BY g.kickoff_at, m.game_id, m.side, m.platform, m.id",
        args,
    ).fetchall()


BARS_SQL = f"""
WITH win AS (
  SELECT m.id AS market_id, g.kickoff_at FROM markets m JOIN games g ON g.game_id = m.game_id
   WHERE m.id = ANY(%(ids)s)
), raw AS (
  SELECT s.market_id, date_trunc('minute', s.ts) AS minute,
         (array_agg(s.mid ORDER BY s.ts DESC, s.id DESC))[1] AS close,
         (array_agg(s.bid ORDER BY s.ts DESC, s.id DESC))[1] AS bid,
         (array_agg(s.ask ORDER BY s.ts DESC, s.id DESC))[1] AS ask,
         min(s.liquidity_usd_cents) AS min_liq
    FROM price_snapshots s JOIN win w ON w.market_id = s.market_id
   WHERE s.mid IS NOT NULL AND s.ts >= w.kickoff_at - interval '{WINDOW}' AND s.ts < w.kickoff_at
   GROUP BY 1, 2
), bars AS (
  SELECT b.market_id, b.minute, b.close, b.bid, b.ask, b.min_liquidity_usd_cents AS min_liq
    FROM price_bars b JOIN win w ON w.market_id = b.market_id
   WHERE b.minute >= w.kickoff_at - interval '{WINDOW}' AND b.minute < w.kickoff_at
)
SELECT COALESCE(r.market_id, b.market_id) AS market_id, COALESCE(r.minute, b.minute) AS minute,
       COALESCE(r.bid, b.bid) AS bid, COALESCE(r.ask, b.ask) AS ask, COALESCE(r.close, b.close) AS close,
       LEAST(r.min_liq, b.min_liq) AS min_liq
  FROM raw r FULL JOIN bars b ON b.market_id = r.market_id AND b.minute = r.minute
 ORDER BY 1, 2
"""

DEPTH_SQL = f"""
SELECT DISTINCT ON (s.market_id, date_trunc('minute', s.ts)) s.market_id, s.ts, s.bid_depth, s.ask_depth
  FROM price_snapshots s JOIN markets m ON m.id = s.market_id JOIN games g ON g.game_id = m.game_id
 WHERE s.market_id = ANY(%(ids)s) AND s.ts >= g.kickoff_at - interval '{WINDOW}' AND s.ts < g.kickoff_at
 ORDER BY s.market_id, date_trunc('minute', s.ts), s.ts DESC, s.id DESC
"""


def prices(conn: psycopg.Connection, since: datetime | None = None, platform: str | None = None) -> dict[str, Any]:
    """The feed body as a dict."""
    markets = _markets(conn, since, platform)
    ids = [m["id"] for m in markets]
    bars: dict[Any, list[list[Any]]] = {i: [] for i in ids}
    depth: dict[Any, list[list[Any]]] = {i: [] for i in ids}
    if ids:
        for r in conn.execute(BARS_SQL, {"ids": ids}).fetchall():
            liq = None if r["min_liq"] is None else int(r["min_liq"])
            bars[r["market_id"]].append([iso(r["minute"]), _num(r["bid"]), _num(r["ask"]), _num(r["close"]), liq])
        for r in conn.execute(DEPTH_SQL, {"ids": ids}).fetchall():
            depth[r["market_id"]].append([iso(r["ts"]), r["bid_depth"] or [], r["ask_depth"] or []])
    rows = [{
        "market_id": str(m["id"]), "game_id": m["game_id"], "side": m["side"], "platform": m["platform"],
        "confirmed": bool(m["mapping_confirmed"]), "closing_price": _num(m["closing_price"]),
        "kickoff_at": iso(m["kickoff_at"]), "bars": bars[m["id"]], "depth": depth[m["id"]],
    } for m in markets]
    return {"markets": rows, "count": len(rows)}


def dumps_prices(conn: psycopg.Connection, since: datetime | None, platform: str | None) -> bytes:
    """The response body of GET /api/v1/data/prices."""
    return json.dumps(prices(conn, since, platform), separators=(",", ":")).encode("utf-8")
