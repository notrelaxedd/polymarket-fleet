# Models, backtester, search and training (step 3)

Everything here runs on workers in pure Python (standard library only) and is
deterministic for a given seed. Money is integer cents. Probabilities are floats in (0, 1).

## Data

`games` rows come from nflverse `games.csv` (CC BY 4.0; show attribution in the README and
the Models page). The host ingests it and serves it to workers (`GET /api/v1/data/games`).
Fields used by models: `game_id, season, game_type (REG|POST), week, kickoff_at (UTC),
home_team, away_team, home_score, away_score, home_moneyline, away_moneyline, spread_line,
total_line, home_rest, away_rest, div_game, roof, surface, temp, wind`. Team codes are
normalised for continuity: `OAK -> LV`, `SD -> LAC`, `STL -> LA`. Games without both
moneylines are used for Elo updates but never bet on or scored.

Signals (step 6B, docs/ROBUSTNESS.md B2). Every game row also carries `signals`:
`{"home_qb_changed", "away_qb_changed", "home_out_qb", "away_out_qb", "home_out_count",
"away_out_count"}` (ints; all 0 when absent). `qb_changed` is 1 when the team's starting
quarterback for this game differs from its last known starter (across seasons; the
first game of a team and a game whose starter is unknown, such as an unplayed one, read
0). `out_qb` / `out_count` come from the nflverse injury reports: players listed `Out`
whose report was modified before the game's decision time (kickoff minus
`decision_minutes_before_kickoff`; kickoff when unknown); `out_qb` is 1 when one of them
is a quarterback. The host serves them in the games feed; the worker's games.csv
adapter derives the quarterback flags from `home_qb_id` / `away_qb_id` itself (no
injuries there). The pure helpers live in `fleet/sim/signals.py`.

Team stats: the games feed also has a top-level `team_game_stats` list (one row per
team and played game: `game_id, season, week, team, kickoff_at, off_epa_per_play,
def_epa_per_play, pass_rate, plays, success_rate`, from nflverse play-by-play).
`load_games` attaches to every game `team_stats = {"home": [...], "away": [...]}`: that
team's rows with a strictly earlier kickoff, oldest first, at most 16. A cache without
the list (or a plain list of games) loads with empty lists.

Odds: American `o` to implied probability: `|o| / (|o| + 100)` when `o < 0`, else
`100 / (o + 100)`. Devig: `p_home_market = imp_home / (imp_home + imp_away)`.

## Model interface (`fleet/models/base.py`)

```python
class Model:
    family: str                     # registry key, e.g. "elo_blend"
    params: dict                    # hyperparameters (JSON)
    def fit(self, games, through, should_stop) -> None   # games sorted by kickoff; `through`
                                                          # = (season, week) inclusive, or None
    def predict(self, game, market_p, features) -> float # P(home win) given the current
                                                          # market price and a features dict
    def to_json(self) -> dict        # artifact (ratings, coefficients, through); no pickle
    @classmethod
    def from_json(cls, params, artifact) -> "Model"
    @staticmethod
    def search_space(rng) -> dict    # one random params draw for model_search
    @staticmethod
    def summary(params, metrics) -> str   # exactly three sentences, plain text
```
`fleet/models/registry.py`: `FAMILIES = {"elo_blend": EloBlend, "epa_blend": EpaBlend}`.
`features` (`fleet.sim.data.features_of`) is a dict with `home_rest, away_rest, div_game,
roof, surface, temp, wind, week, season, game_type`, plus `signals` (the dict above, all
zeros by default) and `team_stats` (`{"home": [], "away": []}` by default). The trade
worker builds it with the same function from the trade state's game.

## First family: `elo_blend`

Elo: every team starts at 1500 (1999). Per game, expected home win probability
`e = 1 / (1 + 10 ** (-(r_home + hfa + rest_adj - r_away) / 400))` where
`rest_adj = rest_per_day * clamp(home_rest - away_rest, -3, 3)`. After the game, with
`s = 1 (home win), 0.5 (tie), 0` and margin `m = |home_score - away_score|`:
`mult = 1 if mov_scale == 0 else mov_scale * ln(max(m, 1) + 1) * 2.2 / (0.001 * d + 2.2)` where
`d` is the winner's pre-game rating advantage including hfa (0 for ties); the margin is
floored at 1 (as in the 538 reference) so a tie still moves the ratings by `k * ln 2 * (0.5 - e)`;
`delta = k * mult * (s - e)`; `r_home += delta; r_away -= delta`. At each new season every
rating regresses: `r = 1500 + (r - 1500) * (1 - regress)`.

