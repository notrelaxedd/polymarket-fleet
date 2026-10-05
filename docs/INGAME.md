# In-game trading (step 6 Part C)

Buying and selling while a game is being played needs two things the fleet does not have
yet: a live game-state feed and a model that turns game state into a win probability.
Both are free. The honest caveats come first.

## Caveats

- The live feed is ESPN's unofficial summary endpoint (the scoreboard as a fallback),
  polled every `gamestate_poll_s` (default 4 s) per live game. It can lag the field by
  15 to 60 s. Markets move on the same events faster than the feed, so a naive in-game
  bot is adversely selected: it buys after the market already moved. The rules below
  are conservative for that reason, and in-game orders are paper-only in this step
  (the approval reason for a live in-game order is `ingame_paper_only`; a live
  in-game gate is a later owner decision).
- There are no historical in-game Polymarket prices, so in-game models cannot be
  backtested for edge, only for calibration (against outcomes) and against nflfastR's
  published `vegas_wp` as a market proxy. Paper trading is the real test, scored on
  realized P&L; closing line value is not defined for in-game bets.

## Live game state

The feed ingests plays, not just scores: the ESPN summary endpoint
(`settings.espn_summary_url`, default
`https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={event_id}`)
publishes the drive and play list as the game goes. `host/exchange/gamestate.py` (the
poller) and `host/exchange/gamestate_parse.py` (the parsers) read:

- `header.competitions[0].status` (period, clock, displayClock, type state / completed /
  name) and `competitors[]` (homeAway, score, team id and abbreviation);
- `situation` (down, distance, possession as a team id, possessionText such as
  "LV 38", homeTimeouts, awayTimeouts). `yardLine` alone is not used: its orientation
  is unverified, so the yard line comes from `yardsToEndzone` when present, else from
  possessionText and the team in possession;
- `drives.previous[-1].plays[]` and `drives.current.plays[]` (id, text,
  clock.displayValue, period.number, homeScore and awayScore after the play,
  start {down, distance, yardsToEndzone, team.id}, wallclock). The last finished drive
  is read too, so a scoring play that ends a drive between two polls is not missed.

Each new play becomes a `game_state` row with the situation at its snap and the score
before the play (the nflverse convention the model trains on); the current situation
(header plus `situation`) is stored after the plays whenever it changed or new plays
came in, so the newest row is always the current situation. Rows are `game_state
(game_id, ts (host arrival, UTC), source (espn_summary | espn_scoreboard | yahoo),
event_ts (the play's wallclock), status (pre | in | half | end_period | final), period
(5 and up is overtime), clock_seconds, home_score, away_score, possession (home | away
| null), down, distance, yardline_100, home_timeouts, away_timeouts, play_id,
play_text, raw (the parsed fragment, at most 8 KB))`. Plays are deduplicated by
`(game_id, source, play_id)`. `yardline_100` is the distance to the opponent's end zone
for the team in possession (ESPN's `yardsToEndzone`, nflverse's `yardline_100`).

Which games: an active or halted assignment, kickoff passed (within the last 8 hours),
not final in `games` and not final in their own newest `game_state` row, and an ESPN
event id. The games table has no `espn` column: the id is `games.raw->>'espn'` (the
nflverse `espn` field, indexed by migration 0009). When a summary cannot be parsed the
pass asks the scoreboard (`settings.scores_url`) once for those games
(`parse_scoreboard_states`, source `espn_scoreboard`). Every parser takes text or a
decoded object and returns an empty result on anything it cannot read; it never raises.

`latest_state(conn, game_id)` returns the newest row as `{"state", "ts", "age_s",
"source", "last_change"}`, where `state` has exactly the keys status, period,
clock_seconds, home_score, away_score, possession, down, distance, yardline_100,
home_timeouts, away_timeouts, and `last_change` is the newest score or possession event
(`{"kind", "ts"}`, the time a source first saw it) or null. This is what travels in the
trade state payload as `game_state`. Failures are logged and leave the state stale;
stale state blocks trading (below).

