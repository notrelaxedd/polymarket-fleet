"""The fake ESPN of the in-game end-to-end phase (tests/e2e_ingame.py): the fixture
tests/fixtures/espn_summary_in.json served on 127.0.0.1 with the header status, scores
and situation the test sets, so the exchange's real gamestate task polls it over HTTP
(the settings point espn_summary_url and scores_url here). No broadcast, no network.
"""
from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EVENT_ID = "401800001"  # the fixture's ESPN event
ESPN_TEAM = {"home": "13", "away": "12"}  # LV, KC in the fixture


@dataclass(frozen=True)
class Situation:
    period: int = 2
    clock: int = 600
    home: int = 21
    away: int = 0
    possession: str = "home"
    final: bool = False

    def state(self) -> dict[str, Any]:
        """The state dict the parser should store for this payload."""
        live = not self.final
        return {"status": "final" if self.final else "in", "period": self.period, "clock_seconds": self.clock,
                "home_score": self.home, "away_score": self.away, "possession": self.possession if live else None,
                "down": 1 if live else None, "distance": 10 if live else None, "yardline_100": 75 if live else None,
                "home_timeouts": 3 if live else None, "away_timeouts": 3 if live else None}


def summary_payload(template: dict[str, Any], sit: Situation) -> dict[str, Any]:
    """The fixture summary with the header status, scores and situation of `sit`."""
    data = copy.deepcopy(template)
    comp = data["header"]["competitions"][0]
    comp["status"] = {"clock": float(sit.clock), "displayClock": f"{sit.clock // 60}:{sit.clock % 60:02d}",
                      "period": sit.period, "type": {"id": "3" if sit.final else "2",
                                                     "name": "STATUS_FINAL" if sit.final else "STATUS_IN_PROGRESS",
                                                     "state": "post" if sit.final else "in", "completed": sit.final}}
    for team in comp["competitors"]:
        team["score"] = str(sit.home if team["homeAway"] == "home" else sit.away)
    data["situation"] = {} if sit.final else {
        "down": 1, "distance": 10, "yardsToEndzone": 75, "possession": ESPN_TEAM[sit.possession],
        "possessionText": ("LV 25" if sit.possession == "home" else "KC 25"), "homeTimeouts": 3, "awayTimeouts": 3}
    data["drives"], data["scoringPlays"] = {}, []
    return data


class FakeEspn:
    """ESPN's summary and scoreboard on 127.0.0.1: the summary of `situation` (503 while
    `failing`), an empty scoreboard; every path asked is kept."""

    def __init__(self) -> None:
        self.template = json.loads((FIXTURES / "espn_summary_in.json").read_text(encoding="utf-8"))
        self.situation, self.failing, self.paths = Situation(), False, []
        espn = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                espn.paths.append(self.path)
                if self.path.startswith("/summary") and espn.failing:
                    status, body = 503, b"{}"
                elif self.path.startswith("/summary"):
                    status, body = 200, json.dumps(summary_payload(espn.template, espn.situation)).encode()
                else:
                    status, body = 200, b'{"events": []}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, name="e2e-espn", daemon=True)

    def start(self) -> "FakeEspn":
        self.thread.start()
        return self

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
