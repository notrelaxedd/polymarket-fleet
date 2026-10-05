"""The live game-state feed: ESPN summary and scoreboard parsers, the poller (per-game
cadence, the global rate cap, jittered backoff, deduplication by play id, the
scoreboard fallback), latest_state, the exchange task and the probe.

The tests/fixtures/espn_summary_*.json payloads are UNVERIFIED: they were written from
ESPN's documented summary shapes because ESPN is not reachable from the sandbox. The
owner checks them with `python -m host.exchange.cli probe-gamestate --event <id>`.
"""
from __future__ import annotations

import copy
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from host.exchange import cli, gamestate, main, probe
from host.exchange.adapters.base import SourceError
from host.exchange.gamestate import PollerState, latest_state, parse_scoreboard_states, parse_summary, poll
from host.exchange.gamestate_parse import STATE_KEYS, raw_fragment, yardline_from_text
from tests.test_exchange import make_assignment, make_game, make_model

FIXTURES = Path(__file__).resolve().parent / "fixtures"
T0 = datetime(2026, 10, 5, 22, 45, tzinfo=timezone.utc)
KICKOFF = T0 - timedelta(hours=2, minutes=30)


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"espn_summary_{name}.json").read_text())


def situation(payload: Any) -> dict[str, Any]:
    rows = parse_summary(payload)
    assert rows and rows[-1]["play_id"] is None
    return {k: rows[-1][k] for k in STATE_KEYS}


def with_score(data: dict[str, Any], home: int, away: int) -> dict[str, Any]:
    data = copy.deepcopy(data)
    for entry in data["header"]["competitions"][0]["competitors"]:
        entry["score"] = str(home if entry["homeAway"] == "home" else away)
    return data


def live_game(conn: Any, game_id: str = "2026_05_KC_LV", espn: str | None = "401800001", status: str = "active",
              kickoff: datetime = KICKOFF) -> None:
    make_game(conn, game_id, "LV", "KC", kickoff)
    if espn is not None:
        conn.execute("UPDATE games SET raw = %s WHERE game_id = %s", (Jsonb({"espn": espn}), game_id))
    make_assignment(conn, game_id, make_model(conn), status=status)


class Feed:
    """A fake fetch: (status, text) per call, every call's (now, url) recorded."""

    def __init__(self, answer: Any = None) -> None:
        self.answer = answer if answer is not None else fixture("in")
        self.calls: list[tuple[datetime | None, str]] = []
        self.now: datetime | None = None

    def __call__(self, url: str) -> tuple[int, str]:
        self.calls.append((self.now, url))
        answer = self.answer(url) if callable(self.answer) else self.answer
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, tuple):
            return answer
        return 200, answer if isinstance(answer, str) else json.dumps(answer)


def run(conn: Any, feed: Feed, now: datetime, state: PollerState, rng: random.Random | None = None) -> dict[str, Any]:
    feed.now = now
    return poll(conn, now, feed, rng or random.Random("test"), state)


# ----------------------------------------------------------------------- parsers

def test_parse_in_progress_plays_then_situation():
    rows = parse_summary(json.dumps(fixture("in")))
    assert [r["play_id"] for r in rows] == ["401800001101", "401800001102", "401800001103", "401800001104", "401800001105", None]
    assert all(set(r) == set(STATE_KEYS) | {"play_id", "play_text", "event_ts"} for r in rows)
    punt, first = rows[1], rows[2]
    assert (punt["possession"], punt["down"], punt["distance"], punt["yardline_100"]) == ("home", 4, 4, 70)
    assert (first["possession"], first["yardline_100"], first["clock_seconds"], first["period"]) == ("away", 75, 647, 3)
    assert first["event_ts"] == datetime(2026, 10, 5, 22, 40, 58, tzinfo=timezone.utc) and "Kelce" in first["play_text"]
    assert situation(fixture("in")) == {
        "status": "in", "period": 3, "clock_seconds": 512, "home_score": 17, "away_score": 20, "possession": "away",
        "down": 2, "distance": 7, "yardline_100": 38, "home_timeouts": 2, "away_timeouts": 3}
    assert rows[-1]["event_ts"] == rows[-2]["event_ts"], "the situation carries the newest play's wallclock"
    assert parse_summary(fixture("in")) == rows, "a decoded payload parses the same"