Blend: `logit(p) = a * logit(p_elo) + b * logit(p_market) + c`, with `a, b, c` fitted by
Newton's method on the log-loss of the training games (L2 penalty 1e-3 on a and b, at most
25 iterations, tolerance 1e-8; deterministic). `p_elo` for a training game is the Elo
expectation before that game. The fit uses the finished games with moneylines up to and
including the `through` week (the same window the Elo replay sees), so a child trained
through `(S, 22)` carries exactly the blend the backtest fold testing `S + 1` uses.

Params and search space (uniform draws unless noted):
`k [10, 40]`, `hfa [20, 90]`, `regress [0.1, 0.6]`, `rest_per_day [0, 4]`,
`mov_scale {0, 1}` (coin flip), `min_edge [0.01, 0.08]`, `kelly_fraction [0.1, 0.5]`.
`min_edge` and `kelly_fraction` do not change predictions, only the betting rule.

Signal penalties (step 6B): `qb_change_penalty [0, 80]` Elo points subtracted from a team
whose `qb_changed` is 1, and `out_penalty_per_player [0, 15]` Elo points per player it
lists Out. Their sum enters the home side's pre-game edge (`rest_adj + shift`) in the
expectation the blend is fitted on, in `predict` and in the rating update, so fit and
predict apply the same shift. Both default to 0 and are read with a default rather than
merged into a model's params, so an existing model keeps its params dict and predicts
exactly as before. The search draws them after the original seven draws, so a seed
reproduces the same earlier params. Their search ranges are also clip bounds
(`fleet/models/search_space.py`), so the neighbourhood stress never perturbs a penalty
outside [0, 80] or [0, 15]; a model without the keys is not given them. The summary's
first sentence names them when they are not zero ("QB change costs 40 points, 5.0
points per player Out").
`params_hash` = sha256 of the canonical JSON (sorted keys, 6 decimals), first 16 hex.

Artifact: `{"ratings": {team: float}, "blend": {"a", "b", "c"}, "through": [season, week],
"season": s, "games_seen": n}`. `season` is the season the ratings sit in (the last one
actually replayed, which is before `through[0]` when training through a season that has no
games yet), so a reloaded model applies the between-season regression exactly once;
`from_json` falls back to `through[0]` for an artifact without it. `games_seen` counts
played games only (a schedule row without scores is not learned from).

## Second family: `epa_blend` (step 6B)

`fleet/models/epa_blend.py` with the features in `fleet/models/epa_features.py`. A
logistic regression `logit(p) = w . x` on, per game (every diff is home minus away):

| name | feature |
|---|---|
| `intercept` | 1 |
| `elo` | Elo rating diff / 400 (no home edge; the intercept carries it) |
| `off_epa` | rolling offensive EPA per play diff |
| `def_epa` | rolling defensive EPA allowed per play diff |
| `rest` | `clamp(home_rest - away_rest, -3, 3) / 3` (0 when unknown) |
| `qb_change` | `home_qb_changed - away_qb_changed` |
| `outs` | `home_out_count - away_out_count` |
| `divisional` | `div_game` (0/1) |
| `market` | logit of the devigged home price |

Rolling EPA: the mean of the team's last `window` rows of `team_stats`, shrunk toward the
league mean by `n / (n + shrink)` (`n` rows used; the league mean itself without rows).
The league mean averages every team-game row seen so far in the current and the
previous season; a row is seen once it appears in the `team_stats` of a game already
processed, so it only ever holds games before the predicted one. Elo runs inside the
family with fixed constants (K 20, home edge 55 for its own updates, regress 0.33,
margin scaling on, no rest term).

Fit: the finished moneyline games up to and including `through`, each row built before
that game is learned from, by `fleet.models.newton.fit_logistic` (start: zeros with the
market coefficient at 1; at most 50 iterations, tolerance 1e-9) with the L2 penalty
`l2 * sum w_j^2` on every coefficient except the intercept and the market logit, so a
large `l2` shrinks the model toward "the closing line alone". `predict` without a
market price returns the Elo expectation with its home edge.

Params and search space: `window` int [4, 12], `shrink` [0, 8], `l2` [0.01, 10]
log-uniform, `min_edge` [0.01, 0.08], `kelly_fraction` [0.1, 0.5] (defaults 8, 3, 1,
0.03, 0.25). The backtest's `blend` metric of a fold is the coefficient dict by feature
name. Artifact: `{"features": [...], "coef": [...], "ratings", "season", "games_seen",
"league": {"seasons": {season: [rows, off sum, def sum]}, "last": {team: [kickoff_at,
game_id]}}, "n_train", "through"}`; `from_json(params, to_json())` predicts identically.

Summary: "EPA blend (8-game window, shrink 3.0, L2 1.00, market weight 0.92); the
signals that carried it were offensive EPA (0.16), Elo (0.08) and quarterback change
(0.05) in log-odds per typical gap." A signal carried the result when its coefficient
times a typical home-minus-away gap (100 Elo points, 0.1 EPA per play, 3 days of rest,
one quarterback change, 3 players Out, a divisional game) is at least 0.03 in
log-odds; at most three are named, largest first. Without one it reads "no signal moved
the price by 0.03 in log-odds, so it is close to the closing line alone". The second
and third sentences are the elo_blend ones.

## Backtest (`fleet/sim/backtest.py`)

Walk-forward by season over `seasons = [first, last]` (settings `backtest_seasons`; the
step 3 default `[2010, <last complete season>]` became `[2010, 2021]` in step 6A, and a
null last season resolves to the last complete season, capped before the validation era,
docs/ROBUSTNESS.md A1). For each test season `S`:
1. Replay Elo from 1999 through the end of season `S - 1` (cheap; no stored state needed).
2. Fit the blend on all moneyline games in seasons `< S` (at least 3 seasons of history,
   otherwise skip `S`).
3. Walk season `S` in kickoff order: before each game compute `p = predict(game,
   p_market, features)`, apply the betting rule below, record the outcome, then update Elo
   with the result. The blend is not refitted inside the season.
A test season is one checkpoint unit (well under 3 s on an old i5).

Betting rule ("closing-line fill", the only price we have for history):
- For each side `s` in {home, away}: `price_s = p_market_s + half_spread`;
  `fee_s = taker_rate * price_s * (1 - price_s)`; `cost_s = price_s + fee_s`
  (cents per $1 contract; `fee_model` settings, defaults `taker_rate 0.05`, `half_spread 0.01`).
- `edge_s = p_model_s - cost_s`. Bet the side with the larger edge if `edge >= min_edge`;
  at most one bet per game.
- `bankroll` per game is `default_bankroll_cents` (reset every game). Stake =
  `floor(kelly_fraction * bankroll * edge / (1 - cost))`, capped at `max_bet_cents` and the
  bankroll; contracts = `stake / cost` (float); payout = `contracts * 100` cents if the side
  wins (ties: stake returned), else 0; `pnl = payout - stake`, rounded to cents.
- CLV is 0 by construction here (entry at the close). Backtests on sportsbook closing lines
  measure calibration and discipline, not true edge; the paper phase on live Polymarket
  prices (step 4) and, from step 6B, the snapshot replay below are where CLV is measured.

Metrics (whole backtest and per season): `n_games, n_bets, total_stake_cents, pnl_cents,
roi = pnl / total_stake (0 if no bets), hit_rate, avg_edge, avg_stake_cents, log_loss`
(model `p` vs outcome over every moneyline game, ties count 0.5), `brier, market_log_loss`
(same for `p_market`), `calibration` (10 buckets of `p`: count, mean p, mean outcome),
`max_drawdown_cents` (largest peak-to-trough of cumulative pnl in game order),
`max_drawdown = max_drawdown_cents / (default_bankroll_cents * trade_max_games)` (the
capital one trade worker needs; `null` when that capital is 0, for example while trading is
paused with `trade_max_games 0`, so it never reads as a 0% drawdown and never passes the
eligibility gate), `seasons` (list), `blend` (the fitted `{a, b, c}` of that
season's fold; the whole-backtest value is the last fold's, so `summary` can state the
closing-line weight from params and metrics alone). `n_games` counts scored games (both
moneylines present). Deterministic: no randomness anywhere.

### Snapshot replay (step 6B, `price_source` "snapshots")

A backtest job with `params.price_source = "snapshots"` runs the same walk-forward on
the prices the host recorded instead of the closing line (docs/ROBUSTNESS.md B1 has the
full rule and its deviations; `fleet/sim/prices.py`):
- The worker reads the recorded markets of `params.price_platform` from the context's
  `prices_path` (platform `sim` only with `allow_sim_prices`). A null last season replays
  through the latest season present, the season in progress included, and only seasons
  with a recorded market are planned.
- A played game is scored only when a side's confirmed market has a bar within the 30
  minutes before or at the decision time (kickoff minus
  `decision_minutes_before_kickoff`). `p_market` is the devigged decision-time mid;
  the model predicts with it as usual.
- The bet buys the side with the larger edge at its recorded ask (`cost = ask + fee`,
  no `half_spread`, since the ask already holds the spread), Kelly-sized as above but in
  whole contracts, filled on the recorded book (depth within 2 minutes, at most
  `participation` of each level, never above the ask) or at the ask capped by the bar's
  liquidity. CLV = the bought side's frozen closing price minus the average entry price.
- The result is the same metrics object plus `price_source: "snapshots"`, `platform`,
  `n_unscored_no_prices` and a top-level `avg_clv`; the host stores it in
  `models.snapshot_metrics`, never in `backtest_metrics`, and eligibility ignores it.

## Model search (`fleet/sim/search.py`)

Params: `family, n (default 200), seed, seasons [first, last], top_k (default 5)`.
Candidate `i` draws its params from `random.Random(f"{seed}:{i}")` so resume is exact.
Each candidate runs the full walk-forward backtest; unit = candidate x test season.
Objective: `score = roi * n_bets / (n_bets + 100)` (shrunk ROI), ties broken by lower
`log_loss`. Checkpoint: `{"next": [i, season_index], "current": {partial per-season
metrics of candidate i}, "top": [best candidates so far with params and metrics],
"evaluated": i}`. Result: `{"evaluated": n, "seasons": [...], "top": [...],
"create_models": [{"family", "params", "artifact": null, "backtest_metrics", "summary",
"trained_through": null}]}`; the agent turns `create_models` into `models` rows (see
PROTOCOL step 3) and replaces them with ids in the result it completes with.

## Training (`fleet/sim/train.py`)

Params: `model_id, through {"season", "week"}`. The agent injects the parent model
(params + lineage) into the job context. The job replays Elo through the `through` point
with the parent's params, fits the blend on the finished moneyline games up to and
including it, and returns `create_models: [{"family", "params" (the parent's, verbatim, so
the child keeps the lineage's `params_hash`), "artifact", "parent_model_id",
"trained_through": [season, week]}]`. The host puts the child in the parent's lineage with the lineage's status
and backtest metrics. Unit = one season of Elo replay.

## Summary (three sentences, template per family)

1. What it is: "Elo blend (K 24, home edge 55, 71% weight on the closing line, margin
   scaling on)." The weight is `b / (a + b)` of the fitted blend; when `a <= 0` it reads
   "all weight on the closing line, Elo adds nothing" instead of a percentage.