### Cadence, rate cap and backoff

The exchange loop runs the task `gamestate` every second, right after `snapshots`; the
cadence lives inside the poller:

- Each live game is polled every `max(gamestate_poll_s, n_live / gamestate_max_rps)`
  seconds: `gamestate_poll_s` defaults to 4 (clamped to 3..5) and `gamestate_max_rps`
  to 1.0, so one game is polled every 4 s, four games every 4 s, six games every 6 s.
  The game polled longest ago goes first.
- A sliding window caps every ESPN request (summary and scoreboard, across games):
  within any window of max(1, 1 / gamestate_max_rps) seconds there are at most
  gamestate_max_rps times that many requests (at least one). With the default that is
  one request in any second, whatever the number of live games.
- A 429 or 403 backs ESPN off: 15 s, doubling on each further 429 or 403 up to 300 s,
  each delay jittered by -20 % to +20 % (never above 300 s); the first 200 resets it.
  No request goes out while backing off. Any other failure (timeout, 5xx, a payload the
  parser cannot read) is logged and reported in the pass summary, and the game waits
  for its next turn: never a tight retry loop.
- A pass returns `{"polled", "rows", "backoff_until", "errors"}`.

Yahoo is an opt-in cross-check: `gamestate_sources` defaults to `["espn"]`, and Yahoo is
used only when listed and `yahoo_pbp_url` is set. Its payload has not been seen yet, so
there is no Yahoo parser in this step: when it is listed and the URL is set, each pass
reports that the parser awaits the owner's probe and fetches nothing from Yahoo.
NFL.com's live feeds need rotating app tokens and are not used.

## Feed latency: the lag measurement

For every score change and possession change, the host records when each source first
saw it and when the Polymarket price for that game moved: `feed_lag (game_id,
event_kind (score | possession), event_key, event_ts, source, feed_seen_at,
market_moved_at, lag_s)`, one row per event and source (`host/exchange/feedlag.py`).

- Events are read from a source's situation rows, compared with that source's previous
  situation: a score event needs the score to grow (a corrected, lower score is not an
  event), a possession event needs both the old and the new team known (a halftime gap
  is not one). The first observation of a game is no event. Keys are `score:H-A` and
  `possession:<period>:<side>:<H-A>:<n>` (n counts earlier changes with the same
  prefix), so the same event has the same key in every source.