def test_play_rows_carry_the_score_before_the_play():
    data = fixture("in")
    plays = data["drives"]["current"]["plays"]
    plays[-1]["homeScore"], plays[-1]["text"] = 17, "touchdown"
    plays.append({**copy.deepcopy(plays[-1]), "id": "401800001106", "homeScore": 17, "awayScore": 27})
    rows = parse_summary(with_score(data, 17, 27))
    assert (rows[-2]["home_score"], rows[-2]["away_score"]) == (17, 20), "the scoring play's row is the state at its snap"
    assert (rows[-1]["home_score"], rows[-1]["away_score"]) == (17, 27), "the situation has the new score"


def test_parse_pre_overtime_halftime_missing_situation_and_final():
    assert parse_summary(fixture("pre")) == [{
        "status": "pre", "period": None, "clock_seconds": None, "home_score": 0, "away_score": 0, "possession": None,
        "down": None, "distance": None, "yardline_100": None, "home_timeouts": None, "away_timeouts": None,
        "play_id": None, "play_text": None, "event_ts": None}]
    ot = situation(fixture("overtime"))
    assert (ot["period"], ot["clock_seconds"], ot["possession"], ot["yardline_100"], ot["away_timeouts"]) == (5, 372, "home", 49, 1)
    kickoff = parse_summary(fixture("overtime"))[0]
    assert (kickoff["down"], kickoff["distance"]) == (None, None), "a kickoff has no down"
    half = fixture("in")
    half["header"]["competitions"][0]["status"].update(
        {"period": 2, "clock": 0.0, "displayClock": "0:00", "type": {"name": "STATUS_HALFTIME", "state": "in", "completed": False}})
    del half["situation"]["possession"]
    assert (situation(half)["status"], situation(half)["period"], situation(half)["possession"]) == ("half", 2, None)
    half["header"]["competitions"][0]["status"]["type"]["name"] = "STATUS_END_PERIOD"
    assert situation(half)["status"] == "end_period"
    missing = parse_summary(fixture("no_situation"))
    assert len(missing) == 6 and situation(fixture("no_situation")) == {
        "status": "in", "period": 3, "clock_seconds": 512, "home_score": 17, "away_score": 20, "possession": None,
        "down": None, "distance": None, "yardline_100": None, "home_timeouts": None, "away_timeouts": None}
    assert missing[2]["possession"] == "away", "plays still carry their own situation"
    final = situation(fixture("final"))
    assert (final["status"], final["period"], final["clock_seconds"], final["home_score"], final["away_score"]) == ("final", 4, 0, 23, 27)


def test_garbage_never_raises():
    for payload in ("not json", "", "[]", "{}", b"\xff\xfe", None, 7, [], {"header": []}, {"header": {"competitions": [{}]}},
                    {"header": {"competitions": [{"status": {"type": {"state": "in"}}, "competitors": [{"homeAway": "home"}]}]}}):
        assert parse_summary(payload) == [], payload
        assert parse_scoreboard_states(payload) == {}
    junk = fixture("in")
    junk["situation"].update({"down": "x", "distance": None, "possession": {"weird": 1}, "homeTimeouts": 9})
    junk["drives"]["current"]["plays"][0].update({"period": "three", "clock": None, "start": "nope", "wallclock": 5e20})
    junk["drives"]["current"]["plays"].append("not a play")
    rows = parse_summary(junk)
    assert rows[-1]["possession"] is None and rows[-1]["home_timeouts"] is None and rows[2]["period"] is None


