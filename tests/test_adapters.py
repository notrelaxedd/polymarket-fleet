"""Market source adapters: sim determinism and shape, the Polymarket parsers against
fixture JSON (including malformed payloads), the team alias table. No database."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from host.exchange.adapters import make_source, teams
from host.exchange.adapters.base import Book, NotConfigured, OrderGateway, PaperGateway, SourceError, clean_levels, parse_time
from host.exchange.adapters import polymarket_clob, polymarket_us
from host.exchange.adapters.sim import SimSource, build_book, home_mid, market_ref, parse_ref

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)


def _game(game_id: str = "2026_05_KC_LV", home: str = "LV", away: str = "KC", hours: float = 48, **extra):
    row = {
        "game_id": game_id, "home_team": home, "away_team": away, "kickoff_at": NOW + timedelta(hours=hours),
        "home_moneyline": 150, "away_moneyline": -175, "status": "scheduled", "gameday": (NOW + timedelta(hours=hours)).date(),
    }
    row.update(extra)
    return row


# ------------------------------------------------------------------------- sim

def test_sim_lists_two_markets_per_game_within_lookahead():
    src = SimSource(clock=lambda: NOW)
    games = [_game(), _game("2026_06_SF_SEA", "SEA", "SF", hours=24 * 12), _game("2026_04_DAL_PHI", "PHI", "DAL", hours=-30, status="final")]
    infos = src.list_markets(games, 8)
    assert [(i.market_ref, i.side) for i in infos] == [("sim:2026_05_KC_LV:home", "home"), ("sim:2026_05_KC_LV:away", "away")]
    assert infos[0].home_team == "LV" and infos[0].away_team == "KC" and infos[0].platform == "sim"
    assert infos[0].kickoff_at == games[0]["kickoff_at"] and infos[0].tick == 0.01 and infos[0].min_size == 1
    assert len(src.list_markets(games, 20)) == 4, "the far game enters with a longer lookahead"


def test_sim_books_are_deterministic_per_minute_and_well_formed():
    game = _game()
    a = build_book(game, "home", NOW)
    b = build_book(game, "home", NOW + timedelta(seconds=30))
    c = build_book(game, "home", NOW + timedelta(minutes=1))
    assert a.bids == b.bids and a.asks == b.asks, "same minute, same book"
    assert a.asks != c.asks or a.bids != c.bids, "the walk moves between minutes"
    assert len(a.bids) == 5 and len(a.asks) == 5
    assert all(a.bids[i][0] > a.bids[i + 1][0] for i in range(4)) and all(a.asks[i][0] < a.asks[i + 1][0] for i in range(4))
    assert round(a.best_ask - a.best_bid, 4) == 0.02
    assert all(50 <= s <= 500 for _, s in a.bids + a.asks)
    assert 0.03 <= a.mid <= 0.97
    away = build_book(game, "away", NOW)
    assert round(a.mid + away.mid, 4) == 1.0, "home and away mids are complements"


def test_sim_mid_starts_from_the_devigged_moneyline_and_is_clamped():
    game = _game()
    mids = [home_mid(game, NOW + timedelta(minutes=m)) for m in range(50)]
    assert all(0.03 <= m <= 0.97 for m in mids)
    assert abs(sum(mids) / len(mids) - 0.39) < 0.08, "LV at +150 vs KC -175 is around 0.39 to win"
    missing = _game(home_moneyline=None)
    assert abs(home_mid(missing, NOW) - 0.5) <= 0.005 * 24
    strong = _game(home_moneyline=-5000, away_moneyline=2500)
    assert build_book(strong, "home", NOW).best_ask <= 0.99 and build_book(strong, "away", NOW).best_bid >= 0.01


def test_sim_fetch_book_needs_a_known_game_and_a_sim_ref():
    src = SimSource([_game()], clock=lambda: NOW)
    book = src.fetch_book(market_ref("2026_05_KC_LV", "away"))
    assert isinstance(book, Book) and book.fetched_at == NOW
    with pytest.raises(SourceError):
        src.fetch_book("sim:unknown:home")
    with pytest.raises(SourceError):
        parse_ref("polymarket:123")
    probe = src.probe()
    assert probe["status"] == 200 and json.loads(probe["payload"])["markets"][0]["market_ref"] == "sim:2026_05_KC_LV:home"


def test_make_source_by_name():
    assert make_source("sim").name == "sim"
    assert make_source("polymarket_us", {"polymarket_us": {"base_url": "https://x.test"}}).markets_url() == "https://x.test/v1/markets?sport=nfl"
    assert make_source("polymarket_clob", {}).events_url() == "https://gamma-api.polymarket.com/events?tag_slug=nfl&closed=false&limit=200"
    with pytest.raises(SourceError):
        make_source("kalshi")


def test_gateways():
    paper = PaperGateway()
    assert paper.place({"client_request_id": "abc"}) == "paper:abc" and paper.cancel({}) is True and paper.open_orders() == []
    live = OrderGateway()
    for call in (lambda: live.place({}), lambda: live.cancel({}), live.open_orders, lambda: live.fills(None), live.balance):
        with pytest.raises(NotConfigured):
            call()


# ---------------------------------------------------------------------- common

def test_clean_levels_and_parse_time():
    levels = clean_levels([["0.5", "10"], {"price": 0.6, "size": 5}, [1.5, 10], [0.4, 0], "x", [0.3], {"price": "nan", "size": 1}], descending=True)
    assert levels == [[0.6, 5.0], [0.5, 10.0]]
    assert clean_levels([[0.01 * i, 1] for i in range(1, 30)], descending=False) == [[round(0.01 * i, 10), 1.0] for i in range(1, 11)]
    assert clean_levels(None, True) == [] and clean_levels("bids", True) == []
    assert parse_time("2026-10-05T20:15:00Z") == datetime(2026, 10, 5, 20, 15, tzinfo=timezone.utc)
    assert parse_time("2026-10-05 20:15:00+00") == datetime(2026, 10, 5, 20, 15, tzinfo=timezone.utc)
    assert parse_time(1791232500) == datetime(2026, 10, 5, 20, 35, tzinfo=timezone.utc)
    assert parse_time("not a date") is None and parse_time(None) is None and parse_time(True) is None
    assert parse_time("2026-10-05T16:15:00-04:00") == datetime(2026, 10, 5, 20, 15, tzinfo=timezone.utc)


# --------------------------------------------------------------- polymarket_us

def test_polymarket_us_markets_fixture():
    infos = polymarket_us.parse_markets((FIXTURES / "polymarket_us_markets.json").read_text())
    by_ref = {i.market_ref: i for i in infos}
    assert set(by_ref) == {"pmus-kc-lv-kc", "pmus-kc-lv-lv", "pmus-title-only", "pmus-no-teams", "pmus-bad-fields"}
    kc = by_ref["pmus-kc-lv-kc"]
    assert (kc.home_team, kc.away_team, kc.side) == ("LV", "KC", "away")
    assert kc.kickoff_at == datetime(2026, 10, 5, 20, 15, tzinfo=timezone.utc) and kc.tick == 0.01 and kc.min_size == 5
    assert kc.event_ref == "pmus-ev-1" and kc.platform == "polymarket_us" and kc.raw["id"] == "pmus-kc-lv-kc"
    lv = by_ref["pmus-kc-lv-lv"]
    assert (lv.home_team, lv.away_team, lv.side, lv.min_size) == ("LV", "KC", "home", 5)
    title_only = by_ref["pmus-title-only"]
    assert (title_only.home_team, title_only.away_team) == ("DAL", "PHI"), "teams from the title, away first"
    assert title_only.side is None, "the YES team is unknown from a question with two teams"
    assert title_only.kickoff_at == datetime(2026, 10, 5, 20, 35, tzinfo=timezone.utc)
    none = by_ref["pmus-no-teams"]
    assert none.home_team is None and none.kickoff_at is None and none.tick == 0.01 and none.min_size == 1
    bad = by_ref["pmus-bad-fields"]
    assert (bad.home_team, bad.away_team, bad.side, bad.tick) == ("GB", "CHI", "away", 0.01)


def test_polymarket_us_field_names_come_from_config():
    payload = json.dumps({"data": [{"marketId": "m1", "question": "Bears vs Packers", "yes_team": "Packers", "startDate": "2026-10-04T17:00:00Z"}]})
    infos = polymarket_us.parse_markets(payload, {"fields": {"id": "marketId", "outcome": ["yes_team"]}})
    assert len(infos) == 1 and infos[0].market_ref == "m1" and infos[0].side == "home"
    assert (infos[0].home_team, infos[0].away_team) == ("GB", "CHI")


@pytest.mark.parametrize("text", ["", "null", "{}", "[]", "{\"markets\": \"no\"}", "not json", "[1, 2, \"x\"]"])
def test_polymarket_us_malformed_markets_payloads_give_nothing(text):
    assert polymarket_us.parse_markets(text) == []


def test_polymarket_us_book_fixture():
    book = polymarket_us.parse_book((FIXTURES / "polymarket_us_book.json").read_text(), now=NOW)
    assert book.bids == [[0.57, 80.0], [0.55, 120.0], [0.5, 900.0]]
    assert book.asks == [[0.59, 150.0], [0.61, 70.0], [0.7, 1000.0]]
    assert book.best_bid == 0.57 and book.best_ask == 0.59 and round(book.mid, 4) == 0.58 and book.fetched_at == NOW
    for text in ("not json", "[]", "{}", "{\"book\": 1}"):
        with pytest.raises(SourceError):
            polymarket_us.parse_book(text)
    empty = polymarket_us.parse_book("{\"bids\": [], \"asks\": \"x\"}")
    assert empty.bids == [] and empty.asks == [] and empty.mid is None


def test_polymarket_us_urls():
    src = polymarket_us.PolymarketUSSource({"base_url": "https://gw.test/", "markets_path": "/m", "book_path": "/m/{market_ref}/b", "sport_query": ""})
    assert src.markets_url() == "https://gw.test/m" and src.book_url("abc") == "https://gw.test/m/abc/b"


# ------------------------------------------------------------- polymarket_clob

def test_polymarket_clob_events_fixture():
    infos = polymarket_clob.parse_events((FIXTURES / "polymarket_clob_events.json").read_text())
    by_ref = {i.market_ref: i for i in infos}
    assert set(by_ref) == {"111111", "222222", "555555", "666666"}, "the total, the malformed and the closed markets are skipped"
    kc = by_ref["111111"]
    assert (kc.home_team, kc.away_team, kc.side) == ("LV", "KC", "away"), "Gamma titles list the away team first"
    assert kc.kickoff_at == datetime(2026, 10, 5, 20, 15, tzinfo=timezone.utc) and kc.min_size == 5 and kc.tick == 0.01
    assert kc.event_ref == "90001" and kc.title == "Chiefs vs. Raiders [Chiefs]" and kc.raw["market_id"] == "500001"
    assert by_ref["222222"].side == "home"
    yes, no = by_ref["555555"], by_ref["666666"]
    assert (yes.home_team, yes.away_team) == ("DAL", "PHI")
    assert yes.side == "away" and no.side == "home", "Yes pays the Eagles (away), No the Cowboys"
    assert yes.kickoff_at == datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc), "falls back to the event start"


@pytest.mark.parametrize("text", ["", "not json", "{}", "[]", "{\"events\": 3}", "[{\"markets\": [{\"outcomes\": \"[]\"}]}]"])
def test_polymarket_clob_malformed_events_give_nothing(text):
    assert polymarket_clob.parse_events(text) == []


def test_polymarket_clob_book_fixture():
    book = polymarket_clob.parse_book((FIXTURES / "polymarket_clob_book.json").read_text(), now=NOW)
    assert book.bids == [[0.54, 100.0], [0.52, 250.5], [0.49, 2000.0]]
    assert book.asks == [[0.56, 60.0], [0.58, 400.0], [0.65, 1500.0]]
    assert round(book.mid, 4) == 0.55
    for text in ("[]", "{}", "oops"):
        with pytest.raises(SourceError):
            polymarket_clob.parse_book(text)
    src = polymarket_clob.PolymarketClobSource({"clob_url": "https://clob.test"})
    assert src.book_url("111111") == "https://clob.test/book?token_id=111111"


# ----------------------------------------------------------------------- teams

def test_teams_resolve_and_find():
    assert len(teams.TEAMS) == 32 and len(teams.CODES) == 32
    assert teams.resolve("Kansas City Chiefs") == "KC" and teams.resolve("chiefs") == "KC" and teams.resolve("KC") == "KC"
    assert teams.resolve("OAK") == "LV" and teams.resolve("SD") == "LAC" and teams.resolve("STL") == "LA"
    assert teams.resolve("LAR") == "LA" and teams.resolve("JAC") == "JAX" and teams.resolve("WSH") == "WAS"
    assert teams.resolve("Washington Football Team") == "WAS" and teams.resolve("Redskins") == "WAS"
    assert teams.resolve("San Francisco 49ers") == "SF" and teams.resolve("Niners") == "SF"
    assert teams.resolve("New York") is None and teams.resolve("Los Angeles") is None, "ambiguous cities"
    assert teams.resolve("New York Jets") == "NYJ" and teams.resolve("NY Giants") == "NYG"
    assert teams.resolve("Los Angeles Chargers") == "LAC" and teams.resolve("LA Rams") == "LA"
    assert teams.resolve("") is None and teams.resolve(None) is None and teams.resolve("Toronto Argonauts") is None
    assert teams.find("Chiefs vs. Raiders") == ["KC", "LV"]
    assert teams.find("Will the Los Angeles Rams beat the San Francisco 49ers?") == ["LA", "SF"]
    assert teams.find("Las Vegas Raiders at Kansas City Chiefs") == ["LV", "KC"]
    assert teams.find("Super Bowl winner") == []
    assert teams.find("Giants vs Jets") == ["NYG", "NYJ"]
    for code, city, nickname, _ in teams.TEAMS:
        assert teams.resolve(code) == code and teams.resolve(nickname) == code and teams.resolve(f"{city} {nickname}") == code
