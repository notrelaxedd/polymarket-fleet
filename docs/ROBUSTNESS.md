# Robustness (step 6)

Goal: make a model's numbers trustworthy before it earns paper or live money. Part A
hardens the evaluation we already have; Part B adds the price regime and signals that can
show real edge. Everything stays deterministic for a given seed and standard-library only
on workers (the host may use pandas/pyarrow for ingest).

## Part A

### A1. Held-out validation era

- Settings: `backtest_seasons [2010, 2021]` (the search era keeps this existing key; it is
  not renamed) and `validation_seasons [2022, null]` (null = last complete season). The two must not
  overlap; validation must come after search (with a null last search season, after the
  first search season, so the capped search era is never empty). A job whose eras do not
  resolve cleanly is refused at creation, never run on a fallback era (`host/eras.py`).
- `model_search` evaluates every candidate on the search era only and keeps `top_k` by
  the search score. It then runs the kept candidates on the validation era and stores
  both: `backtest_metrics` (search era, as today) and `validation_metrics` (validation
  era, same shape plus the Part A2 fields). Selection never sees validation numbers.
- `validate` is a new job kind (`{"model_id"}`; role `backtest`): validation-era
  backtest plus A2 and A3, storing `validation_metrics` and `stress_metrics` on the whole
  lineage. The search runs it inline for its top models; the owner can run it from the
  Jobs page for any model whose search era ended before the validation era starts. A
  model whose lineage was searched on a validation-era season (the step 3 default
  `[2010, null]` searched through 2025) is refused (400; the worker refuses it too): its
  validation would be in-sample, so it must be searched again. A `backtest` of a stored
  model, whose result becomes the lineage's search-era metrics, must also end before the
  validation era, so the overfit check below always compares two disjoint eras.
- Overfit flag (stored in `validation_metrics.flags`): `overfit` when the search-era
  shrunk ROI exceeds the validation shrunk ROI by more than 0.03, or when the search era
  beats the market on log-loss (A2 p-value < 0.1) and the validation era does not.
- Leaderboard ranks by the validation era (shrunk ROI then log-loss gain); models without
  validation numbers are listed unranked with "not validated", whatever their paper
  record.

### A2. Confidence intervals and the market test

Computed inside `validate` (and for the search era too, cheaply):
- Bootstrap (B = 1000, `random.Random(f"{seed}:boot")`): resample bets with replacement
  for ROI, average edge, hit rate, average CLV (0 on closing lines; real on snapshots and
  on paper); resample whole seasons with replacement for max drawdown. Store 5th and 95th
  percentiles: `ci: {"roi": [lo, hi], "avg_clv": [lo, hi], "max_drawdown": [lo, hi]}`.
- Market test: per scored game `d = ll_market - ll_model`; `mean_ll_gain = mean(d)`;
  `market_p` = sign-flip permutation p-value (10 000 flips) for `mean(d) > 0`. A model
  "beats the market" when `market_p < 0.05`.
- Calibration: Brier decomposition (reliability, resolution, uncertainty, plus the
  within-bucket variance and covariance of Stephenson et al. 2008 so that brier =
  reliability - resolution + uncertainty + within_variance - within_covariance exactly) and a logistic
  recalibration fit (`calib_slope`, `calib_intercept`), with the ten-bucket table kept.
- Paper: the same bootstrap over a lineage's paper bets gives `paper_ci.avg_clv`.

### A3. Stress tests (stored in `stress_metrics`)

All on the validation era, each a full backtest with one change:
- Prices: half-spread +0.01 and +0.02; taker rate x1.5. Report n_bets, ROI, log-loss.
- Parameter neighbourhood: 10 perturbations, every numeric param scaled by a uniform
  factor in [0.9, 1.1] (`random.Random(f"{seed}:nbhd:{i}")`), clipped to the search
  space. Report the median and the 10th percentile of shrunk ROI and log-loss gain.