def test_yardline_from_possession_text_and_scoreboard_states():
    sides = {"home": {"ids": {"13"}, "code": "LV", "score": 0}, "away": {"ids": {"12"}, "code": "KC", "score": 0}}
    assert yardline_from_text("LV 38", "away", sides) == 38 and yardline_from_text("KC 25", "away", sides) == 75
    assert yardline_from_text("50", "home", sides) == 50 and yardline_from_text("LV 38", None, sides) is None
    assert yardline_from_text("XYZ 10", "home", sides) is None and yardline_from_text("", "home", sides) is None
    board = json.loads((FIXTURES / "espn_scoreboard.json").read_text())
    board["events"][1]["competitions"][0]["situation"] = {"down": 3, "distance": 2, "possession": "21", "possessionText": "DAL 30",
                                                          "homeTimeouts": 1, "awayTimeouts": 2}
    states = parse_scoreboard_states(json.dumps(board))
    assert set(states) == {"401800001", "401800002", "401800003", "401800004"}, "the event id matters, not the team codes"
    assert states["401800004"]["away_score"] is None, "a junk score stays unknown"
    live = states["401800002"]
    assert (live["status"], live["period"], live["clock_seconds"], live["home_score"], live["away_score"]) == ("in", 3, 512, 14, 20)
    assert (live["possession"], live["down"], live["distance"], live["yardline_100"], live["home_timeouts"]) == ("away", 3, 2, 30, 1)
    assert states["401800001"]["status"] == "final"


def test_raw_fragment_is_truncated_to_8_kb():
    assert raw_fragment({"a": 1}) == {"a": 1}
    big = raw_fragment({"text": "x" * 20000})
    assert big["truncated"] is True and len(big["text"]) == 8192


# ------------------------------------------------------------------------ poller

def test_poll_stores_plays_once_and_the_situation_on_change(conn):
    live_game(conn)
    feed, state = Feed(), PollerState()
    first = run(conn, feed, T0, state)
    assert first == {"polled": 1, "rows": 6, "backoff_until": None, "errors": []}
    assert feed.calls[0][1] == "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event=401800001"
    assert run(conn, feed, T0 + timedelta(seconds=2), state)["polled"] == 0, "the per-game cadence (4 s) is not up"
    assert run(conn, feed, T0 + timedelta(seconds=4), state) == {"polled": 1, "rows": 0, "backoff_until": None, "errors": []}
    data = fixture("in")
    data["drives"]["current"]["plays"].append({**copy.deepcopy(data["drives"]["current"]["plays"][-1]), "id": "401800001106"})
    data["header"]["competitions"][0]["status"].update({"clock": 470.0, "displayClock": "7:50"})
    feed.answer = data
    assert run(conn, feed, T0 + timedelta(seconds=8), state)["rows"] == 2, "the new play and the new situation"
    rows = conn.execute("SELECT * FROM game_state ORDER BY id").fetchall()
    assert len(rows) == 8 and len({r["play_id"] for r in rows if r["play_id"]}) == 6
    assert rows[0]["source"] == "espn_summary" and rows[0]["raw"]["id"] == "401800001101" and rows[0]["ts"] == T0
    latest = latest_state(conn, "2026_05_KC_LV", T0 + timedelta(seconds=10))
    assert latest["state"]["clock_seconds"] == 470 and latest["source"] == "espn_summary" and latest["age_s"] == 2.0
    assert latest["last_change"] is None and set(latest["state"]) == set(STATE_KEYS)
    assert latest_state(conn, "nope") is None


def test_poll_only_live_assigned_games_with_an_espn_id(conn):
    live_game(conn, "2026_05_A", espn="1")
    live_game(conn, "2026_05_B", espn="2", status="halted")
    live_game(conn, "2026_05_C", espn=None)
    live_game(conn, "2026_05_D", espn="4", status="cancelled")
    live_game(conn, "2026_05_E", espn="5", kickoff=T0 + timedelta(minutes=5))
    live_game(conn, "2026_05_F", espn="6", kickoff=T0 - timedelta(hours=9))
    live_game(conn, "2026_05_G", espn="7")
    conn.execute("UPDATE games SET status = 'final' WHERE game_id = '2026_05_G'")
    feed, state = Feed(), PollerState()
    assert [g["espn"] for g in gamestate.live_games(conn, T0)] == ["1", "2"]
    conn.execute("UPDATE settings SET value = %s WHERE key = 'gamestate_max_rps'", (Jsonb(2.0),))
    assert run(conn, feed, T0, state)["polled"] == 2 and sorted(url[-2:] for _, url in feed.calls) == ["=1", "=2"]
    feed.answer = fixture("final")
    assert run(conn, feed, T0 + timedelta(seconds=10), state)["rows"] == 6, "two plays and the final, per game"
    assert gamestate.live_games(conn, T0 + timedelta(seconds=30)) == [], "a final game_state stops the polling"


