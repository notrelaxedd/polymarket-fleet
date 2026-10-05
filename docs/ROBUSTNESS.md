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

## Dashboard and docs

Models page columns: validation ROI with its CI, market p, flags; model page Robustness
section; Jobs page gains the `validate` form and the `price_source` choice on backtests;
Settings gains the new keys (search and validation seasons, search_workers, the gate
fields, allow_sim_prices, decision minutes). README: a "Reading a model" guide that
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