2. How it bet in the backtest: "Across 2010-2025 it placed 312 bets at an average edge of
   3.4% and returned +2.1% on stake with a 14% max drawdown." A single season reads
   "Across 2019"; with no bets the sentence is "Across 2010-2025 it never found an edge
   above its 3.0% minimum after fees, so it placed no bets." (never "+0.0% on stake"); with
   fewer than 50 bets (the leaderboard gate) it ends "... max drawdown (too few bets to
   judge)."; a null drawdown reads "an unknown max drawdown".
3. Calibration: "Log-loss 0.662 against the market's 0.659; it leans on the market and adds
   little, so treat the edge as unproven until paper trading shows positive CLV." It says
   "it beats the closing line on calibration" only when the log-loss is more than 0.002
   below the market's and (step 6) the era's market test gives p < 0.05; ahead by that
   margin with a larger p it reads "it is ahead of the closing line but not significantly
   (market test p 0.17), so treat the edge as unproven".
The owner can edit the text on the Models page.

## Eligibility (host, `host/eligibility.py`)

Per lineage, recomputed whenever a model row is created or its backtest metrics change:
`candidate -> paper_ok` when the root model's backtest has `n_bets >= min_bets`,
`roi >= min_roi` and `max_drawdown <= max_drawdown` (settings `thresholds_backtest`,
defaults `{"min_bets": 200, "min_roi": 0.02, "max_drawdown": 0.30}`); otherwise it stays
or returns to `candidate`. `paper_ok -> live_eligible` (and back) is decided after every
settlement from the lineage's pooled paper record against `thresholds_paper`
(docs/TRADING.md, "Settlement, bets, scoring, eligibility"). Status is held on every row
of the lineage.

## Leaderboard (step 3 scope)

One row per lineage, using the root model's backtest metrics: rank by shrunk ROI
(`roi * n_bets / (n_bets + 100)`), tie-break log-loss; ranked only if `n_bets >= 50`,
otherwise listed below as unranked. Columns: status, family, short params, ROI, bets,
log-loss vs market, max drawdown, seasons, summary. Step 4 adds the paper (and later
live) record per lineage and the paper rank mode (docs/TRADING.md, "Leaderboard and P&L").
Step 6A ranks on the validation era (docs/ROBUSTNESS.md A1); step 6B adds a snapshot
column group and the snapshot rank mode between paper and validation (at least 30
replayed bets, by shrunk snapshot CLV `clv * bets / (bets + 25)`, ties by snapshot ROI;
`host/leaderboard_snapshot.py`, docs/ROBUSTNESS.md B1). The short params of an
`epa_blend` lineage read "window 8 · shrink 3.0 · L2 1.00".