def test_six_live_games_never_exceed_the_rate_cap(conn):
    """A fake clock ticking every 250 ms for two minutes: every 1 s window holds at
    most gamestate_max_rps requests and each game waits max(poll_s, 6 / rps) seconds."""
    for i in range(6):
        live_game(conn, f"2026_05_G{i}", espn=str(100 + i))
    for rps, poll_s, window, cap, interval in ((1.0, 4, 1.0, 1, 6.0), (2.0, 3, 1.0, 2, 3.0), (0.5, 4, 2.0, 1, 12.0)):
        conn.execute("UPDATE settings SET value = %s WHERE key = 'gamestate_max_rps'", (Jsonb(rps),))
        conn.execute("UPDATE settings SET value = %s WHERE key = 'gamestate_poll_s'", (Jsonb(poll_s),))
        feed, state = Feed(), PollerState()
        for tick in range(4 * 120):
            run(conn, feed, T0 + timedelta(seconds=tick * 0.25), state)
        times = [c[0].timestamp() for c in feed.calls]
        for t in times:
            assert sum(1 for u in times if t - window < u <= t) <= cap, (rps, t)
        assert len(times) >= int(120 * rps) - 2, "the cap is used, not starved"
        for i in range(6):
            mine = [c[0].timestamp() for c in feed.calls if c[1].endswith(f"={100 + i}")]
            assert len(mine) >= 2 and min(b - a for a, b in zip(mine, mine[1:])) >= interval - 1e-6, (rps, i)


def test_429_and_403_back_off_with_jitter_never_a_tight_loop(conn):
    live_game(conn)
    feed, state, rng = Feed((429, "slow down")), PollerState(), random.Random("backoff")
    first = run(conn, feed, T0, state, rng)
    until = datetime.fromisoformat(first["backoff_until"])
    assert 12 <= (until - T0).total_seconds() <= 18 and "429" in first["errors"][0]
    for tick in range(1, 60 * 30):
        run(conn, feed, T0 + timedelta(seconds=tick), state, rng)
    gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(feed.calls, feed.calls[1:])]
    assert 12 <= gaps[0] <= 18 and 24 <= gaps[1] <= 36 and 48 <= gaps[2] <= 72
    assert max(gaps) <= 300 and min(gaps[5:]) >= 240, "doubling to 300 s, jittered"
    assert len(feed.calls) <= 12, "thirty minutes of 429s cost a dozen requests"
    assert len({round(g, 3) for g in gaps[5:]}) > 1, "jittered, not a fixed beat"
    later = feed.calls[-1][0] + timedelta(seconds=301)
    feed.answer = fixture("in")
    assert run(conn, feed, later, state, rng)["rows"] == 6 and state.failures["espn"] == 0
    feed.answer = (403, "forbidden")
    blocked = run(conn, feed, later + timedelta(seconds=10), state, rng)
    assert 12 <= (datetime.fromisoformat(blocked["backoff_until"]) - later - timedelta(seconds=10)).total_seconds() <= 18


def test_failures_leave_the_state_stale_and_wait_for_the_next_turn(conn):
    live_game(conn)
    feed, state = Feed(SourceError("GET failed: timed out")), PollerState()
    result = run(conn, feed, T0, state)
    assert result["rows"] == 0 and "timed out" in result["errors"][0] and result["backoff_until"] is None
    for tick in range(1, 40):
        run(conn, feed, T0 + timedelta(seconds=tick * 0.25), state)
    assert len(feed.calls) == 3, "a failing game is retried on its cadence (4 s), not every pass"
    feed.answer = (500, "oops")
    assert "answered 500" in run(conn, feed, T0 + timedelta(seconds=12), state)["errors"][0]
    assert conn.execute("SELECT count(*) AS n FROM game_state").fetchone()["n"] == 0


