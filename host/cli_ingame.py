"""Operator CLI commands for the in-game part (docs/INGAME.md), wired into host.cli."""
from __future__ import annotations

import argparse
from typing import Any

from host import db, pbp_rows
from host.config import Config
from host.errors import BadRequest


def cmd_ingest_pbp_rows(config: Config, args: argparse.Namespace) -> None:
    """Stream nflverse play_by_play_{season}.csv.gz (or --file) into pbp_rows, one
    transaction per season so a failure keeps the seasons already done."""
    lo, hi = pbp_rows.season_range(str(args.season))
    seasons = list(range(lo, hi + 1))
    if args.file and len(seasons) != 1:
        raise BadRequest("--file takes exactly one --season")
    for season in seasons:
        with db.connect(config.database_url) as conn:
            result: dict[str, Any] = pbp_rows.ingest_season(conn, season, args.file)
        print(f"season {season}: {result['rows']} plays in {result['games']} games "
              f"({result['with_pregame']} with a closing moneyline) from {result['source']}: "
              f"{result['inserted']} inserted, {result['changed']} changed")


def add_parsers(sub: Any) -> None:
    """Register the in-game subcommands on host.cli's subparsers."""
    p = sub.add_parser("ingest-pbp-rows", help="stream nflverse play-by-play into pbp_rows (in-game training rows)")
    p.add_argument("--season", required=True, help="a season (2023) or a range (2012-2025)")
    p.add_argument("--file", default=None, help="a local play_by_play_{season}.csv.gz instead of the download")
    p.set_defaults(func=cmd_ingest_pbp_rows)
