"""One alias table for the 32 NFL teams: nflverse code, city, nickname, full name, old
codes (OAK, SD, STL) and the common variants other feeds use (LAR, JAC, WSH, ...).

`resolve(text)` turns one team name or code into the nflverse code, `find(text)` lists
the codes mentioned in a longer title ("Chiefs vs. Raiders") in order of appearance.
"""
from __future__ import annotations

import re

# code, city, nickname, extra aliases
TEAMS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("ARI", "Arizona", "Cardinals", ("ARZ", "Cards", "Phoenix Cardinals")),
    ("ATL", "Atlanta", "Falcons", ()),
    ("BAL", "Baltimore", "Ravens", ("BLT",)),
    ("BUF", "Buffalo", "Bills", ()),
    ("CAR", "Carolina", "Panthers", ()),
    ("CHI", "Chicago", "Bears", ()),
    ("CIN", "Cincinnati", "Bengals", ()),
    ("CLE", "Cleveland", "Browns", ("CLV",)),
    ("DAL", "Dallas", "Cowboys", ()),
    ("DEN", "Denver", "Broncos", ()),
    ("DET", "Detroit", "Lions", ()),
    ("GB", "Green Bay", "Packers", ("GNB",)),
    ("HOU", "Houston", "Texans", ("HST",)),
    ("IND", "Indianapolis", "Colts", ()),
    ("JAX", "Jacksonville", "Jaguars", ("JAC", "Jags")),
    ("KC", "Kansas City", "Chiefs", ("KAN",)),
    ("LA", "Los Angeles", "Rams", ("LAR", "STL", "St. Louis Rams", "St Louis Rams", "LA Rams")),
    ("LAC", "Los Angeles", "Chargers", ("SD", "SDG", "San Diego Chargers", "LA Chargers")),
    ("LV", "Las Vegas", "Raiders", ("OAK", "LVR", "Oakland Raiders")),
    ("MIA", "Miami", "Dolphins", ("Fins",)),
    ("MIN", "Minnesota", "Vikings", ("Vikes",)),
    ("NE", "New England", "Patriots", ("NWE", "Pats")),
    ("NO", "New Orleans", "Saints", ("NOR", "NOS")),
    ("NYG", "New York", "Giants", ("NY Giants",)),
    ("NYJ", "New York", "Jets", ("NY Jets",)),
    ("PHI", "Philadelphia", "Eagles", ()),
    ("PIT", "Pittsburgh", "Steelers", ()),
    ("SEA", "Seattle", "Seahawks", ("Hawks",)),
    ("SF", "San Francisco", "49ers", ("SFO", "Niners", "San Francisco 49Ers")),
    ("TB", "Tampa Bay", "Buccaneers", ("TAM", "Bucs", "Tampa Bay Bucs", "Tampa")),
    ("TEN", "Tennessee", "Titans", ()),
    ("WAS", "Washington", "Commanders", ("WSH", "Washington Football Team", "Redskins", "Football Team")),
)

CODES: frozenset[str] = frozenset(t[0] for t in TEAMS)

# Codes that are also English words: they count as a team only when written in
# upper case ("NO" the Saints, but not "No" the answer; "WAS", "TEN", "MIN", "CAR",
# "IND"). Outcome words are never a team however they are written.
WORD_CODES: frozenset[str] = frozenset({"NO", "WAS", "TEN", "MIN", "CAR", "IND"})
NON_TEAM_WORDS: frozenset[str] = frozenset({"YES", "OVER", "UNDER", "DRAW", "TIE", "PUSH"})
_WORD = re.compile(r"[A-Za-z0-9]+")


def _scrub(text: str) -> str:
    """Drop the words that must not be read as a team code: outcome words always,
    the word-like codes unless they are upper case in the source text."""
    def keep(match: re.Match[str]) -> str:
        word = match.group(0)
        upper = word.upper()
        if upper in WORD_CODES:
            return word if word == upper else " "
        return " " if upper in NON_TEAM_WORDS else word

    return _WORD.sub(keep, text or "")


def _norm(text: str) -> str:
    """Upper-case, punctuation to spaces, single spaces (word-like codes scrubbed
    unless upper case, see _scrub)."""
    text = re.sub(r"[^A-Za-z0-9]+", " ", _scrub(text or ""))
    return " ".join(text.upper().split())


def _build() -> tuple[dict[str, str], list[tuple[str, str]]]:
    """(unambiguous alias -> code, [(alias, code)] sorted longest first for find)."""
    aliases: dict[str, set[str]] = {}

    def add(alias: str, code: str) -> None:
        aliases.setdefault(_norm(alias), set()).add(code)

    for code, city, nickname, extra in TEAMS:
        add(code, code)
        add(city, code)
        add(nickname, code)
        add(f"{city} {nickname}", code)
        for alias in extra:
            add(alias, code)
    unique = {alias: next(iter(codes)) for alias, codes in aliases.items() if len(codes) == 1 and alias}
    ordered = sorted(unique.items(), key=lambda item: (-len(item[0]), item[0]))
    return unique, ordered


ALIASES, _ORDERED = _build()


def resolve(text: str | None) -> str | None:
    """The nflverse code for one team name, nickname, code or old code; None when
    unknown or ambiguous ("New York", "Los Angeles" alone)."""
    if not text:
        return None
    key = _norm(str(text))
    if not key:
        return None
    code = ALIASES.get(key)
    if code is not None:
        return code
    found = find(key)
    return found[0] if len(found) == 1 else None


def find(text: str | None) -> list[str]:
    """Team codes mentioned in `text`, in order of appearance, each once. Longer
    aliases win over shorter ones ("Los Angeles Rams" before "Rams" or "LA")."""
    if not text:
        return []
    padded = f" {_norm(str(text))} "
    hits: list[tuple[int, str]] = []
    taken = [False] * len(padded)
    for alias, code in _ORDERED:
        needle = f" {alias} "
        start = 0
        while True:
            at = padded.find(needle, start)
            if at < 0:
                break
            if not any(taken[at + 1:at + 1 + len(alias)]):
                for i in range(at + 1, at + 1 + len(alias)):
                    taken[i] = True
                hits.append((at, code))
            start = at + 1
    out: list[str] = []
    for _, code in sorted(hits):
        if code not in out:
            out.append(code)
    return out