- Regimes: favourite vs underdog (market p >= 0.5 for the bet side), home vs away bets,
  divisional vs not, primetime (kickoff 20:00 ET or later) vs day, outdoor cold or windy
  (temp < 35 F or wind >= 15 mph) vs other. Per regime: n_games, n_bets, ROI, log-loss
  gain.
- Flags: `fragile` when the +0.02 spread run keeps fewer than half the base bets or turns
  a positive base ROI negative, or when the neighbourhood median shrunk ROI is below zero
  while the base is above; `regime_dependent` when, in some regime pair with a positive
  total, one side makes the profit and the other side, holding at least 20% of the
  pair's bets, gives back more than half of it (the winning side earns more than twice
  the pair's net). A literal "one regime holds more than 80% of the profit and is
  negative elsewhere" is met by any losing side at all, which flagged nearly every
  profitable model, so the rule asks for a material loss on a material regime.
- Model page: a "Robustness" section with the CI line, the market test, the stress table,
  the neighbourhood summary and the regime table; flags shown as chips on the leaderboard.

### A4. Stricter gates

- `thresholds_backtest` gains `require_validation true`, `min_roi_ci_low 0.0` (validation
  ROI 5th percentile), `max_market_p 0.10`, `forbid_flags ["overfit", "fragile"]`; its
  `min_bets` applies to the validation era and defaults to 50 (the era is ~4 seasons).
- `thresholds_paper` gains `clv_ci_excludes_zero true` (paper CLV 5th percentile above
  0 over at least `min_bets` bets).
- Eligibility recomputes on every validation and settlement, and for every lineage once
  at host startup after the migrations (so a gate made stricter by a migration demotes
  existing lineages, and halts their live assignments, on the first boot); demotion as
  before.

### A5. Multi-core search

- `search_workers` setting: `"auto"` (the CPUs this process may run on, from the
  scheduler affinity mask, minus one, minimum 1) or an integer. The search runner
  evaluates candidates in forked worker processes (`multiprocessing`, stdlib) and hands
  results on in index order, so checkpoints and resume stay exact: a candidate is the
  unit, the checkpoint advances only through completed indices in order. An index is
  handed out only while it is less than `search_workers` past the first unfinished one,
  so at most `search_workers` candidates (running, or finished out of order) are
  repeated on resume after a SIGTERM. Each worker process inherits the games list
  through the fork. A worker that dies (SIGKILL from the OOM killer, a crash) fails the
  job with "search worker died" instead of leaving it waiting forever; a worker whose
  parent dies is killed by the kernel (`PR_SET_PDEATHSIG`), and the agent's SIGKILL of a
  runner reaches every process of its session. `OMP_NUM_THREADS=1` stays. The memory
  watchdog already measures the whole session.
- `validate` and `backtest` stay single-process (short).

## Part B

### B1. Snapshot replay backtests (the price regime that measures real edge)

- Host serves recorded prices to workers: `GET /api/v1/data/prices?since=<iso>&platform=`
  returns 1-minute bars per market (from `price_bars`, plus raw snapshots of the last
  retention window) with the market to game mapping and the frozen closing prices.
  Workers cache it like the games file (ETag).
- Backtest params gain `price_source: "closing_line" | "snapshots"` and
  `decision_minutes_before_kickoff` (default 60). With `snapshots`, a game is scored only
  if it has a confirmed market with bars within 30 minutes of the decision time; the fill
  walks the recorded book (depth and participation like paper), CLV = frozen closing price
  minus entry, fees from `fee_model`. Platform `sim` data is excluded unless
  `allow_sim_prices` is true (testing).
- Metrics are the same object; the leaderboard shows a "snapshot" column group (games,
  bets, ROI, CLV with CI) when a lineage has them, and ranks by snapshot CLV once a
  lineage has 30 snapshot-scored bets.