def test_unparseable_summary_falls_back_to_the_scoreboard(conn):
    live_game(conn, "2026_05_PHI_DAL", espn="401800002")
    board = (FIXTURES / "espn_scoreboard.json").read_text()
    feed, state = Feed(lambda url: board if "scoreboard" in url else "<html>maintenance</html>"), PollerState()
    conn.execute("UPDATE settings SET value = %s WHERE key = 'gamestate_max_rps'", (Jsonb(2.0),))
    result = run(conn, feed, T0, state)
    assert result["polled"] == 2 and result["rows"] == 1 and "not understood" in result["errors"][0]
    row = conn.execute("SELECT * FROM game_state").fetchone()
    assert (row["source"], row["status"], row["home_score"], row["away_score"], row["period"]) == ("espn_scoreboard", "in", 14, 20, 3)
    assert run(conn, feed, T0 + timedelta(seconds=4), state)["rows"] == 0, "an unchanged scoreboard state is not stored again"


def test_yahoo_is_opt_in_and_not_fetched_without_a_parser(conn):
    live_game(conn)
    feed, state = Feed(), PollerState()
    conn.execute("UPDATE settings SET value = %s WHERE key = 'gamestate_sources'", (Jsonb(["espn", "yahoo"]),))
    assert run(conn, feed, T0, state)["errors"] == [], "listed without yahoo_pbp_url: ignored"
    conn.execute("UPDATE settings SET value = %s WHERE key = 'yahoo_pbp_url'", (Jsonb("https://yahoo.test/{event_id}"),))
    result = run(conn, feed, T0 + timedelta(seconds=4), state)
    assert "yahoo" in result["errors"][0] and all("yahoo" not in url for _, url in feed.calls)


def test_exchange_loop_runs_gamestate_after_snapshots(pool):
    assert main.ORDER.index("gamestate") == main.ORDER.index("snapshots") + 1 and main.INTERVALS["gamestate"] == 1.0
    results = main.run_once(pool, T0)
    assert results["gamestate"] == {"polled": 0, "rows": 0, "backoff_until": None, "errors": []}


# ------------------------------------------------------------------------- probe

def test_probe_gamestate_never_raises(conn, monkeypatch):
    live_game(conn)
    feed = Feed()
    out = probe.probe_gamestate(conn, "401800001", fetch=feed)
    assert out["status"] == 200 and out["game_id"] == "2026_05_KC_LV" and out["error"] is None
    assert out["url"].endswith("event=401800001") and len(out["parsed"]) == 6 and out["parsed"][-1]["down"] == 2
    big = Feed((200, "x" * 100_000))
    out = probe.probe_gamestate(None, "1", fetch=big)
    assert len(out["payload"]) < 70_000 and out["error"] == "the summary parser extracted nothing from this payload"
    assert probe.probe_gamestate(conn, "1", fetch=Feed(SourceError("refused")))["error"] == "refused"
    assert "yahoo_pbp_url is not set" in probe.probe_gamestate(conn, "1", yahoo=True, fetch=feed)["error"]
    out = probe.probe_gamestate(conn, "9", yahoo=True, url="https://yahoo.test/pbp/{event_id}", fetch=feed)
    assert out["url"] == "https://yahoo.test/pbp/9" and out["source"] == "yahoo" and "no Yahoo parser" in out["parsed"]

    class Broken:
        def execute(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("database gone")

    assert probe.probe_gamestate(Broken(), "1", fetch=feed)["error"] == "database gone"


def test_cli_probe_gamestate_prints_status_payload_and_parsed(test_db_url, monkeypatch, capsys):
    monkeypatch.setattr(gamestate, "default_fetch", Feed())
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    assert cli.main(["probe-gamestate", "--event", "401800001"]) == 0
    out = capsys.readouterr().out
    assert "status: 200" in out and "--- payload (first 64 KiB) ---" in out and '"possession": "away"' in out
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setattr(gamestate, "default_fetch", Feed(SourceError("network unreachable")))
    assert cli.main(["probe-gamestate", "--event", "1", "--yahoo"]) == 0
    out = capsys.readouterr().out
    assert "database: not reachable" in out and "yahoo_pbp_url is not set" in out