- `market_moved_at` is the first `price_snapshots.ts`, for either confirmed market of the
  game, at or after `(event_ts or feed_seen_at) - 120 s` whose mid differs by more than
  0.03 from that market's last mid before the window start. `lag_s = feed_seen_at -
  market_moved_at` in seconds: positive means the feed is behind the market, negative
  that the feed saw it first. Each pass retries unmeasured rows for 15 minutes; a market
  with no mid before the window cannot be measured.
- `lag_status(conn, source=None)` returns `{"suspended", "median_lag_s", "n",
  "by_source"}` over the last 20 measured rows (of `source` when given, and per source
  in `by_source`). While the median lag exceeds `ingame_max_lag_s` (default 20 s),
  in-game buying is suspended and only the sell and overreaction rules remain active
  (enforced by the trading part).

Watching broadcasts is out of scope: it is slower than the data feeds and against the
streaming services' terms.

## Probing the feed

ESPN's and Yahoo's payloads were not reachable when this was built, so the parsers were
written against fixtures from the documented shapes
(`tests/fixtures/espn_summary_{pre,in,overtime,no_situation,final}.json`). The owner
checks them against a real game:

```
docker compose run --rm exchange python -m host.exchange.cli probe-gamestate --event <espn event id>
docker compose run --rm exchange python -m host.exchange.cli probe-gamestate --event <id> --yahoo --url '<yahoo url with {event_id}>'
```

The command prints the URL, the HTTP status, the first 64 KiB of the payload and what
the parser extracted (one state per play plus the current situation), and the game id
mapped to that event id. It never raises: a network error, a missing database (the
default ESPN template is then used) or an unparseable payload is printed as `error`.
`probe_gamestate(conn, event_id, yahoo=False, url=None)` in `host/exchange/probe.py`
returns the same as a dict for the dashboard.

## In-game model family `ingame_wp`

Training data: nflverse play-by-play per season (CSV.gz, free) reduced on the host to one
row per play (`season, game_id, home_win, score_diff (home perspective), seconds_remaining,
half, down, ydstogo, yardline_100 (distance to the opponent's end zone for the team in
possession, as nflverse and ESPN's yardsToEndzone give it; the model derives the
home-perspective field position from it and posteam_is_home), posteam_is_home, home_timeouts,
away_timeouts, pregame_p_home (devigged closing moneyline), vegas_wp`) in a
`pbp_rows` table and served to workers as a compact feed (2012 onward, roughly 600 000
rows, under 80 MB as JSON lines, cached on the worker).
Model: logistic regression on engineered features (score difference scaled by the square
root of time remaining, logit of the pre-game probability, possession and field position
terms, down and distance, timeout difference, end-of-half indicators), fitted by Newton
with L2 on seasons before a held-out era; `predict(state, pregame_p) -> p_home`.
Validation: log-loss and calibration on the held-out seasons, per period and per score
bucket, against `vegas_wp` as the baseline; the model must not be worse than the baseline
to be usable. Search over feature scales and L2.

## In-game trade rules (trade worker, every `ingame_tick_s`, default 5 s)

- Only when `trade_ingame` is true for the assignment (settings default false), the game
  is in progress, and `game_state` is fresher than `ingame_max_state_age_s` (30 s).
- No trades for `ingame_quiet_seconds` (20 s) after a score change or possession change
  (the market is repricing), none inside the final `ingame_cutoff_seconds` (120 s), and
  none while the model's probability is within `ingame_dead_zone` (0.03) of the price.
- `p_home = ingame_wp.predict(state, pregame_p)`; buy and sell rules as pre-game with
  `ingame_min_edge` (default 0.05) instead of `min_edge`, `ingame_max_bet_cents` as an
  extra cap, GTD of `ingame_gtd_seconds` (60 s), and the model's own in-game assignment
  (an assignment may carry a pre-game model and an in-game model).
- Approval adds: game state fresh, quiet period respected, cutoff respected; everything
  else (kill, bankroll, daily loss, participation, liquidity, mode) is unchanged.
  In-game orders are paper-only in this step: a live in-game order is rejected with
  `ingame_paper_only`.
- Snapshots: active in-game markets are polled every `snapshot_active_s` as now.

## Scoring and dashboard

`bets` rows gain `ingame` (bool) and `state_at_entry` (score, clock); `model_scores` gain
`ingame_n_bets`, `ingame_pnl_cents`; the leaderboard shows an in-game column group; CLV
excludes in-game rows. The Trading page shows live score and clock per assignment, the
in-game model's probability next to the market, and in-game orders flagged.

## Tests

Game-state parser on ESPN fixtures (pre, in, halftime, overtime, missing situation, final,
garbage); the poller's per-game cadence, the global rate cap (six live games on a fake
clock never exceed `gamestate_max_rps`), the jittered backoff on 429 and 403, play
deduplication and the scoreboard fallback; feed_lag events, the market move and the
suspension rule (tests/test_gamestate.py, tests/test_feedlag.py); staleness,
quiet and cutoff rules; model fit recovers known coefficients on synthetic plays and never
leaks (held-out era); calibration against a fixture of plays with `vegas_wp`; trade rules
by hand; approval rejections for stale state and cutoff; paper fills in-game; settlement
with in-game buys and sells; kill cancels in-game orders.