Delivered in step 6B (host side of B1):
- Job params (`host/jobparams.py`): a backtest takes `price_source` ("closing_line", the
  default when absent, or "snapshots"; stored only when named, so older params stay as
  they were; anything else is a 400, and the key is refused on other kinds). A snapshot backtest also carries the settings in force when it
  was created: `decision_minutes_before_kickoff` (0..300), `allow_sim_prices`,
  `price_platform` (= settings `market_source`) and `participation`; a malformed stored
  value falls back to its default (60, false, 0.5). Deviation: a closing-line backtest
  does not carry these four keys (they mean nothing to it), so its params stay as before.
- Storage (`host/snapshot_store.py`): `POST /api/v1/models/{id}/backtest` routes by the
  job's `price_source`. A snapshot result is stored in `models.snapshot_metrics` on every
  row of the lineage (a child trained later inherits it), logged as the job event
  `model_snapshot_backtest`, and never touches `backtest_metrics` or the status. The
  metrics' own `price_source` must match the job's (400 otherwise), so neither kind of
  result can overwrite the other.
- Leaderboard (`host/leaderboard_snapshot.py`): each entry carries `snapshot` (games,
  bets, ROI, P&L, `avg_clv` with `clv_ci`, platform, games skipped for lack of prices,
  seasons, `score` = shrunk CLV) and `snapshot_score`. Rank basis: paper (unchanged) >
  snapshot (at least 30 snapshot-scored bets with a CLV, sorted by `clv * bets / (bets +
  25)`, ties by snapshot ROI) > validation. Like paper, a snapshot-ranked lineage ranks
  before it is validated; the unranked reasons are unchanged. The CLV point value is the
  result's `avg_clv`; when a result does not report one, the middle of `ci.avg_clv` is
  used (`clv_estimated: true`).
- Eligibility does not read snapshot metrics in this step.
- Seasons: a snapshot backtest whose last season is null (in the request or in settings
  `backtest_seasons`) replays through the latest season in `games`, the season in
  progress included (its played games are scored), not only the last complete one; no
  validation-era cap applies, since a replay selects nothing. The worker plans the same
  way (`season_plan(..., through_latest=True)`) and only replays seasons with at least
  one recorded market.
- The result carries a top-level `avg_clv` (the plain mean CLV over its bets, null
  without bets) next to `ci.avg_clv`, plus `price_source: "snapshots"`, `platform` and
  `n_unscored_no_prices` (also per season). A closing-line result gains none of these
  keys, so it stays byte for byte what it was.

Delivered in step 6B (worker side of B1):
- Prices file (`fleet/worker/context.py`): a backtest with `price_source` "snapshots"
  gets `prices_path` in its context, the body of `GET /api/v1/data/prices?since=
  2000-01-01&platform=<price_platform>` cached at `<state>/cache/prices-<platform>.json`
  with its ETag (conditional GET; the cached copy is used when the host is unreachable).
  The worker refuses platform `sim` unless `allow_sim_prices` is true
  (`SimPricesRefused`, a `ValueError`, so the job fails) and does not even fetch it. An
  unknown `price_source` fails the job too.
- Replay (`fleet/sim/prices.py`, `run_replay_fold` in `fleet/sim/backtest.py`): the
  confirmed markets of the one platform, by game and side. The decision time is kickoff
  minus `decision_minutes_before_kickoff` (clamped to 0..300). A played game is scored
  when a side's market has a bar in the 30 minutes before or at the decision time; the
  last such bar gives that side's mid (its close, else the bid/ask midpoint), its ask and
  its `min_liquidity_usd_cents`. `p_market` is the devigged home mid (one recorded side:
  its own mid, away as `1 - mid`); the model predicts with it and `ll_market` uses it. A
  played game without such a bar counts in `n_unscored_no_prices`; an unplayed one is
  not counted.
