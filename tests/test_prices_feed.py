"""GET /api/v1/data/prices: the recorded prices feed for snapshot replay (docs/ROBUSTNESS.md B1)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

from host import prices_feed
from host.errors import BadRequest
from tests.conftest import ingest_fixture

GAME = "2023_01_DET_KC"
OLD_GAME = "2022_18_KC_LV"
URL = "/api/v1/data/prices"


def market(conn, ref, game=GAME, side="home", platform="polymarket_us", confirmed=True, closing=None):
    return conn.execute(
        "INSERT INTO markets (platform, market_ref, title, game_id, side, mapping_confirmed, closing_price)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (platform, ref, ref, game, side, confirmed, closing),
    ).fetchone()["id"]


def bar(conn, market_id, minute, close, bid=None, ask=None, liq=5000):
    conn.execute(
        "INSERT INTO price_bars (market_id, minute, open, high, low, close, bid, ask, min_liquidity_usd_cents, n)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)",
        (market_id, minute, close, close, close, close, bid, ask, liq),
    )


def snap(conn, market_id, ts, bid, ask, liq=7000):
    return conn.execute(
        "INSERT INTO price_snapshots (market_id, ts, bid, ask, mid, bid_depth, ask_depth, liquidity_usd_cents)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (market_id, ts, bid, ask, round((bid + ask) / 2, 4), Jsonb([[bid, 100]]), Jsonb([[ask, 80], [ask + 0.01, 50]]), liq),
    ).fetchone()["id"]


@pytest.fixture
def setup(conn):
    ingest_fixture(conn)
    kickoff = conn.execute("SELECT kickoff_at FROM games WHERE game_id = %s", (GAME,)).fetchone()["kickoff_at"]
    ids = {
        "home": market(conn, "det-kc-home", closing=0.62),
        "away": market(conn, "det-kc-away", side="away"),
        "unconfirmed": market(conn, "det-kc-guess", confirmed=False),
        "sim": market(conn, "sim-det-kc", platform="sim"),
        "old": market(conn, "kc-lv-home", game=OLD_GAME),
    }
    home = ids["home"]
    bar(conn, home, kickoff - timedelta(hours=7), 0.50)                       # before the window
    bar(conn, home, kickoff - timedelta(hours=6), 0.55, 0.54, 0.56)           # first minute inside
    bar(conn, home, kickoff - timedelta(minutes=61), 0.58, 0.57, 0.59, 4000)
    bar(conn, home, kickoff, 0.70)                                            # at kickoff: outside
    bar(conn, home, kickoff + timedelta(minutes=5), 0.80)
    minute = kickoff - timedelta(minutes=61)
    snap(conn, home, minute + timedelta(seconds=10), 0.56, 0.58, 3000)        # same minute as a bar
    snap(conn, home, minute + timedelta(seconds=40), 0.57, 0.60, 9000)        # last of that minute
    snap(conn, home, kickoff - timedelta(minutes=30, seconds=50), 0.60, 0.62)
    snap(conn, home, kickoff + timedelta(seconds=1), 0.65, 0.67)              # after kickoff: outside
    bar(conn, ids["sim"], kickoff - timedelta(minutes=60), 0.40, 0.39, 0.41)
    bar(conn, ids["unconfirmed"], kickoff - timedelta(minutes=60), 0.40, 0.39, 0.41)
    return {"kickoff": kickoff, **ids}


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def test_only_confirmed_markets_and_the_pre_kickoff_window(client, conn, make_worker, setup):
    worker = make_worker()
    r = client.get(URL, headers=worker.headers)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and r.headers["etag"]
    body = r.json()
    markets = {m["market_id"]: m for m in body["markets"]}
    assert body["count"] == 3 and set(markets) == {str(setup["home"]), str(setup["away"]), str(setup["old"])}
    home = markets[str(setup["home"])]
    assert {k: home[k] for k in ("game_id", "side", "platform", "confirmed", "closing_price", "kickoff_at")} == {
        "game_id": GAME, "side": "home", "platform": "polymarket_us", "confirmed": True, "closing_price": 0.62,
        "kickoff_at": iso(setup["kickoff"])}
    k = setup["kickoff"]
    assert home["bars"] == [
        [iso(k - timedelta(hours=6)), 0.54, 0.56, 0.55, 5000],
        [iso(k - timedelta(minutes=61)), 0.57, 0.6, 0.585, 3000],   # raw snapshot wins, least liquidity
        [iso(k - timedelta(minutes=31)), 0.6, 0.62, 0.61, 7000],    # built from raw snapshots only
    ]
    assert home["depth"] == [
        [iso(k - timedelta(minutes=61) + timedelta(seconds=40)), [[0.57, 100]], [[0.6, 80], [0.61, 50]]],
        [iso(k - timedelta(minutes=30, seconds=50)), [[0.6, 100]], [[0.62, 80], [0.63, 50]]],
    ]
    assert markets[str(setup["away"])]["bars"] == [] and markets[str(setup["away"])]["closing_price"] is None


def test_since_and_platform_filters(client, make_worker, setup):
    worker = make_worker()
    body = client.get(URL, params={"since": "2023-09-01"}, headers=worker.headers).json()
    assert {m["game_id"] for m in body["markets"]} == {GAME}, "the 2022 game kicked off before since"
    body = client.get(URL, params={"since": "2023-09-01T00:00:00+00:00"}, headers=worker.headers).json()
    assert body["count"] == 2
    sim = client.get(URL, params={"platform": "sim"}, headers=worker.headers).json()
    assert [m["market_id"] for m in sim["markets"]] == [str(setup["sim"])] and sim["markets"][0]["platform"] == "sim"
    assert len(sim["markets"][0]["bars"]) == 1
    named = client.get(URL, params={"platform": "polymarket_us"}, headers=worker.headers).json()
    assert named["count"] == 3 and all(m["platform"] == "polymarket_us" for m in named["markets"])
    assert client.get(URL, params={"since": "yesterday"}, headers=worker.headers).status_code == 400
    assert client.get(URL, params={"platform": "a b"}, headers=worker.headers).status_code == 400
    assert client.get(URL).status_code == 401


def test_etag_304_and_changes(client, conn, make_worker, setup):
    worker = make_worker()
    first = client.get(URL, headers=worker.headers)
    etag = first.headers["etag"]
    again = client.get(URL, headers={**worker.headers, "If-None-Match": etag})
    assert again.status_code == 304 and again.headers["etag"] == etag and again.content == b""
    other = client.get(URL, params={"platform": "sim"}, headers={**worker.headers, "If-None-Match": etag})
    assert other.status_code == 200, "another query never answers 304 to this one's tag"
    snap(conn, setup["away"], setup["kickoff"] - timedelta(minutes=45), 0.38, 0.40)
    moved = client.get(URL, headers={**worker.headers, "If-None-Match": etag})
    assert moved.status_code == 200 and moved.headers["etag"] != etag, "a new snapshot moves the tag"
    etag = moved.headers["etag"]
    bar(conn, setup["away"], setup["kickoff"] - timedelta(minutes=90), 0.41)
    assert client.get(URL, headers={**worker.headers, "If-None-Match": etag}).headers["etag"] != etag
    etag = client.get(URL, headers=worker.headers).headers["etag"]
    conn.execute("UPDATE markets SET closing_price = 0.4, updated_at = clock_timestamp() WHERE id = %s", (setup["away"],))
    assert client.get(URL, headers={**worker.headers, "If-None-Match": etag}).status_code == 200


def test_parse_since():
    assert prices_feed.parse_since(None) is None and prices_feed.parse_since("") is None
    assert prices_feed.parse_since("2024-09-01").isoformat() == "2024-09-01T00:00:00+00:00"
    assert prices_feed.parse_since("2024-09-01T12:00:00Z").isoformat() == "2024-09-01T12:00:00+00:00"
    assert prices_feed.parse_since("2024-09-01T12:00:00 00:00").isoformat() == "2024-09-01T12:00:00+00:00"
    with pytest.raises(BadRequest):
        prices_feed.parse_since("09/01/2024")
