# In-game trading (step 6 Part C)

Buying and selling while a game is being played needs two things the fleet does not have
yet: a live game-state feed and a model that turns game state into a win probability.
Both are free. The honest caveats come first.

## Caveats

- The live feed is ESPN's unofficial scoreboard and summary endpoints, polled every 10 s.
  It can lag the field by 15 to 60 s. Markets move on the same events faster than the
  feed, so a naive in-game bot is adversely selected: it buys after the market already
  moved. The rules below are conservative for that reason, and in-game trading starts in
  paper mode like everything else.
- There are no historical in-game Polymarket prices, so in-game models cannot be
  backtested for edge, only for calibration (against outcomes) and against nflfastR's
  published `vegas_wp` as a market proxy. Paper trading is the real test, scored on
  realized P&L; closing line value is not defined for in-game bets.

## Live game state

The feed ingests plays, not just scores: the ESPN summary endpoint publishes the drive
and play list as the game goes (`drives.current.plays[]` with the clock, down, distance,
yard line, play text and the score after the play). Each new play becomes a `game_state`
row, so the in-game model sees the exact situation it was trained on; the scoreboard
endpoint is the fallback when the summary has no plays yet.

`host/exchange/gamestate.py` polls ESPN for every game with an active assignment from
kickoff until final: scoreboard for status, period, clock and scores; the summary endpoint
(`.../summary?event=<espn id>`) for `situation` (possession, down, distance, yard line,
timeouts). Rows go to `game_state (game_id, ts, period, clock_seconds, home_score,
away_score, possession, down, distance, yardline_100, home_timeouts, away_timeouts,
source, raw)`; the latest row and its age travel in the trade state payload as
`game_state`. Mapping to `game_id` uses the ESPN event id already present in the nflverse
games row (`espn` column). Failures are logged and leave the state stale; stale state
blocks trading (below).

## Feed latency: two sources and a lag measurement

Two free play feeds are polled, ESPN and NFL.com (`nfl.com` live game JSON), every 3 to 5
seconds while a game is on; whichever reports a play first wins, and each `game_state`
row records its source and arrival time. For every scoring play and possession change
the host also records when the Polymarket price for that game first moved by more than
3 cents after the event (`feed_lag (game_id, event_ts, source, feed_seen_at,
market_moved_at)`), so the Trading page can show, per source, whether the fleet sees
events before, with, or after the market. When the measured lag is behind the market by
more than `ingame_max_lag_s` (default 20 s) over the last 20 events, in-game buying is
suspended automatically and only the sell and overreaction rules remain active.
Watching broadcasts is out of scope: it is slower than the data feeds and against the
streaming services' terms.

## In-game model family `ingame_wp`

Training data: nflverse play-by-play per season (CSV.gz, free) reduced on the host to one
row per play (`season, game_id, home_win, score_diff (home perspective), seconds_remaining,
half, down, ydstogo, yardline_100 (home perspective), posteam_is_home, home_timeouts,
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
- Snapshots: active in-game markets are polled every `snapshot_active_s` as now.

## Scoring and dashboard

`bets` rows gain `ingame` (bool) and `state_at_entry` (score, clock); `model_scores` gain
`ingame_n_bets`, `ingame_pnl_cents`; the leaderboard shows an in-game column group; CLV
excludes in-game rows. The Trading page shows live score and clock per assignment, the
in-game model's probability next to the market, and in-game orders flagged.

## Tests

Game-state parser on ESPN fixtures (pre, in, post, overtime, missing situation); staleness,
quiet and cutoff rules; model fit recovers known coefficients on synthetic plays and never
leaks (held-out era); calibration against a fixture of plays with `vegas_wp`; trade rules
by hand; approval rejections for stale state and cutoff; paper fills in-game; settlement
with in-game buys and sells; kill cancels in-game orders.