- Fill: the side with the larger edge at its ask (home on a tie), sized by the
  closing-line Kelly rule (`fleet/sim/fills.py`) to whole contracts, `floor(stake /
  (cost * 100))`. With a depth snapshot in the 2 minutes before or at the decision time
  the fill walks its ask levels at or below the bar's ask with `fleet.sim.book.walk` at
  `participation` (the settings value copied into the job, default 0.5); otherwise it
  fills at the bar's ask, capped at `floor(participation * min_liquidity_usd_cents /
  (100 * ask))` contracts. The fee is `taker_rate * price * (1 - price)` per contract on
  each fill price; `stake_cents` is the rounded cost of the fills including fees and the
  bet's `edge` is measured at that average cost. CLV = the bought side's closing price
  minus the average entry price, where the closing price is the market's frozen
  `closing_price`, else the mid of its last bar before kickoff. Settlement and P&L are the
  closing-line rule's (a tie returns the stake).
- Seasons: the plan is the closing-line plan through the latest season (see "Seasons"
  above) restricted to seasons with a recorded market for at least one game, so a
  2010-to-now range does not fit empty folds.
- Checkpoint: a snapshot season entry also stores `prices` (the price facts of its
  scored games, in order) and `n_unscored_no_prices`, so a resume rebuilds finished
  seasons without the prices file; a checkpoint from the other price source restarts the
  run.
- Price stress: the spread stress adds its half-spread change to the snapshot entry
  price (`price_bump`) and "fee x1.5" scales `taker_rate`, so `price_stress` works on
  snapshot records; `validate` and `model_search` still run on closing lines only.

Deviations from the B1 text above, as built:
- The fill limit is the bar's ask: recorded depth above that ask (or an empty book)
  fills nothing and the game is scored without a bet; a touch fill without a recorded
  `min_liquidity_usd_cents` also fills nothing.
- Contracts are whole numbers (the trade worker's rule), not the closing-line rule's
  fractional contracts.
- A snapshot backtest result carries no price-stress table yet (the job is a plain
  backtest; the stress tables come from `validate`, which stays on closing lines).
- The prices fetch always asks `since=2000-01-01`, one cache file per platform.

### B2. Richer signals

- Quarterback change: `games` already carries starting quarterbacks. Feature
  `home_qb_changed` / `away_qb_changed` = the starter differs from the team's previous
  game. `elo_blend` gains `qb_change_penalty` (Elo points, search space [0, 80]).
- Injuries: host ingests nflverse `injuries` (free, weekly) into an `injuries` table
  (season, week, team, player, position, report status); features `home_out_qb`,
  `away_out_qb`, `home_out_count`, `away_out_count` (players listed Out) served with the
  games feed; `elo_blend` gains `out_penalty_per_player` (search space [0, 15]).
- Team strength from play-by-play: host ingests nflverse play-by-play per season (CSV.gz,
  free) into `team_game_stats` (season, week, team, offensive EPA per play, defensive EPA
  per play, pass rate, pace, success rate) and serves it with the games feed. New family
  `epa_blend`: logistic regression on [Elo diff, rolling 8-game offensive and defensive
  EPA diffs shrunk to the league mean, rest diff, qb change, outs, divisional, market
  logit], fitted by Newton with L2; search space over window (4..12), shrinkage, L2,
  betting params. Pure Python; the training set is at most a few thousand rows.
- Honest reporting: every new feature is evaluated against the same validation era, CI
  and market test; the three-sentence summary names the signals that carried the result.

Delivered in step 6B (B2), with the details in docs/MODELS.md ("Data", "First family",
"Second family: epa_blend") and docs/PROTOCOL.md ("Step 6 additions (Part B)"):
- Signals: the games feed gives every game `signals` (`home_qb_changed`,
  `away_qb_changed`, `home_out_qb`, `away_out_qb`, `home_out_count`, `away_out_count`)
  and a top-level `team_game_stats` list; the trade state's game carries the same
  `signals` plus `team_stats`. The rules are the pure functions of `fleet/sim/signals.py`,
  used by both the host (`host/signals.py`) and the worker's games.csv adapter.
- Injury signals only count reports whose `date_modified` is strictly before the game's
  decision time (kickoff minus `decision_minutes_before_kickoff`), so a backtest never
  sees a report written after its bet; a row without a date never counts.
- Ingest: `host/ingest_injuries.py` and `host/ingest_pbp.py`, the CLI commands
  `ingest-injuries` and `ingest-pbp` (`--season <year>`, `--season all` or `--file`), and
  a refresh of the current and previous season every `signals_refresh_hours`
  (`host/data_refresh.py`). The host needs no pandas or pyarrow after all: the
  play-by-play file is streamed through the standard library's gzip and csv.
- `elo_blend` penalties and the `epa_blend` family as specified, searched over the
  ranges above and evaluated on the same validation era, CI and market test. The
  `epa_blend` summary names up to three signals that carried it; the `elo_blend`
  summary names its penalties when they are not zero.

Deviations from the B2 text above, as built:
- `qb_changed` compares with the team's last known starter, so a past game with no
  starter id does not reset the history (the text says "previous game"; the two differ
  only there).
- The two `elo_blend` penalties default to 0 and are read with a default, not merged
  into a model's params, so an older model's params and predictions stay exactly as
  they were. They are clipped to their search ranges in the neighbourhood stress.
- `team_game_stats` carries `plays` (offensive plays per game) where the text says
  "pace", plus `kickoff_at` and `game_id`; `injuries` keeps `gsis_id`, `full_name`,
  `game_type` and `date_modified` besides the listed columns (a player without a
  `gsis_id` is keyed `name:<full name>`).
- `epa_blend` choices the text left open: the L2 penalty spares the intercept and the
  market logit (heavy L2 means "the closing line alone"); its Elo runs with fixed
  constants (K 20, home edge 55, regress 0.33, margin scaling on); the league mean
  covers the rows seen in the current and previous season; without a market price
  `predict` returns the Elo expectation; the rolling window is searched over 4 to 12
  games (default 8).
- The games feed ETag also carries `d<decision_minutes_before_kickoff>`, since the
  injury signals depend on it; the prices feed ETag also carries the games ETag and a
  hash of the query.

## Dashboard and docs

Models page columns: validation ROI with its CI, market p, flags; model page Robustness
section; Jobs page gains the `validate` form and the `price_source` choice on backtests;
Settings gains the new keys (search and validation seasons, search_workers, the gate
fields, allow_sim_prices, decision minutes). (Delivered in step 6B: the Models page has a
"snapshot" column with games, bets, ROI and CLV with its 90% range and a "snapshot" rank
chip; the model page a "Snapshot replay" section with a one-tap "Replay on snapshots"
button; the Jobs backtest form a price source select with the replay settings spelled
out and a warning when the market source is sim while sim prices are off; Settings the
"Snapshot replay" group (decision minutes, allow sim prices) and the "nflverse signals"
group (refresh hours, the injuries and play-by-play URL templates, each must contain
`{season}`). See docs/DASHBOARD.md.) README: a "Reading a model" guide that
explains each number in plain words and what a trustworthy model looks like.

## Tests that must exist

Validation era never leaks into selection (property test: altering validation-era scores
does not change which candidates a search keeps); bootstrap and permutation determinism
and sanity (a model equal to the market gives market_p around 0.5; a shuffled-outcome model
never beats it); stress table shapes and flag rules on constructed cases; gates on
boundary values; multi-core search gives byte-identical results to single-process and
resumes exactly after a SIGTERM; snapshot replay scores only games with data, computes CLV
with the right sign, and excludes sim unless allowed; QB-change and injury features on
fixture rows; EPA ingest on a small play-by-play fixture; epa_blend fit recovers known
coefficients on synthetic data and never leaks (same property test as elo_blend).
