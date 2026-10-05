# In-game trading (step 6 Part C)

Buying and selling while a game is being played needs two things: a live game-state feed
and a model that turns game state into a win probability. Step 6C builds both from free
data (ESPN's unofficial endpoints and nflverse play-by-play) and trades them on paper
only. The honest caveats come first.

## Caveats

- The live feed is ESPN's unofficial summary endpoint (the scoreboard as a fallback),
  polled every `gamestate_poll_s` (default 4 s) per live game. It can lag the field by
  15 to 60 s. Markets move on the same events faster than the feed, so a naive in-game
  bot is adversely selected: it buys after the market already moved. The rules below
  are conservative for that reason, and in-game orders are paper-only in this step
  (the approval reason for a live in-game order is `ingame_paper_only`; a live
  in-game gate is a later owner decision).
- There are no historical in-game Polymarket prices, so in-game models cannot be
  backtested for edge, only for calibration (against outcomes) and against the
  `vegas_wp` that nflverse publishes per play as a market proxy. Paper trading is the real test, scored on
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
(header plus `situation`) is stored after the plays on every successful observation
(one row per observation, changed or not), so the newest row is always the current
situation and its age is the time since the last successful poll: a quiet game (a
timeout, a review, a commercial break) stays fresh while the feed answers, and only a
failing feed goes stale. Rows are `game_state
(game_id, ts (host arrival, UTC), source (espn_summary | espn_scoreboard | yahoo),
event_ts (the play's wallclock), status (pre | in | half | end_period | final), period
(5 and up is overtime), clock_seconds, home_score, away_score, possession (home | away
| null), down, distance, yardline_100, home_timeouts, away_timeouts, play_id,
play_text, raw (the parsed fragment, at most 8 KB))`. Plays are deduplicated by
`(game_id, source, play_id)`. `yardline_100` is the distance to the opponent's end zone
for the team in possession (ESPN's `yardsToEndzone`, nflverse's `yardline_100`). A
play's team in possession is its `start.team`, so a kickoff reads as the kicking team
at its own 35 (`yardsToEndzone` 65). nflverse gives the same kickoff to the receiving
team at `yardline_100` 35, so the training rows store every kickoff in the feed's
convention instead (below, "In-game model family").

Which games: an active or halted assignment, kickoff passed (within the last 8 hours),
not final in `games` and not final in their own newest `game_state` row written since
the current kickoff (a game postponed and rescheduled under the same event id is
polled again), and an ESPN event id. The games table has no `espn` column: the id is
`games.raw->>'espn'` (the nflverse `espn` field, indexed by migration 0009). A postponed,
cancelled or forfeited game is final for the feed; a status block without `type.state`
whose name says nothing about play (STATUS_SUSPENDED, STATUS_DELAYED, a new name) is not
understood. When a summary cannot be parsed the game waits for the scoreboard
(`settings.scores_url`, `parse_scoreboard_states`, source `espn_scoreboard`): the
request goes out in the same pass when the window has room, else first in a later
pass, at most once per per-game interval, until it answers 200 or the summary is
understood again. Every parser takes text or a decoded object and returns an empty
result on anything it cannot read; it never raises. Malformed values cannot break a
pass either: a score outside 0..999 is unknown, NUL characters (which Postgres text and
jsonb refuse) are dropped from play ids, play text and raw, a raw fragment with NaN or
Infinity is kept as text, and each game's rows are stored in a savepoint, so a row
the database still refuses costs that game's observation (reported in `errors`) and
never the other games' rows or the pass.

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
- A sliding window caps every ESPN request (summaries, the scoreboard fallback and the
  scores task's scoreboard poll, across games): within any window of
  max(1, 1 / gamestate_max_rps) seconds there are at most gamestate_max_rps times that
  many requests (at least one). With the default that is one request in any second,
  whatever the number of live games. Each request is stamped by the exchange clock at
  the moment it leaves, not at the start of the loop pass, so a slow earlier task
  cannot squeeze two requests into one second.
- The scores task (60 s, docs/TRADING.md "Settlement") shares that window and the
  backoff below (`gamestate.scores_fetch`): it reuses a scoreboard answer of the last
  5 s, else sends its own request when the window and the backoff allow. Otherwise it
  is deferred (reported as `deferred`, not an error), the feed's next pass asks the
  scoreboard before any summary, and the task asks again a second later; a 429 or 403
  it receives backs the whole feed off.
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
  in `by_source`). While the median lag exceeds `ingame_max_lag_s` (default 20 s) over
  at least `ingame_lag_min_events` measured events (default 5), in-game buying is
  suspended and only the sell rules remain active (approval reason
  `ingame_lag_suspended`); with fewer events the feed is not suspended and the
  dashboard says "not enough data".

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

The command prints the source, event_id, game_id (the game mapped to that event id, or
`-`), url and HTTP status, then the first 64 KiB of the payload and what the parser
extracted (one state per play plus the current situation, the current situation last).
It never raises: a network error or an unparseable payload is printed as `error`, and
without a database the default ESPN template is used and a `database` line says so.
`probe_gamestate(conn, event_id, yahoo=False, url=None)` in `host/exchange/probe.py`
returns the same as a dict. The Trading page's exchange block has the same probe as a
"Probe game state" button (`POST /exchange/probe-gamestate`, ESPN only, the event ids of
assigned unfinished games offered as suggestions), rendered by `probe.html`. With
`--yahoo` the payload is printed and `parsed` says that no Yahoo parser exists yet:
paste the payload back so one can be written.

## In-game model family `ingame_wp`

Training data: nflverse play-by-play per season (`play_by_play_{season}.csv.gz`, the
`nflverse_pbp_url` template, free) reduced on the host to one row per play in
`pbp_rows` (`host/pbp_rows.py`): `game_id, play_id, season, home_win (1, 0.5, 0 from the
final score), score_diff (home minus away before the play), seconds_remaining (3600 at
kickoff, 0 at the end of regulation; overtime plays carry the overtime clock), half (1,
2, 3 = overtime), down, ydstogo, yardline_100 (distance to the opponent's end zone for
the team in possession), posteam_is_home, home_timeouts, away_timeouts, pregame_p_home
(the game's devigged closing moneyline from games, null when missing), vegas_wp
(nflverse's vegas_home_wp)`. A play is kept when it has a clock and a down, or is a
kickoff or a timeout. Kickoffs (play_type `kickoff`, onside kicks and safety free kicks
included) are stored in the live feed's convention: the kicking team in possession at
`100 - yardline_100` (a normal kickoff becomes the kicker at 65, a safety free kick the
kicker at 80), so the same kickoff is the same state in training and in trading.

- Ingest: `python -m host.cli ingest-pbp-rows --season 2012-2025` (a season or a range;
  `--file <csv.gz>` with exactly one season) streams each season through gzip and csv,
  never the whole season in memory, and upserts idempotently, one transaction per
  season; a failing season stops the run with `error: ...` and exit 1, the seasons
  before it kept. One line per season: `season 2023: N plays in G games (W with a
  closing moneyline) from <url>: I inserted, C changed`. `pregame_p_home` comes from
  `games`, so ingest the games first. Rows ingested before the kickoff rule need
  `ingest-pbp-rows` again for those seasons (only the kickoff rows change, and the
  feed's ETag changes with them), and existing `ingame_wp` models need a new search.
- Refresh: the host's data thread ingests the current season weekly while a game kicks
  off within 14 days before or 7 days after now (`host/pbp_refresh.py`: the first run 10
  minutes after start, a failure retried after 30 minutes, out of season checked
  daily). Earlier seasons are the CLI's job.
- Worker feed: `GET /api/v1/data/pbp?seasons=A-B` (worker bearer) serves the rows as
  gzip-compressed JSON lines (`application/x-ndjson+gzip`, no Content-Encoding) with an
  ETag `<count>-<max season>-<checksum>` and 304 on `If-None-Match`; the worker caches
  it as `pbp.jsonl.gz` and reads it lazily (`fleet/worker/pbp_cache.py`). An in-game
  search asks for its first train season to its last validation season, an open end
  meaning the current year.

Model (`fleet/models/ingame_wp.py`, docs/MODELS.md): every training row goes through
`state_from_row` into the same state dict the live feed hands the model. Logistic
regression on engineered features (score difference divided by
`(t + 0.01) ** (0.5 * time_scale)` with t the fraction of the game left, logit of the
pre-game probability and its product with t, possession times field position times
`fp_scale`, signed down and distance terms, timeout difference, signed end-of-half
indicators for the last 120 s of each half), fitted by Newton with an L2 penalty of `l2`
per 1000 plays (intercept unpenalised); `predict(state, pregame_p) -> p_home` clamped
to [0.001, 0.999].

Search and validation (`fleet/sim/ingame.py`, `fleet/sim/ingame_eval.py`): a
`model_search` job with family `ingame_wp` (params `train_seasons` default [2012, 2021],
`validation_seasons` default [2022, null], `n` 20, `seed` 1, `top_k` 5,
`train_fraction` 0.3, the share of train games the fit uses) draws `l2` log-uniform in
[0.01, 10] and `time_scale` and `fp_scale` in [0.5, 2.0]. Each candidate is fitted on
train-season games only and ranked by log-loss on the other train-season games; the
kept ones are then scored on the validation seasons, which never reach a fit or the
ranking: log-loss, Brier and calibration overall, per period and per score bucket,
every number against nflverse's `vegas_wp` as the baseline. The result carries
`create_models` like any search, which the agent posts. An ingame_wp lineage is
`paper_ok` when its validation log-loss is at or below vegas_wp's over at least 10 000
plays, otherwise `candidate`, and never `live_eligible` in this step; backtest, validate
and train jobs refuse it (400 on the host, an error on the worker).

## In-game trade rules (trade worker every `ingame_tick_s`, default 5 s)

An assignment may carry a pre-game model (`model_id`) and an in-game model
(`ingame_model_id`, an `ingame_wp` model of a lineage that is not retired) with the
switch `trade_ingame` (new assignments take `settings.trade_ingame`, default false).
In-game trading is on when both are set (docs/TRADING.md, "In-game trading"). Before
kickoff the pre-game rules run as before; once the game has kicked off, the in-game
rules replace them for that assignment (`fleet/worker/trade_ingame.py`), and while an
in-game assignment is live the trade loop ticks at the shorter of `trade_tick_s` and
`ingame_tick_s`.

- Nothing is proposed unless the assignment is active, the game state's status is
  "in", its age is at most `ingame_max_state_age_s` (30 s), at least
  `ingame_quiet_seconds` (20 s) have passed since the last score or possession change
  (the market is repricing), more than `ingame_cutoff_seconds` (120 s) of game time are
  left (regulation `(4 - period) * 900 + clock`, overtime the clock), and the pre-game
  probability is known.
- `p_home = ingame_wp.predict(state, pregame_p_home)`, where `pregame_p_home` is the
  devigged closing moneyline of the game, else the frozen closing price of the home
  market (1 minus the away market's). A market whose `|p_side - mid|` is under
  `ingame_dead_zone` (0.03) gets nothing.
- Buys follow the pre-game rule with `ingame_min_edge` (0.05) instead of `min_edge` and
  `ingame_max_bet_cents` ($5.00) as an extra cap, only while the feed lag does not
  suspend buying; sells follow the step 6B rule with `ingame_min_edge`, also while
  suspended. Open orders whose edge under the current in-game probability is below zero
  are cancelled while the state is in play and fresh.
- Every request carries `"ingame": true` and `"gtd_seconds"`; its `client_request_id` is
  `ingame-` plus 32 hex characters, so it never matches a pre-game id.

Approval (`host/trading/ingame.py`) checks every rule again under the same lock and
transaction as any order. After kickoff the host, not the request, decides what is in
play (`ingame.route`): a request without `"ingame": true` on a game in play is rejected
`kickoff` while `trade_pregame_only` is on, and with it off runs these same in-game
checks (it stays the pre-game model's order, scored on its lineage), so nothing is
approved after kickoff outside the in-game rules and no live order is approved in play.
A pre-game order is always bounded by kickoff, whatever `trade_pregame_only` says: its
GTD ends at kickoff, the executor cancels it at kickoff and the paper simulator never
fills it on a snapshot taken at or after kickoff (docs/TRADING.md). For an in-game
request the pre-game `kickoff` rejection does not apply; after `killed`, `lease`,
`assignment` and `market` come, in order:

1. `ingame_disabled`: `trade_ingame` off, no in-game model, or an in-game model whose
   lineage is retired (retiring the lineage also switches `trade_ingame` off at once,
   audited, cancelling the open in-game orders; the executor catches any it missed on
   every tick);
2. `ingame_paper_only`: a live assignment;
3. `ingame_stale`: no game state, one older than `ingame_max_state_age_s` (exactly at
   the age passes), a status other than "in", a missing period or clock, or a final game;
4. `ingame_quiet`: fewer than `ingame_quiet_seconds` since the last change;
5. `ingame_cutoff`: game seconds left at or below `ingame_cutoff_seconds`;
6. `ingame_lag_suspended` (buys only): the feed lag suspends buying;

then the usual checks: mode, stale book, liquidity, participation, price band, max bet
(also against `ingame_max_bet_cents`), bankroll, daily loss, exposure and buying power
for buys; mode, stale book, participation, price band and the three position checks for
sells. The order is stored with `orders.ingame` true and its approval event records the
game state at approval (`state_at_entry`). The executor gives it a GTD of
`ingame_gtd_seconds` (60 s) from submission, never bounded by kickoff, and the kickoff
cancel skips it; the paper simulator fills it on snapshots taken after kickoff while it
is open. Markets of a game in progress keep their `snapshot_active_s` cadence while an
assignment is active (snapshots run until 6 hours after kickoff or the final). KILL
cancels open in-game orders like every other open order and nothing else; turning
in-game trading off for an assignment cancels its open in-game orders.

In-game orders are paper-only in this step: `trade_ingame` cannot be turned on for a
live assignment (409), approval rejects a live in-game order with `ingame_paper_only`,
and an ingame_wp lineage is never `live_eligible`. A live in-game gate is a later owner
decision.

## Scoring and dashboard

Settlement (docs/TRADING.md, "In-game trading") writes the `bets` row of an in-game order
with `ingame` true, `clv` null (CLV excludes in-game rows) and `state_at_entry`
`{period, clock_seconds, home_score, away_score, possession}` (the state its approval
recorded, else the newest `game_state` row at or before the order), attributed to the
assignment's in-game model, so the ingame_wp lineage gets its own paper record. A sold
contract belongs to the model that bought it: when both of an assignment's models trade
the same market, a sale is matched to the buys it closes and each model's P&L carries
its own contracts (`host/exchange/settle_owners.py`).
`model_scores` gain `ingame_n_bets` and `ingame_pnl_cents`; `n_bets` and `pnl_cents`
keep counting every bet.

Dashboard (docs/DASHBOARD.md): the Trading page shows per assignment the live score and
clock with the state age ("Q3 4:12 · 17-14 · 3 s ago", or "state stale"), the in-game
model's home probability next to the home market mid, the in-game toggle, an `in-game`
chip on in-game orders and fills, the "Probe game state" button and the "In-game feed"
block (per-source lag over the last 20 measured events, "not enough data" below
`ingame_lag_min_events`, and whether buys are suspended). Models lists ingame_wp
lineages in their own "In-game models" table with the validation against vegas_wp and
the in-game paper record; the model page shows the validation per period, per score
bucket and the calibration. Settings has an "In-game" group with every in-game and
game-state setting.

## Tests

`tests/test_gamestate.py` and `tests/test_gamestate_review.py` (the parsers on the ESPN
fixtures, the per-game cadence, the global rate cap on a fake clock, the jittered backoff
on 429 and 403, play deduplication, the scoreboard fallback at the default rate, the
scores task sharing the window, a quiet game staying fresh, malformed values, postponed
games, a rescheduled game), `tests/test_feedlag.py` (events, the market move, the
suspension rule), `tests/test_pbp_rows.py` (the ingest and the feed),
`tests/test_ingame_wp.py`, `tests/test_ingame_search.py`, `tests/test_ingame_job.py` and
`tests/test_ingame_model_review.py` (the fit, the held-out era, the job end to end, the
kickoff convention, a refit replacing the stored artifact),
`tests/test_ingame_models_host.py` and `tests/test_ingame_settings.py` (job params,
eligibility, the leaderboard, the settings validators), `tests/test_trade_ingame.py`
(the worker rules by hand), `tests/test_ingame_approval.py` and
`tests/test_ingame_state.py` (approval rejections in order, the GTD, paper fills after
kickoff, the trade state payload), `tests/test_ingame_assignments.py` (the assignment
rules, kill cancels in-game orders), `tests/test_ingame_settlement.py` (in-game buys and
sells settled to the cent), `tests/test_ingame_dashboard.py`,
`tests/test_ingame_integration.py`, and the in-game phase of `tests/test_e2e.py`
(`tests/e2e_ingame.py`: a real worker search, a fake ESPN polled over TCP, paper only,
an in-game buy filled after kickoff, stale, quiet, kill, cutoff and settlement).
